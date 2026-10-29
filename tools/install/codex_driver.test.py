#!/usr/bin/env python3
"""The Codex driver no longer installs the interactive managed launcher."""
from __future__ import annotations

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

import fixture_env  # noqa: E402
from drivers import codex as codex_driver  # noqa: E402

SOURCE_ROOT = HERE.parent.parent


def _no_plan_entries(_runtimes, scope="global"):
    return {"codex": []}


class CodexDriverLauncherRetirementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(mock.patch.stopall)
        self.temp = tempfile.TemporaryDirectory(prefix="codex-driver-config-")
        self.addCleanup(self.temp.cleanup)
        self.runtime_home = Path(self.temp.name) / "codex"
        self.runtime_home.mkdir()
        mock.patch.object(codex_driver.paths, "runtime_home",
                          return_value=self.runtime_home).start()
        mock.patch.object(codex_driver.projector, "plan", side_effect=_no_plan_entries).start()
        mock.patch.object(codex_driver, "_plugin_action", return_value={"action": "plugin", "status": "skipped", "detail": "fixture"}).start()
        mock.patch.object(codex_driver.manifest, "record", return_value={"runtime": "codex"}).start()

    def test_every_install_retires_owned_launcher_even_without_plugin(self):
        for plugin in (False, True):
            for dry_run in (False, True):
                with self.subTest(plugin=plugin, dry_run=dry_run), \
                     mock.patch.object(codex_driver.codex_launcher, "uninstall",
                         return_value={"action": "managed-launcher", "status": "restored"}) as retire:
                    result = codex_driver.install(plugin=plugin, dry_run=dry_run)
                    retire.assert_called_once_with(
                        codex_home=codex_driver.paths.runtime_home("codex", "global"), dry_run=dry_run)
                    self.assertFalse(result["blocked"])

    def test_foreign_launcher_conflict_is_reported(self):
        with mock.patch.object(codex_driver.codex_launcher, "uninstall",
                side_effect=codex_driver.codex_launcher.CodexLauncherError("modified successor")):
            result = codex_driver.install(plugin=False, dry_run=False)
        self.assertTrue(result["blocked"])
        launcher = next(action for action in result["actions"] if action.get("action") == "managed-launcher")
        self.assertIn("modified successor", launcher["detail"])

    def test_install_and_dry_run_share_the_statusline_merge(self):
        config = self.runtime_home / "config.toml"
        original = '[tui]\nstatus_line = ["thread-id", "custom", "thread-id"]\nstatus_line_use_colors = false\n'
        config.write_text(original, encoding="utf-8")
        planned = codex_driver.install(dry_run=True)
        self.assertEqual(config.read_text(encoding="utf-8"), original)
        self.assertFalse((self.runtime_home / "config.toml.pre-harness-tui").exists())
        action = next(item for item in planned["actions"] if item["action"] == "codex-tui-statusline")
        self.assertEqual(action["status"], "planned")

        applied = codex_driver.install(dry_run=False)
        import tomllib
        merged_root = tomllib.loads(config.read_text(encoding="utf-8"))
        merged = merged_root["tui"]
        self.assertIs(merged_root["check_for_update_on_startup"], False)
        self.assertEqual(merged["status_line"], ["custom", "thread-id"])
        self.assertIs(merged["status_line_use_colors"], False)
        self.assertEqual((self.runtime_home / "config.toml.pre-harness-tui").read_text(), original)
        action = next(item for item in applied["actions"] if item["action"] == "codex-tui-statusline")
        self.assertEqual(action["status"], "updated")

    def test_install_preserves_explicit_startup_update_values(self):
        import tomllib
        for value in (True, False):
            with self.subTest(value=value):
                config = self.runtime_home / "config.toml"
                original = (f'check_for_update_on_startup = {str(value).lower()}\n'
                            '[tui]\nstatus_line = ["thread-title", "git-branch", "context-used", '
                            '"model-with-reasoning", "five-hour-limit", "weekly-limit", "thread-id"]\n'
                            'status_line_use_colors = true\n')
                config.write_text(original, encoding="utf-8")
                result = codex_driver.install(dry_run=False)
                parsed = tomllib.loads(config.read_text(encoding="utf-8"))
                self.assertIs(parsed["check_for_update_on_startup"], value)
                self.assertEqual(config.read_text(encoding="utf-8"), original)
                action = next(item for item in result["actions"]
                              if item["action"] == "codex-tui-statusline")
                self.assertEqual(action["status"], "unchanged")

    def test_install_updates_symlink_target_and_keeps_link_target_text(self):
        import os
        import tomllib
        target = Path(self.temp.name) / "shared" / "config.toml"
        target.parent.mkdir()
        original = ('[tui]\nstatus_line = ["thread-id", "custom"]\n'
                    'status_line_use_colors = false\n').encode()
        target.write_bytes(original)
        config = self.runtime_home / "config.toml"
        link_target = os.path.relpath(target, self.runtime_home)
        config.symlink_to(link_target)

        result = codex_driver.install(dry_run=False)
        self.assertFalse(result["blocked"])
        self.assertTrue(config.is_symlink())
        self.assertEqual(os.readlink(config), link_target)
        self.assertEqual((self.runtime_home / "config.toml.pre-harness-tui").read_bytes(), original)
        parsed_root = tomllib.loads(target.read_text(encoding="utf-8"))
        parsed = parsed_root["tui"]
        self.assertIs(parsed_root["check_for_update_on_startup"], False)
        self.assertEqual(parsed["status_line"], ["custom", "thread-id"])
        self.assertIs(parsed["status_line_use_colors"], False)

    def test_invalid_statusline_is_skipped_without_blocking_install(self):
        config = self.runtime_home / "config.toml"
        original = '[tui]\nstatus_line = 7\n'
        config.write_text(original, encoding="utf-8")
        result = codex_driver.install(dry_run=False)
        self.assertFalse(result["blocked"])
        self.assertEqual(config.read_text(encoding="utf-8"), original)
        action = next(item for item in result["actions"] if item["action"] == "codex-tui-statusline")
        self.assertEqual(action["status"], "skipped")


