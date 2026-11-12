"""Focused coverage for run-local Fleet group/session ordering."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
import sys
import unittest

_TOOLS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

from fleet import render  # noqa: E402
from fleet.model import Session  # noqa: E402


def _session(sid, cwd="/work/alpha", live="idle", elapsed=1, pid=1):
    return Session("codex", pid, cwd=cwd, session_id=sid, slug=sid,
                   liveness=live, elapsed_min=elapsed)


def _render_text(sessions, live_order=None):
    return "\n".join("".join(text for text, _style in row) for row in render._build_lines(
        sessions, [], "fleet", False, 0, layout="wide", live_order=live_order) if row)


class LiveOrderStateTest(unittest.TestCase):
    def test_groups_survive_swap_append_prune_and_reset(self):
        state = render._LiveOrderState()
        self.assertEqual(state.reconcile_groups(["alpha", "beta"]), ["alpha", "beta"])
        self.assertEqual(state.reconcile_groups(["beta", "alpha"]), ["alpha", "beta"])
        self.assertEqual(state.reconcile_groups(["gamma", "beta", "alpha"]),
                         ["alpha", "beta", "gamma"])
        self.assertEqual(state.reconcile_groups(["beta"]), ["beta"])
        self.assertEqual(state.groups, ["beta"])
        self.assertEqual(state.reconcile_groups(["alpha", "beta"]), ["beta", "alpha"])
        self.assertEqual(render._LiveOrderState().reconcile_groups(["alpha", "beta"]),
                         ["alpha", "beta"])

    def test_groups_repartition_on_tier_change_but_stabilize_within_tier(self):
        state = render._LiveOrderState()
        self.assertEqual(
            state.reconcile_groups(
                ["alpha", "beta", "gamma"], {"alpha": 0, "beta": 1, "gamma": 1}),
            ["alpha", "beta", "gamma"],
        )
        # Snapshot recency flips within the idle tier, but survivors stay put.
        self.assertEqual(
            state.reconcile_groups(
                ["alpha", "gamma", "beta"], {"alpha": 0, "beta": 1, "gamma": 1}),
            ["alpha", "beta", "gamma"],
        )
        # beta is promoted immediately, after the existing working survivor.
        self.assertEqual(
            state.reconcile_groups(
                ["beta", "alpha", "gamma"], {"alpha": 0, "beta": 0, "gamma": 1}),
            ["alpha", "beta", "gamma"],
        )
        # alpha's demotion is immediate and appends after idle-tier survivors.
        self.assertEqual(
            state.reconcile_groups(
                ["beta", "alpha", "gamma"], {"alpha": 1, "beta": 0, "gamma": 1}),
            ["beta", "gamma", "alpha"],
        )

    def test_sessions_survive_swap_append_and_prune(self):
        state = render._LiveOrderState()
        a, b, c = _session("a", elapsed=2, pid=1), _session("b", elapsed=1, pid=2), _session("c", pid=3)
        self.assertEqual([s.session_id for s in state.reconcile_sessions("alpha", [a, b])], ["a", "b"])
        self.assertEqual([s.session_id for s in state.reconcile_sessions("alpha", [b, a])], ["a", "b"])
        self.assertEqual([s.session_id for s in state.reconcile_sessions("alpha", [c, b, a])],
                         ["a", "b", "c"])
        self.assertEqual([s.session_id for s in state.reconcile_sessions("alpha", [b])], ["b"])
        self.assertEqual([s.session_id for s in state.reconcile_sessions("alpha", [a, b])], ["b", "a"])

    def test_identity_keeps_row_variants_and_pid_start(self):
        ordinary = _session("same", pid=1)
        app = _session("same", pid=2)
        app.app_server = True
        self.assertNotEqual(render._live_session_identity(ordinary), render._live_session_identity(app))
        first = _session(None, pid=7)
        first.proc_start = "11"
        reused = _session(None, pid=7)
        reused.proc_start = "12"
        self.assertNotEqual(render._live_session_identity(first), render._live_session_identity(reused))

    def test_build_lines_without_state_remains_snapshot_sorted(self):
        old_show, old_process = render._SHOW_ALL, render._PROCESS_VIEW
        try:
            render.set_show_all(True)
            render.set_process_view(False)
            a = _session("a", cwd="/work/alpha", live="idle", elapsed=1, pid=1)
            b = _session("b", cwd="/work/beta", live="working", elapsed=1, pid=2)
            text = "\n".join("".join(t for t, _ in row) for row in render._build_lines(
                [a, b], [], "fleet", False, 0, layout="wide") if row)
            self.assertLess(text.index("beta/"), text.index("alpha/"))
        finally:
            render.set_show_all(old_show)
            render.set_process_view(old_process)

    def test_live_groups_promote_immediately_and_stay_stable_when_both_working(self):
        old_show, old_process = render._SHOW_ALL, render._PROCESS_VIEW
        try:
            render.set_show_all(True)
            render.set_process_view(False)
            alpha = _session("a", cwd="/work/alpha", live="idle", elapsed=1, pid=1)
            beta = _session("b", cwd="/work/beta", live="idle", elapsed=1, pid=2)
            alpha.mtime, beta.mtime = 20, 10
            state = render._LiveOrderState()

            initial = _render_text([alpha, beta], live_order=state)
            self.assertLess(initial.index("alpha/"), initial.index("beta/"))

            beta.liveness = "working"
            promoted = _render_text([alpha, beta], live_order=state)
            self.assertLess(promoted.index("beta/"), promoted.index("alpha/"))

            alpha.liveness = "working"
            alpha.mtime, beta.mtime = 100, 1
            both_working = _render_text([alpha, beta], live_order=state)
            self.assertLess(both_working.index("beta/"), both_working.index("alpha/"))
        finally:
            render.set_show_all(old_show)
            render.set_process_view(old_process)

    def test_folded_summary_survives_live_prune_and_reveal_appends(self):
        old_show, old_process = render._SHOW_ALL, render._PROCESS_VIEW
        try:
            render.set_show_all(False)
            render.set_process_view(False)
            stale = _session("a", cwd="/work/alpha", live="stale", elapsed=99, pid=1)
            survivor = _session("b", cwd="/work/beta", live="idle", elapsed=1, pid=2)
            state = render._LiveOrderState()

            snapshot = _render_text([stale, survivor])
            live = _render_text([stale, survivor], live_order=state)
            folded_snapshot = [line for line in snapshot.splitlines() if "inactive  +" in line]
            folded_live = [line for line in live.splitlines() if "inactive  +" in line]
            self.assertEqual(folded_live, folded_snapshot)
            self.assertEqual(folded_live, ["· inactive  +1 folded   alpha"])
            self.assertEqual(state.groups, ["beta"])
            self.assertNotIn("alpha", state.sessions)

            render.set_show_all(True)
            shown_all = _render_text([stale, survivor], live_order=state)
            self.assertLess(shown_all.index("beta/"), shown_all.index("alpha/"))
            self.assertEqual(state.groups, ["beta", "alpha"])

            render.set_show_all(False)
            refolded = _render_text([stale, survivor], live_order=state)
            self.assertIn("· inactive  +1 folded   alpha", refolded)
            self.assertEqual(state.groups, ["beta"])
            self.assertNotIn("alpha", state.sessions)

            stale.liveness = "working"
            rerevealed = _render_text([stale, survivor], live_order=state)
            self.assertLess(rerevealed.index("alpha/"), rerevealed.index("beta/"))
            self.assertEqual(state.groups, ["alpha", "beta"])
        finally:
            render.set_show_all(old_show)
            render.set_process_view(old_process)


if __name__ == "__main__":
    unittest.main()
