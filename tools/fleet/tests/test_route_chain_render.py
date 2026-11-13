"""F-<next> route chain rendering — strip formatting, owner dial shape, cap_grounding priority
(tools/fleet/render.py + projection.py, plan §3 Phase C). Stdlib unittest only."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
import sys
import time
import unittest
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import render, route_chain  # noqa: E402
from fleet.model import DispatchJob, Session, WorkProjection  # noqa: E402


def _node(label, state, *, shape=None, round_n=1, handoff_to=None):
    return {"label": label, "capability": label, "capability_mode": "default", "route_id": "rt-" + label,
            "state": state, "shape": shape, "round": round_n, "handoff_to": handoff_to,
            "handoff_from": None, "ambiguity": [], "intensity": "standard"}


def _text(segs):
    return "".join(t for t, _k in segs)


def _cell(chain, width=40, **kw):
    segs = render._route_chain_cell(chain, width, **kw)
    return None if segs is None else _text(segs)


@mock.patch.object(render, "_SHOW_ALL", True)
class RouteChainCellTest(unittest.TestCase):
    def test_mockup_research_done_draft_open_apply_planned(self):
        nodes = [_node("research", "done"), _node("draft", "open", shape="solo"),
                 {"label": "apply", "capability": "apply", "state": "planned", "shape": None,
                  "round": None, "handoff_to": None, "handoff_from": None, "ambiguity": []}]
        chain = {"v": 1, "key": "k1", "visible": True, "plan": ["research", "draft", "apply"],
                 "plan_source": "explicit", "nodes": nodes, "current": nodes[1]}
        self.assertEqual(_cell(chain), "research(std) ✓ › draft(std) ● › apply ○")

    def test_hidden_when_single_node_without_plan(self):
        chain = route_chain.assemble(
            [route_chain.build_line(
                {"route_id": "rt-1", "route_hash": "h1", "capability": "autopilot-research"},
                event="compose", harness="claude", session_id="s", route_file="/tmp/r.json")],
            load_record=lambda *a: None, load_outcome=lambda *a: {"present": False},
        )
        self.assertIsNone(_cell(chain))

    def test_none_chain_produces_no_cell(self):
        self.assertIsNone(_cell(None))

    def test_handoff_origin_dim_with_receiver_tag_and_receiver_plain(self):
        origin_nodes = [_node("research", "done"),
                        _node("draft", "open", shape="solo",
                              handoff_to={"harness": "claude", "session_id": "sid-d1"})]
        origin_chain = {"v": 1, "key": "k1", "visible": True, "plan": [], "plan_source": None,
                        "nodes": origin_nodes, "current": origin_nodes[1]}
        self.assertIn("draft◌→[d1]",
                      _cell(origin_chain, tag_by_key={("claude", "sid-d1"): "d1"}))

        receiver_nodes = [_node("draft", "open", shape="solo"), _node("apply", "planned")]
        receiver_chain = {"v": 1, "key": "k1", "visible": True, "plan": ["draft", "apply"],
                          "plan_source": "inherited", "nodes": receiver_nodes,
                          "current": receiver_nodes[0]}
        self.assertEqual(_cell(receiver_chain), "draft(std) ● › apply ○")

    def test_window_without_a_plan_keeps_the_last_four(self):
        nodes = [_node(name, "done") for name in ("a", "b", "c", "d")]
        nodes.append(_node("e", "open"))
        chain = {"v": 1, "key": "k1", "visible": True, "plan": [], "plan_source": None,
                "nodes": nodes, "current": nodes[4]}
        start, window = render._route_chain_window(nodes, 4)
        self.assertEqual([n["label"] for n in window], ["b", "c", "d", "e"])
        cell = _cell(chain)
        self.assertNotIn("a", cell.split("›")[0])
        self.assertEqual(cell.count("›"), 3)

    def test_window_with_a_plan_shows_three_back_and_one_next(self):
        nodes = [_node(name, "done") for name in ("research", "draft", "refine")]
        nodes += [_node("apply", "open"), _node("code", "planned"), _node("ship", "planned")]
        _start, window = render._route_chain_window(nodes, 3)
        self.assertEqual([n["label"] for n in window], ["draft", "refine", "apply", "code"])

    def test_window_early_in_a_chain_shows_only_the_next_plan_step(self):
        nodes = [_node("research", "open"), _node("draft", "planned"),
                 _node("apply", "planned"), _node("ship", "planned")]
        chain = {"v": 1, "key": "k1", "visible": True, "plan": [], "plan_source": None,
                "nodes": nodes, "current": nodes[0]}
        self.assertEqual(_cell(chain), "research(std) ● › draft ○")

    def test_width_ladder_knobs_fold_past_first_then_current_then_counts(self):
        nodes = [_node("research", "done"), _node("draft", "open"), _node("apply", "planned")]
        chain = {"v": 1, "key": "k1", "visible": True, "plan": ["research", "draft", "apply"],
                "plan_source": "explicit", "nodes": nodes, "current": nodes[1]}
        texts = [_text(body) for body in render._route_chain_bodies(chain)]
        self.assertEqual(texts[0], "research(std) ✓ › draft(std) ● › apply ○")
        self.assertEqual(texts[1], "research ✓ › draft(std) ● › apply ○")
        self.assertEqual(texts[2], "research ✓ › draft ● › apply ○")
        self.assertEqual(texts[3], "draft(std) ● +1✓ +1○")
        self.assertEqual(texts[4], "draft ● +1✓ +1○")
        widths = [sum(render._dw(t) for t, _k in body)
                  for body in render._route_chain_bodies(chain)]
        self.assertEqual(widths, sorted(widths, reverse=True))
        full_w = widths[0]
        self.assertEqual(_text(render._route_chain_cell(chain, full_w - 1)), texts[1])

    def test_mode_and_retry_round_ride_the_same_paren(self):
        node = _node("code", "done", round_n=2)
        node["capability_mode"] = "dev"
        self.assertEqual(render._route_chain_node_knobs(node, True), "dev·std·R2")
        self.assertEqual(render._route_chain_node_knobs(node, False), "R2")

    def test_only_the_current_node_is_lit(self):
        nodes = [_node("code", "done"), _node("lab", "open")]
        chain = {"v": 1, "key": "k1", "visible": True, "plan": [], "plan_source": None,
                "nodes": nodes, "current": nodes[1]}
        working = render._route_chain_bodies(chain, working=True)[0]
        keyed = [(t, k) for t, k in working if t.strip()]
        self.assertEqual(keyed[0], ("code(std)", "dim"))
        self.assertEqual(keyed[1], (" ✓", "dim"))
        self.assertIn(("lab", "g_work"), keyed)
        self.assertIn((" ●", "g_work"), keyed)
        self.assertIn(("std", "dim"), keyed)            # the current node's knobs stay dim
        idle = render._route_chain_bodies(chain, working=False)[0]
        self.assertIn(("lab", None), idle)              # base remains plain while idle

    def test_current_open_item_blinks_while_idle_and_working_but_closed_or_planned_does_not(self):
        for working in (False, True):
            node = {"label": "code", "state": "open"}
            phases, past = [], []
            for phase in (True, False):
                with mock.patch.object(render, "_BLINK_ON", phase):
                    phases.append(render._route_chain_node_segs(node, False, {}, is_current=True, working=working))
                    past.append(render._route_chain_node_segs(node, False, {}, is_current=False, working=working))
            self.assertNotEqual(phases[0], phases[1])
            self.assertEqual(past[0], past[1])
            self.assertEqual(render._snapshot_line(phases[0]), "code ●")
            self.assertEqual(render._snapshot_line(phases[0]), render._snapshot_line(phases[1]))
            self.assertNotIn("\033[5m", render._snapshot_line(phases[0], colored=True))
        for state in ("done", "planned", "failed"):
            segs = render._route_chain_node_segs({"label": "code", "state": state}, False, {},
                                                is_current=True, working=True)
            self.assertNotIn("\033[5m", render._snapshot_line(segs, colored=True))

    def test_failed_past_node_keeps_its_red_glyph(self):
        nodes = [_node("code", "failed"), _node("lab", "open")]
        chain = {"v": 1, "key": "k1", "visible": True, "plan": [], "plan_source": None,
                "nodes": nodes, "current": nodes[1]}
        body = render._route_chain_bodies(chain)[0]
        self.assertIn((" ✕", "lvl_r"), body)

    def test_failed_and_unknown_counts_in_current_only_form(self):
        nodes = [_node("research", "failed"), _node("draft", "unknown"),
                 _node("apply", "open", shape="solo")]
        chain = {"v": 1, "key": "k1", "visible": True, "plan": [], "plan_source": None,
                "nodes": nodes, "current": nodes[2]}
        current_only = _text(render._route_chain_bodies(chain)[3])
        self.assertIn("+1✕", current_only)
        self.assertIn("+1?", current_only)


@mock.patch.object(render, "_SHOW_ALL", True)
class OwnerDialShapeTest(unittest.TestCase):
    def _owner_job(self, shape="staged", route_origin="compose"):
        record = {"selection": {"route_origin": route_origin, "shape": shape}}
        wp = WorkProjection(source="record", _route_view={"record": record})
        job = DispatchJob(key="owner", slug="owner-1", worker_type="owner", depth=1,
                          capability_mode="dev", intensity="standard")
        job.work_projection = wp
        return job

    def test_owner_dial_shows_shape_for_compose_route_only(self):
        job = self._owner_job()
        segs, _w = render._opts_segs(job)
        text = _text(segs)
        self.assertIn("staged", text)

    def test_owner_dial_hides_shape_for_compile_route(self):
        job = self._owner_job(route_origin="compile")
        segs, _w = render._opts_segs(job)
        self.assertNotIn("staged", _text(segs))

    def test_owner_dial_drops_shape_first_when_narrow(self):
        job = self._owner_job()
        wide_text = _text(render._opts_segs(job)[0])
        self.assertIn("staged", wide_text)
        narrow_text = _text(render._opts_segs(job, max_width=len(wide_text) - 1)[0])
        self.assertNotIn("staged", narrow_text)

    def test_non_owner_row_never_shows_shape(self):
        job = self._owner_job()
        job.worker_type = None
        segs, _w = render._opts_segs(job)
        self.assertNotIn("staged", _text(segs))


@mock.patch.object(render, "_SHOW_ALL", True)
class MainStageCellTest(unittest.TestCase):
    def test_open_route_beats_marker_then_marker_then_dash(self):
        session = Session(harness="claude", pid=1, session_id="s1")
        now = time.time()
        session.cap_grounding = route_chain.current_capability(
            {"current": {"state": "open", "ts": now, "capability": "autopilot-code",
                        "capability_mode": "dev", "intensity": "standard", "route_id": "rt-1"}},
            {"capability": "autopilot-refine", "source": "marker"},
            session_start=now - 60, slack=120,
        )
        segs = render._session_stage_segs(session, working=True, max_width=80)
        self.assertIn("code", _text(segs))

        session2 = Session(harness="claude", pid=2, session_id="s2")
        session2.cap_grounding = route_chain.current_capability(
            None, {"capability": "autopilot-refine"}, session_start=now, slack=120,
        )
        segs2 = render._session_stage_segs(session2, working=True, max_width=80)
        self.assertIn("refine", _text(segs2))

        session3 = Session(harness="claude", pid=3, session_id="s3")
        session3.cap_grounding = route_chain.current_capability(None, None, session_start=now, slack=120)
        self.assertIsNone(session3.cap_grounding)
        render._session_stage_segs(session3, working=True, max_width=80)   # must not raise


class JsonAdditiveTest(unittest.TestCase):
    def test_session_carries_route_chain_additively(self):
        session = Session(harness="claude", pid=1, session_id="s1")
        chain = {"v": 1, "key": "k", "visible": True, "plan": [], "plan_source": None,
                "nodes": [], "current": None}
        session.route_chain = chain
        payload = session.to_dict()
        self.assertEqual(payload.get("route_chain"), chain)
        json.dumps(payload)   # additive field must stay JSON-serializable



def _board(sessions, layout="wide", term_width=168, jobs=()):
    return ["" if ln is None else _text(ln)
            for ln in render._build_lines(sessions, list(jobs), "both", False, 0,
                                          layout=layout, term_width=term_width)]


def _session_row_text(rows):
    """The session's own row: its harness cell, not the usage header or an owner card."""
    return next(t for t in rows if "claude code" in t and "usage" not in t and "╭" not in t)


