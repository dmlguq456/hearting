"""F-80 — a transient collection gap or display-filter transition must not promote a
dispatch job's parent edge to a project-level orphan on its first missed tick.

Covers three layers per plan.md §2.4:
  L1 collectors/__init__.py:_mark_dispatch_child_sessions  — fail-closed on a sid-less row
  L2 model.ParentEdgeTracker + collectors/__init__.py:resolve_parent_edges — bounded grace
  L3 collectors/claude.py:enrich                            — env-var sid recovery fallback

d5/d7/d8/d9 below name the plan's exp5.py degradation-matrix scenarios this suite exercises
in portable, dependency-free form (no snap.json fixture required).
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import collectors as fleet_collectors           # noqa: E402
from fleet import model, render                            # noqa: E402
from fleet.model import DispatchJob, Session                # noqa: E402


def _text(lines):
    return "\n".join("".join(part for part, _key in line) for line in lines if line)


class L1FailClosedTest(unittest.TestCase):
    """d8: a sid-less parent must never be reclassified as its own job's child."""

    def test_sidless_same_cwd_session_is_not_marked_child(self):
        # The exact d8 trigger: a real parent session momentarily has no observed
        # session_id (registry gap), and a job claims a parent_sid + shares its cwd/harness.
        parent = Session(harness="claude", pid=1, cwd="/work/proj", session_id=None,
                         slug="proj-parent", liveness="working")
        job = DispatchJob(key="code", slug="proj-child", cwd="/work/proj",
                          parent_sid="sid-that-existed-before-the-gap", is_child=True,
                          harness="claude", liveness="working")
        fleet_collectors._mark_dispatch_child_sessions([parent], [job])
        self.assertFalse(parent.is_child)

    def test_sid_present_and_matching_still_protected_as_before(self):
        # Unaffected control: the pre-existing exact-match guard still works when the sid
        # IS observed.
        parent = Session(harness="claude", pid=1, cwd="/work/proj", session_id="sid-1",
                         slug="proj-parent", liveness="working")
        job = DispatchJob(key="code", slug="proj-child", cwd="/work/proj",
                          parent_sid="sid-1", is_child=True, harness="claude",
                          liveness="working")
        fleet_collectors._mark_dispatch_child_sessions([parent], [job])
        self.assertFalse(parent.is_child)


