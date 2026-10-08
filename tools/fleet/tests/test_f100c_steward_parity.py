#!/usr/bin/env python3
"""F-100c (user 2026-09-03) — steward flag on the board, three harnesses alike.

The ledger tool keeps a marker per session; since 2026-09-06 only role evidence
counts — `steward on` (source=explicit), a SENT `watch` (source=watch) or a `start`
that launched the target (source=start) — and a steer/handoff/gate-relay send is
just a message. The Fleet steward collector joins markers by exact
(harness, session_id) and asks the ledger tool which entries are evidence; the tag
badge then wears bold pink text (`[46]`) and an untagged steward keeps its role mark,
with a legend entry.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

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

    def test_steward_tag_is_bold_pink_text_and_keeps_the_slot_width(self):
        segs = render._session_tag_chip(self._s(session_tag="46", steward=True))
        self.assertEqual(segs, [("[", "dim"), ("46", "tag_steward"), ("]", "dim"), (" ", None)])
        self.assertEqual(sum(render._dw(t) for t, _k in segs), render._TAG_W)
        self.assertEqual(render._HUE_OF["tag_steward"], ("p", render._A_BOLD))

    def test_steward_text_is_pink_without_background_in_curses_and_snapshots(self):
        previous = dict(render._COLOR)
        self.addCleanup(lambda: (render._COLOR.clear(), render._COLOR.update(previous)))
        for colors, foreground in ((256, 219), (8, render.curses.COLOR_MAGENTA)):
            with mock.patch.object(render.curses, "start_color"), \
                 mock.patch.object(render.curses, "use_default_colors"), \
                 mock.patch.object(render.curses, "can_change_color", return_value=False), \
                 mock.patch.object(render.curses, "init_pair") as pair, \
                 mock.patch.object(render.curses, "color_pair", side_effect=lambda value: value << 8), \
                 mock.patch.object(render.curses, "COLORS", colors, create=True):
                render._init_colors()
            self.assertIn(mock.call(18, foreground, -1), pair.call_args_list)
            self.assertFalse(render._COLOR["tag_steward"] & render.curses.A_REVERSE)
            if colors == 256:
                tint = render._TINT_BODY[1]
                self.assertEqual(render._key_attr("tag_steward", tint),
                                 render._TINT_PAIR[(tint, "p")] | render._A_BOLD)
        segs = render._session_tag_chip(self._s(session_tag="46", steward=True))
        self.assertIn("\033[1;38;5;219m46\033[0m", render._snapshot_line(segs, colored=True))
        self.assertIn("\033[1;35m46\033[0m", render._snapshot_line(segs, colored=True, colors=8))
        self.assertNotIn("48;", render._snapshot_line(segs, colored=True))
        self.assertEqual(render._snapshot_line(segs), "[46] ")

    def test_untagged_steward_wears_the_role_mark_not_a_pretend_id(self):
        """The badge column holds session numbers, so `*` there read as an id the user
        could not find (2026-09-09: "그 id 가 안뜨는 경우도 있는것 같은데?"). The steward
        flag is the same glyph this session's relation line already uses."""
        for harness in ("codex", "opencode"):
            with self.subTest(harness=harness):
                segs = render._session_tag_chip(self._s(harness=harness, steward=True))
                self.assertEqual(segs[1], (render._ICON_STEWARD + " ", "tag_steward"))
                self.assertEqual(sum(render._dw(t) for t, _k in segs), render._TAG_W)
                self.assertNotIn("*", "".join(t for t, _k in segs))

    def test_non_steward_rows_are_unchanged(self):
        self.assertEqual(render._session_tag_chip(self._s(session_tag="46"))[1], ("46", "tag"))
        self.assertEqual(render._session_tag_chip(self._s()), [(" " * render._TAG_W, None)])

    def test_dim_rows_stay_dim_even_as_steward(self):
        segs = render._session_tag_chip(self._s(session_tag="46", steward=True, liveness="stale"),
                                        dim=True)
        self.assertEqual(segs[1], ("46", "tag_dim"))

    def test_model_flag_follows_effort_for_all_harnesses_and_layouts(self):
        for harness, model in (("claude", "claude-opus-5.5"),
                               ("codex", "gpt-6.1-sol"),
                               ("opencode", "openai/gpt-6.1-sol")):
            s = self._s(harness=harness, model=model, effort="xhigh",
                        session_tag="46", steward=True)
            for layout in ("wide", "narrow", "stack"):
                with self.subTest(harness=harness, layout=layout):
                    if layout == "wide":
                        row = render._session_row(s, narrow=False)
                    elif layout == "narrow":
                        row = render._session_row_2line(s, term_width=100)[1]
                    else:
                        row = render._session_row_stack(s, term_width=60)[1]
                    self.assertIn(") ⚑", _text(row))
                    self.assertEqual(_text(row).count("⚑"), 1)
                    self.assertIn((" ⚑", "tag_steward"), row)

    def test_model_flag_keeps_the_following_columns_fixed(self):
        for harness, model in (("claude", "claude-opus-5.5"),
                               ("codex", "gpt-6.1-sol"),
                               ("opencode", "provider/a-very-long-model-name")):
            for width in (70, 100, 137, 168):
                with self.subTest(harness=harness, width=width):
                    s = self._s(harness=harness, model=model, effort="xhigh",
                                session_tag="46", title="column-marker")
                    plain = render._session_row(s, narrow=False)
                    plain_l2 = render._session_row_2line(s, term_width=width)[1]
                    s.steward = True
                    marked = render._session_row(s, narrow=False)
                    marked_l2 = render._session_row_2line(s, term_width=width)[1]
                    self.assertEqual(_text(plain).index("column-marker"),
                                     _text(marked).index("column-marker"))
                    self.assertEqual(sum(render._dw(t) for t, _ in plain_l2),
                                     sum(render._dw(t) for t, _ in marked_l2))
                    stage_col = render._NARROW_L2_STAGE_COL
                    self.assertEqual(_text(plain_l2)[stage_col:], _text(marked_l2)[stage_col:])
                    self.assertIn("⚑", _text(marked_l2)[:stage_col])

    def test_model_flag_uses_the_existing_dim_role_color(self):
        s = self._s(model="claude-opus-5.5", effort="xhigh", steward=True,
                    session_tag="46", liveness="stale")
        for row in (render._session_row(s, narrow=False),
                    render._session_row_2line(s, term_width=100)[1]):
            self.assertIn((" ⚑", "tag_dim"), row)

    def test_regular_session_model_has_no_role_flag(self):
        s = self._s(model="claude-opus-5.5", effort="xhigh", session_tag="46")
        for row in (render._session_row(s, narrow=False),
                    render._session_row_2line(s, term_width=100)[1]):
            self.assertNotIn("⚑", _text(row))

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
            "x": {"harness": "codex", "session_id": "x", "name": "w", "kind": "start", "ts": "2",
                  "source": "start"},
            "y": {"harness": "claude", "session_id": "y", "name": "v", "kind": "watch", "ts": "1",
                  "source": "watch"}}}}
        steward.enrich(sessions, markers=markers)
        self.assertEqual([s.steward for s in sessions], [True, False, False, False])
        self.assertEqual([t["session_id"] for t in sessions[0].steward_targets], ["y", "x"])
        self.assertIsNone(sessions[1].steward_targets)

    def test_handoff_only_and_legacy_markers_do_not_make_a_steward(self):
        """Role ≠ communication (user 2026-09-06: "통신만 하면 죄다 d=-1"). A marker whose
        entries are steer/handoff/gate-relay sends — new (source absent, kind=handoff) or
        old (no source at all) — leaves steward=False; a mixed marker keeps only the
        evidence entries in `steward_targets`."""
        sessions = [Session(harness="claude", pid=1, session_id="worker"),
                    Session(harness="claude", pid=2, session_id="legacy-steward"),
                    Session(harness="claude", pid=3, session_id="mixed"),
                    Session(harness="opencode", pid=4, session_id="empty")]
        markers = {
            ("claude", "worker"): {"session_id": "worker", "targets": {
                "w1:p15": {"harness": "claude", "session_id": "steward", "name": "hearting-b0",
                           "kind": "handoff", "ts": "2"},
                "z": {"harness": "codex", "session_id": "z", "name": "z", "kind": "steer", "ts": "1",
                      "source": "steer"}}},
            ("claude", "legacy-steward"): {"session_id": "legacy-steward", "targets": {
                "c": {"harness": "codex", "session_id": "c", "name": "c", "kind": "watch", "ts": "1"}}},
            ("claude", "mixed"): {"session_id": "mixed", "targets": {
                "h": {"harness": "codex", "session_id": "h", "name": "h", "kind": "handoff", "ts": "3"},
                "s": {"harness": "claude", "session_id": "s", "name": "s", "kind": "start", "ts": "2",
                      "source": "start"},
                "-": {"harness": "unknown", "session_id": None, "name": "-", "kind": "explicit",
                      "ts": "1", "source": "explicit"}}},
            ("opencode", "empty"): {"session_id": "empty", "targets": {}},
        }
        steward.enrich(sessions, markers=markers)
        self.assertEqual([s.steward for s in sessions], [False, True, True, False])
        self.assertIsNone(sessions[0].steward_targets)
        self.assertEqual([t["session_id"] for t in sessions[1].steward_targets], ["c"])
        self.assertEqual([t["session_id"] for t in sessions[2].steward_targets], [None, "s"])
        self.assertIsNone(sessions[3].steward_targets)

    def test_without_the_ledger_module_nothing_is_claimed(self):
        sessions = [Session(harness="claude", pid=1, session_id="sid-a")]
        markers = {("claude", "sid-a"): {"session_id": "sid-a", "targets": {
            "y": {"harness": "claude", "session_id": "y", "kind": "watch", "ts": "1", "source": "watch"}}}}
        with mock.patch.object(steward, "_peer_message_module", return_value=None):
            steward.enrich(sessions, markers=markers)
        self.assertFalse(sessions[0].steward)

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
            # C-1 moved the peer ledger's writer root off AGENT_DISPATCH_JOBS onto
            # peer_state_root(); isolating only AGENT_DISPATCH_JOBS leaked this
            # fixture's records into the real per-user ledger root (reproduced during
            # C's verification — see handoff-c-peer-ledger.md).
            os.environ["AGENT_PEER_LEDGER_ROOT"] = os.path.join(tmp, "peer-state")
            os.environ.pop("AGENT_HOME", None)
            try:
                open(os.environ["AGENT_DISPATCH_JOBS"], "w").close()
                ns = pm.argparse.Namespace(
                    from_harness="claude", from_session_id="sid-a", from_project="p",
                    from_name="hearting-46", to_harness="codex", to_session_id="sid-c",
                    to_name="child", kind="handoff", surface="herdr", status="sent",
                    receipt=None, ref=[], body_file=None, body_stdin=False)
                self.assertEqual(pm.cmd_record(ns), 0)
                self.assertNotIn(("claude", "sid-a"), steward.read_markers())   # a handoff is not the role
                ns.kind = "watch"
                self.assertEqual(pm.cmd_record(ns), 0)                          # neither is a watch ROW
                self.assertNotIn(("claude", "sid-a"), steward.read_markers())
                self.assertTrue(pm.mark_steward("claude", "sid-a", {"harness": "codex", "session_id": "sid-c",
                                                                    "name": "child"}, "watch",
                                                "2026-09-06T00:00:00Z", source="watch"))
                markers = steward.read_markers()
                self.assertIn(("claude", "sid-a"), markers)
                sessions = [Session(harness="claude", pid=1, session_id="sid-a")]
                steward.enrich(sessions, markers=markers)
                self.assertTrue(sessions[0].steward)
                self.assertEqual(sessions[0].steward_targets[0]["session_id"], "sid-c")
                self.assertEqual(sessions[0].steward_targets[0]["source"], "watch")
                self.assertEqual(pm.cmd_release(pm.argparse.Namespace(
                    harness="claude", session_id="sid-a")), 0)
                self.assertNotIn(("claude", "sid-a"), steward.read_markers())
            finally:
                os.environ.clear()
                os.environ.update(old)


