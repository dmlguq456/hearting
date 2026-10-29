#!/usr/bin/env python3
import sys
import io
import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from argparse import Namespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_history as viewer


class HistoryTests(unittest.TestCase):
    def test_confirmed_predecessor_beats_chronological_neighbor(self):
        rows = [
            {"harness": "claude", "sid": "new", "relation": "confirmed",
             "predecessor": {"harness": "claude", "sid": "old"}},
            {"harness": "claude", "sid": "old", "relation": "confirmed",
             "predecessor": {"harness": "claude", "sid": "older"}},
            {"harness": "claude", "sid": "older", "relation": "unknown"},
            {"harness": "claude", "sid": "neighbor", "relation": "chronological"},
            {"harness": "claude", "sid": "unknown", "relation": "unknown"},
        ]
        entries, reason = viewer._entries_for_generation(rows, 1)
        self.assertFalse(reason)
        self.assertEqual([row["sid"] for row in entries], ["old", "older", "neighbor", "unknown"])
        self.assertEqual([row["_label"] for row in entries], [
            "확정된 이전 대화", "확정된 이전 대화", "관계 미확인", "시간순 이웃 · 인계 미확인"])

    def test_local_fallback_uses_raw_lineage_and_defaults_to_confirmed_predecessor(self):
        rows = [
            {"harness": "claude", "sid": "new", "relation": "confirmed",
             "predecessor": {"harness": "claude", "sid": "old"}},
            {"harness": "claude", "sid": "old", "relation": "unknown"},
        ]
        seat = viewer.session_tidy.Seat("pane", "key", "pane", "claude")
        with patch.object(viewer, "_identity", return_value=(seat, "", "")), \
             patch.object(viewer.session_tidy, "seat_lineage", return_value=rows) as lineage, \
             patch.object(viewer, "_render", side_effect=lambda row: (row["sid"], None)), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(viewer.main(["--stdout"]), 0)
        lineage.assert_called_once_with(seat)
        self.assertEqual(output.getvalue().strip(), "old")

    def test_generation_walks_confirmed_then_lineage_order_and_labels(self):
        rows = [
            {"harness": "claude", "sid": "new", "relation": "confirmed",
             "predecessor": {"harness": "claude", "sid": "old"}},
            {"harness": "claude", "sid": "old", "relation": "confirmed",
             "predecessor": {"harness": "claude", "sid": "older-confirmed"}},
            {"harness": "claude", "sid": "older-confirmed", "relation": "unknown"},
            {"harness": "claude", "sid": "unknown", "relation": "unknown"},
        ]
        expected = [
            ("old", "확정된 이전 대화"),
            ("older-confirmed", "확정된 이전 대화"),
            ("unknown", "관계 미확인"),
        ]
        for generation, (sid, label) in enumerate(expected, 1):
            entries, reason = viewer._entries_for_generation(rows, generation)
            self.assertFalse(reason)
            self.assertEqual((entries[generation - 1]["sid"], entries[generation - 1]["_label"]),
                             (sid, label))
        self.assertIn("범위 밖", viewer._entries_for_generation(rows, len(expected) + 1)[1])

    def test_chronological_fallback_uses_current_rows_relation(self):
        rows = [
            {"harness": "claude", "sid": "new", "relation": "chronological"},
            {"harness": "claude", "sid": "old", "relation": "unknown"},
        ]
        entries, reason = viewer._entries_for_generation(rows, 1)
        self.assertFalse(reason)
        self.assertEqual((entries[0]["sid"], entries[0]["_label"]),
                         ("old", "시간순 이웃 · 인계 미확인"))

    def test_projected_succession_defaults_to_its_real_predecessor(self):
        rows = [
            {"event": "start", "harness": "claude", "sid": "A", "ts": 1,
             "first_seen": 1, "source": "startup"},
            {"event": "start", "harness": "claude", "sid": "B", "ts": 2,
             "first_seen": 2, "source": "clear"},
            {"event": "succession", "harness": "claude", "sid": "B", "from": "A",
             "source": "tidy-clear", "ts": 2.1},
        ]
        lineage = viewer.session_tidy._project_seat_lineage(rows, {}, {}, sid="B")
        self.assertEqual(lineage[0]["relation"], "confirmed")
        self.assertEqual(lineage[0]["predecessor"], {"harness": "claude", "sid": "A"})
        entries, reason = viewer._entries_for_generation(lineage, 1)
        self.assertFalse(reason)
        self.assertEqual((entries[0]["sid"], entries[0]["_label"]),
                         ("A", "확정된 이전 대화"))

    def test_distinct_handover_and_succession_predecessors_remain_unknown(self):
        rows = [
            {"event": "start", "harness": "claude", "sid": "A", "ts": 1, "first_seen": 1},
            {"event": "start", "harness": "claude", "sid": "C", "ts": 2, "first_seen": 2},
            {"event": "start", "harness": "claude", "sid": "B", "ts": 3, "first_seen": 3},
            {"event": "handover", "harness": "claude", "sid": "B", "from": "A",
             "source": "clear"},
            {"event": "succession", "harness": "claude", "sid": "B", "from": "C",
             "source": "tidy-clear"},
        ]
        lineage = viewer.session_tidy._project_seat_lineage(rows, {}, {}, sid="B")
        self.assertEqual(lineage[0]["relation"], "unknown")
        self.assertIsNone(lineage[0]["predecessor"])

    def test_in_pane_status_and_range_messages_use_the_pager(self):
        with patch.object(viewer, "_show", return_value=False) as show:
            viewer._display([], 1)
            self.assertIn("연결 미확인", show.call_args.args[0])
            viewer._display([{"harness": "claude", "sid": "new", "relation": "chronological"},
                             {"harness": "claude", "sid": "old", "relation": "unknown"}], 3)
            self.assertIn("범위 밖", show.call_args.args[0])
        self.assertEqual(show.call_count, 2)

    def test_herdr_viewer_replaces_the_shell_with_exec(self):
        seat = viewer.session_tidy.Seat("pane", "key", "pane-id", "claude", "",
                                        endpoint="endpoint", socket_path="/tmp/herdr.sock")
        result = type("PaneResult", (), {"pane": "viewer-pane", "placement": "tab"})()
        with patch.object(viewer, "_identity", return_value=(seat, "pane-id", "/tmp/herdr.sock")), \
             patch.object(viewer, "open_pane", return_value=result) as open_pane:
            self.assertEqual(viewer.main([]), 0)
        argv = open_pane.call_args.args[0]
        self.assertEqual(argv[0], "exec")
        self.assertIn("--in-pane", argv)

    def test_split_viewer_focuses_the_new_pane_through_the_bound_runner(self):
        seat = viewer.session_tidy.Seat("pane", "key", "origin", "claude", "",
                                        endpoint="endpoint", socket_path="/tmp/herdr.sock")
        calls = []

        result = type("PaneResult", (), {"pane": "viewer", "placement": "split"})()

        def run(command, **kwargs):
            calls.append((command, kwargs["env"].get("HERDR_SOCKET_PATH")))
            return viewer.subprocess.CompletedProcess(command, 0, "{}", "")

        with patch.object(viewer, "_identity", return_value=(seat, "origin", "/tmp/herdr.sock")), \
             patch.object(viewer.HerdrSurface, "layout", return_value={"panes": [
                 {"pane_id": "origin", "rect": {"x": 0, "y": 0, "width": 50, "height": 40}},
                 {"pane_id": "viewer", "rect": {"x": 50, "y": 0, "width": 50, "height": 40}},
             ]}), \
             patch.object(viewer, "open_pane", return_value=result), \
             patch.object(viewer.subprocess, "run", side_effect=run):
            self.assertEqual(viewer.main([]), 0)
        self.assertEqual(calls, [(["herdr", "pane", "focus", "--pane", "origin",
                                   "--direction", "right"], "/tmp/herdr.sock")])

    def test_tab_viewer_does_not_attempt_to_focus_a_split(self):
        seat = viewer.session_tidy.Seat("pane", "key", "origin", "claude", "",
                                        endpoint="endpoint", socket_path="/tmp/herdr.sock")
        calls = []
        result = type("PaneResult", (), {"pane": "viewer", "placement": "tab"})()

        def runner(command):
            calls.append(command)
            return type("Process", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()

        with patch.object(viewer, "_identity", return_value=(seat, "origin", "/tmp/herdr.sock")), \
             patch.object(viewer, "HerdrSurface", side_effect=lambda runner: object()), \
             patch.object(viewer, "open_pane", return_value=result):
            self.assertEqual(viewer.main([]), 0)
        self.assertEqual(calls, [])

    def test_pager_menu_reports_generation_boundaries_and_clears_screen(self):
        rows = [
            {"harness": "claude", "sid": "new", "relation": "confirmed",
             "predecessor": {"harness": "claude", "sid": "old"}},
            {"harness": "claude", "sid": "old", "relation": "confirmed",
             "predecessor": {"harness": "claude", "sid": "older"}},
            {"harness": "claude", "sid": "older", "relation": "unknown"},
        ]
        with patch.object(viewer, "_show", return_value=True), \
             patch("builtins.input", side_effect=["p", "q"]) as prompt, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            viewer._display(rows, 2, clear_before_prompt=True)
        self.assertIn("더 앞 세대가 없습니다", prompt.call_args_list[1].args[0])
        self.assertEqual(output.getvalue(), "\x1b[2J\x1b[H\x1b[2J\x1b[H")
        with patch.object(viewer, "_show", return_value=True), \
             patch("builtins.input", side_effect=["n", "q"]) as prompt:
            viewer._display(rows, 1, clear_before_prompt=True)
        self.assertIn("더 최근 세대가 없습니다", prompt.call_args_list[1].args[0])

    def test_viewer_renders_missing_and_unreadable_status(self):
        missing = {"harness": "claude", "sid": "missing",
                   "transcript": {"path": "/missing/transcript"}}
        self.assertIn("원문 missing", viewer._render(missing)[0])
        with tempfile.TemporaryDirectory() as directory:
            unreadable = {"harness": "claude", "sid": "unreadable",
                          "transcript": {"path": directory}}
            self.assertIn("원문 unreadable", viewer._render(unreadable)[0])

    def test_detached_active_pane_uses_the_hook_seat_digest(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            cache = state / "hearting" / "session-tidy" / "project-keys.json"
            env = {"HOME": directory, "XDG_STATE_HOME": str(state),
                   "HERDR_ACTIVE_PANE_ID": "workspace:active", "HERDR_PANE_ID": "workspace:other",
                   "HERDR_SOCKET_PATH": "/tmp/herdr.sock"}
            endpoint = viewer.session_tidy._endpoint_scope(
                {"HERDR_SOCKET_PATH": env["HERDR_SOCKET_PATH"]})
            expected = viewer.session_tidy._pane_seat_of(
                env["HERDR_ACTIVE_PANE_ID"], endpoint=endpoint,
                socket_path=env["HERDR_SOCKET_PATH"])
            writes = []
            original_write = viewer.session_tidy.atomic_write_json

            def monitored_write(path, value):
                if Path(path) == cache:
                    writes.append(Path(path))
                return original_write(path, value)

            with patch.dict("os.environ", env, clear=True), \
                    patch.dict(viewer.session_tidy._PROJECT_KEYS, {}, clear=True), \
                    patch.object(viewer.session_tidy, "atomic_write_json",
                                 side_effect=monitored_write):
                self.assertFalse(cache.exists())
                actual, pane, socket = viewer._identity(Namespace(harness=None, session_id=None))
                self.assertFalse(cache.exists())
                self.assertFalse(state.exists())
            self.assertEqual(writes, [])
        self.assertEqual((actual.key, pane, socket), (expected.key, "workspace:active", "/tmp/herdr.sock"))

    def test_ambiguous_inherited_sessions_do_not_select_a_history_author(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            cache = state / "hearting" / "session-tidy" / "project-keys.json"
            env = {"HOME": directory, "XDG_STATE_HOME": str(state),
                   "CLAUDE_CODE_SESSION_ID": "claude-ambiguous",
                   "CODEX_THREAD_ID": "codex-ambiguous"}
            writes = []
            original_write = viewer.session_tidy.atomic_write_json

            def monitored_write(path, value):
                if Path(path) == cache:
                    writes.append(Path(path))
                return original_write(path, value)

            with patch.dict("os.environ", env, clear=True), \
                    patch.dict(viewer.session_tidy._PROJECT_KEYS, {}, clear=True), \
                    patch.object(viewer.session_tidy, "atomic_write_json",
                                 side_effect=monitored_write):
                self.assertFalse(cache.exists())
                seat, pane, socket = viewer._identity(Namespace(harness=None, session_id=None))

            self.assertIsNone(seat)
            self.assertEqual((pane, socket), ("", ""))
            self.assertEqual(writes, [])
            self.assertFalse(state.exists())

    def test_exact_native_query_ignores_a_different_inherited_pane_and_reads_confirmed_lineage(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            cache = state / "hearting" / "session-tidy" / "project-keys.json"
            env = {"HOME": directory, "XDG_STATE_HOME": str(state),
                   "CODEX_THREAD_ID": "native-current",
                   "HERDR_ACTIVE_PANE_ID": "unrelated-pane", "HERDR_SOCKET_PATH": "/tmp/other.sock"}
            cache_writes = []
            original_write = viewer.session_tidy.atomic_write_json

            def monitored_write(path, value):
                if Path(path) == cache:
                    cache_writes.append(Path(path))
                return original_write(path, value)

            with patch.dict("os.environ", env, clear=True), \
                 patch.dict(viewer.session_tidy._PROJECT_KEYS, {}, clear=True), \
                 patch.object(viewer.session_tidy, "atomic_write_json",
                              side_effect=monitored_write), \
                 patch.object(viewer.session_tidy, "codex_pane_for_session", return_value=""), \
                 patch.object(viewer.session_tidy, "_lineage_transcript",
                              return_value={"path": None, "status": "missing"}):
                self.assertFalse(cache.exists())
                caller = viewer.session_tidy.resolve_caller(
                    "codex", "native-current", str(Path.cwd()), project=False)
                self.assertIsNotNone(caller)
                physical, harness, sid = caller
                logical = viewer.session_tidy.session_seat(physical, harness, sid)
                with viewer.session_tidy.session_state_lock(logical):
                    viewer.session_tidy.record_event(logical, "codex", "native-current", "start",
                                                     source="clear", now=200)
                    viewer.session_tidy._append_ledger_entry(logical, {
                        "ts": 201, "harness": "codex", "sid": "native-current",
                        "event": "handover", "from": "native-predecessor", "source": "clear"})
                before_read = {path.relative_to(state): path.read_bytes()
                               for path in state.rglob("*") if path.is_file()}
                args = Namespace(harness="codex", session_id="native-current")
                actual, pane, socket = viewer._identity(args)
                rows = viewer.session_tidy.seat_lineage(actual)
                view_args = Namespace(harness="codex", session_id="native-current",
                                      seat_key="", pane="", endpoint="", socket_path="",
                                      generation=1, stdout=True)
                with patch.object(viewer, "_display", return_value=0) as display:
                    self.assertEqual(viewer._view(view_args), 0)
                view_rows = display.call_args.args[0]
                after_read = {path.relative_to(state): path.read_bytes()
                              for path in state.rglob("*") if path.is_file()}

            self.assertFalse(cache.exists())
            self.assertEqual(cache_writes, [])
            self.assertEqual(after_read, before_read)
            self.assertEqual(viewer.session_tidy.session_identity(actual),
                             ("codex", "native-current"))
            self.assertEqual((actual.sid, pane, socket), ("", "", ""))
            self.assertEqual(viewer.session_tidy.seat_state_key(actual),
                             viewer.session_tidy._digest("native-session", "codex", "native-current"))
            self.assertEqual(viewer.session_tidy._confirmed_predecessor(
                rows, "codex", "native-current")["sid"], "native-predecessor")
            self.assertEqual(viewer.session_tidy._confirmed_predecessor(
                view_rows, "codex", "native-current")["sid"], "native-predecessor")


SOCKET = "/tmp/hearting-session-history-test.sock"
CALLER_PANE, TARGET_PANE = "w1:pA", "w18:p7"
CALLER_OLD = "ca11e401-0000-4000-8000-000000000001"
CALLER = "c3757d71-0000-4000-8000-000000000002"
TARGET_OLD = "bbf14dfc-0000-4000-8000-000000000003"
TARGET = "5ed20fe8-0000-4000-8000-000000000004"
STRANGER = "0dd5e55a-0000-4000-8000-000000000005"   # never recorded anywhere


class ExplicitTargetTests(unittest.TestCase):
    """An explicit other session or seat key never opens the caller's conversation."""

    def setUp(self):
        self.st = viewer.session_tidy
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = Path(directory.name) / "state"
        self.env = {"HOME": directory.name, "XDG_STATE_HOME": str(state),
                    "HERDR_SOCKET_PATH": SOCKET, "HERDR_PANE_ID": CALLER_PANE,
                    "HERDR_ACTIVE_PANE_ID": CALLER_PANE, "CLAUDE_CODE_SESSION_ID": CALLER}
        for context in (
                patch.dict("os.environ", self.env, clear=True),
                patch.dict(self.st._PROJECT_KEYS, {}, clear=True),
                patch.object(self.st, "_herdr_executable", return_value=None),
                patch.object(self.st, "_lineage_transcript",
                             return_value={"path": None, "status": "missing"})):
            context.start()
            self.addCleanup(context.stop)
        self.endpoint = self.st._endpoint_scope({"HERDR_SOCKET_PATH": SOCKET})
        self.caller_seat = self.st._pane_seat_of(CALLER_PANE, endpoint=self.endpoint,
                                                 socket_path=SOCKET)
        self.target_seat = self.st._pane_seat_of(TARGET_PANE, endpoint=self.endpoint,
                                                 socket_path=SOCKET)
        for seat, old, new in ((self.caller_seat, CALLER_OLD, CALLER),
                               (self.target_seat, TARGET_OLD, TARGET)):
            for sid, source, now in ((old, "startup", 100.0), (new, "clear", 200.0)):
                logical = self.st.session_seat(seat, "claude", sid)
                with self.st.session_state_lock(logical):
                    self.st.record_event(logical, "claude", sid, "start", source=source, now=now)
            logical = self.st.session_seat(seat, "claude", new)
            with self.st.session_state_lock(logical):
                self.st._append_ledger_entry(logical, {
                    "ts": 201.0, "harness": "claude", "sid": new, "event": "handover",
                    "from": old, "source": "clear"})
        self.opened = type("PaneResult", (), {"pane": "", "placement": "none",
                                              "notes": ["test: no pane is created"]})()

    def run_main(self, argv, *, found=("", "")):
        with patch.object(viewer, "_herdr_pane_for_session", return_value=found) as lookup, \
             patch.object(viewer, "open_pane", return_value=self.opened) as open_pane, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(viewer.main(argv), 0)
        return output.getvalue(), lookup, open_pane

    def assert_not_caller(self, text):
        for sid in (CALLER, CALLER_OLD):
            self.assertNotIn(sid[:8], text)

    def test_d2a_other_session_id_opens_that_sessions_previous_generation(self):
        text, lookup, open_pane = self.run_main(
            ["--stdout", "--harness", "claude", "--session-id", TARGET],
            found=(TARGET_PANE, "claude"))
        lookup.assert_called_once_with("claude", TARGET)
        self.assertIn(f"claude {TARGET_OLD[:8]}", text)
        self.assert_not_caller(text)
        argv = open_pane.call_args.args[0]
        self.assertEqual(argv[argv.index("--seat-key") + 1], self.target_seat.key)
        self.assertEqual(argv[argv.index("--pane") + 1], TARGET_PANE)
        self.assertEqual(argv[argv.index("--harness") + 1:argv.index("--harness") + 4],
                         ["claude", "--session-id", TARGET])
        # The viewer opens beside the caller; the target's pane only locates its ledger.
        self.assertEqual(open_pane.call_args.kwargs["near"].pane, CALLER_PANE)
        self.assertEqual(open_pane.call_args.kwargs["env"], {"HERDR_SOCKET_PATH": SOCKET})
        # The opened viewer pane runs that argv and reads the same target seat.
        with patch.object(self.st, "resolve_seat", side_effect=AssertionError("re-resolved")), \
             contextlib.redirect_stdout(io.StringIO()) as pane_output:
            self.assertEqual(viewer.main(argv[3:] + ["--stdout"]), 0)
        self.assertIn(f"claude {TARGET_OLD[:8]}", pane_output.getvalue())
        self.assert_not_caller(pane_output.getvalue())

    def test_d2b_seat_key_is_honored_outside_the_viewer_pane(self):
        with patch.object(self.st, "resolve_caller", side_effect=AssertionError("caller")):
            text, lookup, open_pane = self.run_main(
                ["--stdout", "--seat-key", self.target_seat.key, "--pane", TARGET_PANE])
        lookup.assert_not_called()
        self.assertIn(f"claude {TARGET_OLD[:8]}", text)
        self.assert_not_caller(text)
        argv = open_pane.call_args.args[0]
        self.assertEqual(argv[argv.index("--seat-key") + 1], self.target_seat.key)
        self.assertEqual(argv[argv.index("--pane") + 1], TARGET_PANE)
        self.assertEqual(open_pane.call_args.kwargs["near"].pane, CALLER_PANE)

    def test_named_target_outside_herdr_prints_locally_without_a_pane(self):
        for name in ("HERDR_PANE_ID", "HERDR_ACTIVE_PANE_ID"):
            del viewer.os.environ[name]
        text, _lookup, open_pane = self.run_main(
            ["--stdout", "--harness", "claude", "--session-id", TARGET],
            found=(TARGET_PANE, "claude"))
        open_pane.assert_not_called()
        self.assertIn(f"claude {TARGET_OLD[:8]}", text)
        self.assert_not_caller(text)

    def test_d2c_viewer_pane_reads_the_named_seat_without_resolving_again(self):
        args = Namespace(seat_key=self.target_seat.key, pane=TARGET_PANE, harness="claude",
                         session_id=TARGET, endpoint=self.endpoint, socket_path=SOCKET,
                         generation=1, stdout=True)
        with patch.object(self.st, "resolve_seat", side_effect=AssertionError("re-resolved")), \
             patch.object(viewer, "_display", return_value=0) as display:
            self.assertEqual(viewer._view(args), 0)
        rows = display.call_args.args[0]
        self.assertEqual([row["sid"] for row in rows], [TARGET, TARGET_OLD])
        self.assertEqual(self.st._confirmed_predecessor(rows, "claude", TARGET)["sid"], TARGET_OLD)

    def test_d2d_unresolved_target_is_unknown_not_the_callers_conversation(self):
        caller = (self.st.session_seat(self.caller_seat, "claude", STRANGER), "claude", STRANGER)
        for found in (("", ""), (CALLER_PANE, "claude")):
            # The second is a herdr answer whose pane ledger is another session's.
            with self.subTest(found=found), \
                 patch.object(self.st, "resolve_caller", return_value=caller) as resolve:
                text, _lookup, open_pane = self.run_main(
                    ["--stdout", "--harness", "claude", "--session-id", STRANGER], found=found)
                self.assertIn("연결 미확인", text)
                self.assert_not_caller(text)
                open_pane.assert_not_called()
                resolve.assert_not_called()

    def test_d2d_target_without_a_pane_reads_its_exact_native_ledger(self):
        text, _lookup, open_pane = self.run_main(
            ["--stdout", "--harness", "claude", "--session-id", TARGET])
        self.assertIn(f"claude {TARGET_OLD[:8]}", text)
        self.assert_not_caller(text)
        open_pane.assert_not_called()

    def test_d2e_unknown_own_sid_treats_the_named_sid_as_a_target(self):
        # Own SID is unknown (no harness, no session variable). Falling back to
        # resolve_caller would accept the caller's inherited foreground pane.
        caller = (self.st.session_seat(self.caller_seat, "claude", STRANGER), "claude", STRANGER)
        with patch.dict("os.environ", {}, clear=False):
            del viewer.os.environ["CLAUDE_CODE_SESSION_ID"]
            self.assertEqual(self.st.session_from_env(None), (None, None))
            with patch.object(self.st, "resolve_caller", return_value=caller) as resolve:
                text, lookup, _open = self.run_main(
                    ["--stdout", "--session-id", TARGET], found=(TARGET_PANE, "claude"))
                lookup.assert_called_once_with(None, TARGET)
                self.assertIn(f"claude {TARGET_OLD[:8]}", text)
                self.assert_not_caller(text)
                for argv in (["--stdout", "--session-id", STRANGER],
                             ["--stdout", "--harness", "claude", "--session-id", STRANGER]):
                    text, _lookup, open_pane = self.run_main(argv)
                    self.assertIn("연결 미확인", text)
                    self.assert_not_caller(text)
                    open_pane.assert_not_called()
                resolve.assert_not_called()
            # A self call without its own session variable still reaches its own
            # history: through its exact herdr pane, else its exact native ledger.
            for found in ((CALLER_PANE, "claude"), ("", "")):
                text, _lookup, _open = self.run_main(
                    ["--stdout", "--harness", "claude", "--session-id", CALLER], found=found)
                self.assertIn(f"claude {CALLER_OLD[:8]}", text)
                self.assertNotIn(TARGET_OLD[:8], text)

    def test_own_sid_keeps_the_caller_path(self):
        import pane_ownership
        with patch.object(pane_ownership, "verified_pane", return_value=CALLER_PANE), \
             patch.object(self.st, "resolve_caller", wraps=self.st.resolve_caller) as resolve:
            text, lookup, open_pane = self.run_main(
                ["--stdout", "--harness", "claude", "--session-id", CALLER])
        lookup.assert_not_called()
        resolve.assert_called_once()
        self.assertIn(f"claude {CALLER_OLD[:8]}", text)
        argv = open_pane.call_args.args[0]
        self.assertEqual(argv[argv.index("--seat-key") + 1], self.caller_seat.key)

    def test_herdr_lookup_selects_one_exact_pane(self):
        agents = [
            {"agent": "claude", "pane_id": "w18:p7", "agent_session": {"value": TARGET}},
            {"agent": "codex", "pane_id": "w2:p1", "agent_session": {"value": STRANGER}},
            {"agent": "claude", "pane_id": "w3:p1", "agent_session": {"value": STRANGER}},
            {"agent": "claude", "pane_id": "", "agent_session": {"value": CALLER}},
        ]
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs.get("stdin"), kwargs.get("timeout")))
            return viewer.subprocess.CompletedProcess(
                command, 0, viewer.json.dumps({"result": {"agents": agents}}), "")

        with patch.object(self.st, "_herdr_executable", return_value="/fake/herdr"), \
             patch.object(viewer.subprocess, "run", side_effect=run):
            self.assertEqual(viewer._herdr_pane_for_session("claude", TARGET), ("w18:p7", "claude"))
            self.assertEqual(viewer._herdr_pane_for_session(None, TARGET), ("w18:p7", "claude"))
            self.assertEqual(viewer._herdr_pane_for_session("codex", TARGET), ("", ""))
            self.assertEqual(viewer._herdr_pane_for_session(None, STRANGER), ("", ""))
            self.assertEqual(viewer._herdr_pane_for_session("codex", STRANGER), ("w2:p1", "codex"))
            self.assertEqual(viewer._herdr_pane_for_session("claude", CALLER), ("", ""))
            with patch.dict("os.environ", {"AGENT_HERDR_SESSION": "left"}):
                viewer._herdr_pane_for_session("claude", TARGET)
        self.assertEqual(calls[0], (["/fake/herdr", "agent", "list"], viewer.subprocess.DEVNULL,
                                    self.st.HERDR_LOOKUP_TIMEOUT_SEC))
        self.assertEqual(calls[-1][0], ["/fake/herdr", "--session", "left", "agent", "list"])
        with patch.object(self.st, "_herdr_executable", return_value="/fake/herdr"), \
             patch.object(viewer.subprocess, "run", return_value=viewer.subprocess.CompletedProcess(
                 [], 1, "not json", "")):
            self.assertEqual(viewer._herdr_pane_for_session("claude", TARGET), ("", ""))
        with patch.object(viewer.subprocess, "run", side_effect=AssertionError("ran herdr")):
            self.assertEqual(viewer._herdr_pane_for_session("claude", TARGET), ("", ""))


class SeatHistoryIsolationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = {"HOME": str(self.root / "home"), "XDG_STATE_HOME": str(self.root / "state")}
        isolation = patch.dict(os.environ, env)
        isolation.start()
        self.addCleanup(isolation.stop)
        self.tidy = viewer.session_tidy
        self.x = self.tidy.Seat("pane", "seat-X", pane="w1:pX", harness="claude")
        self.y = self.tidy.Seat("pane", "seat-Y", pane="w1:pY", harness="claude")

    def start(self, seat, sid, ts):
        self.tidy._append_ledger_entry(seat, {"event": "start", "harness": "claude",
            "sid": sid, "source": "startup", "ts": ts, "first_seen": ts})

    def edge(self, seat, old, new, ts):
        self.tidy._append_ledger_entry(seat, {"event": "succession", "source": "tidy-clear",
            "harness": "claude", "sid": new, "from": old, "ts": ts})

    def seed_separate_histories(self):
        for seat, prefix, ts in ((self.x, "X", 1), (self.y, "Y", 100)):
            self.start(seat, prefix + "-old", ts)
            self.start(seat, prefix + "-new", ts + 1)
            self.edge(seat, prefix + "-old", prefix + "-new", ts + 2)

    def test_sid_omitted_history_key_uses_requested_seat_and_never_opens_unrelated_files(self):
        import dispatch_seat_handover
        self.seed_separate_histories()
        dispatch_seat_handover._LINEAGE_GRAPH_CACHE.clear()
        with patch.object(self.tidy, "_lineage_transcript", return_value={"path": None,
                          "status": "missing"}) as transcripts, \
             patch.object(self.tidy, "_read_ledger_lines", wraps=self.tidy._read_ledger_lines) as reads, \
             patch.object(self.tidy, "card_json_path", wraps=self.tidy.card_json_path) as cards:
            rows = self.tidy.seat_lineage(self.x)
        self.assertEqual([row["sid"] for row in rows], ["X-new", "X-old"])
        self.assertEqual({call.args[1] for call in transcripts.call_args_list}, {"X-new", "X-old"})
        ledger_keys = {path.stem for path in (self.tidy.state_root() / "sessions").glob("*.jsonl")}
        self.assertEqual([call.args[0].key for call in reads.call_args_list], sorted(ledger_keys),
                         "the graph reads each logical and physical ledger exactly once")
        self.assertEqual({self.tidy.session_identity(call.args[0])[1]
                          for call in cards.call_args_list}, {"X-new", "X-old"})
        selected, reason = viewer._entries_for_generation(rows, 1)
        self.assertEqual((selected[0]["sid"], reason), ("X-old", ""))

    def test_fleet_focus_receives_only_requested_history(self):
        self.seed_separate_histories()
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from fleet.explorer import focus
        with patch.object(focus, "utility", return_value=self.tidy), \
             patch.object(self.tidy, "resolve_seat", return_value=self.x), \
             patch.object(self.tidy, "_lineage_transcript", return_value={"path": None, "status": "missing"}):
            entries, health = focus.read_lineage({"pane": "w1:pX"}, "claude", "X-new")
        self.assertEqual([entry["sid"] for entry in entries], ["X-old"])

    def test_same_sid_pane_resume_keeps_confirmed_cross_ledger_predecessors(self):
        self.start(self.x, "A", 1)
        self.start(self.x, "B", 2)
        self.edge(self.x, "A", "B", 3)
        self.start(self.y, "B", 4)  # Exact SID resumed in another pane.
        self.start(self.y, "C", 5)
        self.edge(self.y, "B", "C", 6)
        with patch.object(self.tidy, "_lineage_transcript", return_value={"path": None, "status": "missing"}):
            rows = self.tidy.seat_lineage(self.y, harness="claude", sid="C")
        self.assertEqual([row["sid"] for row in rows], ["C", "B", "A"])
        entries, reason = viewer._entries_for_generation(rows, 1)
        self.assertEqual([entry["sid"] for entry in entries], ["B", "A"])
        self.assertFalse(reason)
        self.assertTrue(all(entry["_label"] == "확정된 이전 대화" for entry in entries))

    def test_local_chronological_rows_remain_but_ambiguous_foreign_chain_does_not_expand(self):
        self.start(self.x, "chronological", 1)
        self.start(self.x, "A", 2)
        self.start(self.x, "B", 3)
        self.edge(self.x, "A", "B", 4)
        self.start(self.y, "C", 100)
        self.edge(self.y, "A", "C", 101)
        with patch.object(self.tidy, "_lineage_transcript", return_value={"path": None, "status": "missing"}) as transcripts:
            rows = self.tidy.seat_lineage(self.x)
        self.assertEqual([row["sid"] for row in rows], ["B", "A", "chronological"])
        self.assertEqual(rows[0]["relation"], "unknown")
        self.assertIsNone(rows[0]["predecessor"])
        self.assertFalse(any(call.args[1] == "C" for call in transcripts.call_args_list))


if __name__ == "__main__":
    unittest.main()
