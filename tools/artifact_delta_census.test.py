#!/usr/bin/env python3
"""W7F sealed-evidence precedence tests for artifact-delta-census.py."""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "artifact_delta_census", Path(__file__).with_name("artifact-delta-census.py")
)
CENSUS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CENSUS)
sys.path.insert(0, str(ROOT / "utilities"))
import artifact_cutover as CUTOVER  # noqa: E402


def evidence():
    return {
        "journal_target_ancestors": set(),
        "journal_targets": set(),
        "journal_target_dirs": set(),
        "delta_d": {},
        "journal_sources": set(),
        "self_write_scope": [],
        "w7c_scope": [],
        "journal_source_dirs": set(),
        "baseline": set(),
    }


class SealedEvidenceCensusTest(unittest.TestCase):
    def test_exact_set_matches_closeout_executor(self):
        self.assertEqual(CENSUS.SEALED_EVIDENCE_PATHS, CUTOVER.SEALED_EVIDENCE_PATHS)

    def test_exact_prefix_precedes_legacy_and_sealed_w7_evidence(self):
        ev = evidence()
        for prefix in CENSUS.SEALED_EVIDENCE_PATHS:
            ev["delta_d"][prefix] = {"classification": "after_cutoff_arrival"}
            for rel, kind in ((prefix, "directory"), (prefix + "/proof.md", "file")):
                with self.subTest(rel=rel):
                    self.assertEqual(
                        CENSUS.classify(rel, kind, ev),
                        ("c-leg-sealed-evidence", prefix),
                    )

    def test_nearby_sibling_is_not_widened_into_exception(self):
        rel = "plans/2026-08-24_artifact-knowledge-index-w7-extra/proof.md"
        disposition, _ = CENSUS.classify(rel, "file", evidence())
        self.assertNotEqual(disposition, "c-leg-sealed-evidence")

    def test_symlink_row_is_not_followed_or_classified_as_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            outside = Path(tmp) / "outside"
            (root / "plans").mkdir(parents=True)
            outside.mkdir()
            (outside / "hidden.md").write_text("outside\n", encoding="utf-8")
            link = root / CENSUS.SEALED_EVIDENCE_PATHS[0]
            os.symlink(outside, link)
            rows = list(CENSUS.walk(root))
            self.assertIn((CENSUS.SEALED_EVIDENCE_PATHS[0], "symlink"), rows)
            self.assertFalse(any(rel.endswith("hidden.md") for rel, _ in rows))
            self.assertEqual(
                CENSUS.classify(CENSUS.SEALED_EVIDENCE_PATHS[0], "symlink", evidence())[0],
                "symlink-row",
            )


if __name__ == "__main__":
    unittest.main()
