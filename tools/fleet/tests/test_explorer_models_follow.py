"""MODELS shows what following did and will do: marks beside the cells, lines under the table, the chosen cell's box.

The screen reads only the answer of ``model_follow.fleet_view()``. Every picture here is the explorer's own
drawing of a made-up table and a made-up answer (``models_follow_picture`` / ``models_follow_samples``).
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import copy
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tools.fleet.explorer import focus, follow
from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.grid import cell_width
from tools.fleet.explorer.state import NavState, finish_follow_draft, reduce
from tools.fleet.explorer.style import normalize_style
from tools.fleet.explorer.view_model import ExplorerViewModel
from tools.fleet.tests import models_follow_picture as picture
from tools.fleet.tests import models_follow_samples as samples

GOLDEN = Path(__file__).parent / "fixtures" / "explorer" / "models_follow"
TODAY = samples.TODAY
ALL = samples.samples()


def _models(name=None, answer=None, targets=None):
    answer = ALL[name] if answer is None and name else answer
    models = picture.base_models()
    if targets:
        models["profile_target"] = targets
    if answer is not None:
        focus.follow_into(picture.fit_table(models, answer), answer)
    return models


def _pic(name=None, width=140, select="deep/codex", today=TODAY, folds=None, answer=None, **kw):
    return picture.section_text(_models(name, answer, **kw), width, select, today, folds)


def _golden(width, select):
    return (GOLDEN / f"quiet-{width}-{select.replace('/', '-') or 'none'}.txt").read_text(encoding="utf-8").splitlines()


def _box(lines):
    top = next(i for i, row in enumerate(lines) if "┌─" in row)
    return lines[top:]


def _frame(models, width=140, select="deep/codex", today=TODAY, folds=None):
    chosen = {"ui:sel": select} if select else {}
    chosen.update(folds or {})
    state = NavState(view="MODELS", title="MODELS", folds=tuple(sorted(chosen.items())))
    vm = ExplorerViewModel.from_mapping({"models": models, "clock": picture.epoch_of(today) if today else None})
    return vm, state, draw(vm, state, width, 70, beat=1)


def _click(vm, state, frame, width, predicate):
    hit = next(item for item in frame.hits if predicate(item.action))
    state, effects = reduce(state, {"type": "click", "action": hit.action}, frame)
    return state, effects, draw(vm, state, width, 70, beat=1)


class QuietDay(unittest.TestCase):
    """No news is the screen of before this change, byte for byte."""

    def test_without_an_answer_every_picture_is_the_one_from_before(self):
        for width in (100, 140):
            for select in ("deep/codex", "balanced/claude", ""):
                with self.subTest(width=width, select=select):
                    self.assertEqual(picture.section_text(picture.base_models(), width, select, TODAY),
                                     _golden(width, select))

    def test_a_quiet_answer_changes_nothing_but_the_chosen_cells_follow_row(self):
        for width in (100, 140):
            for select in ("deep/codex", "balanced/claude", ""):
                with self.subTest(width=width, select=select):
                    shown = _pic("quiet", width, select)
                    if select == "deep/codex":      # a plain cell of the table: one row, right under `now`
                        rows = [i for i, row in enumerate(shown) if "follow   [ Follow ]   Keep" in row]
                        self.assertEqual(len(rows), 1)
                        self.assertIn("now      gpt-6-astra · high", shown[rows[0] - 1])
                        shown = shown[:rows[0]] + shown[rows[0] + 1:]
                    self.assertEqual(shown, _golden(width, select))

    def test_a_cell_with_nothing_to_follow_has_the_box_it_had(self):
        # a Claude alias the model list does not hold: the answer says no update can follow it
        answer = copy.deepcopy(ALL["quiet"])
        answer[0]["cells"] = [samples.unfollowable("tier/balanced", "sonnet", "high")]
        models = _models(answer=answer)
        self.assertEqual(models["follow"]["cells"]["claude"], [])
        self.assertEqual(picture.section_text(models, 140, "balanced/claude", TODAY), _golden(140, "balanced/claude"))

    def test_nothing_is_drawn_for_an_answer_the_screen_does_not_know(self):
        for odd in ("oops", {"harness": "codex"}, [{"harness": "codex", "cells": "x"}],
                    [{"harness": "nobody", "cells": []}], [7], None):
            with self.subTest(odd=odd):
                models = picture.base_models()
                focus.follow_into(models, odd)
                self.assertNotIn("follow", models)
                self.assertEqual(picture.section_text(models, 140, "deep/codex", TODAY), _golden(140, "deep/codex"))
        for garbage in ("x", 5, ["a"], {"cells": 3}, {"cells": {"codex": "x"}}):
            models = dict(picture.base_models(), follow=garbage)
            self.assertEqual(picture.section_text(models, 140, "deep/codex", TODAY), _golden(140, "deep/codex"))

    def test_a_failing_function_leaves_the_view_without_follow(self):
        class Utility:
            @staticmethod
            def fleet_view(**_kw):
                raise RuntimeError("no table")

        with mock.patch.object(focus, "utility", return_value=Utility):
            self.assertIsNone(focus._read_follow({}))
        models = picture.base_models()
        focus.follow_into(models, None)
        self.assertNotIn("follow", models)


class Marks(unittest.TestCase):
    def test_the_day_after_a_follow_marks_the_cells_and_says_so_under_the_table(self):
        shown = _pic("followed", 140)
        table = "\n".join(shown[:8])
        self.assertIn("^ gpt-6.2-astra · xhigh", shown[2])
        self.assertIn("▶ gpt-6.2-astra · high", shown[3])                  # the chosen cell keeps ▶
        self.assertIn("^ gpt-6.2-sol · xhigh", shown[4])
        self.assertIn("= gpt-6-luna · high", shown[7])
        self.assertNotIn("^ gpt-6.1-terra", table)
        self.assertEqual(shown[9], "│ ^ followed 11-22 · 5 cells ▸")        # three table cells and two tier lines
        self.assertEqual(shown[10], "│ ^ Followed   = Keep")
        self.assertEqual(shown[11], "│")

    def test_a_mark_never_takes_a_place_from_a_name(self):
        # the made-up keep answer leaves every model as it was: only the mark's column differs
        for width in (100, 140):
            with self.subTest(width=width):
                before, after = _golden(width, "balanced/claude"), _pic("keep", width, "balanced/claude")
                marked = [row for row in after[:8] if "=" in row]
                self.assertEqual(len(marked), 2)
                for row in marked:
                    self.assertEqual(row.replace("= ", "  ", 1), before[after.index(row)])
        self.assertIn("= gpt-6-… · high", _pic("keep", 100, "balanced/claude")[7])     # the cut is where it was before

    def test_the_mark_stands_in_the_selection_column_before_the_blank(self):
        marked = _pic("followed", 140, "balanced/claude")[2]
        plain = _golden(140, "balanced/claude")[2]
        self.assertEqual(marked.index("^"), plain.index("gpt-6-astra") - 2)

    def test_three_days_show_and_the_fourth_day_does_not(self):
        shown = _pic("followed-3d", 140, "balanced/claude", today="2026-11-22")
        self.assertIn("^ followed 11-19 · 5 cells ▸", "\n".join(shown))
        self.assertIn("^ gpt-6.2-astra", shown[2])
        gone = _pic("followed-3d", 140, "balanced/claude", today="2026-11-23")
        self.assertFalse(any("^" in row for row in gone), "\n".join(gone))
        self.assertNotIn("followed", "\n".join(gone))
        self.assertIn("= gpt-6-luna · high", gone[7])                      # a keep is shown for as long as it is kept
        self.assertEqual(gone[8:10], ["│    main session only, never dispatched:  claude Fable", "│ = Keep"])
        # the history stays in the cell's own box
        box = "\n".join(_pic("followed-4d", 140, "deep/codex", today="2026-11-23"))
        self.assertIn("followed 2026-11-18 · from gpt-6-astra · high", box)
        self.assertNotIn("^ followed", box)

    def test_a_date_that_cannot_be_read_is_not_recent(self):
        for date in ("11-22", "2026-13-40", "soon", ""):
            answer = copy.deepcopy(ALL["followed"])
            for entry in answer:
                for found in entry["cells"]:
                    if found["why"] == "followed":
                        found["since"]["date"] = date or "x"
            shown = "\n".join(_pic(answer=answer, select="balanced/claude"))
            self.assertNotIn("^", shown, date)
        for clock_less in (None, "synthetic"):
            vm = ExplorerViewModel.from_mapping({"models": _models("followed"), "clock": clock_less})
            state = NavState(view="MODELS", title="MODELS", folds=(("ui:sel", "balanced/claude"),))
            self.assertNotIn("^ followed", "\n".join(draw(vm, state, 140, 70).text_rows()))

    def test_keep_and_held_show_for_as_long_as_they_hold(self):
        keep = _pic("keep", 140, "balanced/claude", today="2027-02-01")
        self.assertIn("= gpt-6-luna · xhigh", keep[6])
        self.assertIn("= gpt-6-luna · high", keep[7])
        self.assertEqual(keep[9], "│ = Keep")                              # no summary line for a keep; the legend only
        held = _pic("held", 140, "balanced/claude", today="2027-02-01")
        self.assertIn("! gpt-6-astra · high", held[3])
        self.assertEqual(held[9], "│ ! held codex profile/deep · back on gpt-6-astra ▸")
        self.assertEqual(held[10], "│ ! Held")

    def test_a_kept_cell_makes_no_summary_line_even_when_a_newer_model_exists(self):
        answer = [samples.item("codex", [samples.cell(
            "profile/mini", "gpt-6-luna", "high", state="keep", why="kept",
            since={"date": "2026-11-20", "from": None, "to": None},
            following=samples.upcoming("gpt-7-luna", "high", "proposal", "other family")),
            samples.cell("profile/light", "gpt-6-luna", "xhigh", state="keep", why="kept",
                         since={"date": "2026-11-20", "from": None, "to": None},
                         following=samples.upcoming("gpt-6.1-luna", "xhigh", "kept"))])]
        shown = _pic(answer=answer, select="balanced/claude")
        self.assertEqual(shown[8:11], ["│    main session only, never dispatched:  claude Fable", "│ = Keep", "│"])
        box = "\n".join(_pic(answer=answer, select="mini/codex"))
        self.assertNotIn("proposal", box)

    def test_a_cell_that_gets_its_value_through_a_tier_shows_the_tiers_state(self):
        answer = [samples.item("codex", [samples.followed("tier/deep", "gpt-6.1-sol", "gpt-6.2-sol", "xhigh"),
                                         samples.held("tier/balanced", "gpt-6.1-terra", "xhigh", "gpt-6.2-terra")])]
        targets = {"codex": {"deep": "tier/deep", "balanced-deep": "tier/deep", "balanced": "tier/balanced"}}
        shown = _pic(answer=answer, targets=targets, select="top/codex")
        self.assertIn("^ gpt-6-astra · high", shown[3])                    # deep
        self.assertIn("^ gpt-6.1-sol · xhigh", shown[4])                   # balanced-deep: the same tier
        self.assertIn("! gpt-6.1-terra · xhigh", shown[5])
        self.assertEqual(shown[9], "│ ^ followed 11-22 · codex tier/deep ▸")

    def test_when_two_states_meet_one_cell_shows_held_then_keep_then_followed(self):
        view = follow.Follow({"cells": {"codex": [samples.held("tier/x", "a", "high", "b"),
                                                  samples.kept("tier/y", "a", "high"),
                                                  samples.followed("tier/z", "a", "b", "high")]}, "cli": []},
                             {"codex": {"top": "tier/x", "deep": "tier/y", "light": "tier/z"}},
                             follow.today_of(picture.epoch_of(TODAY)))
        self.assertEqual([view.mark("codex", p) for p in ("top", "deep", "light", "mini")], ["!", "=", "^", ""])

    def test_the_marks_use_the_existing_palette_and_say_the_same_without_it(self):
        vm, state, frame = _frame(_models("mixed"), 140, "light/codex")
        keys = {}
        for row, text in zip(frame.cells, frame.text_rows()):
            for glyph, key, _tint in row[:]:
                if glyph in "^=!" and key:
                    keys[glyph] = normalize_style(key)[0]
        self.assertEqual(set(keys), {"^", "=", "!"})
        for key in keys.values():
            self.assertIn(key, {"grp_live", "g_idle", "g_blocked"})
        # mono: the glyphs alone give every state (texts above), and the legend names each
        shown = "\n".join(_pic("mixed", 140, "light/codex"))
        self.assertIn("^ Followed   = Keep   ! Held", shown)


class Summary(unittest.TestCase):
    def test_lines_come_in_the_order_followed_next_held_proposal_cli(self):
        shown = _pic("mixed", 140, "light/codex")
        rows = shown[9:15]
        self.assertTrue(rows[0].startswith("│ ^ followed 11-22 · 2 cells"))
        self.assertTrue(rows[1].startswith("│ > next 2 cells + 1 linked line · on the next update or refresh"))
        self.assertTrue(rows[2].startswith("│ ! held codex profile/deep · back on gpt-6-astra"))
        self.assertTrue(rows[3].startswith("│ ? proposal codex profile/balanced · gpt-7-terra (other family)"))
        self.assertTrue(rows[4].startswith("│ codex CLI 0.163.0 · update it yourself · lists gpt-6.1-sol, gpt-6-astra as previous"))
        self.assertEqual(rows[5], "│ ^ Followed   = Keep   ! Held")

    def test_one_target_is_named_and_several_are_counted(self):
        self.assertIn("│ > next opencode tier/deep · on the next update or refresh ▸", _pic("next", 140))
        self.assertIn("│ ? proposal codex profile/balanced · gpt-7-terra (other family) ▸", _pic("proposal", 140))

    def test_a_group_opens_in_place_one_target_a_row_and_closes_again(self):
        models = _models("mixed")
        vm, state, frame = _frame(models, 140, "light/codex")
        state, _effects, frame = _click(vm, state, frame, 140,
                                        lambda a: a == ("ui", "fopen:followed", "open"))
        shown = picture.section_text(models, 140, "light/codex", TODAY, state.fold_map())
        self.assertEqual(shown[9], "│ ^ followed 11-22 · 2 cells ▾")
        self.assertEqual(shown[10], "│   codex profile/top: gpt-6-astra@xhigh -> gpt-6.2-astra@xhigh")
        self.assertTrue(shown[11].startswith("│   opencode tier/deep: opencode-go/glm-5.2-flash@max -> opencode-go/glm-5.3-flash@max"))
        self.assertTrue(shown[12].startswith("│ > next"))
        nxt = picture.section_text(models, 140, "light/codex", TODAY, {"ui:fopen:next": "open"})
        listed = [row for row in nxt if row.startswith("│   ")]
        self.assertIn("│   codex profile/light: gpt-6-luna@xhigh -> gpt-6.1-luna@xhigh", listed)
        self.assertIn("│   linked cascade/deep: gpt-6.1-sol:xhigh -> gpt-6.2-sol:xhigh", listed)
        state, _e, frame = _click(vm, state, frame, 140, lambda a: a == ("ui", "fopen:followed", "closed"))
        self.assertEqual(picture.section_text(models, 140, "light/codex", TODAY, state.fold_map()),
                         picture.section_text(models, 140, "light/codex", TODAY))

    def test_a_listed_target_selects_its_cell_only_when_the_table_has_one(self):
        vm, state, frame = _frame(_models("mixed"), 140, "light/codex",
                                  folds={"ui:fopen:followed": "open", "ui:fopen:next": "open"})
        picks = [hit.action for hit in frame.hits if hit.action[:2] == ("ui", "sel")]
        self.assertIn(("ui", "sel", "top/codex"), picks)
        self.assertIn(("ui", "sel", "balanced-deep/codex"), picks)
        text = frame.text_rows()
        for needle in ("opencode tier/deep:", "linked cascade/deep:"):
            row = next(i for i, line in enumerate(text) if needle in line)
            self.assertFalse([hit for hit in frame.hits if hit.y == row and hit.action[:2] == ("ui", "sel")])

    def test_the_cli_line_is_one_row_cut_with_an_ellipsis_and_the_row_count_does_not_grow(self):
        wide, narrow = _pic("today", 140, "balanced/claude"), _pic("today", 100, "balanced/claude")
        for shown in (wide, narrow):
            self.assertEqual(len([row for row in shown if "CLI" in row]), 1)
            self.assertEqual(shown[9][:3], "│ >")
            self.assertEqual(shown[11], "│")                                      # the blank row before the box
        self.assertTrue(next(row for row in narrow if "CLI" in row).endswith("…"))
        self.assertTrue(next(row for row in wide if "CLI" in row).endswith("…"))   # 105 cells are not enough
        for shown in (wide, narrow):          # what to do comes before why: the cut takes only the reason
            self.assertIn("codex CLI 0.163.0 · update it yourself · lists gpt-6.1-sol", next(row for row in shown if "CLI" in row))

    def test_todays_machine_two_lines_no_mark_no_legend(self):
        shown = _pic("today", 140, "balanced/claude")
        self.assertEqual(shown[9], "│ > next opencode tier/deep · on the next update or refresh ▸")
        self.assertTrue(shown[10].startswith("│ codex CLI 0.163.0 · update it yourself · lists gpt-6.1-sol, gpt-6-astra as previous"))
        self.assertFalse(any(glyph in "".join(shown[1:8]) for glyph in "^=!"))
        self.assertNotIn("Followed", "\n".join(shown))

    def test_a_cli_note_that_names_a_newer_version_is_told_in_the_core_words(self):
        view = follow.Follow({"cells": {}, "cli": [{"harness": "claude", "version": "2.1.0", "kind": "min-version",
                                                     "models": [["a", "2.2"], ["b", "2.3"], ["c", "2.4"], ["d", "2.5"]],
                                                     "command": "claude update"}]}, {}, None)
        self.assertEqual(view.cli_lines(), ["claude CLI 2.1.0 · claude update · older than a (needs 2.2), "
                                            "b (needs 2.3), c (needs 2.4) and 1 more"])

    def test_every_row_stays_inside_the_pane(self):
        opened = {f"ui:fopen:{g}": "open" for g in follow.GROUPS}
        for name in ALL:
            for width in (100, 140):
                for select in ("deep/codex", "balanced/claude", "light/codex"):
                    with self.subTest(name=name, width=width, select=select):
                        for row in picture.section_text(_models(name), width, select, TODAY, opened):
                            self.assertLessEqual(cell_width(row[2:]), width - 35, row)


class AnotherListDay(unittest.TestCase):
    """The codex list was written by another CLI version: the cells are unknown, the line says why, and
    what the table already followed stays drawn (the answer's `null` meets the old rule `_shown`)."""

    def _row(self, shown):
        return next(row for row in shown if "CLI" in row)

    def test_the_line_comes_in_the_known_order_and_what_to_do_is_always_shown(self):
        for width in (80, 100, 140):
            with self.subTest(width=width):
                shown = _pic("another-list", width, "balanced/claude")
                self.assertEqual(len([row for row in shown if "CLI" in row]), 1)
                self.assertIn("codex CLI 0.171.0 · start codex once", self._row(shown))
        self.assertTrue(self._row(_pic("another-list", 140, "balanced/claude")).endswith(
            "codex CLI 0.171.0 · start codex once · model list is from 0.163.0"))
        self.assertEqual(follow.Follow({"cells": {}, "cli": [follow._note(samples.CLI_ANOTHER)]}, {}, None).cli_lines(),
                         ["codex CLI 0.171.0 · start codex once · model list is from 0.163.0"])

    def test_the_five_followed_cells_keep_their_marks_the_summary_and_the_undo(self):
        for width in (100, 140):
            with self.subTest(width=width):
                shown = _pic("another-list", width)
                self.assertEqual([" ^ " in shown[i] for i in (2, 4)], [True, True])   # top, balanced-deep
                self.assertIn("▶", shown[3])                                          # deep: the chosen cell
                self.assertTrue(any(row.startswith("│ ^ followed 11-22 · 5 cells") for row in shown))
                self.assertIn("[ undo ]", _box(shown)[2])
        before = _pic("another-list", 140, "mini/codex")
        self.assertTrue(any(row.startswith("│ ^ followed") for row in before))

    def test_the_drawing_is_the_followed_day_with_one_more_line(self):
        for width in (100, 140):
            for select in ("deep/codex", "top/codex", "balanced/claude"):
                with self.subTest(width=width, select=select):
                    before, after = _pic("followed", width, select), _pic("another-list", width, select)
                    rest = [row for row in after if "CLI" not in row]
                    self.assertEqual(len(after), len(before) + 1)
                    self.assertEqual(rest, before)

    def test_a_cell_with_no_record_of_a_follow_is_not_offered_one_on_a_list_that_is_not_the_cli_s(self):
        self.assertIn("[ Follow ]", "\n".join(_pic("followed", 140, "light/codex")))
        self.assertNotIn("[ Follow ]", "\n".join(_pic("another-list", 140, "light/codex")))

    def test_a_note_of_an_odd_shape_drops_only_its_line(self):
        for odd in ({"list_version": 5}, {"list_version": None}, {"list_version": ""}, {"version": 7},
                    {"version": ""}, {"harness": "nobody"}, {"kind": "list-from-nobody"}):
            with self.subTest(odd=odd):
                answer = copy.deepcopy(ALL["another-list"])
                answer[1]["cli"] = {**samples.CLI_ANOTHER, **odd}
                models = _models(answer=answer)
                self.assertEqual(models["follow"]["cli"], [])
                shown = picture.section_text(models, 140, "deep/codex", TODAY)
                self.assertEqual(shown, _pic("followed", 140))

    def test_cells_the_answer_cannot_tell_and_that_have_nothing_to_show_are_not_drawn(self):
        answer = samples.another_list([samples.item("claude"), samples.item("codex", samples.plain_codex()),
                                       samples.item("opencode")])
        models = _models(answer=answer)
        self.assertEqual(models["follow"]["cells"]["codex"], [])
        self.assertEqual(len(models["follow"]["cli"]), 1)


class Box(unittest.TestCase):
    def _lines(self, name, select, width=140, **kw):
        box = _box(_pic(name, width, select, **kw))
        return [row.strip("│ ").rstrip(" │┐┘") for row in box[1:5]]

    def test_followed_cell_says_when_from_what_and_offers_undo(self):
        box = _box(_pic("followed", 140))
        self.assertTrue(box[1].startswith("│  │  now      gpt-6.2-astra · high"))
        self.assertTrue(box[2].startswith("│  │  followed 2026-11-22 · from gpt-6-astra · high"))
        self.assertIn("[ undo ]", box[2])
        self.assertTrue(box[3].startswith("│  │  follow   [ Follow ]   Keep"))
        self.assertEqual(box[4].strip("│ "), "")

    def test_kept_cells_say_so_and_say_undone_when_they_were_put_back(self):
        kept = _box(_pic("followed", 140, "mini/codex"))
        self.assertTrue(kept[2].startswith("│  │  kept     2026-11-20"))
        self.assertTrue(kept[3].startswith("│  │  follow     Follow   [ Keep ]"))
        undone = _box(_pic("keep", 140, "light/codex"))
        self.assertTrue(undone[2].startswith("│  │  kept     2026-11-22 · undone from gpt-6.1-luna"))
        self.assertTrue(undone[3].startswith("│  │  follow     Follow   [ Keep ]"))

    def test_a_held_cell_says_what_was_rejected_and_that_it_is_skipped(self):
        box = _box(_pic("held", 140, "deep/codex"))
        self.assertTrue(box[2].startswith("│  │  held     2026-11-22 · gpt-6.2-astra rejected · back on gpt-6-astra"))
        self.assertTrue(box[3].startswith("│  │  follow   [ Follow ]   Keep    skips gpt-6.2-astra"))

    def test_next_and_proposal_lines(self):
        box = _box(_pic("next", 140, "top/opencode", answer=None, targets={"opencode": {"top": "tier/deep"}}))
        self.assertTrue(box[2].startswith("│  │  next     opencode-go/glm-5.4-flash · on the next update or refresh")
                        or box[2].startswith("│  │  next     glm-5.4-flash · on the next update or refresh"), box[2])
        box = _box(_pic("proposal", 140, "balanced/codex"))
        self.assertTrue(box[2].startswith("│  │  proposal gpt-7-terra · other family · choose it yourself"))
        self.assertTrue(box[3].startswith("│  │  follow   [ Follow ]   Keep"))

    def test_at_most_two_status_lines_and_never_the_long_explanations(self):
        answer = copy.deepcopy(ALL["followed"])
        answer[1]["cells"][1]["next"] = samples.upcoming("gpt-6.3-astra", "high")
        box = _box(_pic(answer=answer, select="deep/codex"))
        self.assertTrue(box[2].startswith("│  │  followed"))
        self.assertTrue(box[3].startswith("│  │  next     gpt-6.3-astra"))
        self.assertTrue(box[4].startswith("│  │  follow   "))
        text = "\n".join(box)
        for word in ("list ", "scope", "change ", "launch", "running", "caution", "evidence", "limit "):
            self.assertNotIn("│  │  " + word, text)

    def test_the_status_line_fits_one_row_at_width_100_without_growing_the_box(self):
        answer = copy.deepcopy(ALL["followed"])
        answer[2]["cells"][0] = samples.followed("profile/top", "opencode-go/deepseek-v4.1-flash",
                                                 "opencode-go/glm-5.3-flash", "xhigh")
        wide = _box(_pic(answer=answer, width=140, select="top/opencode"))
        narrow = _box(_pic(answer=answer, width=100, select="top/opencode"))
        for box in (wide, narrow):
            self.assertTrue(box[2].startswith("│  │  followed"), box[2])
            self.assertTrue(box[3].startswith("│  │  follow   "), box[3])
        self.assertIn("[ undo ]", narrow[2])
        self.assertNotIn("\n", narrow[2])
        # the same box, one row of status each: the rows below start where they did
        self.assertEqual(sum(1 for row in narrow if row.startswith("│  │  followed")), 1)

    def test_the_status_line_gives_up_effort_then_year_then_a_cut(self):
        from tools.fleet.explorer import models as screen
        candidates = ["2026-11-22 · from gpt-6-astra · high", "2026-11-22 · from gpt-6-astra", "11-22 · from gpt-6-astra"]
        self.assertEqual(screen._fit(candidates, 40), candidates[0])
        self.assertEqual(screen._fit(candidates, 30), candidates[1])
        self.assertEqual(screen._fit(candidates, 25), candidates[2])
        self.assertTrue(screen._fit(candidates, 12).endswith("…"))
        self.assertLessEqual(cell_width(screen._fit(candidates, 12)), 12)

    def test_models_are_shown_by_the_names_the_table_uses(self):
        answer = copy.deepcopy(ALL["followed"])
        answer[2]["cells"][0] = samples.followed("profile/top", "opencode-go/deepseek-v4.1-flash",
                                                 "opencode-go/glm-5.3-flash", "max")
        box = _box(_pic(answer=answer, width=140, select="top/opencode"))
        self.assertIn("followed 2026-11-22 · from deepseek-v4.1-flash · max", box[2])

    def test_a_chosen_cell_that_is_a_mark_keeps_its_arrow_and_the_box_has_its_state(self):
        shown = _pic("held", 140, "deep/codex")
        self.assertIn("▶ gpt-6-astra · high", shown[3])
        self.assertNotIn("! gpt-6-astra", shown[3])
        self.assertIn("held     2026-11-22", "\n".join(shown))


class Drafts(unittest.TestCase):
    def _draft(self, name, select, chip, width=140, **kw):
        models = _models(name, **kw)
        vm, state, frame = _frame(models, width, select)
        state, _effects, frame = _click(vm, state, frame, width, lambda a: a == ("ui", "fol", chip))
        return models, vm, state, frame

    def test_keep_on_a_following_cell_drafts_the_keep_command_and_writes_nothing(self):
        _m, _vm, state, frame = self._draft("followed", "balanced/codex", "keep")
        self.assertEqual(state.fold_map()["ui:fol"], "keep")
        row = next(r for r in frame.text_rows() if "$ hearting" in r)
        self.assertIn("$ hearting model set codex profile/balanced keep", row)
        self.assertIn("[ dry-run ]", "\n".join(frame.text_rows()))
        self.assertIn("follow     Follow   [ Keep ]", "\n".join(frame.text_rows()))
        actions = {hit.action for hit in frame.hits if hit.action[:1] == ("models.apply",)}
        self.assertEqual(actions, {("models.apply", "codex", "follow", "profile/balanced", "keep", True),
                                   ("models.apply", "codex", "follow", "profile/balanced", "keep", False)})

    def test_follow_on_a_kept_or_held_cell_drafts_the_follow_command(self):
        for name, select, target in (("followed", "mini/codex", "profile/mini"), ("held", "deep/codex", "profile/deep")):
            _m, _vm, state, frame = self._draft(name, select, "follow")
            row = next(r for r in frame.text_rows() if "$ hearting" in r)
            self.assertIn(f"$ hearting model set codex {target} follow", row)

    def test_follow_on_a_cell_that_is_following_is_not_a_draft(self):
        _m, _vm, _state, frame = self._draft("followed", "balanced/codex", "follow")
        self.assertFalse([hit for hit in frame.hits if hit.action[:1] == ("models.apply",)])

    def test_undo_drafts_the_model_before_and_says_the_cell_is_then_kept(self):
        _m, _vm, state, frame = self._draft("followed", "deep/codex", "undo")
        text = "\n".join(frame.text_rows())
        self.assertIn("$ hearting model set codex profile/deep gpt-6-astra@high", text)
        self.assertIn("then Keep", text)
        self.assertIn("now      gpt-6.2-astra · high  →  gpt-6-astra · high", text)
        self.assertEqual(state.fold_map()["ui:fol"], "undo")
        undo_rows = [row for row in frame.text_rows() if "then Keep" in row]
        follow_row = next(i for i, row in enumerate(frame.text_rows()) if "follow   [ Follow ]" in row)
        self.assertIn("then Keep", frame.text_rows()[follow_row])       # at the end of the follow row
        self.assertEqual(len(undo_rows), 1)

    def test_the_draft_keeps_its_command_whole_and_its_buttons_at_width_100(self):
        _m, _vm, _state, frame = self._draft("followed", "deep/codex", "undo", width=100)
        rows = frame.text_rows()
        command = next(i for i, row in enumerate(rows) if "$ hearting" in row)
        self.assertIn("$ hearting model set codex profile/deep gpt-6-astra@high", rows[command])
        self.assertIn("[ dry-run ]  [ apply ]", rows[command - 1])

    def test_a_cell_in_a_tier_names_the_tier_in_the_command(self):
        answer = [samples.item("codex", [samples.followed("tier/deep", "gpt-6.1-sol", "gpt-6.2-sol", "xhigh")])]
        targets = {"codex": {"deep": "tier/deep"}}
        models = _models(answer=answer, targets=targets)
        vm, state, frame = _frame(models, 140, "deep/codex")
        state, _e, frame = _click(vm, state, frame, 140, lambda a: a == ("ui", "fol", "undo"))
        self.assertIn("$ hearting model set codex tier/deep gpt-6.1-sol ",         # the tier line keeps its effort
                      next(r for r in frame.text_rows() if "$ hearting" in r))

    def test_a_model_draft_and_a_keep_draft_do_not_stand_together(self):
        models = _models("followed")
        vm, state, frame = _frame(models, 140, "balanced/codex", folds={"ui:model": "gpt-6-luna"})
        state, _e, frame = _click(vm, state, frame, 140, lambda a: a == ("ui", "fol", "keep"))
        self.assertNotIn("ui:model", state.fold_map())
        state, _e, frame = _click(vm, state, frame, 140, lambda a: a[:2] == ("ui", "effort") and a[2] == "high")
        self.assertNotIn("ui:fol", state.fold_map())
        state, _e, frame = _click(vm, state, frame, 140, lambda a: a == ("ui", "fol", "keep"))
        state, _e, frame = _click(vm, state, frame, 140, lambda a: a[:3] == ("ui", "sel", "top/codex"))
        self.assertNotIn("ui:fol", state.fold_map())

    def test_apply_hands_the_command_on_and_clears_the_draft_a_dry_run_keeps_it(self):
        models, vm, state, frame = self._draft("followed", "balanced/codex", "keep")
        dry = next(h.action for h in frame.hits if h.action[:1] == ("models.apply",) and h.action[-1] is True)
        app = next(h.action for h in frame.hits if h.action[:1] == ("models.apply",) and h.action[-1] is False)
        after, effects = reduce(state, {"type": "click", "action": dry}, frame)
        self.assertEqual((effects, after.fold_map().get("ui:fol")), ((dry,), "keep"))
        after, effects = reduce(state, {"type": "click", "action": app}, frame)
        self.assertEqual((effects, after.fold_map().get("ui:fol")), ((app,), "keep"))   # not cleared before it is written
        for status, kept in (("changed", False), ("unchanged", False), ("failed", True), ("invalid-arguments", True),
                             ("read-only", True)):
            done = finish_follow_draft(after, effects, {"model_action": True, "status": status})
            self.assertEqual("ui:fol" in done.fold_map(), kept, status)
        self.assertIn("ui:fol", finish_follow_draft(state, (dry,), {"status": "planned"}).fold_map())


class Apply(unittest.TestCase):
    def _runtime(self):
        from tools.fleet.explorer.runtime import ExplorerRuntime
        return ExplorerRuntime()

    def test_the_effect_is_checked_against_the_answer_and_the_table_is_read_again_after(self):
        models = _models("followed")
        vm = SimpleNamespace(models=models, ordering={})
        calls, fresh = [], {"profiles": ["balanced"], "marker": "read again"}

        def set_model(harness, target, requested, **kw):
            calls.append((harness, target, requested, kw["dry_run"]))
            return {"status": "changed", "lines": ["model set: ok"]}

        settings = SimpleNamespace(set_model=set_model, ModelSettingsUsageError=ValueError)
        reads = []
        runtime = self._runtime()
        with mock.patch.object(runtime, "_model_settings_module", return_value=settings), \
                mock.patch.object(focus, "read_model_config", side_effect=lambda **kw: reads.append(kw) or fresh), \
                mock.patch.object(focus, "utility", return_value=SimpleNamespace(runtime_home=lambda h: "/nowhere")):
            for requested in ("keep", "follow", "gpt-6-astra@high"):
                result = runtime.apply_effects([("models.apply", "codex", "follow", "profile/deep", requested, False)], vm)
                self.assertEqual((result["status"], result["models"]), ("changed", fresh), requested)
            for bad in (("models.apply", "codex", "follow", "profile/deep", "gpt-9@high", False),
                        ("models.apply", "codex", "follow", "tier/nothing", "keep", False),
                        ("models.apply", "nobody", "follow", "profile/deep", "keep", False),
                        ("models.apply", "codex", "other", "profile/deep", "keep", False),
                        ("models.apply", "codex", "follow", "profile/balanced", "gpt-6-astra@high", False)):
                result = runtime.apply_effects([bad], vm)
                self.assertEqual(result["status"], "failed", bad)
        self.assertEqual(calls, [("codex", "profile/deep", "keep", False), ("codex", "profile/deep", "follow", False),
                                 ("codex", "profile/deep", "gpt-6-astra@high", False)])
        self.assertEqual(reads, [{"refresh": False}] * 3)     # one read after each apply; a refused effect reads nothing

    def test_keep_then_follow_on_a_copy_of_a_table_in_a_temporary_home(self):
        answer = [samples.item("opencode", [samples.cell("tier/light", "opencode-go/deepseek-v4.1-flash", "max")])]
        models = _models(answer=answer)
        vm = SimpleNamespace(models=models, ordering={})
        with TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "opencode-runtime"
            environment = {
                "AGENT_HOME": str(Path(__file__).resolve().parents[3]), "HOME": str(root),
                "CLAUDE_CONFIG_DIR": str(root / "claude"), "CODEX_HOME": str(root / "codex"),
                "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"), "XDG_CACHE_HOME": str(root / "cache"),
                "HEARTING_MODEL_CONFIG_HOME": str(home), "FLEET_DEMO": "",
            }
            with mock.patch.dict(os.environ, environment):
                config = home / "agent-config" / "models.conf"
                config.parent.mkdir(parents=True)
                shutil.copyfile(Path(environment["AGENT_HOME"]) / "adapters/opencode/config/models.conf", config)
                before = config.read_bytes()
                runtime = self._runtime()
                with mock.patch.object(focus, "read_model_config", return_value={"profiles": []}):
                    planned = runtime.apply_effects([("models.apply", "opencode", "follow", "tier/light", "keep", True)], vm)
                    self.assertEqual(planned["status"], "planned")
                    self.assertEqual(config.read_bytes(), before)
                    done = runtime.apply_effects([("models.apply", "opencode", "follow", "tier/light", "keep", False)], vm)
                    self.assertEqual(done["status"], "changed")
                    self.assertIn("# auto-follow keep ", config.read_text())
                    back = runtime.apply_effects([("models.apply", "opencode", "follow", "tier/light", "follow", False)], vm)
                    self.assertEqual(back["status"], "changed")
                    self.assertEqual(config.read_bytes(), before)


