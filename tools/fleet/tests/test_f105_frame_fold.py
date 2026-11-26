"""F-105 — finished frame branches fold under a live parent session.

Backlog O2 (memory project_project-백로그-fleet-결함_71f005, user 2026-10-01):
already-finished frame legs (frame / frame-alternative) kept full dispatch rows
on the parent session card, sitting next to the next round's live rows. A route
group whose every frame leg is finished now folds away at once, so only
in-progress paths keep full rows. While any leg of the route still runs, the
pair stays on screen. `a` / --all reveals folded legs, like every other fold.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import render                                          # noqa: E402
from fleet.model import DispatchJob, ProgressProjection, Session, WorkProjection  # noqa: E402


FRAME_NODES = [{"id": "frame", "state": "done"},
               {"id": "frame-alternative", "state": "done"}]


def _text(segs):
    return "".join(t for t, _k in segs)


def _parent(nodes=None, scope=("frame", "frame-alternative")):
    nodes = FRAME_NODES if nodes is None else nodes
    return Session(
        harness="claude", pid=4242, proc_start="root", cwd="/tmp/f105",
        session_id="sid-f105parent", slug="f105-parent", liveness="working",
        work_projection=WorkProjection(
            source="route-exact", route_id="rt-f105",
            stage_label="frame-alternative",
            scope_node_ids=scope,
            _route_view={"view": {"nodes": nodes}},
        ),
    )


def _frame(slug, node, liveness, node_state="done", afterglow=False,
           route_id="rt-f105"):
    return DispatchJob(
        key="route-frame", slug=slug, cwd="/tmp/f105", harness="claude",
        depth=1, dispatch_depth=1, liveness=liveness, afterglow=afterglow,
        worker_type="frame", route_node=node, route_id=route_id, is_child=True,
        parent_sid="sid-f105parent",
        work_projection=WorkProjection(
            source="route-exact", route_id=route_id, route_node=node,
            node_state=node_state, stage_label="route-frame",
            progress=ProgressProjection(2, 2),
            scope_node_ids=(node,),
            _route_view={"view": {"nodes": list(FRAME_NODES)}},
        ),
    )


def _owner():
    nodes = [{"id": "plan", "state": "active"}, {"id": "execute", "state": "pending"}]
    return DispatchJob(
        key="code", slug="f105-owner", cwd="/tmp/f105", harness="claude",
        depth=1, dispatch_depth=1, liveness="working",
        worker_type="owner", is_child=True, parent_sid="sid-f105parent",
        work_projection=WorkProjection(
            source="route-exact", route_id="rt-f105-leg",
            stage_label="plan", node_state="active",
            progress=ProgressProjection(0, 2),
            _route_view={"view": {"nodes": nodes}},
        ),
    )


def _render(session, jobs, width=180):
    lines = render._build_lines([session], jobs, "both", False, 0,
                                layout="wide", term_width=width)
    return "\n".join(_text(line) for line in lines if line)


class FoldFinishedFrameChildTest(unittest.TestCase):

    def test_done_frame_leg_is_a_fold_candidate(self):
        self.assertTrue(render._fold_finished_frame_child(
            _frame("f105-frame", "frame", "done", afterglow=True)))

    def test_working_frame_leg_is_not_a_candidate(self):
        self.assertFalse(render._fold_finished_frame_child(
            _frame("f105-frame", "frame", "working")))

    def test_stale_frame_without_route_proof_is_not_a_candidate(self):
        self.assertFalse(render._fold_finished_frame_child(
            _frame("f105-frame", "frame", "stale", node_state=None)))

    def test_stale_frame_with_exact_done_node_is_a_candidate(self):
        self.assertTrue(render._fold_finished_frame_child(
            _frame("f105-frame", "frame", "stale", node_state="done")))

    def test_done_non_frame_row_is_not_a_candidate(self):
        """Owners keep their own cards; this fold is frame-legs only."""
        job = _frame("f105-owner", "frame", "done", afterglow=True)
        job.worker_type = "owner"
        self.assertFalse(render._fold_finished_frame_child(job))


class FrameFoldRenderTest(unittest.TestCase):

    def test_finished_pair_folds_together(self):
        parent = _parent()
        parent.title = "f105-parent"
        joined = _render(parent, [_frame("f105-frame", "frame", "done", afterglow=True),
                                 _frame("f105-frame-alt", "frame-alternative",
                                        "done", afterglow=True)])
        self.assertNotIn("f105-frame", joined)
        self.assertNotIn("f105-frame-alt", joined)
        self.assertIn("f105-parent", joined)

    def test_finished_leg_stays_while_route_sibling_runs(self):
        joined = _render(_parent(), [_frame("f105-frame", "frame", "working"),
                                     _frame("f105-frame-alt", "frame-alternative",
                                            "done", afterglow=True)])
        self.assertIn("f105-frame", joined)
        self.assertIn("f105-frame-alt", joined)

    def test_old_round_folds_while_new_round_runs(self):
        joined = _render(_parent(), [
            _frame("f105-old-frame", "frame", "done", afterglow=True,
                   route_id="rt-f105-old"),
            _frame("f105-old-frame-alt", "frame-alternative", "done",
                   afterglow=True, route_id="rt-f105-old"),
            _frame("f105-frame", "frame", "working", node_state="active",
                   route_id="rt-f105"),
            _frame("f105-frame-alt", "frame-alternative", "working",
                   node_state="active", route_id="rt-f105"),
        ])
        self.assertNotIn("f105-old-frame", joined)
        self.assertNotIn("f105-old-frame-alt", joined)
        self.assertIn("f105-frame", joined)
        self.assertIn("f105-frame-alt", joined)

    def test_finished_frames_fold_under_a_working_owner(self):
        joined = _render(_parent(), [_frame("f105-frame", "frame", "done", afterglow=True),
                                     _frame("f105-frame-alt", "frame-alternative",
                                            "done", afterglow=True),
                                     _owner()])
        self.assertNotIn("f105-frame", joined)
        self.assertNotIn("f105-frame-alt", joined)
        self.assertIn("f105-owner", joined)

    def test_show_all_reveals_folded_legs(self):
        prev = render._SHOW_ALL
        render._SHOW_ALL = True
        try:
            joined = _render(_parent(), [_frame("f105-frame", "frame", "done", afterglow=True),
                                         _frame("f105-frame-alt", "frame-alternative",
                                                "done", afterglow=True)])
        finally:
            render._SHOW_ALL = prev
        self.assertIn("f105-frame", joined)
        self.assertIn("f105-frame-alt", joined)


if __name__ == "__main__":
    unittest.main()
