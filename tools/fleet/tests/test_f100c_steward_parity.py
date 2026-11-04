#!/usr/bin/env python3
"""F-100c (user 2026-09-03) — steward flag on the board, three harnesses alike.

The ledger tool keeps a marker per session; since 2026-09-06 only role evidence
counts — `steward on` (source=explicit), a SENT `watch` (source=watch) or a `start`
that launched the target (source=start) — and a steer/handoff/gate-relay send is
just a message. The Fleet steward collector joins markers by exact
(harness, session_id) and asks the ledger tool which entries are evidence; the tag
badge then wears bold pink text followed by the flag (`[46] ⚑ claude`), an untagged
steward keeps the flag in the same place, with a legend entry.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import render                                          # noqa: E402
from fleet import herdr_projection                                # noqa: E402
from fleet.model import Session                                   # noqa: E402
from fleet.collectors import steward                              # noqa: E402
sys.path.append(str(Path(__file__).resolve().parents[3] / "utilities"))  # split_module_fixture
from split_module_fixture import patch_split  # noqa: E402


def _text(segs):
    return render._plain(segs)


class StewardChipTest(unittest.TestCase):
    def _s(self, **over):
        base = dict(harness="claude", pid=1, cwd="/x", slug="s", title="t", liveness="idle",
                    elapsed_min=1)
        base.update(over)
        return Session(**base)

    def _rows(self, session, width=168):
        return [row for row in render._build_lines([session], [], "fleet", False, 0,
                                   layout=render._layout_mode(width), term_width=width) if row]

    def test_steward_tag_is_bold_pink_text_followed_by_the_flag(self):
        segs = render._session_tag_chip(self._s(session_tag="46", steward=True))
        self.assertEqual(segs, [("[", "dim"), ("46", "tag_steward"), ("]", "dim"), (" ", None),
                                ("⚑", "tag_steward"), (" ", None)])
        self.assertEqual(sum(render._dw(t) for t, _k in segs), render._TAG_W + 2)
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
        self.assertEqual(render._snapshot_line(segs), "[46] ⚑ ")

    def test_untagged_steward_wears_the_role_mark_not_a_pretend_id(self):
        """The badge column holds session numbers, so `*` there read as an id the user
        could not find (2026-09-09: "그 id 가 안뜨는 경우도 있는것 같은데?"). The steward
        flag is the same glyph this session's relation line already uses."""
        for harness in ("codex", "opencode"):
            with self.subTest(harness=harness):
                segs = render._session_tag_chip(self._s(harness=harness, steward=True))
                self.assertEqual(segs, [(" " * render._TAG_W, None),
                                        ("⚑", "tag_steward"), (" ", None)])
                rows = self._rows(self._s(harness=harness, steward=True))
                self.assertEqual(sum(_text(row).count("⚑") for row in rows), 1)
                self.assertEqual(sum(render._dw(t) for t, _k in segs), render._TAG_W + 2)
                self.assertNotIn("*", "".join(t for t, _k in segs))

    def test_non_steward_rows_are_unchanged(self):
        self.assertEqual(render._session_tag_chip(self._s(session_tag="46"))[1], ("46", "tag"))
        self.assertEqual(render._session_tag_chip(self._s()), [(" " * render._TAG_W, None)])

    def test_dim_rows_stay_dim_even_as_steward(self):
        segs = render._session_tag_chip(self._s(session_tag="46", steward=True, liveness="stale"),
                                        dim=True)
        self.assertEqual(segs[1], ("46", "tag_dim"))

    def test_flag_sits_between_id_and_harness_for_all_harnesses_and_layouts(self):
        for harness, model in (("claude", "claude-opus-5.5"),
                               ("codex", "gpt-6.1-sol"),
                               ("opencode", "openai/gpt-6.1-sol")):
            s = self._s(harness=harness, model=model, effort="xhigh",
                        session_tag="46", steward=True)
            for width in (168, 100, 60):
                with self.subTest(harness=harness, width=width):
                    rows = self._rows(s, width)
                    identity = next(i for i, row in enumerate(rows) if "[46]" in _text(row))
                    self.assertIn("[46] ⚑ " + harness, _text(rows[identity]))
                    self.assertIn(("⚑", "tag_steward"), rows[identity])
                    self.assertNotIn("⚑", _text(rows[identity + 1]))
                    self.assertEqual(sum(_text(row).count("⚑") for row in rows), 1)
                    self.assertFalse(any("┆" in _text(row) for row in rows))  # off-screen target

    def test_role_keeps_model_and_following_columns_fixed(self):
        for harness, model in (("claude", "claude-opus-5.5"),
                               ("codex", "gpt-6.1-sol"),
                               ("opencode", "provider/a-very-long-model-name")):
            for width in (70, 100, 137, 168):
                with self.subTest(harness=harness, width=width):
                    s = self._s(harness=harness, model=model, effort="xhigh",
                                session_tag="46", runtime_name="column-marker")
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
                    self.assertNotIn("⚑", _text(marked_l2))

    def test_flag_uses_the_existing_dim_role_color(self):
        s = self._s(model="claude-opus-5.5", effort="xhigh", steward=True,
                    session_tag="46", liveness="stale")
        with mock.patch.object(render, "_SHOW_ALL", True):
            for width in (168, 100):
                rows = self._rows(s, width)
                self.assertTrue(any(("⚑", "tag_dim") in row for row in rows))

    def test_flag_width_preserves_following_anchors(self):
        original = [(" " * 8, None), ("anchor", "dim")]
        self.assertEqual(render._dw("⚑"), 1)
        measure = render._dw
        for width in (1, 2):
            with patch_split(render, "_dw", side_effect=lambda text: width if text == "⚑" else measure(text)):
                marked = render._under_id_connector(original, "⚑", "tag_steward")
                self.assertEqual(sum(render._dw(t) for t, _ in marked[:-1]), 8)
                self.assertEqual(sum(render._dw(t) for t, _ in marked), 14)

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
            return "\n".join(_text(ln) for ln in lines if ln and any("session: " in part for part, _key in ln))
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