class ReadOnce(unittest.TestCase):
    def test_the_function_is_asked_once_per_read_with_the_lists_that_were_read(self):
        catalogs = {"codex": SimpleNamespace(state="available", rows=({"id": "gpt-6-astra"},)),
                    "claude": SimpleNamespace(state="available", rows=({"id": "claude-opus-5-5"},)),
                    "opencode": SimpleNamespace(state="unavailable", rows=())}
        asked = []

        class Utility:
            @staticmethod
            def fleet_view(**kw):
                asked.append(kw)
                assert kw["read_catalog"]("codex") is catalogs["codex"]
                return [samples.item("codex", [samples.cell("tier/deep", "gpt-6-astra", "high"),
                                               samples.unfollowable("tier/light", "gpt-5", "high")]),
                        samples.item("claude", [samples.unfollowable("tier/deep", "opus", "xhigh")])]

        with mock.patch.object(focus, "utility", return_value=Utility):
            answer = focus._read_follow(catalogs)
        self.assertEqual(len(asked), 1)
        self.assertEqual(set(asked[0]), {"read_catalog", "probe"})
        self.assertEqual(len(answer), 2)

    def test_drawing_never_asks_for_anything(self):
        models = _models("followed")
        with mock.patch.object(focus, "utility", side_effect=AssertionError("drawing asked")), \
                mock.patch.object(focus, "_read_follow", side_effect=AssertionError("drawing asked")):
            for width in (100, 140):
                picture.section_text(models, width, "deep/codex", TODAY)
                picture.section_text(models, width, "deep/codex", TODAY, {"ui:fol": "undo"})

    def test_the_table_read_names_the_cell_each_profile_gets_its_value_from(self):
        class ModelConfig:
            @staticmethod
            def resolve_config(adapter):
                if adapter != "claude":
                    raise LookupError(adapter)
                return {"CFG_MODEL_PROFILE_DEEP": "opus:xhigh", "CFG_TIER_OPUS_MODEL": "opus",
                        "CFG_MODEL_PROFILE_LIGHT": "model/claude-haiku-5-5:low"}, {}

        with mock.patch.object(focus, "utility", return_value=ModelConfig):
            config = focus.read_model_config()
        self.assertNotIn("follow", config)                    # a function that cannot answer leaves it out
        self.assertEqual(config["profile_target"]["claude"]["deep"], "tier/opus")
        self.assertEqual(config["profile_target"]["claude"]["light"], "profile/light")
        self.assertIsNone(config["profile_target"]["claude"]["top"])
        self.assertEqual(focus._profile_target({"CFG_MODEL_PROFILE_BALANCED_DEEP": "balanced-deep:high"}, "balanced-deep"),
                         "tier/balanced-deep")
        self.assertIsNone(focus._profile_target({"CFG_MODEL_PROFILE_DEEP": "deep:x",
                                                 "CFG_MODEL_PROFILE_GRANULARITY_DEEP": "collapsed"}, "deep"))



