#!/usr/bin/env python3
"""Regression tests for warn-only installer host fitness probes."""

import subprocess
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
import host_probes  # noqa: E402
import installer  # noqa: E402


class HostProbesTest(unittest.TestCase):
    def test_node_missing_and_bwrap_ok(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                host_probes.shutil, "which",
                side_effect=lambda name: None if name == "node" else "/usr/bin/bwrap",
            ))
            stack.enter_context(mock.patch.object(
                host_probes.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            ))
            results = {p["id"]: p for p in host_probes.run()}
        self.assertEqual(results["host.node"]["status"], "warning")
        self.assertEqual(results["host.bwrap-userns"]["status"], "ok")

    def test_bwrap_userns_failure_reports_stderr_and_apparmor_hint(self):
        stderr = "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted\n"
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                host_probes.shutil, "which", return_value="/usr/bin/bwrap",
            ))
            stack.enter_context(mock.patch.object(
                host_probes.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 1, stdout="", stderr=stderr),
            ))
            probe = host_probes.probe_bwrap_userns()
        self.assertEqual(probe["status"], "warning")
        self.assertIn("bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted", probe["detail"])
        self.assertIn("apparmor_restrict_unprivileged_userns", probe["detail"])

    def test_bwrap_binary_absent_is_ok_and_skips_subprocess(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                host_probes.shutil, "which", return_value=None,
            ))
            run_mock = stack.enter_context(mock.patch.object(
                host_probes.subprocess, "run",
            ))
            probe = host_probes.probe_bwrap_userns()
        self.assertEqual(probe["status"], "ok")
        run_mock.assert_not_called()

    def test_bwrap_timeout_reports_warning_and_does_not_raise(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                host_probes.shutil, "which", return_value="/usr/bin/bwrap",
            ))
            stack.enter_context(mock.patch.object(
                host_probes.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(cmd="bwrap", timeout=10),
            ))
            probe = host_probes.probe_bwrap_userns()
        self.assertEqual(probe["status"], "warning")
        self.assertIn("probe could not complete", probe["detail"])


class HostProbesWarnOnlyContractTest(unittest.TestCase):
    def test_cmd_install_stays_ok_when_both_probes_warn(self):
        args = SimpleNamespace(
            runtimes=["claude"], target=None, scope="global",
            plugin=False, dry_run=False, report_bundle_root=None,
        )
        driver = mock.Mock()
        driver.install.return_value = {"actions": [], "blocked": False}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                installer, "get_driver", return_value=driver
            ))
            stack.enter_context(mock.patch.object(
                installer.routing_config, "ensure", return_value={
                    "status": "preserved", "path": "/tmp/config",
                    "enabled": ["claude"],
                }))
            stack.enter_context(mock.patch.object(
                installer.report_bundle_config, "ensure", return_value={
                    "status": "preserved", "path": "/tmp/report-bundle.json",
                    "root": "/tmp/reports",
                }))
            stack.enter_context(mock.patch.object(
                installer.bootstrap, "restore_memory", return_value={
                    "action": "skipped", "detail": "present",
                }))
            stack.enter_context(mock.patch.object(
                installer.bootstrap, "install_launchers", return_value=[]
            ))
            stack.enter_context(mock.patch.object(
                installer.host_probes, "run", return_value=[
                    {"id": "host.node", "status": "warning", "detail": "node not found"},
                    {"id": "host.bwrap-userns", "status": "warning", "detail": "userns unavailable"},
                ]
            ))
            result = installer.cmd_install(args)
        self.assertEqual(result["exit"], installer.EXIT_OK)
        self.assertEqual(
            [c for c in result["checks"] if c["id"].startswith(("environment", "host."))],
            [],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