class RuntimeLedgerRootsTest(unittest.TestCase):
    """F-100c — each installed runtime resolves its own dispatch root, so a Codex or
    OpenCode receiver's `notice` lives under `<runtime home>/.harness/dispatch`; the
    board reads those roots too (existence-gated) for both the ledger and the markers."""

    def test_runtime_roots_are_appended_when_present(self):
        from fleet.collectors import peer_messages
        with tempfile.TemporaryDirectory() as tmp:
            old = dict(os.environ)
            try:
                codex = os.path.join(tmp, "codex"); oc = os.path.join(tmp, "oc"); cl = os.path.join(tmp, "cl")
                os.makedirs(os.path.join(codex, ".harness", "dispatch", "peer-messages"))
                os.makedirs(os.path.join(oc, ".harness", "dispatch", "peer-steward"))
                os.makedirs(cl)                                   # activated, no ledger yet
                os.environ["CODEX_HOME"] = codex
                os.environ["CLAUDE_CONFIG_DIR"] = cl
                os.environ["HOME"] = tmp
                from fleet.collectors import dispatch as _d
                real = _d._opencode_config_home
                _d._opencode_config_home = lambda: oc
                try:
                    roots = peer_messages._runtime_ledger_roots()
                    all_roots = peer_messages._state_roots()
                finally:
                    _d._opencode_config_home = real
                self.assertEqual(roots, [os.path.join(codex, ".harness", "dispatch"),
                                         os.path.join(oc, ".harness", "dispatch")])
                self.assertTrue(set(roots) <= set(all_roots))
            finally:
                os.environ.clear(); os.environ.update(old)

    def test_markers_merge_across_roots_newest_wins(self):
        import importlib.util
        tool = os.path.join(os.path.dirname(_TOOLS_DIR), "utilities", "peer-message.py")
        spec = importlib.util.spec_from_file_location("_pm2", tool)
        pm = importlib.util.module_from_spec(spec); spec.loader.exec_module(pm)
        with tempfile.TemporaryDirectory() as tmp:
            a = os.path.join(tmp, "a"); b = os.path.join(tmp, "b")
            for root, upd in ((a, "2026-09-03T01:00:00Z"), (b, "2026-09-03T02:00:00Z")):
                d = os.path.join(root, "peer-steward", "codex"); os.makedirs(d)
                with open(os.path.join(d, "t1.json"), "w") as fh:
                    json.dump({"session_id": "t1", "updated": upd,
                               "targets": {"x": {"ts": upd, "kind": "watch", "source": "watch"}}}, fh)
            markers = pm.read_steward_markers([a, b])
            self.assertEqual(markers[("codex", "t1")]["updated"], "2026-09-03T02:00:00Z")
            self.assertEqual(pm.read_steward_markers([os.path.join(tmp, "missing")]), {})