# ---- the review of the screen: one box says one thing, the keyboard and the mouse agree, the input
# ---- thread starts no process, an odd answer costs only following, the lines do not run into each other

AGENT_HOME = Path(__file__).resolve().parents[3]
ODD_ANSWERS = ([{"harness": "codex", "cells": 5}],
               [{"harness": "codex", "cells": [{"target": "profile/deep", "model": ["x"], "state": "follow"}]}],
               [{"harness": "codex", "cells": [{"target": "profile/deep", "model": {"a": 1}, "state": "follow"}]}],
               [{"cells": [{"model": "x"}]}])


def _through_a_tier(*, table_effort="medium", tier_effort="xhigh", effort_was=None, moved=()):
    """A codex ``deep`` profile that is ``deep:<table_effort>`` through a tier whose own effort is ``tier_effort``,
    followed the day before: the table and the answer, as the product reads them."""
    answer = [samples.item("codex", [samples.followed("tier/deep", "gpt-6.1-sol", "gpt-6.2-sol", tier_effort,
                                                       effort_was=effort_was, moved=moved)])]
    models = picture.base_models()
    models["profile_map"]["codex"]["deep"] = ["gpt-6.2-sol", table_effort]
    models["profile_target"] = {"codex": {"deep": "tier/deep"}}
    focus.follow_into(picture.fit_table(models, answer), answer)
    return models


