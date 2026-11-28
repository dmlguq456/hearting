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


class _StartHarness(_Isolated):
    """A herdr start with synthetic placement, native call and tidy; no tests of its own."""

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


class StartArrangesItsTabTest(_StartHarness):
    def test_a_split_start_arranges_the_callers_tab_once_inside_its_claim(self):
        code, lines = self.start()
        self.assertEqual(code, 0, lines)
        self.assertEqual(len(lines), 1, lines)  # still the one receipt line
        self.assertTrue(lines[0].startswith("started=true agent=codex name=new pane=w1:pNew"), lines)
        self.assertTrue(lines[0].endswith(" layout=tidied"), lines)
        (near, held), = self.tidies
        # Inside the start's own claim (tidy re-enters it): no other start reads the layout between.
        self.assertEqual((near.pane, near.surface.name, held), ("w1:pOld", "herdr", ["w1"]))
        self.assertEqual(self.events, [("lock", "w1"), ("tidy", "w1:pOld"), ("unlock", "w1")])

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
        self.assertEqual(peer._tidy_after_start(args, True, surface), " layout=tidied")
        (near, _held), = self.tidies
        self.assertEqual((near.surface, near.pane), (surface, args.beside))
        tmux_receipt = Path(peer.__file__).read_text().split("def _tmux_started", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_tidy_after_start(args, True, surface)", tmux_receipt)

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

    def test_an_entry_with_no_recorded_server_stays_on_the_check(self):
        # Review 11-28 (PR #1933): such an entry was read on the default server and a live
        # window on another server was hidden. No server is guessed for it.
        names, calls, _seen = self.observe([self.started("old", "w1:p3")], self.census("w1:p1"))
        self.assertEqual((names, calls), (["old"], []))

    def test_each_server_is_read_once_and_only_for_its_own_targets(self):
        censuses = {"default": self.census("w1:p1"), "lab": self.census("w1:p3", endpoint="lab")}
        targets = [self.started("on-default", "w1:p3", server="default"),
                   self.started("on-lab", "w1:p3", server="lab"),
                   self.started("gone-on-lab", "w1:p4", server="lab")]
        collector = SimpleNamespace(
            _peer_message_module=lambda: None, _registry_sessions=lambda: [],
            role_targets=lambda *a, **k: [], unresolved_role_targets=lambda *a, **k: targets,
            session_liveness=lambda *a, **k: "unknown")
        calls = []
        with mock.patch.object(SC, "_collector", return_value=collector), \
             mock.patch.object(SC, "_end_observation", return_value=None), \
             mock.patch.object(SC, "_session_liveness", REAL_SESSION_LIVENESS), \
             mock.patch.object(SC, "_open_work", return_value=([], [])), \
             mock.patch.object(herdr_observation, "pane_census",
                               side_effect=lambda *, session=None, **k: calls.append(session) or censuses[session]), \
             mock.patch("peer_steward_native.native_agent", return_value=None):
            seen = REAL_OBSERVE("claude", "holder-sid")
        self.assertEqual([t["name"] for t in seen["targets"]], ["on-lab"])
        self.assertEqual(sorted(calls), ["default", "lab"])


class BoundCensusTest(unittest.TestCase):
    """A census names the server it actually read (review 11-28, PR #1933)."""

    def run_census(self, session, environ):
        seen = {}

        def runner(argv, **kwargs):
            seen.update(argv=argv, env=kwargs.get("env"))
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"result": {"panes": []}}), stderr="")
        with mock.patch.dict(os.environ, environ, clear=True):
            census = herdr_observation.pane_census(session=session, runner=runner,
                                                   which=lambda _name: "/usr/bin/herdr")
        return census, seen

    def test_a_default_census_from_a_pane_of_another_server_reads_the_default_one(self):
        census, seen = self.run_census("default", {
            "PATH": "/usr/bin", "HERDR_SESSION": "lab", "HERDR_SOCKET_PATH": "/run/lab.sock",
            "AGENT_HERDR_SESSION": "lab"})
        self.assertEqual(census.endpoint, "default")
        self.assertEqual(seen["argv"], ["herdr", "pane", "list"])
        self.assertFalse({"HERDR_SESSION", "HERDR_SOCKET_PATH", "AGENT_HERDR_SESSION"} & set(seen["env"]))

    def test_a_named_census_keeps_only_that_servers_socket(self):
        census, seen = self.run_census("lab", {"PATH": "/usr/bin", "HERDR_SOCKET_PATH": "/run/default.sock"})
        self.assertEqual(census.endpoint, "lab")
        self.assertEqual(seen["argv"], ["herdr", "--session", "lab", "pane", "list"])
        self.assertNotIn("HERDR_SOCKET_PATH", seen["env"])
        census, seen = self.run_census("lab", {"PATH": "/usr/bin", "HERDR_SESSION": "lab",
                                               "HERDR_SOCKET_PATH": "/run/lab.sock"})
        self.assertEqual(seen["env"]["HERDR_SOCKET_PATH"], "/run/lab.sock")

    def test_a_default_census_with_only_another_servers_socket_reads_the_default_one(self):
        # Review 2 🔴⑤-1: Herdr follows an inherited socket before its default server, and a
        # socket alone names no owner. Two fake servers: default holds the live `w1:p3`.
        lists = {"/run/lab.sock": [{"pane_id": "w1:p9"}], None: [{"pane_id": "w1:p3"}]}

        def runner(argv, **kwargs):
            panes = lists[(kwargs.get("env") or os.environ).get("HERDR_SOCKET_PATH")]
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"result": {"panes": panes}}),
                                               stderr="")
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin", "HERDR_SOCKET_PATH": "/run/lab.sock"},
                             clear=True):
            census = herdr_observation.pane_census(session="default", runner=runner,
                                                   which=lambda _name: "/usr/bin/herdr")
        self.assertEqual(census.endpoint, "default")
        self.assertEqual([row["pane_id"] for row in census.panes], ["w1:p3"])
        self.assertFalse(herdr_observation.confirms_absence(census, "w1:p3", endpoint="default"))

    def test_the_callers_own_server_census_is_as_before(self):
        _census, seen = self.run_census(None, {"PATH": "/usr/bin", "HERDR_SESSION": "lab"})
        self.assertIsNone(seen["env"])

    def test_a_default_census_keeps_the_default_servers_own_socket(self):
        # Review 3: the socket stays when it is where Herdr finds its default server.
        with tempfile.TemporaryDirectory(prefix="srl-") as config:
            own = os.path.join(config, "herdr", "herdr.sock")
            _census, seen = self.run_census("default", {
                "PATH": "/usr/bin", "XDG_CONFIG_HOME": config, "HERDR_SOCKET_PATH": own})
            self.assertEqual(seen["env"]["HERDR_SOCKET_PATH"], own)
            _census, seen = self.run_census("default", {
                "PATH": "/usr/bin", "XDG_CONFIG_HOME": config, "HERDR_SOCKET_PATH": "/run/lab.sock"})
            self.assertNotIn("HERDR_SOCKET_PATH", seen["env"])


