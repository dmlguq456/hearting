#!/usr/bin/env python3
"""F-100c (user 2026-09-03) — steward flag on the board, three harnesses alike.

The ledger tool writes a marker for every SENDING session whose record is a
steer/handoff/gate-relay/watch; the Fleet steward collector joins it by exact
(harness, session_id); the tag badge then wears bold yellow (`[46]`) and an
untagged steward still gets `[*]`, with a legend entry.
"""
import json
import os
import sys
import tempfile
import unittest

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import render                                          # noqa: E402
from fleet.model import Session                                   # noqa: E402
from fleet.collectors import steward                              # noqa: E402


def _text(segs):
    return "".join(t for t, _k in segs)


class StewardChipTest(unittest.TestCase):
    def _s(self, **over):
        base = dict(harness="claude", pid=1, cwd="/x", slug="s", title="t", liveness="idle",
                    elapsed_min=1)
        base.update(over)
        return Session(**base)

    def test_steward_tag_is_bold_yellow_and_keeps_the_slot_width(self):
        segs = render._session_tag_chip(self._s(session_tag="46", steward=True))
        self.assertEqual(segs, [("[", "dim"), ("46", "tag_steward"), ("]", "dim"), (" ", None)])
        self.assertEqual(sum(render._dw(t) for t, _k in segs), render._TAG_W)
        self.assertEqual(render._HUE_OF["tag_steward"], ("y", render._A_BOLD))

    def test_untagged_steward_gets_a_star_badge(self):
        for harness in ("codex", "opencode"):
            with self.subTest(harness=harness):
                segs = render._session_tag_chip(self._s(harness=harness, steward=True))
                self.assertEqual(segs[1], ("* ", "tag_steward"))
                self.assertEqual(sum(render._dw(t) for t, _k in segs), render._TAG_W)

    def test_non_steward_rows_are_unchanged(self):
        self.assertEqual(render._session_tag_chip(self._s(session_tag="46"))[1], ("46", "tag"))
        self.assertEqual(render._session_tag_chip(self._s()), [(" " * render._TAG_W, None)])

    def test_dim_rows_stay_dim_even_as_steward(self):
        segs = render._session_tag_chip(self._s(session_tag="46", steward=True, liveness="stale"),
                                        dim=True)
        self.assertEqual(segs[1], ("46", "tag_dim"))

    def test_legend_entry_appears_only_when_a_steward_is_on_screen(self):
        def legend(**over):
            base = dict(harness="claude", pid=1, cwd="/x", slug="s", liveness="idle",
                        ctx_pct=10, elapsed_min=1)
            base.update(over)
            lines = render._build_lines([Session(**base)], [], "fleet", False, 0,
                                        layout="wide", term_width=168)
            return _text([ln for ln in lines if ln][-1])
        self.assertNotIn("steward", legend(session_tag="46"))
        self.assertIn("steward", legend(session_tag="46", steward=True))


class StewardOrderTest(unittest.TestCase):
    """F-100c — a steward leads its repo group even when idle; everything else keeps
    the liveness → elapsed order it always had."""

    def _s(self, sid, **over):
        base = dict(harness="claude", pid=1, cwd="/x", slug=sid, session_id=sid,
                    liveness="idle", elapsed_min=5)
        base.update(over)
        return Session(**base)

    def test_steward_sorts_first_within_the_group(self):
        working = self._s("w", liveness="working", elapsed_min=50)
        idle_old = self._s("i", liveness="idle", elapsed_min=900)
        steward = self._s("s", liveness="idle", elapsed_min=1, steward=True)
        detached = self._s("d", liveness="idle", detached=True, elapsed_min=2000)
        ordered = render._sort_group_sessions([detached, idle_old, working, steward])
        self.assertEqual([s.session_id for s in ordered], ["s", "w", "i", "d"])

    def test_order_among_non_stewards_is_unchanged(self):
        a = self._s("a", liveness="working", elapsed_min=10)
        b = self._s("b", liveness="working", elapsed_min=30)
        c = self._s("c", liveness="idle", elapsed_min=5)
        self.assertEqual([s.session_id for s in render._sort_group_sessions([c, a, b])],
                         ["b", "a", "c"])

    def test_steward_row_renders_at_the_top_of_its_project_card(self):
        sessions = [self._s("w", liveness="working", elapsed_min=50, session_tag="0a"),
                    self._s("s", liveness="idle", elapsed_min=1, session_tag="46", steward=True)]
        lines = render._build_lines(sessions, [], "fleet", False, 0, layout="wide", term_width=168)
        visible = [_text(ln) for ln in lines if ln]
        first = next(i for i, l in enumerate(visible) if "[46]" in l)
        second = next(i for i, l in enumerate(visible) if "[0a]" in l)
        self.assertLess(first, second)


class StewardCollectorTest(unittest.TestCase):
    def test_join_is_exact_on_harness_and_session_id(self):
        sessions = [Session(harness="claude", pid=1, session_id="sid-a"),
                    Session(harness="codex", pid=2, session_id="sid-a"),
                    Session(harness="claude", pid=3, session_id="sid-b"),
                    Session(harness="claude", pid=4)]
        markers = {("claude", "sid-a"): {"session_id": "sid-a", "targets": {
            "x": {"harness": "codex", "session_id": "x", "name": "w", "kind": "steer", "ts": "2"},
            "y": {"harness": "claude", "session_id": "y", "name": "v", "kind": "watch", "ts": "1"}}}}
        steward.enrich(sessions, markers=markers)
        self.assertEqual([s.steward for s in sessions], [True, False, False, False])
        self.assertEqual([t["session_id"] for t in sessions[0].steward_targets], ["y", "x"])
        self.assertIsNone(sessions[1].steward_targets)

    def test_markers_round_trip_through_the_ledger_tool(self):
        """The writer (`peer-message record`) and the reader agree on the path layout."""
        import importlib.util
        tool = os.path.join(os.path.dirname(_TOOLS_DIR), "utilities", "peer-message.py")
        spec = importlib.util.spec_from_file_location("_pm", tool)
        pm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pm)
        with tempfile.TemporaryDirectory() as tmp:
            old = dict(os.environ)
            os.environ["AGENT_DISPATCH_JOBS"] = os.path.join(tmp, "jobs.log")
            os.environ.pop("AGENT_HOME", None)
            try:
                open(os.environ["AGENT_DISPATCH_JOBS"], "w").close()
                ns = pm.argparse.Namespace(
                    from_harness="claude", from_session_id="sid-a", from_project="p",
                    from_name="hearting-46", to_harness="codex", to_session_id="sid-c",
                    to_name="child", kind="steer", surface="herdr", status="sent",
                    receipt=None, ref=[], body_file=None, body_stdin=False)
                self.assertEqual(pm.cmd_record(ns), 0)
                markers = steward.read_markers()
                self.assertIn(("claude", "sid-a"), markers)
                sessions = [Session(harness="claude", pid=1, session_id="sid-a")]
                steward.enrich(sessions, markers=markers)
                self.assertTrue(sessions[0].steward)
                self.assertEqual(sessions[0].steward_targets[0]["session_id"], "sid-c")
                self.assertEqual(pm.cmd_release(pm.argparse.Namespace(
                    harness="claude", session_id="sid-a")), 0)
                self.assertNotIn(("claude", "sid-a"), steward.read_markers())
            finally:
                os.environ.clear()
                os.environ.update(old)


if __name__ == "__main__":
    unittest.main()
