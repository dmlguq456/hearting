#!/usr/bin/env python3
"""Per-repository steward grant limit and its peer-steward wiring.

Sessions, repositories and registry records are injected or built in a temporary
home; the periodic-check module is replaced by a recording fake.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import fcntl
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_HERE = Path(__file__).resolve().parent
_TOOLS = str(_HERE.parent / "tools")
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(_HERE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


peer_message = _load("peer_message_cap", "peer-message.py")
peer_steward = _load("peer_steward_cap", "peer-steward.py")
from fleet.collectors import steward as fleet_steward                   # noqa: E402

TS = "2026-10-27T00:00:00Z"
EXPLICIT_TO = {"harness": "unknown", "name": "-"}


def _git(*argv):
    subprocess.run(["git", *argv], check=True, capture_output=True, text=True)


class _FakeStewardCheck(types.ModuleType):
    def __init__(self):
        super().__init__("steward_check")
        self.calls = []

    def _record(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return 0
        return call

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self._record(name)


class GrantFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="peer-steward-cap-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.base = base
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in tuple(os.environ):
            if key.startswith(("AGENT_", "HERDR_", "CLAUDE", "CODEX", "OPENCODE")):
                os.environ.pop(key, None)
        home = base / "home"
        os.environ.update({
            "HOME": str(home), "XDG_STATE_HOME": str(base / "xdg-state"),
            "XDG_CONFIG_HOME": str(base / "xdg-config"), "XDG_DATA_HOME": str(base / "xdg-data"),
            "AGENT_PEER_LEDGER_ROOT": str(base / "peer-state"),
            "AGENT_DISPATCH_JOBS": str(base / "jobs.log"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "FLEET_SESSION_REGISTRY_DIR": str(base / "registry"),
            "CODEX_HOME": str(home / ".codex"),
            "OPENCODE_CONFIG_DIR": str(home / ".config" / "opencode"),
            "DISPATCH_DEFAULTS_CONFIG": str(base / "dispatch-defaults.yaml"),
        })
        (base / "jobs.log").touch()
        self.primary, self.linked, self.other = base / "alpha", base / "alpha-wt", base / "beta"
        for repo in (self.primary, self.other):
            _git("init", "-q", str(repo))
            _git("-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                 "commit", "-q", "--allow-empty", "-m", "fixture")
        _git("-C", str(self.primary), "worktree", "add", "-q", "-b", "linked", str(self.linked))
        self.rows = []
        for patcher in (mock.patch.object(fleet_steward, "_projection_sessions",
                                          side_effect=lambda: list(self.rows)),
                        mock.patch.object(fleet_steward, "_registry_sessions",
                                          side_effect=lambda: list(self.rows)),
                        mock.patch("fleet.collectors.herdr.list_agents", return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.check = _FakeStewardCheck()
        patcher = mock.patch.dict(sys.modules, {"steward_check": self.check})
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self, sid, cwd, harness="claude"):
        self.rows.append(SimpleNamespace(harness=harness, session_id=sid, cwd=str(cwd)))
        return sid

    def grant(self, sid, source="explicit", to=EXPLICIT_TO, **kw):
        return peer_message.grant_steward("claude", sid, dict(to), source if source != "explicit"
                                          else "explicit", TS, source=source, **kw)

    def marker_exists(self, sid):
        return peer_message.steward_marker_path("claude", sid).exists()

    def settings(self, limit=None, interval=None):
        shipped = (_HERE.parent / "profiles" / "dispatch-defaults.yaml").read_text(encoding="utf-8")
        if limit is not None:
            shipped = re.sub(r"(?m)^(\s+max_supervisors_per_repo:\s*)\d+", rf"\g<1>{limit}", shipped)
        if interval is not None:
            shipped = re.sub(r"(?m)^(\s+check_interval_seconds:\s*)\d+", rf"\g<1>{interval}", shipped)
        Path(os.environ["DISPATCH_DEFAULTS_CONFIG"]).write_text(shipped, encoding="utf-8")


class GrantLimitTest(GrantFixture):
    def test_limited_names_only_counted_holders_and_never_shows_seat_hash(self):
        holders = [dict(name=name, harness='claude', session_id=name, counted=counted,
                        seat_key='0123456789abcdef', liveness=live)
                   for name, counted, live in (('current', True, 'live'),
                       ('uncertain', True, 'unknown'), ('ended', False, 'ended'),
                       ('prior-seat', False, 'unknown'))]
        line = peer_message.format_steward_limited(dict(holders=holders, counted=2, limit=2))
        self.assertIn('holders=current,uncertain limit=2', line.splitlines()[0])
        self.assertNotIn('0123456789abcdef', line)
        self.assertNotIn('seat=', line)

    def fill(self, *sids, cwd=None):
        for sid in sids:
            self.session(sid, cwd or self.primary)
            self.assertEqual(self.grant(sid)["status"], "granted", sid)

    def test_second_supervisor_is_allowed_and_third_is_limited(self):
        self.fill("s1", "s2")
        self.session("s3", self.primary)
        result = self.grant("s3")
        self.assertEqual((result["status"], result["written"], result["limit"]), ("limited", False, 2))
        self.assertEqual(sorted(h["session_id"] for h in result["holders"]), ["s1", "s2"])
        self.assertFalse(self.marker_exists("s3"))
        line = peer_message.format_steward_limited(result)
        self.assertTrue(line.startswith("steward=limited repo=alpha holders="), line)
        self.assertIn("limit=2", line)
        self.assertIn("liveness=unknown", line)
        self.assertIn("last_activity=", line)
        self.assertIn("steward off", line)

    def test_linked_worktree_session_counts_against_the_primary(self):
        self.fill("s1")
        self.fill("s2", cwd=self.linked)
        self.session("s3", self.linked)
        self.assertEqual(self.grant("s3")["status"], "limited")
        self.session("s4", self.other)
        self.assertEqual(self.grant("s4")["status"], "granted")        # another repository

    def test_regrant_by_the_same_session_is_extended_and_uses_no_slot(self):
        self.fill("s1", "s2")
        self.assertEqual(self.grant("s1")["status"], "extended")
        self.assertEqual(self.grant("s1", source="watch",
                                    to={"harness": "codex", "session_id": "t1", "name": "t1"})["status"],
                         "extended")
        self.session("s3", self.primary)
        self.assertEqual(self.grant("s3")["status"], "limited")
        self.assertEqual(fleet_steward.repo_supervision(
            repository_key=fleet_steward._repository_key(str(self.primary)))["counted"], 2)

    def test_confirmed_handover_successor_extends_instead_of_taking_a_slot(self):
        self.fill("s1", "s2")
        self.session("succ", self.primary)
        relations = [{"generation": "g1", "released": False,
                      "origin": {"harness": "claude", "sid": "s1"},
                      "holder": {"harness": "claude", "sid": "succ"},
                      "targets": [dict(EXPLICIT_TO, session_id=None, kind="explicit",
                                       source="explicit")]},
                     {"generation": "g2", "released": False,
                      "origin": {"harness": "claude", "sid": "s2"},
                      "holder": {"harness": "claude", "sid": "s2"},
                      "targets": [dict(EXPLICIT_TO, session_id=None, kind="explicit",
                                       source="explicit")]}]
        with mock.patch.object(fleet_steward, "read_relations", return_value=relations):
            result = self.grant("succ", source="watch",
                                to={"harness": "codex", "session_id": "t1", "name": "t1"})
        self.assertEqual((result["status"], result["written"]), ("extended", True))

    def test_ended_holder_frees_a_slot_and_revival_only_marks_over_limit(self):
        self.fill("s1", "s2")
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        sessions = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "sessions"
        sessions.mkdir(parents=True)
        record = sessions / f"{dead.pid}.json"
        record.write_text(json.dumps({"pid": dead.pid, "sessionId": "s1"}), encoding="utf-8")
        self.session("s3", self.primary)
        self.assertEqual(self.grant("s3")["status"], "granted")
        record.unlink()                                  # s1 is no longer proven ended
        census = fleet_steward.repo_supervision(
            repository_key=fleet_steward._repository_key(str(self.primary)))
        self.assertEqual((census["counted"], census["over_limit"]), (3, True))
        self.assertTrue(self.marker_exists("s1"))        # nothing is withdrawn
        self.session("s4", self.primary)
        self.assertEqual(self.grant("s4")["status"], "limited")

    def test_repository_unknown_is_granted_without_a_repository_record(self):
        result = self.grant("ghost")
        self.assertEqual((result["status"], result["reason"], result["written"]),
                         ("granted", "repository-unknown", True))
        self.assertNotIn("repository", json.loads(
            peer_message.steward_marker_path("claude", "ghost").read_text()))

    def test_foreign_target_is_communication_and_skips_the_census(self):
        self.fill("s1", "s2")
        self.session("s3", self.primary)
        to = {"harness": "codex", "session_id": "t1", "name": "t1"}
        result = self.grant("s3", source="watch", to=to, target_cwd=str(self.other))
        self.assertEqual((result["status"], result["reason"]), ("granted", "foreign-target"))
        self.assertTrue(self.marker_exists("s3"))
        # The foreign watch made no supervisor of s3: a same-repository watch is a new grant.
        self.assertEqual(self.grant("s3", source="watch", to=to,
                                    target_cwd=str(self.linked))["status"], "limited")

    def test_grant_records_the_repository_on_the_marker(self):
        self.fill("s1")
        data = json.loads(peer_message.steward_marker_path("claude", "s1").read_text())
        self.assertEqual(data["repository"], fleet_steward._repository_key(str(self.primary)))

    def test_unavailable_census_refuses_a_new_holder_without_writing(self):
        self.session("s1", self.primary)
        with mock.patch.object(fleet_steward, "repo_supervision", side_effect=RuntimeError("boom")):
            result = self.grant("s1")
        self.assertEqual((result["status"], result["written"]), ("unavailable", False))
        self.assertFalse(self.marker_exists("s1"))

    def test_mark_steward_keeps_its_bool_contract(self):
        self.fill("s1", "s2")
        self.session("s3", self.primary)
        self.assertFalse(peer_message.mark_steward("claude", "s3", EXPLICIT_TO, "explicit", TS,
                                                   source="explicit"))
        self.assertTrue(peer_message.mark_steward("claude", "s1", EXPLICIT_TO, "explicit", TS,
                                                  source="explicit"))
        self.assertFalse(peer_message.mark_steward("claude", "s1", EXPLICIT_TO, "explicit", TS))


class ConcurrentGrantTest(GrantFixture):
    def test_simultaneous_grants_never_pass_the_limit(self):
        self.session("s0", self.primary)
        self.assertEqual(self.grant("s0")["status"], "granted")
        sids = [self.session(f"c{i}", self.primary if i % 2 else self.linked) for i in range(4)]
        barrier = threading.Barrier(len(sids))
        real = fleet_steward.repo_supervision

        def slow(*args, **kwargs):
            time.sleep(0.05)                 # widen the race window between census and write
            return real(*args, **kwargs)

        def run(sid):
            barrier.wait()
            return self.grant(sid)["status"]

        with mock.patch.object(fleet_steward, "repo_supervision", side_effect=slow):
            with ThreadPoolExecutor(max_workers=len(sids)) as pool:
                statuses = list(pool.map(run, sids))
        self.assertEqual(statuses.count("granted"), 1, statuses)
        self.assertEqual(statuses.count("limited"), 3, statuses)
        self.assertEqual(sum(self.marker_exists(sid) for sid in sids), 1)

    def test_grant_lock_timeout_is_unavailable_and_writes_nothing(self):
        self.session("s1", self.primary)
        real = peer_message._steward_grant_lock
        directory = peer_message.peer_state_root() / "peer-steward"
        directory.mkdir(parents=True)
        fd = os.open(directory / "grant.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with mock.patch.object(peer_message, "_steward_grant_lock",
                               lambda wait=10.0: real(wait=0.2)):
            result = self.grant("s1")
        self.assertEqual((result["status"], result["reason"], result["written"]),
                         ("unavailable", "grant-lock-timeout", False))
        self.assertFalse(self.marker_exists("s1"))


class SettingsConsumptionTest(GrantFixture):
    def test_limit_read_back_from_the_settings_file(self):
        self.settings(limit=3)
        for sid in ("s1", "s2", "s3"):
            self.session(sid, self.primary)
            self.assertEqual(self.grant(sid)["status"], "granted", sid)
        self.session("s4", self.primary)
        self.assertEqual(self.grant("s4")["status"], "limited")
        self.settings(limit=1)                           # the next grant reads the new value
        self.session("s5", self.other)
        self.assertEqual(self.grant("s5")["status"], "granted")
        self.session("s6", self.other)
        result = self.grant("s6")
        self.assertEqual((result["status"], result["limit"]), ("limited", 1))

    def test_unreadable_settings_fall_back_to_the_shipped_limit(self):
        Path(os.environ["DISPATCH_DEFAULTS_CONFIG"]).write_text("not: [valid", encoding="utf-8")
        self.assertEqual(fleet_steward._steward_limit(), 2)


class PeerStewardWiringTest(GrantFixture):
    def identity(self, sid, harness="claude"):
        return mock.patch.object(peer_steward, "_current_session_identity",
                                 return_value=(sid, harness))

    def prefill(self):
        for sid in ("s1", "s2"):
            self.session(sid, self.primary)
            self.assertEqual(self.grant(sid)["status"], "granted")
        self.session("s3", self.linked)

    def test_steward_on_reports_limited_on_stdout_with_rc_1(self):
        self.prefill()
        out = io.StringIO()
        with self.identity("s3"), contextlib.redirect_stdout(out):
            rc = peer_steward.cmd_steward(SimpleNamespace(state="on"))
        self.assertEqual(rc, 1)
        self.assertTrue(out.getvalue().startswith("steward=limited repo=alpha "), out.getvalue())
        self.assertFalse(self.marker_exists("s3"))
        out = io.StringIO()
        with self.identity("s1"), contextlib.redirect_stdout(out):
            self.assertEqual(peer_steward.cmd_steward(SimpleNamespace(state="on")), 0)
        self.assertIn("steward=on harness=claude session_id=s1", out.getvalue())

    def test_watch_and_wait_path_prints_limited_on_stderr_only(self):
        self.prefill()
        out, err = io.StringIO(), io.StringIO()
        with self.identity("s3"), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            peer_steward._mark_observed("codex", "t1", "t1", pane="w1:p1", target_cwd=str(self.primary))
        self.assertEqual(out.getvalue(), "")
        self.assertTrue(err.getvalue().startswith("steward=limited repo=alpha "), err.getvalue())
        self.assertFalse(self.marker_exists("s3"))

    def test_start_path_prints_limited_on_stderr_and_still_succeeds(self):
        self.prefill()
        args = SimpleNamespace(kind="codex", name="worker", pane="w1:p2", cwd=str(self.primary))
        out, err = io.StringIO(), io.StringIO()
        with self.identity("s3"), mock.patch.object(peer_steward, "_record"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            peer_steward._mark_started(args, "t2", "bypass")
        self.assertEqual(out.getvalue(), "")
        self.assertIn("steward=limited", err.getvalue())
        self.assertFalse(self.marker_exists("s3"))

    def test_grant_registers_the_check_and_release_settles_it(self):
        self.session("s1", self.primary)
        self.assertEqual(self.grant("s1")["status"], "granted")
        self.assertEqual([c[:2] for c in self.check.calls], [("register", ("claude", "s1"))])
        self.assertEqual(self.grant("s1")["status"], "extended")
        self.assertEqual(len(self.check.calls), 2)             # extension re-registers idempotently
        self.session("s9", self.primary)
        self.settings(limit=1)
        self.assertEqual(self.grant("s9")["status"], "limited")
        self.assertEqual(len(self.check.calls), 2)             # a refused grant schedules nothing
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(peer_message.cmd_release(
                SimpleNamespace(harness="claude", session_id="s1")), 0)
        name, args, kwargs = self.check.calls[-1]
        self.assertEqual((name, args, kwargs), ("settle_session", ("claude", "s1"),
                                                {"reason": "role-released"}))

    def test_missing_check_module_never_fails_a_grant(self):
        with mock.patch.dict(sys.modules, {"steward_check": None}):
            self.session("s1", self.primary)
            self.assertEqual(self.grant("s1")["status"], "granted")
            with contextlib.redirect_stdout(io.StringIO()) as out, self.identity("s1"):
                self.assertEqual(peer_steward.cmd_steward(SimpleNamespace(state="check")), 1)
        self.assertEqual(out.getvalue().strip(), "steward-check=unavailable reason=module-missing")

    def test_steward_check_command_delegates_to_the_schedule_module(self):
        with self.identity("s1"):
            self.assertEqual(peer_steward.cmd_steward(SimpleNamespace(state="check")), 0)
        self.assertEqual(self.check.calls[-1][:2], ("cmd_check", ("claude", "s1")))
        parsed = peer_steward.build_parser().parse_args(["steward", "check"])
        self.assertEqual(parsed.state, "check")

    def test_runner_selects_steward_check_duties_and_advances_them(self):
        duty = {"id": "steward-check-claude-s1", "state": "pending",
                "intent": {"kind": "steward-check"}}
        other = {"id": "x-1", "intent": {"kind": "unrelated"}}
        store = mock.MagicMock()
        store.list.return_value = []
        store.root = Path(self.base)
        self.assertEqual(peer_steward._runner_duties(store, [duty, other]), [duty])
        peer_steward._reconcile_obligations(store, [duty], read_context=None)
        self.assertEqual(self.check.calls[-1][0], "advance")
        self.assertIs(self.check.calls[-1][1][0], duty)


if __name__ == "__main__":
    unittest.main()
