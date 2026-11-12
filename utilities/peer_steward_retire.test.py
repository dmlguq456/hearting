#!/usr/bin/env python3
"""peer-steward grid/beside start, retire, session-notice, native-turn and readiness-runner tests.

Split from peer_steward.test.py, which keeps the shared fixtures, helpers and
screen constants, so the parts run on separate CI shards. Test class and method
names are unchanged.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

# A suite that borrows classes from both parts gets one fixture instance, so
# both see the same loaded peer-steward module.
FIXTURE = sys.modules.get("peer_steward_test_fixture")
if FIXTURE is None:
    _FIXTURE_SPEC = importlib.util.spec_from_file_location(
        "peer_steward_test_fixture", Path(__file__).resolve().with_name("peer_steward.test.py"))
    FIXTURE = importlib.util.module_from_spec(_FIXTURE_SPEC)
    sys.modules[_FIXTURE_SPEC.name] = FIXTURE
    _FIXTURE_SPEC.loader.exec_module(FIXTURE)
# The classes below use the fixture module's imports, helpers and constants by
# their bare names, as they did before the split. Its own test classes are not
# imported, so they run only in peer_steward.test.py.
globals().update({name: value for name, value in vars(FIXTURE).items()
                  if not name.startswith("__")
                  and not (isinstance(value, type) and issubclass(value, unittest.TestCase))})


class GridStartTest(_TmpRootMixin, unittest.TestCase):
    """Start through the shared pane opener; its placement cases live in terminal_surface.test.py."""
    FULL = [(10, 2, 200, 100)]
    TWO = [(10, 2, 100, 100), (110, 2, 100, 100)]
    THREE = [(10, 2, 100, 100), (110, 2, 100, 50), (110, 52, 100, 50)]
    FOUR = [(10, 2, 100, 50), (10, 52, 100, 50), (110, 2, 100, 50), (110, 52, 100, 50)]

    def layout(self, rects, area=None):
        return {"workspace_id": "w1", "tab_id": "w1:tGrid",
                "area": dict(zip(("x", "y", "width", "height"), area or self.FULL[0])),
                "panes": [{"pane_id": f"w1:p{i}", "rect": dict(zip(("x", "y", "width", "height"), rect))}
                          for i, rect in enumerate(rects)]}

    def test_start_takes_the_existing_peer_start_claim_file(self):
        """Pinned older releases take this exact file; the shared opener must take the same one."""
        seen = []
        def launch(args):
            seen.extend(path.name for path in terminal_surface._HELD)
            return 0
        args = peer_steward.build_parser().parse_args(["start", "grid", "--kind", "codex", "--beside", "w1:p0"])
        for session in (None, "work"):
            with self.subTest(session=session), \
                 mock.patch.object(peer_steward, "_start_in_pane", side_effect=launch):
                seen.clear()
                peer_steward._HERDR_SESSION = session
                self.assertEqual(peer_steward.cmd_start(args), 0)
                key = hashlib.sha256(f"{session or 'default'}:w1".encode()).hexdigest()
                self.assertEqual(seen, [f"{key}.lock"])
                self.assertTrue((peer_steward.peer_message.peer_state_root() / "peer-starts" / f"{key}.lock").exists())

    def test_layout_read_failure_does_not_start_or_type(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json({"error": {"code": "pane_not_found"}})) as run, \
             mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["start", "grid", "--kind", "codex", "--beside", "w1:p0",
                                               "--cwd", str(self.tmp_root)]), 1)
        self.assertEqual(len(run.call_args_list), 1)
        self.assertIn("reason=pane-layout-unavailable", printed.call_args[0][0])

    def test_concurrent_starts_in_the_same_workspace_hold_the_claim_through_launch(self):
        self.assert_serialized(None, None)

    def test_implicit_and_explicit_default_session_share_the_workspace_claim(self):
        for first_session, second_session in ((None, "default"), ("default", None)):
            with self.subTest(first=first_session, second=second_session):
                self.assert_serialized(first_session, second_session)

    def test_project_start_and_beside_start_share_the_same_workspace_claim(self):
        self.assert_serialized(None, None, project=True)

    def assert_serialized(self, first_session, second_session, project=False):
        import multiprocessing
        ctx = multiprocessing.get_context("fork")
        first_entered, release, second_entered = ctx.Event(), ctx.Event(), ctx.Event()
        def launch(args):
            if args.name == "first":
                first_entered.set()
                release.wait(5)
            else:
                second_entered.set()
            return 0
        first_args = peer_steward.build_parser().parse_args(["start", "first", "--kind", "codex", "--beside", "w1:p0"])
        if project:
            first_args.beside = None
        second_args = peer_steward.build_parser().parse_args(["start", "second", "--kind", "claude", "--beside", "w1:p1"])
        def start(args, session):
            peer_steward._HERDR_SESSION = session
            peer_steward.cmd_start(args)
        with mock.patch.object(peer_steward, "_start_in_pane", side_effect=launch), \
             mock.patch.object(peer_steward, "_caller_pane", return_value=""), \
             mock.patch.object(peer_steward, "_project_start_workspace", return_value="w1"):
            first = ctx.Process(target=start, args=(first_args, first_session))
            second = ctx.Process(target=start, args=(second_args, second_session))
            first.start()
            try:
                self.assertTrue(first_entered.wait(5))
                second.start()
                self.assertFalse(second_entered.wait(.1))
                release.set()
                self.assertTrue(second_entered.wait(5))
            finally:
                release.set()
                for child in (first, second):
                    if child.pid:
                        child.join(5)
                        if child.is_alive():
                            child.terminate()
                            child.join(5)
            self.assertEqual((first.exitcode, second.exitcode), (0, 0))

    def test_each_harness_uses_the_decision_in_one_start_including_overflow_and_shell_reuse(self):
        for kind in ("claude", "codex", "opencode"):
            for rects, free, action, target, direction in (
                    (self.FULL, None, "split", "w1:p0", "right"),
                    (self.TWO, None, "split", "w1:p1", "down"),
                    (self.THREE, None, "split", "w1:p0", "down"),
                    (self.FOUR, "w1:p3", "reuse", "w1:p3", None),
                    (self.FOUR, None, "split", "w1:p0", "right")):
                with self.subTest(kind=kind, action=action, target=target):
                    calls = []
                    def run(argv, **kwargs):
                        calls.append(argv)
                        if argv[1:3] == ["pane", "layout"]:
                            return _herdr_json({"result": {"layout": self.layout(rects)}})
                        if argv[1:3] == ["tab", "list"]:
                            return _herdr_json({"result": {"tabs": []}})
                        if argv[1:3] in (["pane", "split"], ["tab", "create"]):
                            return _herdr_json({"result": {"pane" if action == "split" else "root_pane":
                                                          {"pane_id": "w1:pNew", "focused": False}}})
                        if argv[1:3] == ["agent", "start"]:
                            return _herdr_json({"result": {"agent": {"agent": kind, "name": "grid"}}})
                        if argv[1:3] == ["pane", "get"]:
                            return _herdr_json({"result": {"pane": {"pane_id": argv[3],
                                                                    "agent": None if argv[3] == free else "codex"}}})
                        if argv[1:3] == ["pane", "process-info"]:
                            return _herdr_json({"result": {"process_info": {
                                "pane_id": argv[-1], "shell_pid": 7, "foreground_process_group_id": 7,
                                "foreground_processes": [{"pid": 7}]}}})
                        if argv[1:3] == ["pane", "wait-output"]:
                            return _herdr_json({"result": {}})
                        if argv[1:3] == ["pane", "list"]:
                            return _herdr_json({"result": {"panes": [
                                {"pane_id": p["pane_id"], "workspace_id": "w1", "tab_id": "w1:tGrid"}
                                for p in self.layout(rects)["panes"]]}})
                        raise AssertionError(argv)
                    with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
                         mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
                         mock.patch.object(peer_steward, "_pane_has_agent", side_effect=lambda p: None if p == free else "pane-occupied"), \
                         mock.patch.object(peer_steward, "_pane_foreground_shell", return_value=None), \
                         mock.patch.object(peer_steward, "_wait_for_shell_prompt", return_value=True), \
                         mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
                         mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
                         mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
                         mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="ready"), \
                         mock.patch.object(peer_steward, "_export_opencode_tui_scoped", return_value=None), \
                         mock.patch.object(peer_steward, "_process_session", return_value=None), \
                         mock.patch.object(peer_steward, "_read_screen", return_value=None), \
                         mock.patch.object(peer_steward, "_pane_is_managed", return_value=False), mock.patch("builtins.print"):
                        self.assertEqual(peer_steward.main(["start", "grid", "--kind", kind,
                                                           "--beside", "w1:p0", "--cwd", str(self.tmp_root)]), 0)
                    launch = next(a for a in calls if a[1:3] == ["agent", "start"])
                    self.assertEqual(launch[1:3], ["agent", "start"])
                    self.assertEqual(launch[launch.index("--pane") + 1], target if action == "reuse" else "w1:pNew")
                    if action == "reuse":
                        self.assertEqual(sum(a[1:3] == ["agent", "start"] for a in calls), 1)
                    else:
                        create = next(a for a in calls if a[1:3] in (["pane", "split"], ["tab", "create"]))
                        self.assertIn("--no-focus", create)
                        if action == "split":
                            self.assertEqual(create[create.index("--pane") + 1], target)
                            self.assertEqual(create[create.index("--direction") + 1], direction)
                            self.assertEqual(create[create.index("--ratio") + 1], "0.5")
                        else:
                            self.assertEqual(create[create.index("--workspace") + 1], "w1")

    def test_beside_a_full_reference_tab_splits_that_pane_instead_of_spilling(self):
        """The reference tab wins over a newer tab with room: split it, never fill another."""
        reference = self.layout(self.FOUR)
        reference["tab_id"] = "w1:t1"
        newer = self.layout(self.TWO)
        newer["tab_id"] = "w1:t2"
        newer["panes"] = [{"pane_id": f"w1:p2-{i}", "rect": pane["rect"]}
                          for i, pane in enumerate(newer["panes"])]
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            action = argv[1:3]
            if action == ["pane", "layout"]:
                target = argv[argv.index("--pane") + 1]
                return _herdr_json({"result": {"layout":
                                               newer if target.startswith("w1:p2-") else reference}})
            if action == ["tab", "list"]:
                return _herdr_json({"result": {"tabs": [
                    {"tab_id": "w1:t1", "workspace_id": "w1", "number": 1},
                    {"tab_id": "w1:t2", "workspace_id": "w1", "number": 2}]}})
            if action == ["pane", "list"]:
                return _herdr_json({"result": {"panes": [
                    {"pane_id": pane["pane_id"], "workspace_id": "w1", "tab_id": layout["tab_id"]}
                    for layout in (reference, newer) for pane in layout["panes"]
                ] + [{"pane_id": "w1:pNew", "workspace_id": "w1", "tab_id": "w1:t1"}]}})
            if action == ["pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pNew", "focused": False}}})
            if action == ["tab", "create"]:
                return _herdr_json({"result": {"root_pane": {"pane_id": "w1:pTab", "focused": False}}})
            if action == ["agent", "start"]:
                return _herdr_json({"result": {"agent": {"agent": "codex", "name": "grid"}}})
            if action == ["pane", "get"]:
                return _herdr_json({"result": {"pane": {"pane_id": argv[3], "agent": "codex"}}})
            if action == ["pane", "process-info"]:
                return _herdr_json({"result": {"process_info": {
                    "pane_id": argv[-1], "shell_pid": 7, "foreground_process_group_id": 7,
                    "foreground_processes": [{"pid": 7}]}}})
            if action == ["pane", "wait-output"]:
                return _herdr_json({"result": {}})
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_pane_has_agent", return_value="pane-occupied"), \
             mock.patch.object(peer_steward, "_pane_foreground_shell", return_value=None), \
             mock.patch.object(peer_steward, "_wait_for_shell_prompt", return_value=True), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="ready"), \
             mock.patch.object(peer_steward, "_export_opencode_tui_scoped", return_value=None), \
             mock.patch.object(peer_steward, "_process_session", return_value=None), \
             mock.patch.object(peer_steward, "_read_screen", return_value=None), \
             mock.patch.object(peer_steward, "_pane_is_managed", return_value=False), \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["start", "grid", "--kind", "codex",
                                    "--beside", "w1:p0", "--cwd", str(self.tmp_root)])
        self.assertEqual(rc, 0, printed.call_args_list)
        splits = [a for a in calls if a[1:3] == ["pane", "split"]]
        self.assertEqual(len(splits), 1, calls)
        split = splits[0]
        self.assertEqual(split[split.index("--pane") + 1], "w1:p0")
        self.assertIn("--no-focus", split)
        self.assertEqual(split[split.index("--direction") + 1], "right")
        self.assertFalse(any(a[1:3] == ["tab", "create"] for a in calls))
        self.assertFalse(any(a[1:3] == ["tab", "list"] for a in calls))
        self.assertFalse(any(a[1:3] == ["pane", "layout"]
                             and a[a.index("--pane") + 1].startswith("w1:p2-") for a in calls))
        start = next(a for a in calls if a[1:3] == ["agent", "start"])
        self.assertEqual(start[start.index("--pane") + 1], "w1:pNew")


class ProjectStartTest(_TmpRootMixin, unittest.TestCase):
    """An unattached caller can place a peer without claiming a terminal seat."""

    def launch(self, kind="codex", *, free=None, foreign=False, missing=False):
        calls, lines = [], []
        pane = {"pane_id": "w1:pHint", "workspace_id": "w1",
                "foreground_cwd": str(self.tmp_root / "other" if foreign else self.tmp_root)}
        if foreign:
            (self.tmp_root / "other").mkdir(exist_ok=True)
        panes = [{"pane_id": "w1:pHint", "workspace_id": "w1"},
                 {"pane_id": "w1:pEmpty", "workspace_id": "w1"}]

        def run(argv, **kwargs):
            calls.append(argv)
            action = argv[1:3]
            if action == ["agent", "get"]:
                return _herdr_json({"result": {"agent": None if missing else pane}})
            if action == ["pane", "list"]:
                return _herdr_json({"result": {"panes": panes}})
            if action == ["tab", "create"]:
                return _herdr_json({"result": {"root_pane": {"pane_id": "w1:pNew", "focused": False}}})
            if action == ["agent", "start"]:
                return _herdr_json({"result": {"agent": {"agent": kind, "name": "project-peer"}}})
            if action == ["pane", "get"]:
                return _herdr_json({"result": {"pane": {"pane_id": argv[3],
                                                        "agent": None if argv[3] == free else "codex"}}})
            if action == ["pane", "process-info"]:
                return _herdr_json({"result": {"process_info": {
                    "pane_id": argv[-1], "shell_pid": 7, "foreground_process_group_id": 7,
                    "foreground_processes": [{"pid": 7}]}}})
            if action == ["pane", "wait-output"]:
                return _herdr_json({"result": {}})
            raise AssertionError(argv)

        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pHint"}), \
             mock.patch.object(peer_steward, "_caller_pane", return_value=""), \
             mock.patch.object(peer_steward.os, "getcwd", return_value=str(self.tmp_root)), \
             mock.patch.object(peer_steward.INSTALL_PATHS, "primary_checkout", side_effect=lambda cwd: Path(cwd)), \
             mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_pane_has_agent", side_effect=lambda p: None if p == free else "pane-occupied"), \
             mock.patch.object(peer_steward, "_pane_foreground_shell", return_value=None), \
             mock.patch.object(peer_steward, "_wait_for_shell_prompt", return_value=True), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="ready"), \
             mock.patch.object(peer_steward, "_export_opencode_tui_scoped", return_value=None), \
             mock.patch.object(peer_steward, "_read_screen", return_value=None), \
             mock.patch.object(peer_steward, "_pane_is_managed", return_value=False), \
             mock.patch.object(peer_steward, "_mark_seat_successor") as successor, \
             mock.patch("builtins.print", side_effect=lambda line: lines.append(line)):
            rc = peer_steward.main(["start", "project-peer", "--kind", kind])
        successor.assert_not_called()
        return rc, calls, lines

    def test_unproven_attachment_starts_in_an_unfocused_project_tab_on_every_harness(self):
        for kind in ("claude", "codex", "opencode"):
            with self.subTest(kind=kind):
                rc, calls, lines = self.launch(kind)
                self.assertEqual(rc, 0, lines)
                create = next(a for a in calls if a[1:3] == ["tab", "create"])
                self.assertEqual(create[create.index("--workspace") + 1], "w1")
                self.assertIn("--no-focus", create)
                self.assertEqual(create[create.index("--cwd") + 1], str(self.tmp_root))
                self.assertFalse(any(a[1:3] == ["pane", "split"] for a in calls))
                self.assertIn("placement=project-workspace", lines[-1])

    def test_empty_shell_is_reused_without_touching_the_hinted_pane(self):
        rc, calls, lines = self.launch(free="w1:pEmpty")
        self.assertEqual(rc, 0, lines)
        self.assertFalse(any(a[1:3] == ["tab", "create"] for a in calls))
        start = next(a for a in calls if a[1:3] == ["agent", "start"])
        self.assertEqual(start[start.index("--pane") + 1], "w1:pEmpty")

    def test_foreign_or_missing_workspace_hint_never_allocates_or_types(self):
        for options in ({"foreign": True}, {"missing": True}):
            with self.subTest(options=options):
                rc, calls, lines = self.launch(**options)
                self.assertEqual(rc, 1, lines)
                self.assertEqual([a[1:3] for a in calls], [["agent", "get"]])
                self.assertIn("reason=pane-unknown", lines[-1])

    def test_project_match_includes_a_real_linked_worktree_subdirectory(self):
        primary, linked = self.tmp_root / "primary", self.tmp_root / "linked"
        for argv in (["git", "init", "-q", str(primary)],
                     ["git", "-C", str(primary), "-c", "user.name=Fixture", "-c",
                      "user.email=fixture@example.invalid", "commit", "-q", "--allow-empty", "-m", "fixture"],
                     ["git", "-C", str(primary), "worktree", "add", "-q", "-b", "linked", str(linked)]):
            subprocess.run(argv, check=True, capture_output=True, text=True)
        nested = linked / "nested"
        nested.mkdir()
        pane = {"pane_id": "w1:pHint", "workspace_id": "w1", "foreground_cwd": str(primary)}
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pHint"}), \
             mock.patch.object(peer_steward.os, "getcwd", return_value=str(nested)), \
             mock.patch.object(peer_steward, "_placement_result", return_value={"agent": pane}):
            self.assertEqual(peer_steward._project_start_workspace(), "w1")
            pane["foreground_cwd"] = str(self.tmp_root)
            self.assertIsNone(peer_steward._project_start_workspace())
            del pane["foreground_cwd"]
            self.assertIsNone(peer_steward._project_start_workspace())


class BesideStartTest(_TmpRootMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # These tests own startup/readiness; terminal_surface.test.py owns layout selection.
        placement = mock.patch.object(terminal_surface, "place",
                                      side_effect=lambda surface, pane=None, workspace=None, **kwargs:
                                      ("split", pane, "right"))
        placement.start()
        self.addCleanup(placement.stop)
        census = mock.patch.object(terminal_surface.HerdrSurface, "pane_ids", return_value=None)
        census.start()
        self.addCleanup(census.stop)
        self.resolve_primary = peer_steward.INSTALL_PATHS.primary_checkout
        patch = mock.patch.object(peer_steward.INSTALL_PATHS, "primary_checkout",
                                  side_effect=lambda cwd: Path(cwd))
        self.primary_mock = patch.start()
        self.addCleanup(patch.stop)

    def test_layout_failure_returns_once_to_the_exact_native_controller_then_same_start_continues(self):
        peer = peer_steward.peer_message
        opened = [terminal_surface.PaneResult("herdr", None, "none", ["layout-unavailable"]),
                  terminal_surface.PaneResult("herdr", None, "none", ["layout-unavailable"]),
                  terminal_surface.PaneResult("herdr", "w1:pNew", "split")]
        native_starts = []
        def run(argv, **kwargs):
            self.assertEqual(argv[:3], ["herdr", "agent", "start"])
            native_starts.append(argv)
            return _herdr_json({"result": {"agent": {"agent": "codex", "name": "new",
                "agent_session": {"kind": "id", "value": "new-native"}}}})
        def queued(ref, **kwargs):
            row = peer._read_pending(ref)
            self.assertEqual(row["to"]["session_id"], "original-controller")
            row["state"] = "queued"
            peer._save_pending(row)
            return {"status": "queued"}
        with mock.patch.object(peer_steward, "_current_session_identity", return_value=("original-controller", "codex")), \
             mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner"), \
             mock.patch.object(terminal_surface, "open_pane", side_effect=opened) as opener, \
             mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="ready"), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_read_screen", return_value=None), \
             mock.patch.object(peer_steward, "_pane_is_managed", return_value=False), \
             mock.patch.object(peer, "deliver_pending_codex", side_effect=queued) as queue, \
             contextlib.redirect_stdout(io.StringIO()):
            argv = ["start", "new", "--kind", "codex", "--beside", "w1:pOld", "--cwd", str(self.tmp_root)]
            self.assertEqual(peer_steward.main(argv), 1)
            self.assertEqual(peer_steward.main(argv), 1)
            rows = list(peer._pending_rows())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["from"], rows[0]["to"])
            self.assertIn("w1:pOld", rows[0]["text"])
            store = peer_steward.peer_obligations.ObligationStore()
            duty = store.get("message-" + rows[0]["ref"])
            peer_steward._resume_message_obligation(duty, store)
            peer_steward._resume_message_obligation(store.get(duty["id"]), store)
            self.assertEqual(queue.call_count, 1)
            self.assertEqual(peer_steward.main(argv), 0)
        self.assertEqual(len(native_starts), 1)
        self.assertTrue(all(call.kwargs["near"].pane == "w1:pOld" and call.kwargs["near"].same_tab
                            for call in opener.call_args_list))

    def test_failed_native_results_retain_one_original_request_and_retry_the_effective_target(self):
        import shlex
        peer = peer_steward.peer_message
        cases = [(placement, cleanup) for placement in ("beside", "project")
                 for cleanup in ("closed", "retained", "close-failed")]
        cases += [("before-allocation", None), ("explicit", None), ("default-cwd", "closed")]
        primary, launch = self.tmp_root / "primary", self.tmp_root / "linked" / "nested"
        primary.mkdir(); launch.mkdir(parents=True)
        for index, (placement, cleanup) in enumerate(cases):
            name = "request-" + str(index)
            state = {"failed": True, "agent": None}
            starts, openings, wires = [], [], []
            expected_cwd = str(primary if placement == "default-cwd" else launch)
            def run(argv, **kwargs):
                self.assertEqual(argv[:3], ["herdr", "--session", "selected"])
                action = argv[3:5]
                if action == ["agent", "get"]:
                    return _herdr_json({"result": {"agent": state["agent"]}})
                self.assertEqual(action, ["agent", "start"])
                starts.append(argv)
                if state["failed"]:
                    raise OSError("transient native invocation failure")
                return _herdr_json({"result": {"agent": {"agent": "codex", "name": name}}})
            def open_pane(**kwargs):
                openings.append(kwargs["near"])
                if state["failed"] and placement == "before-allocation":
                    return terminal_surface.PaneResult("herdr", None, "none", ["layout-unavailable"])
                return terminal_surface.PaneResult("herdr", "w1:pNew", "split")
            def placement_result(*argv):
                self.assertEqual(argv, ("agent", "get", "w1:pHint"))
                return {"agent": {"pane_id": "w1:pHint", "workspace_id": "w1",
                                   "foreground_cwd": str(launch)}}
            def queue(ref, **kwargs):
                row = peer._read_pending(ref)
                full = peer.read_peer_payload(ref)
                wires.append(full["body"] if full else row["text"])
                row["state"] = "queued"; peer._save_pending(row)
                return {"status": "queued"}
            with contextlib.ExitStack() as patches:
                patches.enter_context(mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pHint"}))
                patches.enter_context(mock.patch.object(peer_steward, "_HERDR_SESSION", None))
                patches.enter_context(mock.patch.object(peer_steward, "_current_session_identity", return_value=("original-controller", "codex")))
                patches.enter_context(mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner"))
                patches.enter_context(mock.patch.object(peer_steward, "_caller_pane", return_value=""))
                patches.enter_context(mock.patch.object(peer_steward, "_placement_result", side_effect=placement_result))
                patches.enter_context(mock.patch.object(peer_steward.os, "getcwd", return_value=str(launch)))
                patches.enter_context(mock.patch.object(peer_steward.INSTALL_PATHS, "primary_checkout", return_value=Path(expected_cwd)))
                patches.enter_context(mock.patch.object(terminal_surface, "open_pane", side_effect=open_pane))
                patches.enter_context(mock.patch.object(peer_steward.shutil, "which", return_value="herdr"))
                patches.enter_context(mock.patch.object(peer_steward.subprocess, "run", side_effect=run))
                patches.enter_context(mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")))
                patches.enter_context(mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))))
                patches.enter_context(mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="ready"))
                patches.enter_context(mock.patch.object(peer_steward, "_failed_start_cleanup", return_value=cleanup))
                ingress = patches.enter_context(mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None))
                patches.enter_context(mock.patch.object(peer_steward, "_process_session", return_value=None))
                patches.enter_context(mock.patch.object(peer_steward, "_read_screen", return_value=None))
                patches.enter_context(mock.patch.object(peer_steward, "_pane_is_managed", return_value=False))
                patches.enter_context(mock.patch.object(peer, "deliver_pending_codex", side_effect=queue))
                patches.enter_context(contextlib.redirect_stdout(io.StringIO()))
                patches.enter_context(self.subTest(placement=placement, cleanup=cleanup))
                argv = ["--herdr-session", "selected", "start", name, "--kind", "codex"]
                if placement == "explicit":
                    argv += ["--pane", "w1:pExplicit"]
                elif placement != "project":
                    argv += ["--beside", "w1:pOld"]
                if placement != "default-cwd":
                    argv += ["--cwd", str(launch)]
                argv += ["--permission-mode", "inherit", "--", "--model", "fixture-model"]
                self.assertEqual(peer_steward.main(argv), 4 if placement == "explicit" else 1)
                rows = [r for r in peer._pending_rows() if r.get("start_request", {}).get("name") == name]
                self.assertEqual(len(rows), 1)
                row = rows[0]
                identity = {key: row[key] for key in ("ref", "refs", "text", "from", "to")}
                store = peer_steward.peer_obligations.ObligationStore()
                duty = store.get("message-" + row["ref"])
                self.assertEqual(duty["intent"]["identity"]["server"], "selected")
                peer_steward._resume_message_obligation(duty, store)
                peer_steward._resume_message_obligation(store.get(duty["id"]), store)
                self.assertEqual(len(wires), 1)
                self.assertIn('"server": "selected"', wires[0])
                command = shlex.split(wires[0].split("Same start: ", 1)[1].splitlines()[0])
                self.assertEqual(command[command.index("--herdr-session") + 1], "selected")
                self.assertEqual(command[command.index("--cwd") + 1], expected_cwd)
                if cleanup in {"retained", "close-failed"}:
                    self.assertEqual(command[command.index("--pane") + 1], "w1:pNew")
                elif placement == "explicit":
                    self.assertEqual(command[command.index("--pane") + 1], "w1:pExplicit")
                else:
                    self.assertNotIn("--pane", command)
                retry_env = {}
                if command[0] == "env":
                    self.assertEqual(command[:3], ["env", "-C", str(launch)])
                    retry_env["HERDR_PANE_ID"] = command[3].split("=", 1)[1]
                    command = command[4:]
                self.assertEqual(command[:3], ["hearting", "run", "peer-steward"])
                # A second failed invocation of the transmitted command keeps the same
                # original ref even if it now names the retained allocated pane.
                with mock.patch.dict(os.environ, retry_env):
                    self.assertEqual(peer_steward.main(command[3:]), 4 if "--pane" in command else 1)
                    rows = [r for r in peer._pending_rows() if r.get("start_request", {}).get("name") == name]
                    self.assertEqual(len(rows), 1)
                    self.assertEqual({key: rows[0][key] for key in identity}, identity)
                    state["failed"] = False
                    before = len(starts)
                    if "--pane" in command:
                        pane = command[command.index("--pane") + 1]
                        state["agent"] = {"pane_id": pane, "agent": "codex", "name": name,
                                          "cwd": expected_cwd, "agent_session": {"value": "late-native"}}
                    # Simulate another endpoint in the new turn; argv must still select the original.
                    peer_steward._HERDR_SESSION = "other"
                    self.assertEqual(peer_steward.main(command[3:]), 0)
                    self.assertEqual(len(starts) - before, 0 if state["agent"] else 1)
                self.assertEqual(len(wires), 1)
                if placement == "project" and cleanup == "closed":
                    self.assertTrue(all(near.workspace == "w1" and near.pane is None for near in openings))
                if placement in {"beside", "before-allocation", "default-cwd"} and cleanup not in {"retained", "close-failed"}:
                    self.assertTrue(all(near.pane == "w1:pOld" and near.same_tab for near in openings))

    def test_closed_split_retry_command_keeps_endpoint_and_original_beside(self):
        import shlex
        with contextlib.ExitStack() as patches:
            patches.enter_context(mock.patch.object(peer_steward, "_current_session_identity", return_value=("original-controller", "codex")))
            patches.enter_context(patch_split(peer_steward, "_herdr_missing", return_value=False))
            patches.enter_context(mock.patch.object(terminal_surface, "open_pane", return_value=terminal_surface.PaneResult("herdr", "w1:pNew", "split")))
            patches.enter_context(mock.patch.object(peer_steward.subprocess, "run", side_effect=OSError("start unavailable")))
            patches.enter_context(mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")))
            patches.enter_context(mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))))
            patches.enter_context(mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="ready"))
            patches.enter_context(mock.patch.object(peer_steward, "_failed_start_cleanup", return_value="closed"))
            patches.enter_context(mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None))
            patches.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(peer_steward.main(["--herdr-session", "selected", "start", "original",
                                               "--kind", "codex", "--beside", "w1:pOld",
                                               "--cwd", str(self.tmp_root)]), 1)
        row, = peer_steward.peer_message._pending_rows()
        full = peer_steward.peer_message.read_peer_payload(row["ref"])
        body = full["body"] if full else row["text"]
        command = shlex.split(body.split("Same start: ", 1)[1].splitlines()[0])
        self.assertNotIn("--pane", command, "cleanup closed the allocated pane")
        self.assertEqual(command[command.index("--beside") + 1], "w1:pOld")
        self.assertEqual(command[command.index("--herdr-session") + 1], "selected")

    def test_native_refusal_exit_codes_and_unavailable_requester_keep_real_outcome(self):
        peer = peer_steward.peer_message
        for index, (rc, stdout, stderr) in enumerate(((0, {"error": {"code": "agent_name_taken"}}, ""),
                                                     (1, {"error": {"code": "agent_name_taken"}}, ""),
                                                     (2, None, "native refused"))):
            with self.subTest(rc=rc), \
                 mock.patch.object(peer_steward, "_current_session_identity", return_value=("original-controller", "codex")), \
                 patch_split(peer_steward, "_herdr_missing", return_value=False), \
                 mock.patch.object(terminal_surface, "open_pane", return_value=terminal_surface.PaneResult("herdr", "w1:pNew", "split")), \
                 mock.patch.object(peer_steward.subprocess, "run", return_value=subprocess.CompletedProcess([], rc, json.dumps(stdout) if stdout else "", stderr)), \
                 mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
                 mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
                 mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="ready"), \
                 mock.patch.object(peer_steward, "_failed_start_cleanup", return_value="closed"), \
                 mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                argv = ["start", "refused-" + str(index), "--kind", "codex", "--beside", "w1:pOld"]
                self.assertEqual(peer_steward.main(argv), 1)
                self.assertIn("started=false", output.getvalue())
                rows = list(peer._pending_rows())
                self.assertEqual(len(rows), index + 1)
                with mock.patch.object(peer_steward, "_current_session_identity", return_value=(None, None)):
                    self.assertEqual(peer_steward.main(argv), 1)
                self.assertEqual(len(list(peer._pending_rows())), index + 1)

    def test_default_cwd_resolves_a_real_linked_worktree_subfolder_to_primary(self):
        primary, linked = self.tmp_root / "primary", self.tmp_root / "linked"
        native_run = subprocess.run
        for argv in (["git", "init", "-q", str(primary)],
                     ["git", "-C", str(primary), "-c", "user.name=Fixture", "-c",
                      "user.email=fixture@example.invalid", "commit", "-q", "--allow-empty", "-m", "fixture"],
                     ["git", "-C", str(primary), "worktree", "add", "-q", "-b", "linked", str(linked)]):
            native_run(argv, check=True, capture_output=True, text=True)
        subfolder = linked / "nested"
        subfolder.mkdir()
        self.primary_mock.side_effect = self.resolve_primary
        calls = []

        def run(argv, **kw):
            if argv[0] == "git":
                return native_run(argv, **kw)
            calls.append(argv)
            if argv[:3] == ["herdr", "pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
            if argv[:3] == ["herdr", "agent", "start"]:
                self.assertEqual(kw["cwd"], str(primary))
                return _herdr_json({"result": {"agent": {"agent": "codex", "name": "new"}}})
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.os, "getcwd", return_value=str(subfolder)), \
             mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="ready"), \
             mock.patch.object(peer_steward, "_read_screen", return_value=None), \
             mock.patch.object(peer_steward, "_pane_is_managed", return_value=False), \
             mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["start", "new", "--kind", "codex", "--beside", "w1:pOld"]), 0)
        self.assertEqual(calls[0][-2:], ["--cwd", str(primary)])
        self.assertEqual(calls[1][-2:], ["--cd", str(primary)])
        self.assertIn("cwd=" + str(primary), printed.call_args[0][0])

    def test_opencode_late_bootstrap_waits_for_stable_shell_before_one_native_start(self):
        clock, events = {"now": 0.0}, []

        def export(pane):
            events.append("export")
            return "exported"

        def snapshot(pane, shell, deadline=None):
            self.assertEqual(deadline, peer_steward._BESIDE_READY_SECONDS)
            events.append("snapshot")
            return "settled" if clock["now"] >= .05 else None

        def run(argv, **kw):
            if argv[:3] == ["herdr", "pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
            if argv[:3] == ["herdr", "agent", "start"]:
                self.assertGreaterEqual(clock["now"], .05)
                events.append("native start")
                return _herdr_json({"result": {"agent": {"agent": "opencode", "name": "new"}}})
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_export_opencode_tui_scoped", side_effect=export), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", side_effect=snapshot), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: clock["now"]), \
             mock.patch.object(peer_steward.time, "sleep", side_effect=lambda n: clock.update(now=clock["now"] + n)), \
             mock.patch.object(peer_steward, "_read_screen", return_value=None), mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["start", "new", "--kind", "opencode", "--beside", "w1:pOld",
                                               "--cwd", str(self.tmp_root)]), 0)
        self.assertEqual(events, ["export", "snapshot", "snapshot", "native start"])
        self.assertIn("started=true", printed.call_args[0][0])
        self.primary_mock.assert_not_called()

    def test_unstable_bootstrap_uses_original_deadline_and_offers_retained_pane_reuse(self):
        import shlex
        clock = {"now": 0.0}
        split = _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=split) as run, \
             mock.patch.object(peer_steward, "_BESIDE_READY_SECONDS", .1), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_export_opencode_tui_scoped", return_value="exported"), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", return_value=None), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: clock["now"]), \
             mock.patch.object(peer_steward.time, "sleep", side_effect=lambda n: clock.update(now=clock["now"] + n)), \
             mock.patch.object(peer_steward, "_close_pane") as close, mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["start", "new", "--kind", "opencode", "--beside", "w1:pOld",
                                               "--cwd", str(self.tmp_root), "--permission-mode", "inherit",
                                               "--", "--model", "user/model"]), 1)
        self.assertEqual(clock["now"], .1)
        self.assertEqual(run.call_count, 1)
        close.assert_not_called()
        line = printed.call_args[0][0]
        self.assertIn("reason=beside-shell-readiness-timeout", line)
        self.assertIn("pane_cleanup=retained", line)
        reuse = next(v.split("=", 1)[1] for v in shlex.split(line) if v.startswith("reuse_command="))
        self.assertEqual(shlex.split(reuse), ["hearting", "run", "peer-steward", "--herdr-session", "default", "start", "new",
                         "--kind", "opencode", "--pane", "w1:pN", "--cwd", str(self.tmp_root),
                         "--permission-mode", "inherit", "--", "--model", "user/model"])

    def test_same_tab_right_no_focus_split_reuses_start_with_new_pane_and_cwd(self):
        calls = []
        cwd = str(self.tmp_root)

        def run(argv, **kw):
            calls.append(list(argv))
            if argv[:3] == ["herdr", "pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False,
                                                          "tab_id": "w1:tA"}}})
            if argv[:3] == ["herdr", "agent", "start"]:
                return _herdr_json({"result": {"agent": {"agent": "claude", "name": "new",
                    "pane_id": "w1:pN", "agent_session": {"value": "new-native-sid"}}}})
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None) as ingress, \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="recorded screen"), \
             mock.patch.object(peer_steward, "_read_screen", return_value=None), \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["start", "new", "--kind", "claude", "--beside", "w1:pOld",
                                   "--cwd", cwd, "--permission-mode", "inherit", "--", "--model", "opus"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0], ["herdr", "pane", "split", "--pane", "w1:pOld", "--direction",
                                   "right", "--ratio", "0.5", "--no-focus", "--cwd", cwd])
        self.assertEqual(calls[1], ["herdr", "agent", "start", "new", "--kind", "claude",
                                   "--pane", "w1:pN", "--", "--model", "opus"])
        ingress.assert_called_once_with("w1:pN", "claude", None)
        self.assertIn("started=true", printed.call_args[0][0])
        self.assertIn("pane=w1:pN", printed.call_args[0][0])

    def test_beside_waits_for_delayed_split_cwd_before_single_start(self):
        calls, events = [], []
        cwd = str(self.tmp_root)
        clock = {"now": 0.0}

        def run(argv, **kw):
            calls.append(list(argv))
            if argv[:3] == ["herdr", "pane", "split"]:
                self.assertEqual(argv[-2:], ["--cwd", cwd])
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
            if argv[:3] == ["herdr", "pane", "get"]:
                return _herdr_json({"result": {"pane": {"agent": None}}})
            if argv[:3] == ["herdr", "pane", "process-info"]:
                busy = clock["now"] < .2
                return _herdr_json({"result": {"process_info": {"pane_id": "w1:pN",
                    "shell_pid": 101, "foreground_process_group_id": 202 if busy else 101,
                    "foreground_processes": [{"pid": 202 if busy else 101}]}}})
            if argv[:3] == ["herdr", "pane", "wait-output"]:
                self.assertGreaterEqual(clock["now"], .2)
                events.append("ready prompt")
                return subprocess.CompletedProcess(argv, 0, stdout="prompt", stderr="")
            if argv[:3] == ["herdr", "agent", "start"]:
                self.assertGreaterEqual(clock["now"], .2)
                self.assertIn("snapshot", events)
                events.append("native start")
                return _herdr_json({"result": {"agent": {"agent": "claude", "name": "new",
                    "pane_id": "w1:pN", "agent_session": {"value": "new-native-sid"}}}})
            raise AssertionError(argv)

        def snapshot(pane, shell, deadline=None):
            self.assertGreaterEqual(clock["now"], .2)
            self.assertEqual((pane, shell), ("w1:pN", (101, "700")))
            events.append("snapshot")
            return "ready screen"

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             patch_split(peer_steward, "_managed_ingress_dir", return_value=None), \
             patch_split(peer_steward, "_proc_start_ticks", return_value="700"), \
             patch_split(peer_steward, "_proc_cwd", side_effect=lambda pid:
                               cwd if clock["now"] >= .2 else "/previous"), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: clock["now"]), \
             mock.patch.object(peer_steward.time, "sleep", side_effect=lambda seconds:
                               clock.update(now=clock["now"] + seconds)), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", side_effect=snapshot), \
             mock.patch.object(peer_steward, "_read_screen", return_value=None), \
             mock.patch.object(peer_steward, "_close_pane") as close, mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["start", "new", "--kind", "claude", "--beside", "w1:pOld",
                                   "--cwd", cwd, "--permission-mode", "inherit"])
        self.assertEqual(rc, 0)
        self.assertIn("started=true", printed.call_args[0][0])
        self.assertEqual(sum(a[:3] == ["herdr", "agent", "start"] for a in calls), 1)
        self.assertFalse(any(a[:3] in (["herdr", "pane", "send-text"],
                                     ["herdr", "pane", "send-keys"]) for a in calls))
        self.assertEqual(events[-2:], ["snapshot", "native start"])
        close.assert_not_called()

    def test_slow_nas_prompt_finishes_in_the_same_start_call_on_every_harness(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                clock, calls = {"now": 0.0}, []
                cwd = str(self.tmp_root)

                def run(argv, **kw):
                    calls.append(list(argv))
                    if argv[:3] == ["herdr", "pane", "split"]:
                        return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
                    if argv[:3] == ["herdr", "pane", "get"]:
                        return _herdr_json({"result": {"pane": {"agent": None}}})
                    if argv[:3] == ["herdr", "pane", "process-info"]:
                        processes = [{"pid": 101, "argv": ["zsh"]}]
                        if clock["now"] < 240:
                            processes.append({"pid": 202, "argv": ["git", "status"]})
                        return _herdr_json({"result": {"process_info": {"pane_id": "w1:pN",
                            "shell_pid": 101, "foreground_process_group_id": 101,
                            "foreground_processes": processes}}})
                    if argv[:3] == ["herdr", "pane", "wait-output"]:
                        self.assertGreaterEqual(clock["now"], 240)
                        return subprocess.CompletedProcess(argv, 0, stdout="prompt", stderr="")
                    if argv[:3] == ["herdr", "agent", "start"]:
                        self.assertEqual(argv[7], "w1:pN")
                        self.assertGreaterEqual(clock["now"], 240)
                        return _herdr_json({"result": {"agent": {"agent": harness, "name": "new"}}})
                    raise AssertionError(argv)

                with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
                     mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
                     patch_split(peer_steward, "_proc_start_ticks", return_value="700"), \
                     patch_split(peer_steward, "_proc_cwd", return_value=cwd), \
                     patch_split(peer_steward, "_managed_ingress_dir", return_value=None), \
                     mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
                     mock.patch.object(peer_steward, "_export_opencode_tui_scoped", return_value="missing"), \
                     mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="stable prompt"), \
                     mock.patch.object(peer_steward, "_read_screen", return_value=None), \
                     mock.patch.object(peer_steward, "_pane_is_managed", return_value=False), \
                     mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: clock["now"]), \
                     mock.patch.object(peer_steward.time, "sleep", side_effect=lambda seconds:
                                       clock.update(now=clock["now"] + max(seconds, 5))), \
                     mock.patch.object(peer_steward, "_close_pane") as close, mock.patch("builtins.print") as printed:
                    rc = peer_steward.main(["start", "new", "--kind", harness, "--beside", "w1:pOld",
                                           "--cwd", cwd, "--permission-mode", "inherit"])
                self.assertEqual(rc, 0)
                self.assertIn("started=true", printed.call_args[0][0])
                self.assertEqual(sum(a[:3] == ["herdr", "agent", "start"] for a in calls), 1)
                self.assertFalse(any(a[:3] in (["herdr", "pane", "send-text"],
                                             ["herdr", "pane", "send-keys"]) for a in calls))
                self.assertLess(clock["now"], 300)
                close.assert_not_called()

    def test_slow_shell_replacement_still_refuses_launch(self):
        info = {"pane_id": "w1:pN", "shell_pid": 101,
                "foreground_process_group_id": 101,
                "foreground_processes": [{"pid": 101}, {"pid": 202}]}
        with mock.patch.object(peer_steward, "_pane_has_agent", return_value=None), \
             mock.patch.object(peer_steward, "_retire_pane_info", return_value=info), \
             patch_split(peer_steward, "_proc_start_ticks", side_effect=["700", "701"]), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=None), \
             mock.patch.object(peer_steward.time, "sleep"), \
             mock.patch.object(peer_steward, "_wait_for_shell_prompt") as prompt:
            reason, shell = peer_steward._wait_for_created_shell(
                "w1:pN", "/fixture", (101, "700"), time.monotonic() + 300)
        self.assertEqual((reason, shell), ("beside-shell-changed", (101, "700")))
        prompt.assert_not_called()

    def test_beside_readiness_deadline_retains_pane_without_start_or_snapshot(self):
        clock = {"now": 0.0}
        calls = []
        cwd = str(self.tmp_root)

        def run(argv, **kw):
            calls.append(list(argv))
            if argv[:3] == ["herdr", "pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
            if argv[:3] == ["herdr", "pane", "get"]:
                return _herdr_json({"result": {"pane": {"agent": None}}})
            if argv[:3] == ["herdr", "pane", "process-info"]:
                return _herdr_json({"result": {"process_info": {"pane_id": "w1:pN",
                    "shell_pid": 101, "foreground_process_group_id": 101,
                    "foreground_processes": [{"pid": 101}]}}})
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_BESIDE_READY_SECONDS", .1), \
             patch_split(peer_steward, "_proc_start_ticks", return_value="700"), \
             patch_split(peer_steward, "_proc_cwd", return_value="/previous"), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: clock["now"]), \
             mock.patch.object(peer_steward.time, "sleep", side_effect=lambda seconds:
                               clock.update(now=clock["now"] + seconds)), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress") as ingress, \
             mock.patch.object(peer_steward, "_start_shell_snapshot") as snapshot, \
             mock.patch.object(peer_steward, "_close_pane") as close, mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["start", "new", "--kind", "claude", "--beside", "w1:pOld",
                                   "--cwd", cwd, "--permission-mode", "inherit"])
        self.assertEqual(rc, 1)
        self.assertIn("started=false reason=beside-shell-readiness-timeout", printed.call_args[0][0])
        self.assertIn("pane=w1:pN", printed.call_args[0][0])
        self.assertIn("pane_cleanup=retained", printed.call_args[0][0])
        self.assertEqual(clock["now"], .1)
        self.assertFalse(any(a[:3] == ["herdr", "agent", "start"] for a in calls))
        ingress.assert_not_called(); snapshot.assert_not_called(); close.assert_not_called()

    def test_split_failure_malformed_focused_or_same_pane_never_starts_or_types(self):
        cases = [subprocess.CompletedProcess([], 1, stdout="", stderr="denied"),
                 subprocess.CompletedProcess([], 0, stdout="null", stderr=""),
                 _herdr_json({"error": {"code": "pane_not_found"}}),
                 _herdr_json({"result": {"pane": {"pane_id": "w1:pOld", "focused": False}}}),
                 _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": True}}}),
                 _herdr_json({"result": {"pane": {"pane_id": "w1:pN"}}})]
        for response in cases:
            with self.subTest(response=response), \
                 mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
                 mock.patch.object(peer_steward.subprocess, "run", return_value=response) as run, \
                 mock.patch.object(peer_steward, "_ensure_pane_ingress") as ingress, \
                 mock.patch("builtins.print") as printed:
                rc = peer_steward.main(["start", "new", "--kind", "codex", "--beside", "w1:pOld"])
                self.assertEqual(rc, 1)
                self.assertEqual(run.call_count, 1)
                ingress.assert_not_called()
                self.assertIn("started=false reason=pane-split-failed", printed.call_args[0][0])

    def test_start_with_no_pane_named_opens_beside_the_calling_pane_in_the_calling_cwd(self):
        response = _herdr_json({"error": {"code": "pane_not_found"}})   # stop right after the split
        with tempfile.TemporaryDirectory() as cwd, \
             mock.patch.dict(peer_steward.os.environ, {"HERDR_PANE_ID": "w1:pMe"}), \
             mock.patch.object(peer_steward.os, "getcwd", return_value=cwd), \
             mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=response) as run, \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(["start", "new", "--kind", "codex"]), 1)
            split = run.call_args_list[0][0][0]
            self.assertEqual(split[:5], ["herdr", "pane", "split", "--pane", "w1:pMe"])
            self.assertEqual(split[split.index("--cwd") + 1], os.path.realpath(cwd))
        self.assertEqual(self.resolve_primary(cwd), Path(cwd))

    def test_start_outside_a_herdr_pane_with_no_pane_named_says_so_and_runs_nothing(self):
        env = {k: v for k, v in os.environ.items() if k != "HERDR_PANE_ID"}
        with mock.patch.dict(peer_steward.os.environ, env, clear=True), \
             mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run") as run, \
             mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["start", "new", "--kind", "claude"]), 1)
        run.assert_not_called()
        self.assertIn("started=false reason=pane-unknown", printed.call_args[0][0])

    def test_non_json_start_failure_has_bounded_reason_and_closes_only_owned_empty_split(self):
        calls = []

        def run(argv, **kw):
            calls.append(list(argv))
            if argv[:3] == ["herdr", "pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
            if argv[:3] == ["herdr", "agent", "start"]:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="\x1b[31mError: name too long\x1b[0m\n" + "secret second line")
            if argv[:3] == ["herdr", "pane", "close"]:
                return _herdr_json({"result": {"type": "ok"}})
            if argv[:3] == ["herdr", "pane", "process-info"]:
                return _herdr_json({"result": {"process_info": {"pane_id": "w1:pN", "shell_pid": 101,
                    "foreground_process_group_id": 101, "foreground_processes": [{"pid": 101}]}}})
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="recorded screen"), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_pane_has_agent", return_value=None), \
             patch_split(peer_steward, "_proc_start_ticks", return_value="700"), \
             mock.patch.object(peer_steward, "_start_pane_screen", return_value="recorded screen"), \
             mock.patch("builtins.print") as printed:
            peer_steward.main(["start", "too-long", "--kind", "claude", "--beside", "w1:pOld"])
        self.assertIn("started=false", printed.call_args[0][0])
        self.assertIn("reason=herdr-stderr-error-name-too-long", printed.call_args[0][0])
        self.assertIn("pane_cleanup=closed", printed.call_args[0][0])
        self.assertNotIn("secret", printed.call_args[0][0])
        self.assertEqual(calls[-1], ["herdr", "pane", "close", "w1:pN"])
        self.assertFalse(any(a[:3] == ["herdr", "pane", "close"] and a[-1] == "w1:pOld" for a in calls))

    def test_failed_cleanup_refuses_agent_changed_shell_unknown_or_draft(self):
        for occupied, shell, prompt in (("pane-occupied", (101, "700"), True),
                                        ("pane-unknown", (101, "700"), True),
                                        (None, (101, "701"), True),
                                        (None, None, True), (None, (101, "700"), False)):
            clock = iter(range(20))
            with self.subTest(occupied=occupied, shell=shell, prompt=prompt), \
                 mock.patch.object(peer_steward, "_pane_has_agent", return_value=occupied), \
                 mock.patch.object(peer_steward, "_start_shell_identity", return_value=shell), \
                 patch_split(peer_steward, "_proc_start_ticks", return_value="701" if shell == (101, "701") else "700"), \
                 mock.patch.object(peer_steward, "_retire_pane_info", return_value=None if shell is None else {"shell_pid": 101}), \
                 mock.patch.object(peer_steward, "_start_pane_screen", return_value="recorded screen" if prompt else "changed screen"), \
                 mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
                 mock.patch.object(peer_steward.time, "sleep"), \
                 mock.patch.object(peer_steward, "_close_pane") as close:
                self.assertEqual(peer_steward._failed_start_cleanup("w1:pN", (101, "700"), "recorded screen"), "retained")
                close.assert_not_called()

    def test_existing_pane_failure_never_cleans_up_and_stderr_is_bounded(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_wait_for_created_shell") as readiness, \
             mock.patch.object(peer_steward.subprocess, "run", return_value=subprocess.CompletedProcess(
                 [], 1, stdout="", stderr="Error: duplicate name\n")) as run, \
             mock.patch.object(peer_steward, "_failed_start_cleanup") as cleanup, mock.patch("builtins.print") as printed:
            peer_steward.main(["start", "old", "--kind", "claude", "--pane", "w1:pOld"])
            self.assertIn("reason=herdr-stderr-error-duplicate-name", printed.call_args[0][0])
            cleanup.assert_not_called(); self.assertEqual(run.call_count, 2)  # one observation, one start
            readiness.assert_not_called()
        code = peer_steward._start_stderr_code("Error: " + "long\tvalue\x00" * 100 + "\n")
        self.assertLessEqual(len(code.encode()), 80)
        self.assertRegex(code, r"^[a-z0-9_-]+$")

    def test_failed_fresh_start_waits_for_same_shell_then_closes_once(self):
        clock = iter(range(20))
        with mock.patch.object(peer_steward, "_pane_has_agent", return_value=None), \
             patch_split(peer_steward, "_proc_start_ticks", return_value="700"), \
             mock.patch.object(peer_steward, "_retire_pane_info", return_value={"shell_pid": 101}), \
             mock.patch.object(peer_steward, "_start_shell_identity", side_effect=[None, (101, "700"), (101, "700")]), \
             mock.patch.object(peer_steward, "_start_pane_screen", return_value="recorded screen"), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(peer_steward.time, "sleep") as pause, \
             mock.patch.object(peer_steward, "_close_pane", return_value=True) as close:
            self.assertEqual(peer_steward._failed_start_cleanup("w1:pN", (101, "700"), "recorded screen"), "closed")
            pause.assert_called_once()
            close.assert_called_once_with("w1:pN")

    def test_fresh_snapshot_requires_two_stable_nonblank_reads_and_same_shell(self):
        cases = [(["recorded screen", "recorded screen"], "recorded screen"),
                 (["", "recorded screen", "recorded screen"], "recorded screen"),
                 ([""] * 100, None), ([str(i) for i in range(100)], None), ([None], None)]
        for screens, expected in cases:
            clock = iter(i / 100 for i in range(1000))
            with self.subTest(screens=screens[:3]), \
                 mock.patch.object(peer_steward, "_pane_has_agent", return_value=None), \
                 mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
                 mock.patch.object(peer_steward, "_start_pane_screen", side_effect=screens), \
                 mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
                 mock.patch.object(peer_steward.time, "sleep"):
                self.assertEqual(peer_steward._start_shell_snapshot("w1:pN", (101, "700")), expected)
        with mock.patch.object(peer_steward, "_pane_has_agent", return_value="pane-unknown"), \
             mock.patch.object(peer_steward, "_start_pane_screen") as read:
            self.assertIsNone(peer_steward._start_shell_snapshot("w1:pN", (101, "700")))
            read.assert_not_called()

    def test_visible_screen_is_verbatim_and_unavailable_read_is_unknown(self):
        for stdout, rc, expected in (("user@host $ ", 0, "user@host $ "),
                ("\x1b[0mhost  draft", 0, "\x1b[0mhost  draft"), ("", 0, ""),
                ("denied", 1, None), ("x" * 65537, 0, None), ('{"error":{"code":"denied"}}', 0, None)):
            with self.subTest(rc=rc, length=len(stdout)), mock.patch.object(peer_steward.subprocess,
                    "run", return_value=subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")) as run:
                self.assertEqual(peer_steward._start_pane_screen("w1:pN"), expected)
                self.assertEqual(run.call_args[0][0], ["herdr", "pane", "read", "w1:pN",
                    "--source", "visible", "--format", "ansi"])

    def test_post_record_drafts_and_redraw_never_close_owned_split(self):
        plain = "user@host $ "
        powerline = "user@host  /work  main  "
        styled = "\x1b[0m\x1b[38;5;2m\x1b[0m "
        cases = [(plain, "user@host $ echo $ "), ("user@host % ", "user@host % printf % "),
                 ("user@host ❯ ", "user@host ❯ : # "), (plain, "user@host $echo $ "),
                 ("%", "%printf % "), ("❯", "❯: # "),
                 (powerline, powerline + "echo " + styled),
                 (powerline, powerline + "printf " + styled),
                 (plain, "redrawn prompt"), (plain, None)]
        for recorded, current in cases:
            with self.subTest(current=current), \
                 mock.patch.object(peer_steward, "_pane_has_agent", return_value=None), \
                 patch_split(peer_steward, "_proc_start_ticks", return_value="700"), \
                 mock.patch.object(peer_steward, "_retire_pane_info", return_value={"shell_pid": 101}), \
                 mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
                 mock.patch.object(peer_steward, "_start_pane_screen", return_value=current), \
                 mock.patch.object(peer_steward, "_close_pane", return_value=True) as close:
                self.assertEqual(peer_steward._failed_start_cleanup("w1:pN", (101, "700"), recorded), "retained")
                close.assert_not_called()
        with mock.patch.object(peer_steward, "_close_pane") as close:
            self.assertEqual(peer_steward._failed_start_cleanup("w1:pN", (101, "700"), None), "retained")
            close.assert_not_called()

    def test_beside_records_stable_screen_before_start_and_retains_later_change(self):
        events = []
        screens = iter(["before", "before", "after draft"])
        clock = iter(i / 100 for i in range(1000))

        def read(pane):
            value = next(screens); events.append("screen:" + value); return value

        def run(argv, **kw):
            if argv[:3] == ["herdr", "pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
            if argv[:3] == ["herdr", "agent", "start"]:
                events.append("native start")
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="start rejected")
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_pane_has_agent", return_value=None), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             patch_split(peer_steward, "_proc_start_ticks", return_value="700"), \
             mock.patch.object(peer_steward, "_retire_pane_info", return_value={"shell_pid": 101}), \
             mock.patch.object(peer_steward, "_start_pane_screen", side_effect=read), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(peer_steward.time, "sleep"), \
             mock.patch.object(peer_steward, "_close_pane") as close, mock.patch("builtins.print") as printed:
            peer_steward.main(["start", "new", "--kind", "claude", "--beside", "w1:pOld"])
            self.assertIn("pane_cleanup=retained", printed.call_args[0][0])
            close.assert_not_called()
        self.assertEqual(events, ["screen:before", "screen:before", "native start", "screen:after draft"])

    def test_invalid_cwd_does_not_split_and_pane_choices_are_exclusive(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run") as run, mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(["start", "new", "--kind", "codex", "--beside", "w1:pOld",
                                               "--cwd", str(self.tmp_root / "missing")]), 1)
        run.assert_not_called()
        with mock.patch.object(peer_steward.sys, "stderr"), self.assertRaises(SystemExit):
            peer_steward.build_parser().parse_args(["start", "new", "--kind", "codex", "--pane", "w1:pA",
                                                   "--beside", "w1:pB"])


class RetireTest(_TmpRootMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # The fixture PIDs (101 shell, 4242 agent) are plain numbers, not live
        # processes: on a host where either belongs to another user, reading its
        # /proc environ fails and the pane looks unverifiable.
        patcher = mock.patch.object(peer_steward.peer_obligations,
                                    "execution_attempts_for_processes", return_value=())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_runner_finishes_delayed_claude_background_dialog_without_another_exit(self):
        world = _RetireWorld(harness="claude", exits=False)
        rc, line = self.retire(world)
        self.assertEqual(rc, 0)
        self.assertIn("reason=agent-still-running", line)
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.list()[0]
        self.assertEqual(duty["observation"]["phase"], "exit-requested")
        world.screen = ("Background work is running\nlocal monitor\n"
                        "❯ 1. Exit and stop tasks\n2. Move to background and exit\n"
                        "3. Stay\nEnter to confirm · Esc to cancel\n")
        normal_run = world.run
        selected = []

        def finish_dialog(argv, **kwargs):
            if argv[:3] == ["herdr", "pane", "send-text"]:
                selected.append(argv[-1])
            if argv[:3] == ["herdr", "pane", "send-keys"] and selected == ["1"]:
                world.exits = True
            return normal_run(argv, **kwargs)

        world.run = finish_dialog
        self._run_retire_runner(world, once=True)
        settled = store.get(duty["id"])
        self.assertEqual((settled["state"], settled["result"], settled["cleanup"]),
                         ("complete", "normal-exit", "complete"))
        self.assertEqual(world.actions(), [
            ["herdr", "pane", "send-text", "w1:pOld", "/exit"],
            ["herdr", "pane", "send-keys", "w1:pOld", "enter"],
            ["herdr", "pane", "send-text", "w1:pOld", "1"],
            ["herdr", "pane", "send-keys", "w1:pOld", "enter"],
            ["herdr", "pane", "close", "w1:pOld"],
        ])
        self.assertTrue(any(call[:4] == ["herdr", "agent", "read", "w1:pOld"]
                            for call in world.calls))

    def test_runner_does_not_confirm_ambiguous_or_foreign_retire_dialog(self):
        dialog = ("Background work is running\n❯ 1. Exit and stop tasks\n"
                  "2. Move to background and exit\n3. Stay\n"
                  "Enter to confirm · Esc to cancel\n")
        cases = [
            ("claude", "Do you want to proceed?\nEnter to confirm\n", None),
            ("claude", dialog + "another prompt\n", None),
            ("claude", dialog, {"foreground_process_group_id": 999}),
            ("codex", dialog, None),
            ("opencode", dialog, None),
        ]
        for harness, screen, changed in cases:
            with self.subTest(harness=harness, screen=screen, changed=changed):
                world = _RetireWorld(harness=harness, exits=False)
                self.retire(world)
                actions = world.actions()
                world.screen, world.bad_process = screen, changed
                self._run_retire_runner(world, once=True)
                self.assertEqual(world.actions(), actions)
                duty = peer_steward.peer_obligations.ObligationStore().list()[0]
                self.assertEqual(duty["observation"]["phase"], "exit-requested")

    def test_late_background_confirmation_keeps_failed_pane_close_for_cleanup(self):
        world = _RetireWorld(harness="claude", exits=False)
        self.retire(world)
        store = peer_steward.peer_obligations.ObligationStore()
        duty_id = store.list()[0]["id"]
        world.screen = ("Background work is running\n❯ 1. Exit and stop tasks\n"
                        "2. Move to background and exit\n3. Stay\n"
                        "Enter to confirm · Esc to cancel\n")
        world.close_error = True
        normal_run = world.run

        def finish_dialog(argv, **kwargs):
            if argv[:3] == ["herdr", "pane", "send-keys"]:
                world.exits = True
            return normal_run(argv, **kwargs)

        world.run = finish_dialog
        self._run_retire_runner(world, once=True)
        pending = store.get(duty_id)
        self.assertEqual((pending["state"], pending["observation"]["phase"]),
                         ("cleanup-pending", "shell-returned"))
        before_cleanup = world.actions()
        world.close_error = False
        self._run_retire_runner(world, once=True)
        self.assertEqual(store.get(duty_id)["state"], "complete")
        self.assertEqual(world.actions(), before_cleanup + [
            ["herdr", "pane", "close", "w1:pOld"]])

    def test_inline_background_confirmation_yields_to_existing_retire_runner(self):
        world = _RetireWorld(harness="claude", exits=False)
        store = peer_steward.peer_obligations.ObligationStore()
        with (store.root / "runner.lock").open("w") as lock, \
             mock.patch.object(peer_steward, "_retire_claude_background_confirm") as confirm:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            rc, line = self.retire(world)
            self.assertEqual(rc, 0)
            self.assertIn("reason=retire-already-pending", line)
            confirm.assert_not_called()

    def test_waiting_retire_answers_immediate_background_dialog_in_same_runner_pass(self):
        world = _RetireWorld(harness="claude", status="working", exits=False)
        self.retire(world)
        store = peer_steward.peer_obligations.ObligationStore()
        duty_id = store.list()[0]["id"]
        self.assertEqual(store.get(duty_id)["observation"].get("phase", "waiting"), "waiting")
        self.assertEqual(world.actions(), [])
        dialog = ("Background work is running\n❯ 1. Exit and stop tasks\n"
                  "2. Move to background and exit\n3. Stay\n"
                  "Enter to confirm · Esc to cancel\n")
        world.status = "idle"
        normal_run = world.run
        selected = []

        def exit_and_confirm(argv, **kwargs):
            if argv[:3] == ["herdr", "pane", "send-text"]:
                selected.append(argv[-1])
            if argv[:3] == ["herdr", "pane", "send-keys"]:
                if selected == ["/exit"]:
                    world.screen = dialog
                elif selected == ["/exit", "1"]:
                    world.exits = True
            return normal_run(argv, **kwargs)

        world.run = exit_and_confirm
        self._run_retire_runner(world, once=True)
        settled = store.get(duty_id)
        self.assertEqual((settled["state"], settled["result"], settled["cleanup"]),
                         ("complete", "normal-exit", "complete"))
        self.assertEqual(world.actions(), [
            ["herdr", "pane", "send-text", "w1:pOld", "/exit"],
            ["herdr", "pane", "send-keys", "w1:pOld", "enter"],
            ["herdr", "pane", "send-text", "w1:pOld", "1"],
            ["herdr", "pane", "send-keys", "w1:pOld", "enter"],
            ["herdr", "pane", "close", "w1:pOld"],
        ])

    def test_retire_subject_treats_only_same_birth_same_namespace_zombie_as_gone(self):
        def stat_row(state, start):
            fields = [state, *(["0"] * 18), start]
            return "4242 (opencode) " + " ".join(fields)

        def readlink(path):
            return "pid:[same]" if str(path) in {"/proc/4242/ns/pid", "/proc/self/ns/pid"} else ""

        with mock.patch.object(peer_steward.Path, "read_text", return_value=stat_row("Z", "800")), \
             mock.patch.object(peer_steward.os, "readlink", side_effect=readlink):
            self.assertTrue(peer_steward._retire_zombie_matches(4242, "800"))
            self.assertFalse(peer_steward._retire_zombie_matches(4242, "801"))

        def foreign_namespace(path):
            return "pid:[foreign]" if str(path) == "/proc/4242/ns/pid" else "pid:[same]"

        with mock.patch.object(peer_steward.Path, "read_text", return_value=stat_row("Z", "800")), \
             mock.patch.object(peer_steward.os, "readlink", side_effect=foreign_namespace):
            self.assertFalse(peer_steward._retire_zombie_matches(4242, "800"))

        duty = {"intent": {"identity": {"foreground": {"pid": 4242, "start": "800"}}}}
        with patch_split(peer_steward, "_retire_process_record", return_value=None), \
             patch_split(peer_steward, "_retire_zombie_matches", return_value=True):
            self.assertEqual(peer_steward._retire_subject_state(duty), "gone")

    def test_exact_opencode_native_status_overrides_stale_herdr_state(self):
        world = _RetireWorld(harness="opencode", status="working")
        native = {"status": "idle"}
        with mock.patch.object(peer_steward.herdr_observation, "read_native_status",
                               return_value=native) as read_status:
            rc, line = self.retire(world)
        self.assertEqual(rc, 0, line)
        self.assertIn("retired=true reason=normal-exit", line)
        self.assertEqual(world.actions(), [
            ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
            ["herdr", "pane", "close", "w1:pOld"],
        ])
        self.assertTrue(any(call.args[0:4] == ("default", "w1:pOld", "opencode", "old-sid")
                            and call.args[4:] == (4242, "800")
                            for call in read_status.call_args_list))

    def test_exact_opencode_native_busy_status_overrides_stale_herdr_idle(self):
        world = _RetireWorld(harness="opencode", status="idle")
        with mock.patch.object(peer_steward.herdr_observation, "read_native_status",
                               return_value={"status": "working"}):
            rc, line = self.retire(world)
        self.assertEqual(rc, 0, line)
        self.assertIn("reason=agent-working-opencode-session-status", line)
        self.assertIn("pending=true", line)
        self.assertEqual(world.actions(), [])

    def test_normal_retire_requests_the_exact_watch_terminal_receipt(self):
        root = peer_steward._watch_root()
        root.mkdir(parents=True, exist_ok=True)
        watch_id = "retire-watch"
        arm = {"schema_version": 1, "watch_id": watch_id, "target": "old",
               "server": "default", "armed_ts": "then", "agent": {
                   "harness": "opencode", "session_id": "old-sid", "name": "old",
                   "pane": "w1:pOld"}, "foreground_identity": {"pid": 4242, "start": "800"},
               "steward": {}, "watcher": {}, "refs": []}
        paths = peer_steward._watch_paths(watch_id, root)
        paths.arm.write_text(json.dumps(arm), encoding="utf-8")
        lock_fd = peer_steward._open_lock(paths.lock)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            with mock.patch.object(peer_steward.herdr_observation, "read_native_status",
                                   return_value={"status": "idle"}):
                rc, line = self.retire(_RetireWorld(harness="opencode", status="working"))
            self.assertEqual(rc, 0, line)
            self.assertEqual(json.loads(paths.observation.read_text())["state"], "terminal_pending")
            self.assertFalse(paths.receipt.exists())
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        self.assertTrue(peer_steward._settle_terminal_watch_if_unobserved(
            watch_id, arm, root))
        receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
        self.assertEqual((receipt["state"], receipt["reason"]),
                         ("pane-exited", "completed-retire"))

    def test_current_runner_leaves_retire_untouched_while_legacy_runner_is_live(self):
        world = _RetireWorld(status="working")
        self.retire(world)
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.list()[0]
        world.status = "idle"
        with (store.root / "runner.lock").open("w") as legacy, \
             (store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME).open("w") as current:
            fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(current, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(peer_steward, "_resume_retire_obligation") as resume, \
                 mock.patch.object(peer_steward.time, "sleep", side_effect=InterruptedError("fixture")):
                with self.assertRaises(InterruptedError):
                    peer_steward.cmd_obligation_runner(
                        SimpleNamespace(state_root=self.tmp_root, lock_fd=current.fileno()))
                resume.assert_not_called()
        self.assertEqual(store.get(duty["id"]), duty)
        self.assertEqual(world.actions(), [])
        self._run_retire_runner(world)
        self.assertEqual(store.get(duty["id"])["state"], "complete")

    def _run_retire_runner(self, world, crash_phase=None, *, once=False):
        store = peer_steward.peer_obligations.ObligationStore()
        lock_fd = os.open(store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        original_stat = os.stat
        clock = iter(range(100))

        def stat(path, *args, **kwargs):
            if str(path) == "/proc/4242":
                if world.sent and world.exits:
                    raise FileNotFoundError(path)
                return object()
            return original_stat(path, *args, **kwargs)

        def run(argv, **kwargs):
            if crash_phase == "exit-requested" and world.sent and argv[:3] == ["herdr", "pane", "process-info"]:
                raise RuntimeError("fixture observation interrupted after exit send")
            if crash_phase == "shell-returned" and argv[:3] == ["herdr", "pane", "close"]:
                raise RuntimeError("fixture cleanup observation interrupted")
            return world.run(argv, **kwargs)

        try:
            with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
                 mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
                 patch_split(peer_steward, "_retire_process_record",
                                   return_value={"start": "800", "group": 4242, "argv": [world.harness]}), \
                 patch_split(peer_steward, "_proc_start_ticks",
                                   side_effect=lambda pid: "700" if pid == 101 else "800"), \
                 mock.patch.object(peer_steward.os, "stat", side_effect=stat), \
                 mock.patch.object(peer_steward, "_seat_handover", return_value=None), \
                 mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
                 mock.patch.object(peer_steward.time, "sleep",
                                   side_effect=InterruptedError("fixture runner stopped") if crash_phase else None), \
                 mock.patch("builtins.print"):
                if once:
                    peer_steward._reconcile_obligations(store)
                elif crash_phase:
                    with self.assertRaises(InterruptedError):
                        peer_steward.cmd_obligation_runner(
                            SimpleNamespace(state_root=self.tmp_root, lock_fd=lock_fd))
                else:
                    self.assertEqual(peer_steward.cmd_obligation_runner(
                        SimpleNamespace(state_root=self.tmp_root, lock_fd=lock_fd)), 0)
        finally:
            os.close(lock_fd)

    def test_observer_error_preserves_exit_phase_and_restart_only_finishes_cleanup(self):
        for phase in ("exit-requested", "shell-returned"):
            with self.subTest(phase=phase):
                world = _RetireWorld(status="working")
                rc, _line = self.retire(world)
                self.assertEqual(rc, 0)
                store = peer_steward.peer_obligations.ObligationStore()
                duty_id = store.list()[0]["id"]
                world.status = "idle"
                self._run_retire_runner(world, crash_phase=phase)
                interrupted = store.get(duty_id)
                self.assertEqual(interrupted["state"], "unknown")
                self.assertEqual(interrupted["observation"]["phase"], phase)
                self.assertEqual(interrupted["observation"]["foreground"]["pid"], 4242)
                self.assertEqual(interrupted["observation"]["reason"], "observer-unavailable:RuntimeError")
                self._run_retire_runner(world)
                settled = store.get(duty_id)
                self.assertEqual((settled["state"], settled["result"], settled["cleanup"]),
                                 ("complete", "normal-exit", "complete"))
                self.assertEqual(world.actions(), [
                    ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
                    ["herdr", "pane", "close", "w1:pOld"],
                ])

    def test_stale_runner_observation_cannot_undo_foreground_exit_claim_or_completion(self):
        original = peer_steward._pane_readiness
        for exits in (False, True):
            with self.subTest(exits=exits):
                world = _RetireWorld(status="working", exits=exits)
                self.retire(world)
                store = peer_steward.peer_obligations.ObligationStore()
                duty_id = store.list()[0]["id"]
                raced = []
                def readiness(*args, **kwargs):
                    stale = original(*args, **kwargs)
                    if not raced:
                        raced.append(True)
                        world.status = "idle"
                        with mock.patch.object(peer_steward, "_pane_readiness", original):
                            self.retire(world, clear_existing=False, resume_request_id=duty_id)
                    return stale
                with mock.patch.object(peer_steward, "_pane_readiness", side_effect=readiness):
                    if exits:
                        self._run_retire_runner(world)
                    else:
                        self._run_retire_runner(world, crash_phase="checkpoint")
                duty = store.get(duty_id)
                if exits:
                    self.assertEqual((duty["state"], duty["observation"]["phase"]), ("complete", "complete"))
                else:
                    self.assertEqual(duty["observation"]["phase"], "exit-requested")
                    self.assertEqual(duty["observation"]["foreground"]["pid"], 4242)
                    world.exits = True
                    self._run_retire_runner(world)
                    self.assertEqual(store.get(duty_id)["state"], "complete")
                self.assertEqual(world.actions(), [["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
                                                   ["herdr", "pane", "close", "w1:pOld"]])

    def test_finalizing_retire_is_accepted_then_resumed_once(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                world = _RetireWorld(harness=harness, status="finalizing")
                rc, line = self.retire(world)
                self.assertEqual(rc, 0)
                self.assertIn("reason=agent-finalizing", line)
                self.assertIn("pending=true", line)
                self.assertEqual(world.actions(), [])
                store = peer_steward.peer_obligations.ObligationStore()
                duty = store.list()[0]
                self.assertEqual(duty["state"], "pending")
                world.status = "idle"
                self._run_retire_runner(world)
                settled = store.get(duty["id"])
                self.assertEqual((settled["state"], settled["result"], settled["cleanup"]),
                                 ("complete", "normal-exit", "complete"))
                exits = ([
                    ["herdr", "pane", "send-text", "w1:pOld", "/exit"],
                    ["herdr", "pane", "send-keys", "w1:pOld", "enter"],
                ] if harness == "claude" else [
                    ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
                ])
                self.assertEqual(world.actions(), exits + [["herdr", "pane", "close", "w1:pOld"]])

    def test_idle_retire_with_bound_pending_or_unknown_work_sends_no_exit(self):
        from dispatch_contract import ObservedAttemptLiveness
        meta = ("attempt_id=att-bound,parent_sid=old-sid,"
                "parent_harness=codex")
        self.jobs_path.write_text(f"2026-10-09T00:00:00Z\topen\trepo\t-\tslug\t{meta}\n")
        for state, reason in (("live", "bound-registered-work-pending"),
                              ("unverifiable", "bound-registered-work-unknown")):
            with self.subTest(process_state=state), split_patch(
                    "dispatch_contract.observed_attempt_liveness",
                    return_value=ObservedAttemptLiveness(
                        "alive" if state == "live" else "unverifiable", "fixture", state, "fixture")):
                world = _RetireWorld(status="idle")
                rc, line = self.retire(world)
                self.assertEqual(rc, 0)
                self.assertIn("reason=" + reason, line)
                self.assertIn("pending=true", line)
                self.assertEqual(world.actions(), [])

    def test_retire_uses_bounded_agent_wait_and_one_final_process_observation(self):
        world = _RetireWorld(harness="opencode", status="idle", exits=False)
        with mock.patch.object(peer_steward, "_OPENCODE_RETIRE_SECONDS", 0.01):
            rc, line = self.retire(world)
        self.assertEqual(rc, 0, line)
        self.assertIn("reason=agent-still-running", line)
        sent_at = next(index for index, argv in enumerate(world.calls)
                       if argv[:3] == ["herdr", "pane", "send-keys"])
        post_send_reads = [argv for argv in world.calls[sent_at + 1:]
                           if argv[:3] == ["herdr", "pane", "process-info"]]
        self.assertEqual(len(post_send_reads), 1)
        wait_args = next(argv for argv in world.calls if argv[:3] == ["herdr", "agent", "wait"])
        wait_states = [wait_args[index + 1] for index, value in enumerate(wait_args[:-1]) if value == "--until"]
        self.assertEqual(wait_states, ["done"])

    def retire(self, world, record=None, *, clear_existing=True, resume_request_id=None):
        printed = []
        store = peer_steward.peer_obligations.ObligationStore()
        if clear_existing:
            # Every row here belongs to this test's isolated temporary root.
            for path in store.root.glob("retire-*.json"):
                path.unlink()
        original_stat = os.stat
        clock = iter(range(100))

        def stat(path, *a, **kw):
            if str(path) == "/proc/4242" and world.sent and world.exits:
                raise FileNotFoundError(path)
            if str(path) == "/proc/4242":
                return object()
            return original_stat(path, *a, **kw)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=world.run), \
             patch_split(peer_steward, "_retire_process_record", return_value=(record if record is not None else
                              {"start": "800", "group": 4242, "argv": [world.harness]})), \
             patch_split(peer_steward, "_proc_start_ticks", side_effect=lambda pid: "700" if pid == 101 else "800"), \
             mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner", return_value=False), \
             mock.patch.object(peer_steward.os, "stat", side_effect=stat), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(peer_steward.time, "sleep"), \
             mock.patch("builtins.print", side_effect=lambda *a, **kw: printed.append(" ".join(map(str, a)))):
            rc = (peer_steward.cmd_retire(SimpleNamespace(target="old", _resume_request_id=resume_request_id))
                  if resume_request_id else peer_steward.main(["retire", "old"]))
        return rc, printed[-1]

    def test_busy_retire_persists_then_task_runner_finishes_without_replaying_exit(self):
        world = _RetireWorld(status="working")
        rc, line = self.retire(world)
        self.assertEqual(rc, 0)
        self.assertIn("pending=true", line)
        self.assertEqual(world.actions(), [])
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.list()[0]
        self.assertEqual(duty["intent"]["kind"], "retire")
        self.assertEqual(duty["state"], "pending")

        world.status = "idle"
        lock_path = store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        original_stat = os.stat
        clock = iter(range(100))

        def stat(path, *args, **kwargs):
            if str(path) == "/proc/4242" and world.sent and world.exits:
                raise FileNotFoundError(path)
            if str(path) == "/proc/4242":
                return object()
            return original_stat(path, *args, **kwargs)

        try:
            with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
                 mock.patch.object(peer_steward.subprocess, "run", side_effect=world.run), \
                 patch_split(peer_steward, "_retire_process_record",
                                   return_value={"start": "800", "group": 4242,
                                                 "argv": [world.harness]}), \
                 patch_split(peer_steward, "_proc_start_ticks",
                                   side_effect=lambda pid: "700" if pid == 101 else "800"), \
                 mock.patch.object(peer_steward.os, "stat", side_effect=stat), \
                 mock.patch.object(peer_steward, "_seat_handover", return_value=None), \
                 mock.patch.object(peer_steward, "_request_watch_terminal", return_value=0), \
                 mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
                 mock.patch.object(peer_steward.time, "sleep"), \
                 mock.patch.object(peer_steward, "_runner_readiness_loop"), \
                 mock.patch("builtins.print"):
                self.assertEqual(peer_steward.cmd_obligation_runner(
                    SimpleNamespace(state_root=self.tmp_root, lock_fd=lock_fd)), 0)
        finally:
            os.close(lock_fd)

        settled = store.get(duty["id"])
        self.assertEqual((settled["state"], settled["result"], settled["cleanup"]),
                         ("complete", "normal-exit", "complete"))
        self.assertEqual(world.actions(), [
            ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
            ["herdr", "pane", "close", "w1:pOld"],
        ])
        before = list(world.actions())
        rc, line = self.retire(world, clear_existing=False, resume_request_id=duty["id"])
        self.assertEqual(rc, 0)
        self.assertIn("reason=already-complete", line)
        self.assertEqual(world.actions(), before)

    def test_pane_before_its_first_session_retires_by_its_pane(self):
        """2026-10-10 regression: a Codex that never took input has no session
        in herdr, and its retire stayed `pane-identity-unverified` for good."""
        world = _RetireWorld(sid=None)
        with mock.patch.object(peer_steward, "_proven_session", return_value=None):
            rc, line = self.retire(world)
        self.assertEqual((rc, line), (0, "retired=true reason=normal-exit agent=codex name=old pane=w1:pOld"))
        self.assertEqual(world.actions(), [
            ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
            ["herdr", "pane", "close", "w1:pOld"],
        ])

    def test_pending_sessionless_retire_never_exits_a_later_process(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                world = _RetireWorld(harness=harness, sid=None, status="working")
                with mock.patch.object(peer_steward, "_proven_session", return_value=None):
                    self.retire(world)
                    store = peer_steward.peer_obligations.ObligationStore()
                    duty = store.list()[0]
                    world.status = "idle"
                    rc, line = self.retire(world, clear_existing=False,
                        resume_request_id=duty["id"],
                        record={"start": "900", "group": 4242, "argv": [harness]})
                self.assertEqual(rc, 0)
                self.assertIn("reason=target-already-gone", line)
                self.assertEqual(world.actions(), [])
                settled = store.get(duty["id"])
                self.assertEqual((settled["state"], settled["result"], settled["cleanup"]),
                                 ("complete", "target-already-gone", "complete"))

    def test_runner_closes_gone_subject_even_when_pane_has_a_new_session(self):
        for reused in (False, True):
            with self.subTest(pid_reused=reused):
                world = _RetireWorld(sid=None, status="working")
                self.retire(world)
                store = peer_steward.peer_obligations.ObligationStore()
                duty = store.list()[0]
                original_stat = os.stat

                def stat(path, *args, **kwargs):
                    if str(path) == "/proc/4242":
                        raise FileNotFoundError(path)
                    return original_stat(path, *args, **kwargs)

                record = {"start": "900", "group": 4242, "argv": ["codex"]} if reused else None
                with patch_split(peer_steward, "_retire_process_record", return_value=record), \
                     mock.patch.object(peer_steward.os, "stat", side_effect=stat), \
                     mock.patch.object(peer_steward.subprocess, "run", side_effect=world.run), \
                     mock.patch.object(peer_steward, "_seat_handover") as handover:
                    world.sid, world.status = "new-sid", "idle"
                    peer_steward._resume_retire_obligation(duty, store)
                    peer_steward._resume_retire_obligation(store.get(duty["id"]), store)
                self.assertEqual(world.actions(), [])
                handover.assert_not_called()
                self.assertEqual(store.get(duty["id"])["result"], "target-already-gone")

    def test_unreadable_or_missing_original_birth_never_adopts_current_process(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                world = _RetireWorld(sid=None, status="working")
                self.retire(world)
                store = peer_steward.peer_obligations.ObligationStore()
                duty = store.list()[0]
                if legacy:
                    duty["intent"]["identity"].pop("foreground")
                original_stat = os.stat

                def stat(path, *args, **kwargs):
                    if str(path) == "/proc/4242":
                        raise PermissionError(path)
                    return original_stat(path, *args, **kwargs)

                with patch_split(peer_steward, "_retire_process_record", return_value=None), \
                     patch_split(peer_steward, "_proc_start_ticks", return_value=None), \
                     mock.patch.object(peer_steward.os, "stat", side_effect=stat), \
                     mock.patch.object(peer_steward.subprocess, "run", side_effect=world.run):
                    world.status = "idle"
                    peer_steward._resume_retire_obligation(duty, store)
                self.assertEqual(world.actions(), [])
                self.assertEqual(store.get(duty["id"])["state"], "unknown")

    def test_busy_subject_is_booked_with_children_without_authorizing_busy_exit(self):
        world = _RetireWorld(sid=None, status="working")
        original_info = world.info

        def info():
            value = original_info()
            if world.status == "working":
                value["foreground_processes"].append({"pid": 4243, "argv": ["sleep"]})
            return value

        world.info = info
        self.retire(world)
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.list()[0]
        self.assertEqual(duty["intent"]["identity"]["foreground"]["start"], "800")
        self.assertEqual(world.actions(), [])
        world.status = "idle"
        self.retire(world, clear_existing=False, resume_request_id=duty["id"])
        self.assertEqual(store.get(duty["id"])["result"], "normal-exit")

    def test_new_explicit_sessionless_retire_has_its_own_process_identity(self):
        for sid in (None, "same-resumed-sid"):
            with self.subTest(sid=sid):
                world = _RetireWorld(sid=sid, status="working")
                self.retire(world)
                store = peer_steward.peer_obligations.ObligationStore()
                first = store.list()[0]
                self.retire(world, clear_existing=False,
                            record={"start": "900", "group": 4242, "argv": ["codex"]})
                duties = store.list()
                self.assertEqual(len(duties), 2)
                self.assertEqual({row["intent"]["identity"]["foreground"]["start"] for row in duties}, {"800", "900"})
                self.assertEqual(store.get(first["id"])["intent"], first["intent"])
                self.assertEqual(world.actions(), [])

    def test_start_tick_proves_reuse_when_argv_is_unreadable(self):
        world = _RetireWorld(sid=None, status="working")
        self.retire(world)
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.list()[0]
        with patch_split(peer_steward, "_retire_process_record", return_value=None), \
             patch_split(peer_steward, "_proc_start_ticks", return_value="900"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=world.run):
            peer_steward._resume_retire_obligation(duty, store)
        self.assertEqual(world.actions(), [])
        self.assertEqual(store.get(duty["id"])["result"], "target-already-gone")

    def test_successor_after_exit_or_before_close_finishes_without_closing_its_pane(self):
        for phase in ("exit-requested", "shell-returned"):
            with self.subTest(phase=phase):
                world = _RetireWorld(sid=None, status="working")
                self.retire(world)
                store = peer_steward.peer_obligations.ObligationStore()
                duty = store.list()[0]
                store.claim_phase(duty["id"], {"waiting"}, phase,
                                  extra={"foreground": duty["intent"]["identity"]["foreground"]})
                world.sid, world.status = "new-sid", "idle"
                with mock.patch.object(peer_steward.subprocess, "run", side_effect=world.run), \
                     patch_split(peer_steward, "_retire_process_record",
                                       return_value={"start": "900", "group": 4242, "argv": ["codex"]}), \
                     patch_split(peer_steward, "_proc_start_ticks",
                                       side_effect=lambda pid: "700" if pid == 101 else "900"), \
                     mock.patch.object(peer_steward, "_seat_handover") as handover:
                    peer_steward._resume_retire_obligation(store.get(duty["id"]), store)
                self.assertEqual(world.actions(), [])
                handover.assert_not_called()
                self.assertEqual(store.get(duty["id"])["result"], "target-already-gone")

    def test_pane_whose_process_proves_a_session_herdr_lacks_retires_by_that_session(self):
        """2026-10-10 recurrence: Codex 0.162 opens its thread before any input,
        so the process proves a session herdr was not told yet. That session is
        the pane's own: retire proceeds, and work bound to it still holds it."""
        from dispatch_contract import ObservedAttemptLiveness
        meta = "attempt_id=att-bound,parent_sid=thread-real,parent_harness=codex"
        self.jobs_path.write_text(f"2026-10-09T00:00:00Z\topen\trepo\t-\tslug\t{meta}\n")
        world = _RetireWorld(sid=None)
        with mock.patch.object(peer_steward, "_proven_session", return_value="thread-real"), \
             split_patch("dispatch_contract.observed_attempt_liveness",
                        return_value=ObservedAttemptLiveness("alive", "fixture", "live", "fixture")):
            rc, line = self.retire(world)
        self.assertIn("retired=false reason=bound-registered-work-pending", line)
        self.assertEqual(world.actions(), [])
        self.jobs_path.write_text("")
        with mock.patch.object(peer_steward, "_proven_session", return_value="thread-real"):
            rc, line = self.retire(world, clear_existing=False)
        self.assertEqual((rc, line), (0, "retired=true reason=normal-exit agent=codex name=old pane=w1:pOld"))
        self.assertEqual(world.actions(), [
            ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
            ["herdr", "pane", "close", "w1:pOld"],
        ])

    def test_repeated_retire_retries_its_waiting_duty_now(self):
        world = _RetireWorld(status="working")
        rc, line = self.retire(world)
        self.assertEqual(rc, 0)
        self.assertIn("retired=false reason=agent-working", line)
        world.status = "idle"
        rc, line = self.retire(world, clear_existing=False)
        self.assertEqual((rc, line), (0, "retired=true reason=normal-exit agent=codex name=old pane=w1:pOld"))
        self.assertEqual(len(peer_steward.peer_obligations.ObligationStore().list()), 0)
        self.assertEqual(world.actions(), [
            ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
            ["herdr", "pane", "close", "w1:pOld"],
        ])

    def test_cancelled_retire_accepts_one_fresh_request_and_preserves_history(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                world = _RetireWorld(harness=harness, status="working")
                self.retire(world)
                store = peer_steward.peer_obligations.ObligationStore()
                first = store.list()[0]
                cancelled = store.update(first["id"], state="cancelled", cleanup="complete",
                                         observation={"phase": "cancelled", "reason": "self-target"})
                with mock.patch.object(peer_steward, "_current_session_identity", return_value=("new-requester", "codex")):
                    rc, line = self.retire(world, clear_existing=False)
                    self.assertEqual(rc, 0)
                    self.assertIn("reason=agent-working", line)
                    fresh = store.list()[0]
                    self.assertNotEqual(fresh["id"], first["id"])
                    self.assertEqual(fresh["intent"]["retry_of"], first["id"])
                    self.assertEqual(fresh["intent"]["requester"]["session_id"], "new-requester")
                    self.retire(world, clear_existing=False)
                    self.assertEqual([d["id"] for d in store.list()], [fresh["id"]])
                    world.status = "idle"
                    rc, line = self.retire(world, clear_existing=False)
                self.assertEqual(rc, 0)
                self.assertIn("retired=true reason=normal-exit", line)
                self.assertEqual(store.get(first["id"]), cancelled)
                self.assertEqual(store.get(fresh["id"])["result"], "normal-exit")
                self.assertEqual(sum(a[:3] == ["herdr", "pane", "close"] for a in world.actions()), 1)

    def test_runner_in_target_environment_uses_recorded_requester(self):
        for harness in ("claude", "codex", "opencode"):
            for legacy in (False, True):
                with self.subTest(harness=harness, legacy=legacy):
                    world = _RetireWorld(harness=harness, status="working")
                    with mock.patch.object(peer_steward, "_current_session_identity", return_value=("requester-sid", "claude")), \
                         mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pCaller"}):
                        self.retire(world)
                    store = peer_steward.peer_obligations.ObligationStore()
                    duty = store.list()[0]
                    if legacy:
                        duty["intent"]["requester"].pop("pane", None)
                        duty["intent"]["requester"].pop("server", None)
                        peer_steward.peer_obligations._write_atomic(store._record_path(duty["id"]), duty)
                    world.status = "idle"
                    with mock.patch.object(peer_steward, "_current_session_identity", return_value=(world.sid, harness)), \
                         mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pOld"}):
                        self._run_retire_runner(world)
                    settled = store.get(duty["id"])
                    self.assertEqual((settled["state"], settled["result"]), ("complete", "normal-exit"))
                    self.assertEqual(sum(a[:3] == ["herdr", "pane", "close"] for a in world.actions()), 1)

    def test_resumed_self_target_is_refused_despite_different_runner(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                world = _RetireWorld(harness=harness, status="working")
                self.retire(world)
                store = peer_steward.peer_obligations.ObligationStore()
                duty = store.list()[0]
                duty["intent"]["requester"] = {"harness": harness, "session_id": world.sid}
                peer_steward.peer_obligations._write_atomic(store._record_path(duty["id"]), duty)
                world.status = "idle"
                with mock.patch.object(peer_steward, "_current_session_identity", return_value=("runner-sid", "claude")), \
                     mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pOther"}):
                    self._run_retire_runner(world)
                self.assertEqual(store.get(duty["id"])["state"], "cancelled")
                self.assertEqual(world.actions(), [])

    def test_cancelled_resume_stays_cancelled_without_creating_new_request(self):
        world = _RetireWorld(status="working")
        self.retire(world)
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.list()[0]
        cancelled = store.update(duty["id"], state="cancelled", cleanup="complete",
                                 observation={"phase": "cancelled", "reason": "self-target"})
        world.status = "idle"
        rc, line = self.retire(world, clear_existing=False, resume_request_id=duty["id"])
        self.assertEqual(rc, 1)
        self.assertIn("reason=retire-cancelled", line)
        self.assertEqual(store.get(duty["id"]), cancelled)
        self.assertEqual(world.actions(), [])

    def test_confirmed_normal_exit_closes_once_and_records_notice(self):
        for harness, status in (("codex", "idle"), ("opencode", "done"), ("claude", "idle")):
            with self.subTest(harness=harness):
                world = _RetireWorld(harness=harness, status=status)
                rc, line = self.retire(world)
                self.assertEqual((rc, line), (0, f"retired=true reason=normal-exit agent={harness} name=old pane=w1:pOld"))
                expected = ([["herdr", "pane", "send-text", "w1:pOld", "/exit"],
                             ["herdr", "pane", "send-keys", "w1:pOld", "enter"]] if harness == "claude"
                            else [["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"]])
                self.assertEqual(world.actions(), expected + [["herdr", "pane", "close", "w1:pOld"]])
                row = self._all_records()[-1]
                self.assertEqual((row["kind"], row["to"]["pane"], row["delivery"]["receipt"]),
                                 ("notice", "w1:pOld", "normal-exit"))

    def test_a_retire_from_the_pane_started_beside_hands_the_routes_on(self):
        # RA-2 (decision 5f0f10): beside -> ACK -> retire moves the parent role, across harnesses.
        handed_line = "retired=true reason=normal-exit agent=codex name=old pane=w1:pOld"
        def run(mark_beside, mark_sid):
            peer_steward._mark_seat_successor("w1:pNew", mark_beside, "claude", mark_sid)
            with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pNew"}), \
                 mock.patch.object(peer_steward, "_current_session_identity", return_value=("new-sid", "claude")), \
                 mock.patch("dispatch_seat_handover.record_retire_handover",
                            return_value={"bindings": [{}, {}]}) as handed:
                rc, line = self.retire(_RetireWorld(harness="codex"))
            return rc, line, handed
        rc, line, handed = run("w1:pOld", "new-sid")
        self.assertEqual((rc, line), (0, handed_line + " handover=2"))
        handed.assert_called_once_with("old-sid", "codex", "new-sid", "claude", env=mock.ANY,
                                      successor_seat=mock.ANY)
        self.assertEqual(handed.call_args.kwargs["successor_seat"].pane, "w1:pNew")
        self.assertFalse(peer_steward._seat_successor_path("w1:pNew").exists())      # used once
        rc, line, handed = run("w1:pElse", "new-sid")                               # not beside this one
        self.assertEqual((rc, line), (0, handed_line))
        handed.assert_not_called()
        rc, line, handed = run("w1:pOld", "someone-else")                           # another session's pane
        self.assertEqual((rc, line), (0, handed_line + " handover=skipped:successor-unverified"))
        handed.assert_not_called()

    def test_prompt_git_helpers_after_exit_are_not_live_agents(self):
        for harness in ("codex", "claude", "opencode"):
            with self.subTest(harness=harness):
                world = _RetireWorld(harness=harness)
                normal_info = world.info

                def prompt_info():
                    value = normal_info()
                    if world.sent:
                        value["foreground_processes"] += [
                            {"pid": 102, "argv": ["zsh"]},
                            {"pid": 103, "argv": ["git", "status", "--porcelain",
                                                  "--ignore-submodules=dirty"]},
                        ]
                    return value

                world.info = prompt_info
                rc, line = self.retire(world)
                self.assertEqual(rc, 0)
                self.assertIn("retired=true reason=normal-exit", line)
                self.assertEqual(world.actions()[-1], ["herdr", "pane", "close", "w1:pOld"])

    def test_only_opencode_waits_for_a_slow_normal_exit(self):
        for harness in ("opencode", "codex", "claude"):
            with self.subTest(harness=harness):
                world = _RetireWorld(harness=harness, exits=False)
                if harness == "opencode":
                    transports = []

                    class EventTransport:
                        def __init__(self, *_args, **_kwargs):
                            self._state = SimpleNamespace(complete=True)
                            self.snapshot_count = 0
                            transports.append(self)

                        def observe(self):
                            return SimpleNamespace(endpoint="default", complete=True, panes=())

                        def resnapshot(self):
                            self.snapshot_count += 1
                            return SimpleNamespace(endpoint="default", complete=True, panes=())

                        def wait_for_event(self, pane_id, _timeout, event_types=None):
                            self.assert_target = pane_id
                            self.assert_types = event_types
                            world.exits = True
                            return {"event": "pane.closed", "data": {"pane_id": pane_id}}

                        def close(self):
                            pass

                    with mock.patch.object(peer_steward.herdr_observation,
                                           "socket_path_for_endpoint", return_value="/fixture.sock"), \
                         mock.patch.object(peer_steward.herdr_observation,
                                           "ObservationTransport", EventTransport):
                        rc, line = self.retire(world)
                else:
                    rc, line = self.retire(world)
                if harness == "opencode":
                    self.assertEqual(rc, 0)
                    self.assertIn("retired=true reason=normal-exit", line)
                    self.assertEqual(transports[0].snapshot_count, 1)
                    self.assertEqual(world.actions(), [
                        ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
                        ["herdr", "pane", "close", "w1:pOld"],
                    ])
                else:
                    self.assertEqual(rc, 0)
                    self.assertIn("retired=false reason=agent-still-running", line)
                    expected = ([
                        ["herdr", "pane", "send-text", "w1:pOld", "/exit"],
                        ["herdr", "pane", "send-keys", "w1:pOld", "enter"],
                    ] if harness == "claude" else [
                        ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
                    ])
                    self.assertEqual(world.actions(), expected)

    def test_opencode_still_running_is_bounded_without_another_exit_key(self):
        world = _RetireWorld(harness="opencode", exits=False)
        rc, line = self.retire(world)
        self.assertEqual(rc, 0)
        self.assertIn("retired=false reason=agent-still-running", line)
        self.assertEqual(world.actions(), [
            ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
        ])

    def test_busy_form_draft_unknown_self_or_changed_target_receives_no_exit(self):
        cases = [(_RetireWorld(status=s), "agent-" + s) for s in ("working", "blocked", "unknown")]
        cases += [(_RetireWorld(screen=s), reason) for s, reason in
                  ((CODEX_DRAFT, "draft"), (CODEX_POPUP, "draft-unknown"), (NO_BOX, "draft-unknown"),
                   (CLAUDE_FORM, "form-open"), ("", "screen-unavailable"))]
        cases += [(_RetireWorld("claude", screen=CLAUDE_DRAFT_SECOND_LINE), "draft"),
                  (_RetireWorld("opencode", screen=OPENCODE_DRAFT), "draft")]
        changed = _RetireWorld(); changed.changed = True
        cases.append((changed, "pane-identity-unverified"))
        for world, reason in cases:
            with self.subTest(reason=reason):
                rc, line = self.retire(world)
                self.assertEqual(rc, 0)
                self.assertIn("reason=" + reason, line)
                self.assertEqual(world.actions(), [])
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pOld"}):
            world = _RetireWorld(); rc, line = self.retire(world)
            self.assertIn("reason=self-target", line); self.assertEqual(world.actions(), [])

    def test_unverified_or_foreign_foreground_receives_no_exit(self):
        for patch in ({"pane_id": "w1:pForeign"}, {"shell_pid": True},
                      {"foreground_process_group_id": 999}, {"foreground_processes": []},
                      {"foreground_processes": [{"pid": 4242, "argv": ["other"]}]}):
            with self.subTest(patch=patch):
                world = _RetireWorld(); world.bad_process = patch
                rc, line = self.retire(world)
                self.assertIn("reason=", line); self.assertEqual(world.actions(), [])
        world = _RetireWorld()
        rc, line = self.retire(world, {"start": "800", "group": 999, "argv": ["codex"]})
        self.assertIn("reason=", line); self.assertEqual(world.actions(), [])

    def test_claude_exit_command_unknown_or_still_live_never_closes_or_retries(self):
        # One /exit submission; a missing shell return never permits a retry or close.
        for unknown in (False, True):
            world = _RetireWorld("claude", exits=False); world.post_unknown = unknown
            rc, line = self.retire(world)
            self.assertEqual(rc, 0)
            self.assertIn("reason=" + ("shell-return-unverified" if unknown else "agent-still-running"), line)
            self.assertEqual(world.actions(), [["herdr", "pane", "send-text", "w1:pOld", "/exit"],
                                               ["herdr", "pane", "send-keys", "w1:pOld", "enter"]])

    def test_error_body_on_send_or_close_is_not_success(self):
        world = _RetireWorld(); world.send_error = True
        rc, line = self.retire(world)
        self.assertIn("reason=exit-send-unknown", line)
        self.assertEqual(len(world.actions()), 1)
        world = _RetireWorld(); world.close_error = True
        rc, line = self.retire(world)
        self.assertIn("retired=false reason=pane-close-failed", line)
        self.assertEqual(len(world.actions()), 2)

    def test_claude_failed_exit_text_or_changed_foreground_never_submits_enter(self):
        world = _RetireWorld("claude")
        normal_run = world.run

        def failed_text(argv, **kw):
            if argv[:3] == ["herdr", "pane", "send-text"]:
                world.calls.append(list(argv))
                return _herdr_json({"error": {"code": "denied"}})
            return normal_run(argv, **kw)

        world.run = failed_text
        rc, line = self.retire(world)
        self.assertEqual(rc, 0); self.assertIn("reason=exit-send-unknown", line)
        self.assertEqual(world.actions(), [["herdr", "pane", "send-text", "w1:pOld", "/exit"]])
        world = _RetireWorld("claude")
        normal_info = world.info

        def changed_after_text():
            value = normal_info()
            if any(a[:3] == ["herdr", "pane", "send-text"] for a in world.calls):
                value["foreground_process_group_id"] = 999
            return value

        world.info = changed_after_text
        rc, line = self.retire(world)
        self.assertEqual(rc, 0); self.assertIn("reason=foreground-changed", line)
        self.assertEqual(world.actions(), [["herdr", "pane", "send-text", "w1:pOld", "/exit"]])

    def test_changed_shell_after_exit_is_not_closed(self):
        world = _RetireWorld()
        original_info = world.info

        def changed_after_exit():
            value = original_info()
            if world.sent:
                value["shell_pid"] = 999
            return value

        world.info = changed_after_exit
        rc, line = self.retire(world)
        self.assertIn("retired=false reason=agent-still-running", line)
        self.assertEqual(world.actions(), [["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"]])

    def test_protocol_error_or_missing_herdr_is_typed_and_never_exits(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward, "_run_herdr_get", return_value={"result": {"agent": {
                 "agent": "codex", "agent_status": "idle", "agent_session": "bad"}}}), \
             mock.patch.object(peer_steward.subprocess, "run") as run, mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["retire", "old"]), 1)
            self.assertIn("retired=false reason=herdr-protocol-error", printed.call_args[0][0])
            run.assert_not_called()
        with mock.patch.object(peer_steward.shutil, "which", return_value=None), \
             mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["retire", "old"]), 1)
            self.assertIn("retired=false reason=herdr-not-found", printed.call_args[0][0])

    def test_shell_return_requires_original_birth_and_absent_predecessor(self):
        identity = {"pid": 4242, "start": "800", "shell_pid": 101, "shell_start": "700"}
        info = {"shell_pid": 101, "foreground_process_group_id": 101, "foreground_processes": [{"pid": 101}]}
        with patch_split(peer_steward, "_proc_start_ticks", return_value="701"):
            self.assertFalse(peer_steward._retire_shell_returned(info, identity))
        for error in (None, PermissionError()):
            with patch_split(peer_steward, "_proc_start_ticks", side_effect=lambda pid: "700" if pid == 101 else "800"), \
                 mock.patch.object(peer_steward.os, "stat", return_value=object(), side_effect=error):
                self.assertFalse(peer_steward._retire_shell_returned(info, identity))
        with patch_split(peer_steward, "_proc_start_ticks", return_value="700"), \
             mock.patch.object(peer_steward.os, "stat", side_effect=FileNotFoundError()):
            self.assertTrue(peer_steward._retire_shell_returned(info, identity))

        for birth, expected in (("800", False), ("801", True), (None, False)):
            with self.subTest(birth=birth), \
                 patch_split(peer_steward, "_proc_start_ticks", side_effect=lambda pid: "700" if pid == 101 else birth), \
                 mock.patch.object(peer_steward.os, "stat", return_value=object()):
                self.assertEqual(peer_steward._retire_shell_returned(info, identity), expected)

    def test_kernel_record_refuses_unreadable_namespace_or_reused_birth(self):
        stat = "4242 (codex) " + " ".join(["S", "101", "4242"] + ["0"] * 16 + ["800"])
        for birth, namespaces, read_error in (("800", ["pid:[1]", "pid:[1]"], None),
                ("801", [], None), ("800", ["pid:[2]", "pid:[1]"], None),
                ("800", [], PermissionError())):
            with self.subTest(birth=birth, namespaces=namespaces, read_error=read_error), \
                 mock.patch.object(peer_steward.Path, "read_text", return_value=stat, side_effect=read_error), \
                 mock.patch.object(peer_steward.Path, "read_bytes", return_value=b"codex\0"), \
                 patch_split(peer_steward, "_proc_start_ticks", return_value=birth), \
                 mock.patch.object(peer_steward.os, "readlink", side_effect=namespaces):
                result = peer_steward._retire_process_record(4242)
                if birth == "800" and namespaces == ["pid:[1]", "pid:[1]"]:
                    self.assertEqual(result, {"start": "800", "group": 4242, "argv": ["codex"]})
                else:
                    self.assertIsNone(result)


class DeferredRetireHandoverTest(_TmpRootMixin, unittest.TestCase):
    """The recorded successor inherits; observers and later pane occupants do not."""

    def test_waiting_normal_retire_cuts_over_before_busy_or_unknown_without_exit(self):
        for predecessor, successor in (("claude", "codex"), ("claude", "opencode"),
                                      ("codex", "claude"), ("codex", "opencode"),
                                      ("opencode", "claude"), ("opencode", "codex")):
            for subject, readiness_state in (("same", "pending"), ("same", "unknown"),
                                             ("unknown", "unknown")):
                with self.subTest(predecessor=predecessor, successor=successor,
                                  subject=subject, readiness=readiness_state), \
                     tempfile.TemporaryDirectory() as root, \
                     mock.patch.dict(os.environ, {"XDG_STATE_HOME": root,
                                                  "AGENT_PEER_LEDGER_ROOT": root}):
                    store, duty = self.booking(predecessor, successor)
                    original_intent = json.loads(json.dumps(duty["intent"]))
                    candidate = {"pane": "w1:pNew", "harness": successor,
                                 "session_id": "new-sid"}
                    old = dict(duty["intent"]["identity"])
                    with patch_split(peer_steward, "_retire_subject_state", return_value=subject), \
                         mock.patch.object(peer_steward, "_prompt_current_candidate",
                                           return_value=(candidate, None)), \
                         mock.patch.object(peer_steward, "_retire_target",
                                           return_value=("working", old, 0, None)), \
                         mock.patch.object(peer_steward, "_pane_readiness", return_value=SimpleNamespace(
                             state=readiness_state, reason="bound-registered-work-" + readiness_state)) as ready, \
                         mock.patch.object(peer_steward.subprocess, "run") as native:
                        peer_steward._resume_retire_obligation(duty, store)
                        self.assert_authority()
                        self.assertEqual(self.handover.effective_parent_harness(
                            self.meta, self.jobs_path), successor)
                        native.assert_not_called()
                        if subject == "same":
                            self.assertEqual(ready.call_args.kwargs["purpose"], "input")
                        # The same accepted mark survives a busy pass; replay
                        # still proves the recipient without another request.
                        self.assertTrue(peer_steward._seat_successor_path("w1:pNew").exists())
                        peer_steward._resume_retire_obligation(store.get(duty["id"]), store)
                        self.assert_authority()
                        native.assert_not_called()
                    self.assertEqual(store.get(duty["id"])["intent"], original_intent)
                    self.assertNotEqual(store.get(duty["id"])["state"], "complete")

    def test_waiting_normal_retire_does_not_trust_an_unproven_successor(self):
        for changed in ("missing", "late", "replaced", "unverified"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as root, \
                 mock.patch.dict(os.environ, {"XDG_STATE_HOME": root,
                                              "AGENT_PEER_LEDGER_ROOT": root}):
                store, duty = self.booking()
                path = peer_steward._seat_successor_path("w1:pNew")
                if changed == "missing":
                    path.unlink()
                elif changed in {"late", "replaced"}:
                    mark = json.loads(path.read_text())
                    if changed == "late":
                        mark["at"] = duty["accepted_at"] + 1
                    else:
                        mark["successor"]["session_id"] = "other-sid"
                    path.write_text(json.dumps(mark))
                candidate = {"pane": "w1:pNew", "harness": "opencode", "session_id": "new-sid"}
                with patch_split(peer_steward, "_retire_subject_state", return_value="unknown"), \
                     mock.patch.object(peer_steward, "_prompt_current_candidate", return_value=(
                         None if changed == "unverified" else candidate,
                         "target-identity-unverified" if changed == "unverified" else None)), \
                     mock.patch.object(peer_steward.subprocess, "run") as native:
                    peer_steward._resume_retire_obligation(duty, store)
                    native.assert_not_called()
                self.assert_authority("old-sid")

    def test_normal_retire_keeps_past_notification_states_and_protects_actual_executors(self):
        from dispatch_attempt_policy import AttemptDecision, RegisteredWorkObservation
        history = tuple(RegisteredWorkObservation(
            attempt_id="past-" + str(i),
            decision=AttemptDecision("succeeded", "advance", "owner", "completed"),
            process_state="quiescent", evidence_state="terminal",
            pane_relation="parent", delivery_state="unknown",
            provenance=(("pending_state", state), ("membership", member)))
            for i, (state, member) in enumerate((("rejected", "exact"),
                ("acked", "replacement-one"), ("acked", "replacement-two"))))
        for executor in (None, "live", "unknown"):
            with self.subTest(executor=executor), tempfile.TemporaryDirectory() as root, \
                 mock.patch.dict(os.environ, {"XDG_STATE_HOME": root,
                                              "AGENT_PEER_LEDGER_ROOT": root}):
                store, duty = self.booking("claude", "codex")
                # A complete kernel identity, as the ordinary retire booking
                # records it; only native observations are fixtures here.
                duty["intent"]["identity"]["foreground"].update(group=4242, argv=["claude"])
                peer_steward.peer_obligations._write_atomic(store._record_path(duty["id"]), duty)
                world = _RetireWorld(harness="claude")
                bound = history
                if executor:
                    bound += (RegisteredWorkObservation(
                        attempt_id="actual-executor", decision=AttemptDecision(
                            "running" if executor == "live" else "unknown", "wait", "owner", "active"),
                        process_state=executor, evidence_state="unknown", pane_relation="executor"),)
                candidate = {"pane": "w1:pNew", "harness": "codex", "session_id": "new-sid"}
                pending = []
                for i, item in enumerate(history):
                    path = Path(root) / ("past-delivery-" + str(i) + ".json")
                    path.write_text(json.dumps(dict(item.provenance)))
                    pending.append((path, path.read_bytes()))
                with mock.patch.object(peer_steward, "_prompt_current_candidate",
                                       return_value=(candidate, None)), \
                     mock.patch.object(peer_steward.peer_obligations, "bound_work_for_pane",
                                       return_value=(bound, "observed")) as observed:
                    rc, line = RetireTest.retire(self, world, clear_existing=False,
                                                resume_request_id=duty["id"])
                self.assertEqual(rc, 0, line)
                self.assert_authority()
                self.assertTrue(any(call.kwargs.get("include_parent") is False
                                    for call in observed.call_args_list))
                if executor:
                    self.assertEqual(world.actions(), [], line)
                    self.assertNotEqual(store.get(duty["id"])["state"], "complete")
                else:
                    self.assertIn("retired=true", line)
                    self.assertEqual(store.get(duty["id"])["state"], "complete")
                for path, original in pending:
                    self.assertEqual(path.read_bytes(), original)

    def booking(self, predecessor="claude", successor="opencode"):
        import dispatch_seat_handover as handover
        self.handover = handover
        self.meta = {"attempt_id": "att-original", "dispatch_depth": "1", "worker_type": "owner",
                     "parent_sid": "old-sid", "parent_harness": predecessor,
                     "route_id": "rt-original", "route_hash": "sha256:original", "route_node": "owner"}
        unrelated = {**self.meta, "attempt_id": "att-unrelated", "parent_sid": "other-parent",
                     "route_id": "rt-unrelated", "route_hash": "sha256:unrelated"}
        self.jobs_path.write_text("".join("now\trunning\t0\tjob\tlog\t" +
            ",".join(f"{key}={value}" for key, value in row.items()) + "\n"
            for row in (self.meta, unrelated)))
        self.registry_bytes = self.jobs_path.read_bytes()
        self.unrelated = unrelated
        peer_steward._mark_seat_successor("w1:pNew", "w1:pOld", successor, "")
        requester = {"session_id": "new-sid", "harness": successor,
                     "pane": "w1:pNew", "server": "default"}
        foreground = {"pid": 4242, "start": "800", "shell_pid": 101, "shell_start": "700"}
        identity = {"session_id": "old-sid", "harness": predecessor,
                    "pane": "w1:pOld", "foreground": foreground, "server": "default", "name": "old"}
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.create("retire-fixture", "retire", identity,
                            {"target": "old", "requester": requester})
        return store, duty

    def assert_authority(self, successor="new-sid"):
        self.assertEqual(self.handover.effective_parent(self.meta, self.jobs_path), successor)
        self.assertEqual(self.handover.effective_parent(self.unrelated, self.jobs_path), "other-parent")
        self.assertEqual(self.jobs_path.read_bytes(), self.registry_bytes)

    def test_gone_predecessor_hands_over_from_an_observer_on_every_harness(self):
        import session_tidy
        import pane_ownership
        for predecessor, successor in (("claude", "codex"), ("claude", "opencode"),
                                      ("codex", "claude"), ("codex", "opencode"),
                                      ("opencode", "claude"), ("opencode", "codex")):
            with self.subTest(predecessor=predecessor, successor=successor):
                # Each chain has its own isolated state, as independent seats do.
                with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {
                        "XDG_STATE_HOME": root}):
                    store, duty = self.booking(predecessor, successor)
                    with mock.patch.object(peer_steward, "_caller_pane", return_value="w9:pObserver"), \
                         patch_split(peer_steward, "_retire_subject_state", return_value="gone"), \
                         mock.patch.object(pane_ownership, "verified_pane", return_value=""), \
                         mock.patch.object(peer_steward.subprocess, "run") as native:
                        peer_steward._resume_retire_obligation(duty, store)
                        native.assert_not_called()
                    self.assert_authority()
                    settled = store.get(duty["id"])
                    self.assertEqual((settled["state"], settled["result"]), ("complete", "target-already-gone"))
                    seat = session_tidy._pane_seat_of("w1:pNew")
                    before = session_tidy._ledger_path(seat).read_bytes()
                    peer_steward._resume_retire_obligation(settled, store)
                    self.assertEqual(session_tidy._ledger_path(seat).read_bytes(), before)
                    # Keep the next case's immutable duty identity separate.
                    store._record_path(duty["id"]).unlink()

    def test_missing_pane_finishes_each_exit_phase_without_input_or_close(self):
        for phase in ("exit-requested", "shell-returned"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root, \
                 mock.patch.dict(os.environ, {"XDG_STATE_HOME": root}):
                store, duty = self.booking()
                duty = store.claim_phase(duty["id"], {"waiting"}, phase,
                                         extra={"foreground": duty["intent"]["identity"]["foreground"]})
                with mock.patch.object(peer_steward, "_retire_pane_info", return_value=None), \
                     patch_split(peer_steward, "_retire_subject_state", return_value="gone"), \
                     mock.patch.object(peer_steward.subprocess, "run") as native:
                    peer_steward._resume_retire_obligation(duty, store)
                    native.assert_not_called()
                self.assert_authority()
                self.assertEqual(store.get(duty["id"])["state"], "complete")
                store._record_path(duty["id"]).unlink()

    def test_normal_exit_cleanup_uses_the_booked_successor_pane(self):
        store, duty = self.booking()
        foreground = duty["intent"]["identity"]["foreground"]
        info = {"pane_id": "w1:pOld", "shell_pid": 101, "foreground_process_group_id": 101}
        with mock.patch.object(peer_steward, "_caller_pane", return_value="w9:pObserver"), \
             mock.patch.object(peer_steward, "_retire_pane_info", return_value=info), \
             mock.patch.object(peer_steward, "_retire_shell_returned", return_value=True), \
             patch_split(peer_steward, "_proc_start_ticks", return_value="700"), \
             mock.patch.object(peer_steward, "_close_pane", return_value=True) as close:
            peer_steward._finish_retire_cleanup(store, duty, duty["intent"]["identity"], foreground)
        close.assert_called_once_with("w1:pOld")
        self.assert_authority()

    def test_completed_legacy_retire_repairs_via_the_same_command(self):
        store, duty = self.booking()
        completed = store.update(duty["id"], state="complete", result="normal-exit",
                                 observation={"phase": "complete"})
        with mock.patch.object(peer_steward, "_retire_target", return_value=("unknown", {
                "harness": "-", "name": "old", "pane": "-", "session_id": "-"}, 1, "pane-unverified")), \
             mock.patch.object(peer_steward, "_current_session_identity", return_value=("new-sid", "opencode")), \
             mock.patch.object(peer_steward, "_caller_pane", return_value="w1:pNew"), \
             patch_split(peer_steward, "_herdr_missing", return_value=False), \
             mock.patch.object(peer_steward.subprocess, "run") as native:
            self.assertEqual(peer_steward.cmd_retire(SimpleNamespace(target="old")), 0)
            native.assert_not_called()
        self.assert_authority()
        self.assertEqual(store.get(duty["id"]), completed)

    def _blank_requester(self, store, duty):
        changed = json.loads(json.dumps(duty))
        changed["intent"]["requester"]["pane"] = ""
        peer_steward.peer_obligations._write_atomic(store._record_path(duty["id"]), changed)
        return store.get(duty["id"])

    def _verified_requester_observation(self, pane, *, expected_harness=None,
                                        expected_sid=None, target_observation=None,
                                        pane_info=None, **_kwargs):
        state, ident, _code, reason = target_observation
        if (reason or ident.get("pane") != pane
                or ident.get("harness") != expected_harness
                or ident.get("session_id") != expected_sid):
            return None, reason or "target-identity-unverified"
        info = pane_info or {"pane_id": pane, "foreground_processes": [{"pid": 4242}]}
        return {"state": state, "harness": expected_harness, "session_id": expected_sid,
                "pane": pane, "_identity": dict(ident, server="default"),
                "_pane_info": info, "_native_session_id": expected_sid}, None

    def _legacy_identity_patches(self):
        ident = {"server": "default", "pane": "w1:pNew", "harness": "opencode",
                 "session_id": "new-sid", "name": "new"}
        info = {"pane_id": "w1:pNew", "foreground_processes": [{"pid": 4242}]}
        return (
            mock.patch.object(peer_steward, "_retire_target",
                              return_value=("idle", ident, 0, None)),
            mock.patch.object(peer_steward, "_retire_pane_info", return_value=info),
            mock.patch.object(peer_steward, "_prompt_current_candidate",
                              side_effect=self._verified_requester_observation),
        )

    def test_legacy_empty_pane_reconciles_and_keeps_original_intent(self):
        store, duty = self.booking()
        duty = self._blank_requester(store, duty)
        original_requester = json.loads(json.dumps(duty["intent"]["requester"]))
        with self._legacy_identity_patches()[0], self._legacy_identity_patches()[1], \
             self._legacy_identity_patches()[2]:
            self.assertEqual(peer_steward._retire_handover(duty), "1")
        self.assert_authority()
        current = store.get(duty["id"])
        self.assertEqual(current["intent"]["requester"], original_requester)
        self.assertEqual(current["id"], duty["id"])

    def test_two_installed_legacy_requests_reconcile_from_their_exact_preacceptance_marks(self):
        cases = (
            ("retire-407ed2a6eef9409ac778fe003ca67d19", "wB:p8S",
             "1000006c-0000-4000-8000-00000000007a",
             "01000054-0000-7000-8000-000000000062", "wB:p99"),
            ("retire-8a1db76365414b3c02603500d366c97f", "wB:p8Z",
             "1000006a-0000-4000-8000-000000000078",
             "01000054-0000-7000-8000-000000000061", "wB:p98"),
        )
        for duty_id, predecessor_pane, predecessor_sid, successor_sid, successor_pane in cases:
            with self.subTest(target=predecessor_pane), tempfile.TemporaryDirectory() as root, \
                 mock.patch.dict(os.environ, {"XDG_STATE_HOME": root}):
                meta = {"attempt_id": "att-" + duty_id.removeprefix("retire-"),
                    "dispatch_depth": "1", "worker_type": "owner",
                    "parent_sid": predecessor_sid, "parent_harness": "claude",
                    "route_id": "rt-" + duty_id.removeprefix("retire-"),
                    "route_hash": "sha256:" + duty_id.removeprefix("retire-"),
                    "route_node": "owner"}
                self.jobs_path.write_text("now\trunning\t0\tjob\tlog\t" +
                    ",".join(f"{key}={value}" for key, value in meta.items()) + "\n")
                original_jobs = self.jobs_path.read_bytes()
                identity = {"server": "default", "pane": predecessor_pane,
                    "harness": "claude", "session_id": predecessor_sid, "name": "predecessor",
                    "foreground": {"pid": 166000 + len(duty_id), "start": "165000"}}
                requester = {"session_id": successor_sid, "harness": "codex",
                    "pane": "", "server": "default"}
                peer_steward._mark_seat_successor(
                    successor_pane, predecessor_pane, "codex", successor_sid)
                store = peer_steward.peer_obligations.ObligationStore()
                duty = store.create(duty_id, "retire", identity,
                                    {"target": predecessor_pane, "requester": requester})
                observed = {"server": "default", "pane": successor_pane,
                    "harness": "codex", "session_id": successor_sid, "name": "successor"}
                pane_info = {"pane_id": successor_pane,
                    "foreground_processes": [{"pid": 171000 + len(duty_id)}]}
                def current(pane, *, expected_harness=None, expected_sid=None,
                            target_observation=None, pane_info=None, **_kwargs):
                    state, ident, _code, reason = target_observation
                    if (reason or ident.get("pane") != pane
                            or ident.get("harness") != expected_harness
                            or ident.get("session_id") != expected_sid):
                        return None, reason or "target-identity-unverified"
                    info = pane_info or {"pane_id": pane,
                        "foreground_processes": [{"pid": 171000 + len(duty_id)}]}
                    return {"state": state, "harness": expected_harness,
                        "session_id": expected_sid, "pane": pane,
                        "_identity": dict(ident, server="default"), "_pane_info": info,
                        "_native_session_id": expected_sid}, None
                with mock.patch.object(peer_steward, "_retire_target",
                                       return_value=("idle", observed, 0, None)), \
                     mock.patch.object(peer_steward, "_retire_pane_info", return_value=pane_info), \
                     mock.patch.object(peer_steward, "_prompt_current_candidate", side_effect=current):
                    self.assertEqual(peer_steward._retire_handover(duty), "1")
                import dispatch_seat_handover as handover
                self.assertEqual(handover.effective_parent(meta, self.jobs_path), successor_sid)
                self.assertEqual(self.jobs_path.read_bytes(), original_jobs)
                self.assertEqual(store.get(duty_id)["intent"]["requester"], requester)

    def test_legacy_completed_runner_reconciles_before_reusing_settled_followup(self):
        store, duty = self.booking()
        duty = self._blank_requester(store, duty)
        duty = store.update(duty["id"], state="complete", result="normal-exit",
            observation={"phase": "complete", "terminal_followup": "complete",
                         "terminal_followup_result": "none"})
        with self._legacy_identity_patches()[0], self._legacy_identity_patches()[1], \
             self._legacy_identity_patches()[2]:
            peer_steward._resume_retire_obligation(duty, store)
        self.assert_authority()
        self.assertEqual(store.get(duty["id"])["intent"]["requester"]["pane"], "")
        self.assertEqual(store.get(duty["id"])["observation"]["terminal_followup_result"], "1")

    def test_legacy_blank_pane_uses_the_existing_retire_target_command(self):
        store, duty = self.booking()
        duty = self._blank_requester(store, duty)
        store.update(duty["id"], state="complete", result="normal-exit",
                     observation={"phase": "complete", "terminal_followup": "pending"})
        for target in ("wB:p8S", "wB:p8Z", "wX:pA"):
            self.assertEqual(peer_steward.build_parser().parse_args(["retire", target]).target, target)
        parsed = peer_steward.build_parser().parse_args(["retire", "old"])
        def target(pane, *, server=None):
            if pane == "old":
                return "unknown", {"harness": "-", "name": "old", "pane": "-", "session_id": "-"}, 1, "pane-unverified"
            return "idle", {"server": "default", "pane": "w1:pNew", "harness": "opencode",
                            "session_id": "new-sid", "name": "new"}, 0, None
        with mock.patch.object(peer_steward, "_retire_target", side_effect=target), \
             mock.patch.object(peer_steward, "_current_session_identity", return_value=("new-sid", "opencode")), \
             mock.patch.object(peer_steward, "_caller_pane", return_value="w1:pNew"), \
             patch_split(peer_steward, "_herdr_missing", return_value=False), \
             self._legacy_identity_patches()[1], self._legacy_identity_patches()[2], \
             mock.patch.object(peer_steward, "_request_watch_terminal", return_value=0), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(parsed.target, "old")
            self.assertEqual(peer_steward.cmd_retire(parsed), 0)
        self.assert_authority()

    def test_legacy_blank_requester_reuses_id_only_for_its_proven_successor(self):
        store, duty = self.booking()
        duty = self._blank_requester(store, duty)
        with self._legacy_identity_patches()[0], self._legacy_identity_patches()[1], \
             self._legacy_identity_patches()[2]:
            self.assertEqual(peer_steward.reserve_handoff_retire(
                "old", duty["intent"]["identity"], duty["intent"]["requester"] | {"pane": "w1:pNew"}),
                duty["id"])
            with self.assertRaisesRegex(ValueError, "requester-conflict"):
                peer_steward.reserve_handoff_retire(
                    "old", duty["intent"]["identity"],
                    {"session_id": "later-sid", "harness": "opencode",
                     "pane": "w1:pNew", "server": "default"})
        self.assertEqual(len(store.list(kinds={"retire"})), 1)
        self.assert_authority("old-sid")

    def test_missing_late_replaced_ambiguous_and_unverified_legacy_marks_grant_nothing(self):
        for failure in ("missing", "late", "replaced", "ambiguous", "unverified"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root, \
                 mock.patch.dict(os.environ, {"XDG_STATE_HOME": root}):
                store, duty = self.booking()
                duty = self._blank_requester(store, duty)
                marker = peer_steward._seat_successor_path("w1:pNew")
                if failure == "missing":
                    marker.unlink()
                elif failure == "late":
                    mark = json.loads(marker.read_text())
                    mark["at"] = duty["accepted_at"] + 1
                    marker.write_text(json.dumps(mark))
                elif failure == "replaced":
                    peer_steward._mark_seat_successor("w1:pNew", "w1:pOld", "opencode", "later-sid")
                elif failure == "ambiguous":
                    peer_steward._mark_seat_successor("w1:pElse", "w1:pOld", "opencode", "new-sid")
                patches = self._legacy_identity_patches()
                if failure == "unverified":
                    patches = (patches[0], patches[1],
                               mock.patch.object(peer_steward, "_prompt_current_candidate",
                                                 return_value=(None, "target-identity-unverified")))
                with patches[0], patches[1], patches[2]:
                    result = peer_steward._retire_handover(duty)
                self.assertIn(result, (None, "skipped:successor-unverified"))
                self.assert_authority("old-sid")
                self.assertTrue(marker.exists() or failure == "missing")
                store._record_path(duty["id"]).unlink(missing_ok=True)

    def test_detached_lifecycle_repairs_only_the_booked_successor(self):
        store, duty = self.booking()
        store.update(duty["id"], state="complete", result="normal-exit")
        with mock.patch.object(peer_steward, "_current_session_identity", return_value=("observer", "claude")), \
             mock.patch.object(peer_steward, "_caller_pane", return_value=""), \
             mock.patch.object(peer_steward, "_ensure_watch_observers"), \
             mock.patch.object(peer_steward.dispatch_batch_obligations, "ensure_observers"), \
             mock.patch.object(peer_steward.peer_obligations, "ensure_runner", return_value=False):
            self.assertEqual(peer_steward.cmd_ensure_obligations(None), 0)
            # The callback only requests recovery; the existing runner owns
            # this same retained duty and performs its repair in its pass.
            peer_steward._reconcile_external_obligations(store)
        self.assert_authority()

    def test_later_requester_cannot_replay_another_sessions_retire(self):
        store, duty = self.booking()
        store.update(duty["id"], state="complete", result="normal-exit")
        with mock.patch.object(peer_steward, "_retire_target", return_value=("unknown", {
                "harness": "-", "name": "old", "pane": "-", "session_id": "-"}, 1, "pane-unverified")), \
             mock.patch.object(peer_steward, "_current_session_identity", return_value=("later-sid", "opencode")), \
             mock.patch.object(peer_steward, "_caller_pane", return_value="w1:pNew"), \
             patch_split(peer_steward, "_herdr_missing", return_value=False):
            self.assertEqual(peer_steward.cmd_retire(SimpleNamespace(target="old")), 1)
        self.assert_authority("old-sid")

    def test_cancelled_or_unknown_lifetime_grants_nothing(self):
        store, duty = self.booking()
        with patch_split(peer_steward, "_retire_subject_state", return_value="unknown"):
            peer_steward._resume_retire_obligation(duty, store)
        self.assert_authority("old-sid")
        cancelled = store.update(duty["id"], state="cancelled")
        peer_steward._resume_retire_obligation(cancelled, store)
        self.assert_authority("old-sid")

    def test_a_later_or_mismatched_successor_mark_grants_nothing(self):
        store, duty = self.booking()
        marker = peer_steward._seat_successor_path("w1:pNew")
        original = json.loads(marker.read_text())
        for change in ({"at": duty["accepted_at"] + 1}, {"server": "another-server"},
                       {"successor": {"harness": "claude", "session_id": "new-sid"}},
                       {"successor": {"harness": "opencode", "session_id": "later-sid"}}):
            with self.subTest(change=change):
                marker.write_text(json.dumps({**original, **change}))
                self.assertEqual(peer_steward._retire_handover(duty), "skipped:successor-unverified")
                self.assert_authority("old-sid")
                self.assertTrue(marker.exists())

    def test_completed_retirement_without_original_birth_evidence_grants_nothing(self):
        store, duty = self.booking()
        completed = store.update(duty["id"], state="complete", result="normal-exit")
        for foreground in (None, {"pid": 4242}, {"start": "800"}):
            with self.subTest(foreground=foreground):
                value = json.loads(json.dumps(completed))
                value["intent"]["identity"]["foreground"] = foreground
                peer_steward._resume_retire_obligation(value, store)
                self.assert_authority("old-sid")

    def test_existing_transfer_to_another_successor_is_preserved(self):
        import session_tidy
        store, duty = self.booking()
        self.handover.record_retire_handover("old-sid", "claude", "first-successor", "codex",
            jobs=self.jobs_path, successor_seat=session_tidy._pane_seat_of("w1:pFirst"))
        store.update(duty["id"], state="complete", result="normal-exit")
        peer_steward._resume_retire_obligation(store.get(duty["id"]), store)
        self.assert_authority("first-successor")

    def test_concurrent_replacement_survives_the_original_marks_consumption(self):
        store, duty = self.booking()
        marker = peer_steward._seat_successor_path("w1:pNew")
        requested, replaced = threading.Event(), threading.Event()
        import opencode_session_runtime as runtime
        original_read = runtime._read_native_record
        reads = 0

        def replace():
            requested.wait(2)
            peer_steward._mark_seat_successor("w1:pNew", "w2:pLater", "opencode", "later-sid")
            replaced.set()

        writer = threading.Thread(target=replace)
        writer.start()

        def read(path, *args, **kwargs):
            nonlocal reads
            value = original_read(path, *args, **kwargs)
            if path == marker:
                reads += 1
                if reads == 2:
                    # Precisely between the preimage read and unlink. Without
                    # shared serialization the replacement is lost here.
                    requested.set()
                    self.assertFalse(replaced.wait(.1))
            return value

        try:
            with mock.patch.object(runtime, "_read_native_record", read):
                self.assertEqual(peer_steward._retire_handover(duty), "1")
        finally:
            requested.set()
            writer.join(2)
        self.assertFalse(writer.is_alive())
        self.assertTrue(replaced.is_set())
        self.assertEqual(json.loads(marker.read_text())["successor"]["session_id"], "later-sid")
        self.assert_authority()


class RetireTerminalFollowupRegressionTest(_TmpRootMixin, unittest.TestCase):
    def completed_duty(self, *, requester=True):
        self._followup_counter = getattr(self, "_followup_counter", 0) + 1
        store = peer_steward.peer_obligations.ObligationStore()
        identity = {"server": "default", "pane": "w1:pPredecessor",
                    "harness": "claude", "session_id": "old-session",
                    "name": "old-agent"}
        intent = {"target": "old-agent"}
        if requester:
            intent["requester"] = {
                "server": "default", "pane": "w1:pRequester",
                "harness": "codex", "session_id": "next-session"}
        duty = store.create(f"retire-terminal-followup-{self._followup_counter}",
                            "retire", identity, intent)
        duty = store.update(duty["id"], state="complete", result="normal-exit",
                            delivery="completed", cleanup="complete",
                            observation={"phase": "complete"})
        return store, duty, identity

    def test_legacy_handover_uses_the_existing_caller_fallback(self):
        identity = {"pane": "w1:pPredecessor", "harness": "claude",
                    "session_id": "old-session"}
        duty = {"intent": {"identity": identity}}
        with mock.patch.object(peer_steward, "_current_session_identity",
                               return_value=("caller-session", "codex")), \
             mock.patch.object(peer_steward, "_retire_predecessor",
                               return_value=(identity, None)), \
             mock.patch.object(peer_steward, "_seat_handover", return_value="none") as seat:
            self.assertEqual(peer_steward._retire_handover(duty), "none")

        self.assertEqual(seat.call_args.args, (identity, "caller-session", "codex"))
        self.assertIsNone(seat.call_args.kwargs["requester"])

    def test_none_handover_keeps_followup_pending_while_requester_mark_exists(self):
        store, duty, identity = self.completed_duty()
        requester_mark = self.tmp_root / "requester-successor.json"
        requester_mark.touch()
        predecessor_mark = self.tmp_root / "predecessor-successor.json"
        paths = {"w1:pRequester": requester_mark, "w1:pPredecessor": predecessor_mark}
        with patch_split(peer_steward, "_seat_successor_path",
                               side_effect=lambda pane: paths[pane]), \
             mock.patch.object(peer_steward, "_request_watch_terminal", return_value=0):
            result = peer_steward._mark_completed_retire_followup(
                store, duty, identity, "completed-retire", "none")

        self.assertEqual(result["observation"]["terminal_followup"], "pending")
        self.assertEqual(result["observation"]["terminal_followup_result"], "none")
        self.assertIn(duty["id"], {row["id"] for row in store.list()})

    def test_followup_checks_requester_pane_and_treats_only_enoent_as_absent(self):
        store, duty, identity = self.completed_duty()
        requester_mark = self.tmp_root / "requester-successor.json"
        predecessor_mark = self.tmp_root / "predecessor-successor.json"
        predecessor_mark.touch()
        paths = {"w1:pRequester": requester_mark, "w1:pPredecessor": predecessor_mark}
        with patch_split(peer_steward, "_seat_successor_path",
                               side_effect=lambda pane: paths[pane]) as path, \
             mock.patch.object(peer_steward, "_request_watch_terminal", return_value=0):
            self.assertTrue(peer_steward._retire_followup_ready(
                duty, identity, "completed-retire", "none"))
        path.assert_called_once_with("w1:pRequester")

        class UnreadableMark:
            @staticmethod
            def lstat():
                raise PermissionError("fixture denies lstat")

        with patch_split(peer_steward, "_seat_successor_path",
                               return_value=UnreadableMark()), \
             mock.patch.object(peer_steward, "_request_watch_terminal", return_value=0):
            self.assertFalse(peer_steward._retire_followup_ready(
                duty, identity, "completed-retire", "none"))

    def test_defer_if_missing_keeps_unlocated_successor_unresolved(self):
        store, duty, identity = self.completed_duty(requester=False)
        with mock.patch.object(peer_steward, "_caller_pane", return_value=""), \
             mock.patch.object(peer_steward, "_request_watch_terminal", return_value=0):
            self.assertFalse(peer_steward._retire_followup_ready(
                duty, identity, "target-already-gone", None, defer_if_missing=True))

        store, duty, identity = self.completed_duty()
        with patch_split(peer_steward, "_seat_successor_path",
                               return_value=self.tmp_root / "missing-successor.json"), \
             mock.patch.object(peer_steward, "_request_watch_terminal", return_value=0):
            self.assertFalse(peer_steward._retire_followup_ready(
                duty, identity, "target-already-gone", None, defer_if_missing=True))

    def test_failed_terminal_watch_request_stays_active_until_retry_succeeds(self):
        store, duty, identity = self.completed_duty()
        watch = mock.Mock(side_effect=[OSError("terminal-watch write failed"), 0])
        with patch_split(peer_steward, "_seat_successor_path",
                               return_value=self.tmp_root / "absent-successor.json"), \
             mock.patch.object(peer_steward, "_request_watch_terminal", watch):
            pending = peer_steward._mark_completed_retire_followup(
                store, duty, identity, "completed-retire", "none")
            self.assertEqual(pending["observation"]["terminal_followup"], "pending")
            self.assertIn(duty["id"], {row["id"] for row in store.list()})
            with mock.patch.object(peer_steward, "_retire_handover", return_value="none"):
                peer_steward._resume_retire_obligation(
                    pending, store, persist_followup=True)

        self.assertEqual(watch.call_count, 2)
        completed = store.get(duty["id"])
        self.assertEqual(completed["observation"]["terminal_followup"], "complete")
        self.assertNotIn(duty["id"], {row["id"] for row in store.list()})

    def test_inaccessible_watch_enumeration_stays_active_until_readable_retry(self):
        store, duty, identity = self.completed_duty()
        watch_root = self.tmp_root / "watch-root"
        watch_root.mkdir()
        absent_successor = self.tmp_root / "absent-successor.json"
        real_scandir = os.scandir
        scans = []

        def fail_first_watch_scan(path):
            if isinstance(path, (str, os.PathLike)) and Path(path) == watch_root:
                scans.append(Path(path))
                if len(scans) == 1:
                    raise PermissionError("fixture denies watch-root enumeration")
            return real_scandir(path)

        with patch_split(peer_steward, "_watch_root", return_value=watch_root), \
             patch_split(peer_steward, "_seat_successor_path",
                               return_value=absent_successor), \
             mock.patch.object(peer_steward.os, "scandir",
                               side_effect=fail_first_watch_scan):
            pending = peer_steward._mark_completed_retire_followup(
                store, duty, identity, "completed-retire", "none")
            self.assertEqual(pending["observation"]["terminal_followup"], "pending")
            self.assertIn(duty["id"], {row["id"] for row in store.list()})
            self.assertEqual(scans, [watch_root])

            with mock.patch.object(peer_steward, "_retire_handover", return_value="none"):
                peer_steward._resume_retire_obligation(
                    pending, store, persist_followup=True)

        self.assertEqual(scans, [watch_root, watch_root])
        self.assertEqual(peer_steward._watch_entries(watch_root), [])
        self.assertEqual(peer_steward._watch_entries(watch_root / "missing"), [])
        completed = store.get(duty["id"])
        self.assertEqual(completed["observation"]["terminal_followup"], "complete")
        self.assertNotIn(duty["id"], {row["id"] for row in store.list()})

    def test_watch_root_symlink_loop_stays_active_until_same_path_is_repaired(self):
        store, duty, identity = self.completed_duty()
        watch_root = self.tmp_root / "watch-root"
        watch_root.symlink_to(watch_root)
        self.assertFalse(watch_root.is_dir())
        with self.assertRaises(OSError) as raised:
            with os.scandir(watch_root):
                pass
        self.assertEqual(raised.exception.errno, errno.ELOOP)

        absent_successor = self.tmp_root / "absent-successor.json"
        with patch_split(peer_steward, "_watch_root", return_value=watch_root), \
             patch_split(peer_steward, "_seat_successor_path",
                               return_value=absent_successor):
            pending = peer_steward._mark_completed_retire_followup(
                store, duty, identity, "completed-retire", "none")
            self.assertEqual(pending["observation"]["terminal_followup"], "pending")
            self.assertIn(duty["id"], {row["id"] for row in store.list()})

            watch_root.unlink()
            watch_root.mkdir()
            with mock.patch.object(peer_steward, "_retire_handover", return_value="none"):
                peer_steward._resume_retire_obligation(
                    pending, store, persist_followup=True)

        completed = store.get(duty["id"])
        self.assertEqual(completed["observation"]["terminal_followup"], "complete")
        self.assertNotIn(duty["id"], {row["id"] for row in store.list()})


class AcceptedHandoffRunnerSelectionTest(_TmpRootMixin, unittest.TestCase):
    def test_exact_accepted_handoff_replay_keeps_notice_and_steward_duties(self):
        import session_tidy_runner as runner
        root = self.tmp_root
        predecessor = {"server": "default", "pane": "old-pane", "harness": "codex",
                       "session_id": "old-session"}
        retire = {"intent": {"kind": "retire", "identity": predecessor, "target": "old-target"},
                  "state": "pending"}
        accepted = {"id": "accepted-message", "state": "complete",
                    "intent": {"kind": "message", "ref": "accepted-ref", "handoff_acceptance": True}}
        notices = [{"id": kind, "intent": {"kind": kind}}
                   for kind in ("session-notice", "run-notice", "steward-check")]
        store = SimpleNamespace(root=root, list=lambda **_kw: [accepted], get=lambda _id: retire)
        row = {"acceptance": {"read_and_accepted": True, "settled": True, "retire_id": "retire-id"}}
        replayed = peer_steward._HANDOFF_ACCEPTANCE_REPLAYED
        with mock.patch.object(peer_steward.peer_message, "_read_pending", return_value=row), \
             mock.patch.object(runner, "_handoff_context", return_value=(
                 {"target": "old-target"}, predecessor, {}, {}, "old-target")):
            self.assertEqual(peer_steward._runner_duties(store, notices), [*notices, accepted])
            self.assertEqual(peer_steward._settled_handoff_acceptance_duties(store), [accepted])
            retire["intent"]["identity"] = {**predecessor, "session_id": "unrelated"}
            self.assertEqual(peer_steward._runner_duties(store, notices), notices)
            self.assertEqual(peer_steward._settled_handoff_acceptance_duties(store), [])
            retire["intent"]["identity"] = predecessor
            key = (str(root.resolve()), accepted["id"])
            replayed.add(key)
            try:
                self.assertEqual(peer_steward._runner_duties(store, notices), notices)
            finally:
                replayed.discard(key)


class _ShellHost:
    """A simulated session-shell host for exact recipients; no window is ever typed.

    ``session_shell.exact_binding`` answers the attached seats, ``session_shell.control``
    plays the host: ``status`` reports ``self.status``; ``inject`` makes the host's final
    observation (``before_input`` may refuse it, not started), begins the exact pending
    write as the real host does, gives the text to ``self.native`` (the native input
    counted by tests) and replies ``self.result``. Unattached sessions are absent."""

    def __init__(self, seats):
        self.seats = {}
        for harness, sid in seats:
            self.attach(harness, sid)
        self.status = {"ok": True, "state": "idle", "empty": True, "focused": False, "epoch": 1}
        self.before_input = None
        self.result = {"ok": True}
        self.native = mock.Mock(name="native-input", return_value=None)

    def attach(self, harness, sid):
        self.seats[(harness, sid)] = {"harness": harness, "sid": sid, "native_pid": os.getpid(),
                                      "host_pid": os.getpid(), "incarnation": "fixture-" + sid}

    def exact_binding(self, harness, sid):
        binding = self.seats.get((harness, sid))
        return (binding, "") if binding is not None else (None, "attachment-missing")

    def control(self, binding, operation, **fields):
        import session_shell
        if operation == "status":
            return dict(self.status)
        if operation != "inject":
            return {"ok": False, "reason": "unknown-operation", "not_started": True}
        if self.before_input is not None:
            refusal = self.before_input(binding)
            if refusal is not None:
                return refusal
        peer = session_shell._peer_message()
        row, claim = peer._read_pending(fields.get("ref")), fields.get("claim") or {}
        begun = row and peer.begin_pending_write(
            row, recipient={"harness": binding["harness"], "session_id": binding["sid"]},
            receipt="session-shell-inflight",
            claimant={"pid": claim.get("pid"), "start": claim.get("start")})
        if not begun:
            return {"ok": False, "reason": "peer-claim-changed", "not_started": True}
        self.native(binding["harness"], binding["sid"], row["text"])
        return dict(self.result)

    def presence(self, harness, sid):
        return "present" if (harness, sid) in self.seats else "absent"

    @contextlib.contextmanager
    def attached(self):
        import peer_address
        import session_shell
        with mock.patch.object(session_shell, "exact_binding", side_effect=self.exact_binding), \
             mock.patch.object(session_shell, "control", side_effect=self.control), \
             mock.patch.object(peer_address, "presence", side_effect=self.presence):
            yield self


class SessionNoticeIntegrationCorrectionTest(_TmpRootMixin, unittest.TestCase):
    def notice(self, harness="claude", sid="A", key="correction"):
        import session_notice
        return _legacy_notice(harness, sid, key=key, subject="integration notice",
                                     text="immutable completion", jobs=self.jobs_path)

    def edge(self, harness, origin, target):
        import session_tidy as tidy
        seat = tidy.Seat("pane", "integration-" + harness, pane="w1:pIntegration", harness=harness)
        tidy._append_ledger_entry(seat, {"event": "succession", "source": "tidy-clear",
            "harness": harness, "sid": target, "from": {"harness": harness, "sid": origin}, "ts": 1})
        return seat

    def store(self):
        return peer_steward.peer_obligations.ObligationStore(self.tmp_root)

    def duties(self):
        return self.store().list(kinds={"session-notice"})

    @contextlib.contextmanager
    def transport(self, sid="B", *, readiness=None):
        """Real courier, peer preparation and claims; ``sid`` is a shell-attached Claude.

        Yields the native-input mock: one call per write the host really began.
        ``readiness`` is the host's final observation before that write."""
        inventory = {"result": {"agents": [{"agent": "claude",
            "agent_session": {"value": sid}, "pane_id": "w1:pLive"}]}}
        host = _ShellHost([("claude", sid)])
        if readiness is not None:
            def before_input(_binding):
                if getattr(readiness(), "state", None) != "ready":
                    return {"ok": False, "reason": "not-idle-empty", "not_started": True}
                return None
            host.before_input = before_input
        host.native.host = host
        with mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps(inventory))), \
             host.attached():
            yield host.native

    def test_predecessor_own_key_is_not_current_after_confirmed_clear(self):
        import dispatch_session_sweep as sweep
        import dispatch_seat_handover as handover
        import dispatch_pending_delivery as pending
        for harness, carrier in [("claude", "claude-parent-runtime"),
                                 ("codex", "codex-native-queue"), ("opencode", "opencode-turn")]:
            with self.subTest(harness=harness):
                from harness_capabilities import parent_completion
                carrier = parent_completion(harness)["carrier"]
                old, new = harness + "-A", harness + "-B"
                n = self.notice(harness, old, "own-" + harness)
                seat = self.edge(harness, old, new)
                before = pending.read(self.tmp_root, old, n["delivery_id"])
                with mock.patch.object(handover, "pane_seat", return_value=seat):
                    self.assertEqual(sweep.sweep_deliver(self.tmp_root, carrier, old)[0], [])
                self.assertEqual(pending.read(self.tmp_root, old, n["delivery_id"]), before)

    def test_stale_inherited_scope_cannot_override_a_new_fork(self):
        import dispatch_session_sweep as sweep
        import dispatch_seat_handover as handover
        n = self.notice()
        seat = self.edge("claude", "A", "B")
        original = handover.storage_recipients
        def enumerate_then_fork(*args, **kwargs):
            found = original(*args, **kwargs)
            self.edge("claude", "A", "C")
            return found
        with mock.patch.object(handover, "pane_seat", return_value=seat), \
             mock.patch.object(handover, "storage_recipients", side_effect=enumerate_then_fork):
            self.assertEqual(sweep.addressed_records(self.tmp_root, "claude-parent-runtime", "B")[0], [])
        self.assertIsNone(handover.record_recipient(n, "A", harness="claude"))

    def test_legacy_exact_attempt_binding_cannot_override_a_changed_current_chain(self):
        import dispatch_seat_handover as handover
        record = {"attempt_ids": ["legacy-attempt"]}
        allowed = frozenset({"legacy-attempt"})
        import session_tidy as tidy
        tidy._append_ledger_entry(tidy.Seat("pane", "legacy-bound-B"), {
            "event": "handover", "harness": "claude", "sid": "B", "from": "A", "ts": 1,
            "bindings": [{"jobs": str(self.jobs_path), "route": "rt-legacy", "hash": "sha256:legacy",
                          "node": "execute", "attempt": "legacy-attempt", "parent": "A"}]})
        self.assertTrue(handover.record_for_session(record, allowed, session="B",
                                                    harness="claude", storage_key="A"))
        self.assertTrue(handover.record_for_session(record, allowed))
        self.assertFalse(handover.record_for_session(
            {"attempt_ids": ["legacy-attempt", "foreign-attempt"]}, allowed))
        self.edge("claude", "A", "B")
        self.edge("claude", "B", "C")
        self.assertFalse(handover.record_for_session(record, allowed, session="B",
                                                     harness="claude", storage_key="A"))
        self.assertFalse(handover.record_for_session(record, None, session="A",
                                                     harness="claude", storage_key="A"))

    def test_sweep_rechecks_after_both_native_observations_and_releases_only_its_claim(self):
        import dispatch_session_sweep as sweep
        import dispatch_seat_handover as handover
        import dispatch_pending_delivery as pending
        for move_on in (1, 2):
            with self.subTest(move_on=move_on):
                old, new, end = ("old-" + str(move_on), "new-" + str(move_on), "end-" + str(move_on))
                n = self.notice(sid=old, key="observation-" + str(move_on))
                seat = self.edge("claude", old, new)
                calls = []
                def observe(*args):
                    calls.append(True)
                    if len(calls) == move_on:
                        self.edge("claude", new, end)
                    return None
                with mock.patch.object(handover, "pane_seat", return_value=seat):
                    self.assertEqual(sweep.sweep_deliver(self.tmp_root, "claude-parent-runtime", new,
                                                         receipt_observer=observe)[0], [])
                stored = pending.read(self.tmp_root, old, n["delivery_id"])
                self.assertEqual(stored["state"], "pending")
                self.assertIsNone(stored["claim_owner"])

    def test_courier_rechecks_after_claim_before_calling_the_real_consumer(self):
        import dispatch_pending_delivery as pending
        n = self.notice()
        self.edge("claude", "A", "B")
        claim = pending.claim
        def claim_then_move(*args, **kwargs):
            result = claim(*args, **kwargs)
            self.edge("claude", "B", "C")
            return result
        with self.transport() as native, mock.patch.object(pending, "claim", side_effect=claim_then_move):
            peer_steward._resume_session_notice_obligations(self.duties(), self.store())
        native.assert_not_called()
        self.assertEqual(pending.read(self.tmp_root, "A", n["delivery_id"])["state"], "pending")

    def test_common_preparation_keeps_runtime_schema_sender_and_reference_validation(self):
        import dispatch_pending_delivery as pending
        n = self.notice()
        duty = self.duties()[0]
        pending.claim(self.tmp_root, "A", n["delivery_id"], claim_owner="retained-rewake:" + duty["id"],
                      lease_seconds=30, require_generation_proof=False, jobs=self.jobs_path)
        recipient = {"harness": "claude", "session_id": "A"}
        runtime = {"harness": "runtime", "session_id": None, "name": "Hearting runtime"}
        refs = [duty["id"], n["delivery_id"]]
        producer = {"schema": "session-notice-runtime-v1", "duty_id": duty["id"],
            "jobs": str(self.jobs_path), "records": [{"duty_id": duty["id"], "storage_sid": "A",
                "delivery_id": n["delivery_id"], "receipt_digest": n["receipt_digest"]}],
            "recipient": recipient}
        invalid = [({"harness": "claude", "session_id": "ordinary-peer", "name": "peer"}, producer, refs),
                   (runtime, dict(producer, schema="unknown-producer"), refs),
                   (runtime, producer, [duty["id"]])]
        for sender, binding, selected_refs in invalid:
            with self.subTest(sender=sender, schema=binding["schema"], refs=selected_refs), \
                 self.assertRaises(ValueError):
                peer_steward.peer_message.prepare_peer_message("notice", sender, recipient, defer=True,
                    refs=selected_refs, producer_binding=binding)

    def test_last_native_guard_rechecks_notice_before_the_first_write(self):
        import dispatch_pending_delivery as pending
        n = self.notice()
        self.edge("claude", "A", "B")
        guards = []
        def guard():
            # The host's last native observation before its first write.
            guards.append(True)
            self.edge("claude", "B", "C")
            return SimpleNamespace(state="ready")
        with self.transport(readiness=guard) as native:
            peer_steward._resume_session_notice_obligations(self.duties(), self.store())
        self.assertEqual(len(guards), 1)
        native.assert_not_called()
        # Preparation already happened: retain the frozen transfer and never
        # turn the refused first write into authority for a replacement send.
        ref = self.duties()[0]["observation"]["transfer_ref"]
        transfer = peer_steward.peer_message._read_pending(ref)
        self.assertEqual((transfer["state"], transfer["to"]["session_id"]), ("pending", "B"))
        self.assertEqual(pending.read(self.tmp_root, "A", n["delivery_id"])["state"], "sent-ambiguous")

    def prepared_notice(self, harness, old, current, key):
        import dispatch_pending_delivery as pending
        n = self.notice(harness, old, key)
        self.edge(harness, old, current)
        duty = next(d for d in self.duties() if d["intent"]["identity"]["storage_sid"] == old)
        owner = "retained-rewake:" + duty["id"]
        claimed = pending.claim(self.tmp_root, old, n["delivery_id"], claim_owner=owner,
                                lease_seconds=1, require_generation_proof=False, jobs=self.jobs_path)
        recipient = {"harness": harness, "session_id": current}
        producer = {"schema": "session-notice-runtime-v1", "duty_id": duty["id"],
            "jobs": str(self.jobs_path), "records": [{"duty_id": duty["id"], "storage_sid": old,
                "delivery_id": n["delivery_id"], "receipt_digest": n["receipt_digest"]}],
            "recipient": recipient}
        from dispatch_recipient_receipt import receipt_marker
        text, ref = peer_steward.peer_message.prepare_peer_message("frozen notice\n" + receipt_marker(claimed, old),
            {"harness": "runtime", "session_id": None, "name": "Hearting runtime"}, recipient,
            defer=True, refs=[duty["id"], n["delivery_id"]], producer_binding=producer)
        pending.mark_sent_ambiguous(self.tmp_root, old, n["delivery_id"], claim_owner=owner)
        self.store().update(duty["id"], observation={"transfer_ref": ref})
        return n, ref, recipient

    def test_deferred_claude_and_opencode_notice_writes_recheck_current_authority(self):
        import dispatch_pending_delivery as pending
        for harness in ("claude", "opencode"):
            for move in (False, True):
                with self.subTest(harness=harness, move=move):
                    old, current, end = (harness + "-A-" + str(move), harness + "-B-" + str(move),
                                         harness + "-C-" + str(move))
                    n, ref, recipient = self.prepared_notice(harness, old, current, old)
                    record = pending.read(self.tmp_root, old, n["delivery_id"])
                    with mock.patch.object(pending.time, "monotonic_ns",
                                           return_value=record["claim_deadline_ns"] + 1):
                        reserved = peer_steward.peer_message.reserve_pending_herdr(ref, recipient)
                        self.assertIsNotNone(reserved)
                        if move:
                            self.edge(harness, current, end)
                        writing = peer_steward.peer_message.begin_pending_write(reserved, recipient=recipient)
                    self.assertEqual(writing is None, move)
                    if move:
                        peer_steward.peer_message.release_pending_herdr_claim(reserved)
                        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "pending")
                    else:
                        self.assertEqual(writing["state"], "unverified")
                        self.assertIsNone(peer_steward.peer_message.begin_pending_write(
                            reserved, recipient=recipient), "an unverified first write is never repeated")
                    after = pending.read(self.tmp_root, old, n["delivery_id"])
                    self.assertEqual({**after, "claim_deadline_ns": record["claim_deadline_ns"]}, record)
                    if move:
                        self.assertEqual(after, record)
                    else:
                        self.assertGreater(after["claim_deadline_ns"], record["claim_deadline_ns"] + 1)

    def test_peer_codex_before_add_rechecks_lineage_after_native_observation(self):
        import codex_queue_delivery
        import codex_queue_dispatch
        import dispatch_pending_delivery as pending
        for move in (False, True):
            with self.subTest(move=move):
                old, current, end = "queue-A-" + str(move), "queue-B-" + str(move), "queue-C-" + str(move)
                n, ref, recipient = self.prepared_notice("codex", old, current, old)
                added = []
                def send(*args, **kwargs):
                    if kwargs["allow_add"]:
                        if move:
                            self.edge("codex", current, end)
                        kwargs["before_add"]()
                        added.append(kwargs["thread_id"])
                    return {"status": "queued"}
                with mock.patch.object(codex_queue_dispatch, "resolve_queue_socket",
                                       return_value=Path("/tmp/unused-fake-queue.sock")), \
                     mock.patch.object(codex_queue_delivery, "send_at_least_once", side_effect=send):
                    result = peer_steward.peer_message.deliver_pending_codex(ref)
                    if not move:
                        peer_steward.peer_message.deliver_pending_codex(ref)
                self.assertEqual(added, [] if move else [current])
                self.assertEqual(result["status"], "unverified" if move else "queued")
                self.assertEqual(pending.read(self.tmp_root, old, n["delivery_id"])["state"], "sent-ambiguous")

    def test_route_free_missing_registry_delivers_receives_and_acks_original_identity(self):
        import dispatch_pending_delivery as pending
        self.jobs_path.unlink()  # This fixture's empty registry only.
        n = self.notice()
        self.edge("claude", "A", "B")
        # Writer retries keep the same duty; old-writer discovery restores it.
        self.assertEqual(self.notice()["delivery_id"], n["delivery_id"])
        duty = self.duties()[0]
        (self.store().root / (duty["id"] + ".json")).unlink()
        peer_steward._discover_session_notice_duties(self.store())
        self.assertEqual([d["id"] for d in self.duties()], [duty["id"]])
        with self.transport() as native:
            peer_steward._resume_session_notice_obligations(self.duties(), self.store())
        native.assert_called_once()
        row = pending.read(self.tmp_root, "A", n["delivery_id"])
        self.assertEqual(row["state"], "sent-ambiguous")
        self.assertEqual(row["receipt_digest"], n["receipt_digest"])
        ref = self.duties()[0]["observation"]["transfer_ref"]
        transfer = peer_steward.peer_message._read_pending(ref)
        self.assertEqual(transfer["state"], "queued")
        self.assertEqual(peer_steward.peer_message.prompt_origin(
            transfer["text"], "claude", "B")["producer_binding"], transfer["producer_binding"])
        self.edge("claude", "B", "C")  # Receipt from historical B is still exact.
        peer_steward.peer_message.receive_peer_message(
            transfer["text"], {"harness": "claude", "session_id": "B"})
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "received")
        self.assertEqual(pending.read(self.tmp_root, "A", n["delivery_id"])["state"], "acked")
        self.assertEqual(self.store().get(duty["id"])["state"], "complete")
        self.assertIsNone(pending.read(self.tmp_root, "B", n["delivery_id"]))
        self.assertFalse(self.jobs_path.exists())

    def test_exact_receipt_ack_failure_is_retried_without_resubmission_even_after_fork(self):
        import dispatch_pending_delivery as pending
        n = self.notice()
        self.edge("claude", "A", "B")
        with self.transport() as native:
            peer_steward._resume_session_notice_obligations(self.duties(), self.store())
        native.assert_called_once()
        ref = self.duties()[0]["observation"]["transfer_ref"]
        transfer = peer_steward.peer_message._read_pending(ref)
        with mock.patch.object(pending, "ack_received", side_effect=OSError("fake ACK write failure")):
            peer_steward.peer_message.receive_peer_message(
                transfer["text"], {"harness": "claude", "session_id": "B"})
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "received")
        self.assertNotEqual(pending.read(self.tmp_root, "A", n["delivery_id"])["state"], "acked")
        self.edge("claude", "A", "C")
        with mock.patch.object(peer_steward, "cmd_prompt") as send:
            peer_steward._resume_session_notice_obligations(self.duties(), self.store())
        send.assert_not_called()
        self.assertEqual(pending.read(self.tmp_root, "A", n["delivery_id"])["state"], "acked")

    def test_census_errors_stay_unknown_instead_of_escaping_reconciliation(self):
        import session_shell
        self.notice()
        errors = [OSError("fake attachment read"), subprocess.TimeoutExpired(["fake-observer"], 5),
                  ValueError("fake attachment record"), json.JSONDecodeError("fake", "{", 0)]
        for error in errors:
            with self.subTest(error=str(error)), \
                 mock.patch.object(peer_steward, "_HERDR_SESSION", "original-endpoint"), \
                 mock.patch.object(peer_steward, "cmd_prompt") as send, \
                 mock.patch.object(session_shell, "exact_binding", side_effect=error):
                peer_steward._reconcile_obligations(self.store(), self.duties(), retry_state={})
                send.assert_not_called()
                self.assertEqual(peer_steward._HERDR_SESSION, "original-endpoint")
                duty = self.duties()[0]
                self.assertEqual(duty["state"], "unknown")
                self.assertRegex(duty["observation"]["reason"],
                                 r"^observer-unavailable:[A-Za-z_][A-Za-z0-9_]*$")

    def test_expired_unsubmitted_claim_recovers_after_interruption_with_original_keys(self):
        import dispatch_pending_delivery as pending
        n = self.notice()
        self.edge("claude", "A", "B")
        claim = pending.claim(self.tmp_root, "A", n["delivery_id"], claim_owner="interrupted-owner",
                              lease_seconds=1, require_generation_proof=False, jobs=self.jobs_path)
        before = pending.received_ack_identity(claim, "A", n["delivery_id"])
        with mock.patch.object(pending.time, "monotonic_ns", return_value=claim["claim_deadline_ns"] + 1), \
             self.transport() as native:
            peer_steward._resume_session_notice_obligations(self.duties(), self.store())
        native.assert_called_once()
        after = pending.read(self.tmp_root, "A", n["delivery_id"])
        self.assertEqual(pending.received_ack_identity(after, "A", n["delivery_id"]), before)
        self.assertEqual(after["attempts"], claim["attempts"] + 1)
        self.assertEqual(after["claim_owner"], "retained-rewake:" + self.duties()[0]["id"])
        self.assertEqual(after["state"], "sent-ambiguous")

    def test_real_runner_continues_after_census_error_with_readiness_lease_and_context_intact(self):
        self.notice(sid="A-blocked", key="loop-first")
        self.notice(sid="Z-live", key="loop-later")
        store = self.store()
        registered = store.create("registered-batch-loop-control", "registered-batch",
                                  {"session_id": "registered-control"}, {})
        lock_path = store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME
        started, stopped = threading.Event(), threading.Event()
        worker = peer_steward._runner_readiness_loop
        workers, passes, registered_calls = [], [], []
        def readiness(root, fd, incarnation, stop):
            workers.append((threading.current_thread(), incarnation))
            self.assertTrue(peer_steward._runner_lock_current(store, fd))
            started.set()
            try:
                worker(root, fd, incarnation, stop)
            finally:
                stopped.set()
        def registered_resume(duty, current_store, **kwargs):
            registered_calls.append(duty["id"])
            current_store.update(duty["id"], state="complete", cleanup="complete")
        def wait(_delay):
            passes.append(_delay)
            self.assertEqual(peer_steward._HERDR_SESSION, "runner-endpoint")
            if len(passes) == 4:
                lock_path.unlink()  # Only this local fake's normal replaced-root exit.
        import session_shell
        census_calls = []
        with lock_path.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            def census(*args, **kwargs):
                # The first recipient observation fails; later ones read the attachment.
                self.assertTrue(started.wait(3), "the actual readiness worker started")
                census_calls.append(True)
                if len(census_calls) == 1:
                    raise OSError("first recipient observation unavailable")
                return native.host.exact_binding(*args, **kwargs)
            with self.transport("Z-live") as native, \
                 mock.patch.object(session_shell, "exact_binding", side_effect=census), \
                 mock.patch.object(peer_steward, "_HERDR_SESSION", "runner-endpoint"), \
                 mock.patch.object(peer_steward, "_runner_readiness_loop", side_effect=readiness), \
                 mock.patch.object(peer_steward, "_resume_registered_obligation", side_effect=registered_resume), \
                 patch_split(peer_steward, "_external_obligation_snapshot", return_value={}), \
                 mock.patch.object(peer_steward, "_external_obligation_signature", return_value=()), \
                 mock.patch.object(peer_steward, "_external_obligation_snapshot_unchanged", return_value=True), \
                 mock.patch.object(peer_steward, "_reconcile_external_obligations"), \
                 mock.patch.object(peer_steward, "_publish_watch_readiness"), \
                 mock.patch.object(peer_steward, "_runner_readiness_fds", return_value=[]), \
                 mock.patch.object(peer_steward.time, "sleep", side_effect=wait):
                self.assertEqual(peer_steward.cmd_obligation_runner(SimpleNamespace(
                    state_root=self.tmp_root, lock_fd=lock.fileno())), 0)
            native.assert_called_once()
        self.assertEqual(len(passes), 4)
        self.assertEqual(registered_calls, [registered["id"]])
        self.assertTrue(stopped.is_set())
        self.assertEqual(len(workers), 1)
        self.assertTrue(workers[0][1])
        self.assertFalse(workers[0][0].is_alive(), "readiness worker joined on ordinary runner exit")

    def test_live_claim_is_not_stolen(self):
        import dispatch_pending_delivery as pending
        n = self.notice()
        claim = pending.claim(self.tmp_root, "A", n["delivery_id"], claim_owner="live-owner",
                              lease_seconds=30, require_generation_proof=False, jobs=self.jobs_path)
        with mock.patch.object(pending.time, "monotonic_ns", return_value=claim["claim_deadline_ns"] - 1), \
             self.transport("A") as native, mock.patch.object(pending, "reclaim", wraps=pending.reclaim) as reclaim:
            peer_steward._resume_session_notice_obligations(self.duties(), self.store())
        native.assert_not_called()
        reclaim.assert_not_called()
        self.assertEqual(pending.read(self.tmp_root, "A", n["delivery_id"]), claim)

    def test_prepared_queued_unverified_and_missing_transfers_never_create_a_new_send(self):
        import dispatch_pending_delivery as pending
        from dispatch_recipient_receipt import receipt_marker
        for state in ("pending", "queued", "unverified", "missing"):
            with self.subTest(state=state):
                n = self.notice(sid=state, key=state)
                duty = next(d for d in self.duties() if d["intent"]["identity"]["storage_sid"] == state)
                owner = "retained-rewake:" + duty["id"]
                claim = pending.claim(self.tmp_root, state, n["delivery_id"], claim_owner=owner,
                    lease_seconds=1, require_generation_proof=False, jobs=self.jobs_path)
                recipient = {"harness": "claude", "session_id": state}
                producer = {"schema": "session-notice-runtime-v1", "duty_id": duty["id"],
                    "jobs": str(self.jobs_path), "recipient": recipient, "records": [{
                        "duty_id": duty["id"], "storage_sid": state, "delivery_id": n["delivery_id"],
                        "receipt_digest": n["receipt_digest"]}]}
                _wire, ref = peer_steward.peer_message.prepare_peer_message(receipt_marker(claim, state),
                    {"harness": "runtime", "session_id": None, "name": "Hearting runtime"},
                    recipient, defer=True, refs=[duty["id"], n["delivery_id"]], producer_binding=producer)
                self.store().update(duty["id"], observation={"transfer_ref": ref})
                if state == "missing":
                    peer_steward.peer_message._pending_path(ref).unlink()  # Own isolated missing fixture.
                else:
                    peer_steward.peer_message._save_pending(dict(
                        peer_steward.peer_message._read_pending(ref), state=state))
                with mock.patch.object(pending.time, "monotonic_ns",
                                       return_value=claim["claim_deadline_ns"] + 1), \
                     mock.patch.object(peer_steward, "_schedule_message_obligation") as schedule, \
                     self.transport(state) as native:
                    peer_steward._resume_session_notice_obligations([self.store().get(duty["id"])], self.store())
                native.assert_not_called()
                self.assertEqual(schedule.call_count, int(state == "pending"))
                self.assertEqual(pending.read(self.tmp_root, state, n["delivery_id"]), claim)

    def test_native_queue_fence_precedes_expired_claim_recovery(self):
        import dispatch_pending_delivery as pending
        n = self.notice()
        claim = pending.claim(self.tmp_root, "A", n["delivery_id"], claim_owner="old-queue",
                              lease_seconds=1, require_generation_proof=False, jobs=self.jobs_path)
        identity = pending.submission_fence_identity(claim, "A")
        fence = pending.prepare_submission_fence(self.tmp_root, "A", n["delivery_id"],
                                                 claim_owner="old-queue", expected_identity=identity)
        with mock.patch.object(pending.time, "monotonic_ns", return_value=claim["claim_deadline_ns"] + 1), \
             self.transport("A") as native, mock.patch.object(pending, "reclaim", wraps=pending.reclaim) as reclaim:
            peer_steward._resume_session_notice_obligations(self.duties(), self.store())
        native.assert_not_called()
        reclaim.assert_not_called()
        self.assertEqual(pending.read_submission_fence(self.tmp_root, "A", n["delivery_id"],
                                                       expected_identity=identity), fence)
        self.assertEqual(pending.read(self.tmp_root, "A", n["delivery_id"]), claim)

    def test_claim_followed_by_observer_error_releases_before_preparation(self):
        import dispatch_pending_delivery as pending
        n = self.notice()
        self.edge("claude", "A", "B")
        with self.transport(), \
             mock.patch.object(peer_steward.peer_message, "_valid_session_notice_producer_binding",
                               side_effect=OSError("fake post-claim observer outage")):
            peer_steward._reconcile_obligations(self.store(), self.duties())
        row = pending.read(self.tmp_root, "A", n["delivery_id"])
        self.assertEqual(row["state"], "pending")
        self.assertIsNone(row["claim_owner"])
        self.assertEqual(self.duties()[0]["observation"]["reason"], "observer-unavailable:OSError")

    def test_route_free_retention_and_ack_reject_foreign_root_symlink_and_wrong_storage(self):
        import dispatch_pending_delivery as pending
        n = self.notice()
        duty = self.duties()[0]
        foreign = self.tmp_root / "foreign"
        foreign.mkdir()
        self.assertFalse(peer_steward.peer_obligations.retain_session_notice(
            foreign / "jobs.log", "claude", "A", n))
        link = self.tmp_root / "linked.log"
        link.symlink_to(self.jobs_path)
        self.assertFalse(peer_steward.peer_obligations.retain_session_notice(link, "claude", "A", n))
        self.assertFalse(peer_steward.peer_obligations.retain_session_notice(self.jobs_path, "claude", "B", n))
        with self.transport("A") as native:
            peer_steward._resume_session_notice_obligations([duty], self.store())
        native.assert_called_once()
        transfer = peer_steward.peer_message._read_pending(self.duties()[0]["observation"]["transfer_ref"])
        exact = dict(transfer, state="received", receipt="exact-peer-ref")
        for jobs in (str(link), str(foreign / "jobs.log")):
            altered = dict(exact, producer_binding=dict(exact["producer_binding"], jobs=jobs))
            self.assertFalse(peer_steward.peer_obligations.acknowledge_session_notice_delivery(
                altered, roots=[self.tmp_root]))
        self.assertNotEqual(pending.read(self.tmp_root, "A", n["delivery_id"])["state"], "acked")

    def test_missing_registry_and_missing_recipient_keep_the_original_notice(self):
        import dispatch_pending_delivery as pending
        self.jobs_path.unlink()
        n = self.notice()
        before = pending.read(self.tmp_root, "A", n["delivery_id"])
        with mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout='{"result":{"agents":[]}}')), \
             mock.patch.object(peer_steward, "cmd_prompt") as send:
            peer_steward._resume_session_notice_obligations(self.duties(), self.store())
        send.assert_not_called()
        self.assertEqual(pending.read(self.tmp_root, "A", n["delivery_id"]), before)
        self.assertEqual(self.duties()[0]["observation"]["reason"], "recipient-unavailable")
        self.assertFalse(self.jobs_path.exists())


class SessionNoticeTerminalIdentityTest(_TmpRootMixin, unittest.TestCase):
    """Terminal delivery settles only its original canonical or legacy duty."""
    transport = SessionNoticeIntegrationCorrectionTest.transport

    def setUp(self):
        import dispatch_contract
        for name, value in (("_proc_start_for_store", "100001"),
                            ("request_dispatch_reconciliation", False)):
            patch = mock.patch.object(peer_steward.peer_obligations, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        for name, value in (("process_start_ticks", "100001"),
                            ("process_identity_disposition", "live")):
            patch = mock.patch.object(dispatch_contract, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        super().setUp()
        self.store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)

    def notice(self, kind, key, *, harness="claude", jobs=None, lineage=None):
        import session_notice
        jobs = jobs or self.jobs_path
        writer = session_notice.notify if kind == "run-notice" else _legacy_notice
        extra = {"lineage": lineage} if kind == "run-notice" else {}
        record = writer(harness, "terminal-A", key=key, subject="terminal notice",
                        text="original result", route_id="rt-original", jobs=jobs, **extra)
        duties = self.store.list(kinds={kind})
        duty, = [d for d in duties if (d["intent"].get("delivery_id") or
                    d["intent"]["identity"].get("delivery_id")) == record["delivery_id"]]
        self.assertTrue(peer_steward.peer_obligations._notice_duty_matches(
            duty, record, self.tmp_root, "terminal-A", record["delivery_id"], jobs))
        return record, duty

    def replace_record(self, original, **changes):
        """Install a normally readable private replacement with its own sidecars."""
        import dispatch_pending_delivery as pending
        value = {**original, **changes}
        replacement_root = self.tmp_root / ("replacement-" + original["delivery_id"])
        pending.create(replacement_root, recipient_key="terminal-A", **{
            key: value[key] for key in ("recipient_kind", "delivery_id", "session_generation",
                "session_generation_supported", "attempt_ids", "parent_attempt_id", "route_id",
                "route_node", "receipt", "receipt_digest", "row_revisions", "lineage")})
        shutil.copytree(pending.record_directory(replacement_root, "terminal-A"),
                        pending.record_directory(self.tmp_root, "terminal-A"), dirs_exist_ok=True)
        return pending.read(self.tmp_root, "terminal-A", original["delivery_id"])

    def terminal(self, record, state):
        import dispatch_pending_delivery as pending
        delivery_id = record["delivery_id"]
        claimed = pending.claim(self.tmp_root, "terminal-A", delivery_id,
                                claim_owner="private-terminal", lease_seconds=30)
        if state == "acked":
            result = pending.ack(self.tmp_root, "terminal-A", delivery_id, acked_by="private-receiver")
        elif state == "rejected":
            result = pending.reject_claimed(self.tmp_root, "terminal-A", delivery_id,
                claim_owner="private-terminal", reason="private-carrier-refusal",
                expected_identity=pending.received_ack_identity(claimed, "terminal-A", delivery_id))
        else:
            result = pending.expire_if_due(self.tmp_root, "terminal-A", delivery_id,
                actor="dispatch-reconcile", reason="recipient-session-gone")
        self.assertEqual(result["state"], state)
        return result

    def resume(self, duties):
        peer_steward._resume_session_notice_obligations(duties, self.store)

    def test_matching_original_terminal_states_settle_both_producers_on_every_harness(self):
        import dispatch_pending_delivery as pending
        for kind in ("run-notice", "session-notice"):
            for harness in ("claude", "codex", "opencode"):
                for state in ("acked", "rejected", "expired"):
                    with self.subTest(kind=kind, harness=harness, state=state):
                        record, duty = self.notice(kind, kind + harness + state, harness=harness)
                        terminal = self.terminal(record, state)
                        path = pending.record_path(self.tmp_root, "terminal-A", record["delivery_id"])
                        before = (path.read_bytes(), path.stat().st_mtime_ns)
                        with mock.patch.object(peer_steward, "cmd_prompt") as send:
                            self.resume([duty])
                        send.assert_not_called()
                        settled = self.store.get(duty["id"])
                        self.assertEqual((settled["state"], settled["delivery"], settled["cleanup"]),
                                         ("complete", state, "complete"))
                        self.assertEqual(settled["intent"], duty["intent"])
                        self.assertEqual(pending.read(self.tmp_root, "terminal-A", record["delivery_id"]), terminal)
                        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_foreign_terminal_digest_and_carrier_leave_the_original_duty_unchanged(self):
        import dispatch_pending_delivery as pending
        for kind in ("run-notice", "session-notice"):
            for changed in ("digest", "carrier"):
                for state in ("acked", "rejected", "expired"):
                    with self.subTest(kind=kind, changed=changed, state=state):
                        record, duty = self.notice(kind, kind + changed + state)
                        receipt = {**record["receipt"], "text": "different immutable result"}
                        changes = ({"receipt": receipt, "receipt_digest": pending._canonical_receipt_digest(receipt)}
                                   if changed == "digest" else {"recipient_kind": "codex-native-queue"})
                        replacement = self.replace_record(record, **changes)
                        terminal = self.terminal(replacement, state)
                        self.assertFalse(peer_steward.peer_obligations._notice_duty_matches(
                            duty, terminal, self.tmp_root, "terminal-A", record["delivery_id"], self.jobs_path))
                        self.resume([duty])
                        self.assertEqual(self.store.get(duty["id"]), duty)
                        self.assertEqual(pending.read(self.tmp_root, "terminal-A", record["delivery_id"]), terminal)

    def test_canonical_route_and_stored_lineage_must_match_before_terminal_settlement(self):
        import session_notice
        for field in ("route_id", "lineage"):
            for state in ("acked", "rejected", "expired"):
                with self.subTest(field=field, state=state):
                    record, duty = self.notice("run-notice", field + state)
                    changes = {field: "rt-foreign"} if field == "route_id" else {field: [
                        session_notice._stored_lineage(self.jobs_path, {"kind": "owner-session",
                            "harness": "claude", "session_id": "terminal-A", "attempt_id": "att-foreign"})]}
                    replacement = self.terminal(self.replace_record(record, **changes), state)
                    self.assertFalse(peer_steward.peer_obligations._notice_duty_matches(
                        duty, replacement, self.tmp_root, "terminal-A", record["delivery_id"], self.jobs_path))
                    self.resume([duty])
                    self.assertEqual(self.store.get(duty["id"]), duty)

    def test_legacy_route_compatibility_does_not_require_canonical_lineage_fields(self):
        record, duty = self.notice("session-notice", "legacy-route")
        self.assertNotIn("lineage", duty["intent"]["identity"])
        self.assertNotIn("route_id", duty["intent"])
        terminal = self.terminal(self.replace_record(record, route_id="rt-legacy-compatible"), "acked")
        self.assertTrue(peer_steward.peer_obligations._notice_duty_matches(
            duty, terminal, self.tmp_root, "terminal-A", record["delivery_id"], self.jobs_path))
        self.resume([duty])
        self.assertEqual(self.store.get(duty["id"])["state"], "complete")

    def test_missing_or_unreadable_terminal_record_keeps_duty_until_original_evidence_returns(self):
        import dispatch_pending_delivery as pending
        for kind in ("run-notice", "session-notice"):
            for damage in ("missing", "json", "directory"):
                with self.subTest(kind=kind, damage=damage):
                    record, duty = self.notice(kind, kind + damage)
                    self.terminal(record, "acked")
                    path = pending.record_path(self.tmp_root, "terminal-A", record["delivery_id"])
                    original = path.read_bytes()
                    path.unlink()
                    if damage == "json":
                        path.write_text("{")
                    elif damage == "directory":
                        path.mkdir()
                    self.resume([duty])
                    self.assertEqual(self.store.get(duty["id"]), duty)
                    if damage == "directory":
                        path.rmdir()  # Only this fixture's non-file read error.
                    path.write_bytes(original)
                    self.resume([duty])
                    self.assertEqual(self.store.get(duty["id"])["state"], "complete")

    def test_terminal_settlement_uses_original_lineage_and_nondefault_registry_filename(self):
        import session_notice
        jobs = self.tmp_root / "original-notices.tsv"
        jobs.touch()
        lineage = {"kind": "attempt-parent", "harness": "claude", "session_id": "terminal-A",
                   "attempt_id": "att-original-parent"}
        record, duty = self.notice("run-notice", "stored-origin", jobs=jobs, lineage=lineage)
        identity = copy.deepcopy(duty["intent"]["identity"])
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.jobs_path)}):
            retry = session_notice.notify("claude", "terminal-A", key="stored-origin",
                subject="terminal notice", text="original result", jobs=jobs)
            self.assertEqual(retry["delivery_id"], record["delivery_id"])
            self.assertEqual(self.store.get(duty["id"])["intent"]["identity"], identity)
            self.terminal(record, "acked")
            jobs.unlink()  # A completed original needs no current registry row.
            self.resume([duty])
        self.assertEqual(identity["jobs"], str(jobs))
        self.assertEqual(identity["lineage"], lineage)
        self.assertEqual(self.store.get(duty["id"])["state"], "complete")
        self.assertEqual(self.store.get(duty["id"])["intent"], duty["intent"])

    def test_original_and_foreign_terminals_do_not_block_valid_group_siblings(self):
        import dispatch_pending_delivery as pending
        terminals, foreign, open_records = [], [], []
        for kind in ("run-notice", "session-notice"):
            record, duty = self.notice(kind, kind + "original-terminal")
            self.terminal(record, "acked")
            terminals.append(duty)
            record, duty = self.notice(kind, kind + "foreign-terminal")
            receipt = {**record["receipt"], "text": "foreign result"}
            self.terminal(self.replace_record(record, receipt=receipt,
                receipt_digest=pending._canonical_receipt_digest(receipt)), "acked")
            foreign.append(duty)
            for index in range(2):
                open_records.append(self.notice(kind, kind + "open-" + str(index)))
        duties = [*foreign, *terminals, *(d for _record, d in open_records)]
        with self.transport("terminal-A") as native:
            self.resume(duties)
        native.assert_called_once()
        self.assertTrue(all(self.store.get(d["id"])["state"] == "complete" for d in terminals))
        self.assertEqual([self.store.get(d["id"]) for d in foreign], foreign)
        refs = {self.store.get(d["id"])["observation"]["transfer_ref"] for _r, d in open_records}
        self.assertEqual(len(refs), 1)
        transfer = peer_steward.peer_message._read_pending(refs.pop())
        self.assertEqual(transfer["text"].count("HEARTING_RECEIPT_V1 "), 4)
        self.assertEqual({r["delivery_id"] for r, _d in open_records},
                         {item["delivery_id"] for item in transfer["producer_binding"]["records"]})
        self.assertEqual(peer_steward.peer_message.receive_peer_message(transfer["text"],
            {"harness": "claude", "session_id": "terminal-A"}), 0)
        with mock.patch.object(peer_steward, "cmd_prompt") as send:
            self.resume([self.store.get(d["id"]) for d in duties])
        send.assert_not_called()
        self.assertEqual([self.store.get(d["id"]) for d in foreign], foreign)
        self.assertTrue(all(self.store.get(d["id"])["state"] == "complete" for _r, d in open_records))

    def test_concurrent_terminal_duty_is_preserved_at_the_store_update_lock(self):
        for kind in ("run-notice", "session-notice"):
            for state in ("complete", "cancelled"):
                with self.subTest(kind=kind, state=state):
                    record, duty = self.notice(kind, kind + state)
                    self.terminal(record, "acked")
                    original_lock = self.store._lock
                    settled = []
                    def lock(duty_id):
                        other = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
                        settled.append(other.update(duty_id, state=state, delivery="already-settled",
                            cleanup="complete", observation={"transfer_ref": "f" * 32}))
                        return original_lock(duty_id)
                    with mock.patch.object(self.store, "_lock", side_effect=lock):
                        self.resume([duty])
                    self.assertEqual(self.store.get(duty["id"]), settled[0])


class SessionNoticeProgressRaceTest(_TmpRootMixin, unittest.TestCase):
    """Notice progress never replaces a duty settled by another store writer."""
    notice = SessionNoticeTerminalIdentityTest.notice
    replace_record = SessionNoticeTerminalIdentityTest.replace_record
    resume = SessionNoticeTerminalIdentityTest.resume
    transport = SessionNoticeTerminalIdentityTest.transport
    edge = SessionNoticeIntegrationCorrectionTest.edge

    def setUp(self):
        import dispatch_contract
        for module, name, value in (
                (peer_steward.peer_obligations, "_proc_start_for_store", "100001"),
                (peer_steward.peer_obligations, "request_dispatch_reconciliation", False),
                (dispatch_contract, "process_start_ticks", "100001"),
                (dispatch_contract, "process_identity_disposition", "live")):
            patch = patch_split(module, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        super().setUp()
        self.store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)

    def settle(self, duty, terminal):
        other = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
        record = other.update(duty["id"], state=terminal,
            delivery="already-settled" if terminal else "peer-courier",
            cleanup="complete" if terminal else "pending",
            observation={"phase": "settled" if terminal else "waiting", "transfer_ref": "f" * 32,
                "recipient_sid": "latest-recipient",
                "transfer_bindings": {"f" * 32: {"checkpoint": "latest"}},
                "exact_receiver_receipt": {"checkpoint": "terminal"}})
        path = other._record_path(duty["id"])
        return record, path.read_bytes(), path.stat().st_mtime_ns

    def assert_preserved(self, duty, snapshots):
        self.assertEqual(len(snapshots), 1)
        record, raw, mtime = snapshots[0]
        self.assertEqual(self.store.get(duty["id"]), record)
        path = self.store._record_path(duty["id"])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (raw, mtime))

    @contextlib.contextmanager
    def settle_before_lock(self, duty, terminal, predicate):
        cls = peer_steward.peer_obligations.ObligationStore
        update = cls.update
        snapshots = []
        active = False
        def progress(store, duty_id, **kwargs):
            nonlocal active
            if active or duty_id != duty["id"] or snapshots or not predicate(kwargs):
                return update(store, duty_id, **kwargs)
            original_lock = store._lock
            def lock(key):
                # Use the real second store and existing lock before letting the
                # paused progress writer acquire that same duty lock.
                snapshots.append(self.settle(duty, terminal))
                return original_lock(key)
            active = True
            try:
                with mock.patch.object(store, "_lock", side_effect=lock):
                    return update(store, duty_id, **kwargs)
            finally:
                active = False
        with mock.patch.object(cls, "update", progress):
            yield snapshots

    def test_exact_receipt_preserves_terminal_bytes_and_still_acks_original_record(self):
        import dispatch_pending_delivery as pending
        for kind in ("run-notice", "session-notice"):
            for terminal in ("complete", "cancelled"):
                with self.subTest(kind=kind, terminal=terminal):
                    record, duty = self.notice(kind, "receipt-" + kind + terminal)
                    with self.transport("terminal-A"):
                        self.resume([duty])
                    current = self.store.get(duty["id"])
                    ref = current["observation"]["transfer_ref"]
                    transfer = peer_steward.peer_message._read_pending(ref)
                    with self.settle_before_lock(duty, terminal,
                            lambda kw: kw.get("delivery") == "acknowledged") as snapshots:
                        self.assertEqual(peer_steward.peer_message.receive_peer_message(
                            transfer["text"], {"harness": "claude", "session_id": "terminal-A"}), 0)
                    self.assert_preserved(duty, snapshots)
                    acked = pending.read(self.tmp_root, "terminal-A", record["delivery_id"])
                    self.assertEqual(acked["state"], "acked")
                    self.assertEqual(pending.received_ack_identity(acked, "terminal-A", record["delivery_id"]),
                                     pending.received_ack_identity(record, "terminal-A", record["delivery_id"]))
                    with mock.patch.object(peer_steward, "cmd_prompt") as send:
                        self.resume([current])
                    send.assert_not_called()
                    self.assert_preserved(duty, snapshots)

    def test_observation_only_preparation_and_finalizer_preserve_terminal_bytes(self):
        for kind in ("run-notice", "session-notice"):
            for terminal in ("complete", "cancelled"):
                for boundary in ("prepared", "finalizer"):
                    with self.subTest(kind=kind, terminal=terminal, boundary=boundary):
                        _record, duty = self.notice(kind, boundary + kind + terminal)
                        predicate = (lambda kw: "transfer_ref" in (kw.get("observation") or {})) if boundary == "prepared" else (
                            lambda kw: kw.get("delivery") == "peer-courier")
                        with self.settle_before_lock(duty, terminal, predicate) as snapshots, \
                             self.transport("terminal-A"):
                            self.resume([duty])
                        self.assert_preserved(duty, snapshots)

    def test_native_callback_completion_and_cancellation_survive_finalizer(self):
        for kind in ("run-notice", "session-notice"):
            for terminal in ("complete", "cancelled"):
                with self.subTest(kind=kind, terminal=terminal):
                    _record, duty = self.notice(kind, "native-" + kind + terminal)
                    snapshots = []
                    def submit(*args, **kwargs):
                        snapshots.append(self.settle(duty, terminal))
                    with self.transport("terminal-A") as native:
                        native.side_effect = submit
                        self.resume([duty])
                    native.assert_called_once()
                    self.assert_preserved(duty, snapshots)

    def test_unavailable_lineage_progress_preserves_terminal_bytes(self):
        notices = [(kind, terminal, *self.notice(kind, "lineage-" + kind + terminal))
                   for kind in ("run-notice", "session-notice")
                   for terminal in ("complete", "cancelled")]
        # Legacy retention needs an addressable original. The lineage becomes
        # ambiguous after retention, before this courier pass observes it.
        self.edge("claude", "terminal-A", "fork-B")
        self.edge("claude", "terminal-A", "fork-C")
        for kind, terminal, record, duty in notices:
            with self.subTest(kind=kind, terminal=terminal):
                from dispatch_seat_handover import record_recipient_binding
                self.assertIsNone(record_recipient_binding(record, "terminal-A", harness="claude",
                                                          jobs=self.jobs_path))
                with self.settle_before_lock(duty, terminal, lambda kw:
                        (kw.get("observation") or {}).get("reason") == "recipient-lineage-unavailable") as snapshots, \
                     mock.patch.object(peer_steward, "cmd_prompt") as send:
                    self.resume([duty])
                send.assert_not_called()
                self.assert_preserved(duty, snapshots)

    def test_native_opencode_owned_progress_preserves_terminal_bytes(self):
        for terminal in ("complete", "cancelled"):
            with self.subTest(terminal=terminal):
                _record, duty = self.notice("run-notice", "opencode-" + terminal, harness="opencode")
                with self.settle_before_lock(duty, terminal, lambda kw:
                        (kw.get("observation") or {}).get("reason") == "native-opencode-carrier-owns-notice") as snapshots, \
                     mock.patch.object(peer_steward, "cmd_prompt") as send:
                    self.resume([duty])
                send.assert_not_called()
                self.assert_preserved(duty, snapshots)
        _record, duty = self.notice("run-notice", "opencode-pending", harness="opencode")
        with mock.patch.object(peer_steward, "cmd_prompt") as send:
            self.resume([duty])
        send.assert_not_called()
        current = self.store.get(duty["id"])
        self.assertEqual(current["state"], "pending")
        self.assertEqual(current["observation"]["reason"], "native-opencode-carrier-owns-notice")

    def test_unbound_received_history_progress_preserves_terminal_bytes(self):
        import dispatch_pending_delivery as pending
        for kind in ("run-notice", "session-notice"):
            for terminal in ("complete", "cancelled"):
                with self.subTest(kind=kind, terminal=terminal):
                    foreign, foreign_duty = self.notice(kind, "foreign-history-" + kind + terminal)
                    with self.transport("terminal-A"):
                        self.resume([foreign_duty])
                    ref = self.store.get(foreign_duty["id"])["observation"]["transfer_ref"]
                    peer = peer_steward.peer_message
                    self.assertEqual(peer.receive_peer_message(peer._read_pending(ref)["text"],
                        {"harness": "claude", "session_id": "terminal-A"}), 0)
                    receipt = {**foreign["receipt"], "text": "replacement history"}
                    self.replace_record(foreign, receipt=receipt,
                        receipt_digest=pending._canonical_receipt_digest(receipt))
                    self.assertFalse(peer_steward.peer_obligations.acknowledge_session_notice_delivery(
                        peer._read_pending(ref), roots=[self.tmp_root]))
                    _record, duty = self.notice(kind, "unbound-" + kind + terminal)
                    duty = self.store.update(duty["id"], observation={"transfer_ref": ref})
                    with self.settle_before_lock(duty, terminal, lambda kw:
                            (kw.get("observation") or {}).get("reason") == "received-notice-history-unbound") as snapshots, \
                         mock.patch.object(peer_steward, "cmd_prompt") as send:
                        self.resume([duty])
                    send.assert_not_called()
                    self.assert_preserved(duty, snapshots)

    def test_availability_observations_keep_terminal_and_latest_pending_checkpoint(self):
        for kind in ("run-notice", "session-notice"):
            for terminal in ("complete", "cancelled", None):
                for presence in ("absent", "unknown"):
                    with self.subTest(kind=kind, terminal=terminal, presence=presence):
                        import peer_address
                        import session_shell
                        _record, duty = self.notice(kind, "availability-" + kind + str(terminal) + presence)
                        boundary = self.settle_before_lock(duty, terminal,
                            lambda kw: kw.get("observer_error") == "recipient-unavailable")
                        with boundary as snapshots, \
                             mock.patch.object(session_shell, "exact_binding",
                                               return_value=(None, "attachment-missing")), \
                             mock.patch.object(peer_address, "presence", return_value=presence), \
                             mock.patch.object(peer_steward, "cmd_prompt") as send:
                            self.resume([duty])
                        send.assert_not_called()
                        if terminal:
                            self.assert_preserved(duty, snapshots)
                        else:
                            self.assertEqual(len(snapshots), 1)
                            current = self.store.get(duty["id"])
                            self.assertEqual((current["state"], current["delivery"], current["cleanup"]),
                                             ("pending", "peer-courier", "pending"))
                            self.assertEqual(current["observation"],
                                {**snapshots[0][0]["observation"], "reason": "recipient-unavailable"})


class ExplicitHolderAuthorityTest(_TmpRootMixin, unittest.TestCase):
    edge = SessionNoticeIntegrationCorrectionTest.edge

    def handoff(self, tag, harness="claude", holder_harness=None):
        import dispatch_seat_handover as handover
        import session_tidy as tidy
        holder_harness = holder_harness or harness
        old, current, holder = (tag + "-" + value for value in ("A", "B", "Z"))
        meta = dict(attempt_id="att-" + hashlib.sha256(tag.encode()).hexdigest()[:32],
            parent_sid=old, parent_harness=harness, dispatch_depth="1", worker_type="frame",
            route_id="rt-" + hashlib.sha256(tag.encode()).hexdigest()[:16],
            route_hash="sha256:" + "c" * 64, route_node="execute")
        with self.jobs_path.open("a") as stream:
            stream.write("now\trunning\t/w\t/w\ttask\t" +
                         ",".join(k + "=" + v for k, v in meta.items()) + "\n")
        self.edge(harness, old, current)
        self.assertTrue(handover.owns(meta, current, self.jobs_path))
        seat = tidy.Seat("pane", tag, pane="w1:pAway", harness=holder_harness)
        with tidy.seat_lock(seat.key):
            tidy._append_ledger_entry(seat, dict(event="handover", source="retire",
                harness=holder_harness, sid=holder, **{"from": current}, ts=2,
                bindings=[handover.binding_of(self.jobs_path, meta)]))
        return meta, {"attempt_ids": [meta["attempt_id"]]}, seat

    def ambiguity(self, harness, holder, kind):
        import session_tidy as tidy
        edges = [(holder, holder + "-C")]
        edges.append((holder, holder + "-D") if kind == "fork" else
                     (holder + "-D", holder + "-C") if kind == "merge" else
                     (holder + "-C", holder))
        seat = tidy.Seat("pane", holder + "-ambiguity", pane="w1:pFork", harness=harness)
        with tidy.seat_lock(seat.key):
            for stamp, (origin, target) in enumerate(edges, 3):
                tidy._append_ledger_entry(seat, dict(event="succession", source="tidy-clear",
                    harness=harness, sid=target, **{"from": {"harness": harness, "sid": origin}}, ts=stamp))

    def test_explicit_holder_ambiguity_never_revives_delivery_or_continuation_authority(self):
        import dispatch_seat_handover as handover
        import route_authority
        for harness, recipient_harness in (("claude", "claude"), ("codex", "codex"),
                                            ("opencode", "opencode"), ("claude", "codex")):
            for kind in ("fork", "merge", "cycle"):
                with self.subTest(harness=harness, recipient_harness=recipient_harness, kind=kind):
                    tag = harness + recipient_harness + kind
                    meta, record, seat = self.handoff(tag, harness, recipient_harness)
                    old, current, holder = tag + "-A", tag + "-B", tag + "-Z"
                    self.assertEqual(handover.record_recipient(record, old, harness=harness), holder)
                    self.assertEqual(handover.effective_parent_harness(meta, self.jobs_path), recipient_harness)
                    self.ambiguity(recipient_harness, holder, kind)
                    self.assertIsNone(handover.record_recipient(record, old, harness=harness))
                    self.assertEqual(handover.effective_parent(meta, self.jobs_path), "")
                    for actor in (old, current, holder, holder + "-C", holder + "-D"):
                        self.assertFalse(handover.record_for_session(record, None, session=actor,
                            harness=recipient_harness, storage_key=old))
                        self.assertFalse(handover.owns(meta, actor, self.jobs_path))
                        decision = route_authority.decide_continuation(action="handover",
                            jobs=self.jobs_path, metadata=meta, status="running",
                            facts={"eligible": True, "actor": actor})
                        self.assertFalse(decision.allowed)
                        self.assertEqual(decision.reason, "handover-unreadable")

    def test_valid_explicit_holder_successor_and_mixed_aggregate_use_selected_binding(self):
        import dispatch_seat_handover as handover
        import session_tidy as tidy
        first, record, _ = self.handoff("valid")
        self.edge("claude", "valid-Z", "valid-Y")
        self.assertEqual(handover.record_recipient(record, "valid-A", harness="claude"), "valid-Y")
        self.assertEqual(handover.effective_parent(first, self.jobs_path), "valid-Y")
        # A fork on the departed original chain must not undo an explicit handoff.
        self.edge("claude", "valid-A", "valid-elsewhere")
        self.assertEqual(handover.record_recipient(record, "valid-A", harness="claude"), "valid-Y")
        second = {**first, "attempt_id": "att-" + "d" * 32,
                  "route_id": "rt-" + "e" * 16}
        with self.jobs_path.open("a") as stream:
            stream.write("now\trunning\t/w\t/w\ttask\t" +
                         ",".join(k + "=" + v for k, v in second.items()) + "\n")
        seat = tidy.Seat("pane", "other-explicit", pane="w1:pOther", harness="claude")
        with tidy.seat_lock(seat.key):
            tidy._append_ledger_entry(seat, dict(event="handover", source="retire",
                harness="claude", sid="valid-R", **{"from": "valid-B"}, ts=3,
                bindings=[handover.binding_of(self.jobs_path, second)]))
        self.assertEqual(handover.record_recipient(
            {"attempt_ids": [second["attempt_id"]]}, "valid-A", harness="claude"), "valid-R")
        # Two individually valid explicit recipients cannot receive an aggregate.
        self.assertIsNone(handover.record_recipient(
            {"attempt_ids": [first["attempt_id"], second["attempt_id"]]}, "valid-A", harness="claude"))

    def test_ambiguous_explicit_holder_blocks_original_key_sweep_and_fleet_is_unknown(self):
        import dispatch_pending_delivery as pending
        import dispatch_session_sweep as sweep
        import dispatch_seat_handover as handover
        from fleet.collectors import dispatch
        meta, record, seat = self.handoff("sweep")
        parent_attempt = "att-" + "b" * 32
        receipt = {"schema_version": 2, "state": "delivered", "parent_attempt_id": parent_attempt,
            "job_registry": str(self.jobs_path), "children": [{"attempt_id": meta["attempt_id"],
                "status": "done", "readiness": "ready", "required_action": "inspect-done-failure",
                "reason": "terminal-failure-or-unclosed", "harness": "claude"}]}
        stored = pending.create(root=self.tmp_root, recipient_kind="claude-parent-runtime",
            recipient_key="sweep-A", delivery_id="delivery-" + "a" * 32,
            session_generation="", session_generation_supported="0",
            attempt_ids=record["attempt_ids"], parent_attempt_id=parent_attempt, route_id=meta["route_id"],
            route_node="execute", receipt=receipt,
            receipt_digest=pending._canonical_receipt_digest(receipt),
            row_revisions={meta["attempt_id"]: "original"})
        self.ambiguity("claude", "sweep-Z", "fork")
        path = pending.record_path(self.tmp_root, "sweep-A", stored["delivery_id"])
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with mock.patch.object(handover, "pane_seat", return_value=seat):
            self.assertEqual(sweep.sweep_deliver(self.tmp_root, "claude-parent-runtime", "sweep-A")[0], [])
            self.assertEqual(sweep.sweep_deliver(self.tmp_root, "claude-parent-runtime", "sweep-Z")[0], [])
        counts = dispatch._pending_delivery_counts([str(self.jobs_path)], session_observation={
            "complete": True, "herdr_complete": True, "process_complete": True,
            "native_sid_complete": True, "live": set()})
        self.assertEqual(counts["pending"], 1)
        self.assertIsNone(counts["no_recipient"])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_codex_targets_keep_unresolved_and_cross_harness_handoffs_unaddressed(self):
        spec = importlib.util.spec_from_file_location("explicit_codex_target",
            _HERE / "codex-managed-completion.py")
        completion = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = completion
        spec.loader.exec_module(completion)
        for recipient_harness in ("codex", "claude"):
            with self.subTest(recipient_harness=recipient_harness):
                tag = "queue-" + recipient_harness
                meta, record, _ = self.handoff(tag, "codex", recipient_harness)
                old = tag + "-A"
                attempts = set(record["attempt_ids"])
                rows = [SimpleNamespace(metadata=meta, attempt_id=meta["attempt_id"])]
                expected = tag + "-Z" if recipient_harness == "codex" else ""
                self.assertEqual(completion.queue_target(self.jobs_path, attempts, old), expected)
                self.assertEqual(completion.queue_target(self.jobs_path, attempts, old,
                    record=record, storage_key=old), expected)
                self.assertEqual(completion._queue_target_from_rows(self.jobs_path, rows, old), expected)
                self.ambiguity(recipient_harness, tag + "-Z", "fork")
                self.assertEqual(completion.queue_target(self.jobs_path, attempts, old), "")
                self.assertEqual(completion.queue_target(self.jobs_path, attempts, old,
                    record=record, storage_key=old), "")
                self.assertEqual(completion._queue_target_from_rows(self.jobs_path, rows, old), "")
                args = SimpleNamespace(jobs=self.jobs_path, thread_id=old,
                                       queue_socket=self.tmp_root / "fake-native.sock")
                with mock.patch.object(completion.codex_queue_delivery, "_rpc") as native:
                    with self.assertRaisesRegex(completion.CompletionError, "native-queue-recipient-unavailable"):
                        completion._native_queue_delivery_once(args, {}, attempts, initial_rows=rows)
                native.assert_not_called()


class SessionNoticeCheckpointRecoveryTest(_TmpRootMixin, unittest.TestCase):
    notice = SessionNoticeIntegrationCorrectionTest.notice
    edge = SessionNoticeIntegrationCorrectionTest.edge
    store = SessionNoticeIntegrationCorrectionTest.store
    duties = SessionNoticeIntegrationCorrectionTest.duties
    transport = SessionNoticeIntegrationCorrectionTest.transport

    def interrupted_preparation(self, old, current):
        import dispatch_pending_delivery as pending
        notice = self.notice(sid=old, key=old)
        self.edge("claude", old, current)
        duties = lambda: [d for d in self.duties()
                          if d["intent"]["identity"]["storage_sid"] == old]
        import session_shell
        with self.transport(current) as native, \
             mock.patch.object(session_shell, "deliver_pending",
                               side_effect=OSError("interrupted after on_prepared")):
            peer_steward._reconcile_obligations(self.store(), duties())
        native.assert_not_called()
        duty = duties()[0]
        ref = duty["observation"]["transfer_ref"]
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "pending")
        self.assertEqual(pending.read(self.tmp_root, old, notice["delivery_id"])["state"],
                         "sent-ambiguous")
        self.assertEqual(self.store().list(kinds={"message"}), [])
        return notice, duty, ref

    def test_prepared_checkpoint_survives_each_ordinary_absence_and_exact_recovery(self):
        import dispatch_pending_delivery as pending
        import peer_address
        import session_shell
        absences = (("attachment-missing", "absent"), ("attachment-missing", "unknown"),
                    ("attachment-ambiguous", "unknown"))
        for index, (attachment, presence) in enumerate(absences):
            with self.subTest(attachment=attachment, presence=presence):
                old, current = "absent-A-" + str(index), "absent-B-" + str(index)
                notice, duty, ref = self.interrupted_preparation(old, current)
                before = pending.read(self.tmp_root, old, notice["delivery_id"])
                with mock.patch.object(session_shell, "exact_binding",
                                       return_value=(None, attachment)), \
                     mock.patch.object(peer_address, "presence", return_value=presence):
                    peer_steward._reconcile_obligations(self.store(), [duty])
                refreshed = self.store().get(duty["id"])
                self.assertEqual(refreshed["observation"],
                                 {**duty["observation"], "reason": "recipient-unavailable"})
                self.assertEqual(pending.read(self.tmp_root, old, notice["delivery_id"]), before)
                # Restoring the same recipient schedules the SAME immutable transfer.
                with self.transport(current) as native, \
                     mock.patch.object(peer_steward, "_schedule_message_obligation",
                                       wraps=peer_steward._schedule_message_obligation) as schedule:
                    peer_steward._resume_session_notice_obligations([refreshed], self.store())
                native.assert_not_called()
                self.assertEqual(schedule.call_args.args[0]["ref"], ref)
                transfer = peer_steward.peer_message._read_pending(ref)
                # The shell host's existing first-write boundary works after the original lease.
                with mock.patch.object(pending.time, "monotonic_ns",
                                       return_value=before["claim_deadline_ns"] + 1), \
                     self.transport(current) as native:
                    result = session_shell.deliver_pending(ref, "claude", current)
                    self.assertTrue(result["ok"], result)
                    again = session_shell.deliver_pending(ref, "claude", current)
                    self.assertFalse(again["ok"])
                native.assert_called_once()
                renewed = pending.read(self.tmp_root, old, notice["delivery_id"])
                self.assertGreater(renewed["claim_deadline_ns"], before["claim_deadline_ns"] + 1)
                self.assertEqual({**renewed, "claim_deadline_ns": before["claim_deadline_ns"]}, before)
                peer_steward.peer_message.receive_peer_message(
                    transfer["text"], {"harness": "claude", "session_id": current})
                self.assertEqual(pending.read(self.tmp_root, old, notice["delivery_id"])["state"], "acked")
                self.assertEqual(self.store().get(duty["id"])["state"], "complete")
                self.assertIsNone(pending.read(self.tmp_root, current, notice["delivery_id"]))

    def test_absence_merges_fresh_checkpoint_under_store_lock(self):
        # Pause exactly before update's existing lock. A stale courier pass must
        # read the checkpoint written by the cooperating writer after that pause.
        for terminal in (None, "complete", "cancelled"):
            with self.subTest(terminal=terminal):
                old = "race-" + str(terminal)
                notice = self.notice(sid=old, key=old)
                store = self.store()
                duty = next(d for d in self.duties() if d["intent"]["identity"]["storage_sid"] == old)
                entered, changed = threading.Event(), threading.Event()
                original_lock = store._lock
                latest_observation = {"transfer_ref": "f" * 32, "recipient_sid": "new-current",
                    "storage_sid": old, "producer_binding": {"recipient": "fresh"},
                    "exact_receiver_receipt": {"proof": "fresh"}}
                errors = []
                def writer():
                    try:
                        self.assertTrue(entered.wait(3))
                        self.store().update(duty["id"], observation=latest_observation,
                                            state=terminal, delivery="acknowledged" if terminal else None,
                                            cleanup="complete" if terminal else None)
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        changed.set()
                def lock(duty_id):
                    entered.set()
                    self.assertTrue(changed.wait(3))
                    return original_lock(duty_id)
                thread = threading.Thread(target=writer)
                thread.start()
                try:
                    with mock.patch.object(store, "_lock", side_effect=lock), \
                         mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                             returncode=0, stdout='{"result":{"agents":[]}}')):
                        peer_steward._resume_session_notice_obligations([duty], store)
                finally:
                    thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                after = self.store().get(duty["id"])
                self.assertEqual(after["state"], terminal or duty["state"])
                self.assertEqual(after["observation"], latest_observation if terminal else
                                 {**latest_observation, "reason": "recipient-unavailable"})
                if terminal:
                    self.assertEqual((after["state"], after["delivery"], after["cleanup"]),
                                     (terminal, "acknowledged", "complete"))

    def test_exact_receipt_settlement_during_absence_is_not_overwritten(self):
        import dispatch_pending_delivery as pending
        notice = self.notice()
        self.edge("claude", "A", "B")
        with self.transport():
            peer_steward._resume_session_notice_obligations(self.duties(), self.store())
        stale = self.duties()[0]
        ref = stale["observation"]["transfer_ref"]
        transfer = peer_steward.peer_message._read_pending(ref)
        # Settle at the census boundary, after this pass inspected the open
        # notice and its transfer. Keep receipt processing itself real.
        settled = []
        read_pending = peer_steward.peer_message._read_pending
        inspected = []
        def read(*args, **kwargs):
            if not inspected:
                inspected.append(True)
                return {**transfer, "state": "pending"}
            return read_pending(*args, **kwargs)
        def census(*args, **kwargs):
            peer_steward.peer_message.receive_peer_message(
                transfer["text"], {"harness": "claude", "session_id": "B"})
            settled.append(self.store().get(stale["id"]))
            return SimpleNamespace(returncode=0, stdout='{"result":{"agents":[]}}')
        with mock.patch.object(peer_steward.peer_message, "_read_pending", side_effect=read), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=census):
            peer_steward._resume_session_notice_obligations([stale], self.store())
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]["state"], "complete")
        self.assertEqual(self.store().get(stale["id"]), settled[0])
        self.assertEqual(pending.read(self.tmp_root, "A", notice["delivery_id"])["state"], "acked")

    def test_missing_prepared_recipient_does_not_block_another_recipient(self):
        notice, duty, ref = self.interrupted_preparation("A", "B")
        self.notice(sid="Z", key="other-recipient")
        selection = {}
        with self.transport("Z") as native:
            for _ in range(2):
                peer_steward._reconcile_obligations(self.store(), self.duties(),
                    notice_selection=selection, runner_incarnation="fixture")
        native.assert_called_once()
        self.assertEqual(self.store().get(duty["id"])["observation"]["transfer_ref"], ref)

    def test_serial_group_advances_after_exact_settlement_and_keeps_partial_unknown(self):
        import dispatch_pending_delivery as pending
        for index in range(12):
            self.notice(sid="B", key="serial-" + str(index))
        with self.transport() as native:
            peer_steward._resume_session_notice_obligations(self.duties(), self.store())
            first = self.duties()
            prepared = [d for d in first if (d.get("observation") or {}).get("transfer_ref")]
            self.assertEqual(len(prepared), 8)
            self.assertEqual(len({d["observation"]["transfer_ref"] for d in prepared}), 1)
            ref = prepared[0]["observation"]["transfer_ref"]
            transfer = peer_steward.peer_message._read_pending(ref)
            # Partial body and an unknown/queued observation are not receipts.
            peer_steward.peer_message.receive_peer_message(
                transfer["text"][:len(transfer["text"]) // 2], {"harness": "claude", "session_id": "B"})
            for _ in range(3):
                peer_steward._resume_session_notice_obligations(self.duties(), self.store())
            self.assertEqual(native.call_count, 1)
            self.assertEqual(len(self.duties()), 12)
            self.assertTrue(all(pending.read(self.tmp_root, "B", d["intent"]["identity"]["delivery_id"])
                                ["state"] != "acked" for d in self.duties()))
            peer_steward.peer_message.receive_peer_message(
                transfer["text"], {"harness": "claude", "session_id": "B"})
            self.assertEqual(len(self.duties()), 4)
            peer_steward._resume_session_notice_obligations(self.duties(), self.store())
            self.assertEqual(native.call_count, 2)
            second_ref = self.duties()[0]["observation"]["transfer_ref"]
            self.assertNotEqual(second_ref, ref)
            second = peer_steward.peer_message._read_pending(second_ref)
            peer_steward.peer_message.receive_peer_message(
                second["text"], {"harness": "claude", "session_id": "B"})
            self.assertEqual(self.duties(), [])
            self.assertEqual(len(first), 12)
            self.assertTrue(all(pending.read(self.tmp_root, "B", d["intent"]["identity"]["delivery_id"])
                                ["state"] == "acked" for d in first))


class RegisteredUnknownRetryTest(_TmpRootMixin, unittest.TestCase):
    def test_unknown_notice_rechecks_new_successor_without_duty_change(self):
        import dispatch_pending_delivery as pending
        import session_notice
        import session_tidy as tidy

        cases = ((harness, producer_name, notify, kind, storage_field)
                 for harness in ("claude", "codex", "opencode")
                 for producer_name, notify, kind, storage_field in (
                     ("canonical", session_notice.notify, "run-notice", "storage_recipient"),
                     ("legacy", _legacy_notice, "session-notice", "storage_sid")))
        for harness, producer_name, notify, kind, storage_field in cases:
            with self.subTest(harness=harness, producer=producer_name):
                storage_sid = harness + "-" + producer_name + "-notice-storage"
                successor_sid = harness + "-" + producer_name + "-notice-successor"
                with mock.patch.object(peer_steward.peer_obligations,
                                       "ensure_normal_runner", return_value=False), \
                        mock.patch.object(peer_steward.peer_obligations,
                                          "request_dispatch_reconciliation", return_value=False):
                    notice = notify(
                        harness, storage_sid, key="unknown-lineage-" + harness + "-" + producer_name,
                        subject="run finished", text="keep the original storage key",
                        jobs=self.jobs_path)
                store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
                duty = next(row for row in store.list(kinds={kind})
                            if row["intent"]["identity"][storage_field] == storage_sid)
                self.assertEqual(duty["intent"]["kind"], kind)
                self.assertEqual(peer_steward.peer_obligations._notice_duty_source(
                    duty, self.tmp_root), (harness, storage_sid, notice["delivery_id"], self.jobs_path))
                duty = store.update(duty["id"], observer_error="recipient-unavailable")
                seat = tidy.Seat("pane", "unknown-notice-" + harness + "-" + producer_name,
                                 pane="w1:p-" + harness, harness=harness)
                inventory = {"result": {"agents": []}}
                deliveries = []

                real_courier = peer_steward.cmd_prompt

                def courier(args, *, expected_recipient, on_prepared, producer_binding):
                    deliveries.append((expected_recipient, producer_binding,
                                       Path(args.body_file).read_text()))
                    return real_courier(args, expected_recipient=expected_recipient,
                        on_prepared=on_prepared, producer_binding=producer_binding)

                def native_observation(argv, **kwargs):
                    if argv[-2:] == ["agent", "list"]:
                        payload = inventory
                    elif argv[-3:] == ["agent", "get", seat.pane]:
                        payload = {"result": {"agent": inventory["result"]["agents"][0]}}
                    else:
                        raise AssertionError("unprovided native observation: " + repr(argv))
                    return SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=0)

                def native_session(pane, observed_harness, info):
                    self.assertEqual((pane, observed_harness), (seat.pane, harness))
                    self.assertEqual(info["pane_id"], seat.pane)
                    return successor_sid, None

                # Keep both resolvers and the courier real. A native session
                # observation without an input-ready process leaves its real
                # prepared transfer queued; it cannot authorize a native write.
                retry_state = {}
                with mock.patch.object(peer_steward.time, "monotonic", return_value=100.0), \
                        mock.patch.object(peer_steward.subprocess, "run", side_effect=native_observation), \
                        patch_split(peer_steward, "_herdr_missing", return_value=False), \
                        mock.patch.object(peer_steward, "_retire_pane_info", return_value={
                            "pane_id": seat.pane, "foreground_processes": []}), \
                        mock.patch.object(peer_steward, "_prompt_process_session",
                                          side_effect=native_session), \
                        mock.patch.object(peer_steward, "_herdr_prompt",
                                          side_effect=AssertionError("native write must stay queued")), \
                        mock.patch.object(peer_steward.peer_obligations,
                                          "request_dispatch_reconciliation", return_value=False), \
                        mock.patch.object(peer_steward, "cmd_prompt", side_effect=courier):
                    peer_steward._reconcile_obligations(store, [duty], retry_state=retry_state)
                    self.assertEqual(deliveries, [])
                    # The first unavailable census writes its recipient into
                    # observation. Let the next pass see those same bytes too.
                    peer_steward._reconcile_obligations(
                        store, [store.get(duty["id"])], retry_state=retry_state)
                    duty_before = store.get(duty["id"])
                    fingerprint_before = peer_steward._obligation_retry_fingerprint(duty_before)
                    tidy._append_ledger_entry(seat, {
                        "event": "succession", "source": "tidy-clear", "harness": harness,
                        "from": {"harness": harness, "sid": storage_sid},
                        "sid": successor_sid, "ts": 1,
                    })
                    inventory["result"]["agents"] = [{
                        "agent": harness, "agent_session": {"value": successor_sid},
                        "pane_id": seat.pane, "agent_status": "working", "name": "fixture",
                    }]
                    self.assertEqual(store.get(duty["id"]), duty_before)
                    self.assertEqual(peer_steward._obligation_retry_fingerprint(
                        store.get(duty["id"])), fingerprint_before)
                    peer_steward._reconcile_obligations(
                        store, [store.get(duty["id"])], retry_state=retry_state)

                self.assertEqual(len(deliveries), 1,
                                 "new lineage must be observed before a stale unknown retry deadline")
                recipient, producer, body = deliveries[0]
                self.assertEqual(recipient, (harness, successor_sid))
                self.assertEqual(producer["recipient"], {
                    "harness": harness, "session_id": successor_sid})
                self.assertEqual(producer["records"][0]["storage_sid"], storage_sid)
                self.assertEqual(producer["records"][0]["delivery_id"], notice["delivery_id"])
                self.assertIn("keep the original storage key", body)
                self.assertIsNone(pending.read(self.tmp_root, successor_sid, notice["delivery_id"]))
                self.assertEqual(pending.read(self.tmp_root, storage_sid,
                    notice["delivery_id"])["state"], "sent-ambiguous",
                    "queue acceptance alone cannot ACK the notice")

    def test_unchanged_unknown_message_keeps_its_bounded_backoff(self):
        store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
        duty = store.create("message-unknown-backoff", "message",
                            {"harness": "codex", "session_id": "message-recipient"}, {})
        duty = store.update(duty["id"], observer_error="fixture-unavailable")
        retry_state = {}
        with mock.patch.object(peer_steward.time, "monotonic", return_value=100.0), \
                mock.patch.object(peer_steward, "_resume_message_obligation") as resume:
            peer_steward._reconcile_obligations(store, [duty], retry_state=retry_state)
            peer_steward._reconcile_obligations(store, [duty], retry_state=retry_state)
            self.assertEqual(resume.call_count, 1)
            self.assertEqual(retry_state[duty["id"]][1], 101.0)
        with mock.patch.object(peer_steward.time, "monotonic", return_value=101.0), \
                mock.patch.object(peer_steward, "_resume_message_obligation") as resume:
            peer_steward._reconcile_obligations(store, [duty], retry_state=retry_state)
            resume.assert_called_once()
            self.assertEqual(retry_state[duty["id"]][1], 103.0)

    def test_unknown_batch_rechecks_changed_exact_row_without_duty_change(self):
        from dispatch_completion_join import current_attempt_row

        attempt_id = "att-" + "a" * 32
        parent_sid = "registered-parent-session"

        def registry_row(status):
            metadata = ",".join((
                f"attempt_id={attempt_id}", "attempt_schema_version=2",
                f"parent_sid={parent_sid}", "parent_harness=codex",
                "parent_completion_delivery=codex-parent-runtime",
                "delivery_intent=1", "route_id=fixture-route",
            ))
            return (f"job\t{status}\t/repo\t2026-10-24T00:00:00Z"
                    f"\towner\t{metadata}\n")

        self.jobs_path.write_text(registry_row("open"), encoding="utf-8")
        identity = {"jobs": str(self.jobs_path), "attempt_id": attempt_id,
                    "harness": "codex", "server": "default"}
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.create("registered-unknown-retry", "registered-batch", identity,
                            {"carrier": "codex-parent-runtime"})
        duty = store.update(duty["id"], state="unknown",
                            observation={"reason": "source-row-unavailable"})
        duty_before = store.get(duty["id"])
        retry_state = {}
        observed_rows = []

        def observe_source(_duty, _store, *, read_context=None, settlement_context=None):
            row = current_attempt_row(self.jobs_path, attempt_id)
            observed_rows.append(row.raw if row is not None else None)

        with mock.patch.object(peer_steward, "_resume_registered_obligation",
                               side_effect=observe_source):
            peer_steward._reconcile_obligations(
                store, [duty], retry_state=retry_state)
            self.assertEqual(store.get(duty["id"]), duty_before)
            self.jobs_path.write_text(registry_row("running"), encoding="utf-8")
            peer_steward._reconcile_obligations(
                store, [store.get(duty["id"])], retry_state=retry_state)

        self.assertEqual(len(observed_rows), 2)
        self.assertNotEqual(observed_rows[0], observed_rows[1])
        self.assertIn("\topen\t", observed_rows[0])
        self.assertIn("\trunning\t", observed_rows[1])
        self.assertEqual(store.get(duty["id"]), duty_before)
        self.assertNotIn(duty["id"], retry_state)


class ClosedParentNativeHandoverTest(_TmpRootMixin, unittest.TestCase):
    """Real native readers, resolver, writer and consumers in private state."""

    PARENT, HEADLESS, SUCCESSOR = "ses_original", "ses_headless", "codex-successor"
    SUCCESSOR_HARNESS = "codex"
    PID, START = 99999991, "800"

    def setUp(self):
        super().setUp()
        import dispatch_seat_handover as handover
        import opencode_session_runtime as native
        import owner_route_binding as owner
        import session_tidy
        self.handover, self.native, self.tidy = handover, native, session_tidy
        self.route = owner.ROUTE.compile_route(
            "autopilot-code", "debug", "quick", self.tmp_root, self.tmp_root / "artifacts",
            predicates=[], transport=None, tracking="tracked",
            tracked_gate_evidence={"spec_read": {"satisfied": True, "source": "canonical-prd-sha256"},
                "drift_verdict": "within-spec", "workflow_mode": "tracked",
                "artifact_guard": {"satisfied": True, "source": "conductor-prechecked"}},
            registered_headless_evidence={"candidates": [
                {"harness": h, "transport": "headless", "surface": "registered-headless",
                 "status": "supported", "probe_source": "fixture", "probe_time": "2026-10-13T00:00:00Z"}
                for h in ("claude", "codex", "opencode")]})
        self.route_path = self.tmp_root / "original-route.json"
        self.route_path.write_text(json.dumps(self.route))
        self.meta = {"attempt_schema_version": "2", "dispatch_depth": "1", "worker_type": "owner",
            "registered_worker": "1", "execution_surface": "registered-headless", "transport": "headless",
            "attempt_id": "att-selected", "harness": "opencode", "parent_harness": "opencode",
            "parent_sid": self.PARENT, "route_node": "owner", "note": "dead-worker-fail",
            "failure_class": "fail", "owner_route_file": str(self.route_path),
            "owner_route_id": self.route["route_id"], "owner_route_hash": self.route["route_hash"]}
        self.rows = [("done", {**self.meta, "attempt_id": aid})
                     for aid in ("att-ancestor-one", "att-ancestor-two", "att-selected")]
        self.foreign = {**self.meta, "attempt_id": "att-foreign", "harness": "codex",
            "parent_sid": self.HEADLESS, "owner_route_id": "rt-foreign", "owner_route_hash": "sha256:foreign"}
        self.rows.append(("running", self.foreign))
        self.write_jobs()
        self.native_args = SimpleNamespace(jobs=self.jobs_path, worktree=str(self.tmp_root),
                                           parent_attempt_id="att-selected")
        native.bind_session(self.native_args, self.HEADLESS)
        self.binding_path = native.binding_path(self.native_args)
        self.native_path = Path(os.environ["HOME"]) / ".local/state/hearting/tui-identity" / f"{self.PID}-{self.START}.json"
        self.native_path.parent.mkdir(parents=True)
        self.native_value = {"schema": "hearting-tui-selection-v1", "pid": self.PID, "start": self.START,
            "sessionID": self.PARENT,
            "writtenAt": peer_steward.datetime.datetime.now(peer_steward.datetime.timezone.utc).isoformat()}
        self.native_path.write_text(json.dumps(self.native_value))
        self.native_path.chmod(0o664)
        self.identity = {"session_id": self.HEADLESS, "harness": "opencode", "pane": "w1:pOld",
            "server": "default", "name": "old", "foreground": {"pid": self.PID, "start": self.START,
                "argv": ["opencode"], "shell_pid": 99999992, "shell_start": "700"}}
        self.store = peer_steward.peer_obligations.ObligationStore()
        self.book(self.SUCCESSOR_HARNESS)
        self.seat = session_tidy._pane_seat_of("w1:pNew")
        # Existing route/runtime payloads survive replay byte for byte.
        for name in ("resource.json", "completed-prefix.json", "owner.log"):
            (self.tmp_root / name).write_text('{"completed":["plan"],"round_ceiling":1,"state":"retained"}')

    def write_jobs(self):
        self.jobs_path.write_text("".join("now\t" + status + "\t" + str(self.tmp_root) + "\t" +
            str(self.tmp_root) + "\towner.log\t" + ",".join(f"{k}={v}" for k, v in meta.items()) + "\n"
            for status, meta in self.rows))

    def book(self, harness, duty_id="retire-native-fixture"):
        peer_steward._mark_seat_successor("w1:pNew", "w1:pOld", harness, "")
        self.mark = peer_steward._seat_successor_path("w1:pNew")
        self.mark.chmod(0o664)
        self.requester = {"session_id": self.SUCCESSOR, "harness": harness,
                          "pane": "w1:pNew", "server": "default"}
        self.accept(duty_id)

    def accept(self, duty_id="retire-native-fixture"):
        # Native and mark creation precede real acceptance, which precedes completion.
        self.duty = self.store.create(duty_id, "retire", self.identity,
                                      {"target": "w1:pOld", "requester": self.requester})
        self.duty = self.store.update(self.duty["id"], state="complete", result="target-already-gone",
            delivery="completed", cleanup="complete", observation={"phase": "complete"})

    def protected(self):
        paths = [self.store._record_path(self.duty["id"]), self.jobs_path, self.route_path,
                 *(self.tmp_root / name for name in ("resource.json", "completed-prefix.json", "owner.log"))]
        return {str(p): p.read_bytes() for p in paths}

    def authority_bytes(self):
        paths = (self.mark, self.tidy._ledger_path(self.seat), self.handover._snapshot_path(self.seat.key))
        return {str(p): p.read_bytes() if p.exists() else None for p in paths}

    def replay(self, duty=None):
        with mock.patch.object(peer_steward.subprocess, "run", side_effect=AssertionError("external transport")), \
             mock.patch.object(peer_steward, "_close_pane", side_effect=AssertionError("pane close")), \
             mock.patch.object(peer_steward, "_caller_pane", return_value="w9:pObserver"):
            return peer_steward._resume_retire_obligation(duty or self.duty, self.store)

    def assert_refused(self, duty=None):
        before, authority = self.protected(), self.authority_bytes()
        self.replay(duty)
        self.assertEqual(self.protected(), before)
        self.assertEqual(self.authority_bytes(), authority)
        self.assertEqual(self.handover.effective_parent(self.meta, self.jobs_path), self.PARENT)
        self.assertEqual(self.handover.effective_parent(self.foreign, self.jobs_path), self.HEADLESS)

    def test_real_recovery_preserves_history_and_hands_only_open_original_route_once(self):
        before = self.protected()
        self.replay()
        self.assertEqual(self.protected(), before)
        self.assertFalse(self.mark.exists())
        self.assertEqual(self.handover.effective_parent(self.meta, self.jobs_path), self.SUCCESSOR)
        self.assertEqual(self.handover.effective_parent_harness(self.meta, self.jobs_path), "codex")
        self.assertTrue(self.handover.owns(self.meta, self.SUCCESSOR, self.jobs_path))
        self.assertFalse(self.handover.owns(self.meta, self.PARENT, self.jobs_path))
        self.assertEqual(self.handover.effective_parent(self.foreign, self.jobs_path), self.HEADLESS)
        ledger = self.tidy._ledger_path(self.seat).read_bytes()
        row = json.loads(ledger)
        self.assertEqual({b["attempt"] for b in row["bindings"]}, {m["attempt_id"] for _, m in self.rows[:3]})
        snapshot_bindings = self.handover.read_snapshot(self.seat.key)["bindings"]
        self.assertEqual(len(snapshot_bindings), 3)
        expected = {(meta["attempt_id"], meta["parent_sid"]) for _, meta in self.rows[:3]}
        self.assertEqual({(binding["attempt"], binding["parent"]) for binding in snapshot_bindings}, expected)
        current = self.handover.current_bindings("opencode", self.SUCCESSOR, self.jobs_path)
        self.assertTrue(all(binding in current for binding in row["bindings"]))
        self.replay()
        self.assertEqual(self.tidy._ledger_path(self.seat).read_bytes(), ledger)
        self.assertEqual(self.protected(), before)

    def _completion_record(self, metadata, *, carrier, storage, route=None):
        """The same private record/route/registry shape used by the real courier."""
        import dispatch_pending_delivery as pending
        from route_identity import route_hash
        if route is not None:
            route["route_hash"] = route_hash(route)
            path = self.tmp_root / (route["route_id"] + ".json")
            path.write_text(json.dumps(route))
            metadata.update(route_id=route["route_id"], route_hash=route["route_hash"],
                            route_file=str(path), route_node=route["nodes"][0]["id"])
        aid, did = metadata["attempt_id"], metadata["delivery_id"]
        receipt = {"schema_version": 2, "state": "ready", "parent_attempt_id": "att-parent",
            "job_registry": str(self.jobs_path), "delivery_classification": "success",
            "children": [{"attempt_id": aid, "status": "done", "readiness": "ready",
                "reason": "terminal-complete", "required_action": "advance-completed",
                "harness": metadata["parent_harness"], "delivery_classification": "success"}]}
        record = pending.create(self.jobs_path.parent, recipient_kind=carrier, recipient_key=storage,
            delivery_id=did, session_generation="", session_generation_supported="0",
            attempt_ids=[aid], parent_attempt_id="att-parent", route_id=metadata["route_id"],
            route_node=metadata["route_node"], receipt=receipt,
            receipt_digest=pending._canonical_receipt_digest(receipt),
            row_revisions={aid: metadata["delivery_row_revision"]})
        metadata.update(delivery_receipt_digest=record["receipt_digest"],
            delivery_intent_at_ns=str(time.monotonic_ns()), failure_class="pass",
            note="completed-marker", launch_outcome="reaped-before-publish")
        return record

    def test_late_completion_transfer_follows_successor_once_and_keeps_old_transfer_immutable(self):
        import dispatch_pending_delivery as pending
        import dispatch_seat_handover as handover
        from harness_capabilities import parent_completion

        self.replay()  # Confirm the real A -> B seat handover before the completion arrives.
        carrier = parent_completion("opencode")["carrier"]
        metadata = {**self.meta, "parent_completion_delivery": carrier, "delivery_intent": "1",
                    "delivery_id": "delivery-late-completion", "delivery_row_revision": "revision-late",
                    "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                    "route_file": str(self.route_path)}
        completion_record = self._completion_record(metadata, carrier=carrier, storage=self.PARENT)
        self.rows[2] = ("done", metadata)
        self.write_jobs()
        identity = {"jobs": str(self.jobs_path), "attempt_id": "att-selected", "harness": "opencode",
                    "session_id": self.PARENT, "server": "default"}
        duty = self.store.create("registered-batch-" + "c" * 32, "registered-batch", identity,
                                 {"carrier": carrier})
        old_text, old_ref = peer_steward.peer_message.prepare_peer_message(
            "late completion payload", {"harness": "opencode", "session_id": "sender"},
            {"harness": "opencode", "session_id": self.PARENT}, defer=True, refs=[duty["id"]])
        old_binding = {"duty_id": duty["id"], "jobs": str(self.jobs_path), "attempt_id": "att-selected",
            "carrier": carrier, "recipient_sid": self.PARENT, "delivery_id": metadata["delivery_id"],
            "storage_recipient": self.PARENT, "claim_owner": "retained-rewake:" + duty["id"]}
        peer_steward.peer_message.bind_dispatch_notice(old_ref, old_binding)
        self.store.update(duty["id"], observation={"transfer_ref": old_ref,
            "recipient_sid": self.PARENT, "delivery_id": metadata["delivery_id"],
            "storage_recipient": self.PARENT, "claim_owner": old_binding["claim_owner"]})
        old_before = peer_steward.peer_message._read_pending(old_ref)
        old_seal = peer_steward.peer_message._transfer_path(old_ref).read_bytes()
        sends, producers = [], []

        def courier(args, expected_recipient, on_prepared, producer_binding=None):
            body = Path(args.body_file).read_text()
            sends.append((expected_recipient, body))
            producers.append(producer_binding)
            _text, ref = peer_steward.peer_message.prepare_peer_message(body,
                {"harness": "runtime", "session_id": None, "name": "Hearting runtime"},
                {"harness": expected_recipient[0], "session_id": expected_recipient[1]},
                defer=True, refs=args.ref, producer_binding=producer_binding)
            on_prepared(ref)  # Real producer binding and every dispatch notice are persisted.
            return 3

        agents = {"result": {"agents": [{"agent": "codex", "pane_id": "w1:pNew",
                    "agent_session": {"value": "codex-successor"}}]}}
        with mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                 stdout=json.dumps(agents), returncode=0)), \
             mock.patch.object(peer_steward, "_resolve_target", return_value=("codex", "codex-successor")), \
             mock.patch.object(peer_steward, "cmd_prompt", side_effect=courier):
            peer_steward._resume_registered_obligation(self.store.get(duty["id"]), self.store)
            after_first = self.store.get(duty["id"])
            new_ref = after_first["observation"]["transfer_ref"]
            self.assertNotEqual(new_ref, old_ref)
            new_transfer = peer_steward.peer_message._read_pending(new_ref)
            self.assertEqual(new_transfer["to"], {"harness": "codex", "session_id": "codex-successor"})
            self.assertEqual(len(sends), 1)
            self.assertEqual(producers[0]["recipient"], {"harness": "codex", "session_id": "codex-successor"})
            self.assertEqual(len(list(peer_steward.peer_obligations._registered_delivery_bindings(
                new_transfer))), 1)
            with mock.patch.object(peer_steward, "_schedule_message_obligation") as schedule:
                peer_steward._resume_registered_obligation(after_first, self.store)
                schedule.assert_called_once()
            self.assertEqual(len(sends), 1)
            self.assertEqual(peer_steward.peer_message._read_pending(old_ref), old_before)
            self.assertEqual(peer_steward.peer_message._transfer_path(old_ref).read_bytes(), old_seal)
            self.assertEqual((old_before["to"], old_before["text"], old_before["state"]),
                             ({"harness": "opencode", "session_id": self.PARENT}, old_text, "pending"))
            self.assertEqual(peer_steward.peer_message.receive_peer_message(
                new_transfer["text"], new_transfer["to"]), 0)
            self.assertEqual(pending.read(self.jobs_path.parent, self.PARENT,
                                         metadata["delivery_id"])["state"], "acked")
            with mock.patch.object(peer_steward, "_resume_native_receiving_turn", return_value=True), \
                 mock.patch.object(peer_steward, "cmd_prompt") as no_replay:
                peer_steward._resume_registered_obligation(self.store.get(duty["id"]), self.store)
                no_replay.assert_not_called()
            self.assertFalse(peer_steward.peer_obligations.acknowledge_registered_delivery(
                peer_steward.peer_message._read_pending(old_ref), roots=[self.jobs_path.parent]))
            self.assertEqual(handover.effective_parent(metadata, self.jobs_path), "codex-successor")

    def test_registered_peer_carrier_emits_one_24_member_transfer_with_all_receipts(self):
        import dispatch_pending_delivery as pending
        import dispatch_seat_handover as handover
        import dispatch_session_sweep as sweep

        carrier, recipient_sid = "codex-native-queue", "codex-batch-recipient"
        storage, jobs = "codex-storage-recipient", self.jobs_path
        duties, records, metas = [], {}, []
        for index in range(24):
            suffix = f"{index:032x}"
            attempt_id, delivery_id = "att-peer-batch-" + suffix, "delivery-peer-batch-" + suffix
            metadata = {"attempt_schema_version": "2", "dispatch_depth": "1", "registered_worker": "1",
                "worker_type": "owner", "attempt_id": attempt_id,
                "parent_sid": storage, "parent_harness": "codex",
                "parent_completion_delivery": carrier, "delivery_intent": "1", "delivery_id": delivery_id,
                "delivery_row_revision": "revision-" + suffix, "parent_attempt_id": "att-owner-batch"}
            route = {"route_id": "rt-peer-batch-" + suffix, "parent_cycle_id": "cycle-peer-batch",
                     "nodes": [{"id": "execute"}]}
            records[delivery_id] = self._completion_record(metadata, carrier=carrier, storage=storage,
                                                         route=route)
            metas.append(metadata)
            identity = {"jobs": str(jobs.resolve()), "attempt_id": attempt_id,
                "session_id": recipient_sid, "harness": "codex", "server": "default"}
            duty_id = peer_steward.peer_obligations.stable_duty_id("registered-batch", identity)
            duties.append(self.store.create(duty_id, "registered-batch", identity, {"carrier": carrier}))
        self.rows.extend(("done", meta) for meta in metas)
        self.write_jobs()
        self.tidy._append_ledger_entry(self.seat, {"event": "handover", "source": "retire",
            "harness": "codex", "sid": recipient_sid, "from": storage, "ts": 1,
            "bindings": [handover.binding_of(jobs, meta) for meta in metas]})
        sends = []

        def courier(args, expected_recipient, on_prepared, producer_binding=None):
            body = Path(args.body_file).read_text()
            _text, ref = peer_steward.peer_message.prepare_peer_message(body,
                {"harness": "runtime", "session_id": None, "name": "Hearting runtime"},
                {"harness": expected_recipient[0], "session_id": expected_recipient[1]},
                defer=True, refs=args.ref, producer_binding=producer_binding)
            on_prepared(ref)
            sends.append((body, ref, producer_binding))
            return 3

        agents = {"result": {"agents": [{"agent": "codex", "pane_id": "w1:pBatch",
            "agent_session": {"value": recipient_sid}}]}}
        with mock.patch.object(pending, "claim_ready_batch", wraps=pending.claim_ready_batch) as claim_batch, \
             mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                 stdout=json.dumps(agents), returncode=0)), \
             mock.patch.object(peer_steward, "_resolve_target", return_value=("codex", recipient_sid)), \
             mock.patch.object(peer_steward, "cmd_prompt", side_effect=courier):
            peer_steward._resume_registered_obligation(self.store.get(duties[0]["id"]), self.store)
            self.assertEqual(len(sends), 1)
            self.assertEqual(len(list(peer_steward.peer_obligations._registered_delivery_bindings(
                peer_steward.peer_message._read_pending(sends[0][1])))), 24)
            self.assertTrue(all(self.store.get(member_duty["id"])["observation"]["transfer_ref"]
                                == sends[0][1] for member_duty in duties[:24]))
            # This prepared transfer has not entered native I/O. Model its expired
            # reservation with the real reclaim transition, then an actual batch hold.
            for delivery_id in records:
                current = pending.read(jobs.parent, storage, delivery_id)
                pending.reclaim(jobs.parent, storage, delivery_id,
                                now_ns=current["claim_deadline_ns"] + 1)
            for member_duty in duties:
                self.store.update(member_duty["id"], observation={})
            claim_batch.side_effect = pending.PendingDeliveryError(
                "pending-delivery-claim-refused", "batch-hold")
            peer_steward._resume_registered_obligation(self.store.get(duties[0]["id"]), self.store)
            self.assertEqual(len(sends), 1)
            self.assertTrue(all(record["state"] == "pending" for record in records.values()))
            self.assertTrue(all(pending.read(jobs.parent, storage, did)["state"] == "pending"
                                for did in records))
            already_received = next(iter(records))
            pending.ack_received(jobs.parent, storage, already_received,
                expected_identity=pending.received_ack_identity(records[already_received], storage,
                                                                 already_received), acked_by="fixture-exact")
            records[already_received] = pending.read(jobs.parent, storage, already_received)
            already_index = list(records).index(already_received)
            with mock.patch.object(peer_steward, "_resume_native_receiving_turn", return_value=False):
                peer_steward._resume_registered_obligation(
                    self.store.get(duties[already_index]["id"]), self.store)
            self.assertEqual(len(sends), 1)
            self.assertEqual(records[already_received]["state"], "acked")

        self.assertEqual(claim_batch.call_count, 2)
        self.assertEqual(len(sends), 1)
        body, ref, producer = sends[0]
        self.assertEqual(sum(line.startswith("HEARTING_RECEIPT_V1 ")
                             for line in body.splitlines()), 24)
        self.assertEqual(len(producer["dispatch_notices"]), 24)
        self.assertLessEqual(len(peer_steward.peer_message._read_pending(ref)["text"].encode("utf-8")), 65536)
        print(f"batch24 emissions={len(sends)} wakes={len(sends)} chars={len(body)}")

    def test_unrelated_closed_missing_sidecar_does_not_become_a_global_requirement(self):
        unrelated = {**self.meta, "attempt_id": "att-unrelated", "parent_sid": "ses_unrelated",
                     "owner_route_id": "rt-closed", "owner_route_hash": "sha256:closed"}
        self.rows.append(("done", unrelated))
        self.write_jobs()
        self.replay()
        self.assertTrue(self.handover.owns(self.meta, self.SUCCESSOR, self.jobs_path))
        self.assertEqual(self.handover.effective_parent(unrelated, self.jobs_path), "ses_unrelated")

    def test_failed_native_source_never_falls_back_to_open_foreign_parent_route(self):
        for raw in (None, b"false", b"null", b"[]", b"{", b"{}", b" " * 1025):
            with self.subTest(raw=raw):
                if raw is None:
                    self.native_path.unlink(missing_ok=True)
                else:
                    self.native_path.write_bytes(raw)
                self.assert_refused()

    def test_false_native_fields_namesake_and_latest_guesses_grant_nothing(self):
        for change in ({"sessionID": "ses_foreign"}, {"sessionID": False}, {"pid": self.PID + 1},
                       {"pid": True}, {"start": "801"}, {"start": True}, {"schema": "other"},
                       {"writtenAt": None}, {"writtenAt": "2026-10-13T00:00:00"}):
            with self.subTest(change=change):
                self.native_path.write_text(json.dumps({**self.native_value, **change}))
                self.assert_refused()
        self.native_path.unlink()
        with mock.patch.dict(os.environ, {"OPENCODE_SESSION_ID": self.PARENT, "AGENT_SESSION_ID": self.PARENT}):
            self.assert_refused()

    def test_original_birth_live_unknown_reused_and_wrong_argv(self):
        real_stat = os.stat
        def proc_stat(path, *args, **kw):
            return object() if str(path) == f"/proc/{self.PID}" else real_stat(path, *args, **kw)
        for birth in ("800", None):
            with self.subTest(birth=birth), \
                 patch_split(peer_steward, "_retire_process_record", return_value=None), \
                 patch_split(peer_steward, "_proc_start_ticks", return_value=birth), \
                 mock.patch.object(peer_steward.os, "stat", side_effect=proc_stat):
                self.assert_refused()
        for change in ({"pid": True}, {"pid": 0}, {"start": None}, {"start": "801"},
                       {"argv": ["opencode", "run"]}, {"argv": ["namesake"]}, {"argv": []}):
            with self.subTest(change=change):
                duty = json.loads(json.dumps(self.duty))
                duty["intent"]["identity"]["foreground"].update(change)
                self.assert_refused(duty)
        # Reuse is proof of the OLD lifetime's end, never a source for its SID.
        with patch_split(peer_steward, "_retire_process_record", return_value={"start": "801"}):
            self.replay()
        self.assertTrue(self.handover.owns(self.meta, self.SUCCESSOR, self.jobs_path))

    def test_invalid_sidecar_and_unregistered_nonowner_relations_refuse(self):
        original = self.binding_path.read_bytes()
        for raw in (b"false", b"[]", b"{}", b"{", b" " * 4097,
                    json.dumps({"attempt_id": "att-other", "worktree": str(self.tmp_root),
                                "session_id": self.HEADLESS}).encode(),
                    json.dumps({"attempt_id": "att-selected", "worktree": str(self.tmp_root / "other"),
                                "session_id": self.HEADLESS}).encode()):
            self.binding_path.write_bytes(raw)
            self.assert_refused()
        self.binding_path.unlink()
        self.assert_refused()
        self.binding_path.write_bytes(original)
        for change in ({"dispatch_depth": "2"}, {"worker_type": "frame"}, {"registered_worker": "0"},
                       {"execution_surface": "native-subagent"}, {"owner_route_hash": ""}):
            self.rows[2] = ("done", {**self.meta, **change})
            self.write_jobs()
            self.assert_refused()

    def test_native_parent_mismatch_and_multiple_matching_attempts_include_closed_foreign(self):
        self.rows[2] = ("done", {**self.meta, "parent_sid": "ses_foreign"})
        self.write_jobs()
        self.assert_refused()
        for parent in (self.PARENT, "ses_foreign"):
            self.rows[2] = ("done", self.meta)
            other = {**self.meta, "attempt_id": "att-duplicate", "parent_sid": parent,
                     "owner_route_id": "rt-closed", "owner_route_hash": "sha256:closed"}
            self.rows = self.rows[:4] + [("done", other)]
            self.write_jobs()
            self.native.bind_session(SimpleNamespace(jobs=self.jobs_path, worktree=str(self.tmp_root),
                                                     parent_attempt_id="att-duplicate"), self.HEADLESS)
            self.assert_refused()

    def test_source_time_and_acceptance_are_real_not_backdated_payloads(self):
        for stamp in (None, False, float("nan"), float("inf"), 0, self.native_path.stat().st_ctime - 1):
            with self.subTest(stamp=stamp):
                duty = {**self.duty, "accepted_at": stamp}
                self.assert_refused(duty)
        # An old payload in a new generation remains too late.
        other = self.native_path.with_suffix(".new")
        other.write_text(json.dumps(self.native_value))
        os.replace(other, self.native_path)
        self.assert_refused()

    def test_late_sidecar_and_mark_with_old_payload_times_are_refused(self):
        original = self.binding_path.read_bytes()
        self.binding_path.write_bytes(original)
        self.assert_refused()

    def test_late_mark_with_old_payload_time_is_refused_independently(self):
        self.mark.write_bytes(self.mark.read_bytes())
        self.assert_refused()

    def test_native_mark_and_sidecar_unsafe_reads_leave_all_authority_unchanged(self):
        for number, path in enumerate((self.native_path, self.mark, self.binding_path)):
            original = path.read_bytes()
            path.unlink()
            path.symlink_to(path.with_suffix(".missing"))
            self.assert_refused()
            path.unlink()
            os.mkfifo(path)
            # Authority snapshots do not read FIFO contents.
            with mock.patch.object(self, "authority_bytes", return_value={"fixture": "unchanged"}):
                self.assertEqual(peer_steward._retire_handover(self.duty),
                                 "skipped:successor-unverified" if path == self.mark else None)
            path.unlink()
            path.write_bytes(original)
            # A restored source is newer than that acceptance; only a later one may use it.
            self.accept(f"retire-native-restored-{number}")
        # UID and EACCES injections use the actual descriptor readers.
        uid = os.getuid()
        with mock.patch.object(os, "getuid", return_value=uid + 1):
            self.assert_refused()
        real_open = os.open
        def denied(path, *args, **kwargs):
            if Path(path) == self.native_path:
                raise PermissionError()
            return real_open(path, *args, **kwargs)
        with mock.patch.object(os, "open", side_effect=denied):
            self.assert_refused()

    def test_requester_mark_and_unsupported_completed_state_refuse(self):
        for state, result in (("cancelled", "normal-exit"), ("complete", "unknown"),
                              ("complete", "force-killed")):
            self.assert_refused({**self.duty, "state": state, "result": result})
        self.assertIsNone(peer_steward._retire_handover({**self.duty, "state": "pending"}))
        for field, value in (("session_id", ""), ("harness", "unknown"), ("pane", "w1:pOther"), ("server", "foreign")):
            duty = json.loads(json.dumps(self.duty))
            duty["intent"]["requester"][field] = value
            self.assert_refused(duty)
        original = json.loads(self.mark.read_text())
        for change in ({"schema": False}, {"pane": "other"}, {"beside": "other"}, {"server": "other"},
                       {"successor": []}, {"successor": {"harness": "codex", "session_id": "other"}},
                       {"at": False}, {"at": float("nan")}, {"at": self.duty["accepted_at"] + 1}):
            self.mark.write_text(json.dumps({**original, **change}))
            self.assert_refused()

    def test_closed_route_and_another_successor_remain_authoritative(self):
        outcome = self.route_path.with_suffix(".outcome.json")
        outcome.write_text('{"state":"closed"}')
        self.assert_refused()
        outcome.unlink()
        self.handover.record_retire_handover(self.PARENT, "opencode", "first-successor", "claude",
            jobs=self.jobs_path, successor_seat=self.tidy._pane_seat_of("w1:pFirst"))
        before = self.authority_bytes()
        self.replay()
        self.assertEqual(self.authority_bytes(), before)
        self.assertEqual(self.handover.effective_parent(self.meta, self.jobs_path), "first-successor")

    def test_selected_generation_is_rechecked_under_mark_lock_without_global_jobs_freeze(self):
        real = peer_steward._retire_predecessor
        calls = 0
        def replaced(duty):
            nonlocal calls
            calls += 1
            if calls == 2:
                other = self.binding_path.with_suffix(".new")
                other.write_bytes(self.binding_path.read_bytes())
                os.replace(other, self.binding_path)
            return real(duty)
        with mock.patch.object(peer_steward, "_retire_predecessor", side_effect=replaced):
            self.assert_refused()

    def test_same_normal_command_and_detached_callback_consume_original_requester(self):
        with mock.patch.object(peer_steward, "_retire_target", return_value=("unknown", {
                "harness": "-", "name": "old", "pane": "-", "session_id": "-"}, 1, "pane-unverified")), \
             mock.patch.object(peer_steward, "_current_session_identity", return_value=(self.SUCCESSOR, "codex")), \
             mock.patch.object(peer_steward, "_caller_pane", return_value="w1:pNew"), \
             patch_split(peer_steward, "_herdr_missing", return_value=False), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=AssertionError("transport")):
            self.assertEqual(peer_steward.cmd_retire(SimpleNamespace(target="w1:pOld")), 0)
        self.assertTrue(self.handover.owns(self.meta, self.SUCCESSOR, self.jobs_path))

    def test_detached_lifecycle_needs_no_observer_native_identity(self):
        before = self.protected()
        with mock.patch.object(peer_steward, "_current_session_identity", return_value=("observer", "claude")), \
             mock.patch.object(peer_steward, "_caller_pane", return_value=""), \
             mock.patch.object(peer_steward, "_ensure_watch_observers"), \
             mock.patch.object(peer_steward.dispatch_batch_obligations, "ensure_observers"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=AssertionError("transport")):
            self.assertEqual(peer_steward.cmd_ensure_obligations(None), 0)
            # The callback is bounded; exercise the runner's retained duty
            # pass with this fixture's native transport boundary in place.
            peer_steward._reconcile_external_obligations(
                peer_steward.peer_obligations.ObligationStore())
        self.assertTrue(self.handover.owns(self.meta, self.SUCCESSOR, self.jobs_path))
        self.assertEqual(self.protected(), before)

    def test_real_start_correct_admission_and_retained_input_use_written_effective_parent(self):
        import work_start
        import dispatch_replacement as replacement
        import dispatch_owner_input as owner_input
        from dispatch_contract import DispatchContractError
        # Normal input admission exists before the owner ends.
        self.rows[2] = ("running", self.meta)
        self.write_jobs()
        owner_input.initialize_owner_input(self.jobs_path, "att-selected", "opencode-next-turn")
        self.rows[2] = ("done", self.meta)
        self.write_jobs()
        before = self.protected()
        self.replay()
        self.assertEqual(self.protected(), before)
        rows = replacement._rows(self.jobs_path.read_text().splitlines())
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": self.SUCCESSOR,
                                         "AGENT_DISPATCH_CALLER_HARNESS": "codex"}):
            # This is the existing correct/start continuation admission, not a
            # mocked owns=True or a new path. Input writes have their own baseline.
            replacement._authorized(self.jobs_path, rows, self.meta)
            input_path = owner_input._path(self.jobs_path, "att-selected")
            initial = input_path.read_bytes()
            result = owner_input.submit(self.jobs_path, "att-selected", "Continue approved work", "exact-answer")
            self.assertTrue(result["retained"])
            self.assertNotEqual(input_path.read_bytes(), initial)
            value = json.loads(input_path.read_bytes())
            self.assertEqual(value["requests"][0]["source_session"], self.SUCCESSOR)
            self.assertEqual(self.meta["parent_sid"], self.PARENT)
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "foreign-successor",
                                         "AGENT_DISPATCH_CALLER_HARNESS": "codex"}):
            with self.assertRaises(DispatchContractError):
                replacement._authorized(self.jobs_path, rows, self.meta)
        # Independent normal live start reuse consumes the SAME logical binding.
        self.rows[2] = ("running", self.meta)
        self.write_jobs()
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": self.SUCCESSOR}):
            self.assertEqual(work_start._slot(self.route, "owner", work_start._rows(self.jobs_path), self.jobs_path),
                             "att-selected")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "foreign-successor"}):
            with self.assertRaises(DispatchContractError):
                work_start._slot(self.route, "owner", work_start._rows(self.jobs_path), self.jobs_path)

    def test_real_completion_storage_allowlist_lease_ack_and_no_redelivery(self):
        import dispatch_pending_delivery as pending
        import dispatch_session_sweep as sweep
        # The synchronous Claude carrier reads the same written handover; native
        # turn-proof support is not inferred for the other harnesses.
        self.book("claude", "retire-native-claude")
        self.replay()
        root, kind = self.jobs_path.parent, "claude-parent-runtime"
        delivery = "delivery-" + "a" * 32
        receipt = {"schema_version": 2, "state": "delivered", "parent_attempt_id": "att-selected",
            "job_registry": str(self.jobs_path), "delivery_classification": "attention",
            "children": [{"attempt_id": "att-selected", "status": "done", "readiness": "ready",
                          "reason": "terminal-failure-or-unclosed", "required_action": "inspect-done-failure",
                          "harness": "opencode", "delivery_classification": "attention"}]}
        pending.create(root, recipient_kind=kind, recipient_key=self.PARENT, delivery_id=delivery,
            session_generation="", session_generation_supported="0", attempt_ids=["att-selected"],
            parent_attempt_id="att-selected", route_id=self.route["route_id"], route_node="owner",
            receipt=receipt, receipt_digest=pending._canonical_receipt_digest(receipt),
            row_revisions={"att-selected": "original-revision"})
        original = pending.read(root, self.PARENT, delivery)
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pNew"}):
            recipients = self.handover.storage_recipients(self.SUCCESSOR, harness="claude")
            self.assertIn((self.PARENT, frozenset({"att-selected", "att-ancestor-one", "att-ancestor-two"})),
                          recipients)
            allowed = dict(recipients)[self.PARENT]
            self.assertFalse(self.handover.record_for_session({"attempt_ids": ["att-foreign"]}, allowed))
            self.assertEqual(sweep.addressed_records(root, kind, "foreign-successor"), ([], 0))
            records, _ = sweep.sweep_deliver(root, kind, self.SUCCESSOR)
            self.assertEqual([r["delivery_id"] for r in records], [delivery])
            claimed = pending.read(root, self.PARENT, delivery)
            for key in ("delivery_id", "receipt_digest", "recipient_digest", "row_revisions"):
                self.assertEqual(claimed[key], original[key])
            self.assertEqual(sweep.ack_delivered(root, self.SUCCESSOR, records, acked_by="fixture-consumed"), 1)
            self.assertEqual(pending.read(root, self.PARENT, delivery)["state"], "acked")
            self.assertEqual(sweep.sweep_deliver(root, kind, self.SUCCESSOR)[0], [])
            self.assertIsNone(pending.read(root, self.SUCCESSOR, delivery))

    def test_pending_gone_settlement_defers_the_stale_label_to_the_completed_replay(self):
        self.duty = self.store.create("retire-native-pending", "retire", self.identity,
                                      {"target": "w1:pOld", "requester": self.requester})
        authority = self.authority_bytes()
        with mock.patch.object(peer_steward.subprocess, "run", side_effect=AssertionError("transport")):
            self.assertTrue(peer_steward._complete_gone_retire(self.store, self.duty))
        # The pending path never forwards the stale headless label (route F stays).
        self.assertEqual(self.authority_bytes(), authority)
        self.assertEqual(self.handover.effective_parent(self.foreign, self.jobs_path), self.HEADLESS)
        settled = self.store.get(self.duty["id"])
        self.assertEqual((settled["state"], settled["result"]), ("complete", "target-already-gone"))
        self.replay(settled)
        self.assertTrue(self.handover.owns(self.meta, self.SUCCESSOR, self.jobs_path))
        self.assertEqual(self.handover.effective_parent(self.foreign, self.jobs_path), self.HEADLESS)

    def test_unrelated_registry_appends_do_not_freeze_the_selected_relation(self):
        real = peer_steward._retire_predecessor
        calls = 0
        def appended(duty):
            nonlocal calls
            calls += 1
            if calls == 2:
                with self.jobs_path.open("a") as handle:
                    handle.write("now\trunning\t/x\t/x\tlog\tattempt_id=att-later,dispatch_depth=2\n")
            return real(duty)
        with mock.patch.object(peer_steward, "_retire_predecessor", side_effect=appended):
            self.replay()
        self.assertEqual(calls, 2)
        self.assertTrue(self.handover.owns(self.meta, self.SUCCESSOR, self.jobs_path))

    def test_ordinary_completed_opencode_booking_keeps_its_label_without_retained_source(self):
        # A booked label that is no headless owner session is the ordinary path:
        # a disposed TUI record does not block the missed-handover replay.
        self.native_path.unlink()
        self.binding_path.unlink()
        self.rows = [("done", {**self.meta, "attempt_id": aid, "parent_sid": self.HEADLESS})
                     for aid in ("att-ancestor-one", "att-ancestor-two", "att-selected")]
        self.write_jobs()
        self.replay()
        self.assertFalse(self.mark.exists())
        self.assertEqual(self.handover.effective_parent(self.rows[2][1], self.jobs_path), self.SUCCESSOR)

    def test_retained_selection_that_contradicts_an_ordinary_label_grants_nothing(self):
        self.binding_path.unlink()
        self.rows = [("done", {**self.meta, "attempt_id": aid, "parent_sid": self.HEADLESS})
                     for aid in ("att-ancestor-one", "att-ancestor-two", "att-selected")]
        self.write_jobs()
        authority = self.authority_bytes()
        self.replay()
        self.assertEqual(self.authority_bytes(), authority)
        self.assertEqual(self.handover.effective_parent(self.rows[2][1], self.jobs_path), self.HEADLESS)


class RetireBackgroundDialogTest(unittest.TestCase):
    def _cells(self, text):
        return [(ch, False) for ch in text]

    def test_detects_live_selection_ui(self):
        lines = [self._cells("Background work is running"),
                 self._cells("python3 train.py"),
                 self._cells("1. Exit and stop tasks"),
                 self._cells("2. Move to background and exit"),
                 self._cells("3. Stay"),
                 self._cells("Enter to confirm · Esc to cancel"),
                 self._cells("❯ ")]
        tasks = peer_steward._retire_background_dialog_lines(lines)
        self.assertIsNotNone(tasks)

    def test_detects_cursor_prefixed_selection(self):
        lines = [self._cells("Background work is running"),
                 self._cells("❯ 1. Exit and stop tasks"),
                 self._cells("2. Move to background and exit"),
                 self._cells("3. Stay"),
                 self._cells("Enter to select · Esc to cancel")]
        tasks = peer_steward._retire_background_dialog_lines(lines)
        self.assertIsNotNone(tasks)

    def test_ignores_other_forms(self):
        lines = [self._cells("Do you want to proceed?"),
                 self._cells("Enter to confirm")]
        self.assertIsNone(peer_steward._retire_background_dialog_lines(lines))
        self.assertIsNone(peer_steward._retire_background_dialog_lines([]))
        self.assertIsNone(peer_steward._retire_background_dialog_lines(None))

    def test_ignores_quoted_transcript_without_live_ui(self):
        lines = [self._cells("● Reading the PR #288 diff"),
                 self._cells("+    The dialog reads \"Background work is running … 1. Exit and stop tasks /"),
                 self._cells("+    2. Move to background and exit / 3. Stay\". …"),
                 self._cells("❯ ")]
        self.assertIsNone(peer_steward._retire_background_dialog_lines(lines))

    def test_ignores_busy_answer_quoting_dialog(self):
        lines = [self._cells("✻ Working… (esc to interrupt)"),
                 self._cells("  The dialog reads \"Background work is running"),
                 self._cells("  1. Exit and stop tasks"),
                 self._cells("  2. Move to background and exit / 3. Stay\"."),
                 self._cells("  Enter to confirm · Esc to cancel is only in the real window."),
                 self._cells("❯ let me check the logs")]
        self.assertIsNone(peer_steward._retire_background_dialog_lines(lines))

    def test_retire_rechecks_before_confirmation_enter_if_another_form_appears(self):
        dialog = peer_steward._screen_lines(
            "Background work is running\n❯ 1. Exit and stop tasks\n"
            "2. Move to background and exit\n3. Stay\nEnter to confirm · Esc to cancel\n")
        unrelated_form = peer_steward._screen_lines("Do you want to proceed?\nEnter to confirm\n")
        for switch_at in (3, 5):
            with self.subTest(switch_at=switch_at):
                reads = [dialog] * (switch_at - 1) + [unrelated_form]
                calls = []
                def run(argv, **kwargs):
                    calls.append(argv)
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
                with mock.patch.object(peer_steward, "_read_screen", side_effect=reads), \
                     mock.patch.object(peer_steward, "_retire_foreground", return_value="same-process"), \
                     mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
                     mock.patch.object(peer_steward, "_RETIRE_SECONDS", 0):
                    result = peer_steward._retire_claude_background_confirm(
                        "peer", "w1:pX", "claude", {}, "same-process",
                        lambda reason, **kwargs: reason, None, None,
                    )
                self.assertEqual(result, "form-open")
                keys = [argv[4:] for argv in calls if argv[:3] == ["herdr", "pane", "send-keys"]]
                self.assertEqual(len(keys), 1 if switch_at == 5 else 0)
                texts = [argv[-1] for argv in calls if argv[:3] == ["herdr", "pane", "send-text"]]
                self.assertEqual(texts, ["1"] if switch_at == 3 else ["1", "3"])

    def test_confirmation_enter_is_not_sent_to_a_changed_foreground(self):
        dialog = peer_steward._screen_lines(
            "Background work is running\n❯ 1. Exit and stop tasks\n"
            "2. Move to background and exit\n3. Stay\nEnter to confirm · Esc to cancel\n")
        with mock.patch.object(peer_steward, "_read_screen", return_value=dialog), \
             mock.patch.object(peer_steward, "_retire_foreground",
                               side_effect=["original", "original", "replacement"]), \
             mock.patch.object(peer_steward.subprocess, "run") as run:
            result = peer_steward._retire_claude_background_confirm(
                "peer", "w1:pX", "claude", {}, "original",
                lambda reason, **kwargs: reason, None, None)
        self.assertEqual(result, "foreground-changed")
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["herdr", "pane", "send-text", "w1:pX", "1"]])


class BlockingScreenClassifierTest(unittest.TestCase):
    @staticmethod
    def cells(text):
        return [(char, False) for char in text]

    def test_codex_hook_review_is_recognized_at_top_for_normal_and_wrapped_views(self):
        for screen in (
            "Hooks need review\n1 hook is new or changed\nReview hooks\n"
            "Trust all and continue\nContinue without trusting\nEnter to confirm · Esc to skip",
            "Hooks need review\n2 hooks are new or changed\nChecking hooks…\n"
            "1. Review hooks\n2. Trust all and continue\n"
            "3. Continue without trusting (hooks\n   won't run)",
        ):
            with self.subTest(screen=screen):
                self.assertEqual("native-trust-wait", peer_steward._blocking_screen(
                    "codex", [self.cells(line) for line in screen.splitlines()]))

    def test_real_codex_01621_captures_and_upstream_snapshots_are_recognized(self):
        capture_names = (
            "cap-alt80.txt", "cap-alt80.ansi", "cap-inline80.txt", "cap-inline80.ansi",
            "cap-alt40.txt", "cap-alt40.ansi", "cap-inline40.txt", "cap-inline40.ansi",
            "cap-80x40-noalt.txt",
        )
        for name in capture_names:
            with self.subTest(capture=name):
                self.assertEqual("native-trust-wait", peer_steward._blocking_screen(
                    "codex", _codex_capture(name)))
        for name in ("startup_hooks_review_prompt.snap", "startup_hooks_picker_progress_40.snap",
                     "startup_hooks_review_prompt_with_trust_error.snap"):
            with self.subTest(snapshot=name):
                self.assertEqual("native-trust-wait", peer_steward._blocking_screen(
                    "codex", _codex_snapshot(name)))

    def test_codex_location_popup_under_transcript_blocks_but_quotes_do_not(self):
        popup = _codex_snapshot("worktrees_new_choices.snap")
        chat_popup = "banner: project · main\nPrior chat text\n" + popup
        self.assertEqual("form-open", peer_steward._blocking_screen("codex", chat_popup))
        quoted = [self.cells("● Working"), self.cells("Quoted: Where should the new conversation run?"),
                  self.cells("› Use current Git worktree; Enter select · Esc back"),
                  self.cells("❯ Continue with the requested change")]
        for harness in ("codex", "claude", "opencode"):
            with self.subTest(harness=harness):
                self.assertIsNone(peer_steward._blocking_screen(harness, quoted))
        # Codex 0.162.1's own composer glyph is `›`, usually followed by its
        # status footer, so the composer is not necessarily the last row.
        popup_rows = popup.strip("\n").splitlines()
        for tail in (["› ordinary draft"],
                     ["› ordinary draft", "", "  ? for shortcuts            100% context left"]):
            with self.subTest(tail=tail):
                quoted_popup = "\n".join(["• Earlier answer quoting the picker:"] + popup_rows + tail)
                self.assertIsNone(peer_steward._blocking_screen("codex", quoted_popup))

    def test_singular_count_with_period_matches_real_dialog_shape(self):
        screen = ("Hooks need review\n1 hook is new or changed.\nReview hooks\n"
                  "Trust all and continue\nContinue without trusting\n")
        self.assertEqual("native-trust-wait", peer_steward._blocking_screen("codex", screen))

    def test_codex_hook_review_quote_and_unknown_screen_do_not_block(self):
        quote = [self.cells("● Reviewing documentation"),
                 self.cells("+ Hooks need review 1 hook is new or changed"),
                 self.cells("+ Review hooks / Trust all and continue / Continue without trusting"),
                 self.cells("❯ let me inspect the implementation")]
        self.assertIsNone(peer_steward._blocking_screen("codex", quote))
        self.assertIsNone(peer_steward._blocking_screen("codex", None))
        self.assertIsNone(peer_steward._blocking_screen("codex", []))

    def test_hook_review_signature_is_codex_scoped(self):
        lines = [self.cells(row) for row in (
            "Hooks need review", "1 hook is new or changed", "Review hooks",
            "Trust all and continue", "Continue without trusting")]
        self.assertEqual("native-trust-wait", peer_steward._blocking_screen("codex", lines))
        self.assertIsNone(peer_steward._blocking_screen("claude", lines))
        self.assertIsNone(peer_steward._blocking_screen("opencode", lines))


class ObservedStartTest(_TmpRootMixin, unittest.TestCase):
    def invoke(self, kind, world):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name, value in (("_herdr_missing", False), ("_ensure_pane_ingress", None),
                                ("_export_opencode_tui_scoped", "missing"),
                                ("_read_screen", None), ("_pane_is_managed", False),
                                ("_process_session", "observed-sid" if kind == "codex" else None),
                                ("_current_session_identity", ("parent-sid", "claude"))):
                stack.enter_context(patch_split(peer_steward, name, return_value=value))
            stack.enter_context(mock.patch.object(peer_steward.subprocess, "run", side_effect=world))
            stack.enter_context(mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: world.clock))
            stack.enter_context(mock.patch.object(peer_steward.time, "sleep", side_effect=world.sleep))
            output = stack.enter_context(mock.patch("builtins.print"))
            rc = peer_steward.main(["start", "requested", "--kind", kind, "--pane", "w1:pM",
                                    "--cwd", str(self.tmp_root)])
        return rc, output.call_args[0][0]

    def world(self, kind, *, existing=False, error="timeout", name=None, sid="observed-sid"):
        owner = self

        class World:
            clock = 0.0
            launched = existing
            calls = None

            def __init__(self):
                self.calls = []
                self.name = name
                self.kind = kind
                self.sid = sid
                self.cwd = str(owner.tmp_root)
                self.ready_after = .1

            def sleep(self, value):
                self.clock += value

            def __call__(self, argv, **kwargs):
                self.calls.append(argv)
                if argv[:3] == ["herdr", "agent", "get"]:
                    if not self.launched or (not existing and self.clock < self.ready_after):
                        return _herdr_json({"error": {"code": "agent_not_found"}})
                    return _herdr_json({"result": {"agent": {
                        "agent": self.kind, "pane_id": "w1:pM", "name": self.name,
                        "cwd": self.cwd, "agent_session": {"value": self.sid}, "agent_status": "idle"}}})
                if argv[:3] == ["herdr", "agent", "start"]:
                    self.launched = True
                    return _herdr_json({"error": {"code": error}})
                if argv[:3] == ["herdr", "agent", "rename"]:
                    self.name = argv[-1]
                    return _herdr_json({"result": {"type": "ok"}})
                raise AssertionError(argv)

        return World()

    def test_late_native_timeout_and_busy_settle_once_on_all_harnesses(self):
        for kind in ("claude", "codex", "opencode"):
            for error in ("timeout", "agent_pane_busy"):
                with self.subTest(kind=kind, error=error):
                    world = self.world(kind, error=error)
                    rc, line = self.invoke(kind, world)
                    self.assertEqual(rc, 0)
                    self.assertIn("started=true", line)
                    self.assertIn("start_verify=observed-pane", line)
                    self.assertEqual(sum(a[:3] == ["herdr", "agent", "start"] for a in world.calls), 1)
                    self.assertEqual(sum(a[:3] == ["herdr", "agent", "rename"] for a in world.calls), 1)
                    pane_info_reads = [a for a in world.calls
                                       if a[:3] == ["herdr", "pane", "process-info"]]
                    self.assertEqual(pane_info_reads, [])  # this case stubs the shared resolver
                    marker = peer_steward.peer_message.read_steward_markers()[("claude", "parent-sid")]
                    entry = marker["targets"]["observed-sid"]
                    self.assertEqual((entry["harness"], entry["pane"], entry["source"]), (kind, "w1:pM", "start"))

    def test_a_refused_start_names_why_the_observed_agent_was_not_adopted(self):
        for cwd, miss in (("", "cwd-empty"), ("/elsewhere", "cwd-mismatch")):
            with self.subTest(cwd=cwd):
                world = self.world("claude", error="agent_pane_busy")
                world.cwd = cwd
                rc, line = self.invoke("claude", world)
                self.assertIn("started=false", line)
                self.assertIn("reason=agent_pane_busy", line)
                self.assertIn(f"observed={miss}", line)
                self.assertEqual(sum(a[:3] == ["herdr", "agent", "rename"] for a in world.calls), 0)
        world = self.world("claude", error="agent_pane_busy")
        rc, line = self.invoke("claude", world)
        self.assertIn("started=true", line)
        self.assertNotIn("observed=", line)

    def test_reuse_observes_existing_pane_without_another_launch_or_keyboard_input(self):
        for kind in ("claude", "codex", "opencode"):
            with self.subTest(kind=kind):
                world = self.world(kind, existing=True)
                rc, line = self.invoke(kind, world)
                self.assertEqual(rc, 0)
                self.assertIn("started=true", line)
                self.assertEqual([a[2] for a in world.calls], ["get", "rename", "get"])

    def test_delayed_sid_completes_only_the_matching_name_and_pane_entry(self):
        world = self.world("opencode", existing=True, sid="-")
        _, line = self.invoke("opencode", world)
        self.assertIn("session_id=-", line)
        marker = peer_steward.peer_message.read_steward_markers()[("claude", "parent-sid")]
        self.assertIsNone(marker["targets"]["requested"]["session_id"])
        world.sid = "ses_observed"
        self.invoke("opencode", world)
        marker = peer_steward.peer_message.read_steward_markers()[("claude", "parent-sid")]
        self.assertEqual(set(marker["targets"]), {"ses_observed"})
        self.assertEqual(marker["targets"]["ses_observed"]["pane"], "w1:pM")

    def test_foreign_name_kind_or_directory_never_claims_an_existing_agent(self):
        for change in (dict(name="foreign"), dict(kind="codex"), dict(cwd="/elsewhere")):
            with self.subTest(change=change):
                world = self.world("opencode", existing=True, name="requested")
                for key, value in change.items():
                    setattr(world, key, value)
                rc, line = self.invoke("opencode", world)
                self.assertIn("started=false", line)
                self.assertFalse(any(a[:3] == ["herdr", "agent", "rename"] for a in world.calls))
                self.assertEqual(peer_steward.peer_message.read_steward_markers(), {})

    def test_a_real_timeout_stays_failed_with_no_marker_and_one_launch(self):
        world = self.world("opencode")
        world.ready_after = 100
        _, line = self.invoke("opencode", world)
        self.assertIn("started=false", line)
        self.assertIn("reason=timeout", line)
        self.assertLessEqual(world.clock, 5)
        self.assertEqual(peer_steward.peer_message.read_steward_markers(), {})
        self.assertEqual(sum(a[:3] == ["herdr", "agent", "start"] for a in world.calls), 1)


class NativeReceivingTurnRecoveryTest(_TmpRootMixin, unittest.TestCase):
    def _duty(self):
        import dispatch_completion_join as join
        import dispatch_pending_delivery as pending
        aid, sid, delivery_id, revision = "att-native-receipt", "codex-thread", "delivery-native-crash", "rev-1"
        receipt = {"schema_version": 2, "state": "ready", "parent_attempt_id": "parent-session-native",
            "job_registry": str(self.jobs_path), "children": [{"attempt_id": aid, "status": "done",
                "readiness": "ready", "reason": "registry-closed", "required_action": "advance-completed",
                "harness": "codex"}]}
        meta = {"attempt_schema_version": "2", "dispatch_depth": "1", "transport": "headless",
            "execution_surface": "registered-headless", "registered_worker": "1", "launch_claimed": "1",
            "attempt_id": aid, "parent_sid": sid, "parent_completion_delivery": "codex-native-queue",
            "delivery_intent": "1", "delivery_id": delivery_id,
            "delivery_receipt_digest": pending._canonical_receipt_digest(receipt),
            "delivery_row_revision": revision, "delivery_recipient_kind": "codex-native-queue",
            "route_id": "rt-native", "route_node": "execute", "parent_attempt_id": "att-owner",
            "harness": "codex", "note": "completed-marker", "launch_outcome": "completed"}
        self.jobs_path.write_text("t\tdone\t/repo\t/wt\tchild\t" + ",".join(
            f"{key}={value}" for key, value in meta.items()) + "\n")
        pending.create(self.tmp_root, recipient_kind="codex-native-queue", recipient_key=sid,
            delivery_id=delivery_id, session_generation="", session_generation_supported="0",
            attempt_ids=[aid], parent_attempt_id="att-owner", route_id="rt-native", route_node="execute",
            receipt=receipt, receipt_digest=pending._canonical_receipt_digest(receipt),
            row_revisions={aid: revision})
        pending.claim(self.tmp_root, sid, delivery_id, claim_owner="fixture", lease_seconds=30)
        pending.ack(self.tmp_root, sid, delivery_id, acked_by="codex-native-prompt:" + sid)
        identity = {"jobs": str(self.jobs_path.resolve()), "attempt_id": aid, "session_id": sid,
                    "harness": "codex", "server": "default"}
        store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
        duty_id = peer_steward.peer_obligations.stable_duty_id("registered-batch", identity)
        duty = store.create(duty_id, "registered-batch", identity, {"carrier": "codex-native-queue"})
        binding = {"thread_id": sid, "turn_id": "turn-exact", "status": "inProgress",
            "delivery_id": delivery_id, "receipt_digest": meta["delivery_receipt_digest"],
            "row_revision": revision, "storage_recipient": sid, "route_id": "rt-native",
            "route_node": "execute", "parent_attempt_id": "att-owner", "recipient_sid": sid}
        duty = store.update(duty_id, observation={"native_receiving_turn": binding})
        return store, duty, aid, sid

    def test_present_registered_record_does_not_rematerialize(self):
        import dispatch_completion_join as join
        store, duty, _aid, sid = self._duty()
        with mock.patch.object(join, "materialize_after_terminal_close") as materialize, \
             mock.patch("dispatch_seat_handover.effective_parent", return_value=sid), \
             mock.patch.object(peer_steward, "_resume_native_receiving_turn", return_value=False):
            peer_steward._resume_registered_obligation(duty, store)
        materialize.assert_not_called()
        self.assertEqual(store.get(duty["id"])["state"], "complete")

    def test_missing_registered_record_recovers_after_transient_materialization_failure(self):
        import dispatch_completion_join as join
        import dispatch_pending_delivery as pending
        store, duty, aid, sid = self._duty()
        exact = pending.read(self.tmp_root, sid, "delivery-native-crash")
        path = pending.record_path(self.tmp_root, sid, "delivery-native-crash")
        path.unlink()
        attempts = 0

        def recover_once(jobs, attempt_id):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                restored = pending.create(
                    self.tmp_root,
                    recipient_kind=exact["recipient_kind"], recipient_key=sid,
                    delivery_id=exact["delivery_id"],
                    session_generation=exact["session_generation"],
                    session_generation_supported=exact["session_generation_supported"],
                    attempt_ids=exact["attempt_ids"],
                    parent_attempt_id=exact["parent_attempt_id"],
                    route_id=exact["route_id"], route_node=exact["route_node"],
                    receipt=exact["receipt"], receipt_digest=exact["receipt_digest"],
                    row_revisions=exact["row_revisions"], lineage=exact.get("lineage", []))
                pending.claim(self.tmp_root, sid, restored["delivery_id"],
                              claim_owner="fixture", lease_seconds=30)
                pending.ack(self.tmp_root, sid, restored["delivery_id"],
                            acked_by="codex-native-prompt:" + sid)

        with mock.patch.object(join, "materialize_after_terminal_close",
                               side_effect=recover_once) as materialize, \
             mock.patch("dispatch_seat_handover.effective_parent", return_value=sid), \
             mock.patch.object(peer_steward, "_resume_native_receiving_turn", return_value=False):
            peer_steward._resume_registered_obligation(store.get(duty["id"]), store)
            self.assertEqual(store.get(duty["id"])["state"], "unknown")
            peer_steward._resume_registered_obligation(store.get(duty["id"]), store)

        self.assertEqual(materialize.call_count, 2)
        self.assertEqual(pending.read(self.tmp_root, sid, "delivery-native-crash")["state"], "acked")
        current = store.get(duty["id"])
        self.assertEqual((current["state"], current["delivery"]), ("complete", "acked"))

    def test_registered_and_external_settlement_share_owner_retry(self):
        import dispatch_batch_obligations as obligations
        import dispatch_completion_join as join
        import dispatch_pending_delivery as pending
        import dispatch_registry_cache
        from dispatch_terminal_commit import CompletionState

        store, duty, aid, sid = self._duty()
        pending.record_path(self.tmp_root, sid, "delivery-native-crash").unlink()
        text = self.jobs_path.read_text(encoding="utf-8").replace(
            f"attempt_id={aid}",
            f"attempt_id={aid},worker_type=owner,workflow_completion=runtime-v1")
        self.jobs_path.write_text(text, encoding="utf-8")
        dispatch_registry_cache.clear_cache()
        duties = [duty]
        for index in range(1, 4):
            duties.append(store.create(
                f"registered-shared-owner-{index}", "registered-batch",
                dict(duty["intent"]["identity"]), {"carrier": "codex-native-queue"}))
        context_type = getattr(obligations, "OwnerSettlementReadContext", None)
        context = context_type() if context_type is not None else None
        if context is not None:
            context._clock = lambda: 1000.0
            self.addCleanup(context.close)
        inspect = __import__("inspect")
        supports_settlement_context = "settlement_context" in inspect.signature(
            peer_steward._resume_registered_obligation).parameters
        supports_owner_context = "context" in inspect.signature(
            obligations._resume_owner_settlements).parameters

        def resume_registered(member):
            kwargs = ({"settlement_context": context}
                      if supports_settlement_context else {})
            peer_steward._resume_registered_obligation(
                store.get(member["id"]), store, **kwargs)

        def resume_external():
            kwargs = {"context": context} if supports_owner_context else {}
            obligations._resume_owner_settlements(self.jobs_path, **kwargs)

        with mock.patch.object(join, "materialize_after_terminal_close") as materialize, \
             mock.patch("dispatch_terminal_commit.owner_completion_state",
                        return_value=CompletionState("pending")), \
             mock.patch("dispatch_contract.supervisor_lease_is_held", return_value=False), \
             mock.patch.object(peer_steward.peer_obligations,
                               "_registered_terminal_state", return_value=None), \
             mock.patch("dispatch_seat_handover._effective", return_value=(sid, "codex")):
            for _pass in range(3):
                for member in duties:
                    resume_registered(member)
                resume_external()
                self.assertEqual(materialize.call_count, 1,
                                 "R1_THREE_PASSES_ONE_REGISTERED_REDRIVE")

    def test_shared_owner_retry_observes_new_pending_before_due(self):
        import dispatch_batch_obligations as obligations
        import dispatch_completion_join as join
        import dispatch_pending_delivery as pending
        import dispatch_registry_cache
        from dispatch_terminal_commit import CompletionState

        store, duty, aid, sid = self._duty()
        exact = pending.read(self.tmp_root, sid, "delivery-native-crash")
        pending.record_path(self.tmp_root, sid, "delivery-native-crash").unlink()
        text = self.jobs_path.read_text(encoding="utf-8").replace(
            f"attempt_id={aid}",
            f"attempt_id={aid},worker_type=owner,workflow_completion=runtime-v1")
        self.jobs_path.write_text(text, encoding="utf-8")
        dispatch_registry_cache.clear_cache()
        duties = [duty]
        for index in range(1, 4):
            duties.append(store.create(
                f"registered-shared-owner-{index}", "registered-batch",
                dict(duty["intent"]["identity"]), {"carrier": "codex-native-queue"}))
        context_type = getattr(obligations, "OwnerSettlementReadContext", None)
        context = context_type() if context_type is not None else None
        if context is not None:
            context._clock = lambda: 1000.0
            self.addCleanup(context.close)
        inspect = __import__("inspect")
        supports_settlement_context = "settlement_context" in inspect.signature(
            peer_steward._resume_registered_obligation).parameters
        supports_owner_context = "context" in inspect.signature(
            obligations._resume_owner_settlements).parameters

        def resume_registered(member):
            kwargs = ({"settlement_context": context}
                      if supports_settlement_context else {})
            peer_steward._resume_registered_obligation(
                store.get(member["id"]), store, **kwargs)

        def resume_external():
            kwargs = {"context": context} if supports_owner_context else {}
            obligations._resume_owner_settlements(self.jobs_path, **kwargs)

        with mock.patch.object(join, "materialize_after_terminal_close") as materialize, \
             mock.patch("dispatch_terminal_commit.owner_completion_state",
                        return_value=CompletionState("pending")), \
             mock.patch("dispatch_contract.supervisor_lease_is_held", return_value=False), \
             mock.patch.object(peer_steward.peer_obligations,
                               "_registered_terminal_state", return_value=None), \
             mock.patch("dispatch_seat_handover._effective", return_value=(sid, "codex")):
            for member in duties:
                resume_registered(member)
            resume_external()

            restored = pending.create(
                self.tmp_root, recipient_kind=exact["recipient_kind"], recipient_key=sid,
                delivery_id=exact["delivery_id"],
                session_generation=exact["session_generation"],
                session_generation_supported=exact["session_generation_supported"],
                attempt_ids=exact["attempt_ids"], parent_attempt_id=exact["parent_attempt_id"],
                route_id=exact["route_id"], route_node=exact["route_node"],
                receipt=exact["receipt"], receipt_digest=exact["receipt_digest"],
                row_revisions=exact["row_revisions"], lineage=exact.get("lineage", []))
            self.assertEqual(restored["recipient_kind"], "codex-native-queue")
            self.assertEqual(restored["receipt_digest"], duty["observation"][
                "native_receiving_turn"]["receipt_digest"])
            self.assertEqual(restored["row_revisions"][aid], duty["observation"][
                "native_receiving_turn"]["row_revision"])
            self.assertTrue(pending.received_ack_identity(
                restored, sid, restored["delivery_id"]))
            pending.claim(self.tmp_root, sid, restored["delivery_id"],
                           claim_owner="fixture", lease_seconds=30)
            pending.ack(self.tmp_root, sid, restored["delivery_id"], acked_by="fixture")
            with mock.patch.object(peer_steward, "_resume_native_receiving_turn",
                                   return_value=False) as delivery_seam:
                resume_registered(duty)
            delivery_seam.assert_called_once()

    def test_registered_redrive_waits_for_held_owner_lease(self):
        import dispatch_batch_obligations as obligations
        import dispatch_completion_join as join
        import dispatch_pending_delivery as pending
        import dispatch_registry_cache
        from dispatch_terminal_commit import CompletionState

        store, duty, aid, sid = self._duty()
        pending.record_path(self.tmp_root, sid, "delivery-native-crash").unlink()
        text = self.jobs_path.read_text(encoding="utf-8").replace(
            f"attempt_id={aid}",
            f"attempt_id={aid},worker_type=owner,workflow_completion=runtime-v1")
        self.jobs_path.write_text(text, encoding="utf-8")
        dispatch_registry_cache.clear_cache()
        context = obligations.OwnerSettlementReadContext()
        context._clock = lambda: 1000.0
        self.addCleanup(context.close)

        def resume():
            peer_steward._resume_registered_obligation(
                store.get(duty["id"]), store, settlement_context=context)

        with mock.patch.object(join, "materialize_after_terminal_close") as materialize, \
             mock.patch("dispatch_terminal_commit.owner_completion_state",
                        return_value=CompletionState("pending")), \
             mock.patch("dispatch_contract.supervisor_lease_is_held",
                        return_value=True) as lease_is_held, \
             mock.patch.object(peer_steward.peer_obligations,
                               "_registered_terminal_state", return_value=None), \
             mock.patch("dispatch_seat_handover._effective", return_value=(sid, "codex")):
            resume()
            resume()
            materialize.assert_not_called()
            self.assertEqual(store.get(duty["id"])["observation"]["reason"],
                             "completion-delivery-record-unavailable")

            lease_is_held.return_value = False
            resume()
            materialize.assert_called_once()

    def test_shared_owner_retry_rejects_wrong_digest_recipient_and_revision(self):
        import dispatch_completion_join as join
        import dispatch_pending_delivery as pending

        original_root, original_jobs = self.tmp_root, self.jobs_path
        for field, value in (("receipt_digest", "f" * 64),
                             ("recipient_kind", "claude-parent-runtime"),
                             ("row_revision", "stale-revision")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                self.tmp_root = Path(temporary)
                self.jobs_path = self.tmp_root / "jobs.log"
                self.jobs_path.touch()
                with mock.patch.dict(os.environ, {
                        "AGENT_DISPATCH_JOBS": str(self.jobs_path),
                        "AGENT_PEER_LEDGER_ROOT": str(self.tmp_root)}):
                    store, duty, aid, sid = self._duty()
                    exact = pending.read(self.tmp_root, sid, "delivery-native-crash")
                    invalid = dict(exact, state="pending")
                    if field == "row_revision":
                        invalid["row_revisions"] = {aid: value}
                    else:
                        invalid[field] = value
                    read_calls = 0
                    materialized = False
                    def read_missing_then_materialized(*_args):
                        nonlocal read_calls
                        read_calls += 1
                        return None if read_calls == 1 or not materialized else invalid
                    def materialize_invalid_record(*_args):
                        nonlocal materialized
                        materialized = True
                    with mock.patch.object(join, "materialize_after_terminal_close",
                                           side_effect=materialize_invalid_record) as materialize, \
                         mock.patch("dispatch_seat_handover._effective",
                                    return_value=(sid, "codex")), \
                         mock.patch.object(pending, "read",
                                           side_effect=read_missing_then_materialized), \
                         mock.patch.object(pending, "claim_ready_batch") as deliver, \
                         mock.patch.object(peer_steward, "_resume_native_receiving_turn") as receive:
                        peer_steward._resume_registered_obligation(store.get(duty["id"]), store)
                    materialize.assert_called_once()
                    deliver.assert_not_called()
                    receive.assert_not_called()
                    self.assertEqual(store.get(duty["id"])["observation"]["reason"],
                                     "completion-delivery-record-unavailable")
        self.tmp_root, self.jobs_path = original_root, original_jobs

    def test_existing_exact_record_skips_missing_record_validation(self):
        import dispatch_completion_join as join

        store, duty, aid, sid = self._duty()
        row = join.current_attempt_row(self.jobs_path, aid)
        metadata = dict(row.metadata)
        metadata.pop("delivery_receipt_digest")
        metadata.pop("delivery_row_revision")
        row_without_delivery_metadata = SimpleNamespace(
            attempt_id=row.attempt_id, status=row.status, raw=row.raw, metadata=metadata)
        with mock.patch.object(join, "current_attempt_row",
                               return_value=row_without_delivery_metadata), \
             mock.patch("dispatch_seat_handover._effective", return_value=(sid, "codex")), \
             mock.patch.object(peer_steward, "_resume_native_receiving_turn",
                               return_value=False):
            peer_steward._resume_registered_obligation(duty, store)

        settled = store.get(duty["id"])
        self.assertEqual((settled["state"], settled["delivery"]), ("complete", "acked"))
        self.assertNotEqual(settled.get("observation", {}).get("reason"),
                            "completion-delivery-record-unavailable")

    def test_matching_terminal_tombstone_is_never_rematerialized(self):
        import dispatch_attempt_reference as attempt_reference
        import dispatch_completion_join as join
        import dispatch_pending_delivery as pending
        store, duty, _aid, sid = self._duty()
        path = pending.record_path(self.tmp_root, sid, "delivery-native-crash")
        exact = pending.read(self.tmp_root, sid, "delivery-native-crash")
        # Production writes a tombstone inside the removal's reference admission.
        with attempt_reference.admission(path.parents[2], pending.record_references(exact)):
            pending._write_prune_tombstone(path, exact)
        path.unlink()

        with mock.patch.object(join, "materialize_after_terminal_close") as materialize, \
             mock.patch("dispatch_seat_handover.effective_parent", return_value=sid):
            peer_steward._resume_registered_obligation(duty, store)
            peer_steward._resume_registered_obligation(store.get(duty["id"]), store)

        materialize.assert_not_called()
        self.assertIsNone(pending.read(self.tmp_root, sid, "delivery-native-crash"))
        current = store.get(duty["id"])
        self.assertEqual((current["state"], current["delivery"]), ("complete", "acked"))

    def _run_empty_native_recovery(self, request_context, native_items=()):
        import codex_queue_delivery
        import codex_queue_dispatch
        import dispatch_completion_join as join
        import dispatch_pending_delivery as pending
        store, duty, aid, sid = self._duty()
        if request_context is not None:
            binding = store.get(duty["id"])["observation"]["native_receiving_turn"]
            store.retain_native_receiving_turn(
                duty["id"], binding, request_context=request_context)
        # Codex 0.163 captured this full-turn shape in the recorded replay
        # fixture; only the post-crash status transition is reflected here.
        native_turn = {
            "completedAt": None, "durationMs": None, "error": None,
            "id": "turn-exact", "items": list(native_items), "itemsView": "full",
            "rootTurnId": "turn-exact", "startedAt": 1792389273,
            "status": "interrupted",
        }
        row = SimpleNamespace(attempt_id=aid, status="done", metadata={
            "attempt_id": aid, "parent_sid": sid, "delivery_id": "delivery-native-crash",
            "delivery_receipt_digest": store.get(duty["id"])["observation"][
                "native_receiving_turn"]["receipt_digest"],
            "delivery_row_revision": "rev-1", "route_id": "rt-native",
            "route_node": "execute", "parent_attempt_id": "att-owner",
        })
        exact = pending.read(self.tmp_root, sid, "delivery-native-crash")
        store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
        duty = store.get(duty["id"])
        with mock.patch.object(join, "materialize_after_terminal_close", return_value=None), \
             mock.patch.object(join, "current_attempt_row", return_value=row), \
             mock.patch.object(join, "pending_record_identity",
                               return_value=("rt-native", "execute", "att-owner")), \
             mock.patch("dispatch_seat_handover.effective_parent", return_value=sid), \
             split_patch("dispatch_contract.terminal_conflict_pending", return_value=False), \
             mock.patch.object(codex_queue_dispatch, "resolve_queue_socket", return_value=Path("/tmp/fixture.sock")), \
             mock.patch.object(codex_queue_delivery, "read_exact_turn",
                               side_effect=[native_turn, native_turn]) as read_turn, \
             mock.patch.object(codex_queue_delivery, "_latest_turn", return_value={"id": "turn-exact",
                                                                                  "status": "interrupted"}), \
             mock.patch.object(codex_queue_delivery, "_known_consumed", return_value=None), \
             mock.patch.object(codex_queue_delivery, "list_queue", return_value=[]), \
             mock.patch.object(codex_queue_delivery, "_latest_turn_status",
                               return_value="interrupted"), \
             mock.patch.object(codex_queue_delivery, "_rpc",
                               return_value={"queuedSubmission": {"id": "queue-item"}}) as rpc, \
             mock.patch.object(codex_queue_delivery, "_start_interrupted_owned_item",
                               return_value=False), \
             mock.patch("dispatch_completion_join.completion_followup_text", return_value="follow-through"):
            peer_steward._resume_native_receiving_turn(
                duty, store, row, exact, sid, sid)
        self.assertEqual(read_turn.call_count, 2, "the real sender must invoke before_add")
        self.assertEqual([call.args[1] for call in rpc.call_args_list], ["thread/queue/add"])
        message = rpc.call_args.args[2]["input"][0]["text"]
        client_id = rpc.call_args.args[2]["clientUserMessageId"]
        self.assertTrue(client_id.startswith("delivery-"))
        self.assertIn("turn-exact", message)
        current = store.get(duty["id"])
        self.assertEqual(current["state"], "pending")
        self.assertEqual(current["delivery"], "native-continuation-accepted")
        self.assertEqual(current["observation"]["continuation_source_turn"], "turn-exact")
        return current, message

    def test_empty_native_turn_resumes_with_exact_hook_request_through_real_sender(self):
        prompt = "Original task with exact requested format\nOUTPUT_FORMAT_CANARY: exact JSON"
        current, message = self._run_empty_native_recovery(prompt)
        self.assertIn("Original user request received by Codex UserPromptSubmit", message)
        self.assertIn(prompt, message)
        context = current["observation"]["native_request_context"]
        self.assertEqual(context["source"], "codex-userprompt-submit")
        self.assertEqual(context["status"], "available")
        self.assertEqual(context["text"], prompt)
        self.assertEqual(current["observation"]["continuation_source"]["source"],
                         "codex-userprompt-submit")
        self.assertIn(prompt, current["observation"]["continuation_prepared"]["message"])

    def test_empty_native_turn_resumes_and_records_unavailable_original_request(self):
        current, message = self._run_empty_native_recovery("")
        self.assertIn("original user request could not be recovered", message)
        self.assertNotIn("original task and requested format", message)
        self.assertEqual(current["observation"]["native_request_context"]["status"],
                         "unavailable")
        self.assertEqual(current["observation"]["continuation_source"], {
            "source": "unavailable", "status": "unavailable",
            "reason": "hook-prompt-empty-or-invalid",
        })
        self.assertEqual(current["delivery"], "native-continuation-accepted")

    def test_exact_native_user_message_remains_a_fallback_without_hook_context(self):
        items = [{"id": "user-item-exact", "type": "userMessage",
                  "content": [{"type": "text", "text": "legacy exact request"}]}]
        current, message = self._run_empty_native_recovery(None, native_items=items)
        self.assertIn("original userMessage item user-item-exact", message)
        self.assertIn("legacy exact request", message)
        self.assertEqual(current["observation"]["continuation_source"], {
            "source": "codex-native-userMessage", "status": "available",
            "item_ids": ["user-item-exact"],
        })

    def test_long_hook_request_uses_the_bound_durable_obligation_field(self):
        prompt = "x" * (peer_steward._NATIVE_QUEUE_MESSAGE_LIMIT + 1024)
        current, message = self._run_empty_native_recovery(prompt)
        self.assertLessEqual(len(message.encode("utf-8")),
                             peer_steward._NATIVE_QUEUE_MESSAGE_LIMIT)
        self.assertNotIn("x" * 256, message)
        self.assertIn("observation.native_request_context.text", message)
        self.assertIn(str(self.tmp_root), message)
        self.assertEqual(current["observation"]["native_request_context"]["text"], prompt)

    def test_original_request_is_verbatim_at_utf8_limit_and_referenced_above_it(self):
        marker = "\nOUTPUT_FORMAT_CANARY: exact JSON object\n"
        item_id = "user-item-final-format"
        source = [{"item_id": item_id, "text": ""}]
        empty = peer_steward._native_continuation_message(
            thread_id="thread-exact", turn_id="turn-exact",
            source_turn_id="turn-exact", receipt_text="{}",
            followup="approved follow-through", source_messages=source)
        budget = peer_steward._NATIVE_QUEUE_MESSAGE_LIMIT - len(empty.encode("utf-8"))
        marker_size = len(marker.encode("utf-8"))
        fill_bytes = budget - marker_size
        text = "한" * (fill_bytes // 3) + "x" * (fill_bytes % 3) + marker
        source[0]["text"] = text
        exact = peer_steward._native_continuation_message(
            thread_id="thread-exact", turn_id="turn-exact",
            source_turn_id="turn-exact", receipt_text="{}",
            followup="approved follow-through", source_messages=source)
        self.assertEqual(len(exact.encode("utf-8")), peer_steward._NATIVE_QUEUE_MESSAGE_LIMIT)
        self.assertTrue(exact.endswith(text))
        self.assertIn("OUTPUT_FORMAT_CANARY: exact JSON object", exact)

        source[0]["text"] = text + "한"
        bounded = peer_steward._native_continuation_message(
            thread_id="thread-exact", turn_id="turn-exact",
            source_turn_id="turn-exact", receipt_text="{}",
            followup="approved follow-through", source_messages=source)
        self.assertLessEqual(len(bounded.encode("utf-8")),
                             peer_steward._NATIVE_QUEUE_MESSAGE_LIMIT)
        self.assertNotIn(text, bounded)
        self.assertIn(item_id, bounded)
        self.assertIn("exact userMessage item(s)", bounded)
        self.assertIn("every requested content and output-format requirement", bounded)

    def test_unreadable_original_user_message_remains_unresolved(self):
        # Captured Codex 0.163 turns can be full-view while having no items.
        self.assertEqual(peer_steward._native_user_message_sources({
            "id": "turn-exact", "status": "interrupted", "items": [],
            "itemsView": "full"}), [])

    def test_queued_continuation_keeps_same_duty_and_does_not_resend(self):
        import codex_queue_delivery
        import codex_queue_dispatch
        import dispatch_completion_join as join
        store, duty, aid, sid = self._duty()
        duty = store.update(duty["id"], observation={**duty["observation"],
            "continuation_id": "delivery-stable", "continuation_source_turn": "turn-exact"})
        with mock.patch.object(join, "materialize_after_terminal_close", return_value=None), \
             mock.patch.object(join, "pending_record_identity", return_value=("rt-native", "execute", "att-owner")), \
             mock.patch("dispatch_seat_handover.effective_parent", return_value=sid), \
             mock.patch.object(codex_queue_dispatch, "resolve_queue_socket", return_value=Path("/tmp/fixture.sock")), \
             mock.patch.object(codex_queue_delivery, "read_exact_turn", return_value={"id": "turn-exact", "status": "interrupted"}), \
             mock.patch.object(codex_queue_delivery, "find_turn_by_client_message_id", return_value=None), \
             mock.patch.object(codex_queue_delivery, "list_queue", return_value=[{"clientUserMessageId": "delivery-stable"}]), \
             mock.patch.object(codex_queue_delivery, "send_at_least_once") as send:
            peer_steward._reconcile_obligations(store, [duty])
        send.assert_not_called()
        self.assertEqual(store.get(duty["id"])["state"], "pending")
        self.assertEqual(store.get(duty["id"])["observation"]["continuation_id"], "delivery-stable")

    def test_live_or_unknown_turn_never_starts_a_competing_continuation(self):
        import codex_queue_delivery
        import codex_queue_dispatch
        import dispatch_completion_join as join
        store, duty, _aid, sid = self._duty()
        with mock.patch.object(join, "materialize_after_terminal_close", return_value=None), \
             mock.patch.object(join, "pending_record_identity",
                               return_value=("rt-native", "execute", "att-owner")), \
             mock.patch("dispatch_seat_handover.effective_parent", return_value=sid), \
             mock.patch.object(codex_queue_dispatch, "resolve_queue_socket",
                               return_value=Path("/tmp/fixture.sock")), \
             mock.patch.object(codex_queue_delivery, "read_exact_turn", side_effect=[
                 {"id": "turn-exact", "status": "inProgress"},
                 {"id": "turn-exact", "status": "unknown"}]), \
             mock.patch.object(codex_queue_delivery, "send_at_least_once") as send:
            peer_steward._reconcile_obligations(store, [duty])
            peer_steward._reconcile_obligations(store, [store.get(duty["id"])])
        send.assert_not_called()
        self.assertNotEqual(store.get(duty["id"])["state"], "complete")


class NativeReceivingTurnSettlementTests(unittest.TestCase):
    """A native turn settles only its delivery duty; it does not mutate route work."""

    def _fixture(self, root: Path, *, binding=True):
        import dispatch_pending_delivery as pending
        jobs = root / "jobs.log"
        jobs.write_text("fixture registry remains unchanged\n", encoding="utf-8")
        metadata = {
            "attempt_id": "att-native-receive", "route_id": "rt-native-receive",
            "route_node": "execute", "parent_attempt_id": "-",
            "delivery_id": "delivery-native-receive", "delivery_receipt_digest": "digest-native",
            "delivery_row_revision": "revision-native", "harness": "codex",
        }
        row = SimpleNamespace(attempt_id="att-native-receive", status="done", metadata=metadata)
        observation = {}
        if binding:
            observation["native_receiving_turn"] = {
                "thread_id": "native-parent", "recipient_sid": "native-parent",
                "storage_recipient": "native-parent", "delivery_id": metadata["delivery_id"],
                "receipt_digest": metadata["delivery_receipt_digest"],
                "row_revision": metadata["delivery_row_revision"], "turn_id": "turn-exact",
            }
        duty = {"id": "registered-batch-test", "intent": {"identity": {"jobs": str(jobs)}},
                "observation": observation, "state": "pending"}
        receipt = {"children": [{"attempt_id": row.attempt_id, "status": "done",
                                 "required_action": "advance-completed"}]}
        exact = {
            "state": "acked", "receipt": receipt, "receipt_digest": "digest-native",
            "row_revisions": {row.attempt_id: "revision-native"},
            "acked_by": "codex-native-prompt:turn-exact",
        }
        workflow = {"state": "open", "declared_next_work": ["impl-review"]}
        next_notice_receipt = {"kind": "supervision", "reason": "new-work-remains"}
        next_notice = pending.create(
            root, recipient_kind="codex-native-queue", recipient_key="native-parent",
            delivery_id="delivery-next-work", session_generation="",
            session_generation_supported="0", attempt_ids=["att-next-work"],
            parent_attempt_id="att-owner", route_id="rt-next", route_node="impl-review",
            receipt=next_notice_receipt,
            receipt_digest=pending._canonical_receipt_digest(next_notice_receipt),
            row_revisions={"att-next-work": "revision-next"})
        before_registry = jobs.read_bytes()
        return pending, jobs, row, duty, exact, workflow, next_notice, before_registry

    def test_completed_live_ack_only_unknown_and_interrupted_turns_stay_separate_from_workflow(self):
        import dispatch_completion_join as join
        import codex_queue_delivery
        import codex_queue_dispatch
        import dispatch_pending_delivery as pending

        for status, expected_state in (("completed", "complete"),
                                       ("inProgress", "pending"),
                                       ("future-status", "unknown")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                (fixture_pending, jobs, row, duty, exact, workflow, next_notice,
                 before_registry) = self._fixture(Path(temporary))
                store_updates = []

                class Store:
                    def update(_self, _duty_id, **changes):
                        store_updates.append(changes)
                        duty.update(changes)
                        return duty

                with mock.patch.object(fixture_pending, "read", return_value=exact), \
                     mock.patch.object(join, "pending_record_identity",
                                       return_value=("rt-native-receive", "execute", "-")), \
                     mock.patch.object(fixture_pending, "received_ack_identity", return_value={
                         "route_id": "rt-native-receive", "route_node": "execute",
                         "parent_attempt_id": "-"}), \
                     mock.patch.object(codex_queue_dispatch, "resolve_queue_socket",
                                       return_value=Path(temporary) / "queue.sock"), \
                     mock.patch.object(codex_queue_delivery, "read_exact_turn",
                                       return_value={"id": "turn-exact", "status": status}), \
                     mock.patch.object(codex_queue_delivery, "send_at_least_once") as send:
                    self.assertTrue(peer_steward._resume_native_receiving_turn(
                        duty, Store(), row, exact, "native-parent", "native-parent"))

                self.assertEqual(duty["state"], expected_state)
                self.assertEqual(len(store_updates), 1)
                send.assert_not_called()
                self.assertEqual(jobs.read_bytes(), before_registry)
                self.assertEqual(workflow, {"state": "open", "declared_next_work": ["impl-review"]})
                next_after = fixture_pending.read(Path(temporary), "native-parent", "delivery-next-work")
                self.assertEqual(next_after["state"], "pending")
                self.assertEqual(next_after["receipt"], next_notice["receipt"])

        with tempfile.TemporaryDirectory() as temporary:
            (fixture_pending, _jobs, row, duty, exact, _workflow, _next_notice,
             _before_registry) = self._fixture(Path(temporary), binding=False)

            class Store:
                def update(_self, _duty_id, **changes):
                    duty.update(changes)
                    return duty

            with mock.patch.object(fixture_pending, "read") as read, \
                 mock.patch.object(codex_queue_delivery, "send_at_least_once") as send:
                self.assertTrue(peer_steward._resume_native_receiving_turn(
                    duty, Store(), row, exact, "native-parent", "native-parent"))
            self.assertEqual(duty["state"], "unknown")
            self.assertEqual(duty["observation"]["reason"], "native-receiving-turn-binding-unknown")
            read.assert_not_called()
            send.assert_not_called()

    def test_interrupted_turn_reuses_one_stable_continuation(self):
        import dispatch_completion_join as join
        import codex_queue_delivery
        import codex_queue_dispatch
        import dispatch_pending_delivery as pending

        with tempfile.TemporaryDirectory() as temporary:
            (fixture_pending, _jobs, row, duty, exact, _workflow, _next_notice,
             _before_registry) = self._fixture(Path(temporary))

            class Store:
                def update(_self, _duty_id, **changes):
                    duty.update(changes)
                    return duty

                def prepare_native_continuation(_self, _duty_id, *, continuation_id,
                                                source_turn_id, source, message):
                    prepared = {"id": continuation_id, "source_turn_id": source_turn_id,
                                "message": message}
                    duty["observation"].update({
                        "continuation_id": continuation_id,
                        "continuation_source_turn": source_turn_id,
                        "continuation_source": source,
                        "continuation_prepared": prepared,
                    })
                    return duty, prepared

            send = mock.Mock(return_value={"status": "queued"})
            with mock.patch.object(fixture_pending, "read", return_value=exact), \
                 mock.patch.object(join, "pending_record_identity",
                                   return_value=("rt-native-receive", "execute", "-")), \
                 mock.patch.object(fixture_pending, "received_ack_identity", return_value={
                     "route_id": "rt-native-receive", "route_node": "execute",
                     "parent_attempt_id": "-"}), \
                 mock.patch.object(codex_queue_dispatch, "resolve_queue_socket",
                                   return_value=Path(temporary) / "queue.sock"), \
                 mock.patch.object(codex_queue_delivery, "read_exact_turn",
                                   return_value={"id": "turn-exact", "status": "interrupted",
                                                 "items": [{"id": "user-item-exact", "type": "userMessage",
                                                            "content": [{"type": "text",
                                                                         "text": "original exact request"}]}]}), \
                 mock.patch.object(codex_queue_delivery, "_latest_turn",
                                   return_value={"id": "turn-exact", "status": "interrupted"}), \
                 mock.patch.object(codex_queue_delivery, "find_turn_by_client_message_id",
                                   return_value=None), \
                 mock.patch.object(codex_queue_delivery, "list_queue", return_value=[]), \
                 mock.patch.object(codex_queue_delivery, "send_at_least_once", send), \
                 mock.patch.object(join, "completion_followup_text", return_value="approved follow-through"):
                self.assertTrue(peer_steward._resume_native_receiving_turn(
                    duty, Store(), row, exact, "native-parent", "native-parent"))
                continuation_id = duty["observation"]["continuation_id"]
                self.assertTrue(continuation_id.startswith("delivery-"))
                codex_queue_delivery.list_queue.return_value = [
                    {"clientUserMessageId": continuation_id}]
                self.assertTrue(peer_steward._resume_native_receiving_turn(
                    duty, Store(), row, exact, "native-parent", "native-parent"))

            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.kwargs["client_message_id"], continuation_id)
            self.assertEqual(duty["state"], "pending")
            self.assertEqual(duty["observation"]["continuation_source_turn"], "turn-exact")


class NativeDispatchReceiptTest(unittest.TestCase):
    def test_peer_courier_settles_only_verified_claude_session_feedback(self):
        import dispatch_pending_delivery as pending
        import dispatch_recipient_receipt as recipient_receipt
        import claude_project_dir

        with tempfile.TemporaryDirectory(prefix="peer-native-receipt-") as tmp:
            base = Path(tmp)
            root = base / "dispatch"
            cwd = base / "repo"
            cwd.mkdir()
            runtime_home = base / "claude-home"
            sid = "claude-parent-session"
            did = "delivery-" + "e" * 64
            body = {"schema_version": 1, "kind": "supervision",
                    "owner_attempt_id": "att-owner", "reason": "waiting"}
            record = pending.create(
                root, recipient_kind="claude-parent-runtime", recipient_key=sid,
                delivery_id=did, session_generation="", session_generation_supported="0",
                attempt_ids=["att-owner"], parent_attempt_id="att-owner",
                route_id="route-peer", route_node="execute", receipt=body,
                receipt_digest=pending._canonical_receipt_digest(body),
                row_revisions={"att-owner": "revision-1"})
            pending.claim(root, sid, did, claim_owner="rewake", lease_seconds=30,
                          require_generation_proof=False)
            pending.mark_sent_ambiguous(root, sid, did, claim_owner="rewake")
            record = pending.read(root, sid, did)
            receipt_token = recipient_receipt.receipt_marker(record, sid)
            project = runtime_home / "projects" / claude_project_dir.encode_project_dir(str(cwd))
            project.mkdir(parents=True)
            transcript = project / f"{sid}.jsonl"
            transcript.write_text(json.dumps({
                "type": "user", "sessionId": sid, "summary": "Stop hook feedback",
                "message": {"content": receipt_token},
            }) + "\n", encoding="utf-8")
            row = SimpleNamespace(metadata={"parent_cwd": str(cwd)})
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(runtime_home)}):
                received = peer_steward._settle_claude_native_dispatch_receipt(
                    root, sid, row, sid, record)
            self.assertTrue(received)
            self.assertEqual(pending.read(root, sid, did)["state"], "acked")

    def test_registered_peer_courier_skips_new_transfer_after_exact_native_receipt(self):
        from types import SimpleNamespace
        import dispatch_pending_delivery as pending

        with tempfile.TemporaryDirectory(prefix="peer-native-no-send-") as tmp:
            jobs = Path(tmp) / "jobs.log"
            jobs.touch()
            attempt, sid = "att-peer-native", "claude-peer-session"
            carrier = "claude-parent-runtime"
            metadata = {"parent_sid": sid, "parent_harness": "claude",
                        "parent_completion_delivery": carrier, "delivery_intent": "1",
                        "delivery_id": "delivery-" + "a" * 32,
                        "delivery_receipt_digest": "digest-native",
                        "delivery_row_revision": "revision-native",
                        "route_id": "route-native", "route_node": "execute"}
            row = SimpleNamespace(status="done", attempt_id=attempt, metadata=metadata)
            store = peer_steward.peer_obligations.ObligationStore(Path(tmp))
            identity = {"jobs": str(jobs), "attempt_id": attempt, "session_id": sid,
                        "harness": "claude", "server": "default"}
            duty = store.create("registered-batch-native-" + "a" * 20,
                                "registered-batch", identity, {"carrier": carrier})
            record = {"state": "pending", "delivery_id": metadata["delivery_id"],
                      "recipient_kind": carrier, "attempt_ids": [attempt],
                      "receipt_digest": metadata["delivery_receipt_digest"],
                      "row_revisions": {attempt: metadata["delivery_row_revision"]}}
            agents = {"result": {"agents": [{"agent": "claude", "pane_id": "w1:p9",
                      "agent_session": {"value": sid}}]}}
            with mock.patch("dispatch_completion_join.current_attempt_row", return_value=row), \
                 mock.patch("dispatch_completion_join.materialize_after_terminal_close"), \
                 mock.patch("dispatch_seat_handover.effective_parent", return_value=sid), \
                 mock.patch("dispatch_seat_handover.effective_parent_harness", return_value="claude"), \
                 mock.patch.object(pending, "read", return_value=record), \
                 mock.patch.object(pending, "received_ack_identity", return_value={"exact": True}), \
                 mock.patch.object(pending, "claim") as claim, \
                 mock.patch.object(peer_steward, "_settle_claude_native_dispatch_receipt",
                                   return_value=True) as received, \
                 mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                     stdout=json.dumps(agents), returncode=0)), \
                 mock.patch.object(peer_steward, "_resolve_target", return_value=("claude", sid)), \
                 mock.patch.object(peer_steward, "cmd_prompt") as send:
                peer_steward._resume_registered_obligation(store.get(duty["id"]), store)
            received.assert_called_once()
            claim.assert_not_called()
            send.assert_not_called()
            self.assertEqual(store.get(duty["id"])["state"], "pending")

    def test_registered_peer_courier_binds_projected_completion_to_its_stored_identity(self):
        from types import SimpleNamespace
        import dispatch_pending_delivery as pending
        import dispatch_session_sweep as sweep

        with tempfile.TemporaryDirectory(prefix="peer-native-projected-") as tmp:
            root = Path(tmp)
            jobs = root / "jobs.log"
            jobs.touch()
            attempt, sid = "att-peer-projected", "claude-peer-session"
            carrier = "claude-parent-runtime"
            did = "delivery-" + "b" * 32
            receipt = {"schema_version": 2, "state": "delivered", "job_registry": str(jobs),
                       "parent_attempt_id": "att-parent", "delivery_classification": "attention",
                       "children": [{"attempt_id": attempt, "status": "done", "readiness": "ready",
                                     "reason": "terminal-failure-or-unclosed",
                                     "required_action": "inspect-done-failure",
                                     "delivery_classification": "attention"}]}
            stored = pending.create(
                root, recipient_kind=carrier, recipient_key=sid, delivery_id=did,
                session_generation="", session_generation_supported="0",
                attempt_ids=[attempt], parent_attempt_id="att-parent",
                route_id="route-projected", route_node="execute", receipt=receipt,
                receipt_digest=pending._canonical_receipt_digest(receipt),
                row_revisions={attempt: "revision-projected"})
            metadata = {"parent_sid": sid, "parent_harness": "claude",
                        "parent_completion_delivery": carrier, "delivery_intent": "1",
                        "delivery_id": did, "delivery_receipt_digest": stored["receipt_digest"],
                        "delivery_row_revision": "revision-projected",
                        "route_id": "route-projected", "route_node": "execute"}
            row = _register_completion_fixture(jobs, attempt, metadata)
            store = peer_steward.peer_obligations.ObligationStore(root)
            identity = {"jobs": str(jobs), "attempt_id": attempt, "session_id": sid,
                        "harness": "claude", "server": "default"}
            duty = store.create(peer_steward.peer_obligations.stable_duty_id("registered-batch", identity),
                                "registered-batch", identity, {"carrier": carrier})

            def project(_root, _recipient, _delivery_id, record, _owner, jobs=None):
                record["receipt"] = dict(record["receipt"], delivery_classification="success",
                    children=[dict(child, reason="cancelled-by-parent",
                                   required_action="advance-completed",
                                   delivery_classification="success")
                              for child in record["receipt"]["children"]])
                return True

            sent_bodies = []
            def capture_prompt(args, **kwargs):
                sent_bodies.append(Path(args.body_file).read_text())
                return 0

            agents = {"result": {"agents": [{"agent": "claude", "pane_id": "w1:p9",
                      "agent_session": {"value": sid}}]}}
            with mock.patch("dispatch_notice_state.keep_claim", side_effect=project), \
                 mock.patch("dispatch_supervision.continue_for_parent", return_value=None), \
                 mock.patch.object(peer_steward, "_settle_claude_native_dispatch_receipt",
                                   return_value=False) as received, \
                 mock.patch.object(sweep, "delivery_context", wraps=sweep.delivery_context) as render, \
                 mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                     stdout=json.dumps(agents), returncode=0)), \
                 mock.patch.object(peer_steward, "_resolve_target", return_value=("claude", sid)), \
                 mock.patch.object(peer_steward, "cmd_prompt", side_effect=capture_prompt) as send:
                peer_steward._resume_registered_obligation(store.get(duty["id"]), store)
            send.assert_called_once()
            # Receipt evidence is checked against the stored record before and after the claim.
            self.assertEqual([call.args[4]["receipt"] for call in received.call_args_list],
                             [receipt, receipt])
            (rendered,) = render.call_args.args[0][0][1]
            self.assertEqual(rendered["receipt"]["children"][0]["required_action"],
                             "advance-completed")
            self.assertEqual(rendered[sweep.IDENTITY_KEY],
                             pending.received_ack_identity(stored, sid, did))
            current = pending.read(root, sid, did)
            self.assertEqual(current["receipt"], receipt)
            self.assertEqual(pending.received_ack_identity(current, sid, did),
                             pending.received_ack_identity(stored, sid, did))
            from dispatch_recipient_receipt import receipt_marker
            self.assertIn(receipt_marker(stored, sid), sent_bodies[0])
            self.assertIn("required_action=advance-completed", sent_bodies[0])


class RegisteredParentReceiptAfterSuccessorTest(unittest.TestCase):
    """C7: the registered parent already took the result; its successor SID must not get it again."""

    def setUp(self):
        import dispatch_pending_delivery as pending
        import dispatch_recipient_receipt as recipient_receipt
        import claude_project_dir
        self.pending, self.rr = pending, recipient_receipt
        self.tmp = tempfile.TemporaryDirectory(prefix="peer-native-successor-")
        base = Path(self.tmp.name)
        self.root = base / "dispatch"
        self.cwd = base / "repo"
        self.cwd.mkdir()
        self.runtime_home = base / "claude-home"
        self.project = self.runtime_home / "projects" / claude_project_dir.encode_project_dir(str(self.cwd))
        self.project.mkdir(parents=True)
        self.old, self.new = "old-parent-session", "new-parent-session"
        self.attempt = "att-owner-successor"
        self.did = "delivery-" + "c" * 32
        body = {"schema_version": 1, "kind": "supervision",
                "owner_attempt_id": self.attempt, "reason": "waiting"}
        pending.create(
            self.root, recipient_kind="claude-parent-runtime", recipient_key=self.old,
            delivery_id=self.did, session_generation="", session_generation_supported="0",
            attempt_ids=[self.attempt], parent_attempt_id=self.attempt,
            route_id="route-successor", route_node="execute", receipt=body,
            receipt_digest=pending._canonical_receipt_digest(body),
            row_revisions={self.attempt: "revision-1"})
        # The hook-native rewake claims, emits and leaves the record sent-ambiguous.
        pending.claim(self.root, self.old, self.did, claim_owner="claude-async-rewake:1:1",
                      lease_seconds=0.001, require_generation_proof=False)
        pending.mark_sent_ambiguous(self.root, self.old, self.did,
                                    claim_owner="claude-async-rewake:1:1")
        time.sleep(0.01)
        self.record = pending.read(self.root, self.old, self.did)
        self.marker = recipient_receipt.receipt_marker(self.record, self.old)
        self.row = SimpleNamespace(metadata={"parent_cwd": str(self.cwd), "parent_harness": "claude"})

    def tearDown(self):
        self.tmp.cleanup()

    def _native(self, sid, text, **over):
        event = {"type": "user", "sessionId": sid,
                 "origin": {"kind": "task-notification", "producer": "session-task"},
                 "promptSource": "system", "turnOrigin": "task_notification",
                 "message": {"content": ("<task-notification><task-id>t</task-id>"
                                         "<summary>Stop hook feedback</summary>" + text +
                                         "</task-notification>")}}
        event.update(over)
        return event

    def _write(self, sid, *events):
        (self.project / f"{sid}.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

    def _settle(self, row=None):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.runtime_home)}):
            return peer_steward._settle_claude_native_dispatch_receipt(
                self.root, self.old, row or self.row, self.new, self.record)

    def _state(self):
        return self.pending.read(self.root, self.old, self.did)["state"]

    def test_exact_system_event_in_registered_parent_transcript_settles_for_successor(self):
        self._write(self.old, self._native(self.old, self.marker))
        self._write(self.new, {"type": "user", "sessionId": self.new,
                               "message": {"content": "fresh after /clear"}})
        self.assertTrue(self._settle())
        self.assertEqual(self._state(), "acked")

    def test_only_exact_system_origin_evidence_settles(self):
        other_digest = "f" * 64
        wrong_delivery = "HEARTING_RECEIPT_V1 delivery-" + "d" * 32 + " " + self.marker.split()[-1]
        wrong_digest = f"HEARTING_RECEIPT_V1 {self.did} {other_digest}"
        cases = {
            "no-marker": [self._native(self.old, "nothing to see")],
            "other-delivery": [self._native(self.old, wrong_delivery)],
            "other-digest": [self._native(self.old, wrong_digest)],
            "model-authored": [{"type": "assistant", "sessionId": self.old,
                                "message": {"content": self.marker}}],
            "user-typed": [self._native(self.old, self.marker, promptSource="user")],
            "foreign-session": [self._native("someone-else", self.marker)],
        }
        for name, events in cases.items():
            with self.subTest(case=name):
                self._write(self.old, *events)
                self.assertFalse(self._settle())
                self.assertEqual(self._state(), "sent-ambiguous")

    def test_registered_parent_is_consulted_only_when_it_is_a_claude_parent(self):
        self._write(self.old, self._native(self.old, self.marker))
        other = SimpleNamespace(metadata={"parent_cwd": str(self.cwd), "parent_harness": "codex"})
        self.assertFalse(self._settle(other))
        self.assertEqual(self._state(), "sent-ambiguous")

    def test_real_row_shape_without_parent_harness_settles_for_successor(self):
        # Real registry rows omit parent_harness; the harness comes from
        # main_runtime_harness / the completion carrier.
        self._write(self.old, self._native(self.old, self.marker))
        real = SimpleNamespace(metadata={
            "parent_cwd": str(self.cwd), "main_runtime_harness": "claude",
            "parent_completion_delivery": "claude-parent-runtime"})
        self.assertNotIn("parent_harness", real.metadata)
        self.assertTrue(self._settle(real))
        self.assertEqual(self._state(), "acked")

    def test_real_row_shape_with_carrier_only_settles_for_successor(self):
        self._write(self.old, self._native(self.old, self.marker))
        real = SimpleNamespace(metadata={
            "parent_cwd": str(self.cwd),
            "parent_completion_delivery": "claude-parent-runtime"})
        self.assertTrue(self._settle(real))
        self.assertEqual(self._state(), "acked")

    def test_codex_parent_real_row_shape_does_not_consult_registered_parent(self):
        self._write(self.old, self._native(self.old, self.marker))
        codex = SimpleNamespace(metadata={
            "parent_cwd": str(self.cwd), "main_runtime_harness": "codex",
            "parent_completion_delivery": "codex-native-queue"})
        self.assertNotIn("parent_harness", codex.metadata)
        self.assertFalse(self._settle(codex))
        self.assertEqual(self._state(), "sent-ambiguous")

    def _run_courier(self, *events):
        carrier = "claude-parent-runtime"
        jobs = self.root / "jobs.log"
        jobs.touch()
        metadata = {"parent_sid": self.old, "parent_harness": "claude",
                    "parent_completion_delivery": carrier, "delivery_intent": "1",
                    "delivery_id": self.did,
                    "delivery_receipt_digest": self.record["receipt_digest"],
                    "delivery_row_revision": "revision-1",
                    "route_id": "route-successor", "route_node": "execute",
                    "parent_cwd": str(self.cwd)}
        row = SimpleNamespace(status="done", attempt_id=self.attempt, metadata=metadata)
        store = peer_steward.peer_obligations.ObligationStore(Path(self.tmp.name))
        identity = {"jobs": str(jobs), "attempt_id": self.attempt, "session_id": self.old,
                    "harness": "claude", "server": "default"}
        duty = store.create("registered-batch-successor-" + "a" * 20,
                            "registered-batch", identity, {"carrier": carrier})
        agents = {"result": {"agents": [{"agent": "claude", "pane_id": "w1:p9",
                  "agent_session": {"value": self.new}}]}}
        self._write(self.old, *events)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.runtime_home)}), \
             mock.patch("dispatch_completion_join.current_attempt_row", return_value=row), \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close"), \
             mock.patch("dispatch_seat_handover._effective", return_value=(self.new, "claude")), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                 stdout=json.dumps(agents), returncode=0)), \
             mock.patch.object(peer_steward, "_resolve_target", return_value=("claude", self.new)), \
             mock.patch.object(peer_steward, "_schedule_message_obligation"), \
             mock.patch.object(peer_steward, "cmd_prompt", return_value=0) as send:
            peer_steward._resume_registered_obligation(store.get(duty["id"]), store)
        return send, self.pending.read(self.root, self.old, self.did)

    def test_registered_courier_does_not_redeliver_to_successor_after_parent_took_it(self):
        send, record = self._run_courier(self._native(self.old, self.marker))
        send.assert_not_called()
        self.assertEqual(record["state"], "acked")
        self.assertEqual(record["attempts"], 1)  # no new claim by the courier

    def test_registered_courier_still_delivers_to_live_successor_without_evidence(self):
        stop = self.pending.PendingDeliveryError("pending-delivery-claim-refused", "test-stop")
        with mock.patch.object(self.pending, "claim_ready_batch", side_effect=stop) as claim:
            send, record = self._run_courier(self._native(self.old, "no receipt marker here"))
        # Past the receipt check, the courier goes on to claim it for the live successor.
        claim.assert_called_once()
        self.assertNotEqual(record["state"], "acked")


class WatchReceiptLockBudgetTest(_TmpRootMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from dispatch_contract import process_start_ticks
        import session_tidy

        self.server = "receipt-lock-server"
        self.socket_path = str(self.tmp_root / "herdr.sock")
        self.pane = "w1:p55"
        self.watch_id = "a1b2c3d4e5f60718"
        self.watch_root = Path(peer_steward._watch_root())
        self.watch_root.mkdir(parents=True, exist_ok=True)
        self.paths = peer_steward._watch_paths(self.watch_id, self.watch_root)
        self.start = str(process_start_ticks(os.getpid()))
        self.identity = {
            "server": self.server, "endpoint": self.socket_path,
            "pane": self.pane, "harness": "claude", "session_id": "target-session",
            "shell_pid": os.getpid(), "shell_start": self.start,
            "native_state": "idle", "purpose": "completion",
        }
        self.steward_sid = "receipt-lock-steward"
        steward_seat = session_tidy.Seat(
            "pane", "receipt-lock-steward-seat", "w1:p56", "claude",
            sid=self.steward_sid, endpoint=self.socket_path).as_dict()
        self.arm = {
            "watch_id": self.watch_id, "target": "target-agent", "server": self.server,
            "armed_ts": "2026-10-21T00:00:00Z", "agent": {
                "pane": self.pane, "harness": "claude", "session_id": "target-session",
                "name": "target-agent",
            },
            "pane_identity": {"shell_pid": os.getpid(), "shell_start": self.start},
            "steward": {"harness": "claude", "session_id": self.steward_sid,
                        "project": str(self.tmp_root)},
            "steward_seat": steward_seat,
            "watcher": {"pid": os.getpid(), "pid_start": self.start},
            "refs": [],
        }
        self.native_wait = {
            "state": "result", "agent_state": "idle", "agent": self.arm["agent"],
            "reason": "native-idle", "done_ts": "2026-10-21T00:00:01Z",
        }
        self.token = "receipt-lock-request-token"
        self.observation = {
            "watch_id": self.watch_id, "state": "pending",
            "native_wait": self.native_wait,
            "readiness_transition": {
                "token": self.token, "identity": self.identity,
                "consumer": {"pid": os.getpid(), "pid_start": self.start},
                "requested_at": time.time(),
            },
        }
        peer_steward._write_json_atomic(self.paths.arm, self.arm)
        peer_steward._write_json_atomic(self.paths.observation, self.observation)
        self.store = peer_steward.peer_obligations.ObligationStore()
        peer_steward.shared_readiness.publish_runner_incarnation(
            self.store.root, os.getpid(), self.start, "receipt-lock-incarnation")
        self._publish_transition_response()
        self.receipt = {
            "schema_version": 1, "watch_id": self.watch_id,
            "target": "target-agent", "server": self.server,
            "steward": self.arm["steward"], "armed_ts": self.arm["armed_ts"],
            "done_ts": "2026-10-21T00:00:02Z", "state": "idle",
            "agent": self.arm["agent"], "herdr_exit": 0,
            "watcher": self.arm["watcher"], "rearmed_from": None, "refs": [],
        }

    def _publish_transition_response(self):
        from dispatch_registry_cache import registry_view

        peer_steward.shared_readiness.publish_watch_transition(
            self.store.root, self.watch_id, self.identity, self.token,
            {"state": "ready", "reason": "native-turn-ready"},
            producer_pid=os.getpid(), producer_start=self.start,
            incarnation="receipt-lock-incarnation",
            generation=peer_steward.shared_readiness.registry_generation(
                self.jobs_path, registry_view(self.jobs_path), selected=True),
            lineage={"shell_pid": os.getpid(), "shell_start": self.start},
            authority={"rows": [], "handover_keys": []})

    @contextlib.contextmanager
    def _holder(self, mode, *, hold_seconds=0.08):
        ctx = multiprocessing.get_context("fork")
        ready, jobs_released, release = ctx.Event(), ctx.Event(), ctx.Event()
        process = ctx.Process(
            target=_hold_receipt_transition_locks,
            args=(mode, str(self.jobs_path) + ".lock", self.pane, self.server,
                  self.socket_path, ready, jobs_released, release, hold_seconds))
        process.start()
        try:
            self.assertTrue(ready.wait(3), f"{mode} writer did not acquire its lock")
            yield jobs_released
        finally:
            release.set()
            process.join(3)
            if process.is_alive():
                process.terminate()
                process.join(2)
            self.assertEqual(process.exitcode, 0, f"{mode} writer exit={process.exitcode}")

    def _publish_receipt(self, *, deadline=None, readiness=None):
        readiness = readiness or SimpleNamespace(state="ready", reason="native-turn-ready")
        readiness_patch = ({"side_effect": readiness} if callable(readiness)
                           else {"return_value": readiness})
        with mock.patch.object(peer_steward, "_watch_readiness_identity",
                               return_value=self.identity), \
                mock.patch.object(peer_steward.herdr_observation,
                                  "socket_path_for_endpoint", return_value=self.socket_path), \
                mock.patch.object(peer_steward, "_watch_transition_readiness",
                                  **readiness_patch):
            return peer_steward._watch_publish_ready_receipt(
                "target-agent", "idle", self.arm,
                {"pane": self.pane, "harness": "claude", "session_id": "target-session"},
                self.server, self.watch_id, self.paths, self.receipt, deadline=deadline)

    def _probe_jobs_writer_progress(self):
        fd = os.open(str(self.jobs_path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fail("jobs writer remained blocked after the seat acquisition failed")
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _runner_pass(self, wake):
        from peer_read_context import PeerReadContext

        readiness_identity = {
            "server": self.server, "pane": self.pane, "harness": "claude",
            "session_id": "target-session",
            "pid_birth": f"{os.getpid()}:{self.start}",
        }
        def ready(*_args, authority_out=None, **_kwargs):
            if authority_out is not None:
                authority_out.update(rows=[], handover_keys=[])
            return SimpleNamespace(state="ready", reason="native-turn-ready",
                                   identity=tuple(readiness_identity.items()))
        transport = SimpleNamespace(observe=lambda: object(), close=lambda: None)
        snapshot = {"watch_root": self.watch_root, "watches": ((self.watch_id, self.arm),)}
        with mock.patch.object(peer_steward, "_watch_readiness_identity",
                               return_value=self.identity), \
                mock.patch.object(peer_steward, "_runner_readiness_transport",
                                  return_value=transport), \
                mock.patch.object(peer_steward, "_target_from_readiness_view",
                                  return_value=("idle", self.arm["agent"], 0, "")), \
                mock.patch.object(peer_steward, "_retire_pane_info",
                                  return_value={"shell_pid": os.getpid(),
                                                "foreground_processes": []}), \
                mock.patch.object(peer_steward, "_pane_readiness", side_effect=ready), \
                patch_split(peer_steward, "_wake_watch_observer", wake), \
                mock.patch.object(peer_steward.shared_readiness,
                                  "publish_target_projection", return_value=False):
            return peer_steward._publish_watch_readiness(
                self.store, snapshot,
                read_context=PeerReadContext(registry_append_only=True),
                incarnation="receipt-lock-incarnation")

    def test_authority_lock_uses_selected_dispatch_root_when_peer_storage_differs(self):
        dispatch_jobs = self.tmp_root / "dispatch" / "jobs.log"
        dispatch_jobs.parent.mkdir()
        peer_root = self.tmp_root / "peer-state"
        peer_root.mkdir()
        os.environ["AGENT_DISPATCH_JOBS"] = str(dispatch_jobs)
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(peer_root)

        peer_lock = peer_root / "jobs.log.lock"
        peer_fd = os.open(peer_lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(peer_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with mock.patch.object(peer_steward.herdr_observation,
                                   "socket_path_for_endpoint", return_value=self.socket_path):
                with peer_steward._watch_transition_authority_lock(self.identity, self.server):
                    selected_lock = Path(str(dispatch_jobs) + ".lock")
                    self.assertTrue(selected_lock.is_file())
                    selected_probe = os.open(selected_lock, os.O_RDWR)
                    try:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(selected_probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    finally:
                        os.close(selected_probe)
                    peer_probe = os.open(peer_lock, os.O_RDWR)
                    try:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(peer_probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    finally:
                        os.close(peer_probe)
        finally:
            fcntl.flock(peer_fd, fcntl.LOCK_UN)
            os.close(peer_fd)

        selected_fd = os.open(str(dispatch_jobs) + ".lock", os.O_RDWR)
        try:
            fcntl.flock(selected_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(selected_fd, fcntl.LOCK_UN)
            os.close(selected_fd)

    def test_jobs_lock_contention_releases_to_writer_and_receipt_completes(self):
        with self._holder("jobs", hold_seconds=0.08):
            result = self._publish_receipt()
        self.assertTrue(result)
        self.assertEqual(peer_steward._read_json(self.paths.receipt)["state"], "idle")

    def test_seat_contention_is_bounded_and_releases_partial_jobs_ownership(self):
        with self._holder("seat"):
            result = self._publish_receipt()
            self.assertEqual(result, peer_steward._WATCH_RECEIPT_CONTENDED)
            self._probe_jobs_writer_progress()
        self.assertFalse(self.paths.receipt.exists())

    def test_jobs_then_seat_share_one_remaining_budget(self):
        import dispatch_seat_handover

        budget = peer_steward._WATCH_TRANSITION_LOCK_BUDGET_SEC
        scheduler_delay = budget * 0.8

        class _LocalClock:
            """Inject a scheduler pause without patching process-global time."""
            def __init__(self):
                self.now = 10.0
                self.deadline = None
                self.delay_after_jobs_lock = False
                self.delay_applied = False

            def monotonic(self):
                if self.deadline is None:
                    self.deadline = self.now + budget
                if self.delay_after_jobs_lock:
                    self.now += scheduler_delay
                    self.delay_after_jobs_lock = False
                    self.delay_applied = True
                return self.now

            def __getattr__(self, name):
                return getattr(time, name)

        clock = _LocalClock()
        real_fcntl = peer_steward.fcntl

        class _LocalFcntl:
            """Advance only peer-steward's clock after its jobs flock succeeds."""
            def flock(self, fd, operation):
                result = real_fcntl.flock(fd, operation)
                if (operation & real_fcntl.LOCK_EX
                        and operation & real_fcntl.LOCK_NB):
                    clock.delay_after_jobs_lock = True
                return result

            def __getattr__(self, name):
                return getattr(real_fcntl, name)

        real_seat_lock = dispatch_seat_handover.readiness_transition_lock
        with self._holder("seat"):
            with patch_split(peer_steward, "time", clock), \
                    patch_split(peer_steward, "fcntl", _LocalFcntl()), \
                    mock.patch.object(
                        dispatch_seat_handover, "readiness_transition_lock",
                        wraps=real_seat_lock) as seat_lock:
                result = self._publish_receipt()
            self.assertEqual(result, peer_steward._WATCH_RECEIPT_CONTENDED)
            self.assertTrue(clock.delay_applied)
            seat_lock.assert_called_once()
            self.assertAlmostEqual(
                seat_lock.call_args.kwargs["wait"],
                clock.deadline - clock.now, places=6)
            self.assertAlmostEqual(
                seat_lock.call_args.kwargs["wait"], budget - scheduler_delay,
                places=6)
            self._probe_jobs_writer_progress()
        self.assertFalse(self.paths.receipt.exists())

    def test_watch_deadline_during_lock_acquisition_uses_terminal_timeout_receipt(self):
        with self._holder("jobs", hold_seconds=0.4):
            deadline = time.monotonic() + 0.07
            started = time.monotonic()
            result = self._publish_receipt(deadline=deadline)
            elapsed = time.monotonic() - started
        self.assertEqual(result, peer_steward._WATCH_RECEIPT_EXPIRED)
        self.assertLess(elapsed, 0.18)
        self.assertFalse(self.paths.receipt.exists())
        terminal = peer_steward._write_terminal_watch_receipt(
            self.watch_id, self.arm, "watch-deadline")
        self.assertEqual((terminal["state"], terminal["reason"]),
                         ("timeout", "watch-deadline"))

    def test_watch_deadline_during_fresh_authority_blocks_late_receipt(self):
        deadline = time.monotonic() + 0.04
        def late_ready(*_args, **_kwargs):
            time.sleep(0.07)
            return SimpleNamespace(state="ready", reason="native-turn-ready")
        result = self._publish_receipt(deadline=deadline, readiness=late_ready)
        self.assertEqual(result, peer_steward._WATCH_RECEIPT_EXPIRED)
        self.assertFalse(self.paths.receipt.exists())
        terminal = peer_steward._write_terminal_watch_receipt(
            self.watch_id, self.arm, "watch-deadline")
        self.assertEqual((terminal["state"], terminal["reason"]),
                         ("timeout", "watch-deadline"))

    def test_real_500ms_deadline_keeps_timeout_after_valid_late_ready(self):
        arm = {**self.arm, "timeout": 500,
               "deadline": peer_steward._watch_deadline(500)}
        original_deadline = copy.deepcopy(arm["deadline"])
        self.assertIsNot(arm["deadline"], original_deadline)
        self.arm = arm
        peer_steward._write_json_atomic(self.paths.arm, arm)

        # One fixed real wait lets the original absolute deadline expire.
        time.sleep(0.55)
        self.assertEqual(peer_steward._watch_timeout_remaining_ms(arm), (0, "expired"))
        resolved_deadline = peer_steward._watch_monotonic_deadline(arm)
        self.assertEqual(resolved_deadline, original_deadline["monotonic_deadline"])
        self.assertEqual(arm["deadline"], original_deadline)

        # Publish a fresh protocol-valid response after expiry, then let the
        # production receipt edge enforce the arm's original deadline.
        self._publish_transition_response()
        late_ready = peer_steward.shared_readiness.watch_transition_result(
            self.store.root, self.watch_id, self.identity, self.token)
        self.assertIsNotNone(late_ready)
        self.assertEqual((late_ready["state"], late_ready["reason"]),
                         ("ready", "native-turn-ready"))
        self.assertEqual(late_ready["lineage"], {
            "shell_pid": os.getpid(), "shell_start": self.start})
        self.assertTrue(peer_steward.shared_readiness.valid_watch_authority(
            late_ready["authority"]))

        result = peer_steward._watch_publish_ready_receipt(
            "target-agent", "idle", arm,
            {"pane": self.pane, "harness": "claude", "session_id": "target-session"},
            self.server, self.watch_id, self.paths, self.receipt,
            deadline=resolved_deadline)
        self.assertEqual(result, peer_steward._WATCH_RECEIPT_EXPIRED)
        self.assertFalse(self.paths.receipt.exists())
        terminal = peer_steward._write_terminal_watch_receipt(
            self.watch_id, arm, "watch-deadline")
        self.assertEqual((terminal["state"], terminal["reason"]),
                         ("timeout", "watch-deadline"))

    def test_unchanged_runner_response_recovers_once_after_lock_contention(self):
        jobs_before = self.jobs_path.read_bytes()
        runner_wake_fd = peer_steward.shared_readiness.open_runner_wake(self.store.root)
        try:
            with self._holder("seat"):
                result = self._publish_receipt()
            self.assertEqual(result, peer_steward._WATCH_RECEIPT_CONTENDED)
            self.assertEqual(os.read(runner_wake_fd, 32), b"\0")
        finally:
            os.close(runner_wake_fd)
        pending = peer_steward._read_json(self.paths.observation)
        self.assertEqual(pending.get("native_wait"), self.native_wait)
        self.assertEqual(pending.get("readiness_transition", {}).get("token"), self.token)
        self.assertIsNone(peer_steward.shared_readiness.watch_transition_result(
            self.store.root, self.watch_id, self.identity, self.token))

        wake = mock.Mock(return_value=True)
        self.assertTrue(self._runner_pass(wake))
        self.assertEqual(wake.call_count, 1)
        response = peer_steward.shared_readiness.watch_transition_result(
            self.store.root, self.watch_id, self.identity, self.token)
        self.assertEqual(response["state"], "ready")
        self.assertTrue(self._runner_pass(wake))
        self.assertEqual(
            wake.call_count, 2,
            "an unchanged current response still needs notification while its request is pending")
        self.assertEqual(self.jobs_path.read_bytes(), jobs_before)

        result = self._publish_receipt()
        self.assertTrue(result)
        self.assertEqual(peer_steward._read_json(self.paths.receipt)["watch_id"], self.watch_id)
        peer_steward._prepare_watch_notice(self.watch_id, self.arm, self.receipt)
        peer_steward._record(
            to_harness="claude", to_name="target-agent", kind="notice", ref=[],
            receipt=self.watch_id, status="received",
            summary_text=f"[notice] watch {self.watch_id} state=idle target=target-agent",
            from_identity=(self.steward_sid, "claude", str(self.tmp_root)))
        notices = [row for row in self._all_records()
                   if row.get("kind") == "notice"
                   and (row.get("delivery") or {}).get("receipt") == self.watch_id]
        self.assertEqual(len(notices), 1)

        from dispatch_seat_handover import acknowledge_watch, watch_ack_valid
        self.assertEqual(acknowledge_watch(
            self.watch_id, "claude", self.steward_sid, "receipt-lock-recovery"), "created")
        self.assertTrue(watch_ack_valid(
            self.paths.ack, self.watch_id, "claude", self.steward_sid))
        receipt_bytes = self.paths.receipt.read_bytes()
        self.assertFalse(self._runner_pass(wake))
        self.assertEqual(self.paths.receipt.read_bytes(), receipt_bytes)
        self.assertEqual(acknowledge_watch(
            self.watch_id, "claude", self.steward_sid, "receipt-lock-reconnect"), "already")
        self.assertEqual(len([row for row in self._all_records()
                             if row.get("kind") == "notice"
                             and (row.get("delivery") or {}).get("receipt") == self.watch_id]), 1)
        self.assertEqual(wake.call_count, 2)

    def test_missing_initial_projection_ensures_the_existing_runner(self):
        observation = dict(self.observation)
        observation.pop("readiness_transition", None)
        peer_steward._write_json_atomic(self.paths.observation, observation)
        unavailable = SimpleNamespace(state="unknown", reason="shared-readiness-unavailable")
        with mock.patch.object(peer_steward, "_watch_readiness_identity",
                               return_value=self.identity), \
                mock.patch.object(peer_steward, "_watch_shared_readiness",
                                  return_value=unavailable), \
                mock.patch.object(peer_steward.shared_readiness, "wake_runner",
                                  return_value=False) as wake, \
                mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner",
                                  return_value=True) as ensure:
            result = peer_steward._watch_transition_readiness(
                "target-agent", "idle", self.arm,
                {"pane": self.pane, "harness": "claude", "session_id": "target-session"},
                self.server, self.watch_id, self.paths)
        self.assertEqual((result.state, result.reason),
                         ("pending", "shared-readiness-revalidating"))
        request = peer_steward._read_json(self.paths.observation)["readiness_transition"]
        self.assertEqual(request["identity"], self.identity)
        self.assertTrue(peer_steward.shared_readiness.consumer_is_current(
            {"consumer": request["consumer"]}))
        self.assertTrue(request["token"])
        wake.assert_called_once_with(self.store.root)
        ensure.assert_called_once_with(reconciliation_required=True)

    def test_stale_pending_write_keeps_new_consumer_and_contention_reset(self):
        previous = dict(self.observation)
        previous["readiness_transition"] = {
            "token": "dead-previous-consumer-token", "identity": self.identity,
            "consumer": {"pid": 2147483000, "pid_start": "1"},
            "requested_at": time.time() - 30,
        }
        previous["readiness_contention"] = {
            "request_token": "dead-previous-consumer-token"}
        peer_steward._write_json_atomic(self.paths.observation, previous)

        with mock.patch.object(peer_steward, "_watch_readiness_identity",
                               return_value=self.identity), \
                mock.patch.object(peer_steward.peer_obligations,
                                  "ObligationStore", return_value=self.store), \
                mock.patch.object(peer_steward, "_watch_shared_readiness",
                                  return_value=SimpleNamespace(state="ready", reason="fixture-ready")), \
                mock.patch.object(peer_steward.shared_readiness, "clear_watch_transition"), \
                mock.patch.object(peer_steward.shared_readiness, "wake_runner"), \
                mock.patch.object(peer_steward.peer_obligations,
                                  "ensure_normal_runner"):
            result = peer_steward._watch_transition_readiness(
                "target-agent", "idle", self.arm,
                {"pane": self.pane, "harness": "claude", "session_id": "target-session"},
                self.server, self.watch_id, self.paths)

        self.assertEqual((result.state, result.reason),
                         ("pending", "shared-readiness-revalidating"))
        replaced = peer_steward._read_json(self.paths.observation)
        fresh = replaced["readiness_transition"]
        self.assertNotEqual(fresh["token"], "dead-previous-consumer-token")
        self.assertEqual(fresh["consumer"], {"pid": os.getpid(), "pid_start": self.start})
        self.assertNotIn("readiness_contention", replaced)

        # This is the exact stale local snapshot write immediately following
        # _watch_transition_readiness() in cmd_watch_run.
        stale_pending = {**previous, "state": "pending",
                         "reason": result.reason, "native_wait": self.native_wait,
                         "observed_ts": peer_steward._utc_now()}
        peer_steward._write_watch_observation(self.paths, stale_pending)
        after = peer_steward._read_json(self.paths.observation)
        self.assertEqual(after["readiness_transition"], fresh)
        self.assertNotIn("readiness_contention", after)
        self.assertEqual(after["native_wait"], self.native_wait)

        # Explicit contention updates still own their marker, and an ordinary
        # write carrying the old marker cannot roll it back.
        with mock.patch.object(peer_steward.shared_readiness, "clear_watch_transition"), \
                mock.patch.object(peer_steward.shared_readiness, "wake_runner"), \
                mock.patch.object(peer_steward.peer_obligations,
                                  "ensure_normal_runner"):
            peer_steward._watch_note_transition_lock_contention(
                self.store.root, self.watch_id, self.paths)
        contention = peer_steward._read_json(self.paths.observation)["readiness_contention"]
        self.assertEqual(contention, {"request_token": fresh["token"]})
        peer_steward._write_watch_observation(self.paths, stale_pending)
        after_contention = peer_steward._read_json(self.paths.observation)
        self.assertEqual(after_contention["readiness_transition"], fresh)
        self.assertEqual(after_contention["readiness_contention"], contention)
        self.assertEqual(after_contention["native_wait"], self.native_wait)

    def test_runner_uses_bound_cli_observation_when_transport_is_unavailable(self):
        from peer_read_context import PeerReadContext

        observation = dict(self.observation)
        observation.pop("readiness_transition", None)
        peer_steward._write_json_atomic(self.paths.observation, observation)
        selected_jobs = self.tmp_root / "dispatch" / "jobs.log"
        selected_jobs.parent.mkdir()
        readiness_identity = {
            "server": self.server, "pane": self.pane, "harness": "claude",
            "session_id": "target-session",
            "pid_birth": f"{os.getpid()}:{self.start}",
        }

        def ready(*_args, authority_out=None, **_kwargs):
            if authority_out is not None:
                authority_out.update(rows=[], handover_keys=[])
            return SimpleNamespace(state="ready", reason="cli-observation-ready",
                                   identity=tuple(readiness_identity.items()))

        read_context = PeerReadContext(registry_append_only=True)
        with mock.patch.dict(os.environ, {
                    "AGENT_HOME": str(_HERE.parent),
                    "AGENT_DISPATCH_JOBS": str(selected_jobs)}), \
                mock.patch.object(peer_steward, "_watch_readiness_identity",
                               return_value=self.identity), \
                mock.patch.object(peer_steward, "_runner_readiness_transport",
                                  return_value=None), \
                mock.patch.object(peer_steward, "_retire_target",
                                  return_value=("idle", self.arm["agent"], 0, None)) as cli_read, \
                mock.patch.object(peer_steward, "_retire_pane_info", return_value={
                    "shell_pid": os.getpid(), "foreground_processes": []}), \
                mock.patch.object(peer_steward, "_pane_readiness", side_effect=ready) as classify, \
                patch_split(peer_steward, "_wake_watch_observer", return_value=True):
            result = peer_steward._publish_watch_readiness(
                self.store, {"watch_root": self.watch_root,
                             "watches": ((self.watch_id, self.arm),)},
                read_context=read_context, incarnation="receipt-lock-incarnation")

        self.assertTrue(result)
        cli_read.assert_called_once_with("target-agent", server=self.server)
        self.assertEqual(classify.call_count, 1)
        projection = peer_steward.shared_readiness._read_json(
            peer_steward.shared_readiness._target_path(self.store.root, self.identity))
        self.assertEqual((projection["result"]["state"], projection["result"]["reason"]),
                         ("ready", "cli-observation-ready"))
        self.assertEqual(projection["generation"].get("missing"), True)
        self.assertEqual(Path(projection["generation"]["path"]), selected_jobs)
        selected_jobs.write_text(
            "2026-10-21T00:00:00Z\topen\t/repo\t/wt\tnew-bound-work\t"
            "attempt_id=att-new-bound-work,attempt_schema_version=2,dispatch_depth=1,"
            "worker_type=execute,registered_worker=1,harness=claude,"
            "parent_sid=target-session,parent_harness=claude\n",
            encoding="utf-8")
        with mock.patch.dict(os.environ, {
                "AGENT_HOME": str(_HERE.parent),
                "AGENT_DISPATCH_JOBS": str(selected_jobs)}):
            self.assertIsNone(peer_steward.shared_readiness.target_result(
                self.store.root, self.identity),
                "file creation invalidates the prior known-absence projection")
            from peer_read_context import PeerReadContext
            work, binding_state = peer_steward.peer_obligations.bound_work_for_pane(
                self.pane, "claude", "target-session",
                read_context=PeerReadContext(registry_append_only=True),
                registry_append_only=True)
            self.assertEqual(binding_state, "observed")
            self.assertTrue(work, "the selected registry now contains exact-session work")

            with mock.patch.object(peer_steward, "_watch_readiness_identity",
                                   return_value=self.identity), \
                    mock.patch.object(peer_steward.herdr_observation,
                                      "socket_path_for_endpoint", return_value=self.socket_path), \
                    mock.patch.object(peer_steward.shared_readiness, "wake_runner",
                                      return_value=True), \
                    mock.patch.object(peer_steward.peer_obligations,
                                      "ensure_normal_runner"):
                result = peer_steward._watch_publish_ready_receipt(
                    "target-agent", "idle", self.arm,
                    {"pane": self.pane, "harness": "claude",
                     "session_id": "target-session"},
                    self.server, self.watch_id, self.paths, self.receipt)
            self.assertEqual(result, peer_steward._WATCH_RECEIPT_PENDING)
            self.assertFalse(self.paths.receipt.exists(),
                             "new work must block the receipt boundary")

    def test_complete_census_absence_does_not_get_overridden_by_cli(self):
        observation = dict(self.observation)
        observation.pop("readiness_transition", None)
        peer_steward._write_json_atomic(self.paths.observation, observation)
        transport = SimpleNamespace(observe=lambda: SimpleNamespace(
            complete=True, agents=()))
        read_context = SimpleNamespace(registry_view=lambda path, **_kwargs:
            SimpleNamespace(signature=(1, 2, 3), resolved_path=str(path)))
        with mock.patch.object(peer_steward, "_watch_readiness_identity",
                               return_value=self.identity), \
                mock.patch.object(peer_steward, "_runner_readiness_transport",
                                  return_value=transport), \
                mock.patch.object(peer_steward, "_retire_target") as cli_read, \
                mock.patch.object(peer_steward, "_retire_pane_info", return_value=None), \
                patch_split(peer_steward, "_wake_watch_observer", return_value=True):
            peer_steward._publish_watch_readiness(
                self.store, {"watch_root": self.watch_root,
                             "watches": ((self.watch_id, self.arm),)},
                read_context=read_context, incarnation="receipt-lock-incarnation")
        cli_read.assert_not_called()

    def test_unavailable_cli_observation_stays_unknown(self):
        observation = dict(self.observation)
        observation.pop("readiness_transition", None)
        peer_steward._write_json_atomic(self.paths.observation, observation)
        read_context = SimpleNamespace(registry_view=lambda path, **_kwargs:
            SimpleNamespace(signature=(1, 2, 3), resolved_path=str(path)))
        with mock.patch.object(peer_steward, "_watch_readiness_identity",
                               return_value=self.identity), \
                mock.patch.object(peer_steward, "_runner_readiness_transport",
                                  return_value=None), \
                mock.patch.object(peer_steward, "_retire_target", return_value=(
                    "herdr-unavailable", {"harness": "-", "session_id": "-",
                                          "name": "target-agent", "pane": "-"},
                    4, "herdr-protocol-error")), \
                mock.patch.object(peer_steward, "_retire_pane_info", return_value=None), \
                mock.patch.object(peer_steward, "_pane_readiness") as classify, \
                patch_split(peer_steward, "_wake_watch_observer", return_value=True):
            peer_steward._publish_watch_readiness(
                self.store, {"watch_root": self.watch_root,
                             "watches": ((self.watch_id, self.arm),)},
                read_context=read_context, incarnation="receipt-lock-incarnation")
        classify.assert_not_called()
        projection = peer_steward.shared_readiness._read_json(
            peer_steward.shared_readiness._target_path(self.store.root, self.identity))
        self.assertEqual(projection["result"]["state"], "unknown")

    def test_server_specific_cli_observation_does_not_change_global_binding(self):
        selected = "another-herdr-server"
        before = peer_steward._HERDR_SESSION
        process = subprocess.CompletedProcess(
            ["herdr"], 0, json.dumps({"result": {"agent": {"agent_status": "idle"}}}), "")
        with mock.patch.object(peer_steward.subprocess, "run", return_value=process) as run:
            payload = peer_steward._run_herdr_get("target-agent", server=selected)
        self.assertEqual(payload["result"]["agent"]["agent_status"], "idle")
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["herdr", "--session", selected,
                                "agent", "get", "target-agent"])
        self.assertEqual(run.call_args.kwargs["env"]["AGENT_HERDR_SESSION"], selected)
        self.assertEqual(peer_steward._HERDR_SESSION, before)

    def test_repeated_same_transition_contention_coalesces_runner_wake(self):
        wake_fd = peer_steward.shared_readiness.open_runner_wake(self.store.root)
        try:
            with self._holder("both", hold_seconds=0.8):
                first = self._publish_receipt()
                self.assertEqual(first, peer_steward._WATCH_RECEIPT_CONTENDED)
                self.assertEqual(os.read(wake_fd, 32), b"\0")
                first_observation = peer_steward._read_json(self.paths.observation)
                request = first_observation.get("readiness_transition")
                self.assertEqual(
                    first_observation.get("readiness_contention", {}).get("request_token"),
                    request.get("token"))

                # Simulate the same unchanged runner response arriving again
                # while the writer locks remain owned.
                self._publish_transition_response()
                second = self._publish_receipt()
                self.assertEqual(second, peer_steward._WATCH_RECEIPT_CONTENDED)
                self.assertEqual(peer_steward._read_json(self.paths.observation).get("native_wait"),
                                 self.native_wait)
                with self.assertRaises(BlockingIOError):
                    os.read(wake_fd, 32)
        finally:
            os.close(wake_fd)

        wake = mock.Mock(return_value=True)
        self.assertTrue(self._runner_pass(wake), "the existing runner pass can refresh the token")
        response = peer_steward.shared_readiness.watch_transition_result(
            self.store.root, self.watch_id, self.identity, self.token)
        self.assertEqual(response["state"], "ready")
        self.assertTrue(self._publish_receipt(),
                        "same watch recovers after the writer releases its locks")
        self.assertEqual(peer_steward._read_json(self.paths.receipt)["watch_id"], self.watch_id)


class SharedReadinessRunnerTest(unittest.TestCase):
    def test_known_watch_sidless_census_requires_matching_foreground_proof(self):
        from dispatch_contract import process_start_ticks
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = root / "dispatch" / "jobs.log"
            jobs.parent.mkdir()
            jobs.write_text("", encoding="utf-8")
            state_root = root / "peer-state"
            start = str(process_start_ticks(os.getpid()))
            identity = peer_steward.shared_readiness.target_identity(
                server="fixture", endpoint="/fixture.sock", pane="w1:p1",
                harness="codex", session_id="thread-a", shell_pid=os.getpid(),
                shell_start=start, native_state="idle")
            result = {"state": "ready", "reason": "native-turn-ready",
                      "lineage": {"shell_pid": os.getpid(), "shell_start": start}}
            pane_info = {"shell_pid": os.getpid(),
                         "foreground_processes": [{"pid": os.getpid()}]}
            selected_env = {"AGENT_HOME": str(_HERE.parent),
                            "AGENT_DISPATCH_JOBS": str(jobs),
                            "AGENT_PEER_LEDGER_ROOT": str(state_root)}
            with mock.patch.dict(os.environ, selected_env), \
                    mock.patch.object(peer_steward, "_retire_target", return_value=(
                        "idle", {"server": "fixture", "pane": "w1:p1",
                                 "harness": "codex", "session_id": "-",
                                 "name": "agent-a"}, 0, "")), \
                    mock.patch.object(peer_steward, "_retire_pane_info",
                                      return_value=pane_info), \
                    mock.patch.object(peer_steward.peer_obligations,
                                      "execution_attempts_for_processes", return_value=()):
                with mock.patch.object(peer_steward, "_proven_session",
                                       return_value="thread-a"):
                    proven = peer_steward._watch_readiness_result(
                        "agent-a", "idle", identity, result, "fixture")
                self.assertEqual((proven.state, proven.reason),
                                 ("ready", "native-turn-ready"))
                with mock.patch.object(peer_steward, "_proven_session",
                                       return_value="thread-b"):
                    unproven = peer_steward._watch_readiness_result(
                        "agent-a", "idle", identity, result, "fixture")
                self.assertEqual(unproven.state, "unknown")
                with mock.patch.object(peer_steward, "_retire_target", return_value=(
                        "idle", {"server": "fixture", "pane": "w1:p1",
                                 "harness": "codex", "session_id": "thread-b",
                                 "name": "agent-a"}, 0, "")), \
                        mock.patch.object(peer_steward, "_proven_session",
                                          return_value="thread-a"):
                    conflicting = peer_steward._watch_readiness_result(
                        "agent-a", "idle", identity, result, "fixture")
                self.assertEqual(conflicting.state, "unknown")

    def test_watch_identity_keeps_its_selected_endpoint_across_runner_contexts(self):
        from dispatch_contract import process_start_ticks
        start = str(process_start_ticks(os.getpid()))
        arm = {
            "server": "default", "target": "agent-a",
            "readiness_endpoint": "/fixture/default.sock",
            "agent": {"pane": "w1:p1", "harness": "claude", "session_id": "sid-a"},
            "pane_identity": {"shell_pid": os.getpid(), "shell_start": start},
        }
        with mock.patch.dict(os.environ, {
                "AGENT_HERDR_SESSION": "secondary",
                "HERDR_SOCKET_PATH": "/fixture/secondary.sock"}):
            identity = peer_steward._watch_readiness_identity(arm, "idle")
            self.assertEqual(identity["endpoint"], "/fixture/default.sock")
            self.assertTrue(peer_steward._watch_readiness_identity_matches_arm(
                identity, arm, "idle"))
            crossed = {**identity, "endpoint": "/fixture/secondary.sock"}
            self.assertFalse(peer_steward._watch_readiness_identity_matches_arm(
                crossed, arm, "idle"), "a runner's endpoint cannot replace the arm's endpoint")

    def test_one_selection_observation_fans_out_to_live_consumer_duties(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary)
            jobs = state_root / "jobs.log"
            jobs.write_text("fixture\n", encoding="utf-8")
            store = peer_steward.peer_obligations.ObligationStore(state_root)
            from dispatch_contract import process_start_ticks
            start = str(process_start_ticks(os.getpid()))
            identities = [peer_steward.shared_readiness.attempt_identity(
                jobs, "att-shared", consumer_pid=os.getpid(), consumer_start=start,
                request_token=token) for token in ("one", "two")]
            duties = []
            for identity in identities:
                duty_id = peer_steward.shared_readiness.duty_id(identity)
                duties.append(store.create(
                    duty_id, peer_steward.shared_readiness.DUTY_KIND, identity,
                    {"jobs": str(jobs), "attempt_id": "att-shared", "parent": "", "slug": ""}))

            producer_start = str(process_start_ticks(os.getpid()))
            incarnation = "runner-test"
            peer_steward.shared_readiness.publish_runner_incarnation(
                store.root, os.getpid(), producer_start, incarnation)
            selection = mock.Mock(return_value=[])
            classify = mock.Mock(return_value={"state": "pending", "reason": "owner-running",
                                               "children": [{"attempt_id": "att-shared",
                                                             "readiness": "pending"}]})
            api = SimpleNamespace(selected_rows=selection, classify_selection=classify)
            read_context = SimpleNamespace(registry_view=mock.Mock(return_value=SimpleNamespace(
                signature=(1, 2, 3, 4, 5), resolved_path=str(jobs.resolve()))))
            with mock.patch.object(peer_steward, "_dispatch_ready_api", return_value=api):
                peer_steward._reconcile_readiness_observations(
                    store, duties, read_context=read_context, observation_context=object(),
                    incarnation=incarnation)

            selection.assert_called_once_with(
                jobs, parent="", slug="", attempt_id="att-shared",
                read_context=read_context, append_only=True)
            classify.assert_called_once()
            for identity in identities:
                duty = store.get(peer_steward.shared_readiness.duty_id(identity))
                self.assertEqual(peer_steward.shared_readiness.current_result(
                    duty, identity)["state"], "pending")

    def test_watch_consumes_pending_projection_and_rechecks_ready_projection(self):
        from dispatch_contract import process_start_ticks
        start = str(process_start_ticks(os.getpid()))
        arm = {"server": "server-a", "target": "agent-a",
               "agent": {"pane": "w1:p1", "harness": "claude", "session_id": "sid-a"},
               "pane_identity": {"shell_pid": os.getpid(), "shell_start": start}}
        expected = {"pane": "w1:p1", "harness": "claude", "session_id": "sid-a"}
        pending = SimpleNamespace(state="pending", reason="bound-registered-work-pending")
        with mock.patch.object(peer_steward.herdr_observation,
                               "socket_path_for_endpoint", return_value="/tmp/herdr.sock"), \
                mock.patch.object(peer_steward.peer_obligations, "ObligationStore"), \
                mock.patch.object(peer_steward.shared_readiness, "target_result",
                                  return_value={"state": "pending", "reason": pending.reason}), \
                mock.patch.object(peer_steward, "_pane_readiness",
                                  side_effect=AssertionError("pending projection re-read per watch")):
            observed = peer_steward._watch_shared_readiness(
                "agent-a", "idle", arm, expected, "server-a")
        self.assertEqual((observed.state, observed.reason), (pending.state, pending.reason))

        ready = SimpleNamespace(state="ready", reason="native-turn-ready", identity=(
            ("server", "server-a"), ("pane", "w1:p1"), ("harness", "claude"),
            ("session_id", "sid-a"), ("pid_birth", f"{os.getpid()}:{start}")))
        with mock.patch.object(peer_steward.herdr_observation,
                               "socket_path_for_endpoint", return_value="/tmp/herdr.sock"), \
                mock.patch.object(peer_steward.peer_obligations, "ObligationStore"), \
                mock.patch.object(peer_steward.shared_readiness, "target_result",
                                  return_value={"state": "ready", "reason": "native-turn-ready",
                                                "lineage": {"shell_pid": os.getpid(),
                                                            "shell_start": start}}), \
                mock.patch.object(peer_steward, "_retire_target", return_value=(
                    "idle", {"pane": "w1:p1", "harness": "claude", "session_id": "sid-a"}, 0, "")), \
                mock.patch.object(peer_steward, "_retire_pane_info", return_value={
                    "shell_pid": os.getpid()}), \
                mock.patch.object(peer_steward, "_pane_readiness",
                                  side_effect=AssertionError("ready consumer rebuilt registry view")):
            observed = peer_steward._watch_shared_readiness(
                "agent-a", "idle", arm, expected, "server-a")
        self.assertEqual(observed.state, "ready")
        self.assertEqual(dict(observed.identity), dict(ready.identity))

    def test_runner_reads_once_for_five_stewards_and_separates_another_pane(self):
        from dispatch_contract import process_start_ticks
        from dispatch_registry_cache import registry_view
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "peer-state"
            dispatch_root = Path(temporary) / "dispatch"
            dispatch_root.mkdir()
            watch_root = state_root / "peer-watches"
            watch_root.mkdir(parents=True)
            jobs = dispatch_root / "jobs.log"
            jobs.write_text("fixture\n", encoding="utf-8")
            store = peer_steward.peer_obligations.ObligationStore(state_root)
            store.root.mkdir(parents=True)
            start = str(process_start_ticks(os.getpid()))
            arms = []
            for index in range(6):
                pane = "w1:p1" if index < 5 else "w1:p2"
                sid = "sid-a" if index < 5 else "sid-b"
                watch_id = f"watch-{index}"
                arm = {"watch_id": watch_id, "target": f"agent-{index}",
                       "server": "server-a",
                       "agent": {"pane": pane, "harness": "claude", "session_id": sid,
                                 "name": f"agent-{index}"},
                       "pane_identity": {"shell_pid": os.getpid(), "shell_start": start},
                       "steward": {"session_id": f"steward-{index}"}}
                arms.append((watch_id, arm))
                paths = peer_steward._watch_paths(watch_id, watch_root)
                peer_steward._write_json_atomic(paths.arm, arm)
                peer_steward._write_json_atomic(paths.observation, {
                    "watch_id": watch_id, "state": "pending",
                    "native_wait": {"state": "result", "agent_state": "idle"}})

            producer_start = str(process_start_ticks(os.getpid()))
            incarnation = "runner-projection-test"
            peer_steward.shared_readiness.publish_runner_incarnation(
                store.root, os.getpid(), producer_start, incarnation)
            read_context = SimpleNamespace(registry_view=mock.Mock(side_effect=lambda path, **kwargs:
                registry_view(path, append_only=kwargs.get("append_only", False))))
            agents = []
            for index in range(6):
                pane = "w1:p1" if index < 5 else "w1:p2"
                sid = "sid-a" if index < 5 else "sid-b"
                agents.append({"name": f"agent-{index}", "agent": "claude",
                               "agent_session": {"value": sid}, "pane_id": pane,
                               "agent_status": "idle"})
            transport = mock.Mock()
            transport.observe.return_value = SimpleNamespace(agents=tuple(agents))
            def readiness(target, _state, **_kwargs):
                index = int(target.rsplit("-", 1)[1])
                pane = "w1:p1" if index < 5 else "w1:p2"
                sid = "sid-a" if index < 5 else "sid-b"
                return SimpleNamespace(state="pending", reason="bound-registered-work-pending",
                    identity=(("server", "server-a"), ("pane", pane), ("harness", "claude"),
                              ("session_id", sid), ("pid_birth", f"{os.getpid()}:{start}")))
            snapshot = {"watch_root": watch_root, "watches": tuple(arms)}
            selected_env = {"AGENT_HOME": str(_HERE.parent),
                            "AGENT_DISPATCH_JOBS": str(jobs),
                            "AGENT_PEER_LEDGER_ROOT": str(state_root)}
            with mock.patch.dict(os.environ, selected_env), \
                    mock.patch.object(peer_steward.herdr_observation,
                                   "socket_path_for_endpoint", return_value="/tmp/herdr.sock"), \
                    mock.patch.object(peer_steward, "_runner_readiness_transport",
                                      return_value=transport), \
                    mock.patch.object(peer_steward, "_retire_pane_info", return_value={
                        "shell_pid": os.getpid(), "foreground_processes": []}), \
                    mock.patch.object(peer_steward, "_pane_readiness", side_effect=readiness) as heavy_read, \
                    mock.patch.object(peer_steward, "_wake_current_watch_observer",
                                      return_value=True) as wake:
                self.assertTrue(peer_steward._publish_watch_readiness(
                    store, snapshot, read_context=read_context, incarnation=incarnation))
            transport.observe.assert_called_once()
            self.assertGreater(read_context.registry_view.call_count, 0)
            self.assertLess(read_context.registry_view.call_count, len(arms),
                            "runner must not retain/build a RegistryView per watch")
            self.assertTrue(all(call.args == (jobs,) and call.kwargs == {"append_only": True}
                                for call in read_context.registry_view.call_args_list))
            self.assertNotEqual(store.root.parents[1], jobs.parent,
                                "peer obligation storage stays separate from dispatch jobs")
            self.assertEqual(heavy_read.call_count, 2)
            self.assertEqual(wake.call_count, 6)
            with mock.patch.dict(os.environ, selected_env), \
                    mock.patch.object(peer_steward.herdr_observation,
                                   "socket_path_for_endpoint", return_value="/tmp/herdr.sock"):
                for _watch_id, arm in arms:
                    identity = peer_steward._watch_readiness_identity(arm, "idle")
                    projection = peer_steward.shared_readiness._read_json(
                        peer_steward.shared_readiness._target_path(store.root, identity))
                    self.assertEqual(projection["generation"]["path"], str(jobs))
                    self.assertEqual(peer_steward.shared_readiness.target_result(
                        store.root, identity)["state"], "pending")

    def test_cached_ready_projection_revalidates_claim_handover_and_cleanup_gate(self):
        from dispatch_contract import process_start_ticks
        from dispatch_registry_cache import registry_view

        for name, expected_state, changed_reason in (
                ("claim-handover", "pending", "bound-registered-work-pending"),
                ("cleanup-gate", "pending", "owner-completion-cleanup-pending"),
                ("unchanged", "ready", "native-turn-ready")):
            with self.subTest(authority=name), tempfile.TemporaryDirectory() as temporary:
                state_root = Path(temporary)
                watch_root = state_root / "peer-watches"
                watch_root.mkdir()
                jobs = state_root / "jobs.log"
                jobs.write_text("", encoding="utf-8")
                selected_env = {"AGENT_HOME": str(_HERE.parent),
                                "AGENT_DISPATCH_JOBS": str(jobs),
                                "AGENT_PEER_LEDGER_ROOT": str(state_root)}
                jobs_before = jobs.read_bytes()
                store = peer_steward.peer_obligations.ObligationStore(state_root)
                self.assertTrue(store.ensure_directory())
                start = str(process_start_ticks(os.getpid()))
                watch_id = f"watch-{name}"
                arm = {
                    "watch_id": watch_id, "server": "server-a", "target": "agent-a",
                    "agent": {"pane": "w1:p1", "harness": "claude",
                              "session_id": "sid-a", "name": "agent-a"},
                    "pane_identity": {"shell_pid": os.getpid(), "shell_start": start},
                }
                paths = peer_steward._watch_paths(watch_id, watch_root)
                native_wait = {"state": "result", "agent_state": "idle",
                               "agent": arm["agent"]}
                endpoint = "/tmp/herdr-transition.sock"
                identity = peer_steward.shared_readiness.target_identity(
                    server="server-a", endpoint=endpoint, pane="w1:p1",
                    harness="claude", session_id="sid-a", shell_pid=os.getpid(),
                    shell_start=start, native_state="idle")
                old_request = ({"token": "previous-observer-token", "identity": identity,
                                "consumer": {"pid": 2 ** 22, "pid_start": "1"},
                                "requested_at": time.time()}
                               if name == "cleanup-gate" else None)
                peer_steward._write_json_atomic(paths.arm, arm)
                observation = {
                    "state": "pending", "native_wait": native_wait,
                }
                if old_request:
                    observation["readiness_transition"] = old_request
                peer_steward._write_json_atomic(paths.observation, observation)
                producer_start = str(process_start_ticks(os.getpid()))
                incarnation = f"transition-{name}"
                peer_steward.shared_readiness.publish_runner_incarnation(
                    store.root, os.getpid(), producer_start, incarnation)
                peer_steward.shared_readiness.publish_target_projection(
                    store.root, identity, {"state": "ready", "reason": "cached-ready"},
                    producer_pid=os.getpid(), producer_start=producer_start,
                    incarnation=incarnation,
                    generation=peer_steward.shared_readiness.registry_generation(
                        jobs, registry_view(jobs), selected=True), lineage={
                        "shell_pid": os.getpid(), "shell_start": start})

                with mock.patch.object(peer_steward.peer_obligations,
                                       "ObligationStore", return_value=store), \
                        mock.patch.dict(os.environ, {**selected_env,
                                                     "XDG_STATE_HOME": str(state_root / "xdg")}), \
                        mock.patch.object(peer_steward.herdr_observation,
                                          "socket_path_for_endpoint", return_value=endpoint), \
                        mock.patch.object(peer_steward, "_retire_target", return_value=(
                            "idle", {"pane": "w1:p1", "harness": "claude",
                                     "session_id": "sid-a"}, 0, "")), \
                        mock.patch.object(peer_steward, "_retire_pane_info", return_value={
                            "shell_pid": os.getpid()}), \
                        mock.patch.object(peer_steward.shared_readiness, "wake_runner",
                                          return_value=True), \
                        mock.patch.object(peer_steward.peer_obligations,
                                          "ensure_normal_runner"):
                    requested = peer_steward._watch_transition_readiness(
                        "agent-a", "idle", arm, arm["agent"], "server-a", watch_id, paths)
                self.assertEqual((requested.state, requested.reason),
                                 ("pending", "shared-readiness-revalidating"))
                request = json.loads(paths.observation.read_text(encoding="utf-8"))[
                    "readiness_transition"]
                self.assertEqual(request["identity"], identity)
                self.assertEqual(request["consumer"]["pid"], os.getpid())
                self.assertNotEqual(request["token"], "previous-observer-token")
                self.assertEqual(json.loads(paths.observation.read_text(encoding="utf-8"))[
                    "native_wait"], native_wait)

                identity_rows = (
                    ("server", "server-a"), ("pane", "w1:p1"),
                    ("harness", "claude"), ("session_id", "sid-a"),
                    ("pid_birth", f"{os.getpid()}:{start}"),
                )
                fresh_authority = SimpleNamespace(state=expected_state,
                    reason=changed_reason, identity=identity_rows)
                jobs_info = jobs.stat()
                read_context = SimpleNamespace(registry_view=mock.Mock(return_value=
                    SimpleNamespace(
                        signature=(jobs_info.st_dev, jobs_info.st_ino, jobs_info.st_size,
                                   jobs_info.st_mtime_ns, jobs_info.st_ctime_ns),
                        resolved_path=str(jobs.resolve()))))
                transport = mock.Mock()
                transport.observe.return_value = object()
                snapshot = {"watch_root": watch_root, "watches": ((watch_id, arm),)}
                with mock.patch.dict(os.environ, selected_env), \
                        mock.patch.object(peer_steward.herdr_observation,
                                       "socket_path_for_endpoint", return_value=endpoint), \
                        mock.patch.object(peer_steward, "_runner_readiness_transport",
                                          return_value=transport), \
                        mock.patch.object(peer_steward, "_target_from_readiness_view",
                                          return_value=("idle", arm["agent"], 0, "")), \
                        mock.patch.object(peer_steward, "_retire_pane_info", return_value={
                            "shell_pid": os.getpid(), "foreground_processes": []}), \
                        mock.patch.object(peer_steward, "_pane_readiness",
                                          return_value=fresh_authority) as fresh_check, \
                        patch_split(peer_steward, "_wake_watch_observer",
                                          return_value=True):
                    peer_steward._publish_watch_readiness(
                        store, snapshot, read_context=read_context, incarnation=incarnation)

                self.assertEqual(fresh_check.call_count, 2,
                                 "one shared projection and one request-bound refresh classify the target")
                with mock.patch.object(peer_steward.peer_obligations,
                                       "ObligationStore", return_value=store), \
                        mock.patch.dict(os.environ, {**selected_env,
                                                     "XDG_STATE_HOME": str(state_root / "xdg")}), \
                        mock.patch.object(peer_steward.herdr_observation,
                                          "socket_path_for_endpoint", return_value=endpoint), \
                        mock.patch.object(peer_steward, "_retire_target", return_value=(
                            "idle", arm["agent"], 0, "")), \
                        mock.patch.object(peer_steward, "_retire_pane_info", return_value={
                            "shell_pid": os.getpid()}):
                    observed = peer_steward._watch_transition_readiness(
                        "agent-a", "idle", arm, arm["agent"], "server-a", watch_id, paths)
                self.assertEqual((observed.state, observed.reason),
                                 (expected_state, changed_reason))
                self.assertEqual(jobs.read_bytes(), jobs_before)
                self.assertFalse(paths.receipt.exists())

    def test_mutation_between_runner_checks_cannot_publish_ready_transition(self):
        from dispatch_contract import process_start_ticks
        from dispatch_registry_cache import registry_view

        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary)
            watch_root = state_root / "peer-watches"
            watch_root.mkdir()
            jobs = state_root / "jobs.log"
            jobs.write_text("", encoding="utf-8")
            selected_env = {"AGENT_HOME": str(_HERE.parent),
                            "AGENT_DISPATCH_JOBS": str(jobs),
                            "AGENT_PEER_LEDGER_ROOT": str(state_root)}
            store = peer_steward.peer_obligations.ObligationStore(state_root)
            self.assertTrue(store.ensure_directory())
            start = str(process_start_ticks(os.getpid()))
            watch_id = "watch-boundary-race"
            endpoint = "/tmp/herdr-transition.sock"
            arm = {
                "watch_id": watch_id, "server": "server-a", "target": "agent-a",
                "agent": {"pane": "w1:p1", "harness": "claude",
                          "session_id": "sid-a", "name": "agent-a"},
                "pane_identity": {"shell_pid": os.getpid(), "shell_start": start},
            }
            paths = peer_steward._watch_paths(watch_id, watch_root)
            peer_steward._write_json_atomic(paths.arm, arm)
            peer_steward._write_json_atomic(paths.observation, {
                "native_wait": {"state": "result", "agent_state": "idle",
                                "agent": arm["agent"]},
            })
            identity = peer_steward.shared_readiness.target_identity(
                server="server-a", endpoint=endpoint, pane="w1:p1", harness="claude",
                session_id="sid-a", shell_pid=os.getpid(), shell_start=start,
                native_state="idle")
            producer_start = str(process_start_ticks(os.getpid()))
            incarnation = "transition-race"
            peer_steward.shared_readiness.publish_runner_incarnation(
                store.root, os.getpid(), producer_start, incarnation)
            peer_steward.shared_readiness.publish_target_projection(
                store.root, identity, {"state": "ready", "reason": "cached-ready"},
                producer_pid=os.getpid(), producer_start=producer_start,
                incarnation=incarnation,
                generation=peer_steward.shared_readiness.registry_generation(
                    jobs, registry_view(jobs), selected=True),
                lineage={"shell_pid": os.getpid(), "shell_start": start})
            ready = SimpleNamespace(state="ready", reason="first-read-clear",
                identity=(("server", "server-a"), ("pane", "w1:p1"),
                          ("harness", "claude"), ("session_id", "sid-a"),
                          ("pid_birth", f"{os.getpid()}:{start}")))
            pending = SimpleNamespace(state="pending", reason="claim-arrived-between-checks",
                identity=ready.identity)

            with mock.patch.object(peer_steward.peer_obligations,
                                   "ObligationStore", return_value=store), \
                    mock.patch.dict(os.environ, selected_env), \
                    mock.patch.object(peer_steward.herdr_observation,
                                      "socket_path_for_endpoint", return_value=endpoint), \
                    mock.patch.object(peer_steward, "_retire_target", return_value=(
                        "idle", arm["agent"], 0, "")), \
                    mock.patch.object(peer_steward, "_retire_pane_info", return_value={
                        "shell_pid": os.getpid()}), \
                    mock.patch.object(peer_steward.shared_readiness, "wake_runner",
                                      return_value=True), \
                    mock.patch.object(peer_steward.peer_obligations,
                                      "ensure_normal_runner"):
                peer_steward._watch_transition_readiness(
                    "agent-a", "idle", arm, arm["agent"], "server-a", watch_id, paths)

            read_context = SimpleNamespace(registry_view=mock.Mock(side_effect=lambda path, **kwargs:
                registry_view(path, append_only=kwargs.get("append_only", False))))
            transport = mock.Mock()
            transport.observe.return_value = object()
            with mock.patch.dict(os.environ, selected_env), \
                    mock.patch.object(peer_steward.herdr_observation,
                                   "socket_path_for_endpoint", return_value=endpoint), \
                    mock.patch.object(peer_steward, "_runner_readiness_transport",
                                      return_value=transport), \
                    mock.patch.object(peer_steward, "_target_from_readiness_view",
                                      return_value=("idle", arm["agent"], 0, "")), \
                    mock.patch.object(peer_steward, "_retire_pane_info", return_value={
                        "shell_pid": os.getpid(), "foreground_processes": []}), \
                    mock.patch.object(peer_steward, "_pane_readiness",
                                      side_effect=(ready, pending)) as check, \
                    patch_split(peer_steward, "_wake_watch_observer",
                                      return_value=True):
                peer_steward._publish_watch_readiness(
                    store, {"watch_root": watch_root, "watches": ((watch_id, arm),)},
                    read_context=read_context, incarnation=incarnation)

            self.assertEqual(check.call_count, 2)
            with mock.patch.dict(os.environ, selected_env):
                response = peer_steward.shared_readiness.watch_transition_result(
                    store.root, watch_id, identity,
                    json.loads(paths.observation.read_text(encoding="utf-8"))[
                        "readiness_transition"]["token"])
            self.assertEqual((response["state"], response["reason"]),
                             ("pending", "claim-arrived-between-checks"))

    def test_selected_authority_accepts_more_than_64_related_registry_rows(self):
        from dispatch_contract import parse_registry_metadata
        from dispatch_registry_cache import registry_view
        from peer_read_context import PeerReadContext

        with tempfile.TemporaryDirectory() as temporary:
            jobs = Path(temporary) / "jobs.log"
            extensions = {f"extension_{index:03d}": f"generated-value-{index:03d}"
                          for index in range(127)}
            lines = []
            route_id, route_hash = "rt-generated-related-set", "sha256:" + "c" * 64
            for index in range(70):
                metadata = {
                    "attempt_id": f"att-generated-related-{index:03d}",
                    "attempt_schema_version": "2",
                    "dispatch_depth": "1",
                    "worker_type": "review",
                    "registered_worker": "1",
                    "execution_surface": "registered-headless",
                    "harness": "claude",
                    "parent_sid": "generated-parent-session",
                    "parent_harness": "claude",
                    "route_id": route_id,
                    "route_hash": route_hash,
                    "route_node": "review",
                    "note": "completed-marker",
                    "failure_class": "pass",
                    "receipt_state": "settled",
                    "cleanup_pending": "0",
                    "generated_extension_detail": "x" * 2400,
                    **extensions,
                }
                raw = ",".join(f"{key}={value}" for key, value in metadata.items())
                lines.append("\t".join((
                    "2026-10-20T00:00:00Z", "done", "/generated/repo",
                    "/generated/worktree", f"review-{index:03d}", raw)))
            jobs.write_text("\n".join(lines) + "\n", encoding="utf-8")

            view = registry_view(jobs)
            related = view.completion_related("att-generated-owner", route_id, route_hash)
            self.assertEqual(len(related), 70)
            selected = [{
                "status": record.status,
                "metadata": parse_registry_metadata(record.fields[5]),
                "completion_metadata": record.completion_metadata(),
                "fields": list(record.fields),
            } for record in related]
            self.assertEqual(selected[0]["metadata"], selected[0]["completion_metadata"])
            self.assertGreater(len(selected[0]["metadata"]), 64)
            self.assertGreater(len(selected[0]["fields"][5]), 4096)
            self.assertGreater(max(map(len, selected[0]["metadata"].values())), 2048)

            import dispatch_registry_cache
            authority = {"rows": [], "handover_keys": []}
            with mock.patch.object(dispatch_registry_cache, "registry_view",
                                   side_effect=AssertionError(
                                       "selected authority consumer rebuilt RegistryView")):
                work, state = peer_steward.peer_obligations.bound_work_for_pane(
                    "w1:p1", "claude", "sid-unrelated", jobs=jobs,
                    execution_attempt_ids=(), include_parent=False,
                    read_context=PeerReadContext(registry_append_only=True),
                    authority_out=authority, selected_rows=selected,
                    expected_handover_keys=[])
            self.assertEqual(state, "observed")
            self.assertEqual(work, ())
            self.assertEqual(len(authority["rows"]), 70)
            self.assertTrue(peer_steward.shared_readiness.valid_watch_authority(authority))

    def test_receipt_boundary_rechecks_published_authority_and_serializes_seat_writers(self):
        from dispatch_contract import process_start_ticks
        from peer_read_context import PeerReadContext

        terminal_spec = importlib.util.spec_from_file_location(
            "terminal_tests_for_watch_transition", _HERE / "dispatch_terminal_commit.test.py")
        terminal_tests = importlib.util.module_from_spec(terminal_spec)
        terminal_spec.loader.exec_module(terminal_tests)
        terminal_case = terminal_tests.OwnerTerminalPlacementReplayTest(
            "test_a_replayed_settlement_after_a_checkpoint_completes_for_every_harness")
        try:
            with tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                jobs = base / "jobs.log"
                env = {
                    "AGENT_HOME": str(base),
                    "AGENT_DISPATCH_JOBS": str(jobs),
                    "XDG_STATE_HOME": str(base / "xdg-state"),
                }
                with mock.patch.dict(os.environ, env):
                    fixture, terminal, route, route_file, jobs, owner, _report, _meta = (
                        terminal_case._settled("claude", "owner-report.md"))
                    os.environ["AGENT_DISPATCH_JOBS"] = str(jobs)
                    os.environ["AGENT_PEER_LEDGER_ROOT"] = str(jobs.parent)
                    os.environ["XDG_STATE_HOME"] = str(base / "xdg-state")
                    store = peer_steward.peer_obligations.ObligationStore(jobs.parent)
                    self.assertTrue(store.ensure_directory())
                    watch_root = jobs.parent / "peer-watches"
                    watch_root.mkdir()
                    lines = jobs.read_text(encoding="utf-8").splitlines()
                    for index, line in enumerate(lines):
                        fields = line.split("\t")
                        metadata = terminal.dispatch_contract.parse_registry_metadata(fields[5])
                        if metadata.get("attempt_id") == owner:
                            metadata.update(parent_sid="owner-parent", parent_harness="claude")
                            fields[5] = ",".join(
                                f"{key}={value}" for key, value in metadata.items())
                            lines[index] = "\t".join(fields)
                            break
                    task_attempt = "att-watch-handover"
                    task_route, task_hash, task_node = (
                        "rt-watch-handover", "sha256:" + "b" * 64, "owner")
                    import dispatch_pending_delivery as pending
                    task_delivery_id = "delivery-watch-handover"
                    task_delivery_revision = "watch-row-revision"
                    task_receipt = {"schema_version": 1, "attempt_id": task_attempt,
                                    "route_id": task_route, "route_node": task_node}
                    task_delivery_digest = pending._canonical_receipt_digest(task_receipt)
                    task_metadata = {
                        "attempt_id": task_attempt, "attempt_schema_version": "2",
                        "dispatch_depth": "1", "worker_type": "owner",
                        "registered_worker": "1", "harness": "claude",
                        "parent_sid": "fixture-parent", "parent_harness": "claude",
                        "route_id": task_route, "route_hash": task_hash,
                        "route_node": task_node, "note": "completed-marker",
                        "failure_class": "pass", "launch_outcome": "reaped-before-publish",
                        "delivery_intent": "1", "delivery_id": task_delivery_id,
                        "delivery_receipt_digest": task_delivery_digest,
                        "delivery_row_revision": task_delivery_revision,
                        "parent_completion_delivery": "claude-parent-runtime",
                    }
                    lines.append("\t".join((
                        "2026-09-08T00:00:00Z", "done", str(base), str(base),
                        "watch-handover", ",".join(
                            f"{key}={value}" for key, value in task_metadata.items()))))
                    jobs.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    pending.create(
                        root=jobs.parent, recipient_kind="claude-parent-runtime",
                        recipient_key="fixture-parent", delivery_id=task_delivery_id,
                        session_generation="", session_generation_supported="0",
                        attempt_ids=[task_attempt], parent_attempt_id=owner,
                        route_id=task_route, route_node=task_node, receipt=task_receipt,
                        receipt_digest=task_delivery_digest,
                        row_revisions={task_attempt: task_delivery_revision},
                    )
                    from dispatch_completion_join import exact_attempt_row
                    owner_meta = exact_attempt_row(jobs, owner).metadata
                    with mock.patch.dict(os.environ, {
                            "AGENT_DISPATCH_JOBS": str(jobs),
                            "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
                        settled = terminal.settle_owner_completion(jobs, "done", owner_meta)
                    self.assertEqual(settled.result, "completed", settled)
                    request = terminal.TerminalCommitRequest(
                        route_file, owner, jobs, fixture.root)
                    state_path = terminal._commit_state_path(request)
                    claim_path = terminal.dispatch_contract.terminal_claim_path(
                        jobs, route["route_id"], owner)
                    goal_path = terminal.campaign_goal_path(request)

                    # Generate sanitized writer-shaped related rows with an
                    # extensible metadata set. These remain unrelated to the
                    # watched session, but the selected route authority must
                    # carry them intact through the runner and receipt reader.
                    related_route, related_hash, related_node = (
                        task_route, task_hash, task_node)
                    extensions = {
                        f"extension_{index:03d}": f"generated-value-{index:03d}"
                        for index in range(127)
                    }
                    related_lines = []
                    for index in range(6):
                        metadata = {
                            "attempt_id": f"att-generated-related-{index:03d}",
                            "attempt_schema_version": "2",
                            "dispatch_depth": "1",
                            "worker_type": "owner",
                            "registered_worker": "1",
                            "harness": "claude",
                            "parent_sid": "fixture-parent",
                            "parent_harness": "claude",
                            "route_id": related_route,
                            "route_hash": related_hash,
                            "route_node": related_node,
                            "note": "completed-marker",
                            "failure_class": "pass",
                            "launch_outcome": "reaped-before-publish",
                            "receipt_state": "settled",
                            "cleanup_pending": "0",
                            "generated_extension_detail": "x" * 2400,
                            **extensions,
                        }
                        raw_metadata = ",".join(
                            f"{key}={value}" for key, value in metadata.items())
                        related_lines.append("\t".join((
                            "2026-10-20T00:00:00Z", "done", str(base), str(base),
                            f"generated-owner-{index:03d}", raw_metadata)))
                    with jobs.open("a", encoding="utf-8") as stream:
                        stream.write("\n".join(related_lines) + "\n")

                    start = str(process_start_ticks(os.getpid()))
                    server, socket_path = "server-watch-authority", "/tmp/watch-authority.sock"
                    watch_id = "watch-real-authority-boundary"
                    agent = {"pane": "w1:p7", "harness": "claude",
                             "session_id": "sid-a", "name": "agent-a"}
                    import session_tidy
                    steward_sid = "steward-large-envelope"
                    steward_seat = session_tidy.Seat(
                        "pane", "watch-authority-steward-seat", "w1:p8", "claude",
                        sid=steward_sid, endpoint="fixture").as_dict()
                    arm = {
                        "watch_id": watch_id, "server": server, "target": "agent-a",
                        "agent": agent,
                        "steward": {"harness": "claude", "session_id": steward_sid,
                                    "project": str(base)},
                        "steward_seat": steward_seat,
                        "pane_identity": {"shell_pid": os.getpid(), "shell_start": start},
                    }
                    paths = peer_steward._watch_paths(watch_id, watch_root)
                    peer_steward._write_json_atomic(paths.arm, arm)
                    native_wait = {"state": "result", "agent_state": "idle", "agent": agent}
                    identity = peer_steward.shared_readiness.target_identity(
                        server=server, endpoint=socket_path, pane=agent["pane"],
                        harness=agent["harness"], session_id=agent["session_id"],
                        shell_pid=os.getpid(), shell_start=start, native_state="idle")
                    token = "receipt-boundary-token"
                    peer_steward._write_json_atomic(paths.observation, {
                        "watch_id": watch_id, "state": "pending", "native_wait": native_wait,
                        "readiness_transition": {
                            "token": token, "identity": identity,
                            "consumer": {"pid": os.getpid(), "pid_start": start},
                            "requested_at": time.time(),
                        },
                    })
                    incarnation = "real-authority-transition"
                    peer_steward.shared_readiness.publish_runner_incarnation(
                        store.root, os.getpid(), start, incarnation)
                    jobs_before = jobs.read_bytes()
                    snapshot = {"watch_root": watch_root, "watches": ((watch_id, arm),)}

                    def publish_ready_response(expected_state="ready"):
                        transport = mock.Mock()
                        transport.observe.return_value = object()
                        with mock.patch.object(
                                peer_steward.herdr_observation,
                                "socket_path_for_endpoint", return_value=socket_path), \
                                mock.patch.object(peer_steward,
                                    "_runner_readiness_transport", return_value=transport), \
                                mock.patch.object(peer_steward,
                                    "_target_from_readiness_view", return_value=(
                                        "idle", agent, 0, "")), \
                                mock.patch.object(peer_steward, "_retire_pane_info", return_value={
                                    "shell_pid": os.getpid(), "foreground_processes": []}), \
                                mock.patch.object(peer_steward.peer_obligations,
                                    "execution_attempts_for_processes", return_value=(owner,)), \
                                patch_split(peer_steward,
                                    "_wake_watch_observer", return_value=True):
                            peer_steward._publish_watch_readiness(
                                store, snapshot,
                                read_context=PeerReadContext(registry_append_only=True),
                                incarnation=incarnation)
                        response = peer_steward.shared_readiness.watch_transition_result(
                            store.root, watch_id, identity, token)
                        self.assertEqual(response["state"], expected_state,
                                         response.get("reason"))
                        return response

                    # The exact executor remains ready; the separate task is
                    # still owned by fixture-parent and is not yet selected.
                    first = publish_ready_response()
                    self.assertGreaterEqual(len(first["authority"]["rows"]), 2)

                    # Publish an incoming handover through the target pane's
                    # existing seat lock and ledger writer. Registry bytes stay fixed.
                    import dispatch_seat_handover as handover
                    import session_tidy
                    scope_env = {"AGENT_HERDR_SESSION": server,
                                 "HERDR_SOCKET_PATH": socket_path}
                    endpoint_scope = session_tidy._endpoint_scope(scope_env)
                    seat_key = session_tidy._digest("pane", endpoint_scope, agent["pane"])
                    seat = session_tidy.Seat(
                        "pane", seat_key, agent["pane"], "claude", endpoint=endpoint_scope)
                    binding = {
                        "jobs": str(jobs.resolve()), "attempt": task_attempt,
                        "route": task_route, "hash": task_hash,
                        "node": task_node, "parent": "fixture-parent",
                    }
                    st = session_tidy
                    snapshot_value = {
                        "schema": handover.SCHEMA, "seat": seat.as_dict(),
                        "from": {"harness": "claude", "sid": "sid-a"},
                        "at": time.time(), "bindings": [binding],
                    }
                    ledger_row = {
                        "ts": time.time(), "harness": "claude", "sid": "sid-a",
                        "event": handover.ROW_EVENT, "from": "fixture-parent",
                        "source": "fixture", "bindings": [binding],
                    }
                    with st.seat_lock(seat.key):
                        st.atomic_write_json(handover._snapshot_path(seat.key), snapshot_value)
                        st._append_ledger_entry(seat, ledger_row)
                    self.assertEqual(jobs.read_bytes(), jobs_before)

                    receipt = {"schema_version": 1, "watch_id": watch_id,
                               "state": "idle", "agent": agent}
                    with mock.patch.dict(os.environ, {
                            "AGENT_DISPATCH_JOBS": str(jobs),
                            "AGENT_ARTIFACT_ROOT": str(fixture.root)}), \
                            mock.patch.object(peer_steward.herdr_observation,
                                "socket_path_for_endpoint", return_value=socket_path), \
                            mock.patch.object(peer_steward, "_retire_target", return_value=(
                                "idle", agent, 0, "")), \
                            mock.patch.object(peer_steward, "_retire_pane_info", return_value={
                                "shell_pid": os.getpid(), "foreground_processes": []}), \
                            mock.patch.object(peer_steward.peer_obligations,
                                "execution_attempts_for_processes", return_value=(owner,)):
                        accepted = peer_steward._watch_publish_ready_receipt(
                            "agent-a", "idle", arm,
                            {"pane": agent["pane"], "harness": "claude",
                             "session_id": "sid-a"}, server, watch_id, paths, receipt)
                    self.assertFalse(accepted, "the published ready response omitted the new handover")
                    self.assertFalse(paths.receipt.exists())
                    self.assertEqual(peer_steward._read_json(paths.observation)["state"], "pending")
                    self.assertEqual(jobs.read_bytes(), jobs_before)

                    # The runner sees the new parent binding, but its exact
                    # result delivery is still open, so the watch stays pending.
                    pending_response = publish_ready_response("pending")
                    self.assertEqual(pending_response["reason"],
                                     "bound-registered-work-pending")
                    pending_authority = pending_response["authority"]
                    self.assertGreaterEqual(len(pending_authority["rows"]), 3)
                    self.assertGreater(len(pending_authority["rows"]), 6)
                    generated_row = next(
                        row for row in pending_authority["rows"]
                        if row["metadata"].get("attempt_id") == "att-generated-related-000")
                    self.assertGreater(len(generated_row["metadata"]), 64)
                    self.assertGreater(len(generated_row["fields"][5]), 4096)
                    self.assertGreater(max(map(len, generated_row["metadata"].values())), 2048)
                    self.assertEqual(generated_row["metadata"],
                                     generated_row["completion_metadata"])
                    authority_wire_bytes = len(json.dumps(
                        pending_authority, sort_keys=True,
                        separators=(",", ":")).encode("ascii"))
                    self.assertGreater(authority_wire_bytes, 65536)
                    self.assertEqual(jobs.read_bytes(), jobs_before)

                    # A real claim keeps the obligation open. Only the exact
                    # recipient ACK settles this parent delivery.
                    pending.claim(jobs.parent, "fixture-parent", task_delivery_id,
                                  claim_owner="watch-parent-carrier", lease_seconds=30)
                    claimed_response = publish_ready_response("pending")
                    self.assertEqual(claimed_response["reason"],
                                     "bound-registered-work-pending")
                    pending.ack(jobs.parent, "fixture-parent", task_delivery_id,
                                acked_by="watch-parent-recipient")
                    response = publish_ready_response()
                    first_authority = response["authority"]
                    self.assertGreaterEqual(len(first_authority["rows"]), 3)
                    self.assertTrue(peer_steward.shared_readiness.valid_watch_authority(
                        first_authority))

                    # Revoke the actual terminal claim after ready publication.
                    claim_before = claim_path.read_bytes()
                    claim = json.loads(claim_before)
                    claim["proof"] = {}
                    claim_path.write_text(json.dumps(claim), encoding="utf-8")
                    with mock.patch.dict(os.environ, {
                            "AGENT_DISPATCH_JOBS": str(jobs),
                            "AGENT_ARTIFACT_ROOT": str(fixture.root)}), \
                            mock.patch.object(peer_steward.herdr_observation,
                                "socket_path_for_endpoint", return_value=socket_path), \
                            mock.patch.object(peer_steward, "_retire_target", return_value=(
                                "idle", agent, 0, "")), \
                            mock.patch.object(peer_steward, "_retire_pane_info", return_value={
                                "shell_pid": os.getpid(), "foreground_processes": []}), \
                            mock.patch.object(peer_steward.peer_obligations,
                                "execution_attempts_for_processes", return_value=(owner,)):
                        self.assertFalse(peer_steward._watch_publish_ready_receipt(
                            "agent-a", "idle", arm,
                            {"pane": agent["pane"], "harness": "claude",
                             "session_id": "sid-a"}, server, watch_id, paths, receipt))
                    self.assertFalse(paths.receipt.exists())
                    claim_path.write_bytes(claim_before)

                    # A separate actual cleanup/gate record also stays visible
                    # while jobs.log remains byte-identical.
                    goal_before = goal_path.read_bytes() if goal_path.exists() else None
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    goal_path.parent.mkdir(parents=True, exist_ok=True)
                    goal_path.write_text(json.dumps({
                        "status": "pending",
                        "terminal_commit_id": state["terminal_commit_id"],
                    }), encoding="utf-8")
                    with mock.patch.dict(os.environ, {
                            "AGENT_DISPATCH_JOBS": str(jobs),
                            "AGENT_ARTIFACT_ROOT": str(fixture.root)}), \
                            mock.patch.object(peer_steward.herdr_observation,
                                "socket_path_for_endpoint", return_value=socket_path), \
                            mock.patch.object(peer_steward, "_retire_target", return_value=(
                                "idle", agent, 0, "")), \
                            mock.patch.object(peer_steward, "_retire_pane_info", return_value={
                                "shell_pid": os.getpid(), "foreground_processes": []}), \
                            mock.patch.object(peer_steward.peer_obligations,
                                "execution_attempts_for_processes", return_value=(owner,)):
                        self.assertEqual(peer_steward._WATCH_RECEIPT_PENDING,
                                         peer_steward._watch_publish_ready_receipt(
                            "agent-a", "idle", arm,
                            {"pane": agent["pane"], "harness": "claude",
                             "session_id": "sid-a"}, server, watch_id, paths, receipt))
                    self.assertFalse(paths.receipt.exists())
                    if goal_before is None:
                        goal_path.unlink()
                    else:
                        goal_path.write_bytes(goal_before)
                    self.assertEqual(jobs.read_bytes(), jobs_before)

                    # Once the external authority is clear, publish a fresh
                    # response and prove the real receipt edge succeeds. Hold
                    # the receipt write briefly to show a cooperating seat
                    # writer cannot cross that edge.
                    response = publish_ready_response()
                    selected_authority = response["authority"]
                    with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}):
                        selected_work, selected_state = peer_steward.peer_obligations.bound_work_for_pane(
                            agent["pane"], "claude", "sid-a", jobs=jobs,
                            execution_attempt_ids=(owner,),
                            include_parent=True,
                            registry_append_only=True,
                            read_context=PeerReadContext(registry_append_only=True),
                            selected_rows=selected_authority["rows"],
                            expected_handover_keys=selected_authority["handover_keys"])
                    self.assertEqual(selected_state, "observed")
                    executor_work = next(work for work in selected_work
                                         if work.attempt_id == owner)
                    parent_work = next(work for work in selected_work
                                       if work.attempt_id == task_attempt)
                    self.assertEqual((executor_work.pane_relation,
                                      executor_work.owner_completion_state),
                                     ("executor", "settled"), selected_work)
                    self.assertEqual((parent_work.pane_relation,
                                      parent_work.owner_completion_state,
                                      parent_work.delivery_state),
                                     ("parent", "pending", "acked"), selected_work)
                    with mock.patch.object(peer_steward.peer_obligations,
                                           "execution_attempts_for_processes",
                                           return_value=(owner,)):
                        direct_readiness = peer_steward._pane_readiness(
                            "agent-a", "idle", expected_harness="claude",
                            expected_sid="sid-a", expected_pane=agent["pane"],
                            target_observation=("idle", agent),
                            pane_info={"shell_pid": os.getpid(), "foreground_processes": []},
                            registry_append_only=True,
                            read_context=PeerReadContext(registry_append_only=True),
                            selected_authority=selected_authority,
                            server=server)
                    self.assertEqual((direct_readiness.state, direct_readiness.reason),
                                     ("ready", "native-turn-ready"), direct_readiness)
                    receipt_write_entered = threading.Event()
                    release_receipt_write = threading.Event()
                    writer_started = threading.Event()
                    writer_entered = threading.Event()
                    claim_writer_started = threading.Event()
                    claim_writer_entered = threading.Event()
                    result_holder = {}
                    real_write = peer_steward._write_json_atomic
                    def blocked_receipt_write(path, value):
                        if Path(path) == paths.receipt:
                            receipt_write_entered.set()
                            if not release_receipt_write.wait(5):
                                raise TimeoutError("receipt lock fixture timed out")
                        return real_write(path, value)
                    expected = {"pane": agent["pane"], "harness": "claude",
                                "session_id": "sid-a"}
                    import dispatch_registry_cache
                    import owner_route_binding
                    def reject_registry_reparse(*args, **kwargs):
                        import traceback
                        traceback.print_stack()
                        raise AssertionError("receipt consumer rebuilt RegistryView")
                    with mock.patch.dict(os.environ, {
                            "AGENT_DISPATCH_JOBS": str(jobs),
                            "AGENT_ARTIFACT_ROOT": str(fixture.root)}), \
                            mock.patch.object(peer_steward.herdr_observation,
                                "socket_path_for_endpoint", return_value=socket_path), \
                            mock.patch.object(peer_steward, "_retire_target", return_value=(
                                "idle", agent, 0, "")), \
                            mock.patch.object(peer_steward, "_retire_pane_info", return_value={
                                "shell_pid": os.getpid(), "foreground_processes": []}), \
                            mock.patch.object(peer_steward.peer_obligations,
                                "execution_attempts_for_processes", return_value=(owner,)), \
                            patch_split(peer_steward, "_write_json_atomic",
                                              side_effect=blocked_receipt_write), \
                            mock.patch.object(dispatch_registry_cache, "registry_view",
                                side_effect=reject_registry_reparse), \
                            mock.patch.object(owner_route_binding, "registry_view",
                                side_effect=reject_registry_reparse):
                        consumer = threading.Thread(target=lambda: result_holder.setdefault(
                            "accepted", peer_steward._watch_publish_ready_receipt(
                                "agent-a", "idle", arm, expected, server,
                                watch_id, paths, receipt)))
                        transition_started = time.perf_counter()
                        consumer.start()
                        self.assertTrue(receipt_write_entered.wait(3),
                                        f"consumer did not reach receipt write: {result_holder!r}; "
                                        f"observation={peer_steward._read_json(paths.observation)!r}")
                        transition_check_seconds = time.perf_counter() - transition_started
                        def cooperating_handover_writer():
                            writer_started.set()
                            with st.seat_lock(seat.key):
                                writer_entered.set()
                        def cooperating_claim_writer():
                            fd = os.open(str(jobs) + ".lock",
                                         os.O_RDWR | os.O_CREAT, 0o600)
                            try:
                                claim_writer_started.set()
                                fcntl.flock(fd, fcntl.LOCK_EX)
                                claim_writer_entered.set()
                                fcntl.flock(fd, fcntl.LOCK_UN)
                            finally:
                                os.close(fd)
                        writer = threading.Thread(target=cooperating_handover_writer)
                        claim_writer = threading.Thread(target=cooperating_claim_writer)
                        writer.start()
                        claim_writer.start()
                        self.assertTrue(writer_started.wait(1))
                        self.assertTrue(claim_writer_started.wait(1))
                        self.assertTrue(receipt_write_entered.is_set(),
                                        f"receipt transition did not publish: {result_holder!r}")
                        self.assertFalse(writer_entered.wait(0.1),
                                         "target seat writer crossed the receipt transition lock")
                        self.assertFalse(claim_writer_entered.wait(0.1),
                                         "terminal claim writer crossed the jobs lock")
                        release_receipt_write.set()
                        consumer.join(3)
                        writer.join(3)
                        claim_writer.join(3)
                    self.assertFalse(consumer.is_alive())
                    self.assertFalse(writer.is_alive())
                    self.assertTrue(result_holder.get("accepted"))
                    self.assertTrue(writer_entered.is_set())
                    self.assertTrue(claim_writer_entered.is_set())
                    self.assertEqual(peer_steward._read_json(paths.receipt)["state"], "idle")
                    self.assertEqual(jobs.read_bytes(), jobs_before)
                    transfer_ref = peer_steward._prepare_watch_notice(watch_id, arm, receipt)
                    self.assertTrue(transfer_ref)
                    peer_steward._record(
                        to_harness=agent["harness"], to_name=agent["name"],
                        kind="notice", ref=[], receipt=watch_id, status="received",
                        summary_text=f"[notice] watch {watch_id} state=idle target=agent-a",
                        from_identity=(steward_sid, "claude", str(base)))
                    notice_rows = []
                    for ledger in (peer_steward.peer_message.peer_state_root()
                                   / "peer-messages").rglob("*.jsonl"):
                        for line in ledger.read_text(encoding="utf-8").splitlines():
                            if line.strip():
                                row = json.loads(line)
                                if (row.get("kind") == "notice"
                                        and (row.get("delivery") or {}).get("receipt") == watch_id):
                                    notice_rows.append(row)
                    self.assertEqual(len(notice_rows), 1)
                    from dispatch_seat_handover import acknowledge_watch, watch_ack_valid
                    self.assertEqual(acknowledge_watch(
                        watch_id, "claude", steward_sid, "large-authority-test"), "created")
                    self.assertTrue(watch_ack_valid(paths.ack, watch_id, "claude", steward_sid))
                    print(f"watch_authority_rows={len(first_authority['rows'])} "
                          f"watch_authority_wire_bytes={authority_wire_bytes} "
                          f"generated_metadata_keys={len(generated_row['metadata'])} "
                          f"generated_raw_field_chars={len(generated_row['fields'][5])} "
                          f"generated_max_value_chars={max(map(len, generated_row['metadata'].values()))}")
                    print(f"watch_receipt_authority_check_seconds={transition_check_seconds:.6f} "
                          "transport=fake target-observation=fake check=selected-authority "
                          "receipt=notice=ack=verified")
        finally:
            terminal_case.doCleanups()


class RegisteredDeliverySettlementTest(_TmpRootMixin, unittest.TestCase):
    CARRIER = "claude-parent-runtime"

    def _registered_duty(self, attempt, *, status="done", pid=None, start=None,
                         worker_type="stage", ns=None):
        import dispatch_completion_join as join
        import dispatch_contract as D

        jobs = self.jobs_path
        artifact_root = self.tmp_root / "artifacts"
        artifact_root.mkdir(parents=True, exist_ok=True)
        route_hash = "sha256:" + "a" * 64
        route_id = "rt-" + "a" * 16
        recipient = "recipient-" + attempt
        metadata = {
            "attempt_schema_version": "2",
            "dispatch_depth": "1",
            "transport": "headless",
            "execution_surface": "registered-headless",
            "registered_worker": "1",
            "fallback_hop": "same-harness-headless",
            "attempt_id": attempt,
            "parent_attempt_id": "att-d3-parent",
            "worker_type": worker_type,
            "harness": "codex",
            "parent_sid": recipient,
            "parent_harness": "claude",
            "parent_completion_delivery": self.CARRIER,
            "route_id": route_id,
            "route_hash": route_hash,
            "route_node": "execute",
            "artifact_root": str(artifact_root),
        }
        if pid is not None:
            metadata["parent_runtime_pid"] = str(pid)
        if start is not None:
            metadata["parent_runtime_pid_start"] = str(start)
        if ns is not None:
            metadata["parent_runtime_ns"] = str(ns)
        pipe = ",".join(f"{key}={value}" for key, value in metadata.items())
        initial_status = "open" if status == "done" else status
        jobs.write_text(
            f"2026-10-23T00:00:00Z\t{initial_status}\t{self.tmp_root}\t{self.tmp_root}\t"
            f"execute\t{pipe}\n",
            encoding="utf-8",
        )
        if status == "done":
            self.assertTrue(D.close_attempt_row(jobs, attempt, "fixture-complete"))
            self.assertIsNotNone(join.materialize_after_terminal_close(jobs, attempt))
        store = peer_steward.peer_obligations.ObligationStore()
        duty_id = "registered-batch-d3-" + attempt.replace("-", "")
        identity = {"jobs": str(jobs), "attempt_id": attempt, "harness": "claude",
                    "session_id": recipient, "server": "default"}
        duty = store.create(duty_id, "registered-batch", identity, {"carrier": self.CARRIER})
        row = join.current_attempt_row(jobs, attempt)
        record = None
        if row is not None and row.metadata.get("delivery_id"):
            record = join.pending_delivery.read(
                jobs.parent, recipient, row.metadata["delivery_id"])
        return store, duty, row, record, artifact_root, route_id, route_hash

    def _resume_with_agents(self, duty, store, agents=()):
        payload = {"result": {"agents": list(agents)}}
        with mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                stdout=json.dumps(payload), returncode=0)):
            peer_steward._resume_registered_obligation(duty, store)

    def test_dead_recipient_expires_and_completes_without_sweep(self):
        import dispatch_completion_join as join

        store, duty, row, record, _root, _route_id, _route_hash = self._registered_duty(
            "att-d3-dead", pid=999999999, start="123456789")
        self.assertIsNotNone(record)
        with mock.patch.object(join, "reconcile_pending_delivery",
                               side_effect=AssertionError("global sweep")):
            self._resume_with_agents(duty, store)
            expired = join.pending_delivery.read(
                self.jobs_path.parent, row.metadata["parent_sid"], record["delivery_id"])
            self.assertEqual(expired["state"], "expired")
            self.assertEqual(store.get(duty["id"])["state"], "pending")
            self._resume_with_agents(store.get(duty["id"]), store)
        settled = store.get(duty["id"])
        self.assertEqual((settled["state"], settled["delivery"], settled["cleanup"]),
                         ("complete", "expired", "complete"))
        retained = join.pending_delivery.read(
            self.jobs_path.parent, row.metadata["parent_sid"], record["delivery_id"])
        self.assertEqual(retained["state"], "expired")
        self.assertEqual(retained["expiry_reason"], "recipient-session-gone")

    def test_closed_route_without_runtime_pid_is_not_required(self):
        import dispatch_completion_join as join
        import artifact_lifecycle

        store, duty, row, record, root, route_id, route_hash = self._registered_duty(
            "att-d3-closed")
        outcome_path = artifact_lifecycle.canonical_outcome_path(root, route_id)
        outcome_path.parent.mkdir(parents=True, exist_ok=True)
        closed_at = "2026-10-23T00:00:00Z"
        outcome_path.write_text(json.dumps({
            "schema_version": 4, "route_id": route_id, "route_hash": route_hash,
            "closed_at": closed_at,
        }), encoding="utf-8")
        self._resume_with_agents(duty, store)
        settled = store.get(duty["id"])
        self.assertEqual((settled["state"], settled["delivery"], settled["cleanup"]),
                         ("complete", "not-required", "complete"))
        self.assertEqual(settled["observation"], {
            "reason": "route-closed", "route_id": route_id,
            "route_hash": route_hash, "closed_at": closed_at,
            "recipient_sid": row.metadata["parent_sid"],
        })
        retained = join.pending_delivery.read(
            self.jobs_path.parent, row.metadata["parent_sid"], record["delivery_id"])
        self.assertEqual(retained["state"], "pending")

    def _close_route(self, root, route_id, route_hash):
        import artifact_lifecycle

        outcome_path = artifact_lifecycle.canonical_outcome_path(root, route_id)
        outcome_path.parent.mkdir(parents=True, exist_ok=True)
        closed_at = "2026-10-23T00:00:00Z"
        outcome_path.write_text(json.dumps({
            "schema_version": 4, "route_id": route_id, "route_hash": route_hash,
            "closed_at": closed_at,
        }), encoding="utf-8")
        return closed_at

    def _own_runtime(self):
        import dispatch_contract_process as process

        _visibility, start, _state = process._proc_observation(os.getpid())
        return os.getpid(), start

    def test_closed_route_with_live_parent_runtime_keeps_completion_undelivered(self):
        # #1410: a plain `/clear` leaves the registered parent runtime alive under a new session
        # id. A closed route alone does not make the still-open completion "not required".
        import dispatch_completion_join as join

        pid, start = self._own_runtime()
        store, duty, row, record, root, route_id, route_hash = self._registered_duty(
            "att-d3-replaced", pid=pid, start=start)
        closed_at = self._close_route(root, route_id, route_hash)
        self._resume_with_agents(duty, store)
        kept = store.get(duty["id"])
        self.assertNotEqual(kept["state"], "complete")
        self.assertEqual(kept["observation"], {
            "reason": "completion-undelivered", "route_id": route_id, "route_hash": route_hash,
            "closed_at": closed_at, "recipient_sid": row.metadata["parent_sid"],
            "recipient_pid": str(pid), "recipient_pid_start": str(start)})
        retained = join.pending_delivery.read(
            self.jobs_path.parent, row.metadata["parent_sid"], record["delivery_id"])
        self.assertEqual((retained["state"], retained["attempts"]), ("pending", record["attempts"]))
        # The same observation is not written again on a later wake.
        path = store._record_path(duty["id"])
        before = (kept["revision"], kept["updated_at"], path.stat().st_mtime_ns)
        self._resume_with_agents(kept, store)
        again = store.get(duty["id"])
        self.assertEqual((again["revision"], again["updated_at"], path.stat().st_mtime_ns), before)
        # The session that holds the record may still receive it: its own ACK closes the duty
        # through the ordinary settled-record branch.
        sid = row.metadata["parent_sid"]
        join.pending_delivery.claim(self.jobs_path.parent, sid, record["delivery_id"],
                                    claim_owner="session-sweep:" + sid, lease_seconds=30,
                                    require_generation_proof=False, jobs=self.jobs_path)
        join.pending_delivery.ack(self.jobs_path.parent, sid, record["delivery_id"],
                                  acked_by="session-sweep:" + sid)
        self._resume_with_agents(again, store)
        settled = store.get(duty["id"])
        self.assertEqual((settled["state"], settled["delivery"], settled["cleanup"]),
                         ("complete", "acked", "complete"))

    def test_closed_route_with_dead_parent_runtime_expires_before_completing(self):
        import dispatch_completion_join as join

        store, duty, row, record, root, route_id, route_hash = self._registered_duty(
            "att-d3-dead-closed", pid=999999999, start="123456789")
        self._close_route(root, route_id, route_hash)
        self._resume_with_agents(duty, store)
        expired = join.pending_delivery.read(
            self.jobs_path.parent, row.metadata["parent_sid"], record["delivery_id"])
        self.assertEqual((expired["state"], expired["expiry_reason"]),
                         ("expired", "recipient-session-gone"))
        first = store.get(duty["id"])
        self.assertEqual(first["state"], "pending")
        self.assertEqual(first["observation"]["reason"], "recipient-session-gone")
        self._resume_with_agents(first, store)
        settled = store.get(duty["id"])
        self.assertEqual((settled["state"], settled["delivery"], settled["cleanup"]),
                         ("complete", "expired", "complete"))

    def test_closed_route_with_unobservable_parent_runtime_stays_not_required(self):
        import dispatch_completion_join as join

        pid, start = self._own_runtime()
        store, duty, row, record, root, route_id, route_hash = self._registered_duty(
            "att-d3-unresolved", pid=pid, start=start)
        self._close_route(root, route_id, route_hash)
        with mock.patch("dispatch_contract.process_identity_disposition", return_value="unresolved"):
            self._resume_with_agents(duty, store)
        settled = store.get(duty["id"])
        self.assertEqual((settled["state"], settled["delivery"], settled["cleanup"]),
                         ("complete", "not-required", "complete"))
        self.assertEqual(settled["observation"]["reason"], "route-closed")
        self.assertEqual(join.pending_delivery.read(
            self.jobs_path.parent, row.metadata["parent_sid"], record["delivery_id"])["state"], "pending")

    def test_closed_route_with_live_runtime_still_settles_a_notice_duty_not_required(self):
        # The new "stay open" rule is for the owner's completion record. A supervision or gate
        # notice duty (`notice_delivery_id`) has no hook that shows or expires its record, so it
        # keeps the route-closed settlement even while the registered runtime is live.
        import dispatch_completion_join as join

        pid, start = self._own_runtime()
        store, completion_duty, row, _record, root, route_id, route_hash = self._registered_duty(
            "att-d3-notice", pid=pid, start=start)
        self._close_route(root, route_id, route_hash)
        sid = row.metadata["parent_sid"]
        body = {"schema_version": 1, "kind": "supervision", "owner_attempt_id": row.attempt_id,
                "reason": "attempt-stalled"}
        notice_id = "delivery-" + "9" * 32
        notice = join.pending_delivery.create(
            self.jobs_path.parent, recipient_kind=self.CARRIER, recipient_key=sid,
            delivery_id=notice_id, session_generation="", session_generation_supported="0",
            attempt_ids=[row.attempt_id], parent_attempt_id=row.attempt_id, route_id=route_id,
            route_node="execute", receipt=body,
            receipt_digest=join.pending_delivery._canonical_receipt_digest(body),
            row_revisions={row.attempt_id: "revision-notice"})
        identity = {"jobs": str(self.jobs_path), "attempt_id": row.attempt_id, "harness": "claude",
                    "session_id": sid, "server": "default"}
        notice_duty = store.create(
            "registered-batch-d3-notice-duty", "registered-batch", identity,
            {"carrier": self.CARRIER, "notice_delivery_id": notice_id,
             "notice_digest": notice["receipt_digest"]})
        # Control: the owner's completion duty on the same row and runtime stays open.
        self._resume_with_agents(completion_duty, store)
        self.assertEqual(store.get(completion_duty["id"])["observation"]["reason"],
                         "completion-undelivered")
        current = {"owner_attempt_id": row.attempt_id, "job_registry": str(self.jobs_path),
                   "sealed_batch_id": ""}
        with mock.patch("dispatch_supervision.validate", return_value=current), \
             mock.patch("dispatch_supervision.notice_is_current", return_value=True):
            self._resume_with_agents(notice_duty, store)
        settled = store.get(notice_duty["id"])
        self.assertEqual((settled["state"], settled["delivery"], settled["cleanup"]),
                         ("complete", "not-required", "complete"))
        self.assertEqual(settled["observation"]["reason"], "route-closed")
        self.assertEqual(join.pending_delivery.read(
            self.jobs_path.parent, sid, notice_id)["state"], "pending")

    def test_clear_scenario_settles_through_notice_and_closes_the_duty(self):
        # The whole #1410 path: the duty stays open, the next prompt of the new session in the same
        # pane and runtime shows one notice and expires the record, and the duty then completes.
        import dispatch_completion_join as join
        import dispatch_contract_process as process
        import session_tidy

        pid, start = self._own_runtime()
        ns = process.process_namespace_identity(os.getpid())
        store, duty, row, record, root, route_id, route_hash = self._registered_duty(
            "att-d3-clear", pid=pid, start=start, ns=ns)
        self._close_route(root, route_id, route_hash)
        os.environ["HERDR_PANE_ID"] = "fixture:clear-scenario-" + self.tmp_root.name
        self.addCleanup(os.environ.pop, "HERDR_PANE_ID", None)
        original = row.metadata["parent_sid"]
        for sid, source in ((original, "startup"), ("sess-after-clear", "clear")):
            session_tidy.run_hook("claude", "start", sid, source=source, cwd=str(self.tmp_root),
                                  env=dict(os.environ), render_context=False)
        self._resume_with_agents(duty, store)
        self.assertEqual(store.get(duty["id"])["observation"]["reason"], "completion-undelivered")

        hook_spec = importlib.util.spec_from_file_location(
            "clear_scenario_sweep_hook", Path(__file__).resolve().parents[1] / "hooks/dispatch-session-sweep.py")
        hook = importlib.util.module_from_spec(hook_spec)
        hook_spec.loader.exec_module(hook)
        output = io.StringIO()
        with mock.patch.object(hook, "resolve_agent_home", return_value=Path(__file__).resolve().parents[1]), \
             mock.patch.object(hook, "dispatch_state_roots", return_value=(self.jobs_path.parent,)), \
             mock.patch.object(hook, "request_obligation_reconciliation"), \
             mock.patch.object(process, "runtime_ancestry_binding", return_value=(str(pid), str(start), ns)), \
             mock.patch.object(sys, "stdin", io.StringIO(json.dumps({
                 "session_id": "sess-after-clear", "hook_event_name": "UserPromptSubmit"}))), \
             mock.patch.object(sys, "stdout", output):
            self.assertEqual(hook.main(), 0)
        context = json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(sum(line.startswith("previous_session_completion:") for line in context.splitlines()), 1)
        expired = join.pending_delivery.read(
            self.jobs_path.parent, original, record["delivery_id"])
        self.assertEqual((expired["state"], expired["expiry_reason"]),
                         ("expired", "recipient-session-replaced"))
        self._resume_with_agents(store.get(duty["id"]), store)
        settled = store.get(duty["id"])
        self.assertEqual((settled["state"], settled["delivery"], settled["cleanup"]),
                         ("complete", "expired", "complete"))

    def test_first_same_seat_adoption_keeps_closed_route_duty_pending(self):
        import artifact_lifecycle
        import dispatch_seat_handover as handover
        import dispatch_completion_join as join
        import pane_ownership
        import session_tidy

        store, duty, row, record, root, route_id, route_hash = self._registered_duty(
            "att-d3-closed-first-binding", worker_type="frame")
        outcome_path = artifact_lifecycle.canonical_outcome_path(root, route_id)
        outcome_path.parent.mkdir(parents=True, exist_ok=True)
        outcome_path.write_text(json.dumps({
            "schema_version": 4, "route_id": route_id, "route_hash": route_hash,
            "closed_at": "2026-10-23T00:00:00Z",
        }), encoding="utf-8")
        pane = "fixture:closed-first-binding-" + self.tmp_root.name
        env = {"XDG_STATE_HOME": str(self.tmp_root / "state"),
               "HERDR_PANE_ID": pane, "AGENT_DISPATCH_JOBS": str(self.jobs_path)}
        proof = mock.patch.object(pane_ownership, "verified_pane",
                                  side_effect=lambda value, *a, **kw: value or "")
        proof.start()
        self.addCleanup(proof.stop)
        successor = "sess-closed-first-binding-successor"
        with mock.patch.dict(os.environ, env, clear=False):
            seat = session_tidy.resolve_seat("claude", str(self.tmp_root), os.environ)
            self.assertIsNone(handover.read_snapshot(seat.key))
            with session_tidy.seat_lock(seat.key):
                now = session_tidy.now_epoch()
                session_tidy.record_event(seat, "claude", row.metadata["parent_sid"], "start",
                                          cwd=str(self.tmp_root), now=now - 2)
                self.assertEqual(handover.publish_snapshot_locked(
                    seat, "claude", row.metadata["parent_sid"],
                    [handover.binding_of(self.jobs_path, row.metadata)],
                    jobs=self.jobs_path, now=now - 1), 1)
                session_tidy.record_event(seat, "claude", successor, "start",
                                          source="clear", cwd=str(self.tmp_root), now=now)
                self.assertIsNotNone(handover.record_locked(
                    seat, "claude", successor, "start", "clear", now))
            self.assertEqual(handover.effective_parent(row.metadata, self.jobs_path), successor)
            self._resume_with_agents(duty, store)
        self.assertEqual(store.get(duty["id"])["state"], "pending")
        self.assertEqual(join.pending_delivery.read(
            self.jobs_path.parent, row.metadata["parent_sid"], record["delivery_id"])["state"],
            "pending")

    def test_identical_unknown_observation_does_not_rewrite(self):
        store, duty, _row, _record, _root, _route_id, _route_hash = self._registered_duty(
            "att-d3-unknown", status="open")
        self._resume_with_agents(duty, store)
        first = store.get(duty["id"])
        path = store._record_path(duty["id"])
        before = (first["revision"], first["updated_at"], path.stat().st_mtime_ns,
                  json.loads(path.read_text(encoding="utf-8"))["observation"])
        self._resume_with_agents(first, store)
        second = store.get(duty["id"])
        after = (second["revision"], second["updated_at"], path.stat().st_mtime_ns,
                 json.loads(path.read_text(encoding="utf-8"))["observation"])
        self.assertEqual(second["observation"]["reason"], "recipient-unavailable")
        self.assertEqual(after, before)

    def test_unknown_liveness_never_expires_a_terminal_delivery(self):
        import dispatch_completion_join as join

        store, duty, row, record, _root, _route_id, _route_hash = self._registered_duty(
            "att-d3-no-pid")
        self.assertIsNotNone(record)
        with mock.patch.object(join, "process_identity_disposition",
                               side_effect=AssertionError("no runtime PID")):
            self._resume_with_agents(duty, store)
        self.assertEqual(store.get(duty["id"])["state"], "pending")
        retained = join.pending_delivery.read(
            self.jobs_path.parent, row.metadata["parent_sid"], record["delivery_id"])
        self.assertEqual(retained["state"], "pending")

    def test_live_second_successor_receives_despite_dead_original(self):
        import dispatch_completion_join as join
        import dispatch_pending_delivery as pending
        import dispatch_supervision as supervision
        from harness_capabilities import parent_completion

        fixture = ClosedParentNativeHandoverTest()
        fixture.setUp()
        try:
            fixture.meta.update({
                "parent_runtime_pid": "999999991",
                "parent_runtime_pid_start": "123456789",
            })
            fixture.write_jobs()
            fixture.replay()  # Original A handed to the real first successor B.
            first = fixture.handover.effective_parent(fixture.meta, fixture.jobs_path)
            self.assertEqual(first, fixture.SUCCESSOR)
            second = "claude-second-successor"
            second_seat = fixture.tidy._pane_seat_of("w1:pThird")
            fixture.handover.record_retire_handover(
                first, fixture.SUCCESSOR_HARNESS, second, "claude",
                jobs=fixture.jobs_path, successor_seat=second_seat)
            self.assertEqual(fixture.handover.effective_parent(fixture.meta, fixture.jobs_path), second)

            carrier = parent_completion("opencode")["carrier"]
            delivery_id = "delivery-" + "d" * 32
            revision = "revision-d3-live-successor"
            receipt = {
                "schema_version": 2, "state": "delivered",
                "job_registry": str(fixture.jobs_path),
                "parent_attempt_id": "att-selected",
                "delivery_classification": "attention",
                "children": [{
                    "attempt_id": "att-selected", "status": "done",
                    "readiness": "ready", "reason": "terminal-failure-or-unclosed",
                    "required_action": "inspect-done-failure",
                    "harness": "opencode", "delivery_classification": "attention",
                }],
            }
            digest = pending._canonical_receipt_digest(receipt)
            metadata = {
                **fixture.meta,
                "parent_completion_delivery": carrier,
                "delivery_intent": "1",
                "delivery_id": delivery_id,
                "delivery_receipt_digest": digest,
                "delivery_row_revision": revision,
                "delivery_recipient_kind": carrier,
                "delivery_receipt_b64": join.seal_delivery_receipt(receipt),
            }
            fixture.rows[2] = ("done", metadata)
            fixture.write_jobs()
            pending.create(
                fixture.jobs_path.parent, recipient_kind=carrier, recipient_key=fixture.PARENT,
                delivery_id=delivery_id, session_generation="", session_generation_supported="0",
                attempt_ids=["att-selected"], parent_attempt_id="att-selected",
                route_id=fixture.route["route_id"], route_node="_owner",
                receipt=receipt, receipt_digest=digest, row_revisions={"att-selected": revision},
            )
            identity = {
                "jobs": str(fixture.jobs_path), "attempt_id": "att-selected",
                "harness": "opencode", "session_id": fixture.PARENT, "server": "default",
            }
            store = peer_steward.peer_obligations.ObligationStore()
            duty_id = peer_steward.peer_obligations.stable_duty_id("registered-batch", identity)
            duty = store.create(duty_id, "registered-batch", identity, {"carrier": carrier})
            sent = []

            def courier(args, *, expected_recipient, on_prepared, producer_binding):
                self.assertEqual(expected_recipient, ("claude", second))
                body = Path(args.body_file).read_text(encoding="utf-8")
                recipient = {"harness": expected_recipient[0], "session_id": expected_recipient[1]}
                sender = {"harness": "runtime", "session_id": None, "name": "Hearting runtime"}
                text, ref = peer_steward.peer_message.prepare_peer_message(
                    body, sender, recipient, defer=True,
                    refs=[duty_id, "att-selected", delivery_id],
                    producer_binding=producer_binding,
                )
                binding = {
                    "duty_id": duty_id, "jobs": str(fixture.jobs_path),
                    "attempt_id": "att-selected", "carrier": carrier,
                    "recipient_sid": second, "delivery_id": delivery_id,
                    "storage_recipient": fixture.PARENT,
                    "claim_owner": "retained-rewake:" + duty_id,
                }
                peer_steward.peer_message.bind_dispatch_notice(ref, binding)
                on_prepared(ref)
                sent.append((recipient, text, ref))
                return 3

            agents = {"result": {"agents": [{
                "agent": "claude", "pane_id": "w1:pThird",
                "agent_session": {"value": second},
            }]}}
            from dispatch_session_sweep import delivery_context
            with mock.patch.object(join, "materialize_after_terminal_close"), \
                 mock.patch.object(join, "expire_pending_for_dead_recipient",
                                   side_effect=AssertionError("live successor protects original"),
                                   create=True), \
                 mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                     stdout=json.dumps(agents), returncode=0)), \
                 mock.patch.object(peer_steward, "_resolve_target", return_value=("claude", second)), \
                 mock.patch.object(peer_steward, "_settle_claude_native_dispatch_receipt",
                                   return_value=False), \
                 mock.patch.object(supervision, "continue_for_parent", return_value=None), \
                 mock.patch("dispatch_session_sweep.delivery_context",
                            side_effect=lambda groups: "late completion for C\n" + delivery_context(groups)), \
                 mock.patch.object(peer_steward, "cmd_prompt", side_effect=courier):
                peer_steward._resume_registered_obligation(duty, store)
            self.assertEqual(len(sent), 1)
            recipient, text, ref = sent[0]
            self.assertEqual(recipient, {"harness": "claude", "session_id": second})
            self.assertIn("late completion for C", text)
            transfer = peer_steward.peer_message._read_pending(ref)
            self.assertEqual(transfer["to"], recipient)
            self.assertEqual(peer_steward.peer_message.receive_peer_message(
                transfer["text"], transfer["to"]), 0)
            self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "received")
            stored = pending.read(fixture.jobs_path.parent, fixture.PARENT, delivery_id)
            self.assertEqual(stored["state"], "acked")
            self.assertEqual(fixture.handover.effective_parent(fixture.meta, fixture.jobs_path), second)
        finally:
            fixture.doCleanups()

    def test_malformed_or_foreign_outcome_remains_pending(self):
        import dispatch_completion_join as join
        import artifact_lifecycle

        cases = (
            b"{",
            json.dumps({"route_id": "rt-foreign", "route_hash": "sha256:foreign",
                        "closed_at": "2026-10-23T00:00:00Z"}).encode(),
            json.dumps({"route_id": "rt-placeholder", "route_hash": "sha256:foreign",
                        "closed_at": "2026-10-23T00:00:00Z"}).encode(),
            json.dumps({"route_id": "rt-placeholder", "route_hash": "sha256:" + "a" * 64}).encode(),
        )
        for number, body in enumerate(cases):
            with self.subTest(number=number):
                attempt = f"att-d3-outcome-{number}"
                store, duty, row, record, root, route_id, route_hash = self._registered_duty(attempt)
                path = artifact_lifecycle.canonical_outcome_path(root, route_id)
                path.parent.mkdir(parents=True, exist_ok=True)
                if number in {2, 3}:
                    document = json.loads(body)
                    document["route_id"] = route_id
                    if number == 2:
                        document["route_hash"] = "sha256:" + "b" * 64
                    body = json.dumps(document).encode()
                path.write_bytes(body)
                self._resume_with_agents(duty, store)
                self.assertEqual(store.get(duty["id"])["state"], "pending")
                retained = join.pending_delivery.read(
                    self.jobs_path.parent, row.metadata["parent_sid"], record["delivery_id"])
                self.assertEqual(retained["state"], "pending")

    def test_closed_route_settlement_yields_to_adoption_at_effect_boundary(self):
        import artifact_lifecycle
        import dispatch_seat_handover as handover
        import session_tidy
        import contextlib
        import threading

        store, duty, row, record, root, route_id, route_hash = self._registered_duty(
            "att-d3-closed-race", worker_type="frame")
        outcome_path = artifact_lifecycle.canonical_outcome_path(root, route_id)
        outcome_path.parent.mkdir(parents=True, exist_ok=True)
        outcome_path.write_text(json.dumps({
            "schema_version": 4, "route_id": route_id, "route_hash": route_hash,
            "closed_at": "2026-10-23T00:00:00Z",
        }), encoding="utf-8")
        pane = "fixture:closed-route-race"
        env = {"XDG_STATE_HOME": str(self.tmp_root / "state"),
               "HARNESS_STATE_ROOT": str(self.tmp_root / "state" / "hearting"),
               "HERDR_PANE_ID": pane, "AGENT_DISPATCH_JOBS": str(self.jobs_path)}
        with mock.patch.dict(os.environ, env, clear=False):
            seat = session_tidy.resolve_seat("claude", str(self.tmp_root), os.environ)
            original_lock = session_tidy.seat_lock
            successor = "sess-closed-route-successor"
            adopted = threading.Event()
            writer_lock_attempt = threading.Event()
            order = []
            local = threading.local()
            effect_thread = threading.get_ident()

            def commit_adoption():
                with original_lock(seat.key):
                    now = session_tidy.now_epoch()
                    session_tidy.record_event(seat, "claude", row.metadata["parent_sid"], "start",
                                              cwd=str(self.tmp_root), now=now - 2)
                    self.assertEqual(handover.publish_snapshot_locked(
                        seat, "claude", row.metadata["parent_sid"],
                        [handover.binding_of(self.jobs_path, row.metadata)],
                        jobs=self.jobs_path, now=now - 1), 1)
                    session_tidy.record_event(seat, "claude", successor, "start",
                        source="clear", cwd=str(self.tmp_root), now=now)
                    self.assertIsNotNone(handover.record_locked(
                        seat, "claude", successor, "start", "clear", now))
                order.append("adopted")
                adopted.set()

            @contextlib.contextmanager
            def observed_lock(key, *, wait=None):
                if (key.startswith("lineage-binding-")
                        and threading.get_ident() != effect_thread):
                    writer_lock_attempt.set()
                with original_lock(key, wait=wait):
                    previous = getattr(local, "binding", False)
                    if key.startswith("lineage-binding-"):
                        local.binding = True
                    try:
                        yield
                    finally:
                        local.binding = previous

            original_close = peer_steward._closed_registered_route_outcome

            def after_real_close(*args, **kwargs):
                result = original_close(*args, **kwargs)
                if result is not None:
                    if getattr(local, "binding", False):
                        def writer():
                            commit_adoption()
                        threading.Thread(target=writer, daemon=True).start()
                        self.assertTrue(writer_lock_attempt.wait(2),
                                        "retire writer did not reach the real binding lock")
                    else:
                        commit_adoption()
                return result

            original_update = store.update

            def mark_settlement(duty_id, **changes):
                if changes.get("state") == "complete" and changes.get("delivery") == "not-required":
                    order.append("settled")
                return original_update(duty_id, **changes)

            with mock.patch.object(session_tidy, "seat_lock", side_effect=observed_lock), \
                 mock.patch.object(peer_steward, "_closed_registered_route_outcome",
                                   side_effect=after_real_close), \
                 mock.patch.object(store, "update", side_effect=mark_settlement):
                self._resume_with_agents(duty, store)
            self.assertTrue(adopted.wait(3), "first adoption did not finish after settlement")
            self.assertEqual(handover.effective_parent(row.metadata, self.jobs_path), successor)
            self.assertEqual(store.get(duty["id"])["state"], "complete")
            self.assertLess(order.index("settled"), order.index("adopted"), order)
            import dispatch_pending_delivery as pending
            self.assertEqual(pending.read(self.jobs_path.parent, row.metadata["parent_sid"],
                                          record["delivery_id"])["state"], "pending")


class RetireCandidatePromotionLockTest(_TmpRootMixin, unittest.TestCase):
    """A post-lock promotion must already belong to the retire transaction."""

    def test_all_candidates_effect_mutex_and_source_decision_serialize_real_retire(self):
        import dispatch_seat_handover as handover
        import session_tidy
        candidates = [{"dispatch_depth": "1", "worker_type": "owner", "attempt_id": f"att-{i}",
            "parent_sid": "registered", "parent_harness": "claude", "route_id": f"rt-{i}",
            "route_hash": f"sha256:{i}", "route_node": "owner"} for i in range(13)]
        self.jobs_path.write_text("".join("now\trunning\t0\tjob\tlog\t" +
            ",".join(f"{k}={v}" for k,v in meta.items()) + "\n" for meta in candidates))
        retained = self.tmp_root / "retained-job-data.json"
        retained.write_text('{"state":"retained"}')
        seed = session_tidy._pane_seat_of("w1:pSeed")
        handover.record_retire_handover("registered", "claude", "predecessor", "codex",
            successor_seat=seed, jobs=self.jobs_path)
        possible = handover._handover_candidates("codex", "predecessor", self.jobs_path)
        self.assertEqual(len(possible), 13)
        last = possible[-1][0]
        acquiring = threading.Event()
        c_done, d_done = threading.Event(), threading.Event()
        results = {}
        mutexes = handover.binding_mutexes
        @contextlib.contextmanager
        def observe(bindings, **kwargs):
            bindings = list(bindings)
            if any(handover._binding_identity(b) == handover._binding_identity(last) for b in bindings):
                acquiring.set()
            with mutexes(bindings, **kwargs):
                yield
        def transfer(name, done):
            try:
                results[name] = handover.record_retire_handover("predecessor", "codex", name, "claude",
                    successor_seat=session_tidy._pane_seat_of("w1:p" + name), jobs=self.jobs_path)
            except BaseException as exc:
                results[name] = exc
            finally:
                done.set()
        with mock.patch.object(handover, "binding_mutexes", side_effect=observe):
            with mutexes([last]):
                c = threading.Thread(target=transfer, args=("C", c_done))
                c.start()
                self.assertTrue(acquiring.wait(3))
                self.assertFalse(c_done.wait(0.1))
                d = threading.Thread(target=transfer, args=("D", d_done))
                d.start()
                self.assertFalse(d_done.wait(0.1))
            c.join(4); d.join(4)
        self.assertFalse(c.is_alive() or d.is_alive())
        self.assertIsNone(results["D"])
        self.assertIsInstance(results["C"], dict)
        self.assertEqual(len(results["C"]["bindings"]), 13)
        for meta in candidates:
            self.assertEqual(handover.effective_parent(meta, self.jobs_path), "C")
        self.assertEqual(retained.read_text(), '{"state":"retained"}')


class SessionNoticePriorityTest(_TmpRootMixin, unittest.TestCase):
    """Accepted delivery work must reach its consumer before unrelated recovery."""
    notice = SessionNoticeTerminalIdentityTest.notice
    terminal = SessionNoticeTerminalIdentityTest.terminal
    transport = SessionNoticeIntegrationCorrectionTest.transport

    def setUp(self):
        import dispatch_contract
        for module, name, value in (
                (peer_steward.peer_obligations, "_proc_start_for_store", "100001"),
                (peer_steward.peer_obligations, "request_dispatch_reconciliation", False),
                (dispatch_contract, "process_start_ticks", "100001"),
                (dispatch_contract, "process_identity_disposition", "live")):
            patch = patch_split(module, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        super().setUp()
        self.store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)

    class PausePass(BaseException):
        pass

    def notices(self, *, acknowledged=False):
        duties = []
        for subject in ("steward-check", "compute-exit", "experiment-checkpoint"):
            record, duty = self.notice("run-notice", f"{subject}:{acknowledged}")
            if acknowledged:
                self.terminal(record, "acked")
            duties.append(duty)
        return duties

    def progress(self, duties, native):
        return (native.call_count,
                [self.store.get(duty["id"])["state"] for duty in duties])

    def test_delivery_and_ack_settlement_precede_unrelated_duty(self):
        for acknowledged in (False, True):
            with self.subTest(acknowledged=acknowledged):
                duties = self.notices(acknowledged=acknowledged)
                unrelated = self.store.create("message-unrelated", "message",
                                              {"session_id": "other"}, {})
                reached = []
                def pause(*_args, **_kwargs):
                    reached.append(self.progress(duties, native))
                    raise self.PausePass()
                with self.transport("terminal-A") as native, \
                     mock.patch.object(peer_steward, "_resume_message_obligation", side_effect=pause):
                    with self.assertRaises(self.PausePass):
                        peer_steward._reconcile_obligations(self.store, [unrelated, *duties])
                self.assertEqual(reached, [(0, ["complete"] * 3)] if acknowledged
                                 else [(1, ["pending"] * 3)])

    def check_full_pass(self, recover_legacy):
        created = [] if recover_legacy else self.notices()
        def discover(*_args, **_kwargs):
            if recover_legacy:
                _record, duty = self.notice("session-notice", "discovered-after-fast-pass")
                created.append(duty)
            return True
        def pause(*_args, **_kwargs):
            raise self.PausePass()
        lock_path = self.store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME
        resume = peer_steward._resume_session_notice_obligations
        with lock_path.open("w") as lock, self.transport("terminal-A") as native, \
             mock.patch.object(peer_steward, "_runner_readiness_loop"), \
             mock.patch.object(peer_steward, "_discover_session_notice_duties", side_effect=discover), \
             mock.patch.object(peer_steward, "_resume_session_notice_obligations", wraps=resume) as progress, \
             mock.patch.object(peer_steward, "_reconcile_external_obligations", side_effect=pause):
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaises(self.PausePass):
                peer_steward.cmd_obligation_runner(SimpleNamespace(
                    state_root=self.tmp_root, lock_fd=lock.fileno()))
        self.assertTrue(created)
        self.assertEqual(progress.call_count, 1)
        self.assertEqual(native.call_count, 1)

    def test_fast_pass_preserves_later_legacy_recovery(self):
        self.check_full_pass(True)

    def test_an_accepted_notice_advances_only_once_per_pass(self):
        self.check_full_pass(False)

    def test_accepted_delivery_and_ack_settlement_precede_legacy_discovery(self):
        for acknowledged in (False, True):
            with self.subTest(acknowledged=acknowledged):
                duties = self.notices(acknowledged=acknowledged)
                lock_path = self.store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME
                reached = []
                def pause(*_args, **_kwargs):
                    reached.append(self.progress(duties, native))
                    raise self.PausePass()
                with lock_path.open("w") as lock, self.transport("terminal-A") as native, \
                     mock.patch.object(peer_steward, "_runner_readiness_loop"), \
                     mock.patch.object(peer_steward, "_discover_session_notice_duties", side_effect=pause):
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    with self.assertRaises(self.PausePass):
                        peer_steward.cmd_obligation_runner(SimpleNamespace(
                            state_root=self.tmp_root, lock_fd=lock.fileno()))
                self.assertEqual(reached, [(0, ["complete"] * 3)] if acknowledged
                                 else [(1, ["pending"] * 3)])


class DelayNoticeSettlementTest(_TmpRootMixin, unittest.TestCase):
    """U9/C1: a delay notice follows its original duty; addressless senders get none."""

    RUNTIME = {"harness": "runtime", "session_id": None, "name": "Hearting runtime"}

    def _no_send(self):
        pm = peer_steward.peer_message
        stack = contextlib.ExitStack()
        self.sends = [
            stack.enter_context(mock.patch.object(pm, "prepare_peer_message")),
            stack.enter_context(mock.patch.object(pm, "deliver_pending_codex")),
            stack.enter_context(mock.patch.object(peer_steward, "_herdr_prompt")),
            stack.enter_context(mock.patch.object(peer_steward, "cmd_prompt")),
        ]
        self.addCleanup(stack.close)

    def _assert_no_send(self):
        for fake in self.sends:
            fake.assert_not_called()

    def _recorded_delay(self, orig, *, original_state="complete", recipient=None,
                        delay_state="unknown"):
        """Fixed data shaped like delay-0bb79c5f… / message-0bb79c5f… (rows already gone)."""
        store = peer_steward.peer_obligations.ObligationStore()
        sender = {"harness": "claude", "name": "w4:p1W", "pane_id": "w4:p1W",
                  "session_id": "sid-delay-sender"}
        recipient = recipient or self.RUNTIME
        body = ("[notice] Your peer message ref %s is still pending delivery; "
                "it was sent at 2026-10-28 10:13 UTC." % orig)
        notice_ref = "n" * 31 + orig[-1]
        identity = {"server": "default", "recipient_harness": recipient["harness"],
                    "recipient_session_id": recipient.get("session_id")}
        if original_state is not None:
            store.create("message-" + orig, "message",
                         {"server": "default", "pane": "", "harness": "claude",
                          "session_id": "sid-delay-sender"},
                         {"ref": orig, "body_digest": "0" * 64, "from": recipient,
                          "to": sender})
            if original_state != "pending":
                store.update("message-" + orig, state=original_state, result="received",
                             delivery="acknowledged", cleanup="complete",
                             observation={"ref": orig, "reason": "exact-peer-ref",
                                          "receiver_receipt": {"ref": orig}})
        store.create("delay-" + orig, "message", identity,
                     {"delay_notice_for": orig, "target": "Hearting runtime", "body": body,
                      "body_digest": hashlib.sha256(body.encode()).hexdigest(),
                      "from": sender, "to": recipient})
        store.update("delay-" + orig, state=delay_state,
                     observation={"reason": "pending-row-missing", "ref": notice_ref})
        return store, notice_ref

    def _reconcile(self, store):
        peer_steward._reconcile_obligations(store, store.list(), retry_state={})

    def test_delay_notice_settles_from_completed_original(self):
        self._no_send()
        for orig, original_state, recipient in (
                ("a" * 32, "complete", self.RUNTIME),
                ("b" * 32, "cancelled",
                 {"harness": "unknown", "session_id": None, "name": "unknown"})):
            with self.subTest(original=original_state):
                store, _notice = self._recorded_delay(
                    orig, original_state=original_state, recipient=recipient)
                self._reconcile(store)
                delay = store.get("delay-" + orig)
                self.assertEqual((delay["state"], delay["delivery"], delay["cleanup"]),
                                 ("complete", "not-required", "complete"))
                self.assertEqual(delay["observation"]["reason"], "delay-original-settled")
                self.assertEqual(delay["observation"]["original_state"], original_state)
        self._assert_no_send()

    def test_scheduled_notice_message_duty_settles_too(self):
        self._no_send()
        orig = "c" * 32
        store, notice_ref = self._recorded_delay(orig, delay_state="pending")
        sender = {"harness": "claude", "name": "w4:p1W", "pane_id": "w4:p1W",
                  "session_id": "sid-delay-sender"}
        scheduled = store.create(
            "message-" + notice_ref, "message",
            {"server": "default", "pane": "w4:p1W", "harness": "claude",
             "session_id": "sid-delay-sender"},
            {"ref": notice_ref, "body_digest": "1" * 64, "target": "w4:p1W",
             "from": self.RUNTIME, "to": sender, "refs": [], "delay_notice_for": orig})
        self.assertEqual(scheduled["state"], "pending")
        self._reconcile(store)
        settled = store.get("message-" + notice_ref)
        self.assertEqual((settled["state"], settled["delivery"], settled["cleanup"]),
                         ("complete", "not-required", "complete"))
        self.assertEqual(settled["observation"]["reason"], "delay-original-settled")
        self._assert_no_send()

    def test_no_delay_notice_for_addressless_sender(self):
        old = time.time() - 7200
        for sender in (self.RUNTIME,
                       {"harness": "unknown", "session_id": None, "name": "unknown"}):
            with self.subTest(harness=sender["harness"]):
                row = {"ref": "d" * 32, "created": old, "from": sender,
                       "to": {"harness": "codex", "session_id": "sid-r", "name": None},
                       "refs": []}
                self.assertIsNone(peer_steward._ensure_sender_delay_notice_if_due(row))
                self.assertIsNone(peer_steward._ensure_sender_delay_notice(row))
                store = peer_steward.peer_obligations.ObligationStore()
                self.assertEqual([d["id"] for d in store.list()
                                  if d["id"].startswith("delay-")], [])

    def test_valid_sender_still_gets_a_delay_notice(self):
        pm = peer_steward.peer_message
        sender = {"harness": "claude", "session_id": "delay-sender", "name": None}
        recipient = {"harness": "codex", "session_id": "delay-recipient", "name": None}
        _text, ref = pm.prepare_peer_message("original", sender, recipient, defer=True)
        with mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner"):
            duty = peer_steward._ensure_sender_delay_notice(pm._read_pending(ref))
        self.assertEqual(duty["id"], "delay-" + ref)
        stored = peer_steward.peer_obligations.ObligationStore().get("delay-" + ref)
        self.assertTrue(stored["observation"]["transfer_ref"])

    def test_open_original_keeps_delay_notice_open(self):
        for state in ("pending", "unknown"):
            with self.subTest(original=state):
                orig = ("e" if state == "pending" else "f") * 32
                store, _n = self._recorded_delay(orig, original_state=state)
                if state == "unknown":
                    store.update("message-" + orig, state="unknown",
                                 observation={"reason": "pending-row-missing"})
                before = store.get("delay-" + orig)
                self._reconcile(store)
                after = store.get("delay-" + orig)
                self.assertNotEqual(after["observation"].get("reason"), "delay-original-settled")
                self.assertNotEqual(after["state"], "complete")
                self.assertEqual(after["state"], before["state"])

    def test_missing_original_is_not_treated_as_settled(self):
        orig = "9" * 32
        store, _n = self._recorded_delay(orig, original_state=None)
        self.assertIsNone(store.get("message-" + orig))
        self._reconcile(store)
        after = store.get("delay-" + orig)
        self.assertNotEqual(after["state"], "complete")
        self.assertNotEqual((after["observation"] or {}).get("reason"), "delay-original-settled")

    def test_settled_original_leaves_existing_notice_row_untouched(self):
        self._no_send()
        pm = peer_steward.peer_message
        sender = {"harness": "claude", "session_id": "delay-sender", "name": None}
        recipient = {"harness": "codex", "session_id": "delay-recipient", "name": None}
        for state, receipt in (("pending", "sender-delay-notice"),
                               ("queued", "native-prompt-submitted"),
                               ("unverified", "native-prompt-unverified")):
            with self.subTest(row=state), mock.patch.object(
                    peer_steward.peer_obligations, "ensure_normal_runner"):
                # prepare_peer_message is stubbed by _no_send; use the real one.
                orig = {"pending": "1", "queued": "2", "unverified": "3"}[state] * 32
                store = peer_steward.peer_obligations.ObligationStore()
                text, notice_ref = _REAL_PREPARE(
                    "[notice] Your peer message ref %s is still pending delivery." % orig,
                    recipient, sender, defer=True, refs=["delay-notice:" + orig, orig],
                    receipt="sender-delay-notice")
                pm._save_pending(dict(pm._read_pending(notice_ref), state=state,
                                      receipt=receipt))
                store.create("message-" + orig, "message",
                             {"server": "default", "pane": "", "harness": "codex",
                              "session_id": "delay-recipient"},
                             {"ref": orig, "body_digest": "0" * 64, "from": sender,
                              "to": recipient})
                store.update("message-" + orig, state="complete", result="received",
                             delivery="acknowledged", cleanup="complete",
                             observation={"ref": orig, "reason": "exact-peer-ref"})
                store.create("delay-" + orig, "message",
                             {"server": "default", "recipient_harness": "claude",
                              "recipient_session_id": "delay-sender"},
                             {"delay_notice_for": orig, "target": "x", "body": "b",
                              "body_digest": hashlib.sha256(b"b").hexdigest(),
                              "from": recipient, "to": sender})
                store.update("delay-" + orig, state="pending",
                             observation={"ref": notice_ref, "transfer_ref": notice_ref})
                path = pm._pending_path(notice_ref)
                before = Path(path).read_bytes()
                self._reconcile(store)
                self.assertEqual(Path(path).read_bytes(), before)
                self.assertEqual(store.get("delay-" + orig)["observation"]["reason"],
                                 "delay-original-settled")
        self._assert_no_send()


class RunnerObserverErrorCauseTest(_TmpRootMixin, unittest.TestCase):
    """U9/C2 (N29): runner observer failures keep the exception class, stably."""

    def _message_duty(self, ref="a" * 32):
        store = peer_steward.peer_obligations.ObligationStore()
        sender = {"harness": "claude", "session_id": "sid-s", "name": None}
        recipient = {"harness": "codex", "session_id": "sid-r", "name": None}
        store.create("message-" + ref, "message",
                     {"server": "default", "pane": "", "harness": "codex",
                      "session_id": "sid-r"},
                     {"ref": ref, "body_digest": "0" * 64, "from": sender, "to": recipient})
        return store

    def test_message_duty_exception_records_cause_stably(self):
        store = self._message_duty()
        seen = []
        for _ in range(2):
            with mock.patch.object(peer_steward, "_resume_message_obligation",
                                   side_effect=KeyError("x")):
                peer_steward._reconcile_obligations(store, store.list(), retry_state={})
            duty = store.list()[0]
            self.assertEqual(duty["observation"]["reason"], "observer-unavailable:KeyError")
            seen.append((peer_steward._obligation_retry_fingerprint(duty),
                         peer_steward._runner_duty_signature([duty])))
        self.assertEqual(seen[0], seen[1])

    def test_notice_batch_exception_records_cause_after_run_states(self):
        store = peer_steward.peer_obligations.ObligationStore()
        for n, kind in enumerate(("run-notice", "session-notice")):
            store.create("notice-%d" % n, kind,
                         {"server": "default", "pane": "", "harness": "claude",
                          "session_id": "sid-r"}, {"ref": "r%d" % n})
        order = []
        with mock.patch.object(peer_steward, "_publish_run_states",
                               side_effect=lambda *a, **k: order.append("run-states")), \
             mock.patch.object(peer_steward, "_resume_session_notice_obligations",
                               side_effect=lambda *a, **k: (order.append("notices"),
                                                           (_ for _ in ()).throw(RuntimeError("m")))):
            peer_steward._reconcile_obligations(store, store.list(), retry_state={})
        self.assertEqual(order, ["run-states", "notices"])
        for duty in store.list():
            self.assertEqual(duty["observation"]["reason"], "observer-unavailable:RuntimeError")

    def test_exception_does_not_reopen_a_settled_duty(self):
        store = self._message_duty("b" * 32)
        store.update("message-" + "b" * 32, state="complete", result="received",
                     delivery="acknowledged", cleanup="complete",
                     observation={"ref": "b" * 32, "reason": "exact-peer-ref"})
        with mock.patch.object(peer_steward, "_resume_message_obligation",
                               side_effect=KeyError("x")):
            peer_steward._reconcile_obligations(store, store.list(), retry_state={})
        settled = store.get("message-" + "b" * 32)
        self.assertEqual(settled["state"], "complete")
        self.assertEqual(settled["observation"]["reason"], "exact-peer-ref")


class OpenCodeSidRunnerInputTest(_TmpRootMixin, unittest.TestCase):
    """U9/C3 (relay 7): a still-pending OpenCode SID row keeps the N33/N04/A47b
    protections; the runner no longer types it into an idle window."""

    SID, PANE = "sid-opencode", "w1:pX"

    def _pending(self):
        pm = peer_steward.peer_message
        sender = {"harness": "claude", "session_id": "sid-sender", "name": None}
        recipient = {"harness": "opencode", "session_id": self.SID, "name": "oc",
                     "pane_id": self.PANE}
        _text, ref = pm.prepare_peer_message("[steer] relay seven", sender, recipient, defer=True)
        row = pm._read_pending(ref)
        self.assertEqual(row["state"], "pending")
        with mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner"):
            duty = peer_steward._schedule_message_obligation(row, "oc", self.PANE)
        self.assertEqual(duty["id"], "message-" + ref)
        return ref, row

    @contextlib.contextmanager
    def _world(self, *, state="idle", screen=None, foreground=True, agent_states=None,
               exact=True):
        ident = {"harness": "opencode", "session_id": self.SID, "name": "oc", "pane": self.PANE}
        candidate = {"_identity": ident, "state": state, "session_id": self.SID,
                     "_native_session_id": self.SID}
        proof = {"pid": os.getpid(), "start": "fixture-start",
                 "pid_namespace": os.readlink("/proc/self/ns/pid"), "session_id": self.SID}
        screens = peer_steward._screen_lines(OPENCODE_EMPTY if screen is None else screen)
        states = iter(agent_states) if agent_states else None
        agent = ((lambda *_a, **_k: (next(states), self.PANE)) if states
                 else (lambda *_a, **_k: (state, self.PANE)))
        self.sent = []
        def deliver(target, text, *, wait, timeout_ms, start_aware=False):
            self.sent.append((target, text))
            return 0, {}
        with mock.patch.object(peer_steward, "_retire_target",
                               return_value=(state, ident, 0, None)), \
             mock.patch.object(peer_steward, "_prompt_current_candidate",
                               return_value=((candidate, None) if exact
                                             else (None, "target-identity-unverified"))), \
             mock.patch.object(peer_steward, "_prompt_exact_candidate",
                               return_value=(candidate, None)), \
             mock.patch.object(peer_steward, "_agent_state", side_effect=agent), \
             mock.patch.object(peer_steward, "_pane_readiness",
                               return_value=SimpleNamespace(state="ready", reason="")), \
             mock.patch.object(peer_steward, "_prompt_foreground_proof",
                               return_value=proof if foreground else None), \
             mock.patch.object(peer_steward, "_read_screen", return_value=screens), \
             mock.patch.object(peer_steward, "_herdr_prompt", side_effect=deliver), \
             mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner"):
            yield

    def _pass(self):
        store = peer_steward.peer_obligations.ObligationStore()
        peer_steward._reconcile_obligations(store, store.list(), retry_state={})
        return store

    def test_submitted_or_claimed_rows_are_never_retyped(self):
        pm = peer_steward.peer_message
        for state, receipt in (("unverified", "context-api-inflight"),
                               ("queued", "opencode-prompt-inflight")):
            with self.subTest(row=state):
                ref, _row = self._pending()
                pm._save_pending(dict(pm._read_pending(ref), state=state, receipt=receipt))
                before = pm._pending_path(ref).read_bytes()
                with self._world():
                    self._pass()
                self.assertEqual(self.sent, [])
                self.assertEqual(pm._pending_path(ref).read_bytes(), before)
                pm._pending_path(ref).unlink()
                store = peer_steward.peer_obligations.ObligationStore()
                store.update("message-" + ref, state="complete", result="received",
                             delivery="acknowledged", cleanup="complete",
                             observation={"ref": ref, "reason": "exact-peer-ref"})
        with self.subTest(row="pending-with-live-claim"):
            ref, _row = self._pending()
            from dispatch_contract import process_start_ticks
            claim = {"token": "plugin-claim", "pid": os.getpid(),
                     "start": process_start_ticks(os.getpid())}
            pm._save_pending(dict(pm._read_pending(ref), rpc_claim=claim))
            with self._world():
                self._pass()
            self.assertEqual(self.sent, [])
            self.assertEqual(pm._read_pending(ref)["rpc_claim"], claim)

    def test_other_session_process_gets_no_input(self):
        ref, _row = self._pending()
        with self._world(exact=False):
            reason, proof = peer_steward._native_idle_prompt_guard(
                self.PANE, "opencode", self.SID, self.PANE)
        self.assertEqual((reason, proof), ("target-identity-unverified", None))
        with self._world(foreground=False):
            self._pass()
        self.assertEqual(self.sent, [])
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "pending")

class ReceiverStateRetentionCycleTest(_TmpRootMixin, unittest.TestCase):
    """Backlog bundle B (d1): a message stopped by the receiver's own state keeps one
    carrier (`pending` + `message-<ref>`) and is delivered exactly once after the
    block clears; an external write that provably never started returns to pending,
    while an ambiguous write is never retyped. The receiver is a shell-attached
    session; its host's idle/empty input observation is the receiver state."""

    def setUp(self):
        super().setUp()
        for key in ("AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS",
                    "CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID"):
            os.environ.pop(key, None)

    # --- fixture world -----------------------------------------------------------
    @contextlib.contextmanager
    def _world(self, harness, sid, *, empty=True):
        """One shell-attached recipient; ``self.host.status`` is its input state."""
        self.host = _ShellHost([(harness, sid)])
        self.host.status["empty"] = empty
        self.sent = []
        self.host.native.side_effect = lambda h, s, text: self.sent.append((h + ":" + s, text))
        stack = contextlib.ExitStack()
        with stack:
            stack.enter_context(self.host.attached())
            stack.enter_context(mock.patch.object(
                peer_steward.subprocess, "run", side_effect=OSError("no terminal multiplexer")))
            stack.enter_context(mock.patch.object(
                peer_steward, "_current_session_identity",
                return_value=("sid-steward", "claude")))
            stack.enter_context(mock.patch.object(peer_steward, "_from_name", return_value="steward"))
            stack.enter_context(mock.patch.object(peer_steward, "_project_of", return_value="/fixture"))
            stack.enter_context(mock.patch.object(
                peer_steward.peer_obligations, "ensure_normal_runner", return_value=False))
            yield

    def _prompt(self, harness, sid, *, text="[steer] keep this message"):
        args = peer_steward.build_parser().parse_args(["prompt", harness + ":" + sid, text])
        with mock.patch("builtins.print") as printed:
            rc = peer_steward.cmd_prompt(args)
        ref = self._all_records()[-1]["transfer_ref"]
        return rc, printed.call_args[0][0], ref

    def _pass(self):
        store = peer_steward.peer_obligations.ObligationStore()
        peer_steward._reconcile_obligations(store, store.list(), retry_state={})
        return store

    def _duty(self, ref):
        store = peer_steward.peer_obligations.ObligationStore()
        return store.get("message-" + ref)

    def _receive(self, ref, harness, sid):
        pm = peer_steward.peer_message
        row = pm._read_pending(ref)
        rc = pm.receive_peer_message(row["text"], {"harness": harness, "session_id": sid})
        self.assertEqual(rc, 0)

    # --- 5.1/5.2 busy input cycle (form, draft) -----------------------------------
    def _held_input_cycle(self, harness, sid):
        pm = peer_steward.peer_message
        with self._world(harness, sid, empty=False):
            rc, line, ref = self._prompt(harness, sid)
            self.assertEqual(rc, 0, line)
            self.assertIn("reason=not-idle-empty", line)
            self.assertEqual(self.sent, [])
            self.assertEqual(pm._read_pending(ref)["state"], "pending")
            duty = self._duty(ref)
            self.assertIsNotNone(duty, "a verified endpoint keeps exactly one carrier")
            self.assertEqual(duty["state"], "pending")
            store = self._pass()              # input still not empty
            self.assertEqual(self.sent, [])
            self.assertEqual(store.get("message-" + ref)["state"], "pending")
            self.assertEqual(store.get("message-" + ref)["observation"]["reason"], "not-idle-empty")
            self.host.status["empty"] = True
            store = self._pass()              # input cleared
            self.assertEqual(len(self.sent), 1)
            self.assertEqual(self.sent[0], (harness + ":" + sid, pm._read_pending(ref)["text"]))
            after = pm._read_pending(ref)
            self.assertEqual((after["state"], after.get("rpc_claim")), ("queued", None))
            self._receive(ref, harness, sid)
            store = self._pass()
            self.assertEqual(store.get("message-" + ref)["state"], "complete")
            self._pass()
            self._pass()
            self.assertEqual(len(self.sent), 1, "a received row is never retyped")

    def test_claude_form_open_holds_one_carrier_then_delivers_once(self):
        self._held_input_cycle("claude", "sid-child")

    def test_opencode_form_open_holds_one_carrier_then_delivers_once(self):
        self._held_input_cycle("opencode", "sid-opencode")

    # --- 5.3 Codex interrupted conversation + busy input -------------------------
    class _AppServer:
        """Method-keyed fake Codex app-server; records every RPC."""

        def __init__(self, *, turn_status="interrupted", queue=(), turn_id="turn-live"):
            self.turn_status, self.turn_id = turn_status, turn_id
            self.queue = [dict(item) for item in queue]
            self.calls, self.errors = [], {}

        def methods(self):
            return [method for method, _params in self.calls]

        def params(self, method):
            return [params for name, params in self.calls if name == method]

        def __call__(self, _path, method, params, **_kw):
            self.calls.append((method, dict(params)))
            error = self.errors.get(method)
            if error is not None:
                raise error
            if method == "thread/queue/list":
                return {"data": [dict(item) for item in self.queue]}
            if method == "thread/turns/list":
                if self.turn_status is None:
                    return {"data": []}
                return {"data": [{"id": self.turn_id, "status": self.turn_status, "items": []}]}
            if method == "thread/queue/add":
                item = {"id": "queued-%d" % (len(self.queue) + 1), "input": params["input"],
                        "clientUserMessageId": params["clientUserMessageId"]}
                self.queue.append(item)
                return {"queuedSubmission": {"id": item["id"]}}
            if method == "thread/queue/delete":
                self.queue = [i for i in self.queue if i["id"] != params["queuedSubmissionId"]]
                return {"deleted": True}
            if method == "thread/queue/start":
                return {"turn": {"id": "turn-started"}}
            if method == "turn/steer":
                return {"turnId": params["expectedTurnId"]}
            raise AssertionError("unexpected rpc " + method)

    @contextlib.contextmanager
    def _codex_world(self, harness, sid, *, empty=True):
        import codex_queue_delivery

        def observe(*_args, **_kwargs):
            return codex_queue_delivery._PeerObservation("not_observed")

        with self._world(harness, sid, empty=empty), \
             mock.patch("codex_queue_dispatch.resolve_queue_socket",
                        return_value="fixture-socket"), \
             mock.patch.object(codex_queue_delivery, "_rpc", self.server), \
             mock.patch.object(codex_queue_delivery, "_observe_peer_message", side_effect=observe):
            yield

    def test_codex_interrupted_form_holds_then_runner_starts_once(self):
        self.server = self._AppServer(turn_status="interrupted")
        with self._codex_world("codex", "codex-thread", empty=False):
            rc, line, ref = self._prompt("codex", "codex-thread")
            self.assertEqual(rc, 0, line)
            self.assertIn("reason=native-queue-held-interrupted:", line)
            self.assertEqual(self.server.params("thread/queue/start"), [])
            self.assertEqual(len(self.server.params("thread/queue/add")), 1)
            store = self._pass()              # the shell input is still not empty
            self.assertEqual(self.server.params("thread/queue/start"), [])
            self.assertEqual(len(self.server.params("thread/queue/add")), 1)
            self.assertEqual(store.get("message-" + ref)["state"], "pending")
            self.host.status["empty"] = True
            store = self._pass()              # idle, empty, unfocused shell input
            self.assertEqual(len(self.server.params("thread/queue/start")), 1)
            self.assertEqual(len(self.server.params("thread/queue/add")), 1)
            self.assertEqual(peer_steward.peer_message._read_pending(ref)["receipt"],
                             "native-queue-started")
            self._pass()
            self._pass()
            self.assertEqual(len(self.server.params("thread/queue/start")), 1)
            self.assertEqual(len(self.server.params("thread/queue/add")), 1)
        self.assertEqual(self.sent, [], "Codex never receives shell input")

    # --- 5.4 carrier rule ---------------------------------------------------------
    def test_verified_endpoint_holds_get_exactly_one_carrier(self):
        for harness, sid in (("claude", "sid-child"), ("opencode", "sid-opencode")):
            with self.subTest(harness=harness):
                with self._world(harness, sid, empty=False):
                    _rc, _line, ref = self._prompt(
                        harness, sid, text="[steer] carrier " + harness)
                    store = peer_steward.peer_obligations.ObligationStore()
                    duties = [d for d in store.list() if (d.get("intent") or {}).get("ref") == ref]
                    self.assertEqual(len(duties), 1)
                    self.assertEqual(duties[0]["id"], "message-" + ref)
                    self.assertEqual(duties[0]["state"], "pending")

    def test_a_held_message_says_the_runtime_delivers_it_and_nothing_is_resent(self):
        """The prompt line stays as it was; one stderr line says the carrier owns the rest."""
        with self._world("claude", "sid-child", empty=False):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc, line, ref = self._prompt("claude", "sid-child")
            self.assertEqual(rc, 0, line)
            self.assertTrue(line.startswith("prompted=queued "), line)
            self.assertIsNotNone(self._duty(ref))
            self.assertIn(f"next: nothing to resend - the runtime delivers ref={ref} to claude:sid-child",
                          err.getvalue())

    # --- 5.5 provably unstarted write --------------------------------------------
    def test_unstarted_popen_returns_the_row_to_pending_and_retries_once(self):
        pm = peer_steward.peer_message
        with self._world("claude", "sid-child"):
            self.host.before_input = lambda _binding: {
                "ok": False, "reason": "control-unavailable", "not_started": True}
            rc, line, ref = self._prompt("claude", "sid-child")
            self.assertEqual(rc, 0, line)
            # The printed line names the held outcome, not a submission that
            # never happened: queued + the start-failure reason, row pending.
            self.assertIn("prompted=queued", line)
            self.assertIn("reason=control-unavailable", line)
            self.assertEqual(self.sent, [])
            row = pm._read_pending(ref)
            self.assertEqual((row["state"], row.get("rpc_claim")), ("pending", None))
            self.assertIsNotNone(self._duty(ref))
            self.host.before_input = None
            store = self._pass()
            self.assertEqual(len(self.sent), 1)
            self.assertEqual(pm._read_pending(ref)["state"], "queued")
            self._receive(ref, "claude", "sid-child")
            store = self._pass()
            self.assertEqual(store.get("message-" + ref)["state"], "complete")

    def test_ambiguous_or_failed_write_is_never_retyped(self):
        pm = peer_steward.peer_message
        cases = (
            ("reply-lost", {"ok": False, "reason": "control-unavailable", "not_started": False}),
            ("nonzero", {"ok": False, "reason": "inject-failed"}),
        )
        for name, result in cases:
            with self.subTest(case=name):
                with self._world("claude", "sid-child"):
                    self.host.result = result
                    _rc, _line, ref = self._prompt(
                        "claude", "sid-child", text="[steer] ambiguous " + name)
                    self.assertEqual(len(self.sent), 1)
                    self.assertEqual(pm._read_pending(ref)["state"], "unverified")
                    before = pm._pending_path(ref).read_bytes()
                    self.host.result = {"ok": True}
                    store = self._pass()
                    self._pass()
                    self.assertEqual(len(self.sent), 1)
                    self.assertEqual(pm._pending_path(ref).read_bytes(), before)
                    self.assertNotEqual(store.get("message-" + ref)["state"], "complete")
                    pm._pending_path(ref).unlink()
                    store = peer_steward.peer_obligations.ObligationStore()
                    store.update("message-" + ref, state="complete", result="received",
                                 delivery="acknowledged", cleanup="complete",
                                 observation={"ref": ref, "reason": "exact-peer-ref"})

if __name__ == "__main__":
    unittest.main()
