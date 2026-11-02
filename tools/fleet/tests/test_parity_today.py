"""Hermetic coverage for today's parity capture primitives."""
import copy
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from tools.fleet.tests.parity_compare import diff_frames
from tools.fleet.tests.parity_today import (REFERENCE_MASK_VERSION, R14_GENERIC_OVERFLOW_CASES,
                                            R14_HEADER_IDENTITY_PRD_SHA256, R14_REFERENCE_PROFILE,
                                            R14_RESIDUAL_CELL_PAIRS, _frame_from_reference, _jsonable,
                                            _unique_reference_copy_limitations, _validated_header_hosts,
                                            _validated_header_identity,
                                            _wide_continuation_pair_is_complete, build_live_vm, classify, main)


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

    def test_header_oracle_reads_the_captured_validated_fleet_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            capture = Path(temp)
            source = capture / "fleet-json.json"
            source.write_text(json.dumps({"hearting": {
                "version": "v3.50.3-20-gad247e2b6", "install_method": "managed/stable"}}),
                encoding="utf-8")
            self.assertEqual(_validated_header_identity(capture), {
                "version": "v3.50.3-20-gad247e2b6", "install_method": "managed/stable"})
            source.write_text(json.dumps({"compute_hosts": {"hosts": [
                {"host": "gpu-a01", "self": True}, {"host": "cnn", "self": False}]}}),
                encoding="utf-8")
            self.assertEqual(_validated_header_hosts(capture), [
                {"mark": "⌂", "name": "gpu-a01"}, {"mark": "◇", "name": "cnn"}])
            source.write_text("{}", encoding="utf-8")
            self.assertEqual(_validated_header_identity(capture), {
                "version": "unknown", "install_method": "unmanaged"})
            self.assertEqual(_validated_header_hosts(capture), [])

    def test_cli_returns_failure_for_unexplained_strict_residue(self):
        residue = _record(cells=[(20, 50, "a", "dim", "b", "dim")])
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch("tools.fleet.tests.parity_today.run_capture", return_value=(Path(temp), [residue])), \
                mock.patch("tools.fleet.tests.parity_today.write_index"), \
                mock.patch("builtins.print"):
            result = main(["--out", temp, "--reference", temp])

        self.assertEqual(result, 1)


def _record(width=140, rows=45, cells=(), hits=None):
    blank = " " * width
    reference, fleet2 = [blank] * rows, [blank] * rows
    reference_cells = [[(" ", None, None) for _ in range(width)] for _ in range(rows)]
    fleet2_cells = [[(" ", None, None) for _ in range(width)] for _ in range(rows)]
    differences = [{"kind": "cell", "row": row, "column": column, "expected": [a, ka, None], "actual": [b, kb, None]}
                   for row, column, a, ka, b, kb in cells]
    if hits is not None:
        differences.append({"kind": "hits", "expected": hits[0], "actual": hits[1]})
    for row, column, a, _ka, b, _kb in cells:
        reference[row] = reference[row][:column] + a + reference[row][column + 1:]
        fleet2[row] = fleet2[row][:column] + b + fleet2[row][column + 1:]
        reference_cells[row][column] = (a, _ka, None)
        fleet2_cells[row][column] = (b, _kb, None)
    return {"state": "OVERVIEW", "width": width, "rows": rows, "reference_rows": reference, "fleet2_rows": fleet2,
            "reference_edge": [False] * rows, "reference_cells": reference_cells, "fleet2_cells": fleet2_cells,
            "differences": differences, "difference_count": len(differences)}


def _rows_record(width, state, rows, *, start=0, hits=None, profile=None, fleet_style_overrides=None):
    height = 45
    reference_cells = [[(" ", None, None) for _ in range(width)] for _ in range(height)]
    fleet2_cells = [[(" ", None, None) for _ in range(width)] for _ in range(height)]
    fleet_style_overrides = fleet_style_overrides or {}
    for row, (reference, fleet2) in rows.items():
        for offset, char in enumerate(reference):
            column = start + offset
            if column < width:
                style = "spin" if char in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⠯⠷⠻" else "dim"
                reference_cells[row][column] = (char, style, None)
        for offset, char in enumerate(fleet2):
            column = start + offset
            if column < width:
                style = "spin" if char in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⠯⠷⠻" else "dim"
                style = fleet_style_overrides.get((row, column), style)
                fleet2_cells[row][column] = (char, style, None)
    reference_rows = ["".join(cell[0] for cell in row) for row in reference_cells]
    fleet2_rows = ["".join(cell[0] for cell in row) for row in fleet2_cells]
    differences = []
    for row in range(height):
        for column in range(width):
            expected, actual = reference_cells[row][column], fleet2_cells[row][column]
            if expected != actual:
                differences.append({"kind": "cell", "row": row, "column": column,
                                    "expected": list(expected), "actual": list(actual)})
    if hits is not None:
        differences.append({"kind": "hits", "expected": hits[0], "actual": hits[1]})
    record = {"state": state, "width": width, "rows": height, "reference_rows": reference_rows,
              "fleet2_rows": fleet2_rows, "reference_cells": reference_cells, "fleet2_cells": fleet2_cells,
              "reference_edge": [False] * height, "differences": differences}
    if profile:
        record["reference_profile"] = profile
        record["reference_overflow_mask"] = {"version": "r14-v87-explicit-cell-mask-v1", "profile": profile, "cells": []}
    return record