def _chain_session(nodes, current, sid="sid-chain", plan=()):
    session = Session(harness="claude", pid=11, cwd="/x/chain", slug="chain", session_id=sid,
                      liveness="working", elapsed_min=5)
    session.route_chain = {"v": 1, "key": "k1", "visible": True, "plan": list(plan),
                           "plan_source": "explicit" if plan else None,
                           "nodes": nodes, "current": current}
    return session


@mock.patch.object(render, "_SHOW_ALL", True)
class ChainInStageCellTest(unittest.TestCase):
    """User 2026-09-18: the chain takes the session's stage cell (the old `-` slot), up to
    40 cells, as a window of at most four nodes (one next plan step); no separate line."""

    def test_short_chain_fills_the_cell(self):
        nodes = [_node("code", "done"), _node("lab", "open", shape="direct")]
        rows = _board([_chain_session(nodes, nodes[1])])
        self.assertIn("code(std) ✓ › lab(std) ●", _session_row_text(rows))
        self.assertFalse(any("경로 " in t for t in rows))

    def test_long_chain_shows_three_back_one_next_and_no_line(self):
        nodes = [_node("research", "done"), _node("draft", "done"), _node("refine", "done"),
                 _node("apply", "open", shape="staged"), _node("code", "planned")]
        rows = _board([_chain_session(nodes, nodes[3], plan=("research", "draft", "refine",
                                                             "apply", "code"))])
        row = _session_row_text(rows)
        # 42 cells with the current node's details, so every detail folds inside 40
        self.assertIn("draft ✓ › refine ✓ › apply ● › code ○", row)
        self.assertNotIn("research", row)
        self.assertFalse(any("경로 " in t for t in rows))

    def test_cell_budget_caps_at_forty(self):
        self.assertEqual(render._SESSION_CELL_MAX, 40)
        self.assertEqual(render._narrow_session_cell_budget(400), 40)
        self.assertEqual(render._narrow_session_cell_budget(60), 28)   # historical floor

    def test_owner_card_session_still_shows_its_chain_in_the_cell(self):
        nodes = [_node("code", "done"), _node("lab", "open", shape="solo")]
        session = _chain_session(nodes, nodes[1], sid="sid-owner-parent")
        owner = DispatchJob(key="autopilot-lab", slug="lab-owner", worker_type="owner",
                            depth=1, liveness="working", parent_sid="sid-owner-parent",
                            is_child=True, cwd="/x/chain", harness="claude", elapsed_min=1)
        rows = _board([session], jobs=[owner])
        self.assertTrue(any("lab-owner" in t for t in rows))          # the owner card renders
        self.assertIn("code(std) ✓ › lab(std) ●", _session_row_text(rows))
        self.assertFalse(any("경로 " in t for t in rows))

    def test_owner_card_session_without_a_chain_keeps_the_dash(self):
        session = Session(harness="claude", pid=14, cwd="/x/chain", slug="chain",
                          session_id="sid-plain", liveness="working", elapsed_min=5)
        session.cap_grounding = {"capability": "autopilot-lab"}
        owner = DispatchJob(key="autopilot-lab", slug="lab-owner", worker_type="owner",
                            depth=1, liveness="working", parent_sid="sid-plain",
                            is_child=True, cwd="/x/chain", harness="claude", elapsed_min=1)
        row = _session_row_text(_board([session], jobs=[owner]))
        self.assertNotIn("lab", row.split("(—)")[-1].split("5m")[0])   # D3: tag steps aside

    def test_narrow_card_shows_the_capability_tag_not_a_dash(self):
        session = Session(harness="claude", pid=12, cwd="/x/narrow", slug="narrow",
                          session_id="sid-narrow", liveness="working", elapsed_min=5)
        session.cap_grounding = {"capability": "autopilot-lab", "mode": "setup",
                                 "intensity": "direct"}
        l1, l2 = render._session_row_2line(session, term_width=100)
        self.assertIn("lab(setup·direct)", _text(l2))

    def test_narrow_card_prefix_width_matches_the_budget_constant(self):
        session = Session(harness="claude", pid=13, cwd="/x/narrow", slug="narrow",
                          session_id="sid-narrow2", liveness="working", elapsed_min=5,
                          model="claude-opus-5", effort="xhigh")
        session.cap_grounding = {"capability": "autopilot-code"}
        _l1, l2 = render._session_row_2line(session, term_width=100)
        prefix = 0
        for text, _key in l2:
            if text == "code":
                break
            prefix += render._dw(text)
        self.assertEqual(prefix, render._NARROW_L2_STAGE_COL)


if __name__ == "__main__":
    unittest.main()