class StewardConnectorRowsTest(unittest.TestCase):
    """The line runs under the ID through every watched session but the last one."""

    def _session(self, sid, tag, **over):
        base = dict(harness="codex", pid=1, cwd="/tmp/steward-short-project",
                    slug=sid, title=sid + " short", session_id=sid, session_tag=tag,
                    model="gpt-6.1-sol", liveness="idle", elapsed_min=5, ctx_pct=25,
                    summary=sid + " detail")
        base.update(over)
        return Session(**base)

    def _build(self, sessions, width):
        with mock.patch.object(render, "_PROCESS_VIEW", False):
            return render._build_lines(sessions, [], "both", False, 0,
                                       layout=render._layout_mode(width), term_width=width)

    def _row_index(self, lines, marker):
        return next(i for i, row in enumerate(lines) if row and marker in _text(row))

    def test_line_runs_through_middle_targets_and_stops_above_the_last(self):
        for width in (60, 100, 120, 150, 168):
            with self.subTest(width=width):
                parent = self._session("parent-id", "be", steward=True)
                first = self._session("first-id", "7a")
                last = self._session("last-id", "31")
                targets = [
                    {"harness": "codex", "session_id": "first-id"},
                    {"harness": "codex", "session_id": "last-id"},
                ]
                parent.steward_targets = None
                plain = self._build([parent, first, last], width)
                parent.steward_targets = targets
                marked = self._build([parent, first, last], width)

                self.assertEqual(len(marked), len(plain))  # no synthetic connector row
                parent_row = self._row_index(marked, "[be]")
                first_row = self._row_index(marked, "[7a]")
                last_row = self._row_index(marked, "[31]")
                self.assertNotIn("┆", _text(marked[parent_row]))
                self.assertNotIn("┆", _text(marked[first_row]))
                self.assertNotIn("┆", _text(marked[last_row]))
                self.assertIn(("⚑", "tag_steward"), marked[parent_row])
                self.assertEqual(_text(marked[parent_row + 1])[render._STEWARD_LINE_COL], "┆")
                before = _text(plain[self._row_index(plain, "[7a]") + 1])
                after = _text(marked[first_row + 1])
                self.assertEqual(after, before[:render._STEWARD_LINE_COL] + "┆"
                                 + before[render._STEWARD_LINE_COL + 1:])
                self.assertIn(("┆", "dim"), marked[first_row + 1])
                # User 2026-10-14: the last watched session carries no line at all.
                self.assertFalse(any("┆" in _text(row) for row in marked[last_row:] if row))
                self.assertEqual(sum(_text(row).count("⚑") for row in marked if row), 1)

    def test_hidden_cross_group_and_ambiguous_targets_do_not_draw_a_connector(self):
        cases = []

        hidden_parent = self._session("hidden-parent", "be", steward=True,
            steward_targets=[{"harness": "codex", "session_id": "hidden-target"}])
        hidden = self._session("hidden-target", "7a", liveness="stale")
        cases.append(("hidden", [hidden_parent, hidden]))

        other_group_parent = self._session("other-group-parent", "be", steward=True,
            steward_targets=[{"harness": "codex", "session_id": "other-group-target"}])
        other_group = self._session("other-group-target", "7a", cwd="/tmp/another-project")
        cases.append(("other-group", [other_group_parent, other_group]))

        ambiguous_parent = self._session("ambiguous-parent", "be", steward=True,
            steward_targets=[{"harness": "codex", "session_id": "old-target"}])
        alias_a = self._session("target-a", "7a")
        alias_b = self._session("target-b", "31")
        alias_a._gpu_session_aliases = ["old-target"]
        alias_b._gpu_session_aliases = ["old-target"]
        cases.append(("ambiguous-alias", [ambiguous_parent, alias_a, alias_b]))

        for label, sessions in cases:
            with self.subTest(case=label):
                lines = self._build(sessions, 150)
                self.assertFalse(any("┆" in _text(row) for row in lines if row))
                self.assertEqual(sum(_text(row).count("⚑") for row in lines if row), 1)


