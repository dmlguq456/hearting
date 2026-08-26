#!/usr/bin/env python3
"""Gate tests for the `--notes` (cairn-w8-notes/v1) input of artifact-w8-handoff.py."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("w8", ROOT / "tools" / "artifact-w8-handoff.py")
w8 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w8)

GOOD = {"id": "note-a", "parent_id": None, "page_no": None, "repo": "hearting", "source_dir": "/x/.agent_reports/plans/a.md",
        "source_capability": None, "trashed_at": None, "revision": 1}


def write(tmp, doc):
    path = Path(tmp) / "notes.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


class LoadNotesTest(unittest.TestCase):
    def test_accepts_exact_allowlist_and_sorts_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows, meta = w8.load_notes(write(tmp, {"schema": w8.NOTES_SCHEMA, "exported_at": "2026-08-26T00:00:00Z",
                                                   "notes": [{**GOOD, "id": "note-b"}, GOOD]}))
        self.assertEqual([r["id"] for r in rows], ["note-a", "note-b"])
        self.assertEqual(set(rows[0]), w8.NOTE_ALLOWED_KEYS)
        self.assertEqual(meta, {"exported_at": "2026-08-26T00:00:00Z"})

    def test_rejects_body_bearing_keys_anywhere(self):
        for bad in ({**GOOD, "body": ""}, {**GOOD, "title": "t"}):
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(w8.NotesInputError):
                w8.load_notes(write(tmp, {"notes": [bad]}))
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(w8.NotesInputError):
            w8.load_notes(write(tmp, {"source": {"body": "leak"}, "notes": [GOOD]}))

    def test_rejects_extra_missing_keys_duplicates_and_wrong_types(self):
        cases = [
            {**GOOD, "card_id": "c1"},
            {k: v for k, v in GOOD.items() if k != "revision"},
            {**GOOD, "page_no": "1"},
            {**GOOD, "id": ""},
        ]
        for bad in cases:
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(w8.NotesInputError):
                w8.load_notes(write(tmp, {"notes": [bad]}))
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(w8.NotesInputError):
            w8.load_notes(write(tmp, {"notes": [GOOD, dict(GOOD)]}))

    def test_rejects_foreign_schema(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(w8.NotesInputError):
            w8.load_notes(write(tmp, {"schema": "other/v9", "notes": [GOOD]}))


class BundleNotesRowTest(unittest.TestCase):
    def test_existing_notes_row_and_counts(self):
        b = w8.Bundle.__new__(w8.Bundle)
        b.notes = [GOOD, {**GOOD, "id": "note-t", "trashed_at": "2026-01-01T00:00:00Z"}, {**GOOD, "id": "note-o", "repo": "other"}]
        b.notes_meta = {"exported_at": "x"}
        counts = b.existing_note_counts()
        self.assertEqual(counts, {"total": 3, "active": 2, "trashed": 1, "active_by_repo": {"hearting": 1, "other": 1}})
        row = b.existing_notes()
        self.assertEqual(row["schema"], w8.NOTES_SCHEMA)
        self.assertTrue(row["body_free"])
        self.assertEqual(row["columns"], sorted(w8.NOTE_ALLOWED_KEYS))
        self.assertEqual(w8._forbidden_keys(row), [])
        b.notes = None
        self.assertIsNone(b.existing_note_counts())


if __name__ == "__main__":
    unittest.main()
