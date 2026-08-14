"""F-75 — the depth-1 conductor breadcrumb moves from the owner row to the card's close rail.

User 2026-08-14: "박스를 만들면서 하단 가로줄이 한줄을 잡아먹는데, 거기에 … 우측 정렬 같은걸
해서 놓는건 어떨까". Before this, the whole route rode the owner row, which is also the box's
TOP edge: at 140 columns a 7-node strong route clipped to `re…`, and at 200 it shoved the frame
around. The bottom rule was pure decoration.

Now the rule hosts the breadcrumb right-flushed and the owner row keeps only the compact
`<node> <done>/<total>` — the same short form a main session row has always used, so both
altitudes read alike. This suite pins the parts that silently regress: the flush geometry
(every card's label ends on the same column), the ledger (a label never overruns the corner),
and the two suppressions (one-node route, no sealed route).
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import render                                          # noqa: E402
from fleet.model import DispatchJob, ProgressProjection, WorkProjection   # noqa: E402


LONG_ROUTE = [("frame", "done"), ("plan", "done"), ("plan-check", "done"),
              ("execute", "active"), ("impl-review", "pending"),
              ("test", "pending"), ("report", "pending")]


def _text(segs):
    return "".join(t for t, _k in segs)


def _w(segs):
    return sum(render._dw(t) for t, _k in segs)


def _owner(route_seq=LONG_ROUTE, done=3, total=7):
    nodes = [{"id": nid, "state": st} for nid, st in route_seq]
    work = WorkProjection(
        source="route-exact", route_id="rt-f75", stage_label="execute",
        node_state="active", progress=ProgressProjection(done, total),
        _route_view={"view": {"nodes": nodes}},
    )
    return DispatchJob(key="code", slug="f75-owner", cwd="/tmp/f75", harness="claude",
                       depth=1, intensity="thorough", liveness="working",
                       work_projection=work)


class BottomRailGeometryTest(unittest.TestCase):

    def test_label_is_right_flushed_with_a_fixed_tail(self):
        """The user chose right flush over centering: the label's END lands on the same
        column on every card, so a stack of cards shares one baseline."""
        for box_width in (100, 139, 180, 240):
            label = [("execute › test › report", "dim")]
            segs = render._dispatch_box_bottom(box_width, "frm_idle", label_segs=label)
            self.assertEqual(_w(segs), box_width, "box %d" % box_width)
            tail = _text(segs).rstrip("╯")
            self.assertTrue(tail.endswith(" " + "─" * render._BOT_LABEL_TAIL),
                            "box %d tail: %r" % (box_width, tail[-8:]))

    def test_bare_rail_when_no_label(self):
        segs = render._dispatch_box_bottom(139, "frm_idle")
        body = _text(segs).strip()
        self.assertTrue(body.startswith("╰") and body.endswith("╯"))
        self.assertEqual(set(body[1:-1]), {"─"})
        self.assertEqual(_w(segs), 139)

    def test_oversized_label_is_refused_whole_not_clipped(self):
        """The budget is one ledger. A label wider than it never half-draws against the
        corner — the caller is expected to have folded it, and the rail stays bare."""
        box_width = 80
        over = render.bottom_label_budget(box_width) + 1
        segs = render._dispatch_box_bottom(box_width, "frm_idle",
                                           label_segs=[("x" * over, "dim")])
        self.assertNotIn("x", _text(segs))
        self.assertEqual(_w(segs), box_width)

    def test_label_at_exactly_the_budget_still_fits(self):
        box_width = 139
        exact = render.bottom_label_budget(box_width)
        segs = render._dispatch_box_bottom(box_width, "frm_idle",
                                           label_segs=[("y" * exact, "dim")])
        self.assertIn("y" * exact, _text(segs))
        self.assertEqual(_w(segs), box_width)

    def test_rule_keeps_the_steady_grain_while_corners_blink(self):
        """F-68's grain contract survives the new label: horizontal runs wear `run_key`,
        corners wear `key`."""
        segs = render._dispatch_box_bottom(139, "frm3", run_key="frm_idle",
                                           label_segs=[("execute", "stg0_on")])
        rule_keys = {k for t, k in segs if t and set(t) == {"─"}}
        corner_keys = {k for t, k in segs if t in ("╰", "╯")}
        self.assertEqual(rule_keys, {"frm_idle"})
        self.assertEqual(corner_keys, {"frm3"})


class RouteShownExactlyOnceTest(unittest.TestCase):
    """F-75b — one card, one place for the route (user: "위아래로 동시에 뜨니까 어지럽네")."""

    def test_multi_node_route_leaves_the_owner_slot_to_the_rail(self):
        self.assertTrue(render._route_rides_the_rail(_owner(), LONG_ROUTE, in_card=True))

    def test_one_node_route_stays_on_the_owner_row(self):
        """The rail suppresses a one-node breadcrumb, so the row must keep it — otherwise
        the node would appear nowhere at all."""
        seq = [("one-shot", "active")]
        job = _owner(route_seq=seq, done=0, total=1)
        self.assertFalse(render._route_rides_the_rail(job, seq, in_card=True))
        segs = render._dispatch_stage_segs(job, "code", "one-shot", "f75-owner",
                                           working=True, route_seq=seq,
                                           route_zone=40, compact_route=True)
        self.assertEqual(_text(segs), "one-shot 0/1")

    def test_unframed_owner_keeps_its_route_on_the_row(self):
        self.assertFalse(render._route_rides_the_rail(_owner(), LONG_ROUTE, in_card=False))

    def test_depth2_micro_status_is_never_suppressed(self):
        """A child row's slot holds its OWN status (`running`), which the rail does not
        carry — suppressing it would delete information, not relocate it."""
        child = DispatchJob(key="code-execute", slug="f75-child", cwd="/tmp/f75",
                            harness="opencode", depth=2, liveness="working")
        self.assertFalse(render._route_rides_the_rail(child, LONG_ROUTE, in_card=True))
        segs = render._dispatch_stage_segs(child, "code-execute", "exec", "f75-child",
                                           working=True, route_seq=LONG_ROUTE,
                                           compact_route=True)
        self.assertEqual(_text(segs), "running")


class OwnerRowCompactTest(unittest.TestCase):

    def test_compact_form_is_where_now_only(self):
        job = _owner()
        segs = render._dispatch_stage_segs(job, "code", "execute", "f75-owner",
                                           working=True, route_seq=LONG_ROUTE,
                                           route_zone=40, compact_route=True)
        self.assertEqual(_text(segs), "execute 3/7")
        self.assertNotIn("›", _text(segs))

    def test_unframed_row_keeps_the_full_breadcrumb(self):
        """Outside a card there is no rail to move the pipeline to, so the F-28b behavior
        is untouched — this is what keeps orphan/unframed rows honest."""
        job = _owner()
        segs = render._dispatch_stage_segs(job, "code", "execute", "f75-owner",
                                           working=True, route_seq=LONG_ROUTE,
                                           route_zone=60, compact_route=False)
        self.assertIn("›", _text(segs))

    def test_compact_form_folds_whole_components_never_mid_token(self):
        """F-9(c): when the slot cannot hold both, the COUNT survives alone — the node name
        is still lit on the rail below, while `3/7` exists nowhere else on the card."""
        job = _owner()
        segs = render._dispatch_stage_segs(job, "code", "execute", "f75-owner",
                                           working=True, route_seq=LONG_ROUTE,
                                           route_zone=6, compact_route=True)
        self.assertEqual(_text(segs), "3/7")
        self.assertNotIn("…", _text(segs))

    def test_compact_marks_a_failed_current_node(self):
        seq = [("plan", "done"), ("execute", "failed"), ("test", "pending")]
        job = _owner(route_seq=seq, done=1, total=3)
        segs = render._dispatch_stage_segs(job, "code", "execute", "f75-owner",
                                           working=False, route_seq=seq,
                                           route_zone=40, compact_route=True)
        self.assertEqual(_text(segs), "execute✕ 1/3")
        self.assertIn("lvl_r", [k for _t, k in segs])

    def test_owner_row_and_rail_agree_on_the_current_node(self):
        """Both read `_route_current_index`; a card that disagreed with itself would be
        worse than either form alone."""
        job = _owner()
        compact = _text(render._dispatch_stage_segs(
            job, "code", "execute", "f75-owner", working=True, route_seq=LONG_ROUTE,
            route_zone=40, compact_route=True))
        rail = _text(render._route_stage_segs(LONG_ROUTE, True, 120))
        self.assertTrue(compact.startswith("execute"))
        self.assertIn("execute", rail)


class CardIntegrationTest(unittest.TestCase):
    """End-to-end through `_build_lines`, the surface the user actually reads."""

    def _render(self, job, width=140, layout="wide"):
        from fleet.model import Session
        session = Session(harness="claude", pid=910, proc_start="root", cwd="/tmp/f75",
                          session_id="sid-f75", slug="f75-parent", liveness="working")
        job.parent_sid = "sid-f75"
        job.is_child = True
        lines = render._build_lines([session], [job], "both", False, 0,
                                    layout=layout, term_width=width)
        # Render-internal placeholders (the card tag, the right-flush split) are consumed by
        # the frame/paint layer and occupy no cell on screen; strip them before measuring.
        return [re.sub(r"\x00[^\x00]*\x00", "", _text(l)) for l in lines if l]

    @staticmethod
    def _rail_run(rail_row):
        """The rule between the two corners, as it reaches the terminal."""
        return rail_row[rail_row.index("╰") + 1:rail_row.rindex("╯")]

    def test_route_rides_the_close_rail_not_the_owner_row(self):
        rows = self._render(_owner())
        rail = [r for r in rows if "╰" in r]
        owner = [r for r in rows if "╭" in r]
        self.assertTrue(rail and owner)
        self.assertIn("plan-check✓", rail[0])
        self.assertIn("report", rail[0])
        self.assertNotIn("›", owner[0])
        # F-75b: the owner row shows NO stage cell at all — not the compact token and not
        # the bare ` : -` lead-in, which would read as absent rather than relocated.
        self.assertNotIn("execute", owner[0])
        self.assertNotIn(" : ", owner[0])

    def test_child_row_keeps_its_own_status_inside_the_card(self):
        job = _owner()
        child = DispatchJob(key="code-execute", slug="f75-child", cwd="/tmp/f75",
                            harness="opencode", depth=2, liveness="working",
                            parent_slug="f75-owner", is_child=True)
        from fleet.model import Session
        session = Session(harness="claude", pid=911, proc_start="root", cwd="/tmp/f75",
                          session_id="sid-f75b", slug="f75-parent", liveness="working")
        job.parent_sid, job.is_child = "sid-f75b", True
        rows = [_text(l) for l in render._build_lines(
            [session], [job, child], "both", False, 0, layout="wide", term_width=180) if l]
        child_rows = [r for r in rows if "f75-child" in r]
        self.assertTrue(child_rows)
        self.assertIn("running", child_rows[0])

    def test_no_card_row_overruns_its_frame(self):
        """The frame is a hard edge — this is the regression F-65 chased when a wrapped row
        shifted every following screen line and the close rail visually vanished."""
        for width in (140, 168, 200):
            for row in self._render(_owner(), width=width):
                if not any(mark in row for mark in ("╭", "│", "╰")):
                    continue
                self.assertLessEqual(render._dw(row), width,
                                     "width %d overrun: %r" % (width, row))

    def test_one_node_route_leaves_the_rail_bare(self):
        """A one-node route is not a pipeline: its breadcrumb would spell the owner row's
        own compact token a second time (the F-37 single-render contract)."""
        seq = [("one-shot", "active")]
        rows = self._render(_owner(route_seq=seq, done=0, total=1))
        rail = [r for r in rows if "╰" in r]
        self.assertTrue(rail)
        self.assertEqual(set(self._rail_run(rail[0])), {"─"})

    def test_routeless_card_keeps_a_bare_rail(self):
        """F-3/F-42a: no sealed route means no track — never a fabricated one on the rule."""
        job = DispatchJob(key="code", slug="f75-noroute", cwd="/tmp/f75", harness="claude",
                          depth=1, intensity="quick", liveness="working", stage="open")
        rows = self._render(job)
        rail = [r for r in rows if "╰" in r]
        self.assertTrue(rail)
        self.assertEqual(set(self._rail_run(rail[0])), {"─"})


class FinishedRowNameHueTest(unittest.TestCase):
    """F-75b — user 2026-08-14: "done worker의 session에 컬러는 유지하는게 좋겠어 회색이니까
    설명이랑 구분이 안되네".

    The harness hue exists on dispatch titles precisely so a title line does not read the
    same as the grey summary under it (user 2026-07-20). A finished row needs that contrast
    just as much as a live one, and F-64b already settled the principle for model/effort:
    afterglow/stale keep the attempt's static identity; only DEAD collapses (F-13)."""

    def _name_key(self, **kw):
        job = DispatchJob(key="code", slug="finished-worker", cwd="/tmp/f75",
                          harness="opencode", depth=2, **kw)
        segs = render._dispatch_row(job, name_width=40)
        return next(k for t, k in segs if "finished-worker" in t)

    def test_afterglow_keeps_the_harness_hue(self):
        self.assertEqual(self._name_key(liveness="idle", afterglow=True),
                         render._NAME_KEY_DIM["opencode"])

    def test_stale_keeps_the_harness_hue(self):
        self.assertEqual(self._name_key(liveness="stale"),
                         render._NAME_KEY_DIM["opencode"])

    def test_finished_name_hue_matches_the_harness(self):
        """The key changed under F-76b; what the user asked for is the HUE."""
        key = self._name_key(liveness="stale")
        self.assertEqual(render._HUE_OF[key][0], render._HUE_OF["h_opencode"][0])

    def test_dead_still_collapses_to_colorless(self):
        self.assertEqual(self._name_key(liveness="dead"), "nm_dead")

    def test_narrow_row_follows_the_same_rule(self):
        for liveness, kw, expected in (("idle", {"afterglow": True},
                                        render._NAME_KEY_DIM["opencode"]),
                                       ("stale", {}, render._NAME_KEY_DIM["opencode"]),
                                       ("dead", {}, "nm_dead")):
            job = DispatchJob(key="code", slug="finished-worker", cwd="/tmp/f75",
                              harness="opencode", depth=2, liveness=liveness, **kw)
            l1, _l2 = render._dispatch_row_2line(job)
            key = next(k for t, k in l1 if "finished-worker" in t)
            self.assertEqual(key, expected, liveness)


if __name__ == "__main__":
    unittest.main()