class StewardCollectorTest(unittest.TestCase):
    def setUp(self):
        repository = mock.patch.object(steward, "_repository_key", side_effect=lambda cwd: cwd or None)
        repository.start()
        self.addCleanup(repository.stop)
        registry = mock.patch.object(steward, "_registry_sessions", return_value=[])
        self.registry = registry.start()
        self.addCleanup(registry.stop)
        native = mock.patch.object(steward, "_native_role_sessions", return_value=[])
        native.start()
        self.addCleanup(native.stop)
        projection = mock.patch.object(steward, "_projection_sessions",
                                       side_effect=lambda: self.registry.return_value)
        projection.start()
        self.addCleanup(projection.stop)
        cwd = mock.patch.object(herdr_projection.os, "getcwd", return_value="/x")
        cwd.start()
        self.addCleanup(cwd.stop)

    def test_confirmed_clear_keeps_the_same_role_in_fleet_and_herdr(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                session = Session(harness=harness, pid=1, session_id="new", cwd="/x")
                session._gpu_session_aliases = ["old"]
                self.registry.return_value = [session, Session(harness="codex", pid=2,
                                               session_id="ended-target", cwd="/x")]
                markers = {(harness, "old"): {"session_id": "old", "targets": {
                    "ended-target": {"harness": "codex", "session_id": "ended-target",
                                     "kind": "watch", "source": "watch"}}}}
                with mock.patch.object(steward, "read_markers", return_value=markers), \
                     mock.patch("fleet.collectors.herdr._clear_gpu_session_aliases",
                                return_value=["old"]):
                    steward.enrich([session])
                    self.assertTrue(session.steward)
                    self.assertEqual(herdr_projection.is_steward(harness, "new"), session.steward)
                # Without proven history, an unrelated marker never grants the role.
                with mock.patch.object(steward, "read_markers", return_value=markers), \
                     mock.patch("fleet.collectors.herdr._clear_gpu_session_aliases", return_value=[]):
                    self.assertFalse(herdr_projection.is_steward(harness, "unrelated"))

    def test_explicit_self_flag_uses_the_existing_writer_on_all_harnesses(self):
        pm = steward._peer_message_module()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": tmp}), \
             mock.patch("fleet.collectors.herdr._clear_gpu_session_aliases", return_value=[]):
            for harness in ("claude", "codex", "opencode"):
                with self.subTest(harness=harness):
                    sid = "self-" + harness
                    self.assertTrue(pm.mark_steward(harness, sid,
                        {"harness": "unknown", "name": "-"}, "explicit",
                        "2026-10-13T04:00:00Z", source="explicit"))
                    markers = pm.read_steward_markers([tmp])
                    session = Session(harness=harness, pid=1, session_id=sid,
                                      cwd="/x", liveness="working", session_tag="46")
                    with mock.patch.object(steward, "read_markers", return_value=markers):
                        steward.enrich([session])
                        self.assertTrue(session.steward)
                        self.assertTrue(herdr_projection.is_steward(harness, sid))
                        self.assertEqual(steward.role_targets(harness, sid, cwd="/x"), [])
                    self.assertIsNone(session.steward_targets)
                    self.assertIsNone(session.steward_parents)
                    rows = render._build_lines([session], [], "fleet", False, 0,
                                              layout="wide", term_width=168)
                    self.assertEqual(sum(_text(row).count("⚑") for row in rows), 1)
                    self.assertFalse(any("┆" in _text(row) for row in rows))
                    self.assertEqual(render._session_tag_chip(session)[1], ("46", "tag_steward"))
                    # Release the fixture marker through the existing off writer.
                    self.assertEqual(pm.cmd_release(pm.argparse.Namespace(
                        harness=harness, session_id=sid)), 0)
                    steward.enrich([session], markers=pm.read_steward_markers([tmp]))
                    self.assertFalse(session.steward)

    def test_self_flag_accepts_only_its_exact_explicit_sentinel_and_actor(self):
        sentinel = {"harness": "unknown", "name": "-", "session_id": None,
                    "kind": "explicit", "source": "explicit"}
        cases = [({"source": "watch", "kind": "watch"}, "self", "codex"),
                 ({"source": None}, "self", "codex"),
                 ({"kind": "handoff"}, "self", "codex"),
                 ({"session_id": "unknown-child"}, "self", "codex"),
                 ({"harness": "opencode"}, "self", "codex"),
                 ({"name": "unknown-target"}, "self", "codex"),
                 ({"pane": "fixture-pane"}, "self", "codex"),
                 ({}, "old-dead-sid", "codex"),
                 ({}, "self", "claude")]
        for delta, marker_sid, marker_harness in cases:
            with self.subTest(delta=delta, marker_sid=marker_sid, marker_harness=marker_harness):
                marker = {"session_id": marker_sid, "harness": marker_harness,
                          "targets": {"-": dict(sentinel, **delta)}}
                session = Session(harness="codex", pid=1, session_id="self", cwd="/x")
                markers = {("codex", "self"): marker}
                with mock.patch.object(steward, "read_markers", return_value=markers), \
                     mock.patch("fleet.collectors.herdr._clear_gpu_session_aliases", return_value=[]):
                    steward.enrich([session])
                    self.assertFalse(session.steward)
                    self.assertFalse(herdr_projection.is_steward("codex", "self"))
                self.assertIsNone(session.steward_targets)
                self.assertIsNone(session.steward_parents)
        # A valid historical marker alone neither transfers its flag nor creates a row.
        old = {("codex", "old-dead-sid"): {"session_id": "old-dead-sid",
               "targets": {"-": sentinel}}}
        session = Session(harness="codex", pid=1, session_id="self", cwd="/x")
        rows = [session]
        steward.enrich(rows, markers=old)
        self.assertEqual(rows, [session])
        self.assertFalse(session.steward)

    def test_confirmed_clear_self_flag_matches_fleet_and_herdr(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                session = Session(harness=harness, pid=1, session_id="new", cwd="/x")
                session._gpu_session_aliases = ["old"]
                markers = {(harness, "old"): {"session_id": "old", "targets": {
                    "-": {"harness": "unknown", "name": "-", "session_id": None,
                          "kind": "explicit", "source": "explicit"}}}}
                with mock.patch.object(steward, "read_markers", return_value=markers), \
                     mock.patch("fleet.collectors.herdr._clear_gpu_session_aliases", return_value=["old"]):
                    steward.enrich([session])
                    self.assertTrue(session.steward)
                    self.assertTrue(herdr_projection.is_steward(harness, "new"))
                with mock.patch.object(steward, "read_markers", return_value=markers), \
                     mock.patch("fleet.collectors.herdr._clear_gpu_session_aliases", return_value=[]):
                    self.assertFalse(herdr_projection.is_steward(harness, "unrelated"))

    def test_self_flag_does_not_promote_foreign_target_relations(self):
        parent = Session(harness="codex", pid=1, session_id="self", cwd="/x")
        local = Session(harness="claude", pid=2, session_id="local", cwd="/x")
        foreign = Session(harness="opencode", pid=3, session_id="foreign", cwd="/foreign")
        sentinel = {"harness": "unknown", "name": "-", "session_id": None,
                    "kind": "explicit", "source": "explicit"}
        foreign_claim = {"harness": "opencode", "session_id": "foreign",
                         "kind": "explicit", "source": "explicit"}
        marker = {"session_id": "self", "targets": {"foreign": foreign_claim}}
        markers = {("codex", "self"): marker}
        steward.enrich([parent, foreign], markers=markers)
        self.assertFalse(parent.steward)  # Explicit target claim alone is no self flag.
        marker["targets"]["-"] = sentinel
        steward.enrich([parent, foreign], markers=markers)
        self.assertTrue(parent.steward)
        self.assertIsNone(parent.steward_targets)
        self.assertIsNone(foreign.steward_parents)
        marker["targets"]["local"] = {"harness": "claude", "session_id": "local",
                                      "kind": "watch", "source": "watch"}
        steward.enrich([parent, local, foreign], markers=markers)
        self.assertEqual([target["session_id"] for target in parent.steward_targets], ["local"])
        self.assertEqual(local.steward_parents[0]["session_id"], "self")
        self.assertIsNone(foreign.steward_parents)

    def test_foreign_only_holder_shows_no_flag_in_fleet_or_pane_title(self):
        # W7 [b6]: a marker plus an unreleased holder relation whose targets all live in
        # another repository is communication, not a role. Fleet and the pane title
        # (live and runner-published reads) agree on no flag; a same-repository target
        # in the same shape flags on both, so the agreement is not vacuous.
        for harness in ("claude", "codex", "opencode"):
            for target_cwd, expected in (("/foreign", False), ("/x", True)):
                with self.subTest(harness=harness, target_cwd=target_cwd):
                    holder = Session(harness=harness, pid=1, session_id="holder", cwd="/x")
                    targets = [Session(harness="claude", pid=2 + index, session_id=sid,
                                       cwd=target_cwd)
                               for index, sid in enumerate(("t1", "t2"))]
                    claims = {target.session_id: {
                        "harness": "claude", "session_id": target.session_id,
                        "name": target.session_id, "kind": "watch", "source": "watch"}
                        for target in targets}
                    markers = {(harness, "holder"): {"session_id": "holder",
                                                     "targets": claims}}
                    relation = {"generation": "g", "seat_key": "seat",
                                "origin": {"harness": harness, "sid": "holder"},
                                "holder": {"harness": harness, "sid": "holder"},
                                "targets": list(claims.values()), "lineage": [],
                                "released": False}
                    self.registry.return_value = [holder, *targets]
                    with mock.patch.object(steward, "read_markers", return_value=markers), \
                         mock.patch.object(steward, "read_relations", return_value=[relation]), \
                         mock.patch.object(herdr_projection, "_published_steward_relations",
                                           return_value=[relation]), \
                         mock.patch("fleet.collectors.herdr._clear_gpu_session_aliases",
                                    return_value=[]):
                        steward.enrich([holder, *targets])
                        self.assertIs(bool(holder.steward), expected)
                        self.assertIs(herdr_projection.is_steward(harness, "holder"), expected)
                        self.assertIs(herdr_projection.is_steward(
                            harness, "holder", published_only=True), expected)

    def test_many_session_refresh_reads_relation_ledger_once_and_keeps_exact_lineage(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "utilities"))
        import dispatch_seat_handover as handover
        import session_tidy

        with tempfile.TemporaryDirectory(prefix="fleet-steward-refresh-") as tmp:
            base = Path(tmp)
            peer_root = base / "peer"
            marker_path = peer_root / "peer-steward" / "codex" / "old-steward.json"
            marker_path.parent.mkdir(parents=True)
            marker = {
                "schema_version": 1, "harness": "codex", "session_id": "old-steward",
                "generation": "generation-current", "seat_key": "fixture-seat",
                "targets": {"target": {"harness": "claude", "session_id": "target",
                    "name": "target", "kind": "watch", "source": "watch", "ts": "1"}},
            }
            marker_path.write_text(json.dumps(marker), encoding="utf-8")
            target = marker["targets"]["target"]
            current_relation = {
                "generation": "generation-current",
                "origin": {"harness": "codex", "sid": "old-steward"},
                "holder": {"harness": "codex", "sid": "successor"},
                "targets": [target], "lineage": [{"from": "old-steward", "to": "successor"}],
                "seat_key": "fixture-seat", "released": False,
            }
            foreign_relation = {
                "generation": "generation-foreign",
                "origin": {"harness": "opencode", "sid": "foreign-steward"},
                "holder": {"harness": "opencode", "sid": "foreign-steward"},
                "targets": [{"harness": "claude", "session_id": "foreign-target",
                             "name": "foreign-target", "kind": "watch", "source": "watch"}],
                "lineage": [], "seat_key": "fixture-seat", "released": False,
            }
            ledger = base / "xdg-state" / "hearting" / "session-tidy" / "sessions" / "fixture-seat.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text(json.dumps({"event": "handover", "sid": "successor",
                "from": "old-steward", "seat_key": "fixture-seat",
                "steward": {"schema": 1,
                            "roles": [current_relation, foreign_relation], "watches": []}}) + "\n",
                encoding="utf-8")

            successor = Session(harness="codex", pid=1, session_id="successor", cwd="/x")
            target_session = Session(harness="claude", pid=2, session_id="target", cwd="/x")
            foreign_parent = Session(harness="opencode", pid=3,
                                     session_id="foreign-steward", cwd="/foreign")
            foreign_target = Session(harness="claude", pid=4,
                                     session_id="foreign-target", cwd="/x")
            fillers = [Session(harness="codex", pid=10 + index,
                               session_id=f"unrelated-{index}", cwd="/x")
                       for index in range(48)]
            sessions = [successor, target_session, foreign_parent, foreign_target, *fillers]

            pm = handover._peer_message()
            environ = {"AGENT_PEER_LEDGER_ROOT": str(peer_root),
                       "XDG_STATE_HOME": str(base / "xdg-state"),
                       "AGENT_DISPATCH_JOBS": str(base / "dispatch" / "jobs.log"),
                       "HOME": str(base / "home"), "PYTHONDONTWRITEBYTECODE": "1"}
            with mock.patch.dict(os.environ, environ), \
                 mock.patch.object(handover, "_peer_message", return_value=pm), \
                 mock.patch.object(pm, "steward_marker_roots",
                                   return_value=([peer_root], "test-fixture")), \
                 mock.patch.object(handover, "resolved_steward_relations",
                                   wraps=handover.resolved_steward_relations) as resolver, \
                 mock.patch.object(session_tidy, "_read_ledger_lines",
                                   wraps=session_tidy._read_ledger_lines) as ledger_reads:
                markers = pm.read_steward_markers([peer_root])
                steward.enrich(sessions, markers=markers)
                self.assertEqual(resolver.call_count, 1)
                self.assertEqual(ledger_reads.call_count, 1)
                self.assertTrue(successor.steward)
                self.assertEqual([entry["session_id"] for entry in successor.steward_targets],
                                 ["target"])
                self.assertEqual(target_session.steward_parents[0]["session_id"], "successor")
                self.assertFalse(foreign_parent.steward)
                self.assertIsNone(foreign_target.steward_parents)
                self.assertTrue(all(not row.steward for row in fillers))

                # Standalone helpers keep their documented fresh-read behavior.
                self.assertEqual([entry["session_id"] for entry in steward.role_targets(
                    "codex", "successor", markers=markers, cwd="/x", sessions=sessions)],
                    ["target"])
                self.assertEqual((resolver.call_count, ledger_reads.call_count), (2, 2))
                self.assertTrue(steward.role_flag("codex", "successor", markers=markers,
                                                  _targets=[target]))
                self.assertEqual((resolver.call_count, ledger_reads.call_count), (3, 3))

    def test_join_is_exact_on_harness_and_session_id(self):
        sessions = [Session(harness="claude", pid=1, session_id="sid-a", cwd="/x"),
                    Session(harness="codex", pid=2, session_id="sid-a", cwd="/x"),
                    Session(harness="claude", pid=3, session_id="sid-b", cwd="/x"),
                    Session(harness="claude", pid=4, cwd="/x")]
        self.registry.return_value = [Session(harness="codex", pid=5, session_id="x", cwd="/x"),
                                      Session(harness="claude", pid=6, session_id="y", cwd="/x")]
        markers = {("claude", "sid-a"): {"session_id": "sid-a", "targets": {
            "x": {"harness": "codex", "session_id": "x", "name": "w", "kind": "start", "ts": "2",
                  "source": "start"},
            "y": {"harness": "claude", "session_id": "y", "name": "v", "kind": "watch", "ts": "1",
                  "source": "watch"}}}}
        steward.enrich(sessions, markers=markers)
        self.assertEqual([s.steward for s in sessions], [True, False, False, False])
        self.assertEqual([t["session_id"] for t in sessions[0].steward_targets], ["y", "x"])
        self.assertIsNone(sessions[1].steward_targets)

    def test_opencode_watch_target_flags_once_its_native_session_is_confirmed(self):
        # Measured 2026-10-27: supervisor 1b watched Herdr OpenCode panes, but Fleet read those
        # processes without a native session id, so the steward rule found no target and Herdr's
        # flag and Fleet disagreed. The rule itself is unchanged: an exact sid and cwd are enough.
        sid = "ses_fixture00000000000000000001"
        markers = {("claude", "steward"): {"session_id": "steward", "targets": {
            sid: {"harness": "opencode", "session_id": sid, "name": "w", "kind": "watch",
                  "ts": "1", "source": "watch"}}}}
        for target_sid, expected in ((None, False), (sid, True)):
            sessions = [Session(harness="claude", pid=1, session_id="steward", cwd="/x"),
                        Session(harness="opencode", pid=2, session_id=target_sid, cwd="/x")]
            steward.enrich(sessions, markers=markers)
            self.assertEqual(sessions[0].steward, expected)

    def test_handoff_only_and_legacy_markers_do_not_make_a_steward(self):
        """Role ≠ communication (user 2026-09-06: "통신만 하면 죄다 d=-1"). A marker whose
        entries are steer/handoff/gate-relay sends — new (source absent, kind=handoff) or
        old (no source at all) — leaves steward=False; a mixed marker keeps only the
        evidence entries in `steward_targets`."""
        sessions = [Session(harness="claude", pid=1, session_id="worker", cwd="/x"),
                    Session(harness="claude", pid=2, session_id="legacy-steward", cwd="/x"),
                    Session(harness="claude", pid=3, session_id="mixed", cwd="/x"),
                    Session(harness="opencode", pid=4, session_id="empty", cwd="/x")]
        self.registry.return_value = [Session(harness="codex", pid=5, session_id="c", cwd="/x"),
                                      Session(harness="claude", pid=6, session_id="s", cwd="/x")]
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
        self.assertEqual([t["session_id"] for t in sessions[2].steward_targets], ["s"])
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
                sessions = [Session(harness="claude", pid=1, session_id="sid-a", cwd="/x")]
                self.registry.return_value = [Session(harness="codex", pid=2,
                                                       session_id="sid-c", cwd="/x")]
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

    def setUp(self):
        repository = mock.patch.object(steward, "_repository_key", side_effect=lambda cwd: cwd or None)
        repository.start()
        self.addCleanup(repository.stop)
        registry = mock.patch.object(steward, "_registry_sessions", return_value=[])
        registry.start()
        self.addCleanup(registry.stop)
        native = mock.patch.object(steward, "_native_role_sessions", return_value=[])
        native.start()
        self.addCleanup(native.stop)

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
        self.assertFalse(solo.steward)
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

    def test_a_cleared_claude_target_keeps_its_supervisor(self):
        """The start marker names the id a Claude target had before /clear. Claude ids
        are resolved after the herdr pane pass, so the pane's seat-history join waits
        for the id; joined in the pane pass, it saw no id and the relation was lost."""
        from fleet.collectors import herdr, procscan
        parent = Session(harness="claude", pid=1, cwd="/x", slug="p", session_id="sidP")
        target = Session(harness="claude", pid=2, proc_start="22", cwd="", slug="t")

        def lineage(harness, sid, pane, cwd=None):
            return ["sidOld"] if (harness, sid, pane, cwd) == ("claude", "sidNew", "w:p2", "/x") else []

        with mock.patch.object(herdr, "pane_session_aliases", side_effect=lineage), \
             mock.patch.object(herdr, "_clear_gpu_session_aliases", side_effect=lineage), \
             mock.patch.object(procscan, "read_proc_start", return_value="22"), \
             mock.patch.object(herdr, "pid_in_panes", return_value=False):
            herdr.enrich([parent, target], agents=[], pids=(set(), {2}),
                         pane_bindings={2: {"w:p2"}})
            self.assertIsNone(target.session_aliases)
            target.session_id, target.cwd = "sidNew", "/x"   # the Claude enricher, then its directory
            herdr.join_deferred_pane_aliases([parent, target])
        self.assertEqual(target.session_aliases, ["sidOld"])
        self.assertEqual(target._gpu_session_aliases, ["sidOld"])
        self._enrich([parent, target],
                     {"sidOld": {"harness": "claude", "kind": "start", "source": "start"}})
        self.assertTrue(parent.steward)
        self.assertEqual(target.steward_parents[0]["session_id"], "sidP")

    def test_collection_joins_claude_aliases_after_its_id_and_directory(self):
        import importlib
        from types import SimpleNamespace
        from fleet import session_cwd
        from fleet.collectors import herdr
        collectors = importlib.import_module("fleet.collectors")
        session = SimpleNamespace(harness="claude", pid=123, proc_start="1", cwd="",
                                  session_id=None, is_child=False)
        order = []

        class Joined(BaseException):
            pass

        def claude_enrich(row, **_kwargs):
            order.append("claude-id")
            row.session_id = "sid-new"

        def project(rows):
            order.append("directory")
            for row in rows:
                row.cwd = "/work"

        def join(rows):
            order.append("join")
            raise Joined([(row.session_id, row.cwd) for row in rows])

        modules = {".claude": SimpleNamespace(enrich=claude_enrich),
                   ".codex": SimpleNamespace(enrich=lambda *_a, **_k: None),
                   ".opencode": SimpleNamespace(enrich=lambda *_a, **_k: None)}
        with mock.patch.object(collectors.procscan, "scan", return_value=[session]), \
             mock.patch.object(collectors.importlib, "import_module",
                               side_effect=lambda name, _package: modules[name]), \
             mock.patch.object(herdr, "enrich", side_effect=lambda *_a, **_k: order.append("pane")), \
             mock.patch.object(session_cwd, "project", side_effect=project), \
             mock.patch.object(herdr, "join_deferred_pane_aliases", side_effect=join):
            with self.assertRaises(Joined) as joined:
                collectors.collect_all(harness_filter=("claude",))
        self.assertEqual(order, ["pane", "claude-id", "directory", "join"])
        self.assertEqual(joined.exception.args[0], [("sid-new", "/work")])

    def test_no_markers_leaves_every_field_at_its_default(self):
        target = Session(harness="codex", pid=2, cwd="/x", slug="t", session_id="sidT")
        steward.enrich([target], markers={})
        self.assertFalse(target.steward)
        self.assertIsNone(target.steward_parents)