def _r14_narrow_header_record(working=0, idle=0, blocked=0, version="v3.50.3-20-gad247e2b6", clock="02:38"):
    host = "⌂ gpu-a01 +4"
    reference_census = (f" {working} working  ● {idle} idle  ● {blocked} answer needed   hearting "
                        f"{version}  {clock}")
    reference = reference_census.ljust(79) + "⠏"
    census = (f"⠏ {working} ● {idle} ● {blocked} hearting {version} · unmanaged  {clock}")
    gap = 80 - len(host) - len(census)
    if gap < 0:
        raise ValueError("fixture header exceeds 80 cells")
    candidate = host + " " * gap + census
    record = _rows_record(80, "OVERVIEW", {0: (reference, candidate)}, profile=R14_REFERENCE_PROFILE)
    record["validated_header_identity"] = {"version": version, "install_method": "unmanaged"}
    record["validated_header_hosts"] = [
        {"mark": "⌂", "name": "gpu-a01"}, {"mark": "◇", "name": "cnn"},
        {"mark": "◇", "name": "gpu-b1"}, {"mark": "◇", "name": "gpu-c1"},
        {"mark": "◇", "name": "mpwav"},
    ]
    record["reference_edge"][0] = True
    record["reference_overflow_mask"]["version"] = REFERENCE_MASK_VERSION
    record["reference_overflow_mask"]["profile"] = R14_REFERENCE_PROFILE

    def style_text(cells, start, text, style):
        for offset, glyph in enumerate(text):
            column = start + offset
            cells[0][column] = (glyph, style, None)

    candidate_cells = record["fleet2_cells"]
    style_text(candidate_cells, 0, "⌂ ", "g_work")
    style_text(candidate_cells, 2, "gpu-a01", "g_work")
    style_text(candidate_cells, 9, " +4", "dim")
    position = 80 - len(census)
    for column in range(len(host), position):
        candidate_cells[0][column] = (" ", None, None)
    for text, style in (("⠏", "g_spin"), (f" {working}", "g_spin"), (" ", None),
                        ("●", "g_work_off"), (f" {idle}", "g_work_off"), (" ", None),
                        ("●", "g_blocked"), (f" {blocked}", "g_blocked"), (" ", None),
                        ("hearting", "hearting_name"), (" " + version, "version_release"),
                        (" · ", "dim"),
                        ("unmanaged", "version_method"), (f"  {clock}", "dim")):
        style_text(candidate_cells, position, text, style)
        position += len(text)
    record["fleet2_rows"][0] = "".join(cell[0] for cell in candidate_cells[0])
    record["differences"] = []
    for column, (expected, actual) in enumerate(zip(record["reference_cells"][0], candidate_cells[0])):
        if expected != actual:
            record["differences"].append({"kind": "cell", "row": 0, "column": column,
                                          "expected": list(expected), "actual": list(actual)})
    return record


def _r14_wide_header_record(working=0, idle=0, blocked=0, version="v3.50.3-20-gad247e2b6",
                            clock="02:38", method="unmanaged"):
    width = 168
    hosts = [{"mark": "⌂", "name": "gpu-a01"}, {"mark": "◇", "name": "cnn"},
             {"mark": "◇", "name": "gpu-b1"}, {"mark": "◇", "name": "gpu-c1"},
             {"mark": "◇", "name": "mpwav"}]
    host = " hosts ⌂ gpu-a01  ◇ cnn  ◇ gpu-b1  ◇ gpu-c1  ◇ mpwav 5/5"
    old_census = (f"⠏ {working} working  ● {idle} idle  ● {blocked} answer needed   hearting "
                  f"{version}  {clock}")
    new_census = (f"⠏ {working} working  ● {idle} idle  ● {blocked} answer needed   hearting "
                  f"{version} · {method}  {clock}")
    old_start, new_start = width - 1 - len(old_census), width - 1 - len(new_census)
    reference = host + " " * (old_start - len(host)) + old_census + " "
    candidate = host + " " * (new_start - len(host)) + new_census + " "
    record = _rows_record(width, "OVERVIEW", {0: (reference, candidate)}, profile=R14_REFERENCE_PROFILE)
    record["validated_header_identity"] = {"version": version, "install_method": method}
    record["validated_header_hosts"] = hosts
    record["reference_overflow_mask"]["version"] = REFERENCE_MASK_VERSION

    host_parts = [(" hosts ", "dim"), ("⌂ ", "g_work"), ("gpu-a01", "g_work"), ("  ", "branch_s"),
                  ("◇ ", "dim"), ("cnn", "loc_repo"), ("  ", "branch_s"),
                  ("◇ ", "dim"), ("gpu-b1", "loc_repo"), ("  ", "branch_s"),
                  ("◇ ", "dim"), ("gpu-c1", "loc_repo"), ("  ", "branch_s"),
                  ("◇ ", "dim"), ("mpwav", "loc_repo"), (" 5/5", "dim")]
    base_census = [(f"⠏ {working}", "g_spin"), (" working", "dim"),
                   (f"  ● {idle}", "g_work_off"), (" idle", "dim"),
                   (f"  ● {blocked}", "g_blocked"), (" answer needed", "dim"),
                   ("   ", "branch_s"), ("hearting", "hearting_name"),
                   (" " + version, "version_release")]
    reference_census = base_census + [("  " + clock, "dim")]
    candidate_census = base_census + [(" · ", "dim"), (method, "version_method"),
                                      ("  " + clock, "dim")]

    def style_parts(cells, start, parts):
        for text, style in parts:
            for glyph in text:
                cells[0][start] = (glyph, style, None)
                start += 1
        return start

    style_parts(record["reference_cells"], 0, host_parts)
    style_parts(record["fleet2_cells"], 0, host_parts)
    for column in range(len(host), old_start):
        record["reference_cells"][0][column] = (" ", None, None)
    for column in range(len(host), new_start):
        record["fleet2_cells"][0][column] = (" ", None, None)
    style_parts(record["reference_cells"], old_start, reference_census)
    style_parts(record["fleet2_cells"], new_start, candidate_census)
    record["differences"] = []
    for column, (expected, actual) in enumerate(zip(record["reference_cells"][0], record["fleet2_cells"][0])):
        if expected != actual:
            record["differences"].append({"kind": "cell", "row": 0, "column": column,
                                          "expected": list(expected), "actual": list(actual)})
    return record


