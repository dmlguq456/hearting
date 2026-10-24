"""The explorer view model carries observations without action identities or hidden reads."""

import json
import unittest
from pathlib import Path

from tools.fleet.explorer.live import build_view_model
from tools.fleet.explorer.view_model import load_fixture
from tools.fleet.explorer.focus import SelectionReader
from tools.fleet.explorer.types import FocusKey, FocusRequest


FIXTURE = Path(__file__).parent / "fixtures" / "explorer" / "view_model.json"


class SourceBoundaryTests(unittest.TestCase):
    def test_unexpanded_focus_is_passed_without_hidden_session_reads(self):
        seen = []
        reader = SelectionReader(lambda request: seen.append(request.expanded_sessions) or {})
        reader.read(FocusRequest(FocusKey("claude", "s"), 1, 1, ()))
        self.assertEqual(seen, [()])

    def test_fixture_is_synthetic_and_contains_no_process_control_identity(self):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        serialized = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("/home/", serialized)
        self.assertNotIn("pid", serialized.lower())
        vm = load_fixture(FIXTURE)
        self.assertTrue(vm.sessions)
        self.assertTrue(all("pid" not in session for session in vm.sessions))

    def test_live_projection_keeps_unknown_fields_unknown_and_records_immutable(self):
        source = {"sessions": [{"session_tag": "s1", "harness": "claude", "context": None}],
                  "peer_edges": ()}
        vm = build_view_model(source, accepted_detail={"source_records": {"attempts": ("a",)}})
        self.assertIsNone(vm.sessions[0]["context"])
        self.assertEqual(vm.peer_edges, {})
        self.assertEqual(vm.source_records["attempts"], ("a",))
        with self.assertRaises(TypeError):
            vm.source_records["attempts"] = ()


if __name__ == "__main__":
    unittest.main()
