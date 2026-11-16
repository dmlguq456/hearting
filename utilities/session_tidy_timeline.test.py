"""The person's own prompts are kept, one shortened line each, in the session timeline.

The prompt hook of every harness reaches ``session_tidy.run_hook``; this drives that
entry with each harness's input shapes. No model and no real session state is touched.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import stat
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools/memory/tests"), str(ROOT / "tools"), str(ROOT)]
from tidy_isolation import isolated_env
import session_tidy as st
import session_tidy_clear as clear
from fleet import titles


class PromptTimelineTests(unittest.TestCase):
    def setUp(self):
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)
        self.cwd = self.iso.root / "project"
        self.cwd.mkdir()
        self.env = self.iso.patched_environ({"HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
                                             "FLEET_TITLE_STATE_DIR": str(self.iso.root / "titles")})
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        for target in (mock.patch.object(clear, "_start_helper"),
                       mock.patch("human_wait_state._request_producer")):
            target.start()
            self.addCleanup(target.stop)

    def prompt(self, text, harness="claude", sid="timeline-session", now=1000, origin=None,
               transcript="", env=None):
        seat = st.session_seat(st.Seat("native", "", harness=harness, sid=sid), harness, sid)
        with mock.patch.object(st, "resolve_seat", return_value=seat), \
                mock.patch.object(st, "_current_request_origin", return_value=origin):
            st.run_hook(harness, "prompt", sid, cwd=str(self.cwd), prompt_text=text, now=now,
                        transcript=transcript, env=env)
        return titles.read_timeline(sid, harness=harness)

    def test_a_human_prompt_leaves_one_line_in_every_harness(self):
        for harness in st.HARNESSES:
            with self.subTest(harness=harness):
                sid = "human-" + harness
                transcript = self.iso.root / (harness + ".jsonl")
                transcript.write_text("x" * 321)
                rows = self.prompt("첫 줄을 읽어줘\n둘째 줄 비밀 내용\n셋째 줄", harness, sid, 1000,
                                   transcript=str(transcript))
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual((row["kind"], row["at"], row["text"], row["lines"], row["paste"],
                                  row["answer"], row["from"], row["seq"]),
                                 ("prompt", 1000, "첫 줄을 읽어줘", 3, False, False, 321, 1))
                path = Path(titles.timeline_path(sid, harness=harness))
                self.assertNotIn("비밀 내용", path.read_text())  # the original is never stored
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.prompt("두 번째 요청", harness, sid, 1010)
                self.assertEqual([item["text"] for item in titles.read_timeline(sid, harness=harness)],
                                 ["첫 줄을 읽어줘", "두 번째 요청"])

    def test_the_text_is_the_panels_one_line_with_secrets_hidden_and_a_length_cap(self):
        secret = "ghp_" + "a1B2c3D4e5" * 4
        rows = self.prompt("  \n토큰 %s 로 올려줘\n다음 줄" % secret, sid="redact")
        self.assertEqual(rows[0]["text"], st.prompt_first_line("토큰 %s 로 올려줘" % secret))
        self.assertNotIn(secret, Path(titles.timeline_path("redact")).read_text())
        self.assertIn("[redacted]", rows[0]["text"])
        rows = self.prompt("가" * 900, sid="long")
        self.assertEqual(len(rows[0]["text"]), st.PROMPT_LINE_CHARS)
        self.assertTrue(rows[0]["text"].endswith("…"))

    def test_a_whole_paste_is_marked_and_shown_without_its_shell(self):
        for harness in st.HARNESSES:
            with self.subTest(harness=harness):
                sid = "paste-" + harness
                rows = self.prompt('<pasted_content id="p">\n붙인 첫 줄\n둘째\n셋째\n넷째\n</pasted_content>',
                                   harness, sid)
                self.assertEqual((rows[0]["text"], rows[0]["lines"], rows[0]["paste"]),
                                 ("붙인 첫 줄", 4, True))

    def test_only_the_person_is_kept_and_four_runtime_kinds_are_not(self):
        sid = "kinds"
        peer_origin = {"kind": "peer", "harness": "claude", "session_id": sid,
                       "request_digest": "a" * 64, "transfer_ref": "b" * 32,
                       "transfer_body_sha256": "c" * 64, "name": "Codex [73]"}
        shapes = {
            "claude": ("<task-notification>\n<summary>done</summary>\n</task-notification>",
                       "<system-reminder>runtime request</system-reminder>"),
            "codex": ('<subagent_notification>{"status":{"completed":"done"}}</subagent_notification>',
                      "<system-reminder>runtime request</system-reminder>"),
            "opencode": ('<subagent sessionID="child-1" state="completed" description="Review">\nworker output\n</subagent>',
                         '<shell id="s2" state="completed" command="ls">\nout\n</shell>'),
        }
        for harness, (notification, runtime) in shapes.items():
            with self.subTest(harness=harness):
                sid = "kinds-" + harness
                self.prompt("사람이 친 글", harness, sid, 1000)
                self.prompt(notification, harness, sid, 1010)                       # notification
                self.prompt(runtime, harness, sid, 1020)                            # runtime
                self.prompt("다른 세션의 요청\n둘째", harness, sid, 1030, origin=peer_origin)  # peer
                with mock.patch.object(st, "matching_tidy_claim", return_value={"claim_id": "c1"}):
                    self.prompt("하팅의 정리 예약 글", harness, sid, 1040)          # claim
                rows = titles.read_timeline(sid, harness=harness)
                self.assertEqual([row["text"] for row in rows], ["사람이 친 글"])
                self.prompt("또 사람이 친 글", harness, sid, 1050)
                self.assertEqual([row["text"] for row in titles.read_timeline(sid, harness=harness)],
                                 ["사람이 친 글", "또 사람이 친 글"])

    def test_a_human_line_added_to_a_runtime_block_is_still_the_person(self):
        text = "<system-reminder>안내</system-reminder>\n내가 친 글"
        rows = self.prompt(text, sid="mixed")
        # kept because the existing classifier calls it the person's; the text is the panel's line
        self.assertEqual([row["text"] for row in rows], [st.prompt_first_line(text)])

    def test_the_answer_to_an_open_question_is_marked(self):
        open_wait = {"id": "q1", "mode": "message", "lifecycle": "open", "question": "고를까요?"}
        with mock.patch("human_wait_state._read", return_value=open_wait), \
                mock.patch("human_wait_state.close_signal", return_value=True):
            rows = self.prompt("1번으로 해줘", sid="answer")
        self.assertTrue(rows[0]["answer"])
        with mock.patch("human_wait_state._read", return_value=open_wait), \
                mock.patch("human_wait_state.close_signal", return_value=False):
            rows = self.prompt("다른 말", sid="answer", now=1010)
        self.assertFalse(rows[1]["answer"])
        # no open question: close_signal is true but nothing was answered
        with mock.patch("human_wait_state._read", return_value=None), \
                mock.patch("human_wait_state.close_signal", return_value=True):
            rows = self.prompt("그냥 말", sid="answer", now=1020)
        self.assertFalse(rows[2]["answer"])

    def test_a_missing_transcript_leaves_no_position_and_a_failed_write_never_fails_the_hook(self):
        rows = self.prompt("위치 없음", sid="nopos", transcript=str(self.iso.root / "missing.jsonl"))
        self.assertNotIn("from", rows[0])
        with mock.patch.object(titles, "append_timeline", side_effect=OSError("disk")):
            self.prompt("실패해도", sid="fails")
        self.assertEqual(st.prompt_display(
            st.session_seat(st.Seat("native", "", harness="claude", sid="fails"), "claude", "fails"),
            "claude", "fails")["human"]["text"], "실패해도")

    def test_a_registered_worker_session_leaves_nothing(self):
        for key, value in (("AGENT_SESSION_ROLE", "worker"), ("AGENT_DISPATCH_CHILD", "1"),
                           ("AGENT_DISPATCH_DEPTH", "2")):
            with self.subTest(marker=key):
                env = {**os.environ, key: value}
                rows = self.prompt("일꾼의 글", sid="worker-" + key, env=env)
                self.assertEqual(rows, [])

    def test_the_writer_and_the_reader_agree_on_the_field_set(self):
        rows = self.prompt("필드", sid="fields")
        self.assertEqual(sorted(rows[0]), ["answer", "at", "kind", "lines", "paste", "seq", "text"])
        self.assertEqual(json.loads(Path(titles.timeline_path("fields")).read_text()), rows[0])


if __name__ == "__main__":
    unittest.main()