def _r14_residual_record(state, width, row, *, hits=None):
    key = (state, width, row)
    pairs = R14_RESIDUAL_CELL_PAIRS[key]
    text = {
        ("OVERVIEW", 80, 4): ("      ID       HARNESS         TCTXE       AGE",
                               "      ID       HARNESS         CTX         AGE"),
        ("MODELS", 80, 22): ("  └─ models.conf → .bak backup · new sessions a",
                             "  └─ models.conf → .bak backup · new session ┘"),
        ("MODELS", 80, 26): ("    weights:         codex ▮▮▮▮▮▮▮▮▮▮ 1.0    op",
                             "    weights:         codex ▮▮▮▮▮▮▮▮▮▮ 1.0    o"),
        ("MODELS", 80, 32): (" ── CONTEXT & TIDY  input tokens · new sessions",
                             " ── CONTEXT & TIDY  input tokens · new sessio"),
        ("MODELS", 100, 22): ("  └─ models.conf → .bak backup · new sessions and launches only · s",
                              "  └─ models.conf → .bak backup · new sessions and launches only  ┘"),
        ("MODELS", 100, 26): ("    weights:         codex ▮▮▮▮▮▮▮▮▮▮ 1.0    opencode ▮▮▮▮ 0.4    c",
                              "    weights:         codex ▮▮▮▮▮▮▮▮▮▮ 1.0    opencode ▮▮▮▮ 0.4"),
    }[key]
    record = _record(width=width)
    record["state"] = state
    reference_text = (" " * 33 + text[0]).ljust(width)[:width]
    fleet2_text = (" " * 33 + text[1]).ljust(width)[:width]
    record["reference_rows"][row] = reference_text
    record["fleet2_rows"][row] = fleet2_text
    for column in range(width):
        ref_glyph, live_glyph = reference_text[column], fleet2_text[column]
        if key == ("OVERVIEW", 80, 4):
            bg = 234 if 33 <= column < 79 else None
            ref_style = "section_head" if ref_glyph != " " and 33 <= column < 79 else None
            live_style = "section_head" if live_glyph != " " and 33 <= column < 79 else None
        else:
            bg = None
            ref_style = "dim" if ref_glyph != " " else None
            live_style = "dim" if live_glyph != " " else None
        record["reference_cells"][row][column] = (ref_glyph, ref_style, bg)
        record["fleet2_cells"][row][column] = (live_glyph, live_style, bg)
    for column, (expected, actual) in pairs.items():
        record["reference_cells"][row][column] = expected
        record["fleet2_cells"][row][column] = actual
        record["differences"].append({"kind": "cell", "row": row, "column": column,
                                      "expected": list(expected), "actual": list(actual)})
    record["reference_profile"] = R14_REFERENCE_PROFILE
    if key == ("OVERVIEW", 80, 4):
        trace_cells = [{"row": row, "column": 65, "kinds": ["text-overlap"]},
                       {"row": row, "column": 67, "kinds": ["text-overlap"]}]
    elif row == 22:
        trace_cells = [{"row": row, "column": width - 3, "kinds": ["text-on-box-rule"]},
                       {"row": row, "column": width - 2, "kinds": ["text-overlap"]},
                       {"row": row, "column": width - 1, "kinds": ["text-beyond-pane"]}]
    elif row == 26:
        trace_cells = [{"row": row, "column": width - 1, "kinds": ["text-beyond-pane"]}]
    else:
        trace_cells = [{"row": row, "column": 78, "kinds": ["text-edge-predecessor"]},
                       {"row": row, "column": 79, "kinds": ["text-beyond-pane"]}]
    record["reference_overflow_mask"] = {"version": REFERENCE_MASK_VERSION,
                                         "profile": R14_REFERENCE_PROFILE, "cells": trace_cells}
    if hits:
        record["differences"].append({"kind": "hits", "expected": hits, "actual": []})
    return record


def _r14_generic_overflow_record(state="OVERVIEW", width=80, row=5):
    contract = R14_GENERIC_OVERFLOW_CASES[(state, width, row)]
    record = _record(width=width)
    record["state"] = state
    trace_cells = []
    for column, (expected, actual, kinds) in contract.items():
        record["reference_cells"][row][column] = expected
        record["fleet2_cells"][row][column] = actual
        record["differences"].append({"kind": "cell", "row": row, "column": column,
                                      "expected": list(expected), "actual": list(actual)})
        trace_cells.append({"row": row, "column": column, "kinds": list(kinds)})
    record["reference_rows"][row] = "".join(cell[0] or "" for cell in record["reference_cells"][row])
    record["fleet2_rows"][row] = "".join(cell[0] or "" for cell in record["fleet2_cells"][row])
    record["reference_profile"] = R14_REFERENCE_PROFILE
    record["reference_overflow_mask"] = {"version": REFERENCE_MASK_VERSION,
                                         "profile": R14_REFERENCE_PROFILE, "cells": trace_cells}
    return record


def _change_candidate_cell(record, row, column, actual):
    record["fleet2_cells"][row][column] = tuple(actual)
    diff = next((item for item in record["differences"]
                 if item.get("kind") == "cell" and item.get("row") == row and item.get("column") == column), None)
    if diff is None:
        diff = {"kind": "cell", "row": row, "column": column,
                "expected": list(record["reference_cells"][row][column])}
        record["differences"].append(diff)
    diff["actual"] = list(actual)
    row_text = record["fleet2_rows"][row]
    record["fleet2_rows"][row] = row_text[:column] + (actual[0] or " ") + row_text[column + 1:]


def _change_candidate_cells(record, row, updates):
    for column, actual in updates.items():
        record["fleet2_cells"][row][column] = tuple(actual)
        diff = next((item for item in record["differences"]
                     if item.get("kind") == "cell" and item.get("row") == row and
                     item.get("column") == column), None)
        if diff is None:
            diff = {"kind": "cell", "row": row, "column": column,
                    "expected": list(record["reference_cells"][row][column])}
            record["differences"].append(diff)
        diff["actual"] = list(actual)
    record["fleet2_rows"][row] = "".join(cell[0] or "" for cell in record["fleet2_cells"][row])


