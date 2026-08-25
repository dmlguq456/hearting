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
        self.assertEqual(sanitize_title("  hello\nworld\x00 "), "hello world")
        self.assertEqual(clip_cells("가나다", 4), "가…")
        self.assertEqual(session_display_name("codex", "abcdefgh-123", "a very long title", 12), "CX/abcdefgh")
        self.assertEqual(session_display_name("codex", None, None, fallback="legacy"), "legacy")

    def test_title_budget_drops_optional_segment_at_exact_boundary(self):
        self.assertEqual(session_display_name("codex", "abcdefgh-123", "title", 17), "CX/abcdefgh · t…")
        self.assertEqual(session_display_name("codex", "abcdefgh-123", "title", 16), "CX/abcdefgh")
        self.assertEqual(session_display_name("codex", "abcdefgh-123", "title", 11), "CX/abcdefgh")
        self.assertEqual(session_display_name("codex", "abcdefgh-123", "title", 10), "CX/abcdef…")

    def test_exact_gpu_lookup_keeps_same_sid8_sessions_separate(self):
        sessions = [
            SimpleNamespace(harness="codex", session_id="abcdefgh-one", title="first"),
            SimpleNamespace(harness="codex", session_id="abcdefgh-two", title="second"),
        ]
        snapshot = {"hosts": [{"host": "cnn", "gpus": [{"index": 0, "processes": [{
            "session_owner": {"kind": "session", "harness": "codex", "id": "abcdefgh-two"},
        }]}]}]}
        resources = render._gpu_session_resources(snapshot)
        self.assertEqual(render._gpu_resources_for_session(sessions[0], resources), [])
        self.assertEqual(render._gpu_resources_for_session(sessions[1], resources)[0]["host"], "cnn")

    def test_fleet_session_name_uses_summary_without_display_id(self):
        session = SimpleNamespace(harness="codex", session_id="abcdefgh-one",
                                  title="session summary", registry_name="registry",
                                  slug="slug", cwd="/work/repo")
        self.assertEqual(render._session_name(session), "session summary")
        session.title = None
        self.assertEqual(render._session_name(session), "registry")
        self.assertNotIn("CX/", render._session_name(session))

    def test_json_owner_payload_is_not_rewritten(self):
        owner = {"kind": "session", "harness": "codex", "id": "abcdefgh-one", "label": "old"}
        gpu = {"index": 0, "processes": [{"owner": owner}]}
        render._gpu_token(gpu, 100)
        self.assertEqual(owner, {"kind": "session", "harness": "codex", "id": "abcdefgh-one", "label": "old"})

    def test_gpu_mixed_primary_owners_never_return_to_the_upper_row(self):
        sessions = [SimpleNamespace(harness="codex", session_id="abcdefgh-one", title="train")]
        gpu = {"index": 0, "processes": [
            {"owner": {"kind": "session", "harness": "codex", "id": "abcdefgh-one"},
             "session_owner": {"kind": "session", "harness": "codex", "id": "abcdefgh-one"}},
            {"owner": {"kind": "job", "label": "job:train"}},
            {"owner": {"kind": "run", "label": "run:eval"}},
            {"owner": {"kind": "unattributed", "label": "unattributed:worker"}},
        ]}
        text = render._plain(render._gpu_token(gpu, 160, show_name=True, sessions=sessions))
        for owner in ("CX/abcdefgh", "job:train", "run:eval", "unattributed:", "↳"):
            self.assertNotIn(owner, text)

    def test_gpu_unknown_harness_cannot_create_a_resource_relation(self):
        gpu = {"index": 0, "processes": [{"owner": {
            "kind": "session", "harness": "unknown", "id": "abcdefgh-123",
        }, "session_owner": {
            "kind": "session", "harness": "unknown", "id": "abcdefgh-123",
        }}]}
        text = render._plain(render._gpu_token(gpu, 120))
        self.assertNotIn("session unknown/abcdefgh", text)
        self.assertEqual(render._gpu_session_resources({
            "hosts": [{"host": "cnn", "gpus": [gpu]}],
        }), {})


if __name__ == "__main__":
    unittest.main()
