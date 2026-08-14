#!/usr/bin/env python3
"""The OpenCode bootstrap must reach a session exactly once.

OpenCode auto-loads `AGENTS.md` from the global config home *and* reads
`instructions[]`, and it dedupes instruction sources by resolved path — so the
same bootstrap behind two absolute paths is injected twice instead of deduped
(`core/ADAPTATION.md` §6.1). These tests pin the carrier arithmetic: exactly one
carrier passes, zero and two both fail, and the merge never touches user entries.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SOURCE_ROOT = HERE.parent.parent


def _load_driver():
    """Re-import the driver so it re-reads XDG_CONFIG_HOME."""
    for name in ("paths", "projector", "drivers.opencode"):
        sys.modules.pop(name, None)
    return importlib.import_module("drivers.opencode")


class MergeConfigTest(unittest.TestCase):
    def setUp(self):
        self.driver = _load_driver()
        self.bootstrap = self.driver._instructions_path()
        self.skills = self.driver._skills_path()

    def test_auto_loaded_bootstrap_drops_the_duplicate_entry(self):
        existing = {
            "permission": "allow",
            "instructions": [self.bootstrap],
            "skills": {"paths": [self.skills]},
        }
        merged, changed, blocked, _ = self.driver._merge_config(
            existing, None, self.skills
        )
        self.assertTrue(changed)
        self.assertFalse(blocked)
        self.assertNotIn("instructions", merged)
        self.assertEqual(merged["permission"], "allow")

    def test_user_instruction_entries_survive_the_drop(self):
        existing = {"instructions": ["/home/me/mine.md", self.bootstrap]}
        merged, changed, _, _ = self.driver._merge_config(existing, None, self.skills)
        self.assertTrue(changed)
        self.assertEqual(merged["instructions"], ["/home/me/mine.md"])

    def test_dropping_is_idempotent(self):
        existing = {"instructions": ["/home/me/mine.md"], "skills": {"paths": [self.skills]}}
        _, changed, _, detail = self.driver._merge_config(existing, None, self.skills)
        self.assertFalse(changed)
        self.assertEqual(detail, "unchanged")

    def test_absent_auto_load_still_installs_the_entry(self):
        merged, changed, _, _ = self.driver._merge_config({}, self.bootstrap, self.skills)
        self.assertTrue(changed)
        self.assertEqual(merged["instructions"], [self.bootstrap])

    def test_non_list_instructions_still_blocks(self):
        existing = {"instructions": "AGENTS.md"}
        _, changed, blocked, _ = self.driver._merge_config(
            existing, self.bootstrap, self.skills
        )
        self.assertFalse(changed)
        self.assertTrue(blocked)


class BootstrapLoadPathCheckTest(unittest.TestCase):
    """Drive the verify check through each carrier count."""

    def _check(self, populate):
        with tempfile.TemporaryDirectory() as tmp:
            config_home = Path(tmp) / ".config"
            (config_home / "opencode").mkdir(parents=True)
            populate(config_home / "opencode")
            previous = os.environ.get("XDG_CONFIG_HOME")
            os.environ["XDG_CONFIG_HOME"] = str(config_home)
            try:
                driver = _load_driver()
                # Run only this check; the siblings shell out to generate.py and
                # preflight.sh, which would dominate the fixture's runtime.
                check = next(
                    c
                    for c in driver.checks("global")
                    if getattr(c, "__name__", "") == "_bootstrap_load_path"
                )
                result = check()
            finally:
                if previous is None:
                    os.environ.pop("XDG_CONFIG_HOME", None)
                else:
                    os.environ["XDG_CONFIG_HOME"] = previous
        self.assertEqual(result["id"], "opencode.bootstrap-load-path")
        return result

    @staticmethod
    def _link(directory):
        (directory / "AGENTS.md").symlink_to(
            SOURCE_ROOT / "adapters" / "opencode" / "AGENTS.md"
        )

    @staticmethod
    def _entry(directory):
        (directory / "opencode.json").write_text(
            json.dumps(
                {"instructions": [str(SOURCE_ROOT / "opencode_setting" / "AGENTS.md")]}
            ),
            encoding="utf-8",
        )

    def test_auto_load_link_alone_passes(self):
        result = self._check(self._link)
        self.assertTrue(result["ok"], result["detail"])
        self.assertIn("exactly once", result["detail"])

    def test_instructions_entry_alone_passes(self):
        result = self._check(self._entry)
        self.assertTrue(result["ok"], result["detail"])
        self.assertIn("exactly once", result["detail"])

    def test_both_carriers_fail_as_double_loading(self):
        def both(directory):
            self._link(directory)
            self._entry(directory)

        result = self._check(both)
        self.assertFalse(result["ok"])
        self.assertIn("2 times", result["detail"])

    def test_no_carrier_fails(self):
        result = self._check(lambda directory: None)
        self.assertFalse(result["ok"])
        self.assertIn("no bootstrap load path", result["detail"])


if __name__ == "__main__":
    unittest.main()