class ClassificationTests(unittest.TestCase):
    def test_narrow_reference_edge_flag_alone_never_excludes_interior_cell_differences(self):
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
        self.assertEqual(docs[0]["entries"][0]["class"], "unexplained")
        self.assertEqual(docs[1]["entries"][0]["class"], "fleet2-defect")
        self.assertEqual((docs[2]["entries"][0]["class"], docs[2]["entries"][0]["rule"]),
                         ("fleet2-defect", "session-context-line"))

    def test_generic_r14_overflow_requires_exact_safe_case_trace_and_unchanged_prefix(self):
        record = _r14_generic_overflow_record()
        accepted = classify([record])["entries"][0]
        self.assertEqual((accepted["class"], accepted["rule"], accepted["difference_cells"]),
                         ("excluded", "r14-reference-overflow-mask", [[5, 79]]))

        rejected = []
        wrong_glyph = _r14_generic_overflow_record()
        _change_candidate_cell(wrong_glyph, 5, 79, ("X", None, None))
        rejected.append(wrong_glyph)

        wrong_style = _r14_generic_overflow_record()
        _change_candidate_cell(wrong_style, 5, 79, (" ", "reverse", None))
        rejected.append(wrong_style)

        changed_neighbor = _r14_generic_overflow_record()
        _change_candidate_cell(changed_neighbor, 5, 40, ("Z", "dim", None))
        rejected.append(changed_neighbor)

        wrong_trace = _r14_generic_overflow_record()
        wrong_trace["reference_overflow_mask"]["cells"][0]["kinds"] = ["text-overlap"]
        rejected.append(wrong_trace)

        wrong_state = _r14_generic_overflow_record()
        wrong_state["state"] = "PEERS"
        rejected.append(wrong_state)

        wrong_width = _r14_generic_overflow_record()
        wrong_width["width"] = 100
        rejected.append(wrong_width)

        wrong_reference = _r14_generic_overflow_record()
        wrong_reference["reference_cells"][5][79] = ("q", "dim", None)
        wrong_reference["differences"][0]["expected"] = ["q", "dim", None]
        wrong_reference["reference_rows"][5] = "".join(
            cell[0] or "" for cell in wrong_reference["reference_cells"][5])
        rejected.append(wrong_reference)

        for index, candidate in enumerate(rejected):
            with self.subTest(index=index):
                self.assertEqual(classify([candidate])["entries"][0]["class"], "unexplained")

    def test_generic_r14_overflow_validates_the_modeled_divider_and_terminal_pair(self):
        record = _r14_generic_overflow_record("MODELS", 80, 20)
        accepted = classify([record])["entries"][0]
        self.assertEqual((accepted["class"], accepted["rule"], accepted["difference_cells"]),
                         ("excluded", "r14-reference-overflow-mask", [[20, 78], [20, 79]]))

        changed_pair = _r14_generic_overflow_record("MODELS", 80, 20)
        _change_candidate_cells(changed_pair, 20, {78: ("界", "dim", None), 79: ("", "dim", None)})
        self.assertEqual(classify([changed_pair])["entries"][0]["class"], "unexplained")

    def test_generic_overflow_wide_glyphs_are_validated_as_a_complete_pair(self):
        complete = [[("界", "dim", None), ("", "dim", None)]]
        self.assertTrue(_wide_continuation_pair_is_complete(complete, 0, (0, 1)))
        self.assertFalse(_wide_continuation_pair_is_complete(complete, 0, (1,)))
        malformed = [[("界", "dim", None), ("x", "dim", None)]]
        self.assertFalse(_wide_continuation_pair_is_complete(malformed, 0, (0, 1)))

    def test_overflow_trace_does_not_override_invisible_foreground_only_blank_policy(self):
        record = _record(width=80, cells=[(5, 79, " ", "dim", " ", None)])
        record["state"] = "MODELS"
        record["reference_profile"] = R14_REFERENCE_PROFILE
        record["reference_overflow_mask"] = {
            "version": REFERENCE_MASK_VERSION, "profile": R14_REFERENCE_PROFILE,
            "cells": [{"row": 5, "column": 79, "kinds": ["text-beyond-pane"]}],
        }
        entry = classify([record])["entries"][0]
        self.assertEqual((entry["class"], entry["rule"]), ("intended", "blank-cell-key"))

    def test_r14_overview_header_overlap_requires_exact_trace_and_candidate_geometry(self):
        record = _r14_residual_record("OVERVIEW", 80, 4)
        entry = classify([record])["entries"][0]
        self.assertEqual((entry["class"], entry["rule"], entry["difference_cells"]),
                         ("excluded", "r14-reference-residual-mask",
                          [[4, 64], [4, 65], [4, 66], [4, 67], [4, 68]]))

        cases = [
            (64, ("Z", "section_head", 234)),       # wrong canonical CTX glyph
            (65, ("Z", "section_head", 234)),       # wrong glyph at a traced overlap cell
            (64, ("C", "branch_s", 234)),            # wrong header style
            (76, ("X", "section_head", 234)),       # AGE geometry moved or corrupted
        ]
        for column, actual in cases:
            with self.subTest(column=column, actual=actual):
                rejected = _r14_residual_record("OVERVIEW", 80, 4)
                _change_candidate_cell(rejected, 4, column, actual)
                self.assertEqual(classify([rejected])["summary"]["unexplained"], 1)

        untraced = _r14_residual_record("OVERVIEW", 80, 4)
        untraced["reference_overflow_mask"]["cells"] = []
        self.assertEqual(classify([untraced])["entries"][0]["class"], "unexplained")

    def test_r14_models_box_note_mask_keeps_the_candidate_corner_and_prefix(self):
        for width in (80, 100):
            record = _r14_residual_record("MODELS", width, 22)
            entry = classify([record])["entries"][0]
            self.assertEqual((entry["class"], entry["rule"]),
                             ("excluded", "r14-reference-residual-mask"))
            for column, actual in ((width - 2, ("X", "dim", None)),
                                   (width - 2, ("┘", "reverse", None)),
                                   (width - 4, ("X", "dim", None))):
                with self.subTest(width=width, column=column, actual=actual):
                    rejected = _r14_residual_record("MODELS", width, 22)
                    _change_candidate_cell(rejected, 22, column, actual)
                    self.assertEqual(classify([rejected])["summary"]["unexplained"], 1)

    def test_r14_models_row26_splits_invisible_blank_keys_from_edge_clips(self):
        for width, expected_cells in ((80, [59, 79]), (100, [59, 86, 99])):
            record = _r14_residual_record("MODELS", width, 26)
            entries = classify([record])["entries"]
            self.assertEqual([entry["difference_cells"][0][1] for entry in entries], expected_cells)
            self.assertEqual([entry["class"] for entry in entries], ["intended"] * len(expected_cells))
            self.assertEqual([entry["rule"] for entry in entries],
                             ["blank-cell-key" if column in (59, 86) else "r14-models-safe-edge-clip"
                              for column in expected_cells])

        rejected_cases = [
            (80, 59, (" ", "reverse", None)),             # foreground key is outside the approved pair
            (80, 59, (" ", "branch_s", 234)),             # background change
            (80, 79, ("X", "hb_opencode", None)),        # visible clipped glyph changed
            (100, 99, (" ", "reverse", None)),            # edge suffix style changed
        ]
        for width, column, actual in rejected_cases:
            with self.subTest(width=width, column=column, actual=actual):
                rejected = _r14_residual_record("MODELS", width, 26)
                _change_candidate_cell(rejected, 26, column, actual)
                self.assertEqual(classify([rejected])["summary"]["unexplained"], 1)

    def test_r14_models_row32_clip_checks_exact_suffix_and_leaves_hit_changes_unexplained(self):
        record = _r14_residual_record("MODELS", 80, 32)
        entry = classify([record])["entries"][0]
        self.assertEqual((entry["class"], entry["rule"]), ("intended", "r14-models-safe-edge-clip"))

        for column, actual in ((78, ("X", "branch_s", None)),
                               (78, (" ", "reverse", None)),
                               (77, ("X", "dim", None))):
            with self.subTest(column=column, actual=actual):
                rejected = _r14_residual_record("MODELS", 80, 32)
                _change_candidate_cell(rejected, 32, column, actual)
                self.assertEqual(classify([rejected])["summary"]["unexplained"], 1)

        hit = [10, 40, 50, ["ui", "sel", "unmasked"]]
        with_hit = _r14_residual_record("MODELS", 80, 32, hits=[hit])
        entries = classify([with_hit])["entries"]
        self.assertEqual([entry["class"] for entry in entries], ["intended", "unexplained"])

    def test_adjacent_rows_keep_independent_reference_masks(self):
        record = _r14_generic_overflow_record("COMPUTE", 80, 7)
        _change_candidate_cell(record, 8, 50, ("b", "dim", None))
        entries = classify([record])["entries"]
        self.assertEqual([(entry["rows"], entry["class"]) for entry in entries],
                         [([7, 7], "excluded"), ([8, 8], "unexplained")])

    def test_a_known_style_difference_gets_a_reason_and_an_owner(self):
        document = classify([_record(cells=[(1, 21, "━", "g_blocked", "━", "grp_live")])])
        self.assertEqual(document["summary"]["unexplained"], 0)
        entry = document["entries"][0]
        self.assertEqual((entry["rule"], entry["class"]), ("usage-gauge-80", "fleet2-defect"))
        self.assertTrue(entry["owner"].endswith("chrome.py"))

    def test_a_difference_no_rule_explains_stays_unexplained(self):
        document = classify([_record(cells=[(20, 50, "a", "dim", "b", "dim")])])
        self.assertEqual(document["summary"]["unexplained"], 1)

    def test_current_prd_spinner_order_is_intentional_and_not_a_system_pending_defect(self):
        census = " 0 working  ● 0 idle  ● 0 answer needed   hearting v3.50.3-20-gad247e2b6  02:38"
        record = _rows_record(80, "SYSTEM", {0: (census + "⠏", "⠏" + census)})
        entry = classify([record])["entries"][0]

        self.assertEqual((entry["class"], entry["rule"]), ("intended", "prd-census-spinner-order"))

    def test_ellipsis_rule_requires_the_ellipsis_itself_to_be_the_changed_cell(self):
        prefix = "a longer field value"
        record = _rows_record(80, "ROUTES", {10: (prefix + "X", prefix + "…")}, start=33)
        entry = classify([record])["entries"][0]
        self.assertEqual((entry["class"], entry["rule"]), ("fleet2-defect", "hard-cut-vs-ellipsis"))

        record = _rows_record(80, "ROUTES", {10: (prefix + "X", prefix + "…")}, start=33)
        record["fleet2_cells"][10][33 + len(prefix)] = ("X", "dim", None)
        record["differences"] = [diff for diff in record["differences"]
                                 if not (diff.get("kind") == "cell" and diff.get("row") == 10)]
        record["differences"].append({"kind": "cell", "row": 10, "column": 79,
                                      "expected": ["c", "dim", None], "actual": [" ", None, None]})
        record["reference_cells"][10][79] = ("c", "dim", None)
        record["fleet2_cells"][10][79] = (" ", None, None)
        record["reference_rows"][10] = " " * 79 + "c"
        record["fleet2_rows"][10] = " " * 80
        entry = classify([record])["entries"][0]
        self.assertEqual(entry["class"], "unexplained")

    def test_spinner_position_does_not_hide_count_version_clock_or_style_changes(self):
        census = " 0 working  ● 0 idle  ● 0 answer needed   hearting v3.50.3-20-gad247e2b6  02:38"
        cases = [
            ("⠏" + census.replace("0 working", "1 working", 1), {}),
            ("⠏" + census.replace("gad247e2b6", "bad247e2b6", 1), {}),
            ("⠏" + census.replace("02:38", "02:39", 1), {}),
            ("⠏" + census, {(0, 45): "blocked"}),
            ("⠏" + census, {(0, 0): "blocked"}),
        ]
        for candidate, styles in cases:
            with self.subTest(candidate=candidate, styles=styles):
                record = _rows_record(80, "SYSTEM", {0: (census + "⠏", candidate)},
                                      fleet_style_overrides=styles)
                entries = classify([record])["entries"]
                self.assertTrue(entries)
                self.assertEqual(entries[0]["class"], "unexplained")

    def test_r14_narrow_host_identity_difference_is_exact_and_prd_bound(self):
        record = _r14_narrow_header_record()
        entry = classify([record])["entries"][0]
        self.assertEqual((entry["class"], entry["rule"], entry["rows"]),
                         ("intended", "prd-narrow-host-identity", [0, 0]))
        self.assertIn(R14_HEADER_IDENTITY_PRD_SHA256, entry["reason"])

        candidates = []
        for column, actual in (
                (0, ("◇", "g_work", None)),               # wrong host mark
                (7, ("X", "g_work", None)),               # wrong host name
                (11, ("3", "dim", None)),                 # wrong host count
                (13, ("X", None, None)),                   # adjacent cell outside host prefix
                (record["fleet2_rows"][0].index("⠏ 0") + 2, ("1", "g_spin", None)),  # wrong status count
                (record["fleet2_rows"][0].index("● 0", record["fleet2_rows"][0].index("●") + 1) + 2,
                 ("1", "g_blocked", None)),               # wrong blocked count
                (record["fleet2_rows"][0].index("gad247"), ("b", "version_release", None)),
                (record["fleet2_rows"][0].index("hearting"), ("x", "hearting_name", None)),
                (record["fleet2_rows"][0].index("unmanaged"), ("l", "version_method", None)),
                (record["fleet2_rows"][0].index("unmanaged"), (" ", None, None)),
                (79, ("9", "dim", None)),                 # wrong freshness digit
                (record["fleet2_rows"][0].index("⠏"), ("⠙", "g_spin", None)),  # wrong spinner glyph
                (0, ("⌂", "dim", None)),                   # wrong host style
                (record["fleet2_rows"][0].index("⠏ 0") + 2, ("0", "dim", None)),
                (record["fleet2_rows"][0].index("gad247"), ("g", "dim", None)),
        ):
            altered = copy.deepcopy(record)
            _change_candidate_cell(altered, 0, column, actual)
            candidates.append(altered)
        altered_prefix = copy.deepcopy(record)
        _change_candidate_cell(altered_prefix, 0, 0, ("h", "g_work", None))
        candidates.append(altered_prefix)
        altered_prefix_space = copy.deepcopy(record)
        _change_candidate_cell(altered_prefix_space, 0, 1, ("X", "g_work", None))
        candidates.append(altered_prefix_space)
        altered_source_method = copy.deepcopy(record)
        altered_source_method["validated_header_identity"]["install_method"] = "linked"
        candidates.append(altered_source_method)
        altered_source_version = copy.deepcopy(record)
        altered_source_version["validated_header_identity"]["version"] = "v9.9.9"
        candidates.append(altered_source_version)
        altered_brandless = copy.deepcopy(record)
        brand = altered_brandless["fleet2_rows"][0].index("hearting")
        _change_candidate_cell(altered_brandless, 0, brand, (" ", None, None))
        candidates.append(altered_brandless)
        altered_wide = copy.deepcopy(record)
        wide_column = altered_wide["fleet2_rows"][0].index("gad247")
        _change_candidate_cells(altered_wide, 0, {
            wide_column: ("界", "version_release", None),
            wide_column + 1: ("", "version_release", None),
        })
        candidates.append(altered_wide)
        altered_reference = copy.deepcopy(record)
        altered_reference["reference_profile"] = "unregistered-copy"
        candidates.append(altered_reference)
        altered_trace = copy.deepcopy(record)
        altered_trace["reference_overflow_mask"]["version"] = "unregistered-trace"
        candidates.append(altered_trace)
        changed_hits = copy.deepcopy(record)
        changed_hits["differences"].append({"kind": "hits", "expected": [], "actual": [[0, 0, 1, ["ui"]]]})
        candidates.append(changed_hits)

        for altered in candidates:
            with self.subTest(row=altered["fleet2_rows"][0][:20], differences=len(altered["differences"])):
                result = classify([altered])
                self.assertGreater(result["summary"]["unexplained"], 0)
        hit_result = classify([changed_hits])
        self.assertIn(("prd-narrow-host-identity", "intended"),
                      [(entry["rule"], entry["class"]) for entry in hit_result["entries"]])
        self.assertIn(("hits", "unexplained", "unexplained"),
                      [(entry["kind"], entry["class"], entry["rule"]) for entry in hit_result["entries"]])

    def test_r14_wide_method_insertion_is_source_bound_and_tuple_exact(self):
        record = _r14_wide_header_record()
        entry = classify([record])["entries"][0]
        self.assertEqual((entry["class"], entry["rule"], entry["rows"]),
                         ("intended", "prd-f70-install-method", [0, 0]))
        self.assertIn(R14_HEADER_IDENTITY_PRD_SHA256, entry["reason"])

        candidates = []
        row = record["fleet2_rows"][0]
        for column, actual in (
                (row.index("unmanaged"), ("l", "version_method", None)),
                (row.index("hearting"), ("x", "hearting_name", None)),
                (row.index("⠏"), ("⠙", "g_spin", None)),
                (row.index("gpu-a01"), ("X", "g_work", None)),
                (row.index("02:38") + 4, ("9", "dim", None)),
                (row.index("gad247"), ("g", "dim", None)),
        ):
            altered = copy.deepcopy(record)
            _change_candidate_cell(altered, 0, column, actual)
            candidates.append(altered)
        wrong_source_method = copy.deepcopy(record)
        wrong_source_method["validated_header_identity"]["install_method"] = "linked"
        candidates.append(wrong_source_method)
        wrong_source_host = copy.deepcopy(record)
        wrong_source_host["validated_header_hosts"][0]["name"] = "other"
        candidates.append(wrong_source_host)
        wide_pair = copy.deepcopy(record)
        version_column = wide_pair["fleet2_rows"][0].index("gad247")
        _change_candidate_cells(wide_pair, 0, {
            version_column: ("界", "version_release", None),
            version_column + 1: ("", "version_release", None),
        })
        candidates.append(wide_pair)

        for altered in candidates:
            with self.subTest(row=altered["fleet2_rows"][0][:28]):
                self.assertGreater(classify([altered])["summary"]["unexplained"], 0)

        changed_hits = copy.deepcopy(record)
        changed_hits["differences"].append({"kind": "hits", "expected": [],
                                            "actual": [[0, 0, 1, ["ui"]]]})
        hit_result = classify([changed_hits])
        self.assertIn(("prd-f70-install-method", "intended"),
                      [(entry["rule"], entry["class"]) for entry in hit_result["entries"]])
        self.assertIn(("hits", "unexplained", "unexplained"),
                      [(entry["kind"], entry["class"], entry["rule"]) for entry in hit_result["entries"]])

    def test_empty_answered_repo_placeholder_does_not_claim_an_aggregate_count_defect(self):
        record = _record(width=140, cells=[(7, 40, "0", "dim", "·", "dim")])
        record["state"] = "QUESTIONS"
        record["reference_rows"][7] = " " * 33 + " ── ANSWERED TODAY  0 · 0 decision records in all projects ───────"
        record["fleet2_rows"][7] = " " * 33 + " ── ANSWERED TODAY  · 0 · 0 decision records in all projects ─────"

        entry = classify([record])["entries"][0]

        self.assertEqual(entry["class"], "unexplained")

    def test_answered_repo_count_difference_requires_the_same_explicit_repo(self):
        record = _record(width=140, cells=[(7, 40, "6", "dim", "20", "dim")])
        record["state"] = "QUESTIONS"
        record["reference_rows"][7] = " " * 33 + " ── ANSWERED TODAY  SR_CorrNet 6 · 32 decision records in all projects"
        record["fleet2_rows"][7] = " " * 33 + " ── ANSWERED TODAY  SR_CorrNet 20 · 32 decision records in all projects"

        entry = classify([record])["entries"][0]

        self.assertEqual((entry["class"], entry["rule"]), ("fleet2-defect", "answered-head"))

    def test_rightmost_feed_cell_is_the_recorded_terminal_guard(self):
        record = _record(width=80, cells=[(42, 78, "수", "soft", " ", None),
                                         (42, 79, "", "soft", " ", None)])
        record["reference_rows"][42] = " ≋ event text ends with 수"
        record["fleet2_rows"][42] = " ≋ event text ends with"

        entry = classify([record])["entries"][0]

        self.assertEqual((entry["class"], entry["rule"]), ("intended", "right-edge-safe-clip"))

    def test_unknown_compute_track_is_a_narrow_reference_copy_limit(self):
        ref0 = "  gpu-a01   ● 0  RTX 6000 Ada     ·    ·            ·"
        live0 = "  gpu-a01   ● 0  RTX 6000 Ada    ·     ··········   ·"
        ref1 = "            ● 1  RTX 6000 Ada     ·    ·            ·"
        live1 = "            ● 1  RTX 6000 Ada    ·     ··········   ·"
        record = self._unknown_compute_record(140, ref0, live0, ref1, live1)

        entry = classify([record])["entries"][0]

        self.assertEqual((entry["class"], entry["rule"]), ("copy-limitation", "unknown-gpu-track"))

    def test_unknown_compute_track_does_not_hide_interior_glyph_or_style_changes(self):
        ref0 = "  gpu-a01   ● 0  RTX 6000 Ada     ·    ·            ·"
        live0 = "  gpu-a01   ● 0  RTX 6000 Ada    ·     ··········   ·"
        ref1 = "            ● 1  RTX 6000 Ada     ·    ·            ·"
        live1 = "            ● 1  RTX 6000 Ada    ·     ··········   ·"
        wrong_glyph = self._unknown_compute_record(140, ref0, live0.replace("gpu-a01", "gpu-a0X"), ref1, live1)
        wrong_style = self._unknown_compute_record(140, ref0, live0, ref1, live1,
                                                   fleet_style_overrides={(5, 35): "blocked"})
        for record in (wrong_glyph, wrong_style):
            with self.subTest(record=record):
                self.assertEqual(classify([record])["entries"][0]["class"], "unexplained")

    def _unknown_compute_record(self, width, ref0, live0, ref1, live1, fleet_style_overrides=None):
        record = _rows_record(width, "COMPUTE", {5: (" " * 33 + ref0, " " * 33 + live0),
                                                 6: (" " * 33 + ref1, " " * 33 + live1)},
                              fleet_style_overrides=fleet_style_overrides)
        record["reference_copy_limitations"] = [
            {"reference_file": "mock13.py", "host": "gpu-a01", "gpu": gpu,
             "unknown_cells": ["UTIL", "COMPUTE block VRAM percent", "VRAM gauge", "VRAM used/total"]}
            for gpu in (0, 1)]
        record["reference_profile"] = "mock-r14-v87"
        record["reference_overflow_mask"] = {"version": "r14-v87-explicit-cell-mask-v1",
                                               "profile": "mock-r14-v87", "cells": []}
        for row in (5, 6):
            for column in range(73, 82):
                record["reference_cells"][row][column] = (" ", None, None)
                for diff in record["differences"]:
                    if diff.get("kind") == "cell" and diff.get("row") == row and diff.get("column") == column:
                        diff["expected"] = [" ", None, None]
        return record

    def test_a_hit_difference_on_a_row_whose_cells_are_equal_is_not_derived(self):
        hit = [30, 34, 100, ["scope", "x"]]
        document = classify([_record(hits=([hit], []))])
        self.assertEqual([entry["class"] for entry in document["entries"]], ["unexplained"])

    def test_a_hit_difference_on_an_explained_row_follows_its_cells(self):
        hit = [1, 34, 100, ["usage"]]
        document = classify([_record(cells=[(1, 21, "━", "g_blocked", "━", "grp_live")], hits=([hit], []))])
        self.assertEqual(sorted(entry["class"] for entry in document["entries"]), ["fleet2-defect", "unexplained"])

    def test_reference_hits_beyond_the_screen_are_the_recorded_narrow_width_defect(self):
        hit = [6, 71, 88, ["ui", "sel", "top/codex"]]
        document = classify([_record(width=80, hits=([hit], []))])
        self.assertEqual((document["entries"][0]["class"], document["entries"][0]["rule"]), ("unexplained", "unexplained"))

    def test_only_the_versioned_r14_models_hit_mask_excludes_ordered_hit_changes(self):
        tiers = ("top", "deep", "balanced-deep", "balanced", "light", "mini")
        expected = [([row, 71, 88, ["ui", "sel", f"{tier}/codex"]],
                     [row, 90, 107, ["ui", "sel", f"{tier}/opencode"]])
                    for row, tier in enumerate(tiers, 6)]
        before = [5, 34, 42, ["ui", "tab", "OVERVIEW"]]
        after = [12, 34, 42, ["ui", "tab", "SYSTEM"]]
        reference = [before, *(item for pair in expected for item in pair), after]
        fleet2 = [[row, 71, 78, ["ui", "sel", f"{tier}/codex"]]
                  for row, tier in enumerate(tiers, 6)]
        record = _record(width=80, hits=(reference, [before, *fleet2, after]))
        record["state"] = "MODELS"
        record["reference_profile"] = "mock-r14-v87"
        record["reference_overflow_mask"] = {"version": "r14-v87-explicit-cell-mask-v1",
                                               "profile": "mock-r14-v87", "cells": []}
        hit_entry = classify([record])["entries"][0]
        self.assertEqual((hit_entry["class"], hit_entry["rule"]),
                         ("excluded", "r14-models-overflow-hit-mask"))

        reordered = _record(width=80, hits=(reference, [after, *fleet2, before]))
        reordered["state"] = "MODELS"
        reordered["reference_profile"] = "mock-r14-v87"
        reordered["reference_overflow_mask"] = {"version": "r14-v87-explicit-cell-mask-v1",
                                                 "profile": "mock-r14-v87", "cells": []}
        self.assertEqual(classify([reordered])["entries"][0]["class"], "unexplained")

    def test_an_unmasked_or_reordered_hit_stays_unexplained_even_beside_a_cell_band(self):
        first = [6, 40, 60, ["ui", "sel", "model/a"]]
        changed = [6, 40, 61, ["ui", "sel", "model/a"]]
        record = _record(cells=[(6, 45, "a", "dim", "b", "dim")], hits=([first], [changed]))
        record["state"] = "MODELS"
        record["reference_profile"] = "mock-r14-v87"
        record["reference_overflow_mask"] = {"version": "r14-v87-explicit-cell-mask-v1",
                                               "profile": "mock-r14-v87", "cells": []}
        entries = classify([record])["entries"]
        self.assertEqual(entries[1]["class"], "unexplained")

        a, b = [1, 34, 40, ["ui", "tab"]], [2, 34, 40, ["ui", "tab"]]
        reordered = _record(hits=([a, b], [b, a]))
        self.assertEqual(classify([reordered])["entries"][0]["class"], "unexplained")


