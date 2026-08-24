import unittest
from types import SimpleNamespace

from tools.fleet import render
from tools.fleet.session_handle import clip_cells, sanitize_title, session_display_name, session_handle


class SessionHandleTest(unittest.TestCase):
    def test_allowlist_and_opaque_sid8(self):
        self.assertEqual(session_handle("claude", "abcdefgh-123"), "CL/abcdefgh")
        self.assertEqual(session_handle("codex", "abcdefgh-123"), "CX/abcdefgh")
        self.assertEqual(session_handle("opencode", "abcdefgh-123"), "OC/abcdefgh")
        self.assertEqual(session_handle("other", "abcdefgh"), "")
        self.assertEqual(session_handle("codex", ""), "")

    def test_sanitize_and_cell_clipping(self):
        self.assertEqual(sanitize_title("  hello\nworld\x00 "), "hello?world?")
        self.assertEqual(clip_cells("가나다", 4), "가…")
        self.assertEqual(session_display_name("codex", "abcdefgh-123", "a very long title", 12), "CX/abcdefgh")
        self.assertEqual(session_display_name("codex", None, None, fallback="legacy"), "legacy")

    def test_title_budget_drops_optional_segment_at_exact_boundary(self):
        self.assertEqual(session_display_name("codex", "abcdefgh-123", "title", 17), "CX/abcdefgh · t…")
        self.assertEqual(session_display_name("codex", "abcdefgh-123", "title", 16), "CX/abcdefgh")
        self.assertEqual(session_display_name("codex", "abcdefgh-123", "title", 11), "CX/abcdefgh")
        self.assertEqual(session_display_name("codex", "abcdefgh-123", "title", 10), "CX/abcdef…")

    def test_exact_gpu_lookup_keeps_same_sid8_titles_separate(self):
        sessions = [
            SimpleNamespace(harness="codex", session_id="abcdefgh-one", title="first"),
            SimpleNamespace(harness="codex", session_id="abcdefgh-two", title="second"),
        ]
        gpu = {"index": 0, "processes": [{"owner": {"kind": "session", "harness": "codex", "id": "abcdefgh-two"}}]}
        text = render._plain(render._gpu_token(gpu, 100, sessions=sessions))
        self.assertIn("CX/abcdefgh · second", text)
        self.assertNotIn("first", text)

    def test_json_owner_payload_is_not_rewritten(self):
        owner = {"kind": "session", "harness": "codex", "id": "abcdefgh-one", "label": "old"}
        gpu = {"index": 0, "processes": [{"owner": owner}]}
        render._gpu_token(gpu, 100)
        self.assertEqual(owner, {"kind": "session", "harness": "codex", "id": "abcdefgh-one", "label": "old"})

    def test_gpu_mixed_owner_set_keeps_titles_and_single_arrow(self):
        sessions = [SimpleNamespace(harness="codex", session_id="abcdefgh-one", title="train")]
        gpu = {"index": 0, "processes": [
            {"owner": {"kind": "session", "harness": "codex", "id": "abcdefgh-one"}},
            {"owner": {"kind": "job", "label": "job:train"}},
            {"owner": {"kind": "run", "label": "run:eval"}},
            {"owner": {"kind": "unattributed", "label": "unattributed:worker"}},
        ]}
        text = render._plain(render._gpu_token(gpu, 160, show_name=True, sessions=sessions))
        self.assertIn("↳ session CX/abcdefgh · train", text)
        self.assertIn("job:train", text)
        self.assertIn("+2", text)
        self.assertEqual(text.count("↳"), 1)

    def test_gpu_unknown_harness_keeps_safe_owner_fallback(self):
        gpu = {"index": 0, "processes": [{"owner": {
            "kind": "session", "harness": "unknown", "id": "abcdefgh-123",
        }}]}
        text = render._plain(render._gpu_token(gpu, 120))
        self.assertIn("session unknown/abcdefgh", text)
        self.assertNotIn("session ", text.replace("session unknown/abcdefgh", ""))


if __name__ == "__main__":
    unittest.main()
