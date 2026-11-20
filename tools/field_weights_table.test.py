#!/usr/bin/env python3
"""Tests for the field-weights recompute tool, on invented numbers only."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

TOOL = Path(__file__).resolve().with_name("field-weights-table.py")
spec = importlib.util.spec_from_file_location("field_weights_table", TOOL)
fw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fw)


def acc(*values):
    return [{"kind": "accuracy", "value": value} for value in values]


def two_fields():
    return {"fields": {
        "code": {"depths": {"balanced": {"claude": acc(0.8), "codex": acc(0.4)}}},
        "docs": {"depths": {"balanced": {"claude": acc(0.5), "codex": acc(0.5)}}},
    }}


class Rule(unittest.TestCase):
    def test_relative_scores_for_accuracy_and_elo(self):
        self.assertEqual(fw.relative({"a": 0.8, "b": 0.4}, "accuracy"), {"a": 1.0, "b": 0.5})
        elo = fw.relative({"a": 1500, "b": 1400}, "elo")
        self.assertEqual(elo["a"], 1.0)
        self.assertAlmostEqual(elo["b"], 0.72, places=2)               # 100 lower is 0.72
        self.assertEqual(fw.relative({"a": 0.8, "b": None}, "accuracy"), {})   # one value compares to nothing

    def test_general_level_is_removed_then_the_evidence_strength_shrinks_the_gap(self):
        weights, detail = fw.compute(two_fields())
        # code R = (1, .5), docs R = (1, 1): codex's general level is sqrt(.5), claude's 1
        self.assertAlmostEqual(detail["general"]["codex"], 0.5 ** 0.5)
        self.assertEqual(weights["code"], {"claude": 1.0, "codex": 0.7})         # k 1.0: 1 − .2929 → 0.7
        self.assertEqual(weights["docs"], {"claude": 0.95, "codex": 1.0})        # k .25: 1 − .0732 → 0.95
        self.assertEqual(list(weights), ["code", "docs"])

    def test_a_cell_never_goes_below_the_floor(self):
        data = {"fields": {"code": {"depths": {"balanced": {"claude": acc(0.9), "codex": acc(0.1)}}},
                           "docs": {"depths": {"balanced": {"claude": acc(0.5), "codex": acc(0.5)}}}}}
        self.assertEqual(fw.compute(data)[0]["code"]["codex"], 0.6)

    def test_depths_mix_by_their_share_and_a_lone_harness_is_not_compared(self):
        depths = {"deep": {"claude": acc(0.8), "codex": acc(0.4)},
                  "mini": {"claude": acc(0.4), "codex": acc(0.8)},
                  "light": {"claude": acc(0.9)}}
        scores = fw.field_scores(depths, fw.DEPTH_MIX)
        self.assertEqual(scores["claude"], 1.0)
        self.assertAlmostEqual(scores["codex"], ((0.25 * 0.5 + 0.02 * 1) / 0.27) / ((0.25 + 0.02 * 0.5) / 0.27))

    def test_a_missing_measurement_leaves_that_harness_out_of_that_comparison(self):
        scores = fw.depth_scores({"claude": acc(0.8, 0.6), "codex": acc(0.4, None), "opencode": acc(0.8, 0.3)})
        self.assertAlmostEqual(scores["codex"], 0.5)                             # only the first measurement
        self.assertAlmostEqual(scores["claude"], 1.0)
        self.assertAlmostEqual(scores["opencode"], (1.0 + 0.5) / 2)

    def test_a_field_without_data_has_no_row_and_strength_can_be_given(self):
        data = two_fields()
        data["fields"]["plan"] = {"depths": {"balanced": {"claude": acc(None), "codex": acc(None)}}}
        data["fields"]["code"]["strength"] = 0.0
        weights = fw.compute(data)[0]
        self.assertNotIn("plan", weights)
        self.assertEqual(weights["code"], {"claude": 1.0, "codex": 1.0})

    def test_bad_input_is_named(self):
        for data, text in (({}, "`fields`"),
                           ({"fields": {"x": {"depths": {"balanced": {"claude": [{"kind": "stars", "value": 1}]}}}}}, "kind"),
                           ({"fields": {"x": {"depths": {"balanced": {"claude": [{"kind": "elo", "value": "9"}]}}}}}, "kind"),
                           ({"fields": {"x": {}}}, "`depths`")):
            with self.assertRaises(fw.InputError) as caught:
                fw.validate(data)
            self.assertIn(text, str(caught.exception))
        with self.assertRaisesRegex(fw.InputError, "unknown depth"):
            fw.compute({"fields": {"code": {"depths": {"huge": {"claude": acc(1), "codex": acc(1)}}}}})
        with self.assertRaisesRegex(fw.InputError, "no default strength"):
            fw.compute({"fields": {"made-up": {"depths": {"balanced": {"claude": acc(1), "codex": acc(0.5)}}}}})
        mixed = {"claude": [{"kind": "elo", "value": 1}], "codex": acc(1)}
        with self.assertRaisesRegex(fw.InputError, "different kinds"):
            fw.depth_scores(mixed)

    def test_command_prints_the_weights_block_and_changes_nothing_else(self):
        with tempfile.TemporaryDirectory(prefix="fw-table-") as tmp:
            path = Path(tmp) / "input.json"
            path.write_text(json.dumps(two_fields()), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(fw.main([str(path), "--explain"]), 0)
            self.assertEqual(json.loads(out.getvalue()), {"weights": {"code": {"claude": 1.0, "codex": 0.7},
                                                                      "docs": {"claude": 0.95, "codex": 1.0}}})
            self.assertIn("general level", err.getvalue())
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["input.json"])
            path.write_text("[", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()) as bad:
                self.assertEqual(fw.main([str(path)]), 2)
            self.assertIn("field-weights-table:", bad.getvalue())


if __name__ == "__main__":
    unittest.main()
