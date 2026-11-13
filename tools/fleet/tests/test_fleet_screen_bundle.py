"""Approved 1008 bundle: OWNER/FRAME columns, flat supervisor lines, GPU folds, memory events."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import copy
import datetime
import os
import sqlite3
from pathlib import Path
import sys
import tempfile
import time
import re
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import render, fleet
from fleet.collectors import memory, steward, herdr
from fleet import herdr_projection
from fleet.model import DispatchJob, ResourceJob, Session, WorkProjection


def session(sid, harness="codex", **kw):
    return Session(harness=harness, pid=100 + len(sid), proc_start=sid,
                   session_id=sid, session_tag=sid[:2], cwd="/work/bundle",
                   runtime_name=sid, liveness="working", elapsed_min=180, **kw)


def target(sid, harness="codex"):
    return {"harness": harness, "session_id": sid}


def text(lines):
    return [render._plain(line) for line in lines]


class BundleTest(unittest.TestCase):
    def setUp(self):
        for name in ("_PROCESS_VIEW", "_SHOW_ALL", "_COMPUTE_HOSTS", "_COMPUTE_HOSTS_SET_AT", "_TINT_OK", "_LAYOUT",
                     "_ROUTE_FOLD", "_OFFSET", "_FOLD_ROWS", "_FOLDABLE", "_PROMPT",
                     "_SELECT_MODE", "_CURSOR_ID"):
            value = getattr(render, name)
            self.addCleanup(setattr, render, name, value)
        render._PROCESS_VIEW = render._SHOW_ALL = False
        render._COMPUTE_HOSTS = None
        render._LAYOUT = "auto"
        render._TINT_OK = False
        render._ROUTE_FOLD = {}
        render._PROMPT = None
        render._SELECT_MODE = False

    def build(self, sessions, jobs=(), width=168, **kw):
        return render._build_lines(sessions, list(jobs), "both", False, 0,
                                   layout=render._layout_mode(width), term_width=width, **kw)

    def test_current_stall_row_is_visible_on_default_board_widths(self):
        parent = session("stall-parent")
        parent.liveness = "idle"
        job = DispatchJob(key="code", slug="stall-worker", cwd=parent.cwd,
            parent_sid=parent.session_id, is_child=True, depth=2, worker_type="stage",
            harness="codex", route_id="rt-stall", route_node="execute",
            attempt_id="att-stall", stage="execute", status="open", liveness="working",
            stall_observation={"state": "stalled", "observed_at": 700.0,
                               "last_progress_at": 100.0})
        for width in (100, 140, 168):
            with self.subTest(width=width):
                rows = text(self.build([parent], [job], width))
                self.assertTrue(any("! stalled" in line or "! check" in line for line in rows), rows)
                if width in (100, 168):
                    self.assertTrue(any("! stalled 10m" in line for line in rows), rows)
                self.assertTrue(all(render._dw(line) <= width for line in rows), rows)
                job.stall_observation = None
                recovered = text(self.build([parent], [job], width))
                self.assertFalse(any("! stalled" in line or "! check" in line
                                     for line in recovered), recovered)
                job.stall_observation = {"state": "stalled", "observed_at": 700.0,
                                         "last_progress_at": 100.0}

    def test_mixed_owner_capabilities_share_main_routing_column(self):
        main = session("aa", model="Opus 5.5", effort="xhigh", steward=True)
        main.route_chain = {"visible": True, "nodes": [{"label": "MAINROUTE", "state": "open"},
                                     {"label": "PAST", "state": "done"}]}
        owners = []
        for i, (harness, model, effort, capability) in enumerate((
                ("claude", "Opus", "xhigh", "code"),
                ("codex", "gpt-6.1-sol", "medium", "lab"),
                ("opencode", "Opus 5.5", "xhigh", "draft"),
                ("claude", "very-long-model-name", "low", "code"))):
            job = DispatchJob(key=capability, capability_mode="dev",
                              capability_owner="autopilot-" + capability,
                              slug="owner%d" % i, cwd=main.cwd, depth=1 if i < 3 else 2,
                              worker_type="owner", intensity="standard", harness=harness,
                              model=model, effort=effort, parent_sid=main.session_id,
                              is_child=True, liveness="working", stage="scaffold",
                              work_projection=WorkProjection(source="inline", stage_label="scaffold"))
            owners.append(job)
        owners[-1].parent_slug = owners[0].slug
        owners[-1].route_node = "scaffold"
        owners[1].resource_children = [ResourceJob(run_id="fixture-gpu-resource", node="scaffold",
                                                  liveness="working", elapsed_min=60)]
        render.set_compute_hosts({"configured": True, "collected_at": time.time(), "hosts": [
            {"host": "gpu", "reachable": True, "gpus": [{"index": 0, "processes": [
                {"pid": 123, "proc_start": "456", "command": "python train.py",
                 "owner": {"kind": "run", "id": "fixture-gpu-resource"}}]}]}]})
        for tint in (False, True):
            render._TINT_OK = tint
            for width in (60, 100, 120, 168):
                with self.subTest(width=width, tint=tint):
                    # Suppression affects the MAIN content, not its routing anchor.
                    layout = render._layout_mode(width)
                    main_rows = ([render._session_row(main, False, name_width=render._wide_name_width(width))]
                                 if layout == "wide" else list(render._session_row_2line(main, term_width=width)))
                    anchor_row = next(row for row in main_rows if "MAINROUTE" in render._plain(row))
                    anchor_text = render._plain(anchor_row)
                    anchor = render._dw(anchor_text[:anchor_text.index("MAINROUTE")])
                    lines = self.build([main], owners, width)
                    starts = []
                    for row in lines:
                        if not row:
                            continue
                        pos = 0
                        for value, key in row:
                            if value in ("code", "lab", "draft") and key == "name_dim":
                                starts.append(pos)
                            if not render._is_fill(value):
                                pos += render._dw(value)
                    self.assertEqual(starts, [anchor] * len(owners))
                    self.assertTrue(any("GPU gpu:0" in row for row in text(lines)))
                    self.assertEqual(sum("╰" in row for row in text(lines)), len(owners))
                    self.assertFalse(any("scaffold" in row for row in text(lines) if "╭" in row))
                    box_rows = [r for r in text(lines) if any(c in r for c in "╭╰├")]
                    self.assertTrue(all(r.index(next(c for c in "╭╰├" if c in r)) > render._STEWARD_LINE_COL
                                        for r in box_rows))

    def test_all_owner_states_are_once_on_divider_worker_stays_inline(self):
        main = session("aa")
        for state, live in (("preparing", "working"), ("scaffold", "working"),
                            ("done", "done")):
            owner = DispatchJob(key="code", slug="owner", parent_sid="aa", is_child=True,
                                cwd=main.cwd, depth=1, worker_type="owner", harness="codex",
                                stage=state, liveness=live, afterglow=(state == "done"),
                                work_projection=WorkProjection(source="inline", stage_label=state))
            worker = DispatchJob(key="code-execute", slug="worker", parent_slug="owner",
                                 cwd=main.cwd, depth=2, is_child=True, liveness="working")
            for width in (60, 100, 168):
                rows = text(self.build([main], [owner, worker], width))
                rail = next(r for r in rows if "├" in r)
                self.assertIn(state, rail)
                self.assertFalse(any(state in r for r in rows if "╭" in r or "╰" in r))
                self.assertTrue(any("running" in r for r in rows))
                self.assertNotIn("\033[5m", "\n".join(rows))

    def test_explicit_workers_keep_their_status_and_original_columns(self):
        for depth in (1, 2):
            worker = DispatchJob(key="code-execute", slug="worker", harness="codex",
                                 capability_owner="autopilot-code-execute",
                                 depth=depth, worker_type="stage", liveness="working",
                                 model="gpt-6.1-sol", effort="medium", elapsed_min=17)
            self.assertFalse(render._route_rides_the_rail(worker, None, True))
            self.assertEqual(render._dispatch_prefix(worker, in_card=True), "     ")
            l2 = render._plain(render._dispatch_row_2line(worker, in_card=True)[1])
            self.assertEqual(l2.index("17m"), 4)
            self.assertEqual(l2.index("gpt-6.1-sol"), 20)
            self.assertEqual(render._dw(l2[:l2.index("code-execute")]), 47)

    def test_owner_close_rail_preserves_residue_and_dead_resume_evidence(self):
        owner = DispatchJob(key="code", slug="owner", harness="codex", cwd="/work/bundle",
                            parent_sid="aa", is_child=True, worker_type="owner", depth=1,
                            liveness="working", residue_pids=[753216])
        rows = text(self.build([session("aa")], [owner], 100))
        self.assertTrue(any("╰" in row and "left pid 753216" in row for row in rows))
        owner.residue_pids = None
        owner.liveness = "dead"
        owner.note = "dead-runtime-exit"
        owner.resume_boundary = "execute"
        owner._dead_terminal_owner = True
        rows = text(self.build([session("aa")], [owner], 100))
        self.assertTrue(any("╰" in row and "dead-runtime-exit resume=execute" in row for row in rows))

    def test_owner_status_position_across_harnesses_and_child_types(self):
        for harness in ("claude", "codex", "opencode"):
            for child_type in (None, "worker", "resource"):
                for state, label in (("preparing", "preparing"), ("scaffold", "scaffold"),
                                     ("one-shot", "one-shot"), ("done", "done ✓"),
                                     ("dead", "dead @execute"),
                                     ("orphaned", "⚠ ORPHANED resume=execute")):
                    owner = DispatchJob(
                        key="code", slug="owner", cwd="/work/bundle", parent_sid="aa",
                        is_child=True, harness=harness, depth=1, worker_type="owner",
                        intensity="standard", stage="execute" if state in ("dead", "orphaned") else state,
                        liveness="dead" if state in ("dead", "orphaned") else
                                 "done" if state == "done" else "working",
                        note="dead-parent-orphaned" if state == "orphaned" else None,
                        resume_boundary="execute",
                        work_projection=WorkProjection(source="inline", stage_label="execute"
                                                       if state in ("dead", "orphaned") else state))
                    owner.afterglow = state == "done"
                    jobs = [owner]
                    if child_type == "worker":
                        jobs.append(DispatchJob(key="code-execute", slug="worker",
                                                parent_slug=owner.slug, cwd=owner.cwd,
                                                depth=2, is_child=True, liveness="working"))
                    elif child_type == "resource":
                        owner.resource_children = [ResourceJob(run_id="resource", node="train",
                                                               liveness="working", elapsed_min=1)]
                    for width in (60, 100, 168):
                        with self.subTest(harness=harness, child=child_type, state=state, width=width):
                            with mock.patch.object(render, "_SHOW_ALL", state in ("dead", "orphaned")):
                                rows = text(self.build([session("aa", harness)], jobs, width))
                            rail = next(row for row in rows if ("├" if child_type else "╰") in row)
                            self.assertIn(label, rail)
                            self.assertEqual(sum(row.count(label) for row in rows), 1)
                            other = "╰" if child_type else "├"
                            self.assertFalse(any(label in row for row in rows if other in row))
                            self.assertTrue(all(render._dw(row) <= width for row in rows
                                                if any(mark in row for mark in "╭│├╰")))

    def stage_divider_screen(self, width):
        """Fixed screen with GPU, worker and childless cards under a supervisor."""
        main = session("aa", "claude", steward=True, steward_targets=[target("bb")],
                       model="Opus", effort="high", summary="NOW owner work")
        child = session("bb", "codex", model="gpt-6.1-sol", effort="medium")
        owners = []
        for slug, harness, state in (("gpu-owner", "claude", "one-shot"),
                                     ("worker-owner", "codex", "preparing"),
                                     ("childless-owner", "opencode", "scaffold")):
            owners.append(DispatchJob(
                key="code", slug=slug, cwd=main.cwd, parent_sid="aa", is_child=True,
                harness=harness, depth=1, worker_type="owner", intensity="standard",
                model="Opus" if harness == "claude" else "gpt-6.1-sol",
                effort="high", liveness="working", stage=state,
                work_projection=WorkProjection(source="inline", stage_label=state)))
        owners[0].resource_children = [ResourceJob(run_id="golden-gpu", node="train",
                                                  liveness="working", elapsed_min=2)]
        owners.append(DispatchJob(key="code-execute", slug="worker", cwd=main.cwd,
                                  parent_slug="worker-owner", is_child=True, depth=2,
                                  harness="codex", model="gpt-6.1-sol", effort="medium",
                                  liveness="working", elapsed_min=3))
        render.set_compute_hosts({"configured": True, "collected_at": 1700000000.0, "hosts": [
            {"host": "gpu", "reachable": True, "gpus": [{"index": 0, "processes": [
                {"pid": 123, "proc_start": "456", "command": "python train.py",
                 "owner": {"kind": "run", "id": "golden-gpu"}}]}]}]})
        with mock.patch("time.time", return_value=1700000000.0), \
                mock.patch.object(render, "_API_DISABLED", False), \
                mock.patch.object(render, "_HEARTING", None):
            return "\n".join(row.rstrip() for row in
                             text(self.build([main, child], owners, width))) + "\n"

    @mock.patch.dict("os.environ", {"FLEET_NOW_LANG": "Korean"})   # Korean screen words
    def test_stage_divider_screen_matches_wide_and_narrow_goldens(self):
        for width in (60, 100, 168):
            with self.subTest(width=width):
                golden = Path(__file__).parent / "fixtures" / "stage_divider" / ("screen-%d.txt" % width)
                self.assertEqual(self.stage_divider_screen(width), golden.read_text())

    def hierarchy(self):
        a = session("aa", steward=True, steward_targets=[target("cc")])
        b = session("bb", steward=True, steward_targets=[target("old-dd"), target("ee")])
        c, d, e, z = [session(sid) for sid in ("cc", "dd", "ee", "zz")]
        d.session_aliases = ["old-dd"]
        return [z, c, e, d, b, a]

    def test_flat_groups_use_target_order_aliases_and_emit_each_session_once(self):
        sessions = self.hierarchy()
        ordered = render._sort_group_sessions(sessions + [sessions[-1]])
        self.assertEqual([s.session_id for s in ordered], ["aa", "cc", "bb", "dd", "ee", "zz"])
        for shuffled in (list(reversed(sessions)), sessions[2:] + sessions[:2]):
            self.assertEqual([s.session_id for s in render._sort_group_sessions(shuffled)],
                             [s.session_id for s in ordered])
        keeper = render._LiveOrderState()
        keeper.reconcile_sessions("bundle", [session("zz"), session("dd"), session("bb"), session("aa")])
        self.assertEqual([s.session_id for s in keeper.reconcile_sessions("bundle", ordered)],
                         [s.session_id for s in ordered])

    def test_under_id_intervals_preserve_session_rows_model_columns_and_box_edges(self):
        sessions = self.hierarchy()
        owner = DispatchJob(key="code", slug="owner", parent_sid="aa", is_child=True,
                            cwd=sessions[0].cwd, depth=1, worker_type="owner", liveness="working")
        for width in (60, 100, 168):
            rows = text(self.build(sessions, [owner], width))
            ids = {sid: next(i for i, row in enumerate(rows) if "[%s]" % sid in row)
                   for sid in ("aa", "bb", "cc", "dd", "ee", "zz")}
            # Every MAIN chip keeps its original column and no line overlays an ID.
            for sid, i in ids.items():
                self.assertEqual(rows[i].index("[%s]" % sid) + 1, render._STEWARD_LINE_COL)
                self.assertNotIn(render._STEWARD_LINE_MARK, rows[i])
            for parent, end in (("aa", "cc"), ("bb", "ee")):
                for i in range(ids[parent] + 1, ids[end]):
                    if i in ids.values():
                        continue
                    self.assertEqual(rows[i][render._STEWARD_LINE_COL], "┆", rows[i])
                    self.assertEqual(rows[i][render._STEWARD_LINE_COL + 1:render._RAIL_COL], "  ")
            self.assertTrue(any("┆" in r and "╭" in r for r in rows))
            box = [r for r in rows if "╭" in r or "╰" in r]
            self.assertTrue(all(render._dw(r[:r.index(r[-1])]) == render._dispatch_box_width(width, render._layout_mode(width)) - 1
                                for r in box))
            self.assertNotIn("⚑ →", "\n".join(rows))
            self.assertNotIn("⚑ ←", "\n".join(rows))
            self.assertIn("[aa] ⚑ ", rows[ids["aa"]])
            self.assertNotIn("⚑", rows[ids["aa"] + 1])
        original = render._session_row_2line(session("aa", model="MODEL", effort="medium"))[1]
        joined = render._under_id_connector(original)
        self.assertIn("3h", render._plain(joined))
        self.assertIn("┆  3h", render._plain(joined))
        self.assertEqual(render._plain(original).index("MODEL"), render._plain(joined).index("MODEL"))
        worker = DispatchJob(key="code-execute", harness="codex", depth=2, worker_type="stage",
                             model="MODEL", effort="medium", liveness="working", elapsed_min=17)
        l2 = render._dispatch_row_2line(worker, in_card=True)[1]
        framed = render._frame_dispatch_line(l2, 100, "mid", "frm_idle")
        connected = render._plain(render._under_id_connector(framed))
        self.assertEqual(connected[render._STEWARD_LINE_COL], "┆")
        self.assertEqual(connected[render._RAIL_COL], "│")
        self.assertIn("17m", connected)
        self.assertEqual(connected.index("MODEL"), render._plain(l2).index("MODEL"))

    def gpu_snapshot(self):
        return {"configured": True, "hosts": [{"host": "gpu", "reachable": True,
                "gpus": [{"index": index, "memory_used_mib": 1024, "memory_total_mib": 40960,
                          "processes": [{"pid": 10 + index, "proc_start": str(index),
                                         "command": "python train%d.py" % index}]}
                         for index in (0, 1)]}]}

    def test_gpu_default_fold_keyboard_per_gpu_mouse_and_view_maps(self):
        render._COMPUTE_HOSTS = self.gpu_snapshot()
        original = copy.deepcopy(render._COMPUTE_HOSTS)
        for process in (False, True):
            render._PROCESS_VIEW = process
            render._ROUTE_FOLD = {}
            rows = text(self.build([session("aa")]))
            self.assertIn("compute resources", "\n".join(rows))
            self.assertEqual(sum("▸ 1" in row for row in rows), 2)
            self.assertFalse(any("↳ python" in row for row in rows))
            for entry in render._FOLDABLE:
                if entry["card_key"][:1] == render._GPU_FOLD_ALL:
                    self.assertIn("VRAM", rows[entry["line"]])
            self.assertTrue(render._handle_base_key(ord("c"), 20))
            rows = text(self.build([session("aa")]))
            self.assertEqual(sum("↳ python" in row for row in rows), 2)
            entry = next(e for e in render._FOLDABLE if e["card_key"] == ("gpu-commands", "gpu", 0))
            render._FOLD_ROWS = {4: entry}  # viewport offset already applied by _draw
            render._FOLD_SPANS = {4: [(4, 12)]}
            render._HIT_VALID = True
            self.assertTrue(render._handle_mouse(5, 4))
            rows = text(self.build([session("aa")]))
            self.assertFalse(any("↳ python train0" in row for row in rows))
            self.assertTrue(any("↳ python train1" in row for row in rows))
            render._handle_base_key(ord("c"), 20)
            self.assertTrue(all(render._gpu_commands_folded("gpu", i) for i in (0, 1)))
        self.assertEqual(render._COMPUTE_HOSTS, original)
        self.assertIn("c GPU cmd", render._plain(render._footer_segs(False, [], 168)))
        fake = mock.Mock(BUTTON1_PRESSED=1, BUTTON4_PRESSED=2, BUTTON5_PRESSED=4)
        render._configure_input(fake, {})
        fake.mousemask.assert_called_once_with(7)
        fake.mousemask.reset_mock()
        render._configure_input(fake, {"FLEET_MOUSE": "1"})
        fake.mousemask.assert_called_once_with(7)

    def test_memory_event_mode_skips_aggregate_sources_and_keeps_project_events(self):
        now = datetime.datetime(2026, 10, 8, 16, 0)
        events = [{"ts": "2026-10-08T15:00:00", "cwd": "/work/bundle", "action": "add",
                   "tier": "durable", "snippet": "project-memory-kept"}]
        with mock.patch.object(memory, "_read_jsonl_tail", return_value=(events, True)) as read, \
                mock.patch.object(memory, "_graveyard_path", side_effect=AssertionError("graveyard")), \
                mock.patch.object(memory, "_periodic_curate", side_effect=AssertionError("curate")), \
                mock.patch.object(memory, "_durable_over", side_effect=AssertionError("db")):
            snapshot = memory.collect(now, include_summary=False)
        read.assert_called_once()
        self.assertEqual(snapshot["by_repo"]["bundle"][0]["snippet"], "project-memory-kept")
        rows = text(self.build([session("aa")], memory=snapshot))
        self.assertIn("project-memory-kept", "\n".join(rows))
        self.assertNotIn("🧠 mem", "\n".join(rows))
        with mock.patch.object(memory, "collect", return_value=snapshot) as collect:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FLEET_DEMO", None)
                render._collect_memory()
                fleet._collect_memory()
        self.assertEqual(collect.call_args_list, [
            mock.call(include_summary=False, include_backlog=False),
            mock.call(include_summary=False),
        ])

    def test_gpu_mouse_map_uses_actual_draw_scroll_offset_in_both_views(self):
        from fleet.tests.test_f27_mouse import FakeScreen
        render._COMPUTE_HOSTS = self.gpu_snapshot()
        for process in (False, True):
            render._PROCESS_VIEW = process
            self.build([session("aa")], width=100)
            entry = next(e for e in render._FOLDABLE if e["card_key"] == ("gpu-commands", "gpu", 0))
            render._OFFSET = entry["line"] - 2
            with mock.patch.object(render.curses, "doupdate"):
                render._draw(FakeScreen(h=40, w=100), [session("aa")], [], "both", 0)
            row = next(y for y, e in render._FOLD_ROWS.items() if e["card_key"] == entry["card_key"])
            self.assertEqual(row, entry["line"], "compute/GPU fold stays pinned while list scrolls")
            self.assertNotIn(row, render._CLICK_ROWS)
            self.assertNotIn(row, render._TOGGLE_ROWS)
            render._HIT_VALID = True
            render._FOLD_SPANS[row] = [(4, 12)]
            render._handle_mouse(5, row)
            self.assertFalse(render._gpu_commands_folded("gpu", 0))
            self.assertTrue(render._gpu_commands_folded("gpu", 1))
            render._ROUTE_FOLD.clear()

    def test_session_gpu_command_label_obeys_global_and_per_gpu_fold(self):
        resources = [{"host": "gpu", "index": 0, "processes": [
                     {"pid": 4, "proc_start": "1", "command": "python train.py"}]}]
        folded = text(render._gpu_resource_strip(resources, 168))[0]
        self.assertIn("▸ 1", folded)
        self.assertNotIn("train.py", folded)
        render._ROUTE_FOLD[render._GPU_FOLD_ALL] = False
        self.assertIn("train.py", text(render._gpu_resource_strip(resources, 168))[0])
        render._ROUTE_FOLD[render._gpu_fold_key("gpu", 0)] = True
        self.assertNotIn("train.py", text(render._gpu_resource_strip(resources, 168))[0])

    def test_review_support_boxes_and_owner_workers_keep_both_borders_under_connector(self):
        main = session("aa", steward=True, steward_targets=[target("bb")])
        end = session("bb")
        jobs = []
        for worker_type in ("review", "support", "owner"):
            jobs.append(DispatchJob(key="code", slug=worker_type, cwd=main.cwd,
                                    parent_sid="aa", is_child=True, depth=1,
                                    worker_type=worker_type, harness="codex", model="gpt-6.1-sol",
                                    effort="xhigh", liveness="working", elapsed_min=83))
        jobs.append(DispatchJob(key="code-execute", slug="stage-worker", cwd=main.cwd,
                                parent_slug="owner", is_child=True, depth=2,
                                worker_type="stage", harness="codex", model="gpt-6-luna",
                                effort="high", elapsed_min=9, liveness="working"))
        for width in (100, 168):
            with self.subTest(width=width):
                rows = text(self.build([end, main], jobs, width))
                inside = False
                for row in rows:
                    if "╭" in row:
                        inside = True
                        self.assertEqual(row.index("╭"), render._RAIL_COL)
                    if inside and "╭" not in row and "╰" not in row and "├" not in row:
                        self.assertEqual(row[render._RAIL_COL], "│", row)
                        self.assertTrue(row.endswith("│"), row)
                    if "╰" in row:
                        inside = False
                    if inside or "╰" in row:
                        self.assertEqual(row[render._STEWARD_LINE_COL], "┆", row)
                self.assertNotIn("╭│", "\n".join(rows))
                self.assertNotIn("││", "\n".join(rows))
                self.assertTrue(any("9m" in row and row[render._RAIL_COL] == "│"
                                    for row in rows), "\n".join(rows))

    def test_connector_preserves_context_track_percentage_gap(self):
        main = session("aa", ctx_pct=82, context_window_tokens=256000, herdr_attached=True)
        original = render._context_detail_row(main, term_width=168)[0]
        connected = render._under_id_connector(original)
        before, after = render._plain(original), render._plain(connected)
        start = before.index("━")
        self.assertEqual(before[start:], after[start:])
        self.assertIn(" 82%", after)
        self.assertIn("┆  herdr", after)

    def test_long_korean_owner_title_cannot_move_routing_anchor(self):
        main = session("aa", model="gpt-6.1-sol", effort="xhigh")
        main.route_chain = {"visible": True, "nodes": [{"label": "MAINROUTE", "state": "open"}]}
        owners = [DispatchJob(key=cap, slug=cap, title="배치 검증기 무결성 검사 " * 10,
                             capability_owner="autopilot-" + cap,
                             cwd=main.cwd, branch="revision-long-branch", depth=1,
                             worker_type="owner", harness="codex", model="gpt-6.1-sol",
                             effort="xhigh", parent_sid="aa", is_child=True, liveness="working")
                  for cap in ("code", "lab", "draft")]
        for width in (138, 168):
            rows = self.build([main], owners, width)
            anchor = render._session_routing_column("wide", render._wide_name_width(width))
            for row in rows:
                if "╭" not in render._plain(row):
                    continue
                position = 0
                starts = []
                for value, key in row:
                    if value in ("code", "lab", "draft") and key == "name_dim":
                        starts.append(position)
                    if not render._is_fill(value):
                        position += render._dw(value)
                self.assertEqual(starts, [anchor])

    @mock.patch.object(render, "_SHOW_ALL", True)
    def test_narrow_worker_state_ellipsizes_before_two_cell_border_margin(self):
        worker = DispatchJob(key="code", slug="worker", harness="codex", depth=1,
                             worker_type="review", model="gpt-6.1-sol", effort="xhigh",
                             capability_mode="audit", intensity="standard", unit="qa/code-review",
                             stage="preparing", liveness="working", elapsed_min=6)
        row = render._dispatch_row_2line(worker, in_card=True)[1]
        framed = render._frame_dispatch_line(row, 99, "mid", "frm_idle")
        shown = render._plain(framed)
        self.assertEqual(render._dw(shown), 99)
        self.assertTrue(shown.endswith("… │"), shown)

    def test_detail_inset_is_identical_with_and_without_supervisor_lines(self):
        parent = session("aa", steward=True, steward_targets=[target("bb")],
                         model="MODEL", effort="high", herdr_attached=True, summary="NOWTEXT")
        child = session("bb", model="MODEL", effort="high", herdr_attached=True, summary="NOWTEXT")
        for width in (60, 100, 168):
            rows = text(self.build([parent, child], width=width))
            where = [row for row in rows if "herdr" in row and "age unknown" in row]
            self.assertEqual([row.index("herdr") for row in where], [render._SESSION_DETAIL_COL] * 2)
            self.assertEqual([render._dw(row[:re.search(r"(?:\d+%|—) +(\S)", row).start(1)]) for row in where],
                             [render._NAME_COL] * 2)
            self.assertEqual(where[0][render._STEWARD_LINE_COL], "┆")
            self.assertEqual(where[1][render._STEWARD_LINE_COL], " ")
            if width != 168:
                elapsed = [row for row in rows if "3h 00m" in row and "MODEL" in row]
                self.assertEqual([row.index("3h 00m") for row in elapsed], [render._SESSION_DETAIL_COL] * 2)
                self.assertEqual([row.index("MODEL") for row in elapsed], [4 + render._HW] * 2)

    def test_single_row_between_identities_uses_the_same_dashed_mark(self):
        parent = session("aa", steward=True, steward_targets=[target("bb")])
        rows = text(self.build([parent, session("bb")], width=168))
        start = next(i for i, row in enumerate(rows) if "[aa]" in row)
        end = next(i for i, row in enumerate(rows) if "[bb]" in row)
        self.assertEqual(end - start, 2)
        # The flag rides the identity row, so the one row between still shows the line.
        self.assertIn("[aa] ⚑ ", rows[start])
        self.assertEqual(rows[start + 1][render._STEWARD_LINE_COL], "┆")

    def test_mixed_owner_frame_worker_columns_across_harnesses_and_widths(self):
        main = session("aa", model="MAINMODEL", effort="xhigh", ctx_pct=50,
                       context_window_tokens=1000000, herdr_attached=True)
        main.route_chain = {"visible": True, "nodes": [{"label": "MAINROUTE", "state": "open"}]}
        jobs = []
        for harness, model in (("claude", "Opus"), ("codex", "gpt-6.1-sol"), ("opencode", "gpt-6-luna")):
            for kind in ("owner", "frame", "stage"):
                key = {"owner": "code", "frame": "route-frame", "stage": "code-execute"}[kind]
                jobs.append(DispatchJob(key=key, slug=harness + "-" + kind, cwd=main.cwd,
                                        parent_sid="aa", is_child=True, depth=1, worker_type=kind,
                                        harness=harness, model=model, effort="xhigh", elapsed_min=17,
                                        capability_owner="autopilot-" + key, intensity="standard",
                                        stage="running", liveness="working", ctx_pct=50,
                                        work_projection=WorkProjection(source="route", stage_label="frame 0/1")
                                        if kind == "frame" else None))
        for width in (60, 100, 168):
            with self.subTest(width=width):
                lines = self.build([main], jobs, width)
                rows = text(lines)
                layout = render._layout_mode(width)
                anchor = render._session_routing_column(layout, render._wide_name_width(width))
                starts = []
                for row in lines:
                    pos = 0
                    for value, key in row or ():
                        if key == "name_dim" and value in ("code", "route-frame"):
                            starts.append(pos)
                        if not render._is_fill(value):
                            pos += render._dw(value)
                self.assertEqual(starts, [anchor] * 6)
                gauges = [render._dw(row[:row.index("━")]) for row in rows if "━" in row and "50%" in row]
                # MAIN + three OWNER/FRAME pairs + three in-card workers on the owner's column
                self.assertEqual(gauges.count(4 + render._HW), 10)
                self.assertEqual(sum("frame 0/1" in row for row in rows if "╰" in row), 3)
                self.assertFalse(any("frame 0/1" in row for row in rows if "╭" in row or "│" in row))
                self.assertTrue(all(row.index("╭") == render._SESSION_DETAIL_COL
                                    for row in rows if "╭" in row))


class SameRepositoryRoleTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo = root / "a" / "bundle"
        self.foreign = root / "b" / "bundle"  # Equal basenames are not equal repositories.
        (self.repo / ".git").mkdir(parents=True)
        (self.foreign / ".git").mkdir(parents=True)
        self.worktree = root / "linked"
        self.worktree.mkdir()
        linked = self.repo / ".git" / "worktrees" / "linked"
        linked.mkdir(parents=True)
        (self.worktree / ".git").write_text("gitdir: %s\n" % linked)
        self.unknown = root / "missing"
        self._native_implementation = steward._native_role_sessions
        native = mock.patch.object(steward, "_native_role_sessions", return_value=[])
        native.start()
        self.addCleanup(native.stop)
        relations = mock.patch.object(steward, "read_relations", return_value=[])
        relations.start()
        self.addCleanup(relations.stop)

    def row(self, harness, sid, cwd):
        return Session(harness=harness, pid=10, session_id=sid, session_tag=sid[:2],
                       cwd=str(cwd), liveness="idle", elapsed_min=1)

    def test_same_repo_worktree_cross_repo_and_unknown_agree_on_both_surfaces(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                parent = self.row(harness, "parent", self.repo)
                local = self.row("codex", "local", self.worktree)
                remote = self.row("opencode", "foreign", self.foreign)
                unknown = self.row("claude", "unknown", self.unknown)
                entries = {row.session_id: dict(target(row.session_id, row.harness),
                                               source="watch", kind="watch", ts=str(i))
                           for i, row in enumerate((remote, local, unknown))}
                markers = {(harness, "parent"): {"targets": entries}}
                rows = [parent, local, remote, unknown]
                with mock.patch.object(steward, "_registry_sessions", return_value=rows), \
                     mock.patch.object(steward, "_projection_sessions", return_value=rows), \
                     mock.patch.object(herdr_projection.os, "getcwd", return_value=str(self.repo)), \
                     mock.patch.object(steward, "read_markers", return_value=markers), \
                     mock.patch.object(herdr, "_clear_gpu_session_aliases", return_value=[]):
                    steward.enrich(rows)
                    self.assertEqual([t["session_id"] for t in parent.steward_targets], ["local"])
                    self.assertTrue(herdr_projection.is_steward(harness, "parent"))
                    self.assertIsNone(remote.steward_parents)
                    self.assertEqual(local.steward_parents[0]["session_id"], "parent")
                    entries.pop("local")
                    steward.enrich(rows)
                    self.assertFalse(parent.steward)
                    self.assertIsNone(parent.steward_targets)
                    self.assertFalse(herdr_projection.is_steward(harness, "parent"))
                    self.assertIsNone(local.steward_parents)

    def test_name_only_target_joins_existing_pane_metadata_without_new_probe(self):
        parent = self.row("claude", "parent", self.repo)
        child = self.row("opencode", "child", self.worktree)
        agents = [{"agent": "opencode", "pane_id": "w:p1", "name": "nested-supervisor",
                   "agent_session": {"agent": "opencode", "value": "child"}}]
        with mock.patch.object(herdr, "list_agents", side_effect=AssertionError("new probe")):
            herdr.enrich([child], agents=agents, pids=(set(), set()))
        markers = {("claude", "parent"): {"targets": {"named": {
            "harness": "opencode", "session_id": None, "name": "nested-supervisor",
            "source": "start", "kind": "start"}}}}
        with mock.patch.object(steward, "_registry_sessions", return_value=[]):
            steward.enrich([parent, child], markers=markers)
        self.assertTrue(parent.steward)
        self.assertEqual(parent.steward_targets[0]["session_id"], "child")
        self.assertEqual(render._steward_hierarchy([child, parent])[0], [parent, child])
        self.assertIsNone(markers[("claude", "parent")]["targets"]["named"]["session_id"])

    def test_worker_watch_of_its_start_parent_keeps_both_surfaces_and_branch(self):
        for parent_harness in ("claude", "codex", "opencode"):
            for worker_harness in ("claude", "codex", "opencode"):
                for named in (False, True):
                    with self.subTest(parent=parent_harness, worker=worker_harness, named=named):
                        parent = self.row(parent_harness, "parent", self.repo)
                        worker = self.row(worker_harness, "worker", self.worktree)
                        parent.session_aliases = ["old-parent"]
                        worker.session_aliases = ["old-worker"]
                        parent._herdr_name, worker._herdr_name = "launcher-name", "worker-name"
                        parent_sid, worker_sid = ("old-parent", "old-worker") if named else ("parent", "worker")
                        start = dict(harness=worker_harness, session_id=None if named else worker_sid,
                                     name="worker-name", source="start", kind="start", ts="2026-10-09T01:00:00Z")
                        watch = dict(target(parent_sid, parent_harness), source="watch", kind="watch",
                                     ts="2026-10-09T02:00:00Z")
                        markers = {(parent_harness, parent_sid): {"targets": {"worker": start}},
                                   (worker_harness, worker_sid): {"targets": {"parent": watch}}}
                        before = copy.deepcopy(markers)
                        rows = [worker, parent]
                        with mock.patch.object(steward, "_registry_sessions", return_value=[]), \
                             mock.patch.object(steward, "_projection_sessions", return_value=rows), \
                             mock.patch.object(steward, "read_markers", return_value=markers), \
                             mock.patch.object(herdr, "_clear_gpu_session_aliases", side_effect=
                                              lambda h, sid, pane, cwd=None:
                                              ["old-" + sid] if named else []), \
                             mock.patch.object(herdr_projection.os, "getcwd", return_value=str(self.repo)):
                            steward.enrich(rows)
                            self.assertTrue(parent.steward)
                            self.assertFalse(worker.steward)
                            self.assertEqual([t["session_id"] for t in parent.steward_targets], ["worker"])
                            self.assertEqual(worker.steward_parents[0]["session_id"], "parent")
                            self.assertTrue(herdr_projection.is_steward(parent_harness, "parent"))
                            self.assertFalse(herdr_projection.is_steward(worker_harness, "worker"))
                            shown = text(render._build_lines(rows, [], "fleet", False, 0,
                                                             layout="wide", term_width=168))
                            self.assertLess(next(i for i, line in enumerate(shown) if "[pa]" in line),
                                            next(i for i, line in enumerate(shown) if "[wo]" in line))
                        self.assertTrue(any("⚑" in line for line in shown))
                        self.assertEqual(markers, before)  # the real watch remains recorded

    def test_ordinary_watch_and_other_worker_targets_still_grant_the_role(self):
        parent = self.row("claude", "parent", self.repo)
        worker = self.row("opencode", "worker", self.worktree)
        other = self.row("codex", "other", self.repo)
        markers = {("opencode", "worker"): {"targets": {
            "parent": dict(target("parent", "claude"), source="watch")}}}
        with mock.patch.object(steward, "_registry_sessions", return_value=[]):
            steward.enrich([parent, worker], markers=markers)
            self.assertTrue(worker.steward)  # no start relation: an ordinary watch
            markers[("claude", "parent")] = {"targets": {
                "worker": dict(target("worker", "opencode"), source="start")}}
            markers[("opencode", "worker")]["targets"]["other"] = dict(target("other"), source="watch")
            steward.enrich([parent, worker, other], markers=markers)
        self.assertTrue(worker.steward)
        self.assertEqual([t["session_id"] for t in worker.steward_targets], ["other"])
        self.assertEqual(other.steward_parents[0]["session_id"], "worker")

    def test_legacy_watch_kind_follows_the_same_reverse_watch_rule(self):
        parent = self.row("codex", "parent", self.repo)
        worker = self.row("claude", "worker", self.worktree)
        markers = {("codex", "parent"): {"targets": {
            "worker": dict(target("worker", "claude"), source="start", kind="start")}},
            ("claude", "worker"): {"targets": {
                "parent": dict(target("parent"), kind="watch")}}}
        with mock.patch.object(steward, "_registry_sessions", return_value=[]):
            steward.enrich([parent, worker], markers=markers)
        self.assertTrue(parent.steward)
        self.assertFalse(worker.steward)
        self.assertEqual(worker.steward_parents[0]["session_id"], "parent")

    def test_start_parent_is_remembered_after_it_watches_the_same_known_sid(self):
        mod = steward._peer_message_module()
        parent = self.row("claude", "parent", self.repo)
        worker = self.row("opencode", "worker", self.worktree)
        with mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": str(self.repo / "state")}):
            mod.mark_steward("claude", "parent", target("worker", "opencode"),
                             "start", "2026-10-09T01:00:00Z", source="start")
            mod.mark_steward("claude", "parent", target("worker", "opencode"),
                             "watch", "2026-10-09T02:00:00Z", source="watch")
            mod.mark_steward("opencode", "worker", target("parent", "claude"),
                             "watch", "2026-10-09T03:00:00Z", source="watch")
            markers = mod.read_steward_markers([str(self.repo / "state")])
        self.assertEqual(markers[("claude", "parent")]["targets"]["worker"]["source"], "watch")
        self.assertEqual(markers[("claude", "parent")]["targets"]["worker"]["start_ts"],
                         "2026-10-09T01:00:00Z")
        with mock.patch.object(steward, "_registry_sessions", return_value=[]):
            steward.enrich([parent, worker], markers=markers)
        self.assertTrue(parent.steward)
        self.assertFalse(worker.steward)
        self.assertEqual(worker.steward_parents[0]["session_id"], "parent")
        shown = text(render._build_lines([worker, parent], [], "fleet", False, 0,
                                        layout="wide", term_width=168))
        self.assertLess(next(i for i, line in enumerate(shown) if "[pa]" in line),
                        next(i for i, line in enumerate(shown) if "[wo]" in line))
        self.assertTrue(any("⚑" in line for line in shown))

    def test_start_origin_survives_explicit_update_and_name_only_sid_completion(self):
        mod = steward._peer_message_module()
        with mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": str(self.repo / "state")}):
            mod.mark_steward("claude", "parent", {"harness": "opencode", "name": "named",
                             "pane": "w:p1", "session_id": None},
                             "start", "2026-10-09T01:00:00Z", source="start")
            mod.mark_steward("claude", "parent", {"harness": "opencode", "name": "named",
                             "pane": "w:p1", "session_id": "worker"},
                             "watch", "2026-10-09T02:00:00Z", source="watch")
            mod.mark_steward("claude", "parent", {"harness": "opencode", "name": "named",
                             "pane": "w:p1", "session_id": "worker"},
                             "explicit", "2026-10-09T03:00:00Z", source="explicit")
            marker = mod.read_steward_markers([str(self.repo / "state")])[("claude", "parent")]
        self.assertEqual(set(marker["targets"]), {"worker"})
        entry = marker["targets"]["worker"]
        self.assertEqual((entry["source"], entry["ts"], entry["start_ts"]),
                         ("explicit", "2026-10-09T03:00:00Z", "2026-10-09T01:00:00Z"))

    def test_pane_projection_reuses_current_herdr_names_over_stale_native_metadata(self):
        parent = self.row("opencode", "parent", self.repo)
        child = self.row("codex", "child", self.worktree)
        child._herdr_name = "child-pane"
        stale = self.row("codex", "child", self.foreign)
        markers = {("opencode", "parent"): {"targets": {"named": {
            "harness": "codex", "name": "child-pane", "source": "start"}}}}
        agents = [{"agent": "codex", "cwd": str(self.worktree), "name": "child-pane",
                   "agent_session": {"agent": "codex", "value": "child"}}]
        with mock.patch.object(steward, "_registry_sessions", return_value=[stale]), \
             mock.patch.object(steward, "read_markers", return_value=markers), \
             mock.patch.object(herdr, "list_agents", return_value=agents) as metadata, \
             mock.patch.object(herdr, "pane_pids", side_effect=AssertionError("role probe")), \
             mock.patch.object(herdr, "_clear_gpu_session_aliases", return_value=[]), \
             mock.patch.object(herdr_projection.os, "getcwd", return_value=str(self.repo)):
            steward.enrich([parent, child])
            metadata.assert_not_called()  # Fleet already holds the normal snapshot.
            self.assertTrue(parent.steward)
            self.assertTrue(herdr_projection.is_steward("opencode", "parent"))
            metadata.assert_called_once()

    def test_live_repository_overrides_stale_registry_and_clear_alias_joins_both_directions(self):
        parent = self.row("claude", "parent", self.repo)
        child = self.row("opencode", "child-new", self.worktree)
        child._gpu_session_aliases = ["child-old"]
        stale = self.row("opencode", "child-old", self.foreign)
        markers = {("claude", "parent"): {"targets": {"child": {
            "harness": "opencode", "session_id": "child-old", "source": "watch"}}}}
        with mock.patch.object(steward, "_registry_sessions", return_value=[stale]):
            steward.enrich([parent, child], markers=markers)
        self.assertTrue(parent.steward)
        self.assertEqual(child.steward_parents[0]["session_id"], "parent")
        self.assertEqual(render._steward_hierarchy([child, parent])[0], [parent, child])

    def test_newest_same_repo_claim_hands_over_target_without_rewriting_markers(self):
        for harness in ("claude", "codex", "opencode"):
            root = self.row("claude", "root", self.repo)
            inner = self.row(harness, "inner", self.worktree)
            child = self.row("codex", "child-new", self.worktree)
            child.session_aliases = ["child-old"]
            foreign = self.row("opencode", "foreign", self.foreign)
            rows = [root, inner, child, foreign]
            markers = {
                ("claude", "root"): {"targets": {
                    "inner": dict(target("inner", harness), source="start", ts="2026-10-08T01:00:00Z"),
                    "child": dict(target("child-old"), source="start", ts="2026-10-08T02:00:00Z")}},
                (harness, "inner"): {"targets": {
                    "child": dict(target("child-new"), source="watch", ts="2026-10-08T03:00:00Z")}},
                ("opencode", "foreign"): {"targets": {
                    "child": dict(target("child-new"), source="explicit", ts="2026-10-08T04:00:00Z")}},
            }
            before = copy.deepcopy(markers)
            with mock.patch.object(steward, "_registry_sessions", return_value=[]), \
                 mock.patch.object(steward, "_projection_sessions", return_value=rows), \
                 mock.patch.object(steward, "read_markers", return_value=markers), \
                 mock.patch.object(herdr, "_clear_gpu_session_aliases", return_value=[]), \
                 mock.patch.object(herdr_projection.os, "getcwd", return_value=str(self.repo)):
                steward.enrich(rows)
                self.assertIsNone(root.steward_targets)
                self.assertFalse(root.steward)
                self.assertEqual([t["session_id"] for t in inner.steward_targets], ["child-new"])
                self.assertEqual(child.steward_parents[0]["session_id"], "inner")
                self.assertEqual(len(child.steward_parents), 1)
                self.assertIsNone(inner.steward_parents)
                self.assertEqual(steward.role_targets("claude", "root", markers=markers, sessions=rows), [])
                self.assertFalse(herdr_projection.is_steward("claude", "root"))
                self.assertFalse(foreign.steward)
                self.assertTrue(herdr_projection.is_steward(harness, "inner"))
                self.assertEqual(markers, before)
                # A later same-repository claim hands back without refusing either caller.
                markers[("claude", "root")]["targets"]["child"]["ts"] = "2026-10-08T05:00:00Z"
                steward.enrich(rows)
                self.assertEqual(child.steward_parents[0]["session_id"], "root")
                self.assertFalse(inner.steward)
                self.assertFalse(herdr_projection.is_steward(harness, "inner"))

    def test_latest_claim_compares_timestamp_instants_and_ties_are_stable(self):
        root = self.row("claude", "root", self.repo)
        inner = self.row("opencode", "inner", self.worktree)
        child = self.row("codex", "child", self.repo)
        rows = [root, inner, child]
        markers = {
            ("claude", "root"): {"targets": {"child": dict(target("child"), source="start",
                                                          ts="2026-10-08T04:00:00Z")}},
            ("opencode", "inner"): {"targets": {"child": dict(target("child"), source="watch",
                                                              ts="2026-10-08T05:30:00+02:00")}},
        }
        self.assertEqual(len(steward.role_targets("claude", "root", markers=markers, sessions=rows)), 1)
        self.assertEqual(steward.role_targets("opencode", "inner", markers=markers, sessions=rows), [])
        markers[("opencode", "inner")]["targets"]["child"]["ts"] = "2026-10-08T06:00:00+02:00"
        for ordered in (markers, dict(reversed(list(markers.items())))):
            self.assertEqual(len(steward.role_targets("opencode", "inner", markers=ordered, sessions=rows)), 1)

    def test_missing_actor_directory_is_read_from_exact_native_metadata_only(self):
        from fleet.collectors import codex, opencode
        db = self.repo / "native.sqlite"
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE session (id TEXT, directory TEXT)")
            con.executemany("INSERT INTO session VALUES (?,?)",
                            [("wanted", str(self.repo)), ("unrelated", str(self.foreign))])
        implementation = self._native_implementation
        with mock.patch.object(opencode, "_db", return_value=str(db)), \
             mock.patch.object(codex, "_state_db", return_value=None):
            rows = implementation({("opencode", "wanted"): {"targets": {}}}, [])
        self.assertEqual([(row.session_id, row.cwd) for row in rows], [("wanted", str(self.repo))])

    def test_target_only_native_metadata_keeps_role_and_current_metadata_still_wins(self):
        from fleet.collectors import codex, opencode
        db = self.repo / "targets.sqlite"
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE session (id TEXT, directory TEXT)")
            con.execute("CREATE TABLE threads (id TEXT, cwd TEXT)")
            for table in ("session", "threads"):
                con.executemany("INSERT INTO %s VALUES (?,?)" % table,
                                [("target-only", str(self.worktree)),
                                 ("unrelated", str(self.repo)), ("communication-only", str(self.repo))])
        connect = sqlite3.connect
        for harness in ("codex", "opencode"):
            with self.subTest(harness=harness):
                parent = self.row("claude", "parent", self.repo)
                markers = {("claude", "parent"): {"targets": {
                    "target": dict(target("target-only", harness), source="watch"),
                    "message": dict(target("communication-only", harness), source="steer"),
                }}}
                rows = [parent]
                queries = []

                def readonly(*args, **kwargs):
                    self.assertIn("mode=ro", args[0])
                    self.assertTrue(kwargs["uri"])
                    connection = connect(*args, **kwargs)
                    connection.set_trace_callback(queries.append)
                    return connection

                with mock.patch.object(steward, "_native_role_sessions", side_effect=self._native_implementation), \
                     mock.patch.object(steward, "_registry_sessions", return_value=rows), \
                     mock.patch.object(steward, "_projection_sessions", return_value=rows), \
                     mock.patch.object(steward, "read_markers", return_value=markers), \
                     mock.patch.object(opencode, "_db", return_value=str(db)), \
                     mock.patch.object(codex, "_state_db", return_value=str(db)), \
                     mock.patch.object(herdr, "_clear_gpu_session_aliases", return_value=[]), \
                     mock.patch.object(herdr_projection.os, "getcwd", return_value=str(self.repo)), \
                     mock.patch.object(sqlite3, "connect", side_effect=readonly):
                    steward.enrich(rows)
                    self.assertTrue(parent.steward)
                    self.assertEqual(parent.steward_targets[0]["session_id"], "target-only")
                    self.assertTrue(herdr_projection.is_steward("claude", "parent"))
                    self.assertTrue(any("WHERE id='target-only'" in query for query in queries))
                    self.assertFalse(any("unrelated" in query or "communication-only" in query
                                         for query in queries))
                    rows.append(self.row(harness, "target-only", self.foreign))
                    queries.clear()
                    steward.enrich(rows)
                    self.assertFalse(parent.steward)
                    self.assertFalse(herdr_projection.is_steward("claude", "parent"))
                    self.assertEqual(queries, [])

    def test_duplicate_name_with_missing_identity_cannot_bind_either_surface(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                parent = self.row(harness, "parent", self.repo)
                known = self.row("codex", "known", self.worktree)
                unresolved = self.row("codex", "temporary", self.repo)
                known._herdr_name = unresolved._herdr_name = "duplicate-name"
                unresolved.session_id = None
                rows = [parent, known, unresolved]
                markers = {(harness, "parent"): {"targets": {"name": {
                    "harness": "codex", "name": "duplicate-name", "source": "start"}}}}
                with mock.patch.object(steward, "_registry_sessions", return_value=[]), \
                     mock.patch.object(steward, "_projection_sessions", return_value=rows), \
                     mock.patch.object(steward, "read_markers", return_value=markers), \
                     mock.patch.object(herdr, "_clear_gpu_session_aliases", return_value=[]), \
                     mock.patch.object(herdr_projection.os, "getcwd", return_value=str(self.repo)):
                    steward.enrich(rows)
                    self.assertFalse(parent.steward)
                    self.assertIsNone(known.steward_parents)
                    self.assertFalse(herdr_projection.is_steward(harness, "parent"))
                    # The existing uniquely identified name remains a valid relation.
                    rows.remove(unresolved)
                    steward.enrich(rows)
                    self.assertTrue(parent.steward)
                    self.assertEqual(known.steward_parents[0]["session_id"], "parent")
                    self.assertTrue(herdr_projection.is_steward(harness, "parent"))

    def test_pane_metadata_reuses_native_seat_and_rejects_foreign_project(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "utilities"))
        import session_tidy
        record = {"sid": "native-id", "cwd": str(self.worktree)}
        with mock.patch.object(session_tidy, "latest_session", return_value=record), \
             mock.patch.object(herdr, "pane_pids", side_effect=AssertionError("process probe")):
            self.assertEqual(herdr.pane_session_metadata("opencode", "w:p1", str(self.repo)), record)
            self.assertIsNone(herdr.pane_session_metadata("opencode", "w:p1", str(self.foreign)))


if __name__ == "__main__":
    unittest.main()
