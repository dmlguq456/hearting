"""A sealed ``route-frame`` route shows its frame legs at owner level.

The fixture mirrors a real route file: ``capability: route-frame``,
``capability_mode: default``, ``execution_topology: staged`` and the framed
shape in ``selection.shape``. Only the direct ``_draw_route_progress`` case
still uses the older synthetic ``capability: frame`` / ``framed`` shape.
"""
import unittest
from unittest.mock import patch

from tools.fleet.explorer import live
from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.tables import _harness_mix
from tools.fleet.explorer.view_model import ExplorerViewModel
from tools.fleet.tests import explorer_fixture as ef


NOW = 1_000_000.0
ROUTE_ID = "rt-frame"
STAGED_ID = "rt-staged"
REPO = "/synthetic/repo"
FRAME_LEGS = ("frame", "frame-alternative")


def _frame_job(node, harness, model, effort, running=True):
    """A frame leg job with the field names the dispatch registry writes."""
    return {"key": "route-frame", "depth": 1, "dispatch_depth": 1, "route_node": node,
            "worker_type": "frame", "worker_mode": "plan/frame", "unit": "plan/frame",
            "assigned_contract": "plan/frame", "capability_owner": "route-frame",
            "liveness": "working" if running else "done", "harness": harness,
            "model": model, "resolved_model": None, "effort": effort,
            "route_id": ROUTE_ID, "attempt_id": "att-" + node,
            "elapsed_min": 5 if node == "frame" else 4, "route_attempt_round": 1}


def _owner_job(attempt, harness, model="opus", effort="max", liveness="working"):
    """An owner attempt job (the route's real owner, not a frame leg)."""
    return {"key": "route-frame", "depth": 1, "dispatch_depth": 1, "route_node": None,
            "worker_type": "owner", "unit": "_kernel/owner", "worker_mode": None,
            "harness": harness, "model": model, "resolved_model": None, "effort": effort,
            "route_id": ROUTE_ID, "attempt_id": attempt, "elapsed_min": 11}


def _view(running=False, node_state=None, live_legs=FRAME_LEGS):
    node_state = node_state or ("active" if running else "done")
    return {
        "nodes": [
            {"id": "frame", "state": node_state, "depends_on": [], "parallel_group": "frame",
             "unit": "plan/frame", "worker_type": "frame"},
            {"id": "frame-alternative", "state": node_state, "depends_on": [],
             "parallel_group": "frame", "unit": "plan/frame", "worker_type": "frame"},
            {"id": "route-decision", "state": "todo" if running else node_state,
             "depends_on": ["frame", "frame-alternative"], "gate": "route-decision"},
        ],
        "capability": "route-frame", "capability_mode": "default",
        "execution_topology": "staged", "effective_intensity": "standard",
        "cwd": REPO, "slug": "framed subject",
    }


def _record(owner=None):
    record = {"route_id": ROUTE_ID, "capability": "route-frame",
              "capability_mode": "default", "execution_topology": "staged",
              "selection": {"shape": "framed"}, "effective_intensity": "standard",
              "result": None, "slug": "framed subject"}
    if owner is not None:
        record["owner_attempt_id"] = "att-owner"
    return record


