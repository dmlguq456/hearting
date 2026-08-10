#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "user_model_config", HERE / "user_model_config.py"
)
assert SPEC and SPEC.loader
CONFIG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONFIG)


class UserModelConfigTest(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        source = root / "source" / "models.conf"
        source.parent.mkdir()
        source.write_text("CFG_MODEL=default\n", encoding="utf-8")
        return temporary, root, source

    def test_seed_once_preserves_custom_bytes_across_source_updates(self):
        temporary, root, source = self.fixture()
        with temporary:
            home = root / "runtime"
            created = CONFIG.seed_model_config("claude", source, home)
            self.assertEqual(created["status"], "created")
            destination = home / "agent-config" / "models.conf"
            destination.write_bytes(b"CFG_MODEL=custom\n")
            source.write_bytes(b"CFG_MODEL=new-release\n")
            unchanged = CONFIG.seed_model_config("claude", source, home)
            self.assertEqual(unchanged["status"], "unchanged")
            self.assertEqual(destination.read_bytes(), b"CFG_MODEL=custom\n")

    def test_codex_legacy_directory_link_migrates(self):
        temporary, root, source = self.fixture()
        with temporary:
            home = root / "runtime"
            home.mkdir()
            (home / "agent-config").symlink_to(source.parent, target_is_directory=True)
            result = CONFIG.seed_model_config("codex", source, home)
            destination = home / "agent-config" / "models.conf"
            self.assertEqual(result["status"], "migrated")
            self.assertTrue(destination.is_file())
            self.assertFalse((home / "agent-config").is_symlink())

    def test_foreign_links_block(self):
        temporary, root, source = self.fixture()
        with temporary:
            home = root / "runtime"
            foreign = root / "foreign"
            foreign.mkdir()
            home.mkdir()
            (home / "agent-config").symlink_to(foreign, target_is_directory=True)
            with self.assertRaises(CONFIG.UserModelConfigError):
                CONFIG.seed_model_config("codex", source, home)

    def test_dry_run_does_not_create_runtime_home(self):
        temporary, root, source = self.fixture()
        with temporary:
            home = root / "runtime"
            result = CONFIG.seed_model_config("opencode", source, home, dry_run=True)
            self.assertEqual(result["status"], "planned")
            self.assertFalse(home.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