class L2ParentEdgeTrackerTest(unittest.TestCase):
    """Direct unit coverage of the grace/expiry/dead-immediate state machine."""

    def setUp(self):
        model.reset_parent_edge_tracker()

    def tearDown(self):
        model.reset_parent_edge_tracker()

    def test_visible_parent_confirms_edge(self):
        edge, promoted = model.parent_edge_resolve("slug-a", "sid-1", True, False)
        self.assertEqual(edge, "sid-1")
        self.assertFalse(promoted)

    def test_grace_holds_the_confirmed_edge_for_three_ticks(self):
        model.parent_edge_resolve("slug-a", "sid-1", True, False)   # tick 1: confirmed
        for _ in range(3):                                          # ticks 2-4: within grace
            edge, promoted = model.parent_edge_resolve("slug-a", "sid-1", False, False)
            self.assertEqual(edge, "sid-1")
            self.assertFalse(promoted)

    def test_grace_expires_past_the_window(self):
        model.parent_edge_resolve("slug-a", "sid-1", True, False)   # confirmed
        for _ in range(3):
            model.parent_edge_resolve("slug-a", "sid-1", False, False)  # grace ticks 1-3
        edge, promoted = model.parent_edge_resolve("slug-a", "sid-1", False, False)  # tick 4
        self.assertIsNone(edge)
        self.assertTrue(promoted)

    def test_dead_evidence_promotes_immediately_even_mid_grace(self):
        model.parent_edge_resolve("slug-a", "sid-1", True, False)   # confirmed
        model.parent_edge_resolve("slug-a", "sid-1", False, False)  # grace tick 1
        edge, promoted = model.parent_edge_resolve("slug-a", "sid-1", False, True)
        self.assertIsNone(edge)
        self.assertTrue(promoted)

    def test_unconfirmed_edge_never_gets_grace(self):
        # No prior confirmed tick for this slug — grace only extends an edge that was
        # already confirmed at least once; it never manufactures a new attribution.
        edge, promoted = model.parent_edge_resolve("slug-a", "sid-1", False, False)
        self.assertIsNone(edge)
        self.assertTrue(promoted)

    def test_single_observation_is_a_grace_no_op(self):
        # --once/--json semantics: one collect_all() call is exactly one tick, so an
        # invisible-but-otherwise-unconfirmed parent resolves identically to the pre-F-80
        # immediate-orphan behavior — grace never fires on a lone snapshot.
        model.reset_parent_edge_tracker()
        edge, promoted = model.parent_edge_resolve("slug-once", "sid-9", False, False)
        self.assertIsNone(edge)
        self.assertTrue(promoted)

    def test_no_parent_sid_clears_any_prior_entry(self):
        model.parent_edge_resolve("slug-a", "sid-1", True, False)
        edge, promoted = model.parent_edge_resolve("slug-a", None, False, False)
        self.assertIsNone(edge)
        self.assertFalse(promoted)
        # entry was cleared — a later resolve with the same sid starts unconfirmed again.
        edge2, promoted2 = model.parent_edge_resolve("slug-a", "sid-1", False, False)
        self.assertIsNone(edge2)
        self.assertTrue(promoted2)

    def test_sweep_drops_unseen_keys(self):
        model.parent_edge_resolve("slug-a", "sid-1", True, False)   # tick 1: confirmed+seen
        model.parent_edge_sweep()          # end of tick 1 — slug-a WAS seen, so it survives
        model.parent_edge_sweep()          # end of tick 2 — slug-a was never re-resolved,
                                            # so this sweep drops the now-stale entry
        # Re-confirming after the drop starts fresh (unconfirmed first tick), proving the
        # ledger entry was actually removed rather than retained.
        edge, promoted = model.parent_edge_resolve("slug-a", "sid-1", False, False)
        self.assertIsNone(edge)
        self.assertTrue(promoted)


