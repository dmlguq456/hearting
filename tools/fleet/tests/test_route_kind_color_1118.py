"""11-18 (the person: "code, lab 같은 라우트 유형에 컬러를 넣어줘" / "강도도 컬러를 넣어주자"): Fleet's route rows take the kind word's hue.

  * The five field hues (code 69, lab 214, design 205, docs 180, research 38) stand with the zone hues, apart from every state,
    harness, zone and steward number; under 256 colors there is none.
  * A route's field is what it records; an older route that records none takes its kind's own name when that is a field's.
  * The kind word and its bracket (the whole bracket: `(dev·q)` too) wear the hue, the bracket by weight: quick dim, standard
    plain, strong, thorough and adversarial bold; a route with no hue keeps the bold body word and the dim bracket.
  * A finished route's keys recede to the hue's dim rung; curses gets a pair for each.
  * PEERS: a registered relation stands whether or not the other end drew a panel (its number from the tag registry).
Everything is synthetic.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fleet import palette, render  # noqa: E402
from fleet.explorer import live, tables  # noqa: E402
from fleet.explorer.grid import CellGrid  # noqa: E402
from fleet.explorer.style import recede_key  # noqa: E402

FIELDS = ("code", "lab", "design", "docs", "research")
HUES = {"code": 69, "lab": 214, "design": 205, "docs": 180, "research": 38}
NOW = time.mktime((2026, 11, 18, 10, 0, 0, 0, 0, -1))


def lab(number):
    """CIE Lab of a 256-color cube number (the 6x6x6 cube only)."""
    levels = (0, 95, 135, 175, 215, 255)
    n = number - 16
    rgb = [levels[n // 36], levels[(n // 6) % 6], levels[n % 6]]
    lin = [(c / 255 / 12.92) if c / 255 <= 0.04045 else ((c / 255 + 0.055) / 1.055) ** 2.4 for c in rgb]
    x = (0.4124 * lin[0] + 0.3576 * lin[1] + 0.1805 * lin[2]) / 0.95047
    y = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    z = (0.0193 * lin[0] + 0.1192 * lin[1] + 0.9505 * lin[2]) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116  # noqa: E731
    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


class KindHues(unittest.TestCase):
    def test_the_five_hues_are_the_given_numbers_and_apart_from_every_other_number_the_board_uses(self):
        self.assertEqual({field: palette._KIND_256["kind_" + field] for field in FIELDS}, HUES)
        used = set(render._MUTED_256.values()) | set(palette._ZONE_256.values()) | {253, 150, 186, 217, 230, 114, 116, 176, 147, 219}
        for field, number in HUES.items():
            self.assertNotIn(number, used, field)
        for number in HUES.values():                       # (the kind hues and the zone hues are all different from each other)
            for other in set(HUES.values()) - {number}:
                self.assertGreater(sum((a - b) ** 2 for a, b in zip(lab(number), lab(other))) ** 0.5, 30)
        # Each hue is as far from the board's other numbers as the zone hues were (Lab distance of 25 or more).
        for field, number in HUES.items():
            nearest = min(sum((a - b) ** 2 for a, b in zip(lab(number), lab(other))) ** 0.5 for other in (150, 186, 217, 116, 176, 147, 219, 114))
            self.assertGreater(nearest, 20, (field, nearest))

    def test_each_key_has_its_hue_and_weight_and_under_256_colors_none(self):
        for field, number in HUES.items():
            for key, bold, dim in (("kind_", True, False), ("kindq_", False, True), ("kinds_", False, False), ("kindb_", True, False)):
                got = palette.style_components(key + field, colors=256)
                self.assertEqual((got["foreground"], got["bold"], got["dim"]), (number, bold, dim), key + field)
                low = palette.style_components(key + field, colors=8)
                self.assertEqual((low["foreground"], low["bold"], low["dim"]), (-1, bold, dim), key + field)
                plain = palette.style_components(key + field, colors=256, no_color=True)
                self.assertEqual((plain["foreground"], plain["bold"], plain["dim"]), (None, False, False), key + field)

    def test_the_field_is_what_the_route_records_else_the_kinds_own_name(self):
        self.assertEqual(palette.route_field("docs", "draft"), "docs")
        self.assertEqual(palette.route_field("lab", "unit"), "lab")
        self.assertIsNone(palette.route_field("plan", "spec"))        # a field with no hue
        self.assertIsNone(palette.route_field("admin", "code"))       # a recorded field is not overruled by the name
        for kind in ("code", "lab", "design", "research"):
            self.assertEqual(palette.route_field(None, kind), kind)
            self.assertEqual(palette.route_field("", kind), kind)
        for kind in ("draft", "refine", "spec", "ship", "frame", "docs", "", None):
            self.assertIsNone(palette.route_field(None, kind), kind)

    def test_the_bracket_takes_the_weight_of_the_intensity_it_holds(self):
        for word, key in (("quick", "kindq_"), ("light", "kindq_"), ("q", "kindq_"), ("standard", "kinds_"), ("std", "kinds_"),
                          ("strong", "kindb_"), ("thorough", "kindb_"), ("thr", "kindb_"), ("adversarial", "kindb_"), ("adv", "kindb_"),
                          ("setup", "kinds_"), ("", "kinds_")):
            self.assertEqual(palette.kind_intensity_key("lab", word), key + "lab", word)
        # A bracket that holds a mode and the intensity (`dev·q`) takes the intensity's weight; a mode alone is plain.
        self.assertEqual(palette.kind_intensity_key("code", "dev·q"), "kindq_code")
        self.assertEqual(palette.kind_intensity_key("code", "(dev·std)"), "kinds_code")
        self.assertEqual(palette.kind_intensity_key("code", "dev"), "kinds_code")
        self.assertIsNone(palette.kind_intensity_key(None, "quick"))
        self.assertIsNone(palette.kind_word_key(None))


class KindCell(unittest.TestCase):
    def test_the_kind_word_and_its_bracket_wear_the_hue_and_a_route_with_none_reads_as_before(self):
        for field in FIELDS:
            for intensity, weight in (("quick", "kindq_"), ("standard", "kinds_"), ("strong", "kindb_"), ("thorough", "kindb_"), ("adversarial", "kindb_")):
                got = tables._kind_cell({"capability": "word", "field": field, "shape": "staged", "intensity": intensity})
                # 11-19: the bracket holds the form and the intensity (`staged·quick`); its color and weight follow the intensity
                self.assertEqual(got, [("word", "kind_" + field), ("(team·%s)" % intensity, weight + field)])
        self.assertEqual(tables._kind_cell({"capability": "spec", "field": "plan", "shape": "staged", "intensity": "strong"}),
                         [("spec", "grp"), ("(team·strong)", "dim")])
        # An older route (no field) with a field's own name as its kind.
        self.assertEqual(tables._kind_cell({"capability": "lab", "shape": "one-shot-owner", "intensity": "quick"}),
                         [("lab", "kind_lab"), ("(solo·quick)", "kindq_lab")])
        self.assertEqual(tables._kind_cell({"capability": "draft", "shape": "staged", "intensity": "quick"}),
                         [("draft", "grp"), ("(team·quick)", "dim")])
        # No intensity in the record: the form alone; a shape with no form keeps its intensity bracket alone; the form alone is plain.
        self.assertEqual(tables._kind_cell({"capability": "code", "field": "code", "shape": "staged"}), [("code", "kind_code"), ("(team)", "kinds_code")])
        self.assertEqual(tables._kind_cell({"capability": "code", "field": "code", "shape": "resource+verification", "intensity": "strong"}),
                         [("code", "kind_code"), ("(strong)", "kindb_code")])
        # `staged+resource` is staged; the tail folds first and the colour stays that of the intensity
        self.assertEqual(tables._kind_cell({"capability": "lab", "field": "lab", "shape": "staged+resource", "intensity": "thorough"}, tail=False),
                         [("lab", "kind_lab"), ("(team)", "kindb_lab")])
        self.assertEqual(tables._kind_cell({"capability": "lab", "field": "lab", "shape": "staged", "intensity": "strong"}, bracket=False),
                         [("lab", "kind_lab")])

    def test_the_whole_bracket_of_the_unfolded_kind_row_follows_the_same_rule(self):
        from fleet.explorer.tables import draw_routes  # noqa: F401  (the row is drawn there; its pieces are the helpers below)
        self.assertEqual(palette.kind_intensity_key("code", "dev·q"), "kindq_code")

    def test_a_finished_routes_keys_recede_to_the_hues_dim_rung(self):
        for field in FIELDS:
            for key in ("kind_", "kinds_", "kindb_", "kindq_"):
                self.assertEqual(recede_key(key + field), "kindq_" + field)
        self.assertEqual(recede_key("grp"), "grp")

    def test_a_route_row_paints_its_kind_word_in_the_hue(self):
        grid = CellGrid(60, 1)
        route = {"id": "r1", "capability": "lab", "field": "lab", "shape": "one-shot-owner", "intensity": "quick", "state": "working",
                 "stages": [{"name": "one-shot", "st": "active"}], "legs": (("one-shot", "active", None),), "tree_label": "lab",
                 "title": "t", "age": "2m", "repo": "hearting"}
        tables._draw_route_progress(grid, 0, 60, 0, route, card=False)
        keys = {grid.cells[0][x][0]: grid.cells[0][x][1] for x in reversed(range(60))}      # (first cell of each glyph)
        self.assertEqual(grid.cells[0][0][1], "kind_lab", keys)                       # `lab`
        self.assertEqual(keys["("], "kindq_lab", keys)                                # `(solo·quick)`: the whole bracket, the intensity's weight

    def test_curses_gets_a_pair_for_each_hue_above_the_tint_pairs_and_the_words_read_as_before_under_256_colors(self):
        self.assertGreaterEqual(render._KIND_PAIR_START, render._TINT_PAIR_START + len(render._TINT_LVL) * len(palette._HUE_PALETTE))
        previous, old_tint = dict(render._COLOR), render._TINT_OK
        self.addCleanup(lambda: (render._COLOR.clear(), render._COLOR.update(previous)))
        self.addCleanup(setattr, render, "_TINT_OK", old_tint)
        for colors in (256, 8):
            final = {}
            with mock.patch.multiple(render.curses, create=True, COLORS=colors, start_color=mock.Mock(), use_default_colors=mock.Mock(),
                                     can_change_color=mock.Mock(return_value=False), A_BOLD=1 << 20, A_DIM=1 << 21,
                                     init_pair=mock.Mock(side_effect=lambda n, fg, bg: final.__setitem__(n, (fg, bg))),
                                     color_pair=mock.Mock(side_effect=lambda n: n << 8)):
                render._init_colors()
            for number, field in enumerate(FIELDS):
                word, bracket = render._COLOR["kind_" + field], render._COLOR["kindq_" + field]
                if colors == 256:
                    pair = render._KIND_PAIR_START + number
                    self.assertEqual(final[pair], (HUES[field], -1), field)
                    self.assertEqual(word, (pair << 8) | (1 << 20))
                    self.assertEqual(bracket, (pair << 8) | (1 << 21))
                    self.assertEqual(render._COLOR["kinds_" + field], pair << 8)
                    self.assertEqual(render._COLOR["kindb_" + field], (pair << 8) | (1 << 20))
                else:                                                   # as before: the word as `grp`, every bracket dim
                    self.assertEqual(word, render._COLOR["grp"])
                    for weight in "qsb":
                        self.assertEqual(render._COLOR["kind%s_%s" % (weight, field)], 1 << 21)

    def test_zone_names_wear_their_hues_where_no_row_tint_stands_and_the_pair_ranges_never_meet(self):
        """Review of #1715, follow-ups 4 and 6: Fleet's zone names (the `─ direct ─` rules, the request line's `→`) are drawn without a
        tint, so they need a colour pair of their own; the tint pairs, the kind pairs and the zone pairs stand apart, and none of them
        is a colour number `_init_colors` changes with `init_color`."""
        tint = range(render._TINT_PAIR_START, render._TINT_PAIR_START + len(render._TINT_LVL) * len(palette._HUE_PALETTE))
        kind = range(render._KIND_PAIR_START, render._KIND_PAIR_START + len(FIELDS))
        zone = range(render._ZONE_PAIR_START, render._ZONE_PAIR_START + len(palette._ZONE_256))
        for one, other in ((tint, kind), (tint, zone), (kind, zone)):
            self.assertFalse(set(one) & set(other), (one, other))
        self.assertLess(max(tint), render._KIND_PAIR_START)
        self.assertLess(max(zone), 256)
        previous, old_tint = dict(render._COLOR), render._TINT_OK
        self.addCleanup(lambda: (render._COLOR.clear(), render._COLOR.update(previous)))
        self.addCleanup(setattr, render, "_TINT_OK", old_tint)
        # 11-27 (and review of #1898): the zone rule's line takes the zone's pair dimmed only where that reads as the name's hue one
        # step down: 256 colors, `NO_COLOR` unset, a terminal that draws a colored cell dimmed (terminfo `dim`, `ncv` without dim).
        # Elsewhere it is the dim line it was; the zone names' own pairs are the same in every case.
        dim = 1 << 21
        for label, colors, no_color, attrs, ncv, tinted in (
                ("xterm-256color", 256, None, dim | 1 << 20, -1, True),
                ("xterm-256color NO_COLOR=1", 256, "1", dim | 1 << 20, -1, False),
                ("rxvt-unicode-256color (no dim)", 256, None, 1 << 20, 0, False),
                ("256 colors, ncv forbids dim with color", 256, None, dim | 1 << 20, 16, False),
                ("8 colors", 8, None, dim | 1 << 20, -1, False)):
            final = {}
            env = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}
            if no_color is not None:
                env["NO_COLOR"] = no_color
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.multiple(render.curses, create=True, COLORS=colors, start_color=mock.Mock(), use_default_colors=mock.Mock(),
                                     can_change_color=mock.Mock(return_value=False), A_BOLD=1 << 20, A_DIM=dim,
                                     termattrs=mock.Mock(return_value=attrs), tigetnum=mock.Mock(return_value=ncv),
                                     init_pair=mock.Mock(side_effect=lambda n, fg, bg: final.__setitem__(n, (fg, bg))),
                                     color_pair=mock.Mock(side_effect=lambda n: n << 8)):
                render._init_colors()
            for number, (name, hue) in enumerate(palette._ZONE_256.items()):
                rule = "zoned_" + name[len("zone_"):]
                if colors == 256:
                    self.assertEqual(final[render._ZONE_PAIR_START + number], (hue, -1), (label, name))
                    self.assertEqual(render._key_attr(name), (render._ZONE_PAIR_START + number) << 8, (label, name))   # the key's own pair
                else:
                    self.assertEqual(render._COLOR.get(name, 0), 0, (label, name))                                       # as before: no hue
                self.assertEqual(render._key_attr(rule), ((render._ZONE_PAIR_START + number) << 8 | dim) if tinted else dim, (label, rule))


def session(tag, harness="claude", **values):
    row = {"harness": harness, "session_id": "sid-" + tag, "session_tag": tag, "cwd": "/synthetic/hearting",
           "liveness": "idle", "title": "title " + tag, "summary": "now " + tag, "context_pct": 42, "window": 1.0}
    row.update(values)
    return row


def other_end(tag, harness="claude"):
    return {"harness": harness, "session_id": "sid-" + tag, "name": tag, "kind": "watch", "ts": "2026-11-18T09:00:00", "source": "watch"}


class RegisteredRelationWithoutThePanel(unittest.TestCase):
    """The producer reads only the sessions that asked for a panel; a relation to one that did not still stands."""

    def peers(self, sessions, known):
        with mock.patch.object(live, "_registered_tag", side_effect=lambda harness, sid: known.get((harness, sid))):
            return live.session_peers({"sessions": sessions, "resources": [], "observations": {}}, NOW)

    def test_a_steward_names_a_target_that_drew_no_panel_by_the_number_the_registry_holds(self):
        got = self.peers([session("c7", steward=True, steward_targets=[other_end("d4"), other_end("3b", "codex")]), session("d4")],
                         {("codex", "sid-3b"): "3b"})
        self.assertEqual(got["claude\0sid-c7"], {"tag": "c7", "supervisors": [], "targets": [["d4", "claude"], ["3b", "codex"]]})

    def test_a_watched_window_names_a_supervisor_that_drew_no_panel(self):
        got = self.peers([session("d4", steward_parents=[{"harness": "claude", "session_id": "sid-c7"}])], {("claude", "sid-c7"): "c7"})
        self.assertEqual(got["claude\0sid-d4"], {"tag": "d4", "supervisors": [["c7", "claude"]], "targets": []})

    def test_an_end_the_registry_does_not_know_and_a_session_never_its_own_peer_add_nothing(self):
        got = self.peers([session("c7", steward=True, steward_targets=[other_end("ghost"), other_end("c7")])], {})
        self.assertEqual(got, {})
        got = self.peers([session("c7", steward=True, steward_targets=[other_end("c7")])], {("claude", "sid-c7"): "c7"})
        self.assertEqual(got, {})

    def test_the_relation_the_pass_already_read_is_not_doubled(self):
        got = self.peers([session("c7", steward=True, steward_targets=[other_end("d4"), other_end("d4")]), session("d4")],
                         {("claude", "sid-d4"): "d4"})
        self.assertEqual(got["claude\0sid-c7"]["targets"], [["d4", "claude"]])
        self.assertEqual(got["claude\0sid-d4"]["supervisors"], [["c7", "claude"]])


if __name__ == "__main__":
    unittest.main()
