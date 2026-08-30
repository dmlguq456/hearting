#!/usr/bin/env python3
"""The Codex driver's plugin path must reuse one launcher transaction.

`drivers.codex.install(plugin=True)` appends `codex_launcher.install()`'s own
result to its action list; it must never re-implement launcher install logic
or reshape the result. These tests pin that the driver's `managed-launcher`
action carries the launcher's protected/unchanged shape verbatim, and that
`CodexUnavailableError`/`CodexLauncherError` map to the documented
`skipped-unavailable`/`blocked` action shapes without any other transaction.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import codex_launcher  # noqa: E402
from drivers import codex as codex_driver  # noqa: E402


def _no_plan_entries(_runtimes, scope="global"):
    return {"codex": []}


class CodexDriverLauncherReuseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(codex_driver.projector, "plan", side_effect=_no_plan_entries).start()
        mock.patch.object(codex_driver, "_plugin_action", return_value={"action": "plugin", "status": "skipped", "detail": "SKIP(codex): fixture"}).start()
        mock.patch.object(codex_driver.manifest, "record", return_value={"runtime": "codex"}).start()

    def _managed_action(self, actions):
        matches = [a for a in actions if a.get("action") == "managed-launcher"]
        self.assertEqual(len(matches), 1, actions)
        return matches[0]

    def test_protected_created_result_passes_through_verbatim(self) -> None:
        expected = {
            "action": "managed-launcher",
            "status": "created",
            "target": "/fixture/.harness/bin/codex",
            "real_command": "/fixture/.local/bin/codex",
            "protected": True,
            "mode": "protected-path-v1",
        }
        with mock.patch.object(codex_launcher, "install", return_value=dict(expected)) as install_mock:
            result = codex_driver.install(plugin=True, dry_run=False)
        install_mock.assert_called_once_with(dry_run=False)
        self.assertEqual(self._managed_action(result["actions"]), expected)
        self.assertFalse(result["blocked"])

    def test_unchanged_result_passes_through_verbatim(self) -> None:
        expected = {
            "action": "managed-launcher",
            "status": "unchanged",
            "target": "/fixture/.harness/bin/codex",
            "real_command": "/fixture/.local/bin/codex",
            "protected": True,
            "mode": "protected-path-v1",
        }
        with mock.patch.object(codex_launcher, "install", return_value=dict(expected)):
            result = codex_driver.install(plugin=True, dry_run=False)
        self.assertEqual(self._managed_action(result["actions"]), expected)
        self.assertFalse(result["blocked"])

    def test_unavailable_command_maps_to_skipped_unavailable(self) -> None:
        with mock.patch.object(
            codex_launcher, "install", side_effect=codex_launcher.CodexUnavailableError("no codex on PATH")
        ):
            result = codex_driver.install(plugin=True, dry_run=False)
        action = self._managed_action(result["actions"])
        self.assertEqual(action["status"], "skipped-unavailable")
        self.assertIn("no codex on PATH", action["detail"])
        self.assertFalse(result["blocked"])

    def test_launcher_error_maps_to_blocked_without_stopping_other_actions(self) -> None:
        with mock.patch.object(
            codex_launcher, "install", side_effect=codex_launcher.CodexLauncherError("foreign file collision")
        ):
            result = codex_driver.install(plugin=True, dry_run=False)
        action = self._managed_action(result["actions"])
        self.assertEqual(action["status"], "blocked")
        self.assertIn("foreign file collision", action["detail"])
        self.assertTrue(result["blocked"])

    def test_dry_run_forwards_dry_run_to_the_launcher(self) -> None:
        with mock.patch.object(
            codex_launcher, "install", return_value={"action": "managed-launcher", "status": "planned"}
        ) as install_mock:
            codex_driver.install(plugin=True, dry_run=True)
        install_mock.assert_called_once_with(dry_run=True)

    def test_plugin_false_never_calls_the_launcher(self) -> None:
        with mock.patch.object(codex_launcher, "install") as install_mock:
            result = codex_driver.install(plugin=False, dry_run=False)
        install_mock.assert_not_called()
        self.assertFalse(any(a.get("action") == "managed-launcher" for a in result["actions"]))


if __name__ == "__main__":
    unittest.main()