def _box_rows(models, width=140, folds=None, select="deep/codex"):
    return _box(picture.section_text(models, width, select, TODAY, folds))


class OneBoxOneThing(unittest.TestCase):
    """The state line and the preview say what the cell shows after the write: its own effort."""

    def test_a_cell_through_a_tier_shows_its_own_effort_in_the_box_and_asks_for_no_effort(self):
        models = _through_a_tier()
        for width in (100, 140):
            box = "\n".join(_box_rows(models, width, {"ui:fol": "undo"}))
            with self.subTest(width=width):
                self.assertIn("now      gpt-6.2-sol · medium  →  gpt-6.1-sol · medium", box)
                self.assertNotIn("xhigh", box.split("effort")[0])
                self.assertIn("$ hearting model set codex tier/deep gpt-6.1-sol ", box)
                self.assertNotIn("gpt-6.1-sol@", box)
        plain = "\n".join(_box_rows(models, 140))
        self.assertIn("followed 2026-11-22 · from gpt-6.1-sol · medium", plain)

    def test_the_request_of_a_follow_that_lowered_the_effort_puts_it_back(self):
        # the cell goes through a tier: the tier line is put back at the effort the follow lowered it from
        models = _through_a_tier(tier_effort="high", effort_was="xhigh")
        box = "\n".join(_box_rows(models, 140, {"ui:fol": "undo"}))
        self.assertIn("$ hearting model set codex tier/deep gpt-6.1-sol@xhigh", box)
        self.assertIn("now      gpt-6.2-sol · medium  →  gpt-6.1-sol · medium", box)     # the profile line's own effort
        # a cell that names its model goes back to the effort it had before, in the box and in the request
        answer = [samples.item("codex", [samples.followed("profile/deep", "gpt-6-astra", "gpt-6.2-astra", "high",
                                                           effort_was="xhigh")])]
        direct = _models(answer=answer)
        box = "\n".join(_box_rows(direct, 140, {"ui:fol": "undo"}))
        self.assertIn("now      gpt-6.2-astra · high  →  gpt-6-astra · xhigh", box)
        self.assertIn("followed 2026-11-22 · from gpt-6-astra · xhigh", box)
        self.assertIn("$ hearting model set codex profile/deep gpt-6-astra@xhigh", box)
        cell = direct["follow"]["cells"]["codex"][0]
        self.assertEqual(follow.undo_request(cell), "gpt-6-astra@xhigh")
        self.assertEqual(follow.undo_request(_through_a_tier()["follow"]["cells"]["codex"][0]), "gpt-6.1-sol")

    def test_the_runtime_takes_the_same_request_the_box_drafts(self):
        from tools.fleet.explorer.runtime import ExplorerRuntime
        for models, asked in ((_through_a_tier(), "gpt-6.1-sol"),
                              (_through_a_tier(tier_effort="high", effort_was="xhigh"), "gpt-6.1-sol@xhigh")):
            seen = []
            settings = SimpleNamespace(set_model=lambda *a, **kw: seen.append(a) or {"status": "changed", "lines": []},
                                       ModelSettingsUsageError=ValueError)
            runtime = ExplorerRuntime()
            vm = SimpleNamespace(models=models, ordering={})
            with mock.patch.object(runtime, "_model_settings_module", return_value=settings), \
                    mock.patch.object(focus, "read_model_config", return_value=None), \
                    mock.patch.object(focus, "utility", return_value=SimpleNamespace(runtime_home=lambda h: "/nowhere")):
                result = runtime.apply_effects([("models.apply", "codex", "follow", "tier/deep", asked, False)], vm)
                self.assertEqual(result["status"], "changed", asked)
                bad = runtime.apply_effects([("models.apply", "codex", "follow", "tier/deep", "gpt-6.1-sol@low", False)], vm)
                self.assertEqual(bad["status"], "failed")
            self.assertEqual([call[2] for call in seen], [asked])