class L2CollectorIntegrationTest(unittest.TestCase):
    """resolve_parent_edges() against Session/DispatchJob objects — no procscan needed."""

    def setUp(self):
        model.reset_parent_edge_tracker()

    def tearDown(self):
        model.reset_parent_edge_tracker()

    def test_stale_app_server_and_absent_parent_all_grace_identically(self):
        # F-80 L2c: three different reasons a parent is "not visible" (stale filter,
        # app_server filter, complete collection absence) must all extend grace the same
        # way — none of them is death evidence.
        for liveness, app_server, absent in (
            ("stale", False, False),
            ("working", True, False),
            (None, False, True),
        ):
            with self.subTest(liveness=liveness, app_server=app_server, absent=absent):
                model.reset_parent_edge_tracker()
                parent = Session(harness="claude", pid=1, cwd="/w", session_id="sid-p",
                                 liveness="working")
                job = DispatchJob(key="code", slug="j1", parent_sid="sid-p", is_child=True,
                                  harness="claude", cwd="/w")
                # tick 1: parent genuinely visible — this is the only way ANY of the three
                # "not visible" reasons below can start from an already-confirmed edge
                # (grace never confirms a first-ever-invisible parent, "absent" included).
                fleet_collectors.resolve_parent_edges([parent], [job])
                self.assertEqual(job._parent_edge_sid, "sid-p")
                self.assertFalse(job._parent_edge_promoted_orphan)

                if not absent:
                    parent.liveness = liveness if liveness else parent.liveness
                    parent.app_server = app_server
                    sessions = [parent]
                else:
                    sessions = []
                fleet_collectors.resolve_parent_edges(sessions, [job])
                self.assertEqual(job._parent_edge_sid, "sid-p")   # tick 2: still grace-held
                self.assertFalse(job._parent_edge_promoted_orphan)

    def test_dead_parent_promotes_immediately_not_via_grace(self):
        parent = Session(harness="claude", pid=1, cwd="/w", session_id="sid-p",
                         liveness="working")
        job = DispatchJob(key="code", slug="j1", parent_sid="sid-p", is_child=True,
                          harness="claude", cwd="/w")
        fleet_collectors.resolve_parent_edges([parent], [job])
        self.assertEqual(job._parent_edge_sid, "sid-p")

        parent.liveness = "dead"
        fleet_collectors.resolve_parent_edges([parent], [job])
        self.assertIsNone(job._parent_edge_sid)
        self.assertTrue(job._parent_edge_promoted_orphan)

    def test_all_toggle_does_not_change_ledger_confirmation(self):
        # C10c: session_parent_visible() must never consult _SHOW_ALL — it decides
        # attribution history, not what --all additionally displays.
        parent = Session(harness="claude", pid=1, cwd="/w", session_id="sid-p",
                         liveness="stale")
        render.set_show_all(True)
        try:
            self.assertFalse(model.session_parent_visible(parent))
        finally:
            render.set_show_all(False)
        self.assertFalse(model.session_parent_visible(parent))

    def test_all_toggle_does_not_change_ledger_confirmation_through_classification(self):
        # impl-review gap follow-up to C10c above: the assertion there only calls
        # session_parent_visible() directly, so it never actually exercises render's
        # per-job classification loop (render.py's children/grace_jobs/orphans elif
        # ladder) that consumes the ledger. Route the same stale-parent scenario through
        # that loop via resolve_parent_edges + _build_lines and confirm --all still can't
        # override the ledger's confirmed/grace verdict for a job the collector already
        # attributed.
        render.set_process_view(False)
        render.set_show_all(False)
        render.reset_selection()
        model.reset_parent_edge_tracker()
        try:
            parent = Session(harness="claude", pid=1, cwd="/work/stale", session_id="sid-p",
                             slug="stale-parent", liveness="working")
            job = DispatchJob(key="code", slug="stale-child", cwd="/work/stale",
                              parent_sid="sid-p", is_child=True, harness="claude",
                              liveness="working")
            fleet_collectors.resolve_parent_edges([parent], [job])  # tick 1: confirmed
            parent.liveness = "stale"
            fleet_collectors.resolve_parent_edges([parent], [job])  # tick 2: grace-held
            self.assertEqual(job._parent_edge_sid, "sid-p")
            self.assertFalse(job._parent_edge_promoted_orphan)

            for show_all in (False, True):
                with self.subTest(show_all=show_all):
                    render.set_show_all(show_all)
                    rendered = _text(render._build_lines(
                        [parent], [job], section="both", narrow=False, malformed=0,
                        layout="wide", term_width=168,
                    ))
                    self.assertNotIn("(orphan)", rendered)
                    self.assertIn("stale-child", rendered)
        finally:
            render.set_show_all(False)
            render.reset_selection()
            model.reset_parent_edge_tracker()


