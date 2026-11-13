"""F-57 (v41) — the wide main row's dead inline-context reservation is RECLAIMED.

History: this file was `test_wide_ctx_gauge.py` and guarded a 2026-07-16 ask ("context
window의 길이를 더 늘려서 맞춰주고") that widened an INLINE ctx gauge on the wide session
row. F-37a (v16) then moved context telemetry to the subordinate detail row and deleted
that gauge — but its width ledger (`_CTX_W` 24, `_CTX_BOOST` 12, the `ctx_width` plumbing)
survived and kept ~19 columns of the 168-col row permanently blank while the title was
still capped below `_NAME_WIDE_MAX`.

F-57 removed the ledger entries themselves. These tests now assert the reclamation holds:
the row draws no inline gauge, the freed cells go to the title, and nothing overflows.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import render                                     # noqa: E402
from fleet.model import Session                               # noqa: E402


class DeadReservationRemovedTest(unittest.TestCase):
    def test_the_reservation_symbols_are_gone_not_merely_zeroed(self):
        """F-57 is a plumbing removal, not a `= 0` disguise. A future reader must not
        find a zero-valued knob and 'restore' it."""
        for name in ("_CTX_W", "_CTX_BOOST", "_wide_ctx_width", "_wide_alloc"):
            self.assertFalse(hasattr(render, name),
                             "%s should have been removed by F-57" % name)

    def test_session_row_no_longer_accepts_a_ctx_width_argument(self):
        import inspect
        params = inspect.signature(render._session_row).parameters
        self.assertNotIn("ctx_width", params)


class WideNameWidthLedgerTest(unittest.TestCase):
    """`_wide_slack` now feeds exactly one consumer: the name column."""

    def test_no_width_returns_the_floor(self):
        self.assertEqual(render._wide_name_width(None), render._NW_S)
        self.assertEqual(render._wide_name_width(0), render._NW_S)

    def test_monotonic_between_the_floor_and_the_cap(self):
        widths = [render._wide_name_width(w) for w in range(1, 400)]
        self.assertTrue(all(b >= a for a, b in zip(widths, widths[1:])), "not monotonic")
        self.assertTrue(all(render._NW_S <= w <= render._NAME_WIDE_MAX for w in widths))

    def test_all_reclaimed_slack_reaches_the_title(self):
        """Every cell of slack past the `_NW_S` floor goes to the name until the cap —
        no intermediate consumer skims the first 12 (the retired `_CTX_BOOST`)."""
        for w in range(60, 401):
            raw = render._wide_slack(w)
            surplus = max(0, raw - render._NW_S)
            expected = render._NW_S + min(render._NAME_WIDE_MAX - render._NW_S, surplus)
            self.assertEqual(render._wide_name_width(w), expected, "term_width=%d" % w)

    def test_the_cap_is_reached_earlier_than_before_the_reclaim(self):
        """The reclaimed budget is exactly `_CTX_W` (24) + `_CTX_BOOST` (12) = 36, so the
        40-cell cap that used to need 176 columns arrived at 140 — F-58's `_HMW` 42→32
        moved it 10 further to 130, F-64's 32→35→38 (v49) took 6 back, and F-65's
        38→40 took 2 more, to 138. The reclaim itself is what this asserts; the exact
        column is restated here only so a silent re-reservation cannot hide behind it."""
        self.assertLess(render._wide_name_width(137), render._NAME_WIDE_MAX)
        self.assertEqual(render._wide_name_width(138), render._NAME_WIDE_MAX)


class SessionRowContextTest(unittest.TestCase):
    """The v16 context contract keeps telemetry in the subordinate detail row."""

    def _row_text(self, term_width):
        s = Session(harness="claude", pid=1, cwd="/home/u/proj", slug="proj",
                    title="x", liveness="idle", ctx_pct=42, model="Fable 5",
                    effort="xhigh", elapsed_min=42, branch="main")
        name_w = render._wide_name_width(term_width) if term_width else None
        segs = render._session_row(s, narrow=False, name_width=name_w)
        return "".join(t for t, _k in segs if t != render._RFLUSH)

    def test_primary_row_renders_no_inline_context_gauge(self):
        s = Session(harness="claude", pid=1, cwd="/home/u/proj", slug="proj",
                    title="x", liveness="idle", ctx_pct=None, elapsed_min=1)
        text = "".join(t for t, _k in render._session_row(s, narrow=False))
        self.assertNotIn("42%", text)
        self.assertNotIn("📚", text)

    def test_no_overflow_at_wide_layout_widths(self):
        """140/168/200/400 are where the board actually picks the wide layout
        (`_layout_mode` only returns "wide" at >=138 cols) — 60/120 render
        narrow/stack instead, a different row builder this ledger never touches."""
        for term_width in (140, 168, 200, 400):
            self.assertEqual(render._layout_mode(term_width), "wide")
            text = self._row_text(term_width)
            self.assertLessEqual(render._dw(text), term_width,
                                 "term_width=%d row overflowed" % term_width)


if __name__ == "__main__":
    unittest.main()