def _vm(running=False, owner=None, attempts=None, node_state=None, chain=None,
        extra_owners=(), live_legs=FRAME_LEGS, owner_session=None):
    view = _view(running, node_state, live_legs)
    record = _record(owner)
    jobs = [_frame_job("frame", "claude", "opus", "max", "frame" in live_legs),
            _frame_job("frame-alternative", "codex", "gpt-6-astra", None,
                       "frame-alternative" in live_legs)]
    if owner is not None:
        jobs.append(_owner_job("att-owner", owner.get("harness", ""),
                               owner.get("model", "opus"), owner.get("effort", "max")))
    jobs.extend(extra_owners)
    session = {"harness": "claude", "session_id": "sid-owner", "session_tag": "z9",
               "cwd": REPO, "liveness": "idle", "is_child": False, "title": "owner"}
    session.update(owner_session or {})
    if chain is not None:
        session["route_chain"] = {"nodes": chain}
    slot = {"view": view, "record": record, "jobs": jobs, "sessions": [session]}
    with patch.object(live, "_collect_routes", return_value={ROUTE_ID: slot}):
        rows, index = live._session_records({"sessions": [session]}, NOW, lambda _cwd: "repo")
        routes, nodes = live._route_records({"resources": []}, rows, index,
                                            lambda _cwd: "repo", NOW)
    mapping = {"repos": [{"id": "repo", "name": "repo"}],
               "sessions": rows, "routes": routes, "nodes": nodes}
    if attempts is not None:
        mapping["attempts"] = attempts
    vm = ExplorerViewModel.from_mapping(mapping)
    return view, record, routes[0], vm


def _staged_after_frame_vm():
    """A normal staged route whose session chain starts with a finished frame route."""
    view = {
        "nodes": [{"id": "execute", "state": "active", "depends_on": [],
                   "unit": "dev/backend", "worker_type": "stage"}],
        "capability": "autopilot-code", "capability_mode": "dev",
        "execution_topology": "staged", "effective_intensity": "standard",
        "cwd": REPO, "slug": "staged subject",
    }
    record = {"route_id": STAGED_ID, "capability": "autopilot-code",
              "capability_mode": "dev", "execution_topology": "staged",
              "selection": {"shape": "staged"}, "effective_intensity": "standard",
              "owner_attempt_id": "att-owner", "result": None, "slug": "staged subject"}
    jobs = [{"key": "route-staged", "depth": 1, "dispatch_depth": 1, "route_node": None,
             "worker_type": "owner", "unit": "_kernel/owner", "worker_mode": None,
             "harness": "claude", "model": "claude-opus-5-5", "resolved_model": None,
             "effort": "xhigh", "route_id": STAGED_ID, "attempt_id": "att-owner",
             "elapsed_min": 11}]
    chain = [{"route_id": "rt-prev", "capability": "route-frame", "state": "done"},
             {"route_id": STAGED_ID, "capability": "autopilot-code", "state": "active"}]
    session = {"harness": "claude", "session_id": "sid-owner", "session_tag": "z9",
               "cwd": REPO, "liveness": "idle", "is_child": False, "title": "owner",
               "route_chain": {"nodes": chain}}
    slot = {"view": view, "record": record, "jobs": jobs, "sessions": [session]}
    with patch.object(live, "_collect_routes", return_value={STAGED_ID: slot}):
        rows, index = live._session_records({"sessions": [session]}, NOW, lambda _cwd: "repo")
        routes, nodes = live._route_records({"resources": []}, rows, index,
                                            lambda _cwd: "repo", NOW)
    mapping = {"repos": [{"id": "repo", "name": "repo"}],
               "sessions": rows, "routes": routes, "nodes": nodes}
    return routes[0], nodes[STAGED_ID], ExplorerViewModel.from_mapping(mapping)


