#!/usr/bin/env python3
"""The end-of-line record of a table cell: one reader and one writer, and the state they give."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import model_follow_mark as K  # noqa: E402

D = "2026-11-21"
FOLLOW = K.follow_record(D, "gpt-6.1-sol", "gpt-6.2-sol")
FITTED = K.follow_record(D, "gpt-6.1-sol", "gpt-6.2-sol", "xhigh")
KEEP = K.keep_record(D)
HELD = K.held_record(D, "gpt-6.2-sol", "gpt-6.1-sol")


class RoundTripTests(unittest.TestCase):
    def test_every_kind_reads_back_exactly_what_was_written(self):
        for record in (FOLLOW, FITTED, KEEP, HELD):
            with self.subTest(kind=record["kind"], was=record["effort_was"]):
                self.assertEqual(K.parse_record(K.render(record)), record)

    def test_the_short_form_the_design_shows_is_read(self):
        found = K.parse_record("# auto-follow 2026-11-21: gpt-6.1-sol -> gpt-6.2-sol")
        self.assertEqual((found["kind"], found["from"], found["to"], found["effort_was"]),
                         ("follow", "gpt-6.1-sol", "gpt-6.2-sol", None))

    def test_the_text_of_each_kind(self):
        self.assertEqual(K.render(FOLLOW), "# auto-follow 2026-11-21: gpt-6.1-sol -> gpt-6.2-sol")
        self.assertEqual(K.render(FITTED), "# auto-follow 2026-11-21: gpt-6.1-sol -> gpt-6.2-sol (effort was xhigh)")
        self.assertEqual(K.render(KEEP), "# auto-follow keep 2026-11-21")
        self.assertEqual(K.render(HELD), "# auto-follow held 2026-11-21: gpt-6.2-sol rejected; back to gpt-6.1-sol")

    def test_a_form_this_module_does_not_know_or_a_broken_one_is_no_record(self):
        for text in ("", "# my note", "# auto-follow", "# auto-follow 2026-11-21", "# auto-follow keep",
                     "# auto-follow keep 2026-13-40", "# auto-follow 2026-02-30: a -> b",
                     "# auto-follow 2026-11-21: a => b", "# auto-follow 2026-11-21: a ->",
                     "# auto-follow held 2026-11-21: a rejected", "# auto-follow held 2026-11-21: a rejected; back to",
                     "# auto-follow 2026-11-21: a -> b and then some words",
                     "# auto-follow 2026-11-21: a -> b (effort xhigh)", "# auto-follower 2026-11-21: a -> b",
                     "# auto-follow nope 2026-11-21", "# auto-follow keep 2026-11-21 forever"):
            with self.subTest(text=text):
                self.assertIsNone(K.parse_record(text))

    def test_a_record_followed_by_another_comment_is_still_one_record(self):
        self.assertEqual(K.parse_record("# auto-follow keep 2026-11-21 # my own words"), KEEP)
        self.assertEqual(K.parse_record("# mine # auto-follow keep 2026-11-21"), KEEP)


class WriteTests(unittest.TestCase):
    def test_the_user_comment_stays_and_the_line_carries_one_record(self):
        mine = "# my choice, do not touch"
        once = K.with_record(mine, FOLLOW)
        self.assertEqual(once, mine + " " + K.render(FOLLOW))
        twice = K.with_record(once, HELD)
        self.assertEqual(twice, mine + " " + K.render(HELD))
        self.assertEqual(twice.count("auto-follow"), 1)
        self.assertEqual(K.with_record(twice, None), mine)
        self.assertEqual(K.with_record("", KEEP), K.render(KEEP))
        self.assertEqual(K.with_record(K.render(KEEP), None), "")

    def test_a_broken_record_is_the_users_text_and_is_left_alone(self):
        broken = "# auto-follow 2026-13-40: a -> b"
        self.assertEqual(K.with_record(broken, KEEP), broken + " " + K.render(KEEP))
        self.assertEqual(K.parse_record(K.with_record(broken, KEEP)), KEEP)

    def test_a_comment_after_the_record_survives_a_new_record(self):
        text = "# auto-follow keep 2026-11-21 # later words"
        self.assertEqual(K.with_record(text, FOLLOW), "# later words " + K.render(FOLLOW))

    def test_comment_split_follows_the_quotes_of_the_table_parser(self):
        self.assertEqual(K.split_comment("'a # b' # note"), ("'a # b'", "# note"))
        self.assertEqual(K.split_comment('"a b"   # note'), ('"a b"', "# note"))
        self.assertEqual(K.split_comment("plain"), ("plain", ""))


class ReadMarksTests(unittest.TestCase):
    def test_marks_are_read_per_key_from_the_text(self):
        text = ("# header # auto-follow keep 2026-11-21\n"
                "CFG_TIER_DEEP_MODEL='gpt-6.2-sol' # auto-follow 2026-11-21: gpt-6.1-sol -> gpt-6.2-sol\n"
                "CFG_TIER_LIGHT_MODEL=gpt-6-luna # just a note\n"
                "  CFG_TIER_TOP_MODEL=gpt-6-astra   # auto-follow keep 2026-11-21 # mine\n"
                "CFG_TIER_MINI_MODEL=x # auto-follow keep 2026-99-99\n"
                "not an assignment # auto-follow keep 2026-11-21\n")
        found = K.read_marks(text)
        self.assertEqual(sorted(found), ["CFG_TIER_DEEP_MODEL", "CFG_TIER_TOP_MODEL"])
        self.assertEqual(found["CFG_TIER_DEEP_MODEL"], FOLLOW)
        self.assertEqual(found["CFG_TIER_TOP_MODEL"], KEEP)


class StateTests(unittest.TestCase):
    """One row of the state table each: the value now, the record, the state."""

    def state(self, model, record):
        found = K.cell_state(model, record)
        return found["state"], found["why"]

    def test_no_record_follows_whatever_the_value(self):
        self.assertEqual(self.state("anything", None), ("follow", "default"))

    def test_a_follow_record_follows_when_the_value_is_where_it_went(self):
        self.assertEqual(self.state("gpt-6.2-sol", FOLLOW), ("follow", "followed"))
        self.assertEqual(K.cell_state("gpt-6.2-sol", FOLLOW)["date"], D)

    def test_going_back_to_the_earlier_value_holds_the_cell(self):
        self.assertEqual(self.state("gpt-6.1-sol", FOLLOW), ("keep", "reverted"))

    def test_choosing_another_model_follows_and_the_record_is_old(self):
        self.assertEqual(self.state("gpt-9", FOLLOW), ("follow", "changed"))

    def test_keep_holds_whatever_the_value(self):
        for model in ("gpt-6.1-sol", "gpt-9"):
            self.assertEqual(self.state(model, KEEP), ("keep", "kept"))

    def test_held_is_held_at_the_earlier_value_and_follows_at_any_other(self):
        held = K.cell_state("gpt-6.1-sol", HELD)
        self.assertEqual((held["state"], held["why"], held["skip"]), ("held", "held", "gpt-6.2-sol"))
        self.assertEqual(self.state("gpt-9", HELD), ("follow", "changed"))


if __name__ == "__main__":
    unittest.main()