class RenderConsumesLedgerTest(unittest.TestCase):
    """render must consume _parent_edge_sid/_parent_edge_promoted_orphan, never re-derive
    orphan status from shown_sids on its own (C6 / round-1 blocking finding G1)."""

    def setUp(self):
        render.set_process_view(False)
        render.set_show_all(False)
        render.reset_selection()
        model.reset_parent_edge_tracker()

    def tearDown(self):
        render.set_process_view(False)
        render.set_show_all(False)
        render.reset_selection()
        model.reset_parent_edge_tracker()

    def _render(self, sessions, jobs, term_width=168):
        return _text(render._build_lines(
            sessions, jobs, section="both", narrow=False, malformed=0,
            layout="wide", term_width=term_width,
        ))

    def test_grace_held_edge_renders_without_orphan_marker(self):
        # Parent is filtered off-screen (stale) this tick, but the ledger confirmed the
        # edge — render must not fall back to its own shown_sids check and orphan it.
        parent = Session(harness="claude", pid=1, cwd="/work/grace", session_id="sid-p",
                         slug="grace-parent", liveness="stale")
        job = DispatchJob(key="code", slug="grace-child", cwd="/work/grace",
                          parent_sid="sid-p", is_child=True, harness="claude",
                          liveness="working")
        job._parent_edge_sid = "sid-p"                 # collector already confirmed/graced
        job._parent_edge_promoted_orphan = False
        rendered = self._render([parent], [job])
        self.assertNotIn("(orphan)", rendered)
        self.assertIn("grace-child", rendered)

    def test_promoted_orphan_edge_renders_with_orphan_marker(self):
        parent = Session(harness="claude", pid=1, cwd="/work/dead", session_id="sid-p",
                         slug="dead-parent", liveness="dead")
        job = DispatchJob(key="code", slug="dead-child", cwd="/work/dead",
                          parent_sid="sid-p", is_child=True, harness="claude",
                          liveness="working")
        job._parent_edge_sid = None                    # collector already promoted it
        job._parent_edge_promoted_orphan = True
        rendered = self._render([], [job], term_width=200)
        self.assertIn("(orphan)", rendered)
        self.assertIn("dead-child", rendered)

    def test_show_all_does_not_override_promoted_orphan_depth1(self):
        # impl-review gap (round 1 G1 not yet closed in code): with a dead parent AND
        # --all on, render's depth-1 elif ladder used to check `j.parent_sid in
        # shown_sids` BEFORE consuming the ledger's promoted-orphan verdict. --all puts
        # the dead parent into shown_sids, so the child was nested inside the dead
        # parent's card instead of surfacing as an orphan. This must fail on the
        # pre-fix code and pass once the ledger check runs first.
        parent = Session(harness="claude", pid=1, cwd="/work/dead", session_id="sid-p",
                         slug="dead-parent", liveness="dead")
        job = DispatchJob(key="code", slug="dead-child", cwd="/work/dead",
                          parent_sid="sid-p", is_child=True, harness="claude",
                          liveness="working")
        job._parent_edge_sid = None                    # collector already promoted it
        job._parent_edge_promoted_orphan = True
        render.set_show_all(True)
        try:
            rendered = self._render([parent], [job], term_width=200)
        finally:
            render.set_show_all(False)
        self.assertIn("(orphan)", rendered)
        self.assertIn("dead-child", rendered)

    def test_show_all_does_not_override_promoted_orphan_depth2(self):
        # Same repro as above, for the dispatch-depth-2 elif ladder (render.py's
        # `parent_slug`/`depth >= 2` branch), which had the identical
        # shown_sids-before-ledger ordering bug.
        parent = Session(harness="claude", pid=1, cwd="/work/dead2", session_id="sid-p2",
                         slug="dead-parent-2", liveness="dead")
        job = DispatchJob(key="code", slug="dead-child-2", cwd="/work/dead2",
                          parent_sid="sid-p2", is_child=True, harness="claude",
                          liveness="working", depth=2, dispatch_depth=2,
                          parent_slug="unrelated-owner-slug")
        job._parent_edge_sid = None
        job._parent_edge_promoted_orphan = True
        render.set_show_all(True)
        try:
            rendered = self._render([parent], [job], term_width=200)
        finally:
            render.set_show_all(False)
        # impl-review round 2 (non-blocking): assert the CHILD row itself carries the
        # marker. A bare `assertIn("(orphan)", rendered)` also passes when only the
        # parent row is marked, which would not prove the ledger reached this job.
        child_rows = [ln for ln in rendered.splitlines() if "dead-child-2" in ln]
        self.assertTrue(child_rows, "child row missing from render")
        self.assertIn("(orphan)", child_rows[0])

    def test_depth2_nests_under_visible_owner_job_despite_promoted_session_edge(self):
        """A promoted SESSION edge does not detach a depth-2 row from its visible OWNER JOB.

        impl-review round 2 raised this ordering as a blocking ledger bypass. It is not:
        `visible_parent_slugs` (render.py:4921-4923) is a job -> job edge (depth-1 owner
        job <- depth-2 stage worker), while the F-80 ledger governs the session -> job
        edge (`parent_sid`). When the owner job is on screen the child's displayed parent
        genuinely exists, so nesting under it is correct and the orphan condition is
        already carried by the owner row. Marking the whole subtree would restate one fact
        on every descendant (F-9/F-12 noise restraint; PRD 4 R2 marks the promoted row).

        Owner arbitration: `_internal/owner/implreview_arbitration.md`.
        """
        owner = DispatchJob(key="code", slug="dead-owner", cwd="/work/d2", harness="claude",
                            liveness="dead", depth=1, dispatch_depth=1,
                            parent_sid="sid-p2", is_child=True)
        owner._parent_edge_sid = None
        owner._parent_edge_promoted_orphan = True
        child = DispatchJob(key="code", slug="dead-child-2b", cwd="/work/d2",
                            parent_sid="sid-p2", is_child=True, harness="claude",
                            liveness="working", depth=2, dispatch_depth=2,
                            parent_slug="dead-owner")
        child._parent_edge_sid = None
        child._parent_edge_promoted_orphan = True
        render.set_show_all(True)
        try:
            rendered = self._render([], [owner, child], term_width=200)
        finally:
            render.set_show_all(False)
        owner_rows = [ln for ln in rendered.splitlines() if "dead-owner" in ln]
        child_rows = [ln for ln in rendered.splitlines() if "dead-child-2b" in ln]
        self.assertTrue(owner_rows and child_rows)
        # The owner carries the marker; the child stays nested beneath it, unmarked.
        self.assertIn("(orphan)", owner_rows[0])
        self.assertNotIn("(orphan)", child_rows[0])
        # ...and it is still inside the orphan section, not lost off the board.
        self.assertIn("orphaned dispatch rows", rendered)

    def test_confirmed_visible_parent_still_nests_normally(self):
        # Happy-path regression: the ledger must not get in the way of the ordinary
        # visible-parent nesting path.
        parent = Session(harness="claude", pid=1, cwd="/work/live", session_id="sid-p",
                         slug="live-parent", liveness="working")
        job = DispatchJob(key="code", slug="live-child", cwd="/work/live",
                          parent_sid="sid-p", is_child=True, harness="claude",
                          liveness="working")
        job._parent_edge_sid = "sid-p"
        job._parent_edge_promoted_orphan = False
        rendered = self._render([parent], [job])
        self.assertNotIn("(orphan)", rendered)
        self.assertIn("CL/sid-p", rendered)
        self.assertIn("live-child", rendered)


