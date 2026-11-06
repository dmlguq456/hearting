"""The collapsed human input and expanded received inputs have separate sources."""
import os
import unittest
from unittest import mock

from tools.fleet.explorer import live
from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState
from tools.fleet.model import Session


class OverviewLastInputTests(unittest.TestCase):
    def snapshot(self, harness="claude", text="유휴 기준이 너무 짧다", now=1000):
        session = Session(harness=harness, pid=123, cwd="/fixture/repo", session_id="exact-id",
                          session_tag="b6", liveness="working", title="v4 단장",
                          summary="Bash · current work", process_role="conversation")
        session._prompt_display = {"human_state": "known",
            "human": {"sequence": 1, "at": now - 60, "text": text},
            "received": [{"source": "peer", "sender": "Codex [73]", "at": now - 10,
                          "text": "계획 2라운드 검토"}]}
        return {"sessions": [session], "jobs": [], "resources": []}

    def model(self, snapshot):
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "ko"}):
            return live.build_view_model(snapshot, now=1000)

    def test_each_harness_has_same_human_and_received_projection(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                row = self.model(self.snapshot(harness)).sessions[0]
                self.assertEqual(row["last_input"], "› 나 1분 전: 유휴 기준이 너무 짧다")
                self.assertEqual(row["now"], "Bash · current work")
                self.assertIn("⇄ [73]", row["received_inputs"][0])
                self.assertTrue(row["received_inputs"][0].endswith("계획 2라운드 검토"))
                self.assertNotIn("유휴 기준", row["received_inputs"][0])

    def test_absent_and_unavailable_inputs_are_distinct(self):
        snapshot = self.snapshot()
        snapshot["sessions"][0]._prompt_display = {"human": None, "human_state": "none", "received": []}
        self.assertEqual(self.model(snapshot).sessions[0]["last_input"], "› 나 없음")
        snapshot["sessions"][0]._prompt_display = None
        self.assertEqual(self.model(snapshot).sessions[0]["last_input"], "› 나 미확인")

    def test_received_sender_uses_the_exact_native_tag_instead_of_the_saved_name(self):
        snapshot = self.snapshot()
        snapshot["sessions"].append(Session(harness="codex", pid=456, cwd="/fixture/repo",
            session_id="sender-native", session_tag="52", liveness="idle", process_role="conversation"))
        snapshot["sessions"][0]._prompt_display["received"][0].update(
            sender="name-without-a-tag", sender_harness="codex", sender_session_id="sender-native")
        row = next(row for row in self.model(snapshot).sessions if row["tag"] == "b6")
        self.assertTrue(row["received_inputs"][0].startswith("⇄ [52] "))

    def test_long_input_changes_only_its_cells_even_when_the_title_is_clipped(self):
        for width in (140, 168, 274):
            with self.subTest(width=width), mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "ko"}):
                snapshot = self.snapshot(text="짧은 입력")
                snapshot["sessions"][0].title = "제목이 잘리는 긴 작업 " * 30
                short = draw(self.model(snapshot), NavState(), width, 45)
                snapshot["sessions"][0]._prompt_display["human"]["text"] = "긴 입력 " * 100
                long = draw(self.model(snapshot), NavState(), width, 45)
                y = next(y for y, row in enumerate(short.cells) if "[b6]" in "".join(c[0] for c in row))
                marker = next((x for x, cell in enumerate(short.cells[y]) if cell[0] == "›"), None)
                if marker is None:  # The narrow layout omits the whole input cell.
                    self.assertEqual(short.cells, long.cells)
                    continue
                changed = [(cy, cx) for cy, row in enumerate(short.cells)
                           for cx, cell in enumerate(row) if cell != long.cells[cy][cx]]
                self.assertTrue(changed)
                self.assertTrue(all(cy == y and marker <= cx < width - 8 for cy, cx in changed))
                self.assertEqual(short.cells[y][:marker], long.cells[y][:marker])
                self.assertEqual(short.cells[y][-8:], long.cells[y][-8:])

    def test_folded_input_and_unfolded_now_and_messages_at_narrow_and_wide_widths(self):
        snapshot = self.snapshot(text="내 마지막 입력 " + "긴글" * 80)
        vm = self.model(snapshot)
        for width in (100, 140, 168, 274, 410):
            with self.subTest(width=width), mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "ko"}):
                folded = draw(vm, NavState(view="OVERVIEW", title="OVERVIEW"), width, 45)
                expanded = draw(vm, NavState(view="OVERVIEW", title="OVERVIEW", opened="b6"), width, 45)
                folded_text = "\n".join("".join(cell[0] for cell in row) for row in folded.cells)
                expanded_text = "\n".join("".join(cell[0] for cell in row) for row in expanded.cells)
                if width >= 140:
                    self.assertIn("› 나", folded_text)
                self.assertNotIn("Bash · current work", folded_text)
                self.assertIn("Bash · current work", expanded_text)
                self.assertIn("받은 메시지", expanded_text)
                self.assertIn("계획 2라운드", expanded_text)
                self.assertTrue(all(len(row) == width for row in folded.cells))


if __name__ == "__main__":
    unittest.main()
