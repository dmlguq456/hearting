import datetime
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import render
from fleet.collectors import memory
from fleet.model import DispatchJob, Session


def _text(lines):
    return "\n".join("".join(part for part, _key in line) for line in lines if line)


class F63VisibilityTest(unittest.TestCase):
    def tearDown(self):
        render.set_show_all(False)

    def test_mem_worker_is_visible_only_in_system_group_by_default(self):
        worker = Session(harness="codex", pid=2, cwd="/work/project", session_id="mem",
                         slug="curator", mem_worker=True, liveness="idle")
        normal = Session(harness="codex", pid=1, cwd="/work/project", session_id="main",
                         slug="main", liveness="idle")
        text = _text(render._build_lines([normal, worker], [], "fleet", False, 0,
                                         layout="wide", term_width=140))
        self.assertIn("⚙ system", text)
        self.assertEqual(text.count("curator"), 1)
        self.assertLess(text.index("⚙ system"), text.index("project/"))
        empty = _text(render._build_lines([normal], [], "fleet", False, 0,
                                          layout="wide", term_width=140))
        self.assertNotIn("⚙ system", empty)

    def test_periodic_curator_parser_and_system_progress_row(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "memory"
            store.mkdir()
            lock = store / ".distill-lock-periodic-night"
            lock.mkdir()
            os.utime(lock, (1000, 1000))
            log = Path(td) / "periodic.log"
            log.write_text(
                "=== 2026-08-13 night ===\n"
                "select origin=a active=2 cwd=/work/alpha\n"
                "select origin=b active=1 cwd=/work/beta\n"
                "project=/work/alpha elapsed=40s status=ok\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"MEM_STORE": str(store),
                                               "FLEET_PERIODIC_CURATE_LOG": str(log)}):
                snap = memory.collect(now=datetime.datetime.fromtimestamp(1120))
            self.assertEqual(snap["periodic_curate"]["current_cwd"], "/work/beta")
            worker = Session(harness="codex", pid=2, cwd="/work/alpha", session_id="mem",
                             slug="curator", mem_worker=True, liveness="idle")
            text = _text(render._build_lines([worker], [], "fleet", False, 0,
                                             layout="wide", memory=snap, term_width=140))
            self.assertIn("야간 큐레이터 2/2 beta 2m", text)

    def test_dispatch_tree_uses_nested_card_frame_without_depth_tokens(self):
        parent = Session(harness="codex", pid=1, cwd="/work/repo", session_id="p",
                         slug="main", liveness="working")
        owner = DispatchJob(key="code", slug="owner", cwd="/work/repo", parent_sid="p",
                            is_child=True, depth=1, harness="codex", liveness="working")
        child = DispatchJob(key="code", slug="child", cwd="/work/repo", parent_slug="owner",
                            depth=2, harness="codex", liveness="working")
        text = _text(render._build_lines([parent], [owner, child], "both", False, 0,
                                         layout="wide", term_width=160))
        self.assertIn("╭─", text)
        self.assertIn("│ ", text)
        self.assertIn("╰───", text)
        owner_block = text[text.index("╭─"):text.index("╰───")]
        self.assertNotIn("↳", owner_block)
        self.assertNotIn("d1", text)
        self.assertNotIn("d2", text)

    def test_dispatch_model_name_map_preserves_unknown(self):
        self.assertEqual(render._dispatch_display_model("opus"), "Opus 5")
        self.assertEqual(render._dispatch_display_model("claude-haiku-4-5-20251001"), "Haiku 4.5")
        self.assertEqual(render._dispatch_display_model("glm-5.2"), "glm-5.2")

    def test_idle_session_exec_child_is_promoted_in_status_detail_only(self):
        session = Session(harness="codex", pid=1, cwd="/work/repo", session_id="s",
                          slug="experiment", title="calibration", liveness="idle",
                          exec_child={"comm": "python", "etime_s": 11520})
        segs = render._exec_detail_segs(session)
        self.assertEqual(segs, [("⚙ python 3h 12m", "g_work")])
        self.assertNotIn("python", render._session_name(session))


if __name__ == "__main__":
    unittest.main()