class DefaultServerHandoverSeatTest(_Isolated):
    """A handover booked from a default-server window keys the successor as its hooks do.

    Review 3 of PR #1933: other Herdr calls keep the default server's inherited socket, so
    the booked successor seat and the successor window's own hook name one seat ledger."""

    def test_the_booked_successor_seat_is_the_successor_windows_hook_seat(self):
        import session_tidy
        socket = str(self.tmp / "herdr.sock")  # the default server's socket its panes inherit
        os.environ.update({"HERDR_SOCKET_PATH": socket, "HERDR_PANE_ID": "w1:p3"})
        mock.patch.object(peer, "_HERDR_SESSION", None).start()
        peer._mark_seat_successor("w1:p5", "w1:p4", "claude", "new-sid")
        requester = {"pane": "w1:p5", "server": "default", "harness": "claude", "session_id": "new-sid"}
        with mock.patch("dispatch_seat_handover.record_retire_handover",
                        return_value={"bindings": [{}]}) as handed:
            got = peer._seat_handover({"pane": "w1:p4", "session_id": "old-sid", "harness": "codex"},
                                      "new-sid", "claude", requester=requester,
                                      accepted_at=__import__("time").time() + 60)
        self.assertEqual(got, "1")
        booked = handed.call_args.kwargs["successor_seat"]
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p5"}), \
             mock.patch("pane_ownership.verified_pane", side_effect=lambda pane, *_a, **_k: pane):
            hook = session_tidy.resolve_seat("claude", sid="new-sid", hook=True)
        self.assertEqual((hook.pane, hook.socket_path), ("w1:p5", socket))
        self.assertEqual((booked.pane, booked.key, booked.endpoint, booked.socket_path),
                         (hook.pane, hook.key, hook.endpoint, hook.socket_path))


