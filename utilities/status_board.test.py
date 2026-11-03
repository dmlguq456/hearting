#!/usr/bin/env python3
"""A route's close records itself on its campaign's status page, and touches nothing else."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import status_board as SB  # noqa: E402

PAGE = """# 데모 현황

갱신 2026-11-01 09:00 · 계획은 PLAN.md

**한 줄**: 두 번째 단계를 고치는 중.

## 결정 필요
- 배포 시점

## 다음
- 시험 다시 돌리기

## 사용자 결정 기록
- 11-01 범위 확정
"""


class StatusBoardTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / ".agent_reports"
        self.campaign = self.root / "campaigns" / "2026-11-01_demo"
        (self.campaign / "2026-11-01_first" / "artifacts").mkdir(parents=True)
        (self.campaign / "campaign.json").write_text(json.dumps({"key": "demo", "state": "active"}))
        self.page = self.campaign / "2026-11-01_first" / "artifacts" / "현황.md"
        self.route = {"route_id": "rt-1111111111111111", "artifact_root": str(self.root),
                      "campaign_key": "demo", "slug": "fix-step-two", "capability": "autopilot-code"}

    def close(self, minute, route=None, summary="done https://github.com/o/r/pull/7", proven=True):
        return SB.note_route_closed(route or self.route, {"summary": summary, "terminal_gate_proven": proven},
                                    now=datetime(2026, 11, 4, 10, minute))

    def test_a_close_sets_the_stamp_and_adds_one_line_and_keeps_the_persons_parts(self):
        self.page.write_text(PAGE, encoding="utf-8")
        self.assertEqual(self.close(5), self.page)
        text = self.page.read_text(encoding="utf-8")
        self.assertIn("갱신 2026-11-04 10:05 · 계획은 PLAN.md", text)
        for kept in ("**한 줄**: 두 번째 단계를 고치는 중.", "## 결정 필요\n- 배포 시점",
                     "## 다음\n- 시험 다시 돌리기", "## 사용자 결정 기록\n- 11-01 범위 확정"):
            self.assertIn(kept, text)
        self.assertTrue(text.endswith(
            "## 최근 마감\n- 2026-11-04 10:05 · fix-step-two · route rt-1111111111111111"
            " · https://github.com/o/r/pull/7\n"))

    def test_newest_first_at_most_five_and_english_pages_read_english(self):
        self.page.write_text(PAGE, encoding="utf-8")
        for minute in range(7):
            self.close(minute, route={**self.route, "route_id": f"rt-{minute:016d}"}, summary="")
        section = self.page.read_text(encoding="utf-8").split("## 최근 마감\n", 1)[1].strip().splitlines()
        self.assertEqual(len(section), SB.KEEP)
        self.assertIn("rt-0000000000000006", section[0])
        self.assertIn("rt-0000000000000002", section[-1])
        english = self.page.with_name("STATUS.md")
        self.page.unlink()
        english.write_text("# Demo status\n\nUpdated 2026-11-01 09:00\n\n## Next\n- ship\n", encoding="utf-8")
        self.assertEqual(self.close(9), english)
        text = english.read_text(encoding="utf-8")
        self.assertIn("Updated 2026-11-04 10:09\n", text)
        self.assertIn("## Next\n- ship\n\n## Recently closed\n- 2026-11-04 10:09 · ", text)

    def test_nothing_is_created_and_odd_pages_or_campaigns_are_left_alone(self):
        self.assertIsNone(self.close(1))
        self.assertEqual(list((self.campaign / "2026-11-01_first" / "artifacts").iterdir()), [])
        # A page at the cycle root is not the convention's page.
        (self.campaign / "2026-11-01_first" / "현황.md").write_text(PAGE, encoding="utf-8")
        self.assertIsNone(self.close(2))
        # A symlinked page may point anywhere: untouched.
        outside = Path(self.root).parent / "elsewhere.md"
        outside.write_text(PAGE, encoding="utf-8")
        os.symlink(outside, self.page)
        self.assertIsNone(self.close(3))
        self.assertEqual(outside.read_text(encoding="utf-8"), PAGE)
        self.page.unlink()
        self.page.write_text(PAGE, encoding="utf-8")
        for route in ({**self.route, "campaign_key": None}, {**self.route, "campaign_key": "other"},
                      {**self.route, "campaign_key": "../demo"}):
            self.assertIsNone(self.close(4, route=route))
        self.assertIsNone(self.close(4, proven=False))          # a cancelled close is not finished work
        (self.campaign / "campaign.json").write_text(json.dumps({"key": "demo", "state": "satisfied"}))
        self.assertIsNone(self.close(5))
        self.assertEqual(self.page.read_text(encoding="utf-8"), PAGE)

    def test_the_newest_page_of_the_campaign_is_the_one_refreshed(self):
        older = self.page
        older.write_text(PAGE, encoding="utf-8")
        os.utime(older, (1, 1))
        newer = self.campaign / "2026-11-03_second" / "artifacts" / "현황.md"
        newer.parent.mkdir(parents=True)
        newer.write_text(PAGE, encoding="utf-8")
        self.assertEqual(self.close(6), newer)
        self.assertEqual(older.read_text(encoding="utf-8"), PAGE)


if __name__ == "__main__":
    unittest.main()
