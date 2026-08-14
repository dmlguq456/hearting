#!/usr/bin/env python3
"""Hermetic unit tests — the crash→relaunch recovery window (user 2026-08-13).

A crashed attempt (server death, SIGKILL) leaves its registry row dead, and the §3.3 judge
called that node `failed` → the breadcrumb printed `✕`. During the window where the SAME route
already has its recovery staged — the orphan classifier naming the node as the resume boundary,
or a strictly newer open attempt — that `✕` reads as "the pipeline failed" when the truth is
"this node is being brought back" (mem-pipeline-revival r3 크래시 복구 중 사용자 지적).

Contract under test:
  * the judge lives in ONE place (`route._node_state`); render only draws the decided state,
  * a dead row with NO recovery path keeps its `failed` verdict — `✕` is still `✕`,
  * the recovery verdict rests on exact registry-owned evidence (SD-64/71 orphan boundary or a
    newer open/working attempt), never on an inferred "probably coming back",
  * established evidence still outranks it: a completion marker is `done`, an explicit
    killed/cancelled row stays `failed`.

`now` is always passed in; no clock read, no file I/O, no network.
"""
import os
import sys
import unittest

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import render, route                      # noqa: E402
from fleet.model import DispatchJob                  # noqa: E402

NOW = 1_000_000.0


def _row(node, liveness, order, **kw):
    return DispatchJob(key="code", slug="r-" + str(order), route_id="rt-1", route_node=node,
                       liveness=liveness, registry_order=order, elapsed_min=7,
                       model="m", harness="claude", effort="high", pid=4242, **kw)


def _owner(order, liveness="working", **kw):
    """A dispatch-depth-1 owner attempt: bound to the route through `owner_route_id`."""
    return DispatchJob(key="code", slug="owner-" + str(order), owner_route_id="rt-1",
                       depth=1, dispatch_depth=1, liveness=liveness, registry_order=order,
                       elapsed_min=1, harness="claude", **kw)


class RecoveryWindowJudgeTest(unittest.TestCase):
    def test_dead_row_with_no_recovery_path_is_still_failed(self):
        st = route._node_state("r3", [_row("r3", "dead", 1)], {}, NOW)
        self.assertEqual(st["state"], "failed")

    def test_newer_open_owner_attempt_on_the_route_is_a_recovery_window(self):
        dead = _row("r3", "dead", 1)
        st = route._node_state("r3", [dead, _owner(2, liveness="working")], {}, NOW)
        self.assertEqual(st["state"], "recovering")
        self.assertEqual(st["note"], "relaunch-open")
        self.assertIs(st["job"], dead)          # telemetry stays the crashed attempt's

    def test_open_status_counts_even_before_the_process_is_observed_working(self):
        dead = _row("r3", "dead", 1)
        staged = _owner(2, liveness="unknown", status="open")
        self.assertEqual(
            route._node_state("r3", [dead, staged], {}, NOW)["state"], "recovering")

    def test_orphan_resume_boundary_naming_this_node_is_a_recovery_window(self):
        dead = _row("r3", "dead", 1)
        orphan_owner = _owner(1, liveness="dead", note="dead-parent-orphaned")
        orphan_owner.resume_boundary = "r3"
        st = route._node_state("r3", [dead, orphan_owner], {}, NOW)
        self.assertEqual(st["state"], "recovering")
        self.assertEqual(st["note"], "awaiting-resume")

    def test_orphan_boundary_on_another_node_does_not_rescue_this_one(self):
        dead = _row("r3", "dead", 1)
        orphan_owner = _owner(1, liveness="dead", note="dead-parent-orphaned")
        orphan_owner.resume_boundary = "r5"
        self.assertEqual(route._node_state("r3", [dead, orphan_owner], {}, NOW)["state"],
                         "failed")

    def test_missing_resume_boundary_cannot_mint_recovery(self):
        dead = _row("r3", "dead", 1)
        orphan_owner = _owner(1, liveness="dead", note="dead-parent-orphaned")
        orphan_owner.resume_boundary = "-"      # collector's "unknown boundary" placeholder
        self.assertEqual(route._node_state("r3", [dead, orphan_owner], {}, NOW)["state"],
                         "failed")

    def test_older_open_attempt_is_not_a_relaunch(self):
        dead = _row("r3", "dead", 5)
        self.assertEqual(
            route._node_state("r3", [dead, _owner(2, status="open")], {}, NOW)["state"],
            "failed")

    def test_open_stage_row_on_a_different_node_is_not_this_node_recovery(self):
        dead = _row("r3", "dead", 1)
        sibling = _row("r4", "working", 2)       # a downstream stage genuinely running
        sibling.dispatch_depth = sibling.depth = 2
        self.assertEqual(route._node_state("r3", [dead, sibling], {}, NOW)["state"], "failed")

    def test_completion_marker_still_outranks_a_recovery_window(self):
        st = route._node_state("r3", [_row("r3", "dead", 1), _owner(2)], {}, NOW,
                               completion_marked=True)
        self.assertEqual(st["state"], "done")

    def test_explicit_kill_evidence_still_outranks_a_recovery_window(self):
        ev = {"r3": {"status": "killed", "note": "fleet-kill", "ts": NOW - 60}}
        st = route._node_state("r3", [_row("r3", "dead", 1), _owner(2)], ev, NOW)
        self.assertEqual(st["state"], "failed")

    def test_a_live_row_on_the_node_still_wins_as_active(self):
        st = route._node_state("r3", [_row("r3", "dead", 1), _row("r3", "working", 2),
                                      _owner(3)], {}, NOW)
        self.assertEqual(st["state"], "active")


