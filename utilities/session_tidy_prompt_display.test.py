"""The existing prompt classifier also supplies a bounded, read-only Fleet view."""
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools/memory/tests"), str(ROOT)]
from tidy_isolation import isolated_env
import session_tidy as st
import session_tidy_clear as clear
from tools.fleet.collectors import prompt_inputs


class PromptDisplayTests(unittest.TestCase):
    def setUp(self):
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)
        self.cwd = self.iso.root / "project"
        self.cwd.mkdir()
        self.env = self.iso.patched_environ({"HEARTING_TIDY_TEST_ROOT": str(self.iso.root)})
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        self.helper = mock.patch.object(clear, "_start_helper")
        self.helper.start()
        self.addCleanup(self.helper.stop)
        self.wait_projection = mock.patch("human_wait_state._request_producer")
        self.wait_projection.start()
        self.addCleanup(self.wait_projection.stop)
        prompt_inputs._CACHE.clear()

    def seat(self, harness="claude", sid="prompt-session"):
        return st.session_seat(st.Seat("native", "", harness=harness, sid=sid), harness, sid)

    def prompt(self, text, harness="claude", sid="prompt-session", now=1000, origin=None):
        seat = self.seat(harness, sid)
        with mock.patch.object(st, "resolve_seat", return_value=seat), \
                mock.patch.object(st, "_current_request_origin", return_value=origin):
            st.run_hook(harness, "prompt", sid, cwd=str(self.cwd), prompt_text=text, now=now)
        return st.prompt_display(seat, harness, sid)

    def test_all_harnesses_keep_human_input_across_runtime_and_pasted_peer_prompts(self):
        for harness in st.HARNESSES:
            with self.subTest(harness=harness):
                original = self.prompt("내가 쓴 첫 줄\n둘째 줄", harness, now=1000)
                self.assertEqual(original["human"]["text"], "내가 쓴 첫 줄")
                seat = self.seat(harness)
                frontier = st.real_prompt_frontier(seat, harness, "prompt-session")
                for index, text in enumerate((
                    "<task-notification>finished</task-notification>",
                    '<pasted_content id="peer">\n<agent-message>peer request</agent-message>\n</pasted_content id="peer">',
                    "<system-reminder>runtime request</system-reminder>",
                )):
                    result = self.prompt(text, harness, now=1010 + index)
                    self.assertEqual(result["human"], original["human"])
                    self.assertEqual(st.real_prompt_frontier(seat, harness, "prompt-session"), frontier)
                self.assertEqual([row["source"] for row in result["received"]],
                                 ["notification", "peer", "runtime"])

    def test_verified_peer_and_self_request_do_not_replace_the_human(self):
        self.prompt("original", now=1000)
        origin = {"kind": "peer", "harness": "claude", "session_id": "prompt-session",
                  "request_digest": "a" * 64, "transfer_ref": "b" * 32,
                  "transfer_body_sha256": "c" * 64, "name": "Codex [73]"}
        result = self.prompt("human-looking peer first line\nprivate second line", now=1010, origin=origin)
        self.assertEqual(result["human"]["text"], "original")
        self.assertEqual((result["received"][-1]["sender"], result["received"][-1]["text"]),
                         ("Codex [73]", "human-looking peer first line"))
        with mock.patch.object(st, "matching_tidy_claim", return_value={"claim_id": "synthetic-claim"}):
            result = self.prompt("Hearting's own request", now=1020)
        self.assertEqual(result["human"]["text"], "original")
        self.assertEqual(result["received"][-1]["source"], "claim")

    def test_a_literal_approval_is_still_the_persons_input(self):
        result = self.prompt("진행")
        self.assertEqual(result["human"]["text"], "진행")
        self.assertEqual(result["received"], [])

    def test_a_large_received_message_uses_its_verified_original_not_the_preview(self):
        from types import SimpleNamespace
        reader = mock.Mock(return_value={"body": "원문 첫 줄\nPRIVATE SECOND LINE"})
        fake_spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None))
        with mock.patch("importlib.util.spec_from_file_location", return_value=fake_spec), \
                mock.patch("importlib.util.module_from_spec", return_value=SimpleNamespace(read_peer_payload=reader)):
            line = st._received_prompt_line("Long peer message. Preview only", {"transfer_ref": "a" * 32})
        self.assertEqual(line, "원문 첫 줄")
        reader.assert_called_once_with("a" * 32)

    def test_long_multiline_and_secret_values_are_bounded_before_projection(self):
        result = self.prompt("가" * 1000 + "\nSECOND LINE NEVER SHOWN")
        self.assertEqual(len(result["human"]["text"]), st.PROMPT_LINE_CHARS)
        self.assertTrue(result["human"]["text"].endswith("…"))
        result = self.prompt("password=PRIVATE_SECRET\nsecond", now=1010)
        self.assertNotIn("PRIVATE_SECRET", result["human"]["text"])
        self.assertNotIn("second", result["human"]["text"])

    def test_exact_identity_no_alias_and_received_retention(self):
        self.prompt("original")
        for index in range(12):
            result = self.prompt("<system-reminder>notice %d</system-reminder>" % index, now=1100 + index)
        self.assertEqual(len(result["received"]), st.RECEIVED_INPUTS_LIMIT)
        self.assertEqual(result["human"]["text"], "original")
        for harness, sid in (("codex", "prompt-session"), ("claude", "other-session")):
            other = st.prompt_display(self.seat(harness, sid), harness, sid)
            self.assertIsNone(other["human"])
            self.assertEqual(other["received"], [])

    def test_cached_reader_reuses_unchanged_frontier_and_returns_owned_data(self):
        self.prompt("first")
        with mock.patch.object(prompt_inputs.os, "open", wraps=prompt_inputs.os.open) as opened:
            first = prompt_inputs.read("claude", "prompt-session")
            self.assertEqual(opened.call_count, 1)
            first["human"]["text"] = "not a cache edit"
            self.assertEqual(prompt_inputs.read("claude", "prompt-session")["human"]["text"], "first")
            self.assertEqual(opened.call_count, 1)
        self.prompt("second", now=1010)
        self.assertEqual(prompt_inputs.read("claude", "prompt-session")["human"]["text"], "second")

    def test_missing_corrupt_replaced_and_legacy_frontiers_stay_honest(self):
        self.assertIsNone(prompt_inputs.read("claude", "prompt-session"))
        self.prompt("first")
        path = st._prompt_seq_path(self.seat())
        value = st.read_json(path)
        value.pop("last_human_prompt")
        value["request_excerpt"] = "first"  # A legacy frontier that retained this exact quote.
        st.atomic_write_json(path, value)
        self.assertEqual(prompt_inputs.read("claude", "prompt-session")["human"]["text"], "first")
        value.pop("last_human_seq")  # The old frontier used seq before source-specific counters.
        value.pop("prior_human_seq")
        st.atomic_write_json(path, value)
        self.assertEqual(prompt_inputs.read("claude", "prompt-session")["human"]["text"], "first")
        value.pop("request_excerpt")
        st.atomic_write_json(path, value)
        self.assertEqual(prompt_inputs.read("claude", "prompt-session")["human_state"], "unknown")
        path.write_text("{broken")
        self.assertIsNone(prompt_inputs.read("claude", "prompt-session"))
        path.write_text("null")
        with mock.patch.object(prompt_inputs.os, "open", wraps=prompt_inputs.os.open) as opened:
            self.assertEqual(prompt_inputs.read("claude", "prompt-session")["human_state"], "unknown")
            self.assertEqual(opened.call_count, 1)
        path.unlink()
        path.symlink_to(self.cwd / "do-not-read")
        self.assertIsNone(prompt_inputs.read("claude", "prompt-session"))

    def test_a_rekeyed_frontier_does_not_borrow_another_native_sessions_payload(self):
        self.prompt("original")
        value = st.read_json(st._prompt_seq_path(self.seat()))
        value["sid"] = "other-session"
        value["request_excerpt"] = "not an exact quote for this identity"
        st.atomic_write_json(st._prompt_seq_path(self.seat(sid="other-session")), value)
        other = prompt_inputs.read("claude", "other-session")
        self.assertEqual(other["human_state"], "unknown")
        self.assertIsNone(other["human"])
        self.assertEqual(other["received"], [])


if __name__ == "__main__":
    unittest.main()
