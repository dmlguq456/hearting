#!/usr/bin/env python3
"""A closed session must not keep a supervisor seat: the 10-30/11-01 cases, before and after.

Each case drives the real stack — marker and seat-ledger files under a private
``XDG_STATE_HOME``, a fake ``/proc`` for the Claude census, the real ``grant_steward``
limit and the real retire / watch code. Only the outside observations are stubbed: the
herdr agent list, the Codex rollout table, the CLI pane census and process spawning.

A case that shows ``limited`` on the baseline commit and ``granted`` after the fix is
the whole point; the ``control`` cases pin what must NOT change (an ambiguous holder
keeps counting, a live session keeps its role). Run directly if the collector cannot
gather this file: ``python3 tools/fleet/tests/test_session_close_supervision.py``.
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_UTILITIES_DIR = os.path.join(os.path.dirname(_TOOLS_DIR), "utilities")
for _path in (_UTILITIES_DIR, _TOOLS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import claude_process_identity                                          # noqa: E402
import dispatch_seat_handover as handover                               # noqa: E402
import herdr_observation                                                # noqa: E402
import peer_obligations                                                 # noqa: E402
import session_tidy as tidy                                             # noqa: E402
import steward_check as SC                                              # noqa: E402
from fleet.collectors import codex, herdr, steward                      # noqa: E402
from split_module_fixture import patch_split                            # noqa: E402

# The patched module attribute replaces ``claude_session_census``; keep the real one.
_REAL_CENSUS = claude_process_identity.claude_session_census
SELF_EXPLICIT = {"harness": "unknown", "session_id": None, "name": "-"}
NOW = "2026-11-02T00:00:00Z"
_TMP_ROOT = os.environ.get("TMPDIR")


def sid(label):
    """A full session id whose first eight characters are ``label``."""
    return f"{label}-1111-4222-8333-444444444444"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_UTILITIES_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _digest(label):
    return hashlib.sha256(label.encode()).hexdigest()[:16]


class CloseSupervisionBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="close-supervision-", dir=_TMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        (self.base / "home").mkdir()
        env = {"HOME": str(self.base / "home"), "XDG_STATE_HOME": str(self.base / "state"),
               "AGENT_PEER_LEDGER_ROOT": str(self.base / "peer"),
               "CLAUDE_CONFIG_DIR": str(self.base / "claude-home"),
               "FLEET_SESSION_REGISTRY_DIR": str(self.base / "registry"),
               "CODEX_HOME": str(self.base / "codex-home"),
               "AGENT_DISPATCH_JOBS": str(self.base / "jobs.log"),
               "DISPATCH_DEFAULTS_CONFIG": str(self.base / "no-defaults.yaml"),
               # A private HOME hides the global safe.directory entry of a shared mount.
               "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
               "GIT_CONFIG_VALUE_0": "*"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in [key for key in os.environ if key.startswith(("HERDR_", "AGENT_HERDR"))]:
            del os.environ[name]
        (self.base / "claude-home").mkdir()
        (self.base / "jobs.log").write_text("")
        self.repo = self.base / "alpha"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=F", "-c", "user.email=f@x.invalid",
                        "commit", "-q", "--allow-empty", "-m", "fixture"], check=True, capture_output=True)
        self.key = steward._repository_key(str(self.repo))
        self.pm = _load("close_supervision_peer_message", "peer-message.py")
        self.ps = _load("close_supervision_peer_steward", "peer-steward.py")
        self.proc = self.base / "proc"
        (self.proc / "self" / "ns").mkdir(parents=True)
        os.symlink("pid:[4026531836]", self.proc / "self" / "ns" / "pid")
        self.rows, self.agents = [], []
        self.codex_seen = {"complete": True, "open": frozenset()}
        self.codex_rollouts = set()
        self.panes = None
        self.seat_of = {}
        for target, name, replacement in (
                (steward, "_projection_sessions", lambda: list(self.rows)),
                (herdr, "list_agents", lambda: list(self.agents)),
                (codex, "open_rollout_sessions", lambda: dict(self.codex_seen)),
                (codex, "exact_rollout_for_session_id",
                 lambda value: value in self.codex_rollouts or None),
                (claude_process_identity, "claude_session_census",
                 lambda: _census_at(self.proc)),
                (herdr_observation, "pane_census", lambda **_kw: self.panes),
                (self.pm, "_steward_check", lambda *a, **k: None)):
            patcher = mock.patch.object(target, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        SC._THROTTLE.clear()

    # ------------------------------------------------------------ builders

    def requester_row(self, harness, session):
        self.rows.append(SimpleNamespace(harness=harness, session_id=session, cwd=str(self.repo),
                                         session_aliases=(), _gpu_session_aliases=()))

    def holder(self, harness, session, label=None, *, repository=True, generation=None, ts=NOW):
        """A role this session holds: its marker (with the seat and repository)."""
        seat_key = _digest(label or session)
        self.seat_of[(harness, session)] = seat_key
        path = Path(os.environ["AGENT_PEER_LEDGER_ROOT"]) / "peer-steward" / harness / f"{session}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        marker = {"schema_version": 2, "harness": harness, "session_id": session, "since": NOW,
                  "generation": generation or f"gen-{session}", "seat_key": seat_key, "updated": ts,
                  "targets": {"-": {"harness": "unknown", "session_id": None, "name": "-",
                                    "kind": "explicit", "ts": ts, "source": "explicit"}}}
        if repository:
            marker["repository"] = self.key
        path.write_text(json.dumps(marker), encoding="utf-8")
        return seat_key

    def succession(self, *chain, seat_label="lineage-seat"):
        """Confirmed same-harness tidy-clear edges ``chain[0] -> chain[1] -> ...``."""
        seat = tidy.Seat("pane", _digest(seat_label))
        for old, new in zip(chain, chain[1:]):
            tidy._append_ledger_entry(seat, {"ts": time.time(), "harness": "claude", "sid": new,
                                             "event": "succession", "from": old,
                                             "source": "tidy-clear"})

    def claude_process(self, number, *, native=None, taps=(), argv=("claude",), broken_native=False):
        proc = self.proc / str(number)
        (proc / "ns").mkdir(parents=True)
        (proc / "stat").write_text(f"{number} (claude) S " + " ".join(["0"] * 18 + [str(number)]))
        (proc / "cmdline").write_bytes(b"\0".join(item.encode() for item in argv) + b"\0")
        os.symlink("pid:[4026531836]", proc / "ns" / "pid")
        os.symlink("/claude/versions/2.1.30x", proc / "exe")
        home = self.base / f"home-{number}" / ".claude"
        (home / "sessions").mkdir(parents=True)
        (proc / "environ").write_bytes(f"HOME={home.parent}\0".encode())
        if broken_native:
            (home / "sessions" / f"{number}.json").write_text("{not json")
        elif native:
            (home / "sessions" / f"{number}.json").write_text(json.dumps(
                {"pid": number, "procStart": str(number), "sessionId": native}))
        if taps:
            (home / ".statusline").mkdir()
            for value in taps:
                (home / ".statusline" / f"{value}.json").write_text(json.dumps(
                    {"pid": number, "proc_start": str(number), "session_id": value}))

    def unresolved_daemon(self):
        """A Claude-shaped process with no session claim: the census can never be complete."""
        self.claude_process(999, argv=("claude", "--permission-mode", "bypassPermissions"))

    # ------------------------------------------------------------ observations

    def relations(self):
        import dispatch_seat_handover
        return dispatch_seat_handover.resolved_steward_relations(include_released=True)

    def holds_role(self, harness, session):
        """Whether an unreleased role relation still names this session as holder."""
        return any((row["holder"]["harness"], row["holder"]["sid"]) == (harness, session)
                   and not row["released"] for row in self.relations())

    def supervision(self):
        return steward.repo_supervision(repository_key=self.key, limit=2, sessions=list(self.rows),
                                        agents=list(self.agents))

    def liveness(self, harness, session):
        return steward.session_liveness(harness, session, agents=list(self.agents))

    def holder_state(self, result, harness, session):
        found = [item for item in result["holders"]
                 if (item["harness"], item["session_id"]) == (harness, session)]
        return (found[0]["liveness"], found[0]["counted"]) if found else None

    def grant(self, harness, session):
        return self.pm.grant_steward(harness, session, SELF_EXPLICIT, "explicit", NOW,
                                     source="explicit")

    def retire_followup(self, harness, session, name="old"):
        identity = {"harness": harness, "session_id": session, "name": name,
                    "pane": "w1:pOld", "server": "default"}
        duty = {"id": f"retire-{session}", "intent": {"identity": identity,
                                                      "requester": {"harness": "codex"}}}
        return self.ps._retire_followup_ready(duty, identity, "completed-retire", None)

    # ------------------------------------------------------------ watch helpers

    def arm_watch(self, watch_id, *, steward_ident, steward_seat, steward_server="default",
                  target_ident, rearm_count=0):
        root = Path(os.environ["AGENT_PEER_LEDGER_ROOT"]) / "peer-watches"
        root.mkdir(parents=True, exist_ok=True)
        arm = {"schema_version": 1, "watch_id": watch_id, "target": "worker", "server": "default",
               "steward": steward_ident, "steward_server": steward_server,
               "agent": target_ident, "until": ["idle"], "armed_ts": NOW, "wake": "-",
               "refs": [], "observer_generation": 1, "rearm_count": rearm_count, "watcher": {}}
        if steward_seat is not None:
            arm["steward_seat"] = steward_seat
        (root / f"{watch_id}.json").write_text(json.dumps(arm), encoding="utf-8")
        return arm

    def watch_pass(self, watch_id, arm):
        """One re-arm pass over a single watch with spawning stubbed; returns (Popen, receipt)."""
        popen = mock.Mock(return_value=SimpleNamespace(pid=os.getpid()))
        with mock.patch.object(self.ps.subprocess, "Popen", popen), \
                patch_split(self.ps, "_herdr_missing", return_value=False):
            self.ps._ensure_watch_observers(entries=[(watch_id, arm)])
        root = Path(os.environ["AGENT_PEER_LEDGER_ROOT"]) / "peer-watches"
        receipt = root / f"{watch_id}.receipt.json"
        now = json.loads((root / f"{watch_id}.json").read_text())
        return popen, (json.loads(receipt.read_text()) if receipt.exists() else None), now

    def pane_census(self, *panes):
        self.panes = herdr_observation.PaneCensus(
            "default", tuple({"pane_id": pane, **extra} for pane, extra in panes),
            time.monotonic(), True)


def _census_at(root):
    return _REAL_CENSUS(proc_root=root)


class OneFRetireTest(CloseSupervisionBase):
    """1f 65115a33: retired with normal-exit, yet its role kept the repository full."""

    def test_retire_releases_the_closed_sessions_role_and_frees_the_slot(self):
        one_f, other, s1f = sid("65115a33"), sid("c3b9d5e1"), sid("5a5a5a5a")
        self.holder("claude", one_f)
        self.holder("claude", other)
        self.unresolved_daemon()
        self.requester_row("codex", s1f)
        before = self.grant("codex", s1f)
        self.assertEqual(before["status"], "limited", before)
        self.assertEqual(self.supervision()["counted"], 2)

        self.retire_followup("claude", one_f)

        self.assertFalse(self.holds_role("claude", one_f))
        self.assertTrue(self.holds_role("claude", other))
        self.assertEqual(self.supervision()["counted"], 1)
        after = self.grant("codex", s1f)
        self.assertEqual(after["status"], "granted", after)


class E72ClearTest(CloseSupervisionBase):
    """e72c8108 /clear chain: the old tap SID is still named by the live process."""

    CHAIN = [sid(label) for label in ("e72c8108", "aa065c20", "b57cbfd3", "8d7111b4", "c0696404")]

    def build(self, *, lineage=True):
        stale, current = self.CHAIN[3], self.CHAIN[4]
        self.holder("claude", stale)
        self.holder("claude", sid("d4e5f6a7"))
        if lineage:
            self.succession(*self.CHAIN)
        self.claude_process(3541802, native=current, taps=(stale,))
        self.unresolved_daemon()
        self.requester_row("claude", current)
        return stale, current

    def test_stale_tap_sid_of_a_live_process_is_ended_and_frees_the_slot(self):
        stale, current = self.build()
        complete, holders, _reasons = _REAL_CENSUS(proc_root=self.proc)
        self.assertFalse(complete)
        self.assertEqual(set(holders), {stale, current})
        self.assertEqual(self.liveness("claude", stale), "ended")
        self.assertEqual(self.liveness("claude", current), "live")
        self.assertEqual(self.holder_state(self.supervision(), "claude", stale), ("ended", False))
        self.assertIn(self.grant("claude", current)["status"], {"granted", "extended"})

    def test_without_a_confirmed_lineage_the_holder_stays_unknown_and_counted(self):
        stale, current = self.build(lineage=False)
        self.assertEqual(self.holder_state(self.supervision(), "claude", stale), ("live", True))
        self.assertEqual(self.grant("claude", current)["status"], "limited")

    def test_watch_supervised_by_the_stale_sid_ends_without_rearming(self):
        stale, _current = self.build()
        arm = self.arm_watch("e72watch00000001", steward_ident={"harness": "claude", "session_id": stale},
                             steward_seat=None,
                             target_ident={"harness": "codex", "session_id": sid("01a0e6bd"),
                                           "pane": "w1:pT"}, rearm_count=3)
        popen, receipt, now = self.watch_pass("e72watch00000001", arm)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["reason"], "steward-exited")
        self.assertEqual(now["rearm_count"], 3)
        popen.assert_not_called()


class E0CodexDetachedTest(CloseSupervisionBase):
    """Codex [e0]: the detached TUI is gone and nobody opens its rollout."""

    def test_closed_detached_codex_session_does_not_count(self):
        e0, other, requester = sid("01a1816b"), sid("c3b9d5e1"), sid("9c0f1d2e")
        self.holder("codex", e0)
        self.holder("claude", other)
        self.codex_seen = {"complete": True, "open": frozenset({sid("01a0e6bd")})}
        self.codex_rollouts = {e0}
        self.requester_row("claude", requester)
        self.unresolved_daemon()
        self.assertEqual(self.holder_state(self.supervision(), "codex", e0), ("ended", False))
        self.assertEqual(self.grant("claude", requester)["status"], "granted")


class HeartingNamedTest(CloseSupervisionBase):
    """hearting: a closed Codex [e0] and a named holder whose herdr row is still shown."""

    E0 = sid("01a1816b")
    NAMED = sid("0b0b0b0b")

    def setup_named(self, *, row=False, complete=True):
        self.holder("codex", self.E0)
        self.codex_seen = {"complete": complete, "open": frozenset({sid("01a0e6bd")})}
        self.codex_rollouts = {self.E0}
        if row:
            self.rows.append(SimpleNamespace(
                harness="codex", session_id=self.E0, cwd=str(self.repo), _herdr_name="hearting-1f-codex",
                session_aliases=(), _gpu_session_aliases=()))
            self.agents.append({"agent": "codex", "agent_session": {"value": self.E0},
                                "name": "hearting-1f-codex", "pane_id": "w1:pE0",
                                "cwd": str(self.repo)})

    def test_a_closed_codex_without_a_herdr_row_is_ended_and_unnamed(self):
        self.setup_named()
        result = self.supervision()
        self.assertEqual(self.holder_state(result, "codex", self.E0), ("ended", False))
        self.assertIsNone(result["holders"][0]["name"])

    def test_a_closed_codex_with_an_incomplete_open_table_stays_unknown(self):
        self.setup_named(complete=False)
        self.assertEqual(self.holder_state(self.supervision(), "codex", self.E0), ("unknown", True))

    def test_a_named_holder_with_a_herdr_row_is_live_and_counted(self):
        self.setup_named(row=True)
        result = self.supervision()
        self.assertEqual(self.holder_state(result, "codex", self.E0), ("live", True))
        self.assertEqual(result["holders"][0]["name"], "hearting-1f-codex")

    def test_retire_releases_the_named_holder(self):
        self.setup_named(row=True)
        self.retire_followup("codex", self.E0, name="hearting-1f-codex")
        self.assertFalse(self.holds_role("codex", self.E0))
        self.assertEqual(self.supervision()["counted"], 0)

    def test_a_lost_window_does_not_release_a_holder_still_shown_in_its_pane(self):
        self.setup_named(row=True)
        seat_key = self.seat_of[("codex", self.E0)]
        self.arm_watch("namedwatch000001", steward_ident={"harness": "codex", "session_id": self.E0},
                       steward_seat={"kind": "pane", "key": seat_key, "pane": "w1:pE0"},
                       target_ident={"harness": "claude", "session_id": sid("77777777"),
                                     "pane": "w1:pT"})
        self.pane_census(("w1:pE0", {"agent": "codex"}))
        store = peer_obligations.ObligationStore(self.base / "obligations")
        duty = store.create(SC.duty_id("codex", self.E0), SC.DUTY_KIND,
                            {"harness": "codex", "session_id": self.E0}, {"carrier": "x", "schema": 1})
        duty = store.update(duty["id"], state="pending", observation=SC._new_observation(1000.0))
        SC.advance(duty, store, now=1001.0)
        self.assertTrue(self.holds_role("codex", self.E0))
        self.assertEqual(self.supervision()["counted"], 1)


class SrModelaTest(CloseSupervisionBase):
    """SR_ModelA_SD: stale tap SIDs of one live Claude process fill the limit of 2."""

    N = sid("39d9cea4")
    CHAIN = [sid(label) for label in ("aa7f0edc", "bea4b18a", "4e8e77bf", "1cc8159c")]

    def common(self, **process):
        self.holder("claude", sid("bea4b18a"), label="seat-a", generation="gen-1")
        self.holder("claude", sid("aa7f0edc"), label="seat-a", generation="gen-2")
        self.succession(*self.CHAIN, self.N)
        self.claude_process(2092487, **process)
        self.unresolved_daemon()
        self.requester_row("claude", self.N)

    def names_are_unknown(self):
        for label in ("bea4b18a", "aa7f0edc"):
            self.assertIsNone(steward._holder_name("claude", sid(label), self.rows))

    def test_stale_taps_of_the_live_process_stop_counting(self):
        self.common(native=self.N, taps=tuple(self.CHAIN), argv=("claude", "--resume", sid("7dcee068")))
        self.names_are_unknown()
        for label in ("bea4b18a", "aa7f0edc"):
            self.assertEqual(self.liveness("claude", sid(label)), "ended")
        self.assertEqual(self.supervision()["counted"], 0)
        self.assertEqual(self.grant("claude", self.N)["status"], "granted")

    def test_baseline_refusal_names_both_stale_holders(self):
        """Pins the refusal text the user saw; ``limited`` is expected only without the fix."""
        self.common(native=self.N, taps=tuple(self.CHAIN), argv=("claude", "--resume", sid("7dcee068")))
        result = self.grant("claude", self.N)
        line = self.pm.format_steward_limited(result)
        if result["status"] == "limited":
            self.assertIn("holders=claude:bea4b18a,claude:aa7f0edc limit=2", line)
        self.assertEqual(result["status"], "granted", line)

    def test_conflicting_arguments_with_a_native_session_still_resolve(self):
        self.common(native=self.N, taps=tuple(self.CHAIN),
                    argv=("claude", "--resume", sid("7dcee068"), "--session-id", sid("1cc8159c")))
        complete, _holders, reasons = _REAL_CENSUS(proc_root=self.proc)
        self.assertIn("conflicting-session-arguments", reasons)
        self.assertEqual(self.grant("claude", self.N)["status"], "granted")

    def test_conflicting_arguments_without_a_native_claim_stay_ambiguous(self):
        """c2: no native session, so the argv SIDs have no confirmed process successor."""
        a, b = sid("7dcee068"), sid("1cc8159c")
        self.holder("claude", a, label="seat-a")
        self.holder("claude", b, label="seat-a")
        self.succession(*self.CHAIN, self.N)
        self.claude_process(2092487, broken_native=True, argv=("claude", "--resume", a, "--session-id", b))
        self.unresolved_daemon()
        self.requester_row("claude", self.N)
        _complete, _holders, reasons = _REAL_CENSUS(proc_root=self.proc)
        self.assertIn("native-session-claim-unusable", reasons)
        self.assertEqual(self.liveness("claude", a), "live")
        self.assertEqual(self.grant("claude", self.N)["status"], "limited")


class SrAmbiguousTest(CloseSupervisionBase):
    """10-31: holders nothing explains keep counting; limited is the right answer."""

    N = sid("39d9cea4")

    def test_a_holder_with_no_lineage_and_no_claim_stays_counted(self):
        first, second = sid("6a2b5576"), sid("6a2b5577")
        self.holder("claude", first)
        self.holder("claude", second)
        self.claude_process(2092487, native=self.N)
        self.unresolved_daemon()
        self.requester_row("claude", self.N)
        self.assertEqual(self.holder_state(self.supervision(), "claude", first), ("unknown", True))
        self.assertEqual(self.grant("claude", self.N)["status"], "limited")

    def test_a_holder_with_a_lineage_but_no_process_claim_stays_counted(self):
        first, second = sid("6acb557d"), sid("6acb5580")
        self.holder("claude", first)
        self.holder("claude", second)
        self.succession(first, self.N)
        self.claude_process(2092487, native=self.N)
        self.unresolved_daemon()
        self.requester_row("claude", self.N)
        self.assertEqual(self.holder_state(self.supervision(), "claude", first), ("unknown", True))
        self.assertEqual(self.grant("claude", self.N)["status"], "limited")


class CombinedTest(CloseSupervisionBase):
    """hearting at once: a retired 1f, a stale tap, a closed [e0] and a named holder."""

    def test_retire_and_lineage_free_the_repository_for_a_new_steward(self):
        one_f, stale, current = sid("65115a33"), sid("8d7111b4"), sid("c0696404")
        s1f, named, e0 = sid("5a5a5a5a"), sid("0b0b0b0b"), sid("01a1816b")
        self.assertEqual(len({s1f, named, e0, current}), 4)
        self.holder("claude", one_f)
        self.holder("claude", stale)
        self.holder("codex", e0)
        self.holder("codex", named)
        self.succession(sid("e72c8108"), sid("aa065c20"), sid("b57cbfd3"), stale, current)
        self.claude_process(3541802, native=current, taps=(stale,))
        self.unresolved_daemon()
        self.codex_seen = {"complete": True, "open": frozenset({sid("01a0e6bd")})}
        self.codex_rollouts = {e0}
        self.agents.append({"agent": "codex", "agent_session": {"value": named},
                            "name": "hearting-1f-codex", "pane_id": "w1:pNB", "cwd": str(self.repo)})
        self.rows.append(SimpleNamespace(harness="codex", session_id=named, cwd=str(self.repo),
                                         _herdr_name="hearting-1f-codex", session_aliases=(),
                                         _gpu_session_aliases=()))
        self.requester_row("claude", current)

        self.assertEqual(self.grant("claude", current)["status"], "limited")
        self.assertGreaterEqual(self.supervision()["counted"], 2)

        self.retire_followup("claude", one_f)

        result = self.supervision()
        self.assertEqual(result["counted"], 1)
        self.assertEqual(self.holder_state(result, "codex", named), ("live", True))
        self.assertEqual(self.holder_state(result, "codex", e0), ("ended", False))
        self.assertEqual(self.grant("claude", current)["status"], "granted")


class Watch915dTest(CloseSupervisionBase):
    """Host watch 915d390c…: supervisor pane gone, target pane re-used by a /clear'd session."""

    WATCH = "915d390c1290f6f9"

    def arm(self, target_sid):
        return self.arm_watch(
            self.WATCH, steward_ident={"harness": "codex", "session_id": sid("01a1816b")},
            steward_seat={"kind": "pane", "key": _digest("wB:pAP"), "pane": "wB:pAP"},
            target_ident={"harness": "claude", "session_id": target_sid, "pane": "wB:pA6"},
            rearm_count=31)

    def test_closed_supervisor_and_replaced_target_end_the_watch_without_a_socket(self):
        expected, current = sid("e72c8108"), sid("c0696404")
        self.succession(expected, sid("aa065c20"), sid("b57cbfd3"), sid("8d7111b4"), current)
        arm = self.arm(expected)
        self.pane_census(("wB:pA6", {"agent": "claude", "agent_session": {"value": current}}))
        popen, receipt, now = self.watch_pass(self.WATCH, arm)
        self.assertIsNotNone(receipt)
        self.assertIn(receipt["reason"], {"session-replaced", "steward-exited"})
        self.assertEqual(now["rearm_count"], 31)
        popen.assert_not_called()

    def test_closed_supervisor_pane_alone_ends_the_watch(self):
        target = sid("c0696404")
        arm = self.arm(target)
        self.pane_census(("wB:pA6", {"agent": "claude", "agent_session": {"value": target}}))
        popen, receipt, now = self.watch_pass(self.WATCH, arm)
        self.assertEqual(receipt and receipt["reason"], "steward-exited")
        self.assertEqual(now["rearm_count"], 31)
        popen.assert_not_called()

    def test_control_unchanged_panes_keep_rearming_the_watch(self):
        target = sid("c0696404")
        arm = self.arm(target)
        self.pane_census(("wB:pA6", {"agent": "claude", "agent_session": {"value": target}}),
                         ("wB:pAP", {"agent": "codex", "agent_session": {"value": sid("01a1816b")}}))
        popen, receipt, now = self.watch_pass(self.WATCH, arm)
        self.assertIsNone(receipt)
        self.assertEqual(now["rearm_count"], 32)
        popen.assert_called_once()


