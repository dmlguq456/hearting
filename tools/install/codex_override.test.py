"""Hearting's block in a user's Codex AGENTS.override.md: add, refresh, remove.

The user's bytes outside the block never change, and a missing or empty
override is never created or edited.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import codex_override  # noqa: E402
import fixture_env  # noqa: E402

USER_RULE = "# 외부 소통은 읽기 전용\n\n- 메일은 읽기만 한다.\n".encode()


class OverrideBlockTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex-override-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        environment = fixture_env.patched_environment(root / "fixture", ROOT)
        environment.__enter__()
        self.addCleanup(environment.__exit__, None, None, None)
        self.home = Path(os.environ["CODEX_HOME"])
        self.home.mkdir(parents=True, exist_ok=True)
        self.override = self.home / codex_override.OVERRIDE_NAME
        self.bootstrap = root / "fixture" / "AGENTS.md"
        self.bootstrap.write_bytes(b"# AGENTS.md - Codex Adapter Bootstrap\n\nv1 body\n")

    def write_user(self, payload: bytes) -> None:
        self.override.write_bytes(payload)
        self.override.chmod(0o600)

    def test_add_refresh_remove_keeps_user_bytes_for_every_ending(self):
        for label, user in (("newline", USER_RULE), ("no-newline", USER_RULE.rstrip(b"\n")),
                            ("blank-line", USER_RULE + b"\n"),
                            ("crlf", USER_RULE.replace(b"\n", b"\r\n"))):
            with self.subTest(ending=label):
                self.write_user(user)
                added = codex_override.apply(self.home, self.bootstrap)
                self.assertEqual(added["status"], "added")
                data = self.override.read_bytes()
                self.assertTrue(data.startswith(user))
                self.assertIn(b"v1 body\n", data)
                self.assertTrue(data.endswith(b"<!-- hearting:codex-bootstrap end -->\n"))
                # The block starts after one blank line, whatever the user's ending.
                self.assertIn(b"\n\n<!-- hearting:codex-bootstrap begin", data)
                self.assertEqual(stat.S_IMODE(self.override.stat().st_mode), 0o600)
                self.assertTrue(added["report_lines"])

                again = codex_override.apply(self.home, self.bootstrap)
                self.assertEqual((again["status"], again["changed"]), ("unchanged", False))
                self.assertEqual(self.override.read_bytes(), data)

                self.bootstrap.write_bytes(b"# AGENTS.md - Codex Adapter Bootstrap\n\nv2 body\n")
                updated = codex_override.apply(self.home, self.bootstrap)
                self.assertEqual(updated["status"], "updated")
                refreshed = self.override.read_bytes()
                self.assertTrue(refreshed.startswith(user))
                self.assertIn(b"v2 body\n", refreshed)
                self.assertNotIn(b"v1 body", refreshed)

                removed = codex_override.remove(self.home)
                self.assertEqual(removed["status"], "removed")
                self.assertEqual(self.override.read_bytes(), user)
                self.assertEqual(stat.S_IMODE(self.override.stat().st_mode), 0o600)
                self.bootstrap.write_bytes(b"# AGENTS.md - Codex Adapter Bootstrap\n\nv1 body\n")

    def test_text_the_user_adds_after_the_block_survives_refresh_and_removal(self):
        self.write_user(USER_RULE)
        codex_override.apply(self.home, self.bootstrap)
        with self.override.open("ab") as handle:
            handle.write(b"\n- later rule\n")
        self.bootstrap.write_bytes(b"v2 body\n")
        codex_override.apply(self.home, self.bootstrap)
        self.assertTrue(self.override.read_bytes().endswith(b"-->\n\n- later rule\n"))
        codex_override.remove(self.home)
        self.assertEqual(self.override.read_bytes(), USER_RULE + b"\n- later rule\n")

    def test_missing_or_empty_override_is_left_as_it_is(self):
        missing = codex_override.apply(self.home, self.bootstrap)
        self.assertEqual((missing["status"], missing["changed"]), ("absent", False))
        self.assertFalse(self.override.exists())
        self.assertEqual(codex_override.remove(self.home)["status"], "absent")

        for empty in (b"", b"  \n\t\n"):
            with self.subTest(empty=empty):
                self.write_user(empty)
                result = codex_override.apply(self.home, self.bootstrap)
                self.assertEqual((result["status"], result["report_lines"]), ("unchanged", []))
                self.assertEqual(self.override.read_bytes(), empty)

    def test_block_leaves_when_the_user_empties_their_override(self):
        self.write_user(USER_RULE)
        codex_override.apply(self.home, self.bootstrap)
        data = self.override.read_bytes()
        self.override.write_bytes(b"\n" + data[len(USER_RULE):])
        result = codex_override.apply(self.home, self.bootstrap)
        self.assertEqual(result["status"], "removed")
        self.assertEqual(self.override.read_bytes(), b"\n")

    def test_symlinked_override_is_reported_and_not_written(self):
        target = self.home.parent / "dotfiles-override.md"
        target.write_bytes(USER_RULE)
        self.override.symlink_to(target)
        result = codex_override.apply(self.home, self.bootstrap)
        self.assertEqual((result["status"], result["changed"]), ("skipped-symlink", False))
        self.assertIn("symlink", result["report_lines"][0])
        self.assertEqual(target.read_bytes(), USER_RULE)
        self.assertTrue(self.override.is_symlink())

    def test_damaged_markers_are_reported_and_not_written(self):
        self.write_user(USER_RULE)
        codex_override.apply(self.home, self.bootstrap)
        data = self.override.read_bytes()
        damaged = data.replace(b"<!-- hearting:codex-bootstrap end -->\n", b"")
        self.override.write_bytes(damaged)
        for operation in (lambda: codex_override.apply(self.home, self.bootstrap),
                          lambda: codex_override.remove(self.home)):
            result = operation()
            self.assertEqual(result["status"], "skipped-markers")
            self.assertEqual(self.override.read_bytes(), damaged)

    def test_dry_run_reports_without_writing(self):
        self.write_user(USER_RULE)
        planned = codex_override.apply(self.home, self.bootstrap, dry_run=True)
        self.assertEqual((planned["status"], planned["changed"]), ("added", True))
        self.assertTrue(planned["report_lines"][0].startswith("dry-run"))
        self.assertEqual(self.override.read_bytes(), USER_RULE)

    def test_shipped_bootstrap_fits_in_the_block(self):
        self.write_user(USER_RULE)
        shipped = ROOT / "adapters/codex/AGENTS.md"
        self.assertEqual(codex_override.apply(self.home, shipped)["status"], "added")
        data = self.override.read_bytes()
        self.assertEqual(data[len(USER_RULE) + 1:].count(shipped.read_bytes()), 1)
        self.assertIn("AGENTS.md — Codex Adapter Bootstrap".encode(), data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
