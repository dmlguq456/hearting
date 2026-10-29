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
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SOURCE_ROOT = HERE.parent.parent

import fixture_env  # noqa: E402


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

    def test_v2_skills_use_native_list_and_preserve_existing_paths(self):
        existing = {"skills": {"paths": ["/home/me/team-skills"]}}
        merged, changed, blocked, _ = self.driver._merge_config(
            existing, None, self.skills, v2=True)
        self.assertTrue(changed)
        self.assertFalse(blocked)
        self.assertEqual(merged["skills"], ["/home/me/team-skills", self.skills])

    def test_v2_skills_conflict_is_reported_without_overwrite(self):
        existing = {"skills": "user-choice"}
        merged, changed, blocked, detail = self.driver._merge_config(
            existing, None, self.skills, v2=True)
        self.assertFalse(changed)
        self.assertTrue(blocked)
        self.assertEqual(merged, existing)
        self.assertIn("incompatible V2 type", detail)


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


class OpenCodeNativeLeafPreservationTest(unittest.TestCase):
    """Per-leaf agent/command projections keep user entries; real projector."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opencode-native-leaf-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = fixture_env.build_environment(self.root, SOURCE_ROOT)
        fixture_env.prepare_environment(self.env)
        config = Path(self.env["XDG_CONFIG_HOME"]) / "opencode"
        self.agents, self.commands = config / "agent", config / "command"
        self.agents.mkdir(parents=True)
        self.commands.mkdir(parents=True)
        agent_names = sorted(
            p.name for p in (SOURCE_ROOT / "opencode_setting/opencode-agents").glob("*/*.md"))
        command_names = sorted(
            p.name for p in (SOURCE_ROOT / "opencode_setting/opencode-commands").glob("*.md"))
        (self.foreign_agent, self.real_agent, self.dangling_agent,
         self.owned_agent, self.absent_agent) = agent_names[:5]
        self.real_command_dir, self.foreign_command = command_names[:2]
        self.user_target = self.root / "user-owned.md"
        self.user_target.write_bytes(b"USER AGENT\n")
        (self.agents / self.foreign_agent).symlink_to(self.user_target)
        (self.agents / self.real_agent).write_bytes(b"REAL AGENT\n")
        (self.agents / self.dangling_agent).symlink_to(self.root / "gone" / "agent.md")
        stale = next((SOURCE_ROOT / "opencode_setting/opencode-agents").glob(
            f"*/{self.owned_agent}"))
        (self.agents / self.owned_agent).symlink_to(stale)
        (self.commands / self.real_command_dir).mkdir()
        (self.commands / self.real_command_dir / "mine.txt").write_bytes(b"MY COMMAND\n")
        self.user_command = self.root / "user-command.md"
        self.user_command.write_bytes(b"USER COMMAND\n")
        (self.commands / self.foreign_command).symlink_to(self.user_command)

    def _cli(self, *flags):
        proc = subprocess.run(
            ["sh", str(SOURCE_ROOT / "tools/install/harness.sh"), "install", "opencode",
             "--json", "--yes", *flags],
            env=self.env, capture_output=True, text=True, timeout=300)
        return proc.returncode, json.loads(proc.stdout)

    def _assert_user_entries_untouched(self):
        self.assertEqual(os.readlink(self.agents / self.foreign_agent), str(self.user_target))
        self.assertEqual(self.user_target.read_bytes(), b"USER AGENT\n")
        self.assertFalse((self.agents / self.real_agent).is_symlink())
        self.assertEqual((self.agents / self.real_agent).read_bytes(), b"REAL AGENT\n")
        self.assertFalse((self.commands / self.real_command_dir).is_symlink())
        self.assertEqual(os.listdir(self.commands / self.real_command_dir), ["mine.txt"])
        self.assertEqual(os.readlink(self.commands / self.foreign_command), str(self.user_command))
        self.assertEqual(self.user_command.read_bytes(), b"USER COMMAND\n")

    def test_public_install_preserves_user_entries_and_refreshes_ours(self):
        code, report = self._cli()
        self.assertEqual(code, 0, report["lines"])
        self._assert_user_entries_untouched()
        for name in (self.dangling_agent, self.owned_agent, self.absent_agent):
            link = self.agents / name
            self.assertTrue(link.is_symlink() and link.exists(), name)
            self.assertTrue(
                os.readlink(link).startswith(str(SOURCE_ROOT / "opencode_setting")), name)
        checks = {c["id"]: c for c in report["checks"]}
        for leaf in (self.foreign_agent, self.real_agent, self.real_command_dir,
                     self.foreign_command):
            check = checks[f"opencode.symlink.{leaf}"]
            self.assertTrue(check["ok"], check)
            self.assertIn("preserved", check["detail"])

    def test_public_dry_run_reports_the_same_preservation_without_writing(self):
        def leaves():
            return sorted((p.relative_to(self.root), p.is_symlink())
                          for d in (self.agents, self.commands) for p in d.rglob("*"))
        before = leaves()
        code, dry = self._cli("--dry-run")
        self.assertEqual(code, 0, dry["lines"])
        self.assertEqual(before, leaves())
        code, applied = self._cli()
        self.assertEqual(code, 0)
        dry_checks = {c["id"]: c for c in dry["checks"]}
        applied_checks = {c["id"]: c for c in applied["checks"]}
        for leaf in (self.foreign_agent, self.real_agent, self.real_command_dir,
                     self.foreign_command):
            key = f"opencode.symlink.{leaf}"
            self.assertEqual(dry_checks[key]["detail"], applied_checks[key]["detail"])
        for leaf in (self.dangling_agent, self.owned_agent, self.absent_agent):
            self.assertNotIn("preserved", dry_checks[f"opencode.symlink.{leaf}"]["detail"])

    def test_driver_install_matches_dry_run_and_never_blocks_on_user_entries(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            driver = _load_driver()
            dry = driver.install(dry_run=True, plugin=False)
            applied = driver.install(dry_run=False, plugin=False)
        for result in (dry, applied):
            self.assertFalse(result["blocked"])
            by_dest = {a["dest"]: a for a in result["actions"] if a.get("dest")}
            for path in (self.agents / self.foreign_agent, self.agents / self.real_agent,
                         self.commands / self.real_command_dir,
                         self.commands / self.foreign_command):
                self.assertEqual(by_dest[str(path)]["status"], "preserved", path)
        self._assert_user_entries_untouched()


if __name__ == "__main__":
    unittest.main()
