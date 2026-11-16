"""A person's answer from Fleet reaches the idle shell session that asked.

A real session-shell host (this checkout's, and the v4.29.3 host taken from git) runs a
synthetic native in a private PTY with a private HOME; no model, user pane or account is
used. The native acts as Claude does: lifecycle events over the shell socket, a composer
painted as ``❯ …``, and the real UserPromptSubmit hooks run on every prompt it takes.
The obligation runner's own pass (``_reconcile_obligations``) is called here instead of a
detached runner, so the suite owns every process it starts.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import shutil
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from fixtures.session_shell_rig import (  # noqa: E402
    OLD_RELEASE, ROOT, SID, AnswerReachesIdleShellTest, ShellRig)


class AnswerReachesIdleSessionTest(AnswerReachesIdleShellTest):
    def _takes_once(self, harness):
        rig = ShellRig(self, ROOT / "utilities/session-shell/entry.py", "current-" + harness, harness).start()
        signal_ = self.ask(harness)
        self.fleet_answer(signal_)
        self.courier()
        rig.wait(lambda: rig.prompts(), "the idle window never received the answer")
        self.courier(passes=3)
        states = self.states(signal_)
        self.assertEqual(len(rig.prompts()), 1, rig.prompts())
        self.assertIn("PRIVATE-ANSWER", rig.prompts()[0]["prompt"])
        self.assertEqual(states["delivery"]["state"], "acked", states)
        self.assertEqual(states["delivery"]["claim_authority"], "deliverer-unproven", states)
        self.assertEqual(states["request_state"], "applied", states)
        self.assertTrue(rig.wait(rig.idle_empty, "the window did not return to idle"))

    def test_current_claude_shell_takes_the_fleet_answer_once_without_a_person(self):
        self._takes_once("claude")

    def test_current_opencode_shell_takes_the_fleet_answer_once_without_a_person(self):
        self._takes_once("opencode")

    def test_a_typed_draft_keeps_the_answer_waiting_until_the_person_submits(self):
        rig = ShellRig(self, ROOT / "utilities/session-shell/entry.py", "draft").start()
        signal_ = self.ask()
        rig.type("half typed")
        rig.wait_for(b"half typed", "the draft never painted")
        self.fleet_answer(signal_)
        self.courier(passes=3)
        states = self.states(signal_)
        self.assertEqual(rig.prompts(), [], "the answer was typed over the person's draft")
        self.assert_field_wait(states, "native-pending", "not-idle-empty")
        # The person's own Enter submits their text; that prompt's hooks hand the answer over.
        rig.type("\r")
        rig.wait(lambda: rig.prompts(), "the person's prompt never ran")
        self.courier(passes=2)
        prompts = rig.prompts()
        self.assertEqual([row["prompt"] for row in prompts], ["half typed"])
        self.assertIn("PRIVATE-ANSWER", prompts[0]["context"])
        self.assertEqual(self.states(signal_)["request_state"], "applied", self.states(signal_))

    def _old_tree_or_skip(self):
        if self.old_tree is None:
            self.skipTest(OLD_RELEASE + " is not in this checkout's history")
        return self.old_tree

    def test_an_old_shell_host_never_takes_the_answer_and_says_where_it_stopped(self):
        rig = ShellRig(self, self._old_tree_or_skip() / "utilities/session-shell/entry.py", "old").start()
        signal_ = self.ask()
        self.fleet_answer(signal_)
        self.courier(passes=4)
        self.assertTrue(rig.idle_empty(), "the old window is not idle and empty")
        self.assertEqual(rig.prompts(), [])
        # Idle and empty, its host still refuses: its backend cannot verify this release's answer
        # notice, so the host's payload step answers `peer-claim-changed` and starts nothing.
        self.assert_field_wait(self.states(signal_), "session-shell-not-started", "peer-claim-changed")
        # One prompt the person types in that window carries the answer to the session.
        rig.type("hello\r")
        rig.wait(lambda: rig.prompts(), "the person's prompt never ran")
        self.courier(passes=2)
        prompts = rig.prompts()
        self.assertEqual([row["prompt"] for row in prompts], ["hello"])
        self.assertIn("PRIVATE-ANSWER", prompts[0]["context"])
        self.assertEqual(self.states(signal_)["request_state"], "applied", self.states(signal_))

    def test_an_old_window_after_an_install_shows_new_release_and_the_answer_says_why_it_waits(self):
        import session_shell
        from fleet.session_block import sent_answer_line
        link = self.install(self._old_tree_or_skip())
        rig = ShellRig(self, link / "utilities/session-shell/entry.py", "installed").start()
        signal_ = self.ask()
        # Something in the composer: the window neither takes the answer nor hands over yet.
        rig.type("still typing")
        rig.wait_for(b"still typing", "the draft never painted")
        self.assertFalse(session_shell.release_behind(rig.binding))
        self.move(link, ROOT)
        # Its host's own rule (the realpath its launch path names now) says it is behind. Its v4.29.3
        # side panel drew no `New release waiting` here; a window whose launch path was pinned to a
        # release (every Herdr window before this change) cannot see the next release at all.
        rig.wait(lambda: session_shell.release_behind(rig.current_binding()), "the move was not seen as a newer release")
        # The reason is given while the window rests (Fleet's state for it); never while it works.
        self.assertEqual(session_shell.answer_delivery_wait("claude", SID, "idle"), "old-shell")
        self.assertEqual(session_shell.answer_delivery_wait("claude", SID, "working"), "")
        board = [{"id": signal_["id"], "kind": "message", "answer": "sent", "owner_state": "live",
                  "harness": "claude", "session_id": SID}]
        resting = {("claude", SID): "idle"}
        self.assertEqual(session_shell.mark_answer_waits(board, resting)[0]["answer_wait"], "old-shell")
        self.fleet_answer(signal_)
        self.courier(passes=3)
        self.assertEqual(rig.prompts(), [])
        # The records of the stuck window seen on 11-16, by name.
        self.assert_field_wait(self.states(signal_), "native-pending", "not-idle-empty")
        line = "".join(text for text, _key in sent_answer_line(["PRIVATE-ANSWER"], wait="old-shell"))
        self.assertRegex(line, r"PRIVATE-ANSWER · (awaiting delivery|전달 대기) · (New release waiting|새 판 대기) · ")
        # The person's Enter in that window hands the answer over; then the window takes the new release.
        rig.type("\r")
        rig.wait(lambda: rig.prompts(), "the person's prompt never ran")
        self.courier(passes=2)
        self.assertIn("PRIVATE-ANSWER", rig.prompts()[0]["context"])
        self.assertEqual(self.states(signal_)["request_state"], "applied", self.states(signal_))
        rig.wait(lambda: rig.idle_empty() and session_shell.release_behind(rig.current_binding()) is False,
                 "the window never handed over to the installed release")
        self.assertEqual(session_shell.answer_delivery_wait("claude", SID, "idle"), "")
        self.assertNotIn("answer_wait", session_shell.mark_answer_waits(board, resting)[0])

    def test_a_herdr_window_keeps_its_installed_launch_path_and_sees_the_next_release(self):
        import session_shell
        link = self.install(ROOT)
        herdr = {"HERDR_ENV": "1", "HERDR_PANE_ID": "private:p1", "HERDR_BIN_PATH": "/nonexistent/herdr",
                 "HERDR_SOCKET_PATH": str(self.case / "absent-herdr.sock")}
        rig = ShellRig(self, link / "utilities/session-shell/entry.py", "herdr", env=herdr).start()
        environ = Path("/proc", str(rig.binding["host_pid"]), "environ").read_bytes().split(b"\0")
        launch = next(entry for entry in environ if entry.startswith(b"HEARTING_SHELL_HOST_PATH="))
        # The installed link as the launcher named it: the release it points at may move.
        self.assertEqual(launch.split(b"=", 1)[1].decode(), str(link / "utilities/session-shell/host.cjs"))
        self.assertIn(b"HERDR_AGENT=claude", Path("/proc", str(rig.binding["native_pid"]), "environ").read_bytes())
        rig.type("held")
        rig.wait_for(b"held", "the draft never painted")
        # The next release, installed while the window runs (the draft holds its hand-over).
        following = self.case / "following-release/utilities/session-shell"
        following.mkdir(parents=True)
        shutil.copy2(ROOT / "utilities/session-shell/host.cjs", following / "host.cjs")
        self.move(link, self.case / "following-release")
        rig.wait(lambda: self.upgrade_note(rig), "a Herdr window never noticed the next release")
        self.assertTrue(session_shell.release_behind(rig.current_binding()))


class SentAnswerLineTest(unittest.TestCase):
    """The same line in Fleet and the panel; in no count; nothing to send again (rule 5)."""

    def setUp(self):
        for path in (ROOT / "tools", ROOT / "utilities"):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))

    def snapshot(self, wait=None):
        item = {"id": "S4:sent", "source": "S4", "kind": "message", "since": 900, "stale": "current",
                "harness": "claude", "session_id": "sid-a", "answer": "sent"}
        if wait:
            item["answer_wait"] = wait
        from fleet.refresh import fleet_answer_wait_view
        return {"sessions": [{"harness": "claude", "session_id": "sid-a", "session_tag": "a1",
                              "cwd": "/synthetic/repo", "liveness": "idle"}],
                "project_of": lambda _cwd: "repo",
                "answer_waits": fleet_answer_wait_view({"items": [item], "total": 0, "older": 0,
                                                        "complete": True, "health": []}),
                "resources": [], "jobs": []}

    def board(self, vm, state=None):
        from fleet.explorer.frame import draw
        from fleet.explorer.state import NavState
        return draw(vm, state or NavState(view="QUESTIONS", title="QUESTIONS"), 220, 50, beat=1).text_rows()

    def test_fleet_row_says_the_answer_awaits_delivery_and_why_and_counts_nothing(self):
        from fleet.config import ui_text
        from fleet.explorer.live import build_view_model
        for wait, why in (("", None), ("old-shell", "session_shell.upgrade_pending"),
                          ("no-shell", "fleet.session_block.wait_no_shell")):
            with self.subTest(wait=wait):
                vm = build_view_model(self.snapshot(wait), now=1000)
                self.assertEqual([(q["sent"], q["answer_wait"]) for q in vm.questions], [(True, wait or None)])
                rows = self.board(vm)
                row = next(line for line in rows if "[a1]" in line)
                expected = ui_text("fleet.session_block.sent").removeprefix("✓").strip() + " · " + ui_text(
                    "fleet.session_block.delivery_waiting")
                self.assertIn(expected, row)
                self.assertIn("✓", row)
                if why:
                    self.assertIn(ui_text(why), row)
                from fleet.explorer.tables import answer_wait_word
                self.assertIn(answer_wait_word() + " 0 ", next(line for line in rows if answer_wait_word() in line))
                self.assertTrue(all(not session.get("ask") for session in vm.sessions))

    def test_fleet_unfolded_answer_shows_what_was_sent_and_offers_no_resend(self):
        from fleet.config import ui_text
        from fleet.explorer.live import build_view_model
        from fleet.explorer.state import NavState
        vm = build_view_model(self.snapshot("old-shell"), now=1000)
        question = vm.questions[0]
        envelope = {"v": 1, "harness": "claude", "session_id": "sid-a", "request_id": "S4:sent",
                    "kind": "question", "state": "submitted", "support": {"supported": True},
                    "detail": {"questions": [{"id": "message-response", "question": "Which one?",
                                              "choices": [], "is_other": True}]},
                    "question_id": "message-response", "sent": {"answers": {"message-response": ["PRIVATE-ANSWER"]}},
                    "native_ref": {"transport": "hearting-message-answer"}}
        state = NavState(view="QUESTIONS", folds=(("qsel:QUESTIONS", question["tag"]),),
                         question_overlay={"ref": dict(question["ref"]), "detail": envelope, "draft": None,
                                           "state": "submitted", "editing": False, "pending": False})
        text = "\n".join(self.board(vm, state))
        self.assertIn("PRIVATE-ANSWER · " + ui_text("fleet.session_block.delivery_waiting"), text)
        self.assertIn(ui_text("fleet.session_block.wait_old_shell"), text)
        self.assertNotIn("submit", text)

    def test_the_panel_receives_the_same_parts_fleet_draws(self):
        from fleet.session_block import question_presentations, sent_answer_line
        item = {"id": "S4:sent", "revision": "r", "answerable": False, "request_state": "submitted",
                "sent": ["PRIVATE-ANSWER"], "questions": [{"id": "message-response", "question": "Which one?"}]}
        question_presentations([item], 80, brief_columns=36)
        self.assertEqual(item["sent_line"], sent_answer_line(["PRIVATE-ANSWER"]))
        self.assertEqual([part[1] for part in item["sent_line"][:3]], ["lvl_g", "dim", "loc_repo"])
        delivered = dict(item, request_state="applied")
        question_presentations([delivered], 80)
        self.assertNotIn("sent_line", delivered)

    def test_secrets_stay_hidden_in_the_line(self):
        from fleet.session_block import sent_answer_line
        line = "".join(text for text, _key in sent_answer_line(
            {"answers": {"q": ["PRIVATE-SECRET"]}}, questions=[{"id": "q", "is_secret": True}]))
        self.assertNotIn("PRIVATE-SECRET", line)
        self.assertIn("•••", line)

    def test_the_panel_line_hides_a_secret_and_carries_no_control_characters(self):
        from fleet.session_block import question_presentations
        raw = "a\x1b[31mb\x00c\u202ed\u2066e\x9bf\x7fg"
        for key in ("is_secret", "isSecret", "secret"):
            with self.subTest(key=key):
                item = {"id": "S4:sent", "revision": "r", "answerable": False, "request_state": "submitted",
                        "sent": {"answers": {"pw": ["TOP-SECRET-PW"], "note": [raw]}},
                        "questions": [{"id": "pw", "question": "Password?", key: True},
                                      {"id": "note", "question": "Note?"}]}
                question_presentations([item], 80, brief_columns=36)
                line = "".join(text for text, _key in item["sent_line"])
                self.assertNotIn("TOP-SECRET-PW", line)
                self.assertIn("•••", line)
                self.assertIn("a[31mbcdefg", line)
                self.assertEqual([ch for ch in line if ord(ch) < 32 or 0x7f <= ord(ch) <= 0x9f
                                  or ch in "\u202e\u2066"], [])
        # The list the board already carries (`questions.py` hides its secrets) loses its controls too.
        item = {"id": "S4:sent", "revision": "r", "answerable": False, "request_state": "submitted",
                "sent": ["•••", raw], "questions": [{"id": "pw", "question": "Password?"}]}
        question_presentations([item], 80)
        self.assertIn("•••, a[31mbcdefg", "".join(text for text, _key in item["sent_line"]))

    def test_what_each_harness_waits_for_without_a_shell(self):
        import session_shell
        with mock.patch.object(session_shell, "exact_binding", return_value=(None, "attachment-unavailable")):
            self.assertEqual(session_shell.answer_delivery_wait("claude", "sid-a", "idle"), "no-shell")
            # OpenCode's plugin takes it when idle; Codex from its own queue.
            self.assertEqual(session_shell.answer_delivery_wait("opencode", "sid-a", "idle"), "")
            self.assertEqual(session_shell.answer_delivery_wait("codex", "sid-a", "idle"), "")
        with mock.patch.object(session_shell, "exact_binding", return_value=(None, "attachment-ambiguous")):
            self.assertEqual(session_shell.answer_delivery_wait("claude", "sid-a", "idle"), "")
        with mock.patch.object(session_shell, "exact_binding", return_value=(None, "attachment-unavailable")):
            board = [{"kind": "message", "answer": "sent", "owner_state": state, "harness": "claude",
                      "session_id": "sid-a"} for state in ("live", "none", "successor")]
            board.append({"kind": "message", "harness": "claude", "session_id": "sid-a"})
            self.assertEqual([item.get("answer_wait") for item in
                              session_shell.mark_answer_waits(board, {("claude", "sid-a"): "idle"})],
                             ["no-shell", None, None, None])

    def test_the_reason_is_given_only_while_the_session_rests(self):
        # Working: it takes the answer when its turn ends, so no reason. Not known: no reason either.
        import session_shell
        binding = {"harness": "claude", "sid": "sid-a"}
        windows = {"old-shell": ((binding, ""), True), "same-release": ((binding, ""), False),
                   "no-shell": ((None, "attachment-unavailable"), None)}
        expected = {"old-shell": "old-shell", "same-release": "", "no-shell": "no-shell"}
        for window, (bound, behind) in windows.items():
            for state in ("working", "blocked", "idle", "unused", "stale", "unknown", "dead", None):
                want = expected[window] if state in ("idle", "unused", "stale") else ""
                with self.subTest(window=window, state=state), \
                        mock.patch.object(session_shell, "exact_binding", return_value=bound), \
                        mock.patch.object(session_shell, "release_behind", return_value=behind):
                    self.assertEqual(session_shell.answer_delivery_wait("claude", "sid-a", state), want)
                    board = [{"kind": "message", "answer": "sent", "owner_state": "live", "harness": "claude",
                              "session_id": "sid-a", "answer_wait": "old-shell"}]
                    states = {} if state is None else {("claude", "sid-a"): state}
                    self.assertEqual(session_shell.mark_answer_waits(board, states)[0].get("answer_wait"),
                                     want or None)

    def test_fleet_marks_the_reason_from_each_sessions_own_state(self):
        # The collector reads the session's classified state (and its resumed ids) for the mark.
        from types import SimpleNamespace
        import session_shell
        from fleet.collectors import answer_wait_view
        for liveness, want in (("working", None), ("idle", "old-shell"), ("unknown", None)):
            with self.subTest(liveness=liveness), \
                    mock.patch.object(session_shell, "exact_binding", return_value=({"harness": "claude"}, "")), \
                    mock.patch.object(session_shell, "release_behind", return_value=True):
                board = {"items": [{"id": "S4:sent", "source": "S4", "kind": "message", "since": 900,
                                    "stale": "current", "harness": "claude", "session_id": "sid-old",
                                    "answer": "sent", "owner_state": "live"}],
                         "total": 0, "older": 0, "complete": True, "health": []}
                sessions = [SimpleNamespace(harness="claude", session_id="sid-new", session_aliases=["sid-old"],
                                            liveness=liveness)]
                view = answer_wait_view(board, sessions)
                self.assertEqual([item.get("answer_wait") for item in view["items"]], [want])

    def test_each_harness_takes_one_answer_once_through_the_existing_courier(self):
        from fixtures.message_questions import MessageQuestionFixture
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness), MessageQuestionFixture() as fixture:
                signal_, envelope = fixture.ask(harness)
                fixture.submit(envelope, "PRIVATE-ANSWER")
                calls, ref, row = fixture.courier(signal_)
                self.assertEqual(len(calls), 1, calls)
                self.assertIn("PRIVATE-ANSWER", calls[0])
                fixture.consume(signal_, calls[0])
                # Received once: the record is acked and the question applied, so no pass sends it again.
                self.assertEqual(fixture.record(signal_)["state"], "acked")
                import human_wait_state as waits
                self.assertEqual(waits.read_selected_request(
                    {"id": signal_["id"], "harness": harness, "session_id": signal_["session_id"]})["state"], "applied")


if __name__ == "__main__":
    unittest.main()