class ClearedSeatTest(CloseSupervisionBase):
    """hearting 11-02: ``steward=limited repo=hearting holders=claude:8d7111b4,
    hearting-supervisor-claude limit=2``.

    8d7111b4 /cleared on 10-31 in its own pane, before the start hook released on a
    confirmed /clear. No live process names it any more and the census is incomplete,
    so its liveness stays unknown, and its successor two clears later is the supervisor
    in that same pane: one seat held two slots. The steward-check pass reads the same
    confirmed same-seat /clear and lets the predecessor's role go."""

    CHAIN = [sid(label) for label in ("8d7111b4", "c0696404", "adb5fe90")]
    SEAT = "wB:pA6"
    NEWCOMER = sid("b6b6b6b6")

    def build(self, *, edge_seat=None):
        stale, current = self.CHAIN[0], self.CHAIN[-1]
        self.holder("claude", stale, label=self.SEAT)
        self.holder("claude", current, label=self.SEAT, generation="gen-current")
        self.succession(*self.CHAIN, seat_label=edge_seat or self.SEAT)
        self.claude_process(4100, native=current)
        self.unresolved_daemon()
        self.requester_row("claude", current)
        self.requester_row("claude", self.NEWCOMER)
        return stale, current

    def runner_pass(self, harness, session):
        store = peer_obligations.ObligationStore(self.base / "obligations")
        duty = store.create(SC.duty_id(harness, session), SC.DUTY_KIND,
                            {"harness": harness, "session_id": session}, {"carrier": "x", "schema": 1})
        duty = store.update(duty["id"], state="pending", observation=SC._new_observation(1000.0))
        SC.advance(duty, store, now=1001.0)
        return store.get(duty["id"])

    def test_the_cleared_predecessor_leaves_its_slot_after_one_pass(self):
        stale, current = self.build()
        self.assertEqual(self.liveness("claude", stale), "unknown")
        self.assertEqual(self.holder_state(self.supervision(), "claude", stale), ("unknown", True))
        self.assertEqual(self.grant("claude", self.NEWCOMER)["status"], "limited")

        duty = self.runner_pass("claude", stale)

        self.assertEqual((duty["state"], duty["observation"]["reason"]), ("complete", "cleared"))
        self.assertFalse(self.holds_role("claude", stale))
        self.assertTrue(self.holds_role("claude", current))
        self.assertIsNone(self.holder_state(self.supervision(), "claude", stale))
        self.assertEqual(self.grant("claude", self.NEWCOMER)["status"], "granted")

    def test_the_successor_in_the_same_seat_keeps_its_role(self):
        _stale, current = self.build()
        duty = self.runner_pass("claude", current)
        self.assertNotEqual(duty["state"], "complete")
        self.assertTrue(self.holds_role("claude", current))

    def test_a_clear_recorded_on_another_seat_releases_nothing(self):
        stale, _current = self.build(edge_seat="wB:pZZ")
        self.runner_pass("claude", stale)
        self.assertTrue(self.holds_role("claude", stale))

    def test_a_predecessor_live_elsewhere_keeps_its_role(self):
        stale, _current = self.build()
        self.claude_process(4200, native=stale)
        self.runner_pass("claude", stale)
        self.assertTrue(self.holds_role("claude", stale))

    def test_an_ambiguous_lineage_releases_nothing(self):
        stale, _current = self.build()
        self.succession(stale, sid("f0f0f0f0"), seat_label="fork-seat")
        self.runner_pass("claude", stale)
        self.assertTrue(self.holds_role("claude", stale))

    # --- RV-1043: a clear edge is history; later activity or a pending handover wins

    def activity(self, session, event, at, seat_label=None, **extra):
        seat = tidy.Seat("pane", _digest(seat_label or self.SEAT))
        tidy._append_ledger_entry(seat, {"ts": at, "harness": "claude", "sid": session,
                                         "event": event, **extra})

    def test_a_predecessor_resumed_in_its_seat_keeps_its_role(self):
        # Written before the clear edges, so file order says "earlier"; its time is later.
        self.activity(self.CHAIN[0], "start", time.time() + 60, source="resume")
        stale, _current = self.build()
        self.assertEqual(self.liveness("claude", stale), "unknown")
        duty = self.runner_pass("claude", stale)
        self.assertNotEqual(duty["state"], "complete")
        self.assertTrue(self.holds_role("claude", stale))

    def test_activity_older_than_the_clear_still_releases(self):
        stale, _current = self.build()
        # Appended after the edges (file order "later"), but older in time.
        self.activity(stale, "prompt", time.time() - 3600)
        duty = self.runner_pass("claude", stale)
        self.assertEqual(duty["observation"]["reason"], "cleared")
        self.assertFalse(self.holds_role("claude", stale))

    def test_a_predecessor_resumed_elsewhere_and_unseen_by_the_census_keeps_its_role(self):
        stale, _current = self.build()
        self.claude_process(4200, native=stale)
        os.unlink(self.proc / "self" / "ns" / "pid")      # the runner sits in another PID namespace
        os.symlink("pid:[4026599999]", self.proc / "self" / "ns" / "pid")
        self.activity(stale, "start", time.time() + 60, seat_label="wB:pR1", source="resume")
        self.assertEqual(self.liveness("claude", stale), "unknown")
        self.runner_pass("claude", stale)
        self.assertTrue(self.holds_role("claude", stale))

    def test_a_role_granted_again_after_the_clear_is_kept(self):
        stale, _current = self.build()
        again = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60))
        self.holder("claude", stale, label=self.SEAT, generation="gen-again", ts=again)
        self.runner_pass("claude", stale)
        self.assertTrue(self.holds_role("claude", stale))

    def pending_handover(self):
        """The predecessor's tidy snapshot, its clear edge and the successor's start; the
        start hook's handover of the role has not committed (it retries at a prompt)."""
        stale, current = self.CHAIN[0], self.CHAIN[1]
        self.holder("claude", stale, label=self.SEAT)
        seat = tidy.Seat("pane", _digest(self.SEAT))
        with tidy.seat_lock(seat.key):
            handover.write_snapshot_locked(seat, "claude", stale, now=time.time())
        self.succession(stale, current, seat_label=self.SEAT)
        self.activity(current, "start", time.time(), source="clear")
        self.unresolved_daemon()
        self.claude_process(4100, native=current)
        self.requester_row("claude", current)
        return seat, stale, current

    def test_a_pending_handover_is_left_to_carry_the_role(self):
        seat, stale, current = self.pending_handover()
        duty = self.runner_pass("claude", stale)
        self.assertNotEqual(duty["state"], "complete")
        with mock.patch.object(handover, "_confirmed_clear", return_value=True), \
                tidy.seat_lock(seat.key):
            row = handover._record_locked(seat, "claude", current, "prompt", "", time.time())
        self.assertEqual(len(((row or {}).get("steward") or {}).get("roles") or ()), 1)
        self.assertTrue(self.holds_role("claude", current))

    def test_after_the_successor_prompted_an_untaken_role_is_released(self):
        _seat, stale, current = self.pending_handover()
        self.activity(current, "prompt", time.time() + 1)
        duty = self.runner_pass("claude", stale)
        self.assertEqual(duty["observation"]["reason"], "cleared")
        self.assertFalse(self.holds_role("claude", stale))

    def test_a_release_read_before_the_hook_handover_leaves_the_successors_role(self):
        seat, stale, current = self.pending_handover()
        before = handover.resolved_steward_relations(include_released=True)
        with tidy.seat_lock(seat.key):
            row = handover._record_locked(seat, "claude", current, "start", "clear", time.time())
        self.assertTrue(row)
        self.assertTrue(self.holds_role("claude", current))
        real = handover.resolved_steward_relations

        def read_before_the_handover(*args, **kwargs):
            if not args and kwargs == {"include_released": True}:
                return [dict(relation) for relation in before]
            return real(*args, **kwargs)

        with mock.patch.object(handover, "resolved_steward_relations",
                               side_effect=read_before_the_handover):
            summary = self.pm.release_closed_session("claude", stale, reason="cleared")
        self.assertEqual(summary["roles"], 0)
        self.assertTrue(self.holds_role("claude", current))


if __name__ == "__main__":
    unittest.main()