class WatchKeepsTheStartServerTest(_Isolated):
    """`start → watch` on one pane keeps the server the pane was seen on (review 11-28)."""

    def test_a_watch_after_a_start_keeps_the_server(self):
        message = peer.peer_message
        with mock.patch.object(message, "_fleet_steward", return_value=None), \
             mock.patch.object(message, "_has_explicit_self_entry", return_value=True), \
             mock.patch.object(peer, "_print_steward_limited"):
            path = message.steward_marker_path("codex", "holder-sid")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"schema_version": 2, "harness": "codex",
                                        "session_id": "holder-sid", "targets": {}}))
            message.grant_steward("codex", "holder-sid",
                                  {"harness": "codex", "session_id": "target-sid", "name": "new",
                                   "pane": "w1:p3", "server": "lab"},
                                  "start", "2026-11-28T00:00:00Z", source="start")
            peer._mark_observed("codex", "target-sid", "new", ("holder-sid", "codex"),
                                pane="w1:p3", server="lab", steward_seat=None)
        entry = json.loads(path.read_text())["targets"]["target-sid"]
        self.assertEqual((entry["source"], entry["pane"], entry["server"]), ("watch", "w1:p3", "lab"))

    def test_watch_and_wait_pass_the_server_they_read(self):
        source = Path(peer.__file__).read_text()
        for name in ("def cmd_wait", "def cmd_watch", "def _restore_watch_role"):
            body = source.split(name, 1)[1].split("\ndef ", 1)[0]
            with self.subTest(caller=name):
                for call in body.split("_mark_observed(")[1:]:
                    self.assertIn("server=", call.split(")\n", 1)[0])


class WaitAndWatchKeepTheStartServerTest(_StartHarness):
    """Review 2 🔴⑤-2: a start from a pane whose inherited `HERDR_SESSION` is `lab` records
    `lab`; a later `wait` or `watch` by SID with no server option renews that same pane's entry
    and keeps `lab` instead of the command's own default."""

    SID = "01000000-0000-7000-8000-00000000a128"

    def setUp(self):
        super().setUp()
        os.environ["HERDR_SESSION"] = "lab"
        message = peer.peer_message
        for owner, name, value in ((message, "_fleet_steward", None), (message, "_has_explicit_self_entry", True),
                                   (peer, "_print_steward_limited", None), (peer, "_steward_seat", None),
                                   (peer, "_process_session", self.SID), (peer, "_spawn_watch_observer", True),
                                   (peer, "_watcher_present", True), (message, "_steward_check", None),
                                   (message, "observe_steward_resume", None)):
            mock.patch.object(owner, name, return_value=value).start()
        mock.patch("dispatch_seat_handover.pane_seat", return_value=None).start()  # no Herdr seat lookup
        self.marker = message.steward_marker_path("codex", "controller-sid")
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text(json.dumps({"schema_version": 2, "harness": "codex",
                                           "session_id": "controller-sid", "targets": {}}))
        self.row = {"agent": "codex", "agent_status": "idle", "agent_session": {"value": self.SID},
                    "name": "new", "pane_id": "w1:pNew", "pid": os.getpid(), "pid_start": "1",
                    "pid_namespace": "pid:[1]", "cwd": str(self.tmp)}

    def entry(self):
        targets = json.loads(self.marker.read_text())["targets"]
        self.assertIn(self.SID, targets, targets)
        return targets[self.SID]

    def test_wait_and_watch_by_sid_keep_the_server_the_start_recorded(self):
        code, lines = self.start()
        self.assertEqual(code, 0, lines)
        self.assertEqual((self.entry()["source"], self.entry()["pane"], self.entry()["server"]),
                         ("start", "w1:pNew", "lab"))
        answer = {"result": {"agent": self.row}}
        for command in ("wait", "watch"):
            with self.subTest(command=command), \
                 mock.patch.object(terminal_surface, "wait_native", return_value=answer), \
                 mock.patch.object(peer, "native_agent", return_value=answer), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                rc = peer.main([command, "codex:" + self.SID, "--timeout", "1000"] if command == "wait"
                               else [command, "codex:" + self.SID])
                self.assertEqual(rc, 0, output.getvalue())
                self.assertEqual((self.entry()["source"], self.entry()["pane"], self.entry()["server"]),
                                 ("watch", "w1:pNew", "lab"))

    def test_an_entry_with_no_recorded_server_takes_the_observed_one(self):
        with mock.patch.object(terminal_surface, "wait_native", return_value={"result": {"agent": self.row}}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            rc = peer.main(["wait", "codex:" + self.SID, "--timeout", "1000"])
        self.assertEqual(rc, 0, output.getvalue())
        self.assertEqual((self.entry()["pane"], self.entry()["server"]), ("w1:pNew", "default"))


if __name__ == "__main__":
    unittest.main()
