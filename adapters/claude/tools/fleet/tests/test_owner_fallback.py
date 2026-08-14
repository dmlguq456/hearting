"""Focused regression for exact live-session ownership fallback in group view."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import render  # noqa: E402
from fleet.model import ContextProjection, DispatchJob, ProgressProjection, Session, WorkProjection  # noqa: E402


def text(lines):
    return "\n".join("".join(part for part, _ in line) for line in lines if line)


class OwnerFallbackTest(unittest.TestCase):
    def setUp(self):
        render.set_process_view(False)
        render.set_show_all(False)
        render.reset_selection()

    def tearDown(self):
        render.set_process_view(False)
        render.set_show_all(False)
        render.reset_selection()

    def _route_projection(self, route_id="rt-owner-fallback"):
        nodes = [
            {"id": "plan", "state": "done", "level": 0, "depends_on": []},
            {"id": "execute", "state": "active", "level": 1, "depends_on": ["plan"]},
            {"id": "test", "state": "pending", "level": 2, "depends_on": ["execute"]},
        ]
        return WorkProjection(
            source="route-exact", route_id=route_id, route_node="execute",
            stage_label="execute", node_state="active",
            progress=ProgressProjection(1, 3),
            _route_view={
                "record": {"route_id": route_id, "capability": "autopilot-code"},
                "nodes": nodes, "view": {"nodes": nodes},
            },
        )

    def _session(self, sid, slug, work):
        return Session(
            harness="codex", pid=100, proc_start=slug, cwd="/tmp/owner-fallback",
            session_id=sid, slug=slug, liveness="working",
            work_projection=work, context=ContextProjection(63, "normal", "codex"),
            summary="parent now",
        )

    def _stage(self, sid, slug="stage-execute", parent_slug="terminal-owner", work=None):
        return DispatchJob(
            key="code-execute", slug=slug, parent_sid=sid, parent_slug=parent_slug,
            depth=2, is_child=True, cwd="/tmp/owner-fallback", harness="claude",
            liveness="working", route_id=work.route_id if work else None,
            route_node="execute", work_projection=work, summary="stage summary",
            model="sonnet", effort="high",
        )

    def _render(self, sessions, jobs, process=False, term_width=168):
        render.set_process_view(process)
        return text(render._build_lines(
            sessions, jobs, section="both", narrow=False, malformed=0,
            layout="wide", term_width=term_width,
        ))

    def test_exact_parent_sid_recovers_ownership_and_suppresses_only_duplicate_detail(self):
        work = self._route_projection()
        parent = self._session("sid-parent", "parent", work)
        unrelated = self._session("sid-unrelated", "unrelated", work)
        recovered = self._stage(parent.session_id, work=work)

        rendered = self._render([parent, unrelated], [recovered])

        self.assertNotIn("orphaned dispatch rows", rendered)
        self.assertNotIn("(orphan)", rendered)
        self.assertIn("parent", rendered)
        self.assertIn("stage-execute", rendered)
        self.assertIn("stage summary", rendered)
        self.assertIn("Sonnet", rendered)
        self.assertIn("parent now", rendered)
        self.assertEqual(rendered.count("stage plan ✓"), 1)
        self.assertIn("unrelated", rendered)
        self.assertIn("stage plan ✓", rendered)

    def test_unmatched_parent_stays_orphaned(self):
        work = self._route_projection("rt-unmatched")
        stage = self._stage("sid-missing", work=work)
        # 200 cols, not the suite default 168: an ORPHAN row pays for the section's extra
        # indent plus the `` (orphan)`` suffix out of the same name budget, and after F-54
        # (_HMW 33→38 cut the 168-col name zone 40→36) that no longer leaves room to spell
        # `stage-execute` in full — it renders as `stage-ex…`. The subject here is ownership
        # (orphan section + marker + worker identity), not the clip ledger, which
        # test_f22_name_cap already owns, so widen past the cap instead of weakening the
        # identity assertion.
        rendered = self._render([], [stage], term_width=200)
        self.assertIn("orphaned dispatch rows", rendered)
        self.assertIn("(orphan)", rendered)
        self.assertIn("stage-execute", rendered)

    def test_visible_slug_owner_wins_over_exact_session_fallback(self):
        work = self._route_projection("rt-visible-slug")
        parent = self._session("sid-parent", "parent", work)
        owner = DispatchJob(
            key="code", slug="visible-owner", parent_sid=parent.session_id,
            depth=1, is_child=True, cwd=parent.cwd, harness="claude",
            liveness="working", work_projection=work,
        )
        stage = self._stage(parent.session_id, parent_slug=owner.slug, work=work)
        rendered = self._render([parent], [owner, stage])
        self.assertIn("visible-owner", rendered)
        self.assertEqual(rendered.count("stage-execute"), 1)
        self.assertIn("plan ✓", rendered)

    def test_drill_unresolved_root_remains_standalone(self):
        work = self._route_projection("rt-drill")
        stage = self._stage("drill-claude-parent-session", work=work)
        stage.cwd = "/tmp/drill-owner-fallback-abcd/repo"
        rendered = self._render([], [stage])
        self.assertNotIn("orphaned dispatch rows", rendered)
        self.assertNotIn("(orphan)", rendered)
        self.assertIn("stage-execute", rendered)

    def test_child_summary_count_and_process_full_dag_are_preserved(self):
        work = self._route_projection("rt-preserve")
        parent = self._session("sid-parent", "parent", work)
        stage = self._stage(parent.session_id, work=work)
        group = self._render([parent], [stage])
        self.assertIn("parent", group)
        self.assertIn("stage summary", group)
        self.assertIn("Sonnet", group)
        self.assertIn("parent ▾1", group)  # recovered child contributes to parent nesting/count.

        process = self._render([parent], [stage], process=True)
        for node in ("plan", "execute", "test"):
            self.assertIn(node, process)
        self.assertNotIn("orphaned dispatch rows", process)


if __name__ == "__main__":
    unittest.main()
