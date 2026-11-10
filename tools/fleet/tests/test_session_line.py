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


    def test_side_width_wraps_long_groups_without_losing_numeric_status_fields(self):
        from tools.fleet.session_line import status_lines
        from tools.fleet.explorer.grid import cell_width
        values=dict(harness='codex',tag='52',state='idle',cwd='/work/project',title='A long session title '*4,
                    native_sid='00000000-0000-4000-8000-000000000052',branch='topic-'*20,ctx_pct=42,
                    model='fixture',effort='high',quota_windows=[('5h',31,None),('7d',60,None)],
                    jobs='compile '*15,git=dict(repo=1,operation='none',ahead=3,behind=2,extra_worktrees=4,
                    observation='fresh',observed_label='12:34:56'))
        lines=status_lines(values,36,wrap_long=True)
        self.assertTrue(all(cell_width(''.join(text for text,_ in line))<=36 for line in lines))
        compact=''.join(text for line in lines for text,_ in line).replace(' ','')
        for field in ('↑3','↓2','🚧4','12:34:56','42%','5h31%','7d60%',values['native_sid']):
            self.assertIn(field,compact)

if __name__ == "__main__": unittest.main()