class InjectionPieces(unittest.TestCase):
    def test_saved_model_config_is_loaded_when_detail_reads_are_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            capture = Path(temp)
            (capture / "fleet-json.json").write_text("{}", encoding="utf-8")
            (capture / "snapshot-bundle.json").write_text('{"captured_at": 123.0}', encoding="utf-8")
            (capture / "detail-reads.json").write_text("{}", encoding="utf-8")
            (capture / "seat-reads.json").write_text("{}", encoding="utf-8")
            config = {"profiles": []}
            (capture / "model-config.json").write_text('{"profiles": []}', encoding="utf-8")

            with mock.patch("tools.fleet.tests.snapshot_fixture.replay_source_bundle",
                            return_value=("snapshot", None)), \
                    mock.patch("tools.fleet.tests.parity_today._merged_detail",
                               return_value="accepted-detail") as merge_detail, \
                    mock.patch("tools.fleet.explorer.live.build_view_model", return_value="view-model") as build_vm:
                vm, now = build_live_vm(capture)

        self.assertEqual((vm, now), ("view-model", 123.0))
        merge_detail.assert_called_once_with({}, {}, config)
        build_vm.assert_called_once_with("snapshot", accepted_detail="accepted-detail", now=123.0)

    def test_reference_tree_drops_repo_rows_without_a_current_session(self):
        from tools.fleet.tests.reference_injection import _project_rows

        vm = {"repos": [{"id": "ended", "name": "ended"}, {"id": "active", "name": "active"}],
              "sessions": [{"tag": "a3", "repo_id": "active", "repo": "active"}]}
        rows = _project_rows(vm, [{"tag": "a3"}])

        self.assertEqual([(name, len(sessions)) for name, _done, _age, sessions in rows], [("active", 1)])

    def test_absent_model_config_clears_reference_seed_values(self):
        from types import SimpleNamespace
        from tools.fleet.tests.reference_injection import _inject_models

        renderer = SimpleNamespace(
            mock2=SimpleNamespace(LIVE={"allocation_fields": ["seed"], "allocation_note": "seed"}),
            PROFILES=["seed"], PROFILE_MAP={"seed": {}}, MODEL_CHOICES={"seed": []}, MAIN_ONLY={"seed": []},
            EFFORTS=["seed"], POOLS={"seed": None}, EFF_KEY={"seed": "seed"}, MODEL_SHOWN={"seed": "seed"},
            CTX={"seed": 1}, CTX_HOW={"seed": "seed"}, TIDY=(1, 2), KTOK={"seed": 1})

        _inject_models(renderer, None)

        self.assertEqual(renderer.PROFILES, [])
        self.assertEqual(renderer.MODEL_CHOICES, {})
        self.assertEqual(renderer.MAIN_ONLY, {})
        self.assertEqual(renderer.CTX, {})
        self.assertEqual(renderer.TIDY, ())
        self.assertNotIn("allocation_fields", renderer.mock2.LIVE)
        self.assertNotIn("allocation_note", renderer.mock2.LIVE)

    def test_compute_footer_uses_captured_host_gpu_states(self):
        from tools.fleet.tests.reference_injection import _compute_foot

        vm = {"compute": [["a", False, [], [], [], [[0, "working"], [1, "idle"]]],
                           ["b", False, [], [], [], [[0, "idle"]]]]}

        self.assertEqual(_compute_foot(vm), "2/2 hosts · GPU 1/3 working · 1 host idle")

    def test_unknown_compute_metrics_remain_none_and_render_as_unknown_cells(self):
        from types import SimpleNamespace
        from tools.fleet.tests.reference_injection import _adapt_unknown_compute_cells

        class Grid:
            def __init__(self):
                self.cells = {}

            def put(self, x, y, value, _style):
                self.cells[(x, y)] = value

            def segs(self, x, y, value):
                self.cells[(x, y)] = value

        def compute_renderer(g, X, use, y, util, used, total):
            for _host in (None,):
                for _gpu in (0,):
                    st = "working"
                    g.put(X + 31, y, "%3d%%" % util, "soft" if st == "working" else "dim")
                    g.segs(X + 38, y, gauge(round(total / 4), used / total * 100))
                    g.put(X + 51, y, ("%g/%g GB" % (used, total)).rjust(10), "dim")

        def compute_block(g, EW, y1, used, total):
            for _host in (None,):
                for right, lab in [(23, "gpu0")]:
                    for _gpu in (0,):
                        y, gl, gk2 = y1, "●", "dim"
                        p = round(used / total * 100)
                        g.put(right - 5, y, gl, gk2)
                        g.put(right - 3, y, "%3d%%" % p, pct_key(p) if p else "dim")

        renderer = SimpleNamespace(ROOT_FN={"COMPUTE": compute_renderer},
                                   mock12=SimpleNamespace(compute_block=compute_block),
                                   mock6=SimpleNamespace(COMPUTE_BLOCK=compute_block))
        compute = [["gpu-a01", True, [1, 2], [1, 2], [0, 1],
                    [[0, "idle", "GPU", "gpu_test", None, None, None, []]]]]
        with mock.patch.dict(globals(), {"gauge": lambda track, percent: (track, percent),
                                         "pct_key": lambda _percent: "known"}):
            limitations = _adapt_unknown_compute_cells(renderer, compute)
            # Keep the captured values as None; only the isolated renderer's cell formatting adapts.
            self.assertEqual(compute[0][5][0][4:7], [None, None, None])
            self.assertEqual(limitations, [{"reference_file": "mock13.py", "host": "gpu-a01", "gpu": 0,
                                            "unknown_cells": ["UTIL", "COMPUTE block VRAM percent",
                                                              "VRAM gauge", "VRAM used/total"]}])

            unknown = Grid()
            renderer.ROOT_FN["COMPUTE"](unknown, 0, 60, 5, None, None, None)
            self.assertEqual(unknown.cells, {(31, 5): "  ·", (38, 5): "·", (51, 5): "·"})

            unknown_block = Grid()
            renderer.mock12.compute_block(unknown_block, 32, 7, None, None)
            self.assertEqual(unknown_block.cells, {(18, 7): "●", (20, 7): "  ·"})

            known = Grid()
            renderer.ROOT_FN["COMPUTE"](known, 0, 60, 6, 75, 12, 24)
            self.assertEqual(known.cells, {(31, 6): " 75%", (38, 6): (6, 50.0),
                                           (51, 6): "  12/24 GB"})

            known_block = Grid()
            renderer.mock12.compute_block(known_block, 32, 8, 12, 24)
            self.assertEqual(known_block.cells, {(18, 8): "●", (20, 8): " 50%"})

    def test_reference_copy_limitations_are_reported_without_exempting_differences(self):
        limitation = {"reference_file": "mock13.py", "host": "gpu-a01", "gpu": 0,
                      "unknown_cells": ["VRAM gauge", "VRAM used/total"]}
        records = [_record(cells=[(10, 50, "x", "dim", "y", "dim")]) for _ in range(2)]
        for record in records:
            record["reference_copy_limitations"] = [limitation]
        self.assertEqual(_unique_reference_copy_limitations(records), [limitation])
        self.assertEqual(classify(records[:1])["summary"]["unexplained"], 1)

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