class StewardReverseIndexTest(unittest.TestCase):
    """A steward marker records the relation on the STEWARD's side only, so before this
    the watched session had no reverse field at all (measured 2026-09-09) and the board
    could not answer "who is watching me". The reverse index is derived from the same
    evidence entries, never from a second marker."""

    def _enrich(self, sessions, marker_targets):
        markers = {("claude", "sidP"): {"session_id": "sidP", "targets": marker_targets}}
        with mock.patch.object(steward, "_peer_message_module") as module:
            module.return_value.steward_evidence_targets.side_effect = (
                lambda marker: [dict(entry, session_id=sid)
                                for sid, entry in marker["targets"].items()])
            steward.enrich(sessions, markers=markers)

    def test_target_gets_its_supervisor_and_steward_keeps_its_targets(self):
        parent = Session(harness="claude", pid=1, cwd="/x", slug="p", session_id="sidP")
        target = Session(harness="codex", pid=2, cwd="/x", slug="t", session_id="sidT")
        self._enrich([parent, target],
                     {"sidT": {"harness": "codex", "kind": "watch", "source": "watch"}})
        self.assertTrue(parent.steward)
        self.assertEqual([t["session_id"] for t in parent.steward_targets], ["sidT"])
        self.assertEqual(len(target.steward_parents), 1)
        self.assertEqual(target.steward_parents[0]["session_id"], "sidP")
        self.assertEqual(target.steward_parents[0]["harness"], "claude")
        self.assertIsNone(parent.steward_parents)

    def test_a_session_is_never_its_own_supervisor(self):
        solo = Session(harness="claude", pid=1, cwd="/x", slug="p", session_id="sidP")
        self._enrich([solo],
                     {"sidP": {"harness": "claude", "kind": "watch", "source": "watch"}})
        self.assertTrue(solo.steward)
        self.assertIsNone(solo.steward_parents)

    def test_a_resumed_target_still_joins_through_its_alias(self):
        """The marker names the id the target had BEFORE it resumed; an exact-only join
        silently drops the relation on both sides."""
        parent = Session(harness="claude", pid=1, cwd="/x", slug="p", session_id="sidP")
        target = Session(harness="codex", pid=2, cwd="/x", slug="t", session_id="sidNew",
                         session_aliases=["sidOld"])
        self._enrich([parent, target],
                     {"sidOld": {"harness": "codex", "kind": "watch", "source": "watch"}})
        self.assertEqual(target.steward_parents[0]["session_id"], "sidP")

    def test_no_markers_leaves_every_field_at_its_default(self):
        target = Session(harness="codex", pid=2, cwd="/x", slug="t", session_id="sidT")
        steward.enrich([target], markers={})
        self.assertFalse(target.steward)
        self.assertIsNone(target.steward_parents)


if __name__ == "__main__":
    unittest.main()
