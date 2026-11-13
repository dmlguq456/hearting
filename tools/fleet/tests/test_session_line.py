"""The common line keeps the approved order and action counts at narrow widths."""
import unittest
from tools.fleet.session_line import line_segments
from tools.fleet.explorer.grid import cell_width
from tools.fleet.palette import style_components


class CommonSessionLineTest(unittest.TestCase):
    def setUp(self):
        # Korean screen words for the fixed assertions; the English form is checked below.
        from unittest import mock
        patch = mock.patch.dict("os.environ", {"FLEET_NOW_LANG": "Korean"})
        patch.start(); self.addCleanup(patch.stop)

    def test_answer_wait_words_follow_the_screen_language(self):
        from unittest import mock
        for language, words in (("Korean", "◑답 대기 3"), ("English", "◑ to answer 3")):
            with mock.patch.dict("os.environ", {"FLEET_NOW_LANG": language}):
                text = "".join(part for part, _key in line_segments(self.sample(), 240))
                self.assertIn(words, text)
                self.assertIn("context ", text); self.assertIn("route 2", text)

    def test_time_left_and_elapsed_use_fleets_spaced_form(self):
        import time
        from tools.fleet.session_line import _remaining, elapsed_text
        self.assertEqual(elapsed_text(45), "45m"); self.assertEqual(elapsed_text(200), "3h 20m")
        self.assertEqual(elapsed_text(3 * 1440), "3d 0h")
        self.assertEqual(_remaining(time.time() + 200 * 60 + 30), "3h 20m")
        self.assertEqual(_remaining(time.time() - 5), "")
    def sample(self):
        return {"harness": "codex", "tag": "52", "state": "idle", "cwd": "/synthetic/hearting",
                "title": "Synthetic session", "branch": "feature", "ctx_pct": 37,
                "model": "sol", "effort": "xhigh", "weekly_pct": 60,
                "route_label": "route 2 · impl-review, plan-check", "answer_wait": 3}

    def test_wide_line_uses_the_approved_order_and_weekly_quota(self):
        text = "".join(part for part, _key in line_segments(self.sample(), 240))
        # Spec 2026-11-11 rule 10: [number] project (git) │ model effort │ context │ wait │ route │ quota.
        fields = ("[52] hearting", "(feature)", "sol xhigh", "context ━━━━────── 37%", "◑답 대기 3", "route 2", "7d 60%")
        positions = [text.index(field) for field in fields]
        self.assertEqual(positions, sorted(positions))
        self.assertIn(" │ ", text); self.assertNotIn(" · ", text.replace("route 2 · impl-review", ""))
        self.assertNotIn("📁", text); self.assertNotIn("⎇", text); self.assertNotIn("Synthetic session", text)
        self.assertNotIn("› 나", text)

    def test_narrow_cjk_names_leave_percentages_and_action_counts_intact(self):
        sample = {**self.sample(), "cwd": "/synthetic/" + "긴폴더" * 30,
                  "title": "아주긴제목" * 30, "branch": "긴브랜치" * 30}
        for width in (80, 100, 140):
            with self.subTest(width=width):
                text = "".join(part for part, _key in line_segments(sample, width))
                self.assertLessEqual(cell_width(text), width)
                for value in ("37%", "7d 60%", "route 2"):
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
                         exec_tool={"name": "python3", "target": "tools/run-tests.py"},
                         last_tool={"name": "Edit", "target": "utilities/x.py"}, elapsed_min=5.0)],
            "rt-c": [job(attempt_id="o3", depth=1, worker_type="owner", liveness="dead", harness="claude",
                         ctx_pct=12, elapsed_min=54.0)],
        }
        jobs["rt-d"] = [job(attempt_id="o4", depth=1, worker_type="owner", liveness="dead", harness="codex",
                            model="gpt-fixture", effort="high", ctx_pct=40, elapsed_min=30.0)]
        collected = {rid: {"jobs": rows} for rid, rows in jobs.items()}
        # Fleet's own route records (``_route_records``), as the producer observes them.
        closed = {"codes": {"schema": "route_visibility_codes_v1", "verification": {"closed_at": "2026-11-11T00:00:00Z"}}}
        routes = {"rt-a": {"id": "rt-a", "state": "working", "activity": "running", "current": True,
                           "worker_stage": "execute"},
                  "rt-b": {"id": "rt-b", "state": "working", "activity": "running", "current": True, "worker_stage": "one-shot"},
                  "rt-c": {"id": "rt-c", "state": "dead", "activity": "ended", "current": True, "worker_stage": "execute"},
                  "rt-d": {"id": "rt-d", "state": "dead", "activity": "ended", "current": True, "visibility": closed}}
        panels = {"claude\0s": [{"id": rid, "state": route["state"], "worker_stage": route.get("worker_stage")}
                                for rid, route in routes.items()]}
        with mock.patch.object(live, "_collect_routes", return_value=collected):
            session_line.route_details(object(), panels, routes, now=10_000.0)
        a, b, c, d = panels["claude\0s"]
        # MODEL and CONTEXT are the working worker's, else the owner's (Fleet's choice).
        # The percent is Fleet's own CONTEXT gauge for that job (the same number Fleet draws).
        fleet_percent = live._context_gauge(jobs["rt-a"][1])[0]
        self.assertIsNotNone(fleet_percent)
        self.assertEqual((a["harness"], a["model"], a["effort"], a["ctx_pct"], a["tool"], a["agents"]),
                         ("codex", "gpt-fixture", "high", round(fleet_percent, 1), "apply_patch", 1))
        self.assertEqual(a["since"], 10_000.0 - 87 * 60)   # absolute: a re-published pass still ages
        # No window known: the exact token count, never a made-up percent.
        self.assertNotIn("ctx_pct", b); self.assertEqual(b["ctx_tokens"], 250_136); self.assertEqual(b["tool"], "python3")
        # The doer's own now and last line reach the panel row and its painted values.
        self.assertEqual((b["target"], b["after_tool"]), ("tools/run-tests.py", "Edit · utilities/x.py"))
        from tools.fleet.session_panel import route_row
        painted = route_row(b, now=10_000.0)
        self.assertEqual((painted["tool"], painted["target"], painted["after_tool"]),
                         ("python3", "tools/run-tests.py", "Edit · utilities/x.py"))
        self.assertNotIn("after_tool", c); self.assertNotIn("idle_tool", c)
        self.assertNotIn("ctx_pct", c); self.assertNotIn("tool", c); self.assertEqual(c["elapsed_min"], 54.0)
        # Spec rule 27: an owner waiting on its running worker is a running route.
        self.assertEqual([row["marker"] for row in (a, b, c, d)], ["working", "working", "dead", "dead"])
        self.assertTrue(d["closed"])
        # With nothing running, the owner's recorded harness, model and effort stay; no context.
        self.assertEqual((d["harness"], d.get("model"), d.get("effort")), ("codex", "gpt-fixture", "high"))
        self.assertNotIn("ctx_pct", d)
        # Spec E9 rule 36 (a): the doer's recorded role (the live stage worker, else the owner).
        self.assertEqual([row.get("role") for row in (a, b, c, d)], ["worker", "owner", "owner", "owner"])
        # The route page reads the owner Fleet's route row names: the single depth-1 owner.
        self.assertEqual([row.get("owner_attempt") for row in (a, b, c, d)], ["o", "o2", "o3", "o4"])
        # The record's owner_attempt_id picks the current owner over an earlier one;
        # two owners without a record choice name none (nothing is guessed).
        two = [job(attempt_id="old", depth=1, worker_type="owner", liveness="dead"),
               job(attempt_id="new", depth=1, worker_type="owner", liveness="working")]
        for record, want in (({"owner_attempt_id": "new"}, "new"), ({}, None), ({"owner_attempt_id": "-"}, None)):
            rows = {"claude\0s": [{"id": "rt-e"}]}
            with mock.patch.object(live, "_collect_routes", return_value={"rt-e": {"jobs": two, "record": record}}):
                session_line.route_details(object(), rows, {}, now=10_000.0)
            self.assertEqual(rows["claude\0s"][0].get("owner_attempt"), want)
        # Fleet's judgment: a closed route is in no count.
        self.assertEqual(session_line.route_badge("route 4 · execute", panels["claude\0s"]), "route 4 ⠏2 ✕1")
        self.assertEqual(session_line.token_text(250_136), "250K")
        # Without its rows the count stays alone; a missing label stays empty.
        self.assertEqual(session_line.route_badge("route 3 · execute", None), "route 3")
        self.assertEqual(session_line.route_badge("", panels["claude\0s"]), "")
        # Past the panel's eight kept rows the pass's own markers of every current route count.
        markers = [("working", False)] * 7 + [("idle", False)] * 3 + [("dead", False), ("dead", True)]
        self.assertEqual(session_line.route_badge("route 12", panels["claude\0s"][:2], markers),
                         "route 12 ⠏7 ●3 ✕1")
        self.assertEqual(session_line.route_badge("route 12", panels["claude\0s"]), "route 12")

    def test_route_rows_carry_the_owner_and_each_live_worker_apart(self):
        # Spec E9 rule 43: the owner and each stage worker running now, apart, from the jobs of
        # the same pass (Fleet's own live workers, its row's ``worker_contexts``); a finished
        # stage's worker is not listed; two parallel workers are two entries.
        from types import SimpleNamespace
        from unittest import mock
        from tools.fleet import session_line
        from tools.fleet.explorer import live
        from tools.fleet.session_panel import route_row
        job = lambda **kw: SimpleNamespace(key=kw.get("attempt_id"), **kw)
        owner = job(attempt_id="o", depth=1, worker_type="owner", liveness="idle", harness="claude", model="opus",
                    effort="xhigh", ctx_pct=52, elapsed_min=130.0)
        first = job(attempt_id="w1", depth=2, route_node="execute", liveness="working", harness="codex",
                    resolved_model="gpt-fixture", effort="high", ctx_pct=22, exec_tool={"name": "Edit", "target": "tools/x.py"},
                    elapsed_min=12.0)
        second = job(attempt_id="w2", depth=2, route_node="execute", liveness="idle", harness="opencode",
                     model="glm-fixture", effort="max", ctx_pct=71, elapsed_min=8.0)
        done = job(attempt_id="w0", depth=2, route_node="plan", liveness="dead", harness="claude", model="sonnet", elapsed_min=30.0)
        get = live._get

        def contexts(*workers):
            # The row's live workers as Fleet's route record lists them.
            return [{"stage": get(w, "route_node"), "harness": get(w, "harness"),
                     "model": live._model_pair(get(w, "resolved_model", "model"))[0],
                     "ctx_pct": round(session_line.job_context(w, get)[0], 1)} for w in workers]

        def rows(jobs, workers):
            route = {"id": "rt", "state": "working", "activity": "running", "current": True, "worker_stage": "execute"}
            panels = {"claude\0s": [{"id": "rt", "state": "working", "worker_stage": "execute", "worker_contexts": contexts(*workers)}]}
            with mock.patch.object(live, "_collect_routes", return_value={"rt": {"jobs": jobs}}):
                session_line.route_details(object(), panels, {"rt": route}, now=10_000.0)
            return panels["claude\0s"][0]
        # One worker leads the row: the owner's own values ride apart; the finished one is not listed.
        one = rows([owner, done, first], [first])
        self.assertEqual((one["role"], one["harness"]), ("worker", "codex"))
        self.assertEqual(one["owner_doer"], {"harness": "claude", "model": live._model_pair("opus")[0], "effort": "xhigh",
                                             "ctx_pct": round(session_line.job_context(owner, get)[0], 1)})
        self.assertEqual([(w["stage"], w["marker"], w["role"], w["harness"], w["effort"], w["tool"], w["target"], w["since"])
                          for w in one["workers_live"]],
                         [("execute", "working", "worker", "codex", "high", "Edit", "tools/x.py", 10_000.0 - 12 * 60)])
        # Two parallel workers: the owner leads the row itself, both workers are listed.
        two = rows([owner, first, second, done], [first, second])
        self.assertEqual(two["role"], "owner"); self.assertNotIn("owner_doer", two)
        self.assertEqual([(w["harness"], w["marker"]) for w in two["workers_live"]], [("codex", "working"), ("opencode", "idle")])
        self.assertNotIn("tool", two["workers_live"][1])   # an idle worker has no open tool
        # The worker finished: no worker is listed and the owner leads again.
        ended = rows([owner, SimpleNamespace(**{**vars(first), "liveness": "dead"})], [])
        self.assertEqual((ended["role"], ended["workers_live"]), ("owner", []))
        # The painted row: the owner's values and each worker's mark, role, stage, tool and own time.
        painted = route_row(one, now=10_000.0, cells=8)
        self.assertEqual((painted["owner_paint"]["harness"], painted["owner_paint"]["effort"], painted["owner_paint"]["ctx"]),
                         ("claude", "xhigh", "52%"))
        worker = painted["workers_live"][0]
        self.assertEqual((worker["kind"], worker["role"], worker["stage"], worker["tool"], worker["target"], worker["elapsed"],
                          worker["harness"], worker["effort"], worker["ctx"]),
                         ("working", "worker", "execute", "Edit", "tools/x.py", "12m", "codex", "high", "22%"))
        self.assertEqual(len(worker["gauge_row"]), 4)
        # A row from an older producer has neither, and its painted row neither.
        self.assertNotIn("workers_live", route_row({"id": "rt", "state": "working"}, now=10_000.0))

    def test_a_worker_is_found_again_by_its_attempt_and_no_owner_keeps_the_one_doer(self):
        # #1579 review follow-ups: (나) a worker leads a route whose owner is not one known
        # attempt (a replacement owner, the record's owner `-`): no ``workers_live`` at all, so the
        # panel keeps the one-doer rows; (다) each live worker is the job of the attempt Fleet
        # recorded, not one that only shares its stage, harness, model and context (an older
        # retry still alive); (라) a job's model name is read once, not once per comparison.
        from types import SimpleNamespace
        from unittest import mock
        from tools.fleet import session_line
        from tools.fleet.explorer import live
        job = lambda **kw: SimpleNamespace(key=kw.get("attempt_id"), **kw)
        owner = job(attempt_id="o", depth=1, worker_type="owner", liveness="idle", harness="claude", model="opus", ctx_pct=52, elapsed_min=130.0)
        retry = job(attempt_id="w-old", depth=2, route_node="execute", liveness="idle", harness="claude", model="sonnet", ctx_pct=22,
                    elapsed_min=40.0)
        current = job(attempt_id="w-new", depth=2, route_node="execute", liveness="working", harness="claude", model="sonnet", ctx_pct=22,
                      exec_tool={"name": "Edit", "target": "tools/x.py"}, elapsed_min=12.0)
        get = live._get
        context = {"stage": "execute", "harness": "claude", "model": live._model_pair("sonnet")[0], "ctx_pct": 22.0}

        def rows(jobs, item, record=None):
            route = {"id": "rt", "state": "working", "activity": "running", "current": True, "worker_stage": "execute"}
            panels = {"claude\0s": [{"id": "rt", "state": "working", "worker_stage": "execute", "worker_contexts": [item]}]}
            with mock.patch.object(live, "_collect_routes", return_value={"rt": {"jobs": jobs, "record": record or {}}}):
                session_line.route_details(object(), panels, {"rt": route}, now=10_000.0)
            return panels["claude\0s"][0]
        # (다) The older retry comes first among the jobs and has the same values; the recorded attempt wins.
        found = rows([owner, retry, current], {**context, "attempt": "w-new"})
        self.assertEqual([(w["marker"], w.get("tool"), w["since"]) for w in found["workers_live"]],
                         [("working", "Edit", 10_000.0 - 12 * 60)])
        # An attempt no live job carries lists nothing; a row from an older producer (no attempt) matches by values.
        self.assertEqual(rows([owner, retry, current], {**context, "attempt": "w-gone"})["workers_live"], [])
        self.assertEqual(len(rows([owner, current], context)["workers_live"]), 1)
        # (라) The recorded attempt reads no model name to match; the older row's path reads each job's once.
        pairs = mock.Mock(wraps=live._model_pair)
        with mock.patch.object(live, "_model_pair", pairs):
            rows([owner, retry, current], {**context, "attempt": "w-new"})
        by_attempt = pairs.call_count
        pairs.reset_mock()
        with mock.patch.object(live, "_model_pair", pairs):
            rows([owner, retry, current], {**context, "ctx_pct": 99.0})   # no exact match: each job compared once
        self.assertLessEqual(by_attempt, 2)              # the lead's own and the worker's own values
        self.assertLessEqual(pairs.call_count, by_attempt + 2)
        # (나) Two owner attempts, the record's owner `-`, a worker leading: no workers_live, no owner_doer.
        second = job(attempt_id="o2", depth=1, worker_type="owner", liveness="idle", harness="claude", model="opus", elapsed_min=20.0)
        led = rows([owner, second, current], {**context, "attempt": "w-new"}, {"owner_attempt_id": "-"})
        self.assertEqual((led.get("role"), led.get("harness")), ("worker", "claude"))
        self.assertNotIn("workers_live", led); self.assertNotIn("owner_doer", led)
        self.assertEqual(session_line.route_tree({}, [], None, {"worker": True}, get, live, 0.0), {})

    def test_a_doer_reads_its_recorded_role_word(self):
        # Spec E9 rule 36 (a): Fleet's own words. Fleet's shared doer names a stage worker;
        # its route body line says whether that worker is a frame leg. Without a shared doer
        # the job's own node or worker type says it.
        from types import SimpleNamespace
        from tools.fleet import session_line
        from tools.fleet.explorer import live
        get = live._get
        shared = lambda worker, label: {"doer": {"harness": "claude", "worker": worker}, "worker": [label, [], []]}
        self.assertEqual(session_line.route_doer(shared(True, "frame"), [], get, live)["role"], "frame")
        self.assertEqual(session_line.route_doer(shared(True, "worker"), [], get, live)["role"], "worker")
        self.assertEqual(session_line.route_doer(shared(False, "owner"), [], get, live)["role"], "owner")
        # The panel row's flattened body line (``_worker_pieces``) reads the same.
        self.assertEqual(session_line._worker_role({"worker": [["frame", "dim"], [" claude", "dim"]]}), "frame")
        job = lambda **kw: SimpleNamespace(key=kw.get("attempt_id"), **kw)
        leg = [job(attempt_id="f", depth=1, worker_type="frame", route_node="frame", liveness="working", harness="claude")]
        self.assertEqual(session_line.route_doer({"worker_stage": "frame"}, leg, get, live)["role"], "frame")

    def test_side_panel_values_for_worktrees_memory_kind_role_and_gauge(self):
        # Spec E9 rules 36-38: the values the side panel paints; the statusline keeps its words.
        import datetime
        from tools.fleet import session_panel
        values = {**self.sample(), "branch": "main",
                  "git": {"repo": 1, "operation": "none", "extra_worktrees": 12, "ahead": 2}}
        # 37 (a), 41 (e): the common line (Claude's statusline, the bottom layout) keeps the
        # repository name's `/` and `🚧 12` in the parentheses.
        line = "".join(part for part, _key in line_segments(values, 240))
        self.assertIn("hearting/ (main ↑2 🚧 12)", line)
        presented = session_panel.presentation(values, {}, "claude", "s", 40)
        self.assertEqual(presented["git"], [("(main ↑2 🚧 12)", "dim")])
        # 41 (a), (b): beside the native area the REPO name without `/`, `wt 12` and the GIT value, all dim.
        side = lambda parts: ("".join(text for text, _key in parts), {key for _text, key in parts})
        self.assertEqual((presented["repo_side"], presented["worktrees"], side(presented["git_side"])),
                         ([("hearting", "loc_repo")], [("wt 12", "dim")], ("main ↑2", {"dim"})))
        # 42 (b): beside the native area the `CTX` label stands for the word `context`; the
        # bottom layout's fragment keeps it, and an unknown reading has neither.
        self.assertEqual(presented["context"][0], ("context ", "dim"))
        self.assertEqual(presented["context_side"], presented["context"][1:])
        self.assertEqual("".join(text for text, _key in presented["context_side"]), "━━━━────── 37%")
        unknown = session_panel.presentation({**values, "ctx_pct": None}, {}, "claude", "s", 40)
        self.assertEqual((unknown["context"], unknown["context_side"]), ([], []))
        for count in (0, None):
            other = {**values, "git": {**values["git"], "extra_worktrees": count}}
            self.assertEqual(session_panel.session_worktrees(other), [])
            self.assertEqual(side(session_panel.session_git_side(other)), ("main ↑2", {"dim"}))
        # Nothing but the count in the parentheses: no Git row beside the native area.
        bare = {**values, "branch": "", "git": {"repo": 1, "operation": "none", "extra_worktrees": 3}}
        self.assertEqual((session_panel.session_git(bare), session_panel.session_git_side(bare)), ([("(🚧 3)", "dim")], []))
        # 40 (c): `✓` only when ahead and behind are both known to be 0; unknown draws nothing.
        for git, want, shared in (({"ahead": 0, "behind": 0}, "main ✓", "(main ✓ 🚧 12)"),
                                  ({"ahead": 0, "behind": 5}, "main ↓5", "(main ↓5 🚧 12)"),
                                  ({"ahead": 2, "behind": 5}, "main ↑2 ↓5", "(main ↑2 ↓5 🚧 12)"),
                                  ({"ahead": None, "behind": None}, "main", "(main 🚧 12)"),
                                  ({"ahead": 0, "behind": None}, "main", "(main 🚧 12)")):
            other = {**values, "git": {**values["git"], **git}}
            self.assertEqual(side(session_panel.session_git_side(other)), (want, {"dim"}))
            self.assertEqual(session_panel.session_git(other), [(shared, "dim")])
        # 40 (b): the branch keeps 20 cells, 32 with 60 cells inside; the status comes before a
        # warning beside the native area, the shared parentheses keep their order.
        long = {**values, "branch": "fix/fleet-opencode-dup-sid-and-more-words",
                "git": {"repo": 1, "operation": "rebase", "ahead": 0, "behind": 3, "extra_worktrees": 2}}
        self.assertEqual(session_panel.session_git_side(long, 40),
                         [("fix/fleet-opencode-…", "dim"), (" ↓3", "dim"), (" ⚠️rebase", "dim")])
        self.assertEqual(side(session_panel.session_git_side(long, 80)), ("fix/fleet-opencode-dup-sid-and-… ↓3 ⚠️rebase", {"dim"}))
        self.assertIn("(fix/fleet-opencode-… ⚠️rebase ↓3 🚧 2)", "".join(p for p, _k in line_segments(long, 10000, complete=True, branch_room=50)))
        self.assertIn("(fix/fleet-opencode-dup-sid-and-… ⚠️rebase ↓3 🚧 2)", "".join(p for p, _k in line_segments(long, 240)))
        # The statusline's wrapped lines take the room they are shown at.
        from tools.fleet.session_line import status_lines
        narrow = "".join(p for row in status_lines(long, 40) for p, _k in row)
        self.assertIn("(fix/fleet-opencode-… ", narrow); self.assertNotIn("dup-sid-and", narrow)
        self.assertIn("fix/fleet-opencode-dup-sid-and-…", "".join(p for row in status_lines(long, 100) for p, _k in row))
        # 37 (b): the newest saved memory row: age and type dim, the headline (else the
        # snippet's start) in the body color; nothing without a row or a text.
        now = datetime.datetime(2026, 11, 12, 10, 3, 0)
        row = {"ts": "2026-11-12T10:00:00", "type": "decision", "headline": "route rows read who first", "snippet": "body line"}
        self.assertEqual(session_panel.session_memory({"memory_latest": row}, now),
                         [("3m ", "dim"), ("decision ", "dim"), ("route rows read who first", "loc_repo")])
        self.assertEqual(session_panel.session_memory({"memory_latest": {**row, "headline": ""}}, now)[-1], ("body line", "loc_repo"))
        self.assertEqual(session_panel.session_memory({"memory_latest": {**row, "headline": "", "snippet": "superseded_by=x"}}, now), [])
        self.assertEqual(session_panel.session_memory({}, now), [])
        # 46 (b), (c): the MEM section's records, two at most: the type's short word, the age and
        # the headline (else the snippet's start); a row without a text is left out.
        recent = [{**row, "type": "user-correction"}, {**row, "ts": "2026-11-12T08:00:00", "type": "completion", "headline": ""},
                  {**row, "type": "decision"}]
        self.assertEqual(session_panel.session_memory_items({"memory_recent": recent}, now),
                         [{"kind": "correction", "age": "3m", "text": "route rows read who first"},
                          {"kind": "done", "age": "2h 3m", "text": "body line"}])
        self.assertEqual([session_panel.memory_kind(kind) for kind in ("decision", "lesson", "feedback", "fact", "an-unlisted-type")],
                         ["decision", "lesson", "feedback", "fact", "an-unlisted…"])
        self.assertEqual(session_panel.session_memory_items({"memory_recent": [{**row, "headline": "", "snippet": "superseded_by=x"}]}, now), [])
        self.assertEqual(session_panel.session_memory_items({}, now), [])
        self.assertEqual(session_panel.presentation({"memory_recent": recent[:1]}, {}, "claude", "s", 40)["memory_items"][0]["kind"],
                         "correction")
        # 36 (a), (b), 38: a route row's recorded role only, Fleet's kind word, the gauge at the panel's width.
        painted = session_panel.route_row({"role": "owner", "capability": "autopilot-code", "ctx_pct": 38}, cells=8)
        self.assertEqual((painted["role"], painted["kind_word"]), ("owner", "code"))
        self.assertEqual(session_panel.route_gauge_cells(40), 8)
        self.assertEqual("".join(part[0] for part in painted["gauge_row"][:2]), "━━━─────")
        self.assertEqual(painted["gauge_row"][-1][0], "38%")
        for capability, word in (("code", "code"), ("frame", "frame"), ("autopilot-lab", "lab")):
            self.assertEqual(session_panel.route_row({"capability": capability})["kind_word"], word)
        unknown = session_panel.route_row({"role": "boss", "ctx_tokens": 250_136}, cells=8)
        self.assertNotIn("role", unknown); self.assertNotIn("kind_word", unknown)
        self.assertEqual(unknown["gauge_row"], [["250K", "dim"]])

    def test_the_bash_statusline_writes_the_shared_git_words(self):
        # Spec E9 rule 41 (e): Claude's statusline keeps `name/` and the parentheses; its git
        # value takes `✓` (ahead and behind known to be 0, nothing when unknown) and the branch
        # cut (20 cells, 32 on a line of 60 or more), as the shared line writes it.
        import os, re, subprocess, tempfile
        from pathlib import Path
        script = Path(__file__).resolve().parents[3] / "adapters/claude/statusline.sh"
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".claude").mkdir()
            (home / "tools/fleet").mkdir(parents=True)
            (home / "demo").mkdir()
            # A fixture observation: the shell assignments gitinfo's display prints, nothing read.
            (home / "tools/fleet/gitinfo.py").write_text(
                "import os\nfor key in ('repo','branch','operation','merged','branch_done','extra_worktrees','ahead','behind','dirty'):\n"
                "    print('git_'+key+'='+os.environ.get('FIXTURE_GIT_'+key.upper(),'unknown'))\n")
            def line(columns=None, **git):
                env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "AGENT_HOME": str(home), "XDG_STATE_HOME": str(home / "state"),
                       "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
                       **({"COLUMNS": str(columns)} if columns else {}),
                       **{"FIXTURE_GIT_" + key.upper(): str(value) for key, value in git.items()}}
                done = subprocess.run(["bash", str(script)], input='{"cwd":"%s","model":{"id":"fixture"}}' % (home / "demo"),
                                      text=True, capture_output=True, env=env, timeout=20)
                return re.sub(r"\x1b\[[0-9;]*m", "", done.stdout.splitlines()[0])
            base = {"repo": 1, "operation": "none", "extra_worktrees": 3}
            self.assertIn("demo/ (main ✓ 🚧 3)", line(branch="main", ahead=0, behind=0, **base))
            self.assertIn("demo/ (main ↑2 ↓1 🚧 3)", line(branch="main", ahead=2, behind=1, **base))
            self.assertIn("demo/ (main 🚧 3)", line(branch="main", **base))
            long = "fix/fleet-opencode-dup-sid-and-more-words"
            self.assertIn("(fix/fleet-opencode-dup-sid-and-… 🚧 3)", line(branch=long, **base))
            self.assertIn("(fix/fleet-opencode-… ⚠️rebase ↓3)",
                          line(50, branch=long, ahead=0, behind=3, repo=1, operation="rebase", extra_worktrees=0))
            # The shared line writes the same words.
            values = {"cwd": "/synthetic/demo", "branch": long, "git": {"repo": 1, "operation": "rebase", "ahead": 0, "behind": 3,
                                                                          "extra_worktrees": 0}}
            self.assertIn("(fix/fleet-opencode-… ⚠️rebase ↓3)",
                          "".join(p for p, _k in line_segments(values, 10000, complete=True, branch_room=50)))

    def test_messages_are_the_latest_three_peer_rows_either_way(self):
        from tools.fleet.session_line import peer_message_rows
        received = [{"source": "peer", "sender": "[e7] x", "sender_harness": "claude", "sender_session_id": "e7",
                     "text": "first", "at": 1},
                    {"source": "notification", "text": "not a peer message", "at": 2},
                    {"source": "peer", "sender": "someone", "text": "third", "at": 4}]
        sent = [{"to": {"harness": "codex", "session_id": "s73", "name": "x"}, "summary": "second", "at": 3},
                {"to": {"name": "named"}, "summary": "fourth", "at": 5}]
        tags = {("claude", "e7"): "e7", ("codex", "s73"): "73"}
        rows = peer_message_rows(received, sent, lambda h, s: tags.get((h, s)), limit=3)
        self.assertEqual([(r["way"], r["who"], r["text"]) for r in rows],
                         [("→", "[73]", "second"), ("←", "someone", "third"), ("→", "named", "fourth")])
        self.assertEqual(len(peer_message_rows(received, sent, lambda h, s: tags.get((h, s)))), 4)

    def test_a_message_row_carries_its_head_values_and_first_meaningful_text(self):
        # Spec E9 rule 31: harness, time, kind and a sent one's delivery state come from
        # the records as they are; the body drops only leading bracket frames and heading marks.
        import time
        from tools.fleet.session_line import peer_message_rows, message_body
        self.assertEqual(message_body("[52 → 감독 wB:pBA] [review]\n\n## 결과\n#1556 병합"), "결과 #1556 병합")
        self.assertEqual(message_body("plain [kept] text"), "plain [kept] text")
        now = time.mktime((2026, 11, 12, 15, 0, 0, 0, 0, -1))
        received = [{"source": "peer", "sender_harness": "codex", "sender_session_id": "s52",
                     "text": "[notice] 검토 결과", "at": now - 600}]
        sent = [{"to": {"harness": "claude", "session_id": "se8"}, "summary": "설치됨", "kind": "steer",
                 "status": status, "at": now - age} for status, age in
                (("received", 60), ("sent", 120), ("unknown", 5 * 3600), ("unknown", 300), ("failed", 3 * 86400))]
        rows = peer_message_rows(received, sent, lambda h, s: {"s52": "52", "se8": "e8"}.get(s), now=now)
        by_age = sorted(rows, key=lambda row: row["at"])
        failed, old, first, quick, sent_row, delivered = by_age
        self.assertEqual((first["way"], first["who"], first["harness"], first["body"], first["delivery"]),
                         ("←", "[52]", "codex", "검토 결과", None))
        self.assertEqual(first["when"], "14:50")
        self.assertEqual(failed["when"], "11-09")
        self.assertEqual(failed["delivery"][1], "lvl_r")
        # Review RV-PR1560 follow-up 6: `unknown` (recipient not resolved) never arrives:
        # `not delivered`, no age, the caution color however recent.
        self.assertEqual(old["delivery"], quick["delivery"])
        self.assertEqual(old["delivery"][1], "lvl_y")
        self.assertNotRegex(old["delivery"][0], r"\d")
        self.assertEqual(delivered["delivery"][1], "dim")
        self.assertEqual((sent_row["kind"], sent_row["delivery"][1]), ("steer", "dim"))

if __name__ == "__main__": unittest.main()
