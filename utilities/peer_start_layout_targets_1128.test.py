#!/usr/bin/env python3
"""A start arranges the tab it split; a target whose window closed leaves the steward check.

Seen on 11-27: `peer-steward start` split the supervisor's tab and left the panes
uneven until `hearting run pane tidy` was run by hand, and after the four started
windows were closed the steward check still listed them as
`state=unknown liveness=unknown`. Herdr, the pane layout and every process
observation are synthetic: no window, herdr server or native model is touched.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
for path in (str(HERE), str(HERE.parent / "tools")):
    if path not in sys.path:
        sys.path.insert(0, path)
import herdr_observation  # noqa: E402
import steward_check as SC  # noqa: E402
import terminal_surface  # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(HERE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


peer = _load("peer_steward_layout_1128", "peer-steward.py")
REAL_OBSERVE = SC._observe_role
REAL_SESSION_LIVENESS = SC._session_liveness


class _Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="srl-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in tuple(os.environ):
            if key.startswith(("AGENT_", "HERDR_", "TMUX")):
                os.environ.pop(key, None)
        (self.tmp / "jobs.log").touch()
        os.environ.update({"AGENT_PEER_LEDGER_ROOT": str(self.tmp / "peers"),
                           "AGENT_DISPATCH_JOBS": str(self.tmp / "jobs.log"),
                           "XDG_STATE_HOME": str(self.tmp / "state")})
        self.addCleanup(mock.patch.stopall)


class StartArrangesItsTabTest(_Isolated):
    def setUp(self):
        super().setUp()
        self.placement, self.native_error, self.tidy_result = "split", None, None
        self.held, self.events, self.tidies = [], [], []
        test = self

        @contextlib.contextmanager
        def workspace_lock(surface, workspace):
            test.held.append(workspace)
            test.events.append(("lock", workspace))
            try:
                yield
            finally:
                test.held.remove(workspace)
                test.events.append(("unlock", workspace))

        def tidy_panes(*, near=None):
            self.tidies.append((near, list(self.held)))
            self.events.append(("tidy", near.pane))
            return self.tidy_result or terminal_surface.TidyResult(
                near.surface.name, tab="w1:t1", panes=["w1:pOld", "w1:pNew"], changed=True)

        def run(argv, **kwargs):
            if argv[:3] != ["herdr", "agent", "start"]:
                raise AssertionError(f"unexpected native call {argv}")
            body = ({"error": {"code": self.native_error}} if self.native_error else
                    {"result": {"agent": {"agent": "codex", "name": argv[3]}}})
            return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")

        for target, value in (
                ("_herdr_missing", False), ("_current_session_identity", ("controller-sid", "codex")),
                ("_start_shell_identity", (101, "700")),
                ("_wait_for_created_shell", (None, (101, "700"))),
                ("_start_shell_snapshot", "ready"), ("_ensure_pane_ingress", None),
                ("_read_screen", None), ("_pane_is_managed", False), ("_process_session", None),
                ("_mark_seat_successor", None), ("_retain_start_failure", None),
                ("_failed_start_cleanup", "closed")):
            mock.patch.object(peer, target, return_value=value).start()
        mock.patch.object(peer.peer_obligations, "ensure_normal_runner", return_value=False).start()
        mock.patch.object(peer.subprocess, "run", side_effect=run).start()
        mock.patch.object(terminal_surface, "open_pane", side_effect=lambda **kw:
                          terminal_surface.PaneResult("herdr", "w1:pNew", self.placement)).start()
        mock.patch.object(terminal_surface, "workspace_lock", side_effect=workspace_lock).start()
        mock.patch.object(terminal_surface, "tidy_panes", side_effect=tidy_panes).start()

    def start(self, name="new"):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = peer.main(["start", name, "--kind", "codex", "--beside", "w1:pOld",
                              "--cwd", str(self.tmp)])
        return code, output.getvalue().strip().splitlines()

    def test_a_split_start_arranges_the_callers_tab_once_after_its_claim(self):
        code, lines = self.start()
        self.assertEqual(code, 0, lines)
        self.assertEqual(len(lines), 1, lines)  # still the one receipt line
        self.assertTrue(lines[0].startswith("started=true agent=codex name=new pane=w1:pNew"), lines)
        self.assertTrue(lines[0].endswith(" layout=tidied"), lines)
        (near, held), = self.tidies
        self.assertEqual((near.pane, near.surface.name, held), ("w1:pOld", "herdr", []))
        self.assertEqual(self.events, [("lock", "w1"), ("unlock", "w1"), ("tidy", "w1:pOld")])

    def test_an_arranged_or_refused_tidy_reports_once_and_keeps_the_start(self):
        for result, line in (
                (terminal_surface.TidyResult("herdr", tab="w1:t1", changed=False), "layout=unchanged"),
                (terminal_surface.TidyResult("herdr", notes=["pane-tab-inactive"]),
                 "layout=unconfirmed:pane-tab-inactive")):
            with self.subTest(line=line):
                self.tidy_result = result
                code, lines = self.start(name="n-" + line[7:12])
                self.assertEqual(code, 0, lines)
                self.assertEqual(len(lines), 1, lines)
                self.assertTrue(lines[0].startswith("started=true "), lines)
                self.assertTrue(lines[0].endswith(" " + line), lines)

    def test_no_split_or_no_start_arranges_nothing(self):
        for placement, error in (("reuse", None), ("tab", None), ("split", "agent_name_taken")):
            with self.subTest(placement=placement, error=error):
                self.placement, self.native_error = placement, error
                self.tidies.clear()
                _code, lines = self.start(name=f"{placement}-{bool(error)}")
                self.assertEqual(self.tidies, [])
                self.assertFalse(any("layout=" in line for line in lines), lines)

    def test_a_tmux_start_uses_the_same_tidy(self):
        surface = SimpleNamespace(name="tmux")
        args = SimpleNamespace(placement="split", beside="%3@/tmp/tmux-fixture/default")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = peer._start_then_tidy(args, surface, "fixture-session", lambda: print(
                "started=true agent=claude name=x pane=%4@/tmp/tmux-fixture/default surface=tmux") or 0)
        self.assertEqual(code, 0)
        (near, held), = self.tidies
        self.assertEqual((near.surface, near.pane, held), (surface, args.beside, []))
        self.assertEqual(output.getvalue().strip(), "started=true agent=claude name=x "
                         "pane=%4@/tmp/tmux-fixture/default surface=tmux layout=tidied")

    def test_the_start_target_records_its_window_server(self):
        recorded = []
        with mock.patch.object(peer.peer_message, "grant_steward",
                               side_effect=lambda *a, **k: recorded.append(a) or {"status": "granted"}):
            code, lines = self.start()
        self.assertEqual(code, 0, lines)
        (_harness, _sid, target, _kind, _ts), = recorded
        self.assertEqual((target["pane"], target["server"]), ("w1:pNew", "default"))


class GrantKeepsTheServerTest(_Isolated):
    def test_the_marker_entry_keeps_the_pane_server(self):
        message = peer.peer_message
        with mock.patch.object(message, "_fleet_steward", return_value=None), \
             mock.patch.object(message, "_has_explicit_self_entry", return_value=True):
            path = message.steward_marker_path("codex", "holder-sid")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"schema_version": 2, "harness": "codex",
                                        "session_id": "holder-sid", "targets": {}}))
            message.grant_steward("codex", "holder-sid",
                                  {"harness": "codex", "session_id": None, "name": "new",
                                   "pane": "w1:pNew", "server": "lab"},
                                  "start", "2026-11-28T00:00:00Z", source="start")
        entry = json.loads(path.read_text())["targets"]["new"]
        self.assertEqual((entry["pane"], entry["server"]), ("w1:pNew", "lab"))


class ClosedWindowTargetTest(_Isolated):
    """A target whose own window is gone and that lives nowhere else is no longer listed."""

    def observe(self, targets, census, liveness="unknown"):
        collector = SimpleNamespace(
            _peer_message_module=lambda: None, _registry_sessions=lambda: [],
            role_targets=lambda *a, **k: [t for t in targets if not t.get("unresolved")],
            unresolved_role_targets=lambda *a, **k: [t for t in targets if t.get("unresolved")],
            session_liveness=lambda *a, **k: liveness)
        calls = []

        def pane_census(*, session=None, **kwargs):
            calls.append(session)
            return census
        with mock.patch.object(SC, "_collector", return_value=collector), \
             mock.patch.object(SC, "_end_observation", return_value=None), \
             mock.patch.object(SC, "_session_liveness", REAL_SESSION_LIVENESS), \
             mock.patch.object(SC, "_open_work", return_value=([], [])), \
             mock.patch.object(herdr_observation, "pane_census", side_effect=pane_census), \
             mock.patch("peer_steward_native.native_agent", return_value=None):
            seen = REAL_OBSERVE("claude", "holder-sid")
        return [t["name"] for t in seen["targets"]], calls, seen

    @staticmethod
    def started(name, pane, sid=None, server=None, unresolved=True):
        row = {"harness": "codex", "session_id": sid, "name": name, "pane": pane,
               "source": "start", **({"server": server} if server else {})}
        return dict(row, unresolved=True) if unresolved else row

    def census(self, *panes, endpoint="default", complete=True):
        return herdr_observation.PaneCensus(endpoint, tuple({"pane_id": p} for p in panes), 1.0,
                                            complete=complete)

    def test_started_targets_whose_windows_closed_leave_the_check(self):
        targets = [self.started(name, f"w1:p{index}", server="default")
                   for index, name in enumerate(("cx-a", "cx-b", "cl-a", "cl-b"))]
        targets.append(self.started("kept", "w1:p9", server="default"))
        names, calls, seen = self.observe(targets, self.census("w1:p9", "w1:p20"))
        self.assertEqual(names, ["kept"])
        self.assertEqual(calls, ["default"])  # one census for the check
        self.assertEqual(SC._classify(seen), "run-steward-check")
        names, _calls, seen = self.observe(targets[:4], self.census("w1:p20"))
        self.assertEqual(names, [])
        self.assertEqual(SC._classify(seen), "release-if-idle")

    def test_an_ended_session_with_a_known_id_leaves_too_but_a_live_one_stays(self):
        target = self.started("named", "w1:p3", sid="sid-named", server="default", unresolved=False)
        self.assertEqual(self.observe([target], self.census("w1:p1"), liveness="ended")[0], [])
        # Resumed in another window: live elsewhere, so it is still a target.
        self.assertEqual(self.observe([target], self.census("w1:p1"), liveness="live")[0], ["named"])

    def test_uncertain_windows_keep_the_target(self):
        cases = {
            "still-listed": (self.started("x", "w1:p3", server="default"), self.census("w1:p3")),
            "no-census": (self.started("x", "w1:p3", server="default"), None),
            "incomplete": (self.started("x", "w1:p3", server="default"),
                           self.census("w1:p1", complete=False)),
            "other-server": (self.started("x", "w1:p3", server="lab"), self.census("w1:p1")),
            "tmux": (self.started("x", "%3@/tmp/tmux-fixture/default", server="tmux:/tmp/tmux-fixture/default"),
                     self.census("w1:p1")),
            "no-pane": (self.started("x", None, server="default"), self.census("w1:p1")),
        }
        for name, (target, census) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.observe([target], census)[0], ["x"])

    def test_an_entry_from_before_the_server_was_recorded_reads_the_default_server(self):
        names, calls, _seen = self.observe([self.started("old", "w1:p3")], self.census("w1:p1"))
        self.assertEqual((names, calls), ([], ["default"]))


if __name__ == "__main__":
    unittest.main()