class CodexNativeLeafPreservationTest(unittest.TestCase):
    """Per-leaf native projections keep user entries; real projector, no mocks."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex-native-leaf-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = fixture_env.build_environment(self.root, SOURCE_ROOT)
        fixture_env.prepare_environment(self.env)
        self.home = Path(self.env["CODEX_HOME"])
        agents = sorted(p.name for p in (SOURCE_ROOT / "codex_setting/codex-agents").glob("*.toml"))
        skills = sorted(p.name for p in (SOURCE_ROOT / "codex_setting/codex-skills").iterdir())
        self.foreign_agent, self.real_agent, self.dangling_agent, self.owned_agent, self.absent_agent = agents[:5]
        self.foreign_skill, self.real_skill = skills[:2]
        self.agents = self.home / "agents"
        self.skills = self.home / "skills"
        self.agents.mkdir(parents=True)
        self.skills.mkdir(parents=True)
        self.user_target = self.root / "user-owned.toml"
        self.user_target.write_bytes(b"USER TOML\n")
        (self.agents / self.foreign_agent).symlink_to(self.user_target)
        (self.agents / self.real_agent).write_bytes(b"REAL AGENT\n")
        (self.agents / self.dangling_agent).symlink_to(self.root / "gone" / "agent.toml")
        # A stale link into the harness's own generated tree is still ours.
        (self.agents / self.owned_agent).symlink_to(
            SOURCE_ROOT / "codex_setting/codex-agents" / self.owned_agent)
        self.user_skill_dir = self.root / "user-skill"
        self.user_skill_dir.mkdir()
        (self.skills / self.foreign_skill).symlink_to(self.user_skill_dir)
        (self.skills / self.real_skill).mkdir()
        (self.skills / self.real_skill / "SKILL.md").write_bytes(b"MY SKILL\n")

    def _cli(self, *flags: str) -> tuple[int, dict]:
        proc = subprocess.run(
            ["sh", str(SOURCE_ROOT / "tools/install/harness.sh"), "install", "codex",
             "--json", "--yes", *flags],
            env=self.env, capture_output=True, text=True, timeout=300)
        return proc.returncode, json.loads(proc.stdout)

    def _checks(self, report: dict) -> dict[str, dict]:
        return {c["id"]: c for c in report["checks"]}

    def _assert_user_entries_untouched(self) -> None:
        self.assertEqual(os.readlink(self.agents / self.foreign_agent), str(self.user_target))
        self.assertEqual(self.user_target.read_bytes(), b"USER TOML\n")
        self.assertFalse((self.agents / self.real_agent).is_symlink())
        self.assertEqual((self.agents / self.real_agent).read_bytes(), b"REAL AGENT\n")
        self.assertEqual(os.readlink(self.skills / self.foreign_skill), str(self.user_skill_dir))
        self.assertEqual(list(self.user_skill_dir.iterdir()), [])
        self.assertFalse((self.skills / self.real_skill).is_symlink())
        self.assertEqual(os.listdir(self.skills / self.real_skill), ["SKILL.md"])
        self.assertEqual((self.skills / self.real_skill / "SKILL.md").read_bytes(), b"MY SKILL\n")

    def test_public_install_preserves_user_entries_and_refreshes_ours(self):
        code, report = self._cli()
        self.assertEqual(code, 0, report["lines"])
        self._assert_user_entries_untouched()
        source_agents = SOURCE_ROOT / "codex_setting/codex-agents"
        for name in (self.dangling_agent, self.owned_agent, self.absent_agent):
            self.assertEqual(os.readlink(self.agents / name), str(source_agents / name))
        checks = self._checks(report)
        for leaf in (self.foreign_agent, self.real_agent, self.foreign_skill, self.real_skill):
            check = checks[f"codex.symlink.{leaf}"]
            self.assertTrue(check["ok"], check)
            self.assertIn("preserved", check["detail"])

    def test_public_dry_run_reports_the_same_preservation_without_writing(self):
        def leaves():
            return sorted((p.relative_to(self.home), p.is_symlink())
                          for d in (self.agents, self.skills) for p in d.rglob("*"))
        before = leaves()
        code, dry = self._cli("--dry-run")
        self.assertEqual(code, 0, dry["lines"])
        self.assertEqual(before, leaves())
        self._assert_user_entries_untouched()
        code, applied = self._cli()
        self.assertEqual(code, 0)
        dry_checks, applied_checks = self._checks(dry), self._checks(applied)
        for leaf in (self.foreign_agent, self.real_agent, self.foreign_skill, self.real_skill):
            key = f"codex.symlink.{leaf}"
            self.assertEqual(dry_checks[key]["detail"], applied_checks[key]["detail"])
        for leaf in (self.dangling_agent, self.owned_agent, self.absent_agent):
            self.assertNotIn("preserved", dry_checks[f"codex.symlink.{leaf}"]["detail"])

    def test_driver_install_matches_dry_run_and_never_blocks_on_user_entries(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            dry = codex_driver.install(dry_run=True, plugin=False)
            applied = codex_driver.install(dry_run=False, plugin=False)
        for result in (dry, applied):
            self.assertFalse(result["blocked"])
            by_dest = {a["dest"]: a for a in result["actions"] if a.get("dest")}
            for path in (self.agents / self.foreign_agent, self.agents / self.real_agent,
                         self.skills / self.foreign_skill, self.skills / self.real_skill):
                self.assertEqual(by_dest[str(path)]["status"], "preserved", path)
            self.assertEqual(by_dest[str(self.agents / self.absent_agent)]["status"],
                             "planned" if result is dry else "created")
        self._assert_user_entries_untouched()


if __name__ == "__main__":
    unittest.main()