def _isolated(root, *, followed=True, slow_claude=False, list_version="0.170.0"):
    """An isolated HOME: the shipped codex table (``deep:medium`` through a tier whose effort is xhigh), a made-up
    list, and fake CLIs that only count their calls (the real ones are never on the PATH)."""
    import datetime
    home, calls = root / "codex", root / "calls"
    for folder in ("codex/agent-config", "claude/agent-config", "config", "state", "data", "cache", "bin"):
        (root / folder).mkdir(parents=True)
    text = (AGENT_HOME / "adapters/codex/config/models.conf").read_text()
    if followed:
        day = (datetime.date.fromisoformat(TODAY) - datetime.timedelta(days=1)).isoformat()
        text = text.replace("CFG_TIER_DEEP_MODEL=gpt-6.1-sol",
                            f"CFG_TIER_DEEP_MODEL=gpt-6.2-sol # auto-follow {day}: gpt-6.1-sol -> gpt-6.2-sol")
    (home / "agent-config" / "models.conf").write_text(text)
    levels = '"supported_reasoning_levels":["low","medium","high","xhigh"]'
    rows = ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"] + (["gpt-6.2-sol"] if followed else [])
    (home / "models_cache.json").write_text('{"client_version":"%s","models":[' % list_version + ",".join(
        '{"slug":"%s","visibility":"list",%s}' % (slug, levels) for slug in rows) + ']}')
    if slow_claude:
        shutil.copyfile(AGENT_HOME / "adapters/claude/config/models.conf", root / "claude/agent-config/models.conf")
        (root / "claude/cache/model-catalog").mkdir(parents=True)
        (root / "claude/cache/model-catalog/a.json").write_text(
            '{"catalog":{"config":{"models":[{"id":"claude-opus-5-5","section":"main","min_claude_code_version":"9.9.0"},'
            '{"id":"claude-sonnet-5-5","section":"main"},{"id":"claude-haiku-5-5","section":"main"}]}}}')
    for name in ("claude", "codex", "opencode"):
        script = root / "bin" / name
        version = "0.170.0" if name == "codex" else "2.1.0"   # the codex list below was written by this one
        script.write_text(f'#!/bin/sh\necho {name} >> "{calls}"\n[ {name} = opencode ] && exit 1\n'
                          f'[ "$1" = debug ] && echo x >> "{root / "refreshed"}"\n'
                          f'echo "{version} (fake {name})"\n')
        script.chmod(0o755)
    kept = {key: os.environ[key] for key in ("PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE", "LANG", "LC_ALL")
            if key in os.environ}
    return {**kept, "AGENT_HOME": str(AGENT_HOME), "HOME": str(root), "CLAUDE_CONFIG_DIR": str(root / "claude"),
            "CODEX_HOME": str(home), "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
            "XDG_STATE_HOME": str(root / "state"), "XDG_CACHE_HOME": str(root / "cache"), "TMPDIR": str(root),
            "PATH": f"{root / 'bin'}:/usr/bin:/bin"}


def _new_version(root, version):
    """The codex list and the fake codex CLI both say ``version`` from now on (as after the CLI was raised)."""
    cache, script = root / "codex" / "models_cache.json", root / "bin" / "codex"
    cache.write_text(cache.read_text().replace('"client_version":"0.170.0"', '"client_version":"%s"' % version))
    script.write_text(script.read_text().replace("0.170.0 (fake", f"{version} (fake"))


def _env(root, **kw):
    """The environment of an isolated HOME and nothing else (a pinned config home of the caller is not kept)."""
    return mock.patch.dict(os.environ, _isolated(root, **kw), clear=True)


def _calls(root):
    path = root / "calls"                 # (the catalog reader asks `opencode --version` itself: not what is counted here)
    return [name for name in path.read_text().split() if name != "opencode"] if path.exists() else []


class TheTableReadAgain(unittest.TestCase):
    """Written to a real table in an isolated HOME and read again: the box said what the table shows."""

    def test_the_box_and_the_table_read_after_the_write_agree(self):
        from tools.fleet.explorer.runtime import ExplorerRuntime
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with _env(root):
                models = focus.read_model_config()
                self.assertEqual(models["profile_target"]["codex"]["balanced-deep"], "tier/deep")
                vm, state, frame = _frame(models, 140, "balanced-deep/codex")
                state, _e, frame = _click(vm, state, frame, 140, lambda a: a == ("ui", "fol", "undo"))
                rows = frame.text_rows()
                preview = next(row for row in rows if "now      gpt-6.2-sol" in row and "→" in row)
                said = preview.split("→")[1].replace("│", "").strip()
                stated = next(row for row in rows if "from gpt-6.1-sol" in row)
                self.assertIn(f"from gpt-6.1-sol · {said.split(' · ')[1]}", stated)
                apply = next(h for h in frame.hits if h.action[:3] == ("models.apply", "codex", "follow")
                             and h.action[-1] is False)
                result = ExplorerRuntime().apply_effects((apply.action,), vm)
                self.assertEqual(result["status"], "changed")
                again = result["models"]["profile_map"]["codex"]["balanced-deep"]
                self.assertEqual(f"{again[0]} · {again[1]}", said)


class AnotherListRead(unittest.TestCase):
    """The real read on an isolated HOME whose codex list was written by another CLI version."""

    def test_the_cells_are_unknown_the_line_is_there_and_nothing_refreshes_the_list(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with _env(root, list_version="0.163.0"):
                path = root / "codex" / "models_cache.json"
                stat = (path.read_bytes(), path.stat().st_mtime_ns)
                models = focus.read_model_config()
                self.assertFalse((root / "refreshed").exists())              # nothing asked codex for its list
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), stat)
                notes = {note["harness"]: note for note in models["follow"]["cli"]}
                self.assertEqual((notes["codex"]["kind"], notes["codex"]["version"], notes["codex"]["list_version"]),
                                 ("list-from-another-cli", "0.170.0", "0.163.0"))
                shown = picture.section_text(models, 140, "balanced-deep/codex", TODAY)
                self.assertTrue(any("codex CLI 0.170.0 · start codex once · model list is from 0.163.0" in r
                                    for r in shown))
                self.assertTrue(any(row.startswith("│ ^ followed") for row in shown))   # the table's follow stays drawn
                path.write_text(path.read_text().replace("0.163.0", "0.170.0"))     # the list is the CLI's again
                self.assertEqual([note["harness"] for note in focus.read_model_config()["follow"]["cli"]], [])