class L3EnvSidRecoveryTest(unittest.TestCase):
    """A tap-less dispatch worker session recovers its sid from /proc/<pid>/environ."""

    def test_env_recovery_only_after_tap_failure(self):
        from fleet.collectors import claude
        sess = Session(harness="claude", pid=424242, cwd="/work/worker", session_id=None,
                       proc_start=None)
        with mock.patch.object(claude, "read_registry", return_value=None), \
             mock.patch.object(claude, "_tap_sid_by_pid", return_value=None), \
             mock.patch.object(claude.procscan, "read_environ",
                               return_value={"CLAUDE_CODE_SESSION_ID": "sid-from-env"}):
            claude.enrich(sess)
        self.assertEqual(sess.session_id, "sid-from-env")

    def test_tap_hit_wins_over_env_recovery(self):
        from fleet.collectors import claude
        # title set so the unrelated provenance() best-effort lookup (also read_environ-based,
        # gated on "no title yet") does not confound the assertion below — this test's only
        # subject is whether L3's OWN read_environ call for CLAUDE_CODE_SESSION_ID fires.
        sess = Session(harness="claude", pid=424243, cwd="/work/worker", session_id=None,
                       proc_start="777", title="already-titled")
        with mock.patch.object(claude, "read_registry", return_value=None), \
             mock.patch.object(claude, "_tap_sid_by_pid", return_value="sid-from-tap"), \
             mock.patch.object(claude.procscan, "read_environ") as mock_env:
            claude.enrich(sess)
        self.assertEqual(sess.session_id, "sid-from-tap")
        mock_env.assert_not_called()

    def test_env_recovery_failure_is_graceful(self):
        from fleet.collectors import claude
        sess = Session(harness="claude", pid=424244, cwd="/work/worker", session_id=None,
                       proc_start=None)
        with mock.patch.object(claude, "read_registry", return_value=None), \
             mock.patch.object(claude, "_tap_sid_by_pid", return_value=None), \
             mock.patch.object(claude.procscan, "read_environ", return_value={}):
            claude.enrich(sess)   # must not raise
        self.assertIsNone(sess.session_id)


if __name__ == "__main__":
    unittest.main()
