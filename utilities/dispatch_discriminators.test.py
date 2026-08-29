#!/usr/bin/env python3
"""B47-1: a declared-producer ledger + warning-style drift detector, not
full AST coverage (plan.md R5 -- "declaration ledger, never claim it as a
verdict rather than an observation"). Every declared `discriminator` row
must name a real producer that actually writes that `rejection_class`
literal at its declared `write_site`, and a fixture test that exists in the
test file it names.
"""
import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TSV = ROOT / "tools" / "dispatch-discriminators.tsv"
EXPECTED_HEADER = ["discriminator", "producer_symbol", "evidence_input", "write_site", "fixture"]
CLOSED_REJECTION_CLASSES = {"allocation-skip", "candidate-unsupported", "sealed-parent-not-live"}


def _rows():
    with TSV.open(encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream, delimiter="\t")
        header = next(reader)
        return header, [row for row in reader if row]


class DiscriminatorLedgerTest(unittest.TestCase):
    def test_b47_1_declared_producers_exist_and_fire(self):
        header, rows = _rows()
        self.assertEqual(header, EXPECTED_HEADER)
        declared = {row[0] for row in rows}
        self.assertEqual(declared, CLOSED_REJECTION_CLASSES)
        self.assertEqual(len(rows), len(CLOSED_REJECTION_CLASSES), "one row per discriminator")

        for discriminator, producer_symbol, evidence_input, write_site, fixture in rows:
            with self.subTest(discriminator=discriminator):
                write_path_str, _, _write_hint = write_site.partition(":")
                write_path = ROOT / write_path_str
                self.assertTrue(write_path.is_file(), write_path)
                source = write_path.read_text(encoding="utf-8")
                # producer >= 1: the write site literally records this exact
                # rejection_class value -- not just a mention anywhere.
                self.assertIn(f'rejection_class="{discriminator}"', source,
                              f"no producer writes rejection_class={discriminator!r} at {write_site}")

                fixture_path_str, _, fixture_name = fixture.partition("::")
                fixture_path = ROOT / fixture_path_str
                self.assertTrue(fixture_path.is_file(), fixture_path)
                fixture_source = fixture_path.read_text(encoding="utf-8")
                self.assertIn(f"def {fixture_name}(", fixture_source,
                              f"declared fixture {fixture} does not exist")

    def test_rejection_class_enum_matches_the_leaf_module(self):
        # The TSV's declared set must never drift from the leaf module's
        # closed enum (§5.2) -- a fourth discriminator here with no matching
        # REJECTION_CLASSES entry would silently never fire.
        sys.path.insert(0, str(ROOT / "utilities"))
        import dispatch_launch_tuple as LT

        _header, rows = _rows()
        declared = {row[0] for row in rows}
        self.assertEqual(declared, LT.REJECTION_CLASSES)

    def test_no_row_names_the_consumer_only_skip(self):
        # B47-10: `prior-unchanged-failure` (key in failed_tuples alone) is a
        # consumer-only skip and must never be declared as a discriminator.
        _header, rows = _rows()
        declared = {row[0] for row in rows}
        self.assertNotIn("prior-unchanged-failure", declared)


if __name__ == "__main__":
    unittest.main()