class TheKeyboardAndTheMouse(unittest.TestCase):
    def _kept_then_list(self, models):
        vm, state, frame = _frame(models, 100, "deep/codex")
        state, _e, frame = _click(vm, state, frame, 100, lambda a: a == ("ui", "fol", "keep"))
        state, _e, frame = _click(vm, state, frame, 100, lambda a: a[:1] == ("models.find",))
        for key in ("down", "down", "enter"):
            state, _effects = reduce(state, {"type": "key", "key": key}, frame)
            frame = draw(vm, state, 100, 70, beat=1)
        return state, frame

    def test_choosing_a_model_with_the_keys_drops_the_follow_draft(self):
        state, frame = self._kept_then_list(_models("followed"))
        folds = state.fold_map()
        self.assertNotIn("ui:fol", folds)
        chosen = folds["ui:model"]
        self.assertNotEqual(chosen, "gpt-6.2-astra")                     # a model the list gave, not the cell's own
        command = next(row for row in frame.text_rows() if "$ hearting" in row)
        self.assertIn("$ hearting model set codex profil", command)       # a model draft's command (cut at 100), not keep
        self.assertNotIn(" keep", command)
        written = [hit.action for hit in frame.hits if hit.action[:1] == ("models.apply",)]
        self.assertEqual(written, [("models.apply", "codex", "profile", "deep", chosen, "high", True),
                                   ("models.apply", "codex", "profile", "deep", chosen, "high", False)])

    def test_every_way_to_start_one_draft_drops_the_other(self):
        from tools.fleet.explorer.state import _draft
        for key, gone in (("ui:model", ["ui:fol"]), ("ui:effort", ["ui:fol"]),
                          ("ui:fol", ["ui:model", "ui:effort", "ui:mfocus"]),
                          ("ui:sel", ["ui:model", "ui:effort", "ui:fol", "ui:mfocus"])):
            folds = {"ui:fol": "keep", "ui:model": "m", "ui:effort": "high", "ui:mfocus": "1"}
            _draft(folds, {"mpick": 3}, key)
            self.assertEqual([name for name in folds if name in gone], [], key)

    def test_a_refused_write_keeps_the_draft_and_a_written_one_drops_it(self):
        from tools.fleet.explorer.runtime import ExplorerRuntime
        from tools.fleet.explorer import runtime as runtime_module
        models = _models("followed")
        vm, state, frame = _frame(models, 140, "balanced/codex", folds={"ui:fol": "keep"})
        apply = next(h.action for h in frame.hits if h.action[:1] == ("models.apply",) and h.action[-1] is False)
        state, effects = reduce(state, {"type": "click", "action": apply}, frame)
        runtime = ExplorerRuntime()
        for status, left in (("failed", True), ("changed", False)):
            settings = SimpleNamespace(set_model=lambda *a, **kw: {"status": status, "lines": [f"model set: {status}"]},
                                       ModelSettingsUsageError=ValueError)
            with mock.patch.object(runtime, "_model_settings_module", return_value=settings), \
                    mock.patch.object(focus, "read_model_config", return_value=None), \
                    mock.patch.object(focus, "utility", return_value=SimpleNamespace(runtime_home=lambda h: "/nowhere")):
                result = runtime.apply_effects(effects, vm)
            after = finish_follow_draft(state, effects, result)
            self.assertEqual("ui:fol" in after.fold_map(), left, status)


    def test_the_screen_drops_the_draft_only_when_the_result_says_it_was_written(self):
        from tools.fleet import render
        effect = ("models.apply", "codex", "follow", "tier/deep", "keep", False)
        for status, left in (("changed", False), ("failed", True), ("invalid-arguments", True)):
            state = NavState(view="MODELS", title="MODELS", folds=(("ui:fol", "keep"), ("ui:sel", "deep/codex")))
            runtime = SimpleNamespace(apply_effects=lambda effects, vm, status=status: {
                "model_action": True, "status": status, "message": f"model set: {status}", "models": None})
            with mock.patch.object(render, "_explorer_runtime", return_value=runtime), \
                    mock.patch.object(render, "_EXPLORER_STATE", state), \
                    mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", False):
                render._explorer_apply_effects((effect,), SimpleNamespace(models={}))
                self.assertEqual("ui:fol" in render._EXPLORER_STATE.fold_map(), left, status)
                self.assertEqual(render._EXPLORER_STATE.status, f"model set: {status}")


class LeftBehindAndLastCandidate(unittest.TestCase):
    """A cell that followed and later left its list keeps its record and undo; ``_fit`` takes a last
    candidate that is a ``(name, tail, keep)`` tuple."""

    def test_a_followed_cell_whose_model_left_the_list_still_shows_its_record_and_undo(self):
        answer = copy.deepcopy(ALL["followed"])
        codex = next(item for item in answer if item["harness"] == "codex")
        gone = next(c for c in codex["cells"] if c["target"] == "profile/deep")
        gone.update(followable=False, reason="not-in-list")
        plain = samples.unfollowable("profile/mini", "gpt-6-luna", "high")
        codex["cells"] = [gone if c["target"] == "profile/deep" else c for c in codex["cells"]
                          if c["target"] != "profile/mini"] + [plain]
        found = follow.normalize(answer)["cells"]["codex"]
        self.assertIn("profile/deep", [c["target"] for c in found])        # drawn for its record
        self.assertNotIn("profile/mini", [c["target"] for c in found])     # nothing to show: not drawn
        box = _box(_pic(answer=answer, select="deep/codex"))
        self.assertTrue(box[2].startswith("│  │  followed 2026-11-22 · from gpt-6-astra · high"))
        self.assertIn("[ undo ]", box[2])

    def test_a_cell_that_left_the_list_with_a_refused_model_it_holds_is_drawn_too(self):
        answer = copy.deepcopy(ALL["held"])
        codex = next(item for item in answer if item["harness"] == "codex")
        for found in codex["cells"]:
            if found["target"] == "profile/deep":
                found.update(followable=False, reason="not-in-list")
        found = {c["target"]: c for c in follow.normalize(answer)["cells"]["codex"]}
        self.assertEqual(found["profile/deep"]["held"], "gpt-6.2-astra")

    def test_a_last_candidate_that_is_a_tuple_is_cut_from_its_name_and_tail(self):
        from tools.fleet.explorer import models as drawing
        for last in (("a-very-long-model-name", " tail", 8), ("a-very-long-model-name", " tail")):
            with self.subTest(last=last):
                cut = drawing._fit(["x" * 40, last], 6)
                self.assertEqual((cell_width(cut) <= 6, cut.startswith("a-ver")), (True, True))
        self.assertEqual(drawing._fit([("short", " tail", 8)], 40), "short tail")
        self.assertEqual(drawing._fit(["text that is long"], 6), drawing.clip_cells("text that is long", 6))


class SecondStepFailure(unittest.TestCase):
    """The second step of a background read (asking the CLI again) only improves the first answer."""

    def setUp(self):
        focus._CLI_SEEN.clear()
        self.addCleanup(focus._CLI_SEEN.clear)

    def test_an_error_in_the_second_view_keeps_the_first_answer(self):
        note = {"harness": "codex", "version": "0.171.0", "models": [], "kind": "list-from-another-cli",
                "command": None, "list_version": "0.163.0"}
        first = [samples.item("codex", samples.plain_codex(), cli=note)]

        class Utility:
            views = 0

            def fleet_view(self, **_kw):
                Utility.views += 1
                if Utility.views == 2:
                    raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
                return first

            def installed_cli(self, _harness):
                return (0, 171, 0), "/fake/codex"       # not what was remembered: a second view is asked for

        with mock.patch.object(focus, "utility", return_value=Utility()):
            answer = focus._read_follow({}, refresh=True)
        self.assertEqual((Utility.views, answer), (2, first))


class InputReadTakesNoList(unittest.TestCase):
    def test_the_read_made_in_the_input_handling_reads_no_list_and_uses_the_background_ones(self):
        from tools.fleet.explorer import model_catalog
        focus._CATALOGS_SEEN.clear()
        self.addCleanup(focus._CATALOGS_SEEN.clear)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with _env(root):
                background = focus.read_model_config()
                with mock.patch.object(model_catalog, "read_catalogs",
                                       side_effect=AssertionError("the input thread read the lists")):
                    again = focus.read_model_config(refresh=False)
                self.assertEqual(again["model_choices"], background["model_choices"])
                self.assertEqual(again["catalog_sources"], background["catalog_sources"])
                self.assertTrue(again["catalog_sources"])


