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
        # Spec 2026-11-11 rule 10: [number] project (git) │ model effort │ context │ wait │ route │ quota.
        fields = ("[52] hearting", "(feature)", "sol xhigh", "context ━━━━────── 37%", "◑답 대기 3", "⏳ route 2", "7d 60%")
        positions = [text.index(field) for field in fields]
        self.assertEqual(positions, sorted(positions))
        self.assertIn(" │ ", text); self.assertNotIn(" · ", text.replace("⏳ route 2 · impl-review", ""))
        self.assertNotIn("📁", text); self.assertNotIn("⎇", text); self.assertNotIn("Synthetic session", text)
        self.assertNotIn("› 나", text)

    def test_narrow_cjk_names_leave_percentages_and_action_counts_intact(self):
        sample = {**self.sample(), "cwd": "/synthetic/" + "긴폴더" * 30,
                  "title": "아주긴제목" * 30, "branch": "긴브랜치" * 30}
        for width in (80, 100, 140):
            with self.subTest(width=width):
                text = "".join(part for part, _key in line_segments(sample, width))
                self.assertLessEqual(cell_width(text), width)
                for value in ("37%", "7d 60%", "⏳ route 2"):
                    self.assertIn(value, text)
                self.assertTrue("◑답 대기 3" in text or "◑ 3" in text, text)

    def test_unknown_is_not_zero_and_known_zero_stays_zero(self):
        sample = {**self.sample(), "ctx_pct": None, "weekly_pct": None, "answer_wait": None}
        text = "".join(part for part, _key in line_segments(sample, 240))
        self.assertIn("context ?", text); self.assertIn("7d ?", text); self.assertIn("◑답 대기 ?", text)
        known = {**sample, "ctx_pct": 0, "weekly_pct": 0, "answer_wait": 0}
        text = "".join(part for part, _key in line_segments(known, 240))
        self.assertIn("context ────────── 0%", text); self.assertIn("7d 0%", text); self.assertNotIn("◑", text)

    def test_claude_native_row_leaves_out_readings_not_reported_yet(self):
        sample = {**self.sample(), "ctx_pct": None, "weekly_pct": None,
                  "quota_windows": [("5h", 12, None), ("7d", None, None)]}
        text = "".join(part for part, _key in line_segments(sample, 240, reported_only=True))
        self.assertNotIn("context", text); self.assertNotIn("7d", text); self.assertIn("5h 12%", text)
        known = {**sample, "ctx_pct": 0}
        text = "".join(part for part, _key in line_segments(known, 240, reported_only=True))
        self.assertIn("context ────────── 0%", text)

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
        for field in ('↑3','↓2','🚧4','42%','5h31%','7d60%',values['native_sid']):
            self.assertIn(field,compact)
        # The observation clock is not painted (spec 2026-11-11 section 3).
        self.assertNotIn('12:34:56',compact)

    def test_route_rows_take_their_values_from_the_same_snapshot_jobs(self):
        from types import SimpleNamespace
        from unittest import mock
        from tools.fleet import session_line
        from tools.fleet.explorer import live
        job = lambda **kw: SimpleNamespace(key=kw.get("attempt_id"), **kw)
        jobs = {
            "rt-a": [job(attempt_id="o", depth=1, worker_type="owner", liveness="idle", harness="claude", model="opus",
                         elapsed_min=87.0),
                     job(attempt_id="w", depth=2, route_node="execute", liveness="working", harness="codex",
                         resolved_model="gpt-fixture", effort="high", active_context_tokens=72_000,
                         context_window_tokens=100_000, exec_tool={"name": "apply_patch"},
                         subagents=[{"active": True}, {"active": False}], elapsed_min=42.0)],
            # A Claude headless worker reports tokens but no window: no percent is made up.
            "rt-b": [job(attempt_id="o2", depth=1, worker_type="owner", route_node="one-shot", liveness="working",
                         harness="claude", model="opus", effort="medium", active_context_tokens=250_136,
                         exec_tool={"name": "Bash"}, elapsed_min=5.0)],
            "rt-c": [job(attempt_id="o3", depth=1, worker_type="owner", liveness="dead", harness="claude",
                         ctx_pct=12, elapsed_min=54.0)],
        }
        collected = {rid: {"jobs": rows} for rid, rows in jobs.items()}
        panels = {"claude\0s": [{"id": "rt-a", "state": "working", "worker_stage": "execute"},
                                {"id": "rt-b", "state": "working", "worker_stage": "one-shot"},
                                {"id": "rt-c", "state": "dead", "worker_stage": "execute"}]}
        with mock.patch.object(live, "_collect_routes", return_value=collected):
            session_line.route_details(object(), panels, now=10_000.0)
        a, b, c = panels["claude\0s"]
        self.assertEqual((a["harness"], a["model"], a["effort"], a["ctx_pct"], a["tool"], a["agents"]),
                         ("codex", "gpt-fixture", "high", 72.0, "apply_patch", 1))
        self.assertEqual(a["since"], 10_000.0 - 87 * 60)   # absolute: a re-published pass still ages
        self.assertNotIn("ctx_pct", b); self.assertEqual(b["tool"], "Bash")
        self.assertNotIn("ctx_pct", c); self.assertNotIn("tool", c); self.assertEqual(c["elapsed_min"], 54.0)
        self.assertEqual(session_line.route_badge("⏳ route 3 · execute", panels["claude\0s"]), "⏳ route 3 ⠏2 ✕1")
        # Without its rows the count stays alone; a missing label stays empty.
        self.assertEqual(session_line.route_badge("⏳ route 3 · execute", None), "⏳ route 3")
        self.assertEqual(session_line.route_badge("", panels["claude\0s"]), "")

    def test_messages_are_the_latest_three_peer_rows_either_way(self):
        from tools.fleet.session_line import peer_message_rows
        received = [{"source": "peer", "sender": "[e7] x", "sender_harness": "claude", "sender_session_id": "e7",
                     "text": "first", "at": 1},
                    {"source": "notification", "text": "not a peer message", "at": 2},
                    {"source": "peer", "sender": "someone", "text": "third", "at": 4}]
        sent = [{"to": {"harness": "codex", "session_id": "s73", "name": "x"}, "summary": "second", "at": 3},
                {"to": {"name": "named"}, "summary": "fourth", "at": 5}]
        tags = {("claude", "e7"): "e7", ("codex", "s73"): "73"}
        rows = peer_message_rows(received, sent, lambda h, s: tags.get((h, s)))
        self.assertEqual([(r["way"], r["who"], r["text"]) for r in rows],
                         [("→", "[73]", "second"), ("←", "someone", "third"), ("→", "named", "fourth")])

if __name__ == "__main__": unittest.main()
