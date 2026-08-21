import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory_sync_config  # noqa: E402


class MemorySyncConfigTests(unittest.TestCase):
    def env(self, root):
        return mock.patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": str(root / "config"),
             "XDG_STATE_HOME": str(root / "state")},
            clear=True,
        )

    def test_write_is_atomic_canonical_and_private(self):
        with tempfile.TemporaryDirectory() as td, self.env(Path(td)):
            result = memory_sync_config.write(
                remote_url="git@example.invalid:owner/repo.git",
                exchange_dir=Path(td) / "exchange")
            path = Path(result["path"])
            self.assertEqual(result["status"], "written")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["schema_version"],
                             memory_sync_config.SCHEMA_VERSION)
            self.assertEqual(stored["ref"], memory_sync_config.DEFAULT_REF)
            self.assertTrue(stored["enabled"])

    def test_rewrite_replaces_the_previous_policy(self):
        with tempfile.TemporaryDirectory() as td, self.env(Path(td)):
            memory_sync_config.write(
                remote_url="git@example.invalid:owner/first.git",
                exchange_dir=Path(td) / "one")
            second = memory_sync_config.write(
                remote_url="git@example.invalid:owner/second.git",
                exchange_dir=Path(td) / "two")
            # A moved remote must be correctable in place; this is policy, not
            # create-once identity like the report bundle root.
            self.assertEqual(second["status"], "written")
            self.assertEqual(memory_sync_config.resolve()["remote_url"],
                             "git@example.invalid:owner/second.git")

    def test_invalid_ref_and_remote_are_refused(self):
        with tempfile.TemporaryDirectory() as td, self.env(Path(td)):
            with self.assertRaises(ValueError):
                memory_sync_config.write(remote_url="git@example.invalid:o/r.git",
                                         ref="hearting-memory-v2")
            with self.assertRaises(ValueError):
                memory_sync_config.write(remote_url="  ")
            with self.assertRaises(ValueError):
                memory_sync_config.write(remote_url="--upload-pack=evil")
            self.assertIsNone(memory_sync_config.resolve(optional=True))

    def test_relative_exchange_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as td, self.env(Path(td)):
            with self.assertRaises(ValueError):
                memory_sync_config.write(
                    remote_url="git@example.invalid:o/r.git",
                    exchange_dir="relative/exchange")

    def test_dry_run_leaves_no_file(self):
        with tempfile.TemporaryDirectory() as td, self.env(Path(td)):
            result = memory_sync_config.write(
                remote_url="git@example.invalid:o/r.git", dry_run=True)
            self.assertEqual(result["status"], "would-write")
            self.assertFalse(Path(result["path"]).exists())
            self.assertEqual(memory_sync_config.validate()["status"], "absent")

    def test_validate_reports_a_corrupt_file_without_raising(self):
        with tempfile.TemporaryDirectory() as td, self.env(Path(td)):
            path = memory_sync_config.config_path()
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(memory_sync_config.validate()["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
