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
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.style import normalize_style
from tools.fleet.explorer.view_model import ExplorerViewModel
from tools.fleet.tests import models_follow_picture as picture
from tools.fleet.tests import models_follow_samples as samples

GOLDEN = Path(__file__).parent / "fixtures" / "explorer" / "models_follow"
TODAY = samples.TODAY
ALL = samples.samples()


def _models(name=None, answer=None, unlisted=(), targets=None):
    answer = ALL[name] if answer is None and name else answer
    models = picture.base_models()
    if targets:
        models["profile_target"] = targets
    if answer is not None:
        focus.follow_into(picture.fit_table(models, answer), answer, unlisted)
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
        # a Claude alias the model list does not hold: the answer's cell is left out
        answer = copy.deepcopy(ALL["quiet"])
        answer[0]["cells"] = [samples.cell("tier/balanced", "sonnet", "high")]
        models = _models(answer=answer, unlisted={("claude", "sonnet")})
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
            self.assertEqual(focus._read_follow({}), (None, ()))
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
        self.assertTrue(rows[4].startswith("│ codex CLI 0.163.0 lists gpt-6.1-sol, gpt-6-astra as previous generation"))
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

    def test_todays_machine_two_lines_no_mark_no_legend(self):
        shown = _pic("today", 140, "balanced/claude")
        self.assertEqual(shown[9], "│ > next opencode tier/deep · on the next update or refresh ▸")
        self.assertTrue(shown[10].startswith("│ codex CLI 0.163.0 lists gpt-6.1-sol, gpt-6-astra as previous generation"))
        self.assertFalse(any(glyph in "".join(shown[1:8]) for glyph in "^=!"))
        self.assertNotIn("Followed", "\n".join(shown))

    def test_a_cli_note_that_names_a_newer_version_is_told_in_the_core_words(self):
        view = follow.Follow({"cells": {}, "cli": [{"harness": "claude", "version": "2.1.0", "kind": "min-version",
                                                     "models": [["a", "2.2"], ["b", "2.3"], ["c", "2.4"], ["d", "2.5"]],
                                                     "command": "claude update"}]}, {}, None)
        self.assertEqual(view.cli_lines(), ["claude CLI 2.1.0 is older than a (needs 2.2), b (needs 2.3), "
                                            "c (needs 2.4) and 1 more · claude update"])

    def test_every_row_stays_inside_the_pane(self):
        opened = {f"ui:fopen:{g}": "open" for g in follow.GROUPS}
        for name in ALL:
            for width in (100, 140):
                for select in ("deep/codex", "balanced/claude", "light/codex"):
                    with self.subTest(name=name, width=width, select=select):
                        for row in picture.section_text(_models(name), width, select, TODAY, opened):
                            self.assertLessEqual(cell_width(row[2:]), width - 35, row)


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
        self.assertEqual(frame.text_rows()[follow_row + 1].strip(" │"), "then Keep")
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
        self.assertIn("$ hearting model set codex tier/deep gpt-6.1-sol@xhigh",
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
        self.assertEqual((effects, "ui:fol" in after.fold_map()), ((app,), False))


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
                mock.patch.object(focus, "read_model_config", side_effect=lambda: reads.append(1) or fresh), \
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
        self.assertEqual(len(reads), 3)                      # one read after each apply; a refused effect reads nothing

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
                                               samples.cell("tier/light", "gpt-5", "high")]),
                        samples.item("claude", [samples.cell("tier/deep", "opus", "xhigh")])]

        with mock.patch.object(focus, "utility", return_value=Utility):
            answer, unlisted = focus._read_follow(catalogs)
        self.assertEqual(len(asked), 1)
        self.assertEqual(set(asked[0]), {"read_catalog"})
        self.assertEqual(set(unlisted), {("codex", "gpt-5"), ("claude", "opus")})
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


if __name__ == "__main__":
    unittest.main()