class OwnerRowReachesTheRouteViewTest(unittest.TestCase):
    """The owner attempt binds through `owner_route_id`; without it in the route's job list the
    judge could never see a relaunch. It must NOT become a node's own row (it has no
    `route_node`), so node identity is unchanged."""

    RECORD = {"route_id": "rt-1", "capability": "autopilot-code",
              "nodes": [{"id": "r3"}, {"id": "r4", "depends_on": ["r3"]}]}

    def _view(self, jobs):
        return route.build_views(jobs, {}, {"rt-1": self.RECORD}, NOW)[0]

    def test_owner_only_binding_joins_its_route(self):
        view = self._view([_row("r3", "dead", 1), _owner(2, status="open")])
        states = {n["id"]: n["state"] for n in view["nodes"]}
        self.assertEqual(states["r3"], "recovering")
        self.assertEqual(states["r4"], "pending")

    def test_without_the_relaunch_the_same_route_still_reads_failed(self):
        view = self._view([_row("r3", "dead", 1)])
        self.assertEqual({n["id"]: n["state"] for n in view["nodes"]}["r3"], "failed")

    def test_recovering_node_is_not_counted_as_route_progress(self):
        view = self._view([_row("r3", "dead", 1), _owner(2, status="open")])
        self.assertEqual(view["progress"], {"done": 0, "total": 2})

    def test_summary_exposes_the_state_verbatim(self):
        summary = route.summary([self._view([_row("r3", "dead", 1), _owner(2, status="open")])])
        self.assertEqual(summary[0]["nodes"][0]["state"], "recovering")
        self.assertEqual(summary[0]["nodes"][0]["note"], "relaunch-open")


class RecoveringRendersWithoutTheFailureGlyphTest(unittest.TestCase):
    """SD-F2: render draws the decided state and re-derives nothing."""

    def test_breadcrumb_uses_the_waiting_ellipsis_not_the_failure_mark(self):
        segs = render._route_stage_segs([("r3", "recovering"), ("r4", "pending")],
                                        working=False, max_width=None)
        text = "".join(t for t, _k in segs)
        self.assertIn("r3 …", text)
        self.assertNotIn("✕", text)

    def test_breadcrumb_current_index_points_at_the_recovering_node(self):
        self.assertEqual(
            render._route_current_index([("r1", "done"), ("r3", "recovering"),
                                         ("r4", "pending")]), 1)

    def test_card_line_names_the_recovery_reason(self):
        text, key, mark = render._route_node_text(
            {"id": "r3", "state": "recovering", "note": "awaiting-resume",
             "elapsed_min": 7, "depends_on": []})
        self.assertIn("awaiting-resume", text)
        self.assertIn("…", text)
        self.assertNotIn("✕", text)
        self.assertEqual(key, "lvl_y")
        self.assertEqual(mark, "")   # gate-passed is a separate dimension; no claim here

    def test_failed_node_keeps_its_failure_glyph(self):
        text, key, _mark = render._route_node_text(
            {"id": "r3", "state": "failed", "elapsed_min": 7, "depends_on": []})
        self.assertIn("✕", text)
        self.assertEqual(key, "lvl_r")


if __name__ == "__main__":
    unittest.main()