class LineageConsumerWiringTest(unittest.TestCase):
    def test_steward_projection_passes_its_known_repository_to_lineage_reader(self):
        from fleet.collectors import herdr
        cwd = "/fixture/repository"
        agent = {"agent": "codex", "pane_id": "w:p", "cwd": cwd,
                 "agent_session": {"value": "current"}}
        with mock.patch.object(herdr, "list_agents", return_value=[agent]), \
             mock.patch.object(herdr, "pane_session_aliases", return_value=[]), \
             mock.patch.object(herdr, "_clear_gpu_session_aliases", return_value=["older"]) as aliases, \
             mock.patch.object(steward, "_registry_sessions", return_value=[]):
            rows = steward._projection_sessions()
        aliases.assert_called_once_with("codex", "current", "w:p", cwd)
        self.assertEqual(rows[0]._gpu_session_aliases, ["older"])

    def test_role_snapshot_does_not_fold_a_still_live_sid_into_its_parent(self):
        from fleet.collectors import herdr
        cwd = "/fixture/repository"
        agent = {"agent": "codex", "pane_id": "w:p", "cwd": cwd,
                 "agent_session": {"value": "current"}}
        old_live = Session(harness="codex", pid=2, session_id="older", cwd=cwd)
        with mock.patch.object(herdr, "list_agents", return_value=[agent]), \
             mock.patch.object(herdr, "pane_session_aliases", return_value=["older", "ended"]), \
             mock.patch.object(herdr, "_clear_gpu_session_aliases", return_value=["older", "ended"]), \
             mock.patch.object(steward, "_registry_sessions", return_value=[old_live]):
            rows = steward._projection_sessions()
        self.assertEqual([row.session_id for row in rows], ["current", "older"])
        self.assertEqual(rows[0].session_aliases, ["ended"])
        self.assertEqual(rows[0]._gpu_session_aliases, ["ended"])

    def test_herdr_role_lookup_drops_predecessors_that_are_still_live(self):
        from fleet.collectors import herdr
        cwd = "/fixture/repository"
        sessions = [
            Session(harness="codex", pid=1, cwd=cwd, session_id="live-old"),
            Session(harness="codex", pid=2, cwd=cwd, session_id="current"),
        ]
        markers = {("codex", "current"): {"harness": "codex", "session_id": "current",
                                            "targets": {}}}
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w:p"}), \
             mock.patch.object(steward, "read_markers", return_value=markers), \
             mock.patch.object(herdr, "_clear_gpu_session_aliases",
                               return_value=["live-old", "ended-old"]) as lineage, \
             mock.patch.object(steward, "_projection_sessions", return_value=sessions), \
             mock.patch.object(steward, "role_flag", return_value=True) as role_flag, \
             mock.patch.object(herdr_projection.os, "getcwd", return_value=cwd):
            self.assertTrue(herdr_projection.is_steward("codex", "current"))
        lineage.assert_called_once_with("codex", "current", "w:p", cwd)
        self.assertEqual(role_flag.call_args.kwargs["aliases"], ["ended-old"])
        self.assertIs(role_flag.call_args.kwargs["sessions"], sessions)


if __name__ == "__main__":
    unittest.main()