class NoProcessInTheInput(unittest.TestCase):
    """The read made inside the key and mouse handling starts no process: the CLI's answer is the
    last background read's."""

    def setUp(self):
        seen = getattr(focus, "_CLI_SEEN", {})                 # (what the last background read was told)
        seen.clear()
        self.addCleanup(seen.clear)
        focus._CATALOGS_SEEN.clear()                           # (and the lists it held)
        self.addCleanup(focus._CATALOGS_SEEN.clear)

    def test_the_input_thread_asks_the_cli_nothing_and_the_note_stays(self):
        from tools.fleet.explorer.runtime import ExplorerRuntime
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with _env(root, slow_claude=True):
                models = focus.read_model_config()                       # the background read
                asked = _calls(root)
                self.assertLessEqual(asked.count("claude"), 1)
                self.assertLessEqual(asked.count("codex"), 1)
                self.assertEqual([note["harness"] for note in models["follow"]["cli"]], ["claude"])
                runtime, vm = ExplorerRuntime(), SimpleNamespace(models=models, ordering={})
                claude = models["profile_map"]["claude"]["balanced"]
                before = len(_calls(root))
                results = [
                    runtime.apply_effects([("models.apply", "claude", "profile", "balanced", claude[0], claude[1], True)], vm),
                    runtime.apply_effects([("models.apply", "codex", "follow", "tier/deep", "keep", True)], vm),
                    runtime.apply_effects([("models.apply", "claude", "profile", "balanced", claude[0], claude[1], False)], vm),
                    runtime.apply_effects([("alloc.apply", (("window", "5"),), True)], vm)]
                self.assertEqual([r["status"] for r in results[:3]], ["unchanged", "planned", "unchanged"])
                self.assertEqual(len(_calls(root)), before, "the input thread started a process")
                for result in results:
                    if result["models"]:
                        self.assertEqual(result["models"]["follow"]["cli"], models["follow"]["cli"])
                self.assertTrue(any(r["models"] for r in results))

    def test_the_background_reads_after_the_first_use_its_answer_and_start_no_process(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with _env(root, list_version="0.170.0"):
                first = focus.read_model_config()                       # the first background read asks the CLI
                asked = _calls(root)
                self.assertEqual(asked.count("codex"), 1)
                for _again in range(3):                                 # the interval reads after it do not
                    again = focus.read_model_config()
                self.assertEqual(_calls(root), asked)
                self.assertEqual(again["follow"], first["follow"])

    def _another_line(self, models):
        return [note for note in models["follow"]["cli"] if note["kind"] == "list-from-another-cli"]

    def test_a_list_and_a_cli_raised_together_make_the_next_background_read_ask_again_and_the_line_never_stands(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with _env(root, list_version="0.170.0"):
                focus.read_model_config()                               # Fleet remembers 0.170.0
                self.assertEqual(_calls(root).count("codex"), 1)
                _new_version(root, "0.171.0")                           # the CLI is raised and writes its list
                models = focus.read_model_config()                      # the same version no longer: asks again
                self.assertEqual(_calls(root).count("codex"), 2)
                self.assertEqual(self._another_line(models), [])
                self.assertFalse(any("model list is from" in row for row in
                                     picture.section_text(models, 140, "balanced-deep/codex", TODAY)))
                for _again in range(3):                                 # and the reads after it are quiet again
                    models = focus.read_model_config()
                self.assertEqual(_calls(root).count("codex"), 2)
                self.assertEqual(self._another_line(models), [])

    def test_a_list_alone_of_another_version_asks_once_per_background_read_and_the_line_stands(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with _env(root, list_version="0.163.0"):
                for count in (1, 2, 3):
                    models = focus.read_model_config()
                    self.assertEqual(_calls(root).count("codex"), count)
                    self.assertEqual([(n["version"], n["list_version"]) for n in self._another_line(models)],
                                     [("0.170.0", "0.163.0")])
                self.assertFalse((root / "refreshed").exists())          # nothing asked codex for its list
                _new_version(root, "0.163.0")                           # the CLI is changed to the list's version
                self.assertEqual(self._another_line(focus.read_model_config()), [])
                self.assertEqual(_calls(root).count("codex"), 4)
                focus.read_model_config()
                self.assertEqual(_calls(root).count("codex"), 4)

    def _fail_the_first_version(self, root):
        script = root / "bin" / "codex"
        script.write_text(script.read_text().replace(
            "echo codex >>", f'[ -f "{root}/failed_once" ] || {{ touch "{root}/failed_once"; echo codex >> "{root / "calls"}"; exit 1; }}\necho codex >>', 1))

    def test_a_first_version_that_fails_is_asked_again_by_the_next_background_read(self):
        for list_version, line in (("0.170.0", []), ("0.163.0", [("0.170.0", "0.163.0")])):
            with self.subTest(list_version=list_version), TemporaryDirectory() as temp:
                root = Path(temp)
                focus._CLI_SEEN.clear()
                with _env(root, list_version=list_version):
                    self._fail_the_first_version(root)
                    models = focus.read_model_config()                  # --version fails: the version is not known
                    self.assertEqual(_calls(root).count("codex"), 1)
                    self.assertEqual(self._another_line(models), [])
                    focus.read_model_config(refresh=False)               # the input read starts nothing
                    self.assertEqual(_calls(root).count("codex"), 1)
                    models = focus.read_model_config()                  # the next background read asks again
                    self.assertEqual(_calls(root).count("codex"), 2)
                    self.assertEqual([(n["version"], n["list_version"]) for n in self._another_line(models)], line)
                    self.assertEqual(focus._CLI_SEEN["codex"][0], (0, 170, 0))
                    focus.read_model_config(refresh=False)
                    self.assertEqual(_calls(root).count("codex"), 2)
                    focus.read_model_config()
                    self.assertEqual(_calls(root).count("codex"), 2 if not line else 3)   # a line that stands asks once per read

    def test_a_min_version_line_goes_when_the_claude_cli_is_raised_and_the_next_read_after_asks_nothing(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            focus._CLI_SEEN.clear()
            with _env(root, slow_claude=True):
                models = focus.read_model_config()
                self.assertEqual([(n["harness"], n["kind"], n["version"]) for n in models["follow"]["cli"]],
                                 [("claude", "min-version", "2.1.0")])
                self.assertEqual(_calls(root).count("claude"), 1)
                script = root / "bin" / "claude"
                script.write_text(script.read_text().replace("2.1.0 (fake", "9.9.1 (fake"))
                before = len(_calls(root))
                self.assertEqual(focus.read_model_config(refresh=False)["follow"]["cli"][0]["version"], "2.1.0")
                self.assertEqual(len(_calls(root)), before)               # the input read: the remembered answer
                models = focus.read_model_config()                      # the next background read asks again
                self.assertEqual(_calls(root).count("claude"), 2)
                self.assertEqual(models["follow"]["cli"], [])
                for _again in range(3):
                    models = focus.read_model_config()
                focus.read_model_config(refresh=False)
                self.assertEqual(_calls(root).count("claude"), 2)
                self.assertEqual(models["follow"]["cli"], [])

    def test_a_cli_line_of_any_kind_makes_the_harness_be_asked_once_per_read_and_a_second_view_only_when_it_changed(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            focus._CLI_SEEN.clear()
            with _env(root, slow_claude=True):
                follow, views = focus.utility("model_follow"), []
                real = follow.fleet_view
                with mock.patch.object(follow, "fleet_view", side_effect=lambda *a, **k: views.append(1) or real(*a, **k)):
                    focus.read_model_config()
                    self.assertEqual(len(views), 1)                     # the asking read: no second view, the line was asked this read
                    focus.read_model_config()
                    self.assertEqual(len(views), 2)                     # the line stands and the answer is the same: one view
                    self.assertEqual(_calls(root).count("claude"), 2)
                    script = root / "bin" / "claude"
                    script.write_text(script.read_text().replace("2.1.0 (fake", "9.9.1 (fake"))
                    focus.read_model_config()
                    self.assertEqual(len(views), 4)                     # the answer changed: the view is worked out once more

    def test_the_input_read_starts_no_process_with_the_list_the_same_or_another(self):
        for list_version in ("0.170.0", "0.163.0"):
            with self.subTest(list_version=list_version), TemporaryDirectory() as temp:
                root = Path(temp)
                focus._CLI_SEEN.clear()
                with _env(root, list_version=list_version):
                    self.assertEqual(focus.read_model_config(refresh=False)["follow"]["cli"], [])   # never asked
                    self.assertEqual(_calls(root), [])
                    focus.read_model_config()
                    asked = _calls(root)
                    for _again in range(3):
                        focus.read_model_config(refresh=False)
                    self.assertEqual(_calls(root), asked)

    def test_a_list_that_names_no_version_uses_the_remembered_answer(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with _env(root, list_version="0.170.0"):
                focus.read_model_config()
                cache = root / "codex" / "models_cache.json"
                cache.write_text(cache.read_text().replace('"client_version":"0.170.0",', ""))
                _new_version(root, "0.171.0")
                focus.read_model_config()
                self.assertEqual(_calls(root).count("codex"), 1)

    def test_a_harness_the_background_read_never_asked_about_is_not_known_to_the_input_read(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with _env(root, slow_claude=True):
                models = focus.read_model_config(refresh=False)
                self.assertEqual(_calls(root), [])
                self.assertEqual(models["follow"]["cli"], [])


class AnOddAnswerCostsOnlyFollowing(unittest.TestCase):
    def _models_value(self, utility_for_follow):
        from tools.fleet.explorer.types import FocusKey, FocusRequest
        cat = SimpleNamespace(state="available", rows=({"id": "gpt-6-astra"}, {"id": "gpt-6.1-sol"}),
                              models=("gpt-6-astra", "gpt-6.1-sol"), source="/cache/models.json", observed_at=1,
                              variants={})
        real = focus.utility
        with mock.patch.object(focus, "utility", side_effect=lambda name: utility_for_follow if name == "model_follow"
                               else real(name)), \
                mock.patch("tools.fleet.explorer.model_catalog.read_catalogs",
                           return_value={"claude": cat, "codex": cat, "opencode": cat}):
            return focus.collect_aux(FocusRequest(FocusKey("*", "models"), 1, 0)).source_records["models"]

    def test_the_models_value_comes_whole_and_draws_as_it_did_before_following(self):
        class Broken:
            @staticmethod
            def fleet_view(**_kw):
                raise RuntimeError("no")
        with TemporaryDirectory() as temp:
            with _env(Path(temp)):
                reference = self._models_value(Broken)
                self.assertNotIn("follow", reference)
                self.assertTrue(reference["profile_map"])
                for odd in ODD_ANSWERS:
                    class Odd:
                        @staticmethod
                        def fleet_view(**_kw):
                            return copy.deepcopy(odd)
                    with self.subTest(odd=odd):
                        got = self._models_value(Odd)
                        self.assertFalse([cells for cells in (got.get("follow") or {}).get("cells", {}).values() if cells])
                        self.assertEqual({k: v for k, v in got.items() if k != "follow"}, reference)
                        for width in (100, 140):
                            self.assertEqual(picture.section_text(got, width, "deep/codex", TODAY),
                                             picture.section_text(reference, width, "deep/codex", TODAY))


class TheLinesDoNotRunIntoEachOther(unittest.TestCase):
    def _picker(self, harness, answer, source):
        models = picture.base_models()
        models["catalog_sources"] = {harness: {"state": "available", "source": source, "observed_at": 1}}
        focus.follow_into(picture.fit_table(models, answer), answer)
        return models

    def test_then_keep_is_at_the_end_of_the_follow_row_beside_a_list_with_its_notes(self):
        codex = [samples.item("codex", [samples.followed("profile/deep", "gpt-6-astra", "gpt-6.2-astra", "high")])]
        opencode = [samples.item("opencode", [samples.followed("profile/deep", "opencode-go/glm-5.2-flash",
                                                               "opencode-go/glm-5.3-flash", "max")])]
        for harness, answer in (("codex", codex), ("opencode", opencode)):
            models = self._picker(harness, answer, "/cache/models.json")
            if harness == "opencode":
                models["model_choices"]["opencode"] = [f"opencode-go/m{index}" for index in range(5)] \
                    + ["openai/a", "openai/b", "opencode-go/glm-5.3-flash"]
            box = _box_rows(models, 100, {"ui:fol": "undo"}, select=f"deep/{harness}")
            with self.subTest(harness=harness):
                rows = [row.strip("│ ").rstrip(" │┐┘") for row in box]
                self.assertEqual(len([row for row in rows if "then Keep" in row]), 1)
                self.assertTrue(next(row for row in rows if "then Keep" in row).startswith("follow   [ Follow ]   Keep"))
                note = next(row for row in rows if row.startswith(("from cache", "opencode-go 6")))
                self.assertNotIn("Keep", note)
                self.assertTrue(note.startswith("from cache, callabil") or note.startswith("opencode-go 6 · openai 2  · from cache"),
                                note)
                for row in rows:
                    for broken in ("Keepe", "Keepgo", "Keep,", "Keepfrom"):
                        self.assertNotIn(broken, row)
                self.assertEqual(len(box), len(_box_rows(models, 100, {}, select=f"deep/{harness}")) + 0)

    def test_the_words_about_lines_that_moved_with_the_follow(self):
        for moved, said in (((), "then Keep"), (("CFG_TIER_DEEP_FAILOVER_CASCADE",), "then Keep · linked line stays"),
                            (("CFG_TIER_DEEP_FAILOVER_CASCADE", "CFG_MAIN_SESSION_ONLY_MODELS"),
                             "then Keep · linked lines stay")):
            models = _through_a_tier(moved=moved)
            rows = [row.strip("│ ").rstrip(" │┐┘") for row in _box_rows(models, 140, {"ui:fol": "undo"})]
            follow_row = next(row for row in rows if row.startswith("follow "))
            self.assertTrue(follow_row.endswith(said), (follow_row, said))
        narrow = [row.strip("│ ").rstrip(" │┐┘") for row in _box_rows(
            _through_a_tier(moved=("CFG_TIER_DEEP_FAILOVER_CASCADE",)), 100, {"ui:fol": "undo"})]
        row = next(row for row in narrow if row.startswith("follow "))
        self.assertIn("then Keep", row)
        self.assertLessEqual(cell_width(row), 63)

    def test_lines_the_follow_moved_are_counted_from_the_answer(self):
        answer = [samples.item("codex", [
            samples.followed("tier/deep", "gpt-6.1-sol", "gpt-6.2-sol", "xhigh", moved=("CFG_TIER_DEEP_FAILOVER_CASCADE",)),
            samples.followed("tier/light", "gpt-6-luna", "gpt-6.1-luna", "medium", moved=("CFG_TIER_DEEP_FAILOVER_CASCADE",)),
            samples.followed("tier/top", "gpt-6-astra", "gpt-6.2-astra", "xhigh")])]
        shown = _pic(answer=answer, targets={"codex": {"deep": "tier/deep", "light": "tier/light", "top": "tier/top"}})
        self.assertEqual(shown[9], "│ ^ followed 11-22 · 3 cells + 1 linked line ▸")


class TheKeysTheCoreAdded(unittest.TestCase):
    def test_a_cell_no_update_can_follow_gets_nothing_about_following(self):
        answer = copy.deepcopy(ALL["followed"])
        answer[0]["cells"] = [samples.unfollowable("tier/balanced", "sonnet", "high", "not-in-list"),
                              samples.unfollowable("tier/light", "odd", "high", "no-pattern")]
        models = _models(answer=answer)
        self.assertEqual(models["follow"]["cells"]["claude"], [])
        self.assertEqual(picture.section_text(models, 140, "balanced/claude", TODAY)[9:],
                         picture.section_text(_models("followed"), 140, "balanced/claude", TODAY)[9:])
        # a cell the answer cannot tell about (no list: null) with nothing to show is not drawn either
        answer = [samples.item("codex", [dict(samples.cell("profile/deep", "gpt-6-astra", "high"), followable=None,
                                              reason="no-catalog")])]
        self.assertEqual(_pic(answer=answer), _pic(answer=[samples.item("codex")]))

    def test_an_answer_without_the_new_keys_or_with_values_it_does_not_know_still_draws(self):
        answer = copy.deepcopy(ALL["followed"])
        for entry in answer:
            entry.pop("read_only", None)
            for found in entry["cells"]:
                for key in ("followable", "reason"):
                    found.pop(key, None)
                if found["since"]:
                    found["since"] = {k: v for k, v in found["since"].items() if k not in ("effort_was", "lines")}
        self.assertEqual(_pic(answer=answer), _pic("followed"))
        odd = copy.deepcopy(ALL["followed"])
        odd[1]["read_only"] = "sure"
        odd[1]["cells"][0]["followable"] = "maybe"
        odd[1]["cells"][1]["since"].update(effort_was=3, lines="x")
        self.assertEqual(_pic(answer=odd), _pic("followed"))

    def test_a_table_that_is_read_only_does_not_promise_the_next_update(self):
        answer = copy.deepcopy(ALL["next"])
        answer[2]["read_only"] = True
        shown = _pic(answer=answer, targets=None)
        self.assertIn("│ > next opencode tier/deep · table is read-only · not updated ▸", shown)
        box = _box(_pic(answer=answer, select="deep/opencode", targets={"opencode": {"deep": "tier/deep"}}))
        self.assertTrue(any("table is read-only · not updated" in row for row in box))
        many = self._mixed()                          # codex has next cells; one more on the opencode table, read-only
        text = "\n".join(_pic(answer=many, width=200))
        self.assertIn("opencode table is read-only · not updated · on the next update or refresh", text)

    @staticmethod
    def _mixed():
        many = copy.deepcopy(ALL["mixed"])
        many[2]["cells"].append(samples.cell("tier/light", "opencode-go/muse-spark", "max",
                                             following=samples.upcoming("opencode-go/muse-spark-2", "max")))
        many[2]["read_only"] = True
        return many

    def test_at_every_width_a_read_only_row_says_so_and_never_promises(self):
        only = copy.deepcopy(ALL["next"])
        only[2]["read_only"] = True
        every = copy.deepcopy(ALL["next"])            # every table with a next cell is read-only
        every[1]["cells"][0]["next"] = samples.upcoming("gpt-6.2-astra", "xhigh")
        every[1]["read_only"] = every[2]["read_only"] = True
        for width in (80, 100, 140, 200):
            for name, answer in (("opencode only", only), ("both", every)):
                with self.subTest(width=width, answer=name):
                    summary = [row for row in _pic(answer=answer, width=width, select="", targets=None)
                               if row.startswith("│ > next")]
                    self.assertEqual(len(summary), 1)
                    self.assertIn("read-only", summary[0])
                    self.assertNotIn("on the next update", summary[0])
            with self.subTest(width=width, box="next"):
                box = _box(_pic(answer=only, width=width, select="deep/opencode",
                                targets={"opencode": {"deep": "tier/deep"}}))
                line = next(row for row in box if row.startswith("│  │  next"))
                self.assertIn("read-only", line)
                self.assertNotIn("on the next update", line)

    def test_a_mixed_row_puts_the_notice_before_the_promise_at_every_width(self):
        many = self._mixed()
        for width in (80, 100, 140, 200):
            with self.subTest(width=width):
                summary = next(row for row in _pic(answer=many, width=width, select="", targets=None)
                               if row.startswith("│ > next"))
                self.assertIn("read-only", summary)
                if "on the next update" in summary:
                    self.assertLess(summary.index("read-only"), summary.index("on the next update"))
        # a narrow row cuts the promise, then the head; the notice stays whole when it fits at all
        row = next(row for row in _pic(answer=many, width=140, select="", targets=None) if row.startswith("│ > next"))
        self.assertIn("opencode table is read-only · not updated · on the next update or re…", row)


class ACellTheListCannotTell(unittest.TestCase):
    """``followable`` null (no list to tell by): drawn only when there is something about following to show."""

    @staticmethod
    def _unknown(cells, harness="claude"):
        return [samples.item(name, [dict(cell, followable=None, reason="no-catalog") for cell in cells]
                             if name == harness else []) for name in ("claude", "codex", "opencode")]

    def test_with_nothing_to_show_the_picture_is_the_one_from_before(self):
        claude = [samples.cell(f"profile/{name}", model, effort)
                  for name, (model, effort) in picture._CLAUDE.items()]
        answer = self._unknown(claude)
        checked = 0
        for width in (80, 100, 140):
            for select in ("", *(f"{name}/claude" for name in picture._CLAUDE)):
                with self.subTest(width=width, select=select):
                    before = picture.section_text(picture.base_models(), width, select, TODAY)
                    self.assertEqual(_pic(answer=answer, width=width, select=select), before)
                    checked += 1
        self.assertEqual(checked, 21)

    def test_a_cell_with_a_record_keeps_its_mark_and_its_undo(self):
        answer = self._unknown([samples.followed("profile/deep", "gpt-6-astra", "gpt-6.2-astra", "high")], "codex")
        shown = _pic(answer=answer)
        self.assertIn("│ ^ followed 11-22 · codex profile/deep ▸", shown)
        self.assertTrue(any("[ undo ]" in row for row in shown))

    def test_a_cell_the_user_keeps_shows_the_follow_row_and_can_go_back_to_follow(self):
        answer = self._unknown([samples.kept("profile/deep", "gpt-6-astra", "high")], "codex")
        shown = _pic(answer=answer)
        self.assertTrue(any("follow     Follow   [ Keep ]" in row for row in shown))
        models = _models(answer=answer)
        vm, state, frame = _frame(models, 140, "deep/codex", folds={"ui:fol": "follow"})
        self.assertTrue(any(item.action[:3] == ("models.apply", "codex", "follow") for item in frame.hits))

    def test_a_next_update_or_a_held_model_is_drawn(self):
        nxt = self._unknown([samples.cell("profile/deep", "gpt-6-astra", "high",
                                          following=samples.upcoming("gpt-6.2-astra", "high"))], "codex")
        self.assertTrue(any("follow   [ Follow ]" in row for row in _pic(answer=nxt)))
        held = self._unknown([samples.held("profile/deep", "gpt-6-astra", "high", "gpt-6.2-astra")], "codex")
        self.assertTrue(any("rejected" in row for row in _pic(answer=held)))


class ARefusedWriteKeepsTheFollowDraft(unittest.TestCase):
    def test_a_table_with_a_hardlink_alias_refuses_and_the_draft_stays(self):
        from tools.fleet.explorer.runtime import ExplorerRuntime
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with _env(root):
                models = focus.read_model_config()
                vm, state, frame = _frame(models, 140, "deep/codex", folds={"ui:fol": "keep"})
                apply = next(h for h in frame.hits if h.action[:3] == ("models.apply", "codex", "follow")
                             and h.action[-1] is False)
                os.link(root / "codex/agent-config/models.conf", root / "alias.conf")
                state, effects = reduce(state, {"type": "click", "action": apply.action}, frame)
                result = ExplorerRuntime().apply_effects(effects, vm)
                self.assertNotIn(result["status"], ("changed", "unchanged"))
                self.assertEqual(finish_follow_draft(state, effects, result).fold_map().get("ui:fol"), "keep")


if __name__ == "__main__":
    unittest.main()
