"""The common line keeps the approved order and action counts at narrow widths."""
import unittest
from tools.fleet.session_line import line_segments
from tools.fleet.explorer.grid import cell_width
from tools.fleet.palette import style_components


class CommonSessionLineTest(unittest.TestCase):
    def sample(self):
        return {"harness": "codex", "tag": "52", "state": "idle", "cwd": "/synthetic/hearting",
                "title": "Synthetic session", "branch": "feature", "ctx_pct": 37,
                "model": "sol", "effort": "xhigh", "weekly_pct": 60,
                "route_label": "⏳ route 2 · impl-review, plan-check", "answer_wait": 3}

    def test_wide_line_uses_the_approved_order_and_weekly_quota(self):
        text = "".join(part for part, _key in line_segments(self.sample(), 240))
        fields = ("hearting", "Synthetic session", "feature", "context ━━━━────── 37%", "sol", "7d 60%", "⏳ route 2", "◑ 3")
        positions = [text.index(field) for field in fields]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("› 나", text)

    def test_narrow_cjk_names_leave_percentages_and_action_counts_intact(self):
        sample = {**self.sample(), "cwd": "/synthetic/" + "긴폴더" * 30,
                  "title": "아주긴제목" * 30, "branch": "긴브랜치" * 30}
        for width in (80, 100, 140):
            with self.subTest(width=width):
                text = "".join(part for part, _key in line_segments(sample, width))
                self.assertLessEqual(cell_width(text), width)
                for value in ("37%", "7d 60%", "⏳ route 2", "◑ 3"):
                    self.assertIn(value, text)

    def test_unknown_is_not_zero_and_known_zero_stays_zero(self):
        sample = {**self.sample(), "ctx_pct": None, "weekly_pct": None, "answer_wait": None}
        text = "".join(part for part, _key in line_segments(sample, 240))
        self.assertIn("context ?", text); self.assertIn("7d ?", text); self.assertIn("◑ ?", text)
        known = {**sample, "ctx_pct": 0, "weekly_pct": 0, "answer_wait": 0}
        text = "".join(part for part, _key in line_segments(known, 240))
        self.assertIn("context ────────── 0%", text); self.assertIn("7d 0%", text); self.assertNotIn("◑", text)

    def test_fleet_model_effort_and_state_keys_reach_the_terminal_palette(self):
        segments = line_segments(self.sample(), 240)
        keys = {key for _text, key in segments}
        self.assertIn("fam_sol", keys); self.assertIn("eff_xhigh", keys)
        self.assertEqual(style_components("fam_sol")["foreground"], 186)
        self.assertEqual(style_components("fam_sol", colors=8)["foreground"], 3)
        self.assertIsNone(style_components("fam_sol", no_color=True)["foreground"])


if __name__ == "__main__": unittest.main()
