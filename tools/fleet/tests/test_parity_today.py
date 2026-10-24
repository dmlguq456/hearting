"""Hermetic coverage for today's parity capture primitives."""
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from tools.fleet.tests.parity_compare import diff_frames
from tools.fleet.tests.parity_today import _frame_from_reference, _jsonable, classify


class TodayParityPlumbingTests(unittest.TestCase):
    def test_reference_capture_becomes_comparable_frame(self):
        row = {"cells": [["a", "b"]], "hits": [[0, 0, 1, ["view", "OVERVIEW"]]]}
        frame = _frame_from_reference(row)
        self.assertEqual((frame.cols, frame.rows), (2, 1))
        self.assertEqual(diff_frames(frame, frame), [])

    def test_jsonable_keeps_dataclass_fields_and_drops_unknown_objects(self):
        @dataclass
        class Value:
            count: int
            hidden: object
        value = Value(3, object())
        self.assertEqual(_jsonable(value), {"count": 3, "hidden": None})


def _record(width=140, rows=45, cells=(), hits=None):
    blank = " " * width
    reference, fleet2 = [blank] * rows, [blank] * rows
    differences = [{"kind": "cell", "row": row, "column": column, "expected": [a, ka, None], "actual": [b, kb, None]}
                   for row, column, a, ka, b, kb in cells]
    if hits is not None:
        differences.append({"kind": "hits", "expected": hits[0], "actual": hits[1]})
    for row, column, a, _ka, b, _kb in cells:
        reference[row] = reference[row][:column] + a + reference[row][column + 1:]
        fleet2[row] = fleet2[row][:column] + b + fleet2[row][column + 1:]
    return {"state": "OVERVIEW", "width": width, "rows": rows, "reference_rows": reference, "fleet2_rows": fleet2,
            "reference_edge": [False] * rows, "differences": differences}


class ClassificationTests(unittest.TestCase):
    def test_narrow_reference_defects_precede_content_rules_but_clean_edges_keep_real_defects(self):
        answered = _record(width=80, cells=[(10, 40, "x", "dim", "y", "dim")])
        answered["state"] = "QUESTIONS"
        answered["reference_rows"][10] = " " * 33 + "ANSWERED TODAY 20" + " " * 30
        answered["fleet2_rows"][10] = " " * 33 + "ANSWERED TODAY 20" + " " * 30
        answered["reference_edge"][10] = True

        context = _record(width=100, cells=[(10, 40, "x", "dim", "y", "dim")])
        context["state"] = "session"
        context["reference_rows"][10] = " " * 33 + "context: unknown · last clear ·" + " " * 40
        context["fleet2_rows"][10] = " " * 33 + "context: unknown" + " " * 60
        context["reference_edge"][10] = True

        clean_context = _record(width=100, cells=[(10, 40, "x", "dim", "y", "dim")])
        clean_context["state"] = "session"
        clean_context["reference_rows"][10] = " " * 33 + "context: unknown · last clear ·"
        clean_context["fleet2_rows"][10] = " " * 33 + "context: unknown"

        docs = [classify([record]) for record in (answered, context, clean_context)]
        self.assertEqual(docs[0]["entries"][0]["class"], "excluded")
        self.assertEqual(docs[0]["entries"][0]["rule"], "question-header-overflow")
        self.assertEqual(docs[1]["entries"][0]["class"], "excluded")
        self.assertIn(docs[1]["entries"][0]["rule"], {"narrow-header-overflow", "routes-narrow"})
        self.assertEqual((docs[2]["entries"][0]["class"], docs[2]["entries"][0]["rule"]),
                         ("fleet2-defect", "session-context-line"))

    def test_a_known_style_difference_gets_a_reason_and_an_owner(self):
        document = classify([_record(cells=[(1, 21, "━", "g_blocked", "━", "grp_live")])])
        self.assertEqual(document["summary"]["unexplained"], 0)
        entry = document["entries"][0]
        self.assertEqual((entry["rule"], entry["class"]), ("usage-gauge-80", "fleet2-defect"))
        self.assertTrue(entry["owner"].endswith("chrome.py"))

    def test_a_difference_no_rule_explains_stays_unexplained(self):
        document = classify([_record(cells=[(20, 50, "a", "dim", "b", "dim")])])
        self.assertEqual(document["summary"]["unexplained"], 1)

    def test_a_hit_difference_on_a_row_whose_cells_are_equal_is_not_derived(self):
        hit = [30, 34, 100, ["scope", "x"]]
        document = classify([_record(hits=([hit], []))])
        self.assertEqual([entry["class"] for entry in document["entries"]], ["unexplained"])

    def test_a_hit_difference_on_an_explained_row_follows_its_cells(self):
        hit = [1, 34, 100, ["usage"]]
        document = classify([_record(cells=[(1, 21, "━", "g_blocked", "━", "grp_live")], hits=([hit], []))])
        self.assertEqual(sorted(entry["class"] for entry in document["entries"]), ["derived", "fleet2-defect"])

    def test_reference_hits_beyond_the_screen_are_the_recorded_narrow_width_defect(self):
        hit = [6, 71, 88, ["ui", "sel", "top/codex"]]
        document = classify([_record(width=80, hits=([hit], []))])
        self.assertEqual((document["entries"][0]["class"], document["entries"][0]["rule"]), ("excluded", "hit-outside-screen"))


class InjectionPieces(unittest.TestCase):
    def test_a_state_outside_the_five_the_copy_knows_draws_as_an_idle_dot(self):
        from tools.fleet.tests.reference_injection import _session_row
        row = _session_row({"tag": "aa", "harness": "claude", "state": "stale", "context": None, "window": None})
        self.assertEqual((row["st"], row["ctx"], row["win"]), ("idle", None, 1.0))

    def test_a_route_row_keeps_the_captures_own_words(self):
        from tools.fleet.tests.reference_injection import _route_record
        record = _route_record({"id": "rt-1", "state": "working", "harness": "codex", "legs": [["code", "active", "2/6"]],
                                "stages": [{"name": "plan", "st": "done"}], "activity": "running", "tree_label": "code"})
        self.assertEqual((record["act"], record["tree_label"], record["legs"]), ("running", "code", [("code", "active", "2/6")]))

    def test_the_status_words_follow_the_capture(self):
        from types import SimpleNamespace
        from tools.fleet.tests.reference_injection import _status
        renderer = SimpleNamespace(mock5=SimpleNamespace(SEATS=[("aa", "r", 3, "·", False)]))
        vm = {"sessions": [{"steward": True}], "routes": [{"state": "dead"}, {"state": "working"}], "questions": [],
              "compute": [], "system": {"peer_delivery": {}, "install": {"version": "v9"}}}
        status = _status(renderer, vm, 0)
        self.assertEqual(status["OVERVIEW"], "1 session · 2 routes · 0 questions")
        self.assertEqual(status["ROUTES"], "2 open · 1 stopped leg")
        self.assertEqual(status["SYSTEM"], "v9 · · deliveries pending")


if __name__ == "__main__":
    unittest.main()