class FrameOwnerLevelTests(unittest.TestCase):
    def test_a_sealed_route_frame_record_displays_as_frame_without_a_default_suffix(self):
        view, record, route, vm = _vm()
        self.assertEqual(route["capability"], "frame")
        self.assertEqual(route["shape"], "framed")
        self.assertIsNone(route["mode"])
        self.assertIsNone(route["intensity"])
        self.assertEqual(route["legs"], [["frame", "done", "3/3"]])
        # The source record is only read: collectors, pins and fold inputs see it unchanged.
        self.assertEqual(view["capability"], "route-frame")
        self.assertEqual(view["capability_mode"], "default")
        self.assertEqual(view["execution_topology"], "staged")
        self.assertEqual(record["selection"]["shape"], "framed")
        self.assertEqual(record["capability_mode"], "default")
        self.assertTrue(all("harness" not in node for node in view["nodes"]))
        for cols in (120, 180):
            with self.subTest(cols=cols):
                frame, trace = ef.trace_draw(draw, vm, NavState(view="ROUTES", title="ROUTES"),
                                             cols, 60)
                self.assertEqual(ef.frame_violations(frame, trace, cols, 60), [])
                pane = ["".join(cell[0] for cell in row[34:]) for row in frame.cells]
                row = next(line for line in pane if "[z9]" in line)
                self.assertNotIn("route-frame", row)
                self.assertNotIn("(def", row)
                self.assertNotIn("frame(", row)
                self.assertIn("frame", row)

    def test_owner_telemetry_is_borrowed_from_the_frame_legs_without_mutating_the_record(self):
        view, _record_value, route, _vm_value = _vm()
        # Both legs finished: the newest leg (the alternative, started later) names the
        # owner-level cell, and its missing effort leaves no `·` behind.
        self.assertEqual(route["harness"], "codex")
        self.assertEqual(route["model"][0], "gpt-6-astra")
        self.assertIsNone(route["effort"])
        self.assertEqual(route["legs"], [["frame", "done", "3/3"]])
        self.assertEqual([node.get("id") for node in view["nodes"]],
                         ["frame", "frame-alternative", "route-decision"])
        self.assertTrue(all("harness" not in node for node in view["nodes"]))

    def test_a_frame_leg_with_an_alias_model_shows_the_resolved_name(self):
        _view_value, _record_value, route, vm = _vm(live_legs=("frame",))
        self.assertEqual(route["harness"], "claude")
        self.assertEqual(route["model"][0], "Opus")
        self.assertEqual(route["effort"], "max")
        for cols in (120, 180):
            with self.subTest(cols=cols):
                text = "\n".join(draw(vm, NavState(view="ROUTES", title="ROUTES",
                                                  folds=(("ropen:ROUTES", ROUTE_ID),)),
                                      cols, 60).text_rows())
                self.assertIn("frame  claude Opus max", text)
                self.assertIn("alt  codex gpt-6-astra", text)
                self.assertNotIn("gpt-6-astra·", text)
                self.assertNotIn("·)", text)

    def test_route_row_says_frame_and_the_owner_cell_is_not_unknown(self):
        _view_value, _record_value, _route, vm = _vm(running=True, live_legs=("frame",))
        for cols in (120, 180):
            with self.subTest(cols=cols):
                frame, trace = ef.trace_draw(draw, vm, NavState(view="ROUTES", title="ROUTES"),
                                             cols, 60)
                self.assertEqual(ef.frame_violations(frame, trace, cols, 60), [])
                pane = ["".join(cell[0] for cell in row[34:]) for row in frame.cells]
                row = next(line for line in pane if "[z9]" in line)
                self.assertIn("frame", row)
                self.assertNotIn("worker", row)
                self.assertIn("claude Opus max", row)
                # The frame body line unfolds under the one-line row.
                opened = draw(vm, NavState(view="ROUTES", title="ROUTES",
                                           folds=(("ropen:ROUTES", ROUTE_ID),)), cols, 60)
                pane = ["".join(cell[0] for cell in row[34:]) for row in opened.cells]
                self.assertTrue(any("└ frame" in line for line in pane), pane[:12])
        wide = next(line for line in draw(vm, NavState(view="ROUTES", title="ROUTES"),
                                          220, 60).text_rows()
                    if "[z9]" in line and "frame" in line)
        self.assertIn("claude Opus max", wide)

    def test_wide_routes_owner_cell_has_no_separator_without_effort_or_model(self):
        # 435 of 731 frame jobs carry no effort row: the wide owner cell must read
        # `codex gpt-6-astra`, and a missing model falls back to `harness effort`.
        _view_value, _record_value, route, vm = _vm()
        self.assertIsNone(route["effort"])
        for cols in (186, 220):
            with self.subTest(cols=cols, case="no effort"):
                text = "\n".join(draw(vm, NavState(view="ROUTES", title="ROUTES"),
                                      cols, 60).text_rows())
                row = next(line for line in text.splitlines()
                           if "[z9]" in line and "frame" in line)
                self.assertIn("codex gpt-6-astra", row)
                self.assertNotIn("gpt-6-astra·", row)
                self.assertNotIn("(", row.split("frame", 1)[0])
        _view_value, _record_value, _route, vm = _vm(
            owner={"harness": "claude", "model": None, "effort": "xhigh"})
        for cols in (186, 220):
            with self.subTest(cols=cols, case="no model"):
                text = "\n".join(draw(vm, NavState(view="ROUTES", title="ROUTES"),
                                      cols, 60).text_rows())
                row = next(line for line in text.splitlines()
                           if "[z9]" in line and "frame" in line)
                self.assertIn("claude xhigh", row)
                self.assertNotIn("(·", row)

    def test_the_frame_band_says_frame_for_a_borrowed_identity_and_owner_for_a_real_attempt(self):
        _view_value, _record_value, _route, borrowed = _vm()
        for cols in (120, 180):
            with self.subTest(cols=cols, case="borrowed"):
                text = "\n".join(draw(borrowed, NavState(
                    view="ROUTES", title="ROUTES",
                    folds=(("ropen:ROUTES", ROUTE_ID),)), cols, 60).text_rows())
                band = next(line for line in text.splitlines()
                            if "frame ∥ frame-alternative" in line and "┴─" in line)
                self.assertNotIn("owner", band)
                if cols == 180:
                    self.assertIn("rt-frame · frame codex gpt-6-astra", band)
        _view_value, _record_value, _route, owned = _vm(
            owner={"harness": "opencode", "model": "glm-5.2", "effort": "medium"})
        for cols in (120, 180):
            with self.subTest(cols=cols, case="owner"):
                text = "\n".join(draw(owned, NavState(
                    view="ROUTES", title="ROUTES",
                    folds=(("ropen:ROUTES", ROUTE_ID),)), cols, 60).text_rows())
                band = next(line for line in text.splitlines()
                            if "frame ∥ frame-alternative" in line and "┴─" in line)
                self.assertIn("rt-frame · owner opencode", band)
                if cols == 180:
                    self.assertIn("opencode glm-5.2 med", band)

    def test_both_frame_legs_show_their_own_harness_and_model(self):
        _view_value, _record_value, _route, vm = _vm()
        groups = vm.nodes[ROUTE_ID]
        self.assertEqual([group.get("kind") for group in groups], ["frame"])
        self.assertEqual(groups[0].get("n"), "frame ∥ frame-alternative")
        legs = [line[0][0].strip() for line in groups[0].get("lines") or ()]
        self.assertEqual(legs, ["frame", "alt"])
        for cols in (120, 180):
            with self.subTest(cols=cols):
                frame, trace = ef.trace_draw(
                    draw, vm, NavState(view="ROUTES", title="ROUTES",
                                       folds=(("ropen:ROUTES", ROUTE_ID),)), cols, 60)
                self.assertEqual(ef.frame_violations(frame, trace, cols, 60), [])
                text = "\n".join(frame.text_rows())
                self.assertIn("frame  claude Opus max", text)
                self.assertIn("alt  codex gpt-6-astra", text)
                self.assertNotIn("gpt-6-astra·", text)
                self.assertNotIn("·)", text)

    def test_tree_route_line_uses_the_frame_harness_and_mix_counts_the_owner_once(self):
        _view_value, _record_value, route, vm = _vm(running=True)
        frame = draw(vm, NavState(view="ROUTES", title="ROUTES"), 140, 60)
        tree = "\n".join("".join(cell[0] for cell in row[:34]) for row in frame.cells)
        self.assertIn("frame: codex", tree)
        mix = "".join(text for text, _key in _harness_mix([route]))
        self.assertIn("codex 1", mix)
        self.assertIn("claude 0", mix)

    def test_a_framed_route_with_an_owner_attempt_keeps_the_owner_raw_box(self):
        attempts = {"route": ROUTE_ID, "att": "att-owner",
                    "lines": ["assistant: hello", "tool Bash: ls"]}
        _view_value, _record_value, route, vm = _vm(
            owner={"harness": "opencode", "model": "glm-5.2", "effort": "medium"},
            attempts=attempts)
        self.assertEqual(route["harness"], "opencode")
        self.assertEqual(route["model"][0], "glm-5.2")
        for cols in (120, 180):
            with self.subTest(cols=cols):
                state = NavState(view="ROUTES", title="ROUTES",
                                 folds=(("ropen:ROUTES", ROUTE_ID),))
                frame, trace = ef.trace_draw(draw, vm, state, cols, 60)
                self.assertEqual(ef.frame_violations(frame, trace, cols, 60), [])
                text = "\n".join(frame.text_rows())
                self.assertEqual(text.count("owner raw"), 1)
                self.assertIn("attempt-view att-owner", text)
                self.assertIn("● hello", text)
                self.assertIn("● Bash  ls", text)
                self.assertEqual(text.count("frame-review and the legs after it"), 1)
                self.assertEqual(text.count("frame  claude Opus max"), 1)
                self.assertEqual(text.count("alt  codex gpt-6-astra"), 1)
                lines = text.splitlines()
                band = next(i for i, line in enumerate(lines)
                            if "frame ∥ frame-alternative" in line and "┌" in line)
                self.assertIn("frame  claude", lines[band + 1])

    def test_route_row_shows_the_owners_own_state_while_its_legs_run(self):
        """3cffaf: an owner that handed the stage to workers and waits does not spin."""
        wait = {"pid": 9, "comm": "sleep", "kind": "wait", "etime_s": 600}
        work = {"pid": 9, "comm": "python3", "kind": "work", "etime_s": 600}
        for owner_session, waits in (({"liveness": "idle"}, True),
                                     ({"liveness": "working", "exec_child": wait}, True),
                                     ({"liveness": "working", "exec_child": work}, False),
                                     ({"liveness": "working"}, False)):
            with self.subTest(owner=owner_session):
                _view_value, _record_value, route, vm = _vm(running=True,
                                                            owner_session=owner_session)
                self.assertIs(route["owner_waits"], waits)
                for view in ("ROUTES", "OVERVIEW"):
                    text = "\n".join(draw(vm, NavState(view=view, title=view), 180, 60).text_rows())
                    row = next(line for line in text.splitlines()
                               if "[z9]" in line and "frame" in line and "│" in line)
                    if waits:
                        self.assertNotIn("⠏", row)
                    else:
                        self.assertIn("⠏", row)
                    if view == "ROUTES":
                        self.assertIn(("●" if waits else "⠏") + " [z9]", row)
        _view_value, _record_value, route, _vm_value = _vm(owner_session={"liveness": "idle"})
        self.assertIs(route["owner_waits"], False)          # nothing runs: not a delegation

    def test_framed_progress_mark_follows_the_frame_legs(self):
        _view_value, _record_value, _route, running = _vm(running=True)
        _view_value, _record_value, _route, finished = _vm()
        _view_value, _record_value, _route, failed = _vm(node_state="failed")
        for cols in (120, 180):
            with self.subTest(cols=cols):
                text = "\n".join(draw(running, NavState(view="ROUTES", title="ROUTES"),
                                      cols, 60).text_rows())
                row = next(line for line in text.splitlines() if "[z9]" in line and "frame" in line)
                self.assertIn("frame ●", row)
                self.assertIn("∥ frame 2-way", row)
                text = "\n".join(draw(finished, NavState(view="ROUTES", title="ROUTES"),
                                      cols, 60).text_rows())
                row = next(line for line in text.splitlines() if "[z9]" in line and "frame" in line)
                self.assertIn("frame ✓", row)
                self.assertIn("∥ frame 2-way", row)
                text = "\n".join(draw(failed, NavState(view="ROUTES", title="ROUTES"),
                                      cols, 60).text_rows())
                row = next(line for line in text.splitlines() if "[z9]" in line and "frame" in line)
                self.assertIn("frame ✕", row)

    def test_framed_progress_marks_the_routes_own_frame_leg_after_a_predecessor(self):
        chain = [{"route_id": "rt-prev", "capability": "route-frame", "state": "done"},
                 {"route_id": ROUTE_ID, "capability": "route-frame", "state": "active"}]
        for kwargs, expected in (({"running": True}, "frame ●"),
                                 ({"node_state": "failed"}, "frame ✕")):
            with self.subTest(expected=expected):
                _view_value, _record_value, route, vm = _vm(chain=chain, **kwargs)
                self.assertEqual([leg[0] for leg in route["legs"]], ["frame", "frame"])
                for cols in (120, 180):
                    text = "\n".join(draw(vm, NavState(view="ROUTES", title="ROUTES"),
                                          cols, 60).text_rows())
                    row = next(line for line in text.splitlines()
                               if "[z9]" in line and "frame" in line)
                    self.assertIn(expected, row)
                    self.assertNotIn("frame ✓", row)

    def test_the_legacy_synthetic_frame_shape_draws_the_same_progress_suffix(self):
        # Render-only unit case: the pre-collector synthetic shape is not a record the
        # collector emits, so it stays here as a direct drawing input.
        from tools.fleet.explorer import tables
        from tools.fleet.explorer.grid import CellGrid

        route = {"capability": "frame", "shape": "framed",
                 "legs": [["frame", "done", None], ["frame", "active", "0/2"]],
                 "stages": [{"name": "frame-alternative", "st": "done"}]}
        grid = CellGrid(120, 1)
        tables._draw_route_progress(grid, 0, 120, 0, route)
        text = grid.rows_text()[0]
        self.assertIn("frame ●", text)
        self.assertIn("(frame-alternative ✓)", text)

        route["stages"] = [{"name": "frame-alternative", "st": "failed"}]
        grid = CellGrid(120, 1)
        tables._draw_route_progress(grid, 0, 120, 0, route)
        text = grid.rows_text()[0]
        self.assertIn("frame ●", text)
        self.assertIn("(frame-alternative ✕)", text)

    def test_a_resumed_route_with_two_owner_jobs_does_not_borrow_a_frame_leg(self):
        owners = (_owner_job("att-old", "claude", "opus", "max", "dead"),
                  _owner_job("att-new", "codex", "gpt-6-astra", None, "working"))
        _view_value, _record_value, route, vm = _vm(extra_owners=owners)
        self.assertIsNone(route["harness"])
        self.assertIsNone(route["model"])
        self.assertIsNone(route["effort"])
        for cols in (120, 180):
            with self.subTest(cols=cols):
                text = "\n".join(draw(vm, NavState(view="ROUTES", title="ROUTES"),
                                      cols, 60).text_rows())
                row = next(line for line in text.splitlines()
                           if "[z9]" in line and "frame" in line)
                self.assertIn("frame", row)
                self.assertNotIn("claude·max", row)
                self.assertNotIn("Opus", row)

    def test_narrow_routes_keep_the_progress_mark_at_96_and_100_columns(self):
        _view_value, _record_value, _route, vm = _vm(
            owner={"harness": "opencode", "model": "glm-5.2", "effort": "xhigh"})
        # The MODEL cell keeps the harness whole; the model and effort give way first.
        for cols, harness_cell in ((96, "opencode"), (100, "opencode")):
            with self.subTest(cols=cols):
                frame, trace = ef.trace_draw(draw, vm, NavState(view="ROUTES", title="ROUTES"),
                                             cols, 60)
                self.assertEqual(ef.frame_violations(frame, trace, cols, 60), [])
                row = next(line for line in frame.text_rows()
                           if "[z9]" in line and "frame" in line)
                self.assertIn("frame ✓", row)
                self.assertIn(harness_cell, row)
                self.assertNotIn("frame ✓  ∥", row)

    def test_an_owner_attempt_without_a_harness_does_not_borrow_a_frame_leg(self):
        _view_value, _record_value, route, vm = _vm(
            owner={"harness": "", "model": "opus", "effort": "max"})
        self.assertIsNone(route["harness"])
        self.assertEqual(route["model"][0], "Opus")
        self.assertEqual(route["effort"], "max")
        for cols in (120, 180):
            with self.subTest(cols=cols):
                text = "\n".join(draw(vm, NavState(view="ROUTES", title="ROUTES"),
                                      cols, 60).text_rows())
                row = next(line for line in text.splitlines() if "[z9]" in line and "frame" in line)
                self.assertNotIn("claude·max", row)
                self.assertNotIn("Opus", row)

    def test_narrow_routes_harness_fits_the_observed_text_and_clips_with_an_ellipsis(self):
        _view_value, _record_value, _route, vm = _vm(
            owner={"harness": "claude", "model": "opus", "effort": "high"})
        row = next(line for line in draw(vm, NavState(view="ROUTES", title="ROUTES"),
                                         120, 60).text_rows()
                   if "[z9]" in line and "frame" in line)
        self.assertIn("claude Opus high", row[34:])

        _view_value, _record_value, _route, vm = _vm(
            owner={"harness": "opencode", "model": "glm-5.2", "effort": "xhigh"})
        # At 80 columns the one-line row keeps its marker, ID, the whole harness and the
        # elapsed time; TITLE and the stage rail are the cells that give way.
        row = next(line for line in draw(vm, NavState(view="ROUTES", title="ROUTES"),
                                         80, 60).text_rows()
                   if "[z9]" in line)
        self.assertIn("opencode", row[34:])
        self.assertNotIn("opencode·x ", row[34:])
        self.assertIn("11m", row[34:])

    def test_a_frame_leg_shown_as_the_body_row_keeps_the_frame_label(self):
        view = _view(running=True)
        jobs = [_frame_job("frame", "claude", "opus", "max", True)]
        session = {"harness": "claude", "session_id": "sid-owner", "session_tag": "z9",
                   "cwd": REPO, "liveness": "idle", "is_child": False, "title": "owner"}
        slot = {"view": dict(view, nodes=view["nodes"][:1]), "record": _record(),
                "jobs": jobs, "sessions": [session]}
        with patch.object(live, "_collect_routes", return_value={ROUTE_ID: slot}):
            rows, index = live._session_records({"sessions": [session]}, NOW,
                                                lambda _cwd: "repo")
            routes, _nodes = live._route_records({"resources": []}, rows, index,
                                                 lambda _cwd: "repo", NOW)
        self.assertEqual(routes[0]["worker"][0], "frame")

    def test_a_staged_route_after_a_frame_chain_is_not_displayed_as_a_frame_route(self):
        route, groups, vm = _staged_after_frame_vm()
        self.assertEqual(route["capability"], "code")
        self.assertEqual(route["shape"], "staged")
        self.assertEqual(route["mode"], "dev")
        self.assertEqual(route["intensity"], "standard")
        self.assertEqual([group.get("kind") for group in groups], ["leg"])
        for cols in (120, 180):
            with self.subTest(cols=cols):
                text = "\n".join(draw(vm, NavState(
                    view="ROUTES", title="ROUTES",
                    folds=(("ropen:ROUTES", STAGED_ID),)), cols, 60).text_rows())
                self.assertNotIn("frame ∥", text)
                self.assertIn("frame ✓", text)


if __name__ == "__main__":
    unittest.main()
