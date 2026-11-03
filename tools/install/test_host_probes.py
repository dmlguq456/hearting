#!/usr/bin/env python3
"""Regression tests for warn-only installer host fitness probes."""

import importlib.machinery
import subprocess
import sys
import types
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


class HerdrProbeTest(unittest.TestCase):
    def test_herdr_missing_is_warning_no_invented_install_url(self):
        with mock.patch.object(host_probes.shutil, "which", return_value=None):
            probe = host_probes.probe_herdr()
        self.assertEqual(probe["status"], "warning")
        self.assertIn("herdr CLI not found", probe["detail"])
        self.assertNotIn("http", probe["detail"])

    def test_herdr_present_and_version_ok(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                host_probes.shutil, "which", return_value="/usr/bin/herdr",
            ))
            stack.enter_context(mock.patch.object(
                host_probes.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, stdout="herdr 0.8.0\n", stderr=""),
            ))
            probe = host_probes.probe_herdr()
        self.assertEqual(probe["status"], "ok")

    def test_herdr_version_nonzero_exit_is_warning(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                host_probes.shutil, "which", return_value="/usr/bin/herdr",
            ))
            stack.enter_context(mock.patch.object(
                host_probes.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="boom"),
            ))
            probe = host_probes.probe_herdr()
        self.assertEqual(probe["status"], "warning")

    def test_herdr_registered_in_run(self):
        with mock.patch.object(host_probes.shutil, "which", return_value=None), \
             mock.patch.object(host_probes.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")):
            results = {p["id"]: p for p in host_probes.run()}
        self.assertIn("host.herdr", results)

    def test_herdr_probe_never_changes_installer_exit_code(self):
        args = SimpleNamespace(
            runtimes=["claude"], target=None, scope="global",
            plugin=False, dry_run=False, report_bundle_root=None,
        )
        driver = mock.Mock()
        driver.install.return_value = {"actions": [], "blocked": False}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(installer, "get_driver", return_value=driver))
            stack.enter_context(mock.patch.object(
                installer.routing_config, "ensure", return_value={
                    "status": "preserved", "path": "/tmp/config", "enabled": ["claude"],
                }))
            stack.enter_context(mock.patch.object(
                installer.report_bundle_config, "ensure", return_value={
                    "status": "preserved", "path": "/tmp/report-bundle.json", "root": "/tmp/reports",
                }))
            stack.enter_context(mock.patch.object(
                installer.bootstrap, "restore_memory", return_value={"action": "skipped", "detail": "present"}))
            stack.enter_context(mock.patch.object(installer.bootstrap, "install_launchers", return_value=[]))
            stack.enter_context(mock.patch.object(
                installer.host_probes, "run", return_value=[
                    {"id": "host.node", "status": "warning", "detail": "node not found"},
                    {"id": "host.bwrap-userns", "status": "warning", "detail": "userns unavailable"},
                    {"id": "host.herdr", "status": "warning", "detail": "herdr not found"},
                ]))
            stack.enter_context(mock.patch.object(
                installer.node_runtime, "ensure_node", return_value={
                    "id": "host.node-runtime", "status": "warning", "detail": "node install failed: offline",
                }))
            result = installer.cmd_install(args)
        self.assertEqual(result["exit"], installer.EXIT_OK)


def _fake_yaml():
    module = types.ModuleType("yaml")
    module.__spec__ = importlib.machinery.ModuleSpec("yaml", None)
    return module


class PyYamlProbeTest(unittest.TestCase):
    def test_pyyaml_importable_is_ok(self):
        with mock.patch.dict(sys.modules, {"yaml": _fake_yaml()}):
            probe = host_probes.probe_pyyaml()
        self.assertEqual((probe["id"], probe["status"]), ("host.pyyaml", "ok"))

    def test_pyyaml_missing_is_a_warning_that_names_the_fix_and_the_other_shapes(self):
        with mock.patch.dict(sys.modules, {"yaml": None}):
            probe = host_probes.probe_pyyaml()
        self.assertEqual(probe["status"], "warning")
        self.assertIn("-m pip install --user pyyaml", probe["detail"])
        self.assertIn("non-framed shape", probe["detail"])
        with mock.patch.object(host_probes.shutil, "which", return_value=None):
            self.assertIn("host.pyyaml", {p["id"] for p in host_probes.run()})

    def test_doctor_says_once_when_pyyaml_is_missing_and_keeps_its_exit(self):
        # README sends a new install to `runtime doctor --strict`; before this it was
        # silent about PyYAML, and the first non-direct task stopped on yaml-unavailable.
        args = installer.build_parser().parse_args(["runtime", "doctor", "--runtime", "claude", "--strict"])
        report = {"ok": True, "status": "ok", "freshness": "fresh", "next_action": "none"}
        skew = {"ok": True, "surfaces": [], "compared": [], "skewed": []}
        for module, expected in ((_fake_yaml(), 0), (None, 1)):
            with mock.patch.dict(sys.modules, {"yaml": module}), \
                 mock.patch.object(installer.runtime_activation, "doctor", return_value=dict(report)), \
                 mock.patch.object(installer.runtime_activation, "surface_skew", return_value=skew):
                result = installer.cmd_runtime(args)
            missing = [line for line in result["lines"] if line.startswith("missing: PyYAML")]
            self.assertEqual(len(missing), expected, result["lines"])
            self.assertEqual(result["exit"], installer.EXIT_OK)
            if expected:
                self.assertIn("-m pip install --user pyyaml", missing[0])

    def test_verify_passes_with_pyyaml_and_fails_without_it(self):
        args = SimpleNamespace(runtimes=["claude"], target=None, scope="global", plugin=False)
        ok_config = {"ok": True, "status": "ok", "path": "x"}
        for module, ok, code in ((_fake_yaml(), True, installer.EXIT_OK),
                                 (None, False, installer.EXIT_VERIFY_FAIL)):
            with ExitStack() as stack:
                stack.enter_context(mock.patch.dict(sys.modules, {"yaml": module}))
                stack.enter_context(mock.patch.object(
                    installer.paths, "harness_state_dir", return_value=Path("/nonexistent/hearting-state")))
                stack.enter_context(mock.patch.object(installer.verifier, "run", return_value=[]))
                stack.enter_context(mock.patch.object(installer.routing_config, "validate", return_value=ok_config))
                stack.enter_context(mock.patch.object(
                    installer.report_bundle_config, "validate", return_value=ok_config))
                stack.enter_context(mock.patch.object(
                    installer.user_config, "status", return_value=[{"ok": True, "reading": "ok", "path": "x"}]))
                stack.enter_context(mock.patch.object(
                    installer.bootstrap, "compute_hosts_status", return_value={"status": "missing", "target": "x"}))
                stack.enter_context(mock.patch.object(installer.bootstrap, "compute_hosts_expected", return_value=False))
                result = installer.cmd_verify(args)
            row = next(c for c in result["checks"] if c["id"] == "host.pyyaml")
            self.assertEqual((row["ok"], result["exit"]), (ok, code), row)
            if not ok:
                self.assertIn("-m pip install --user pyyaml", row["detail"])


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
            stack.enter_context(mock.patch.object(
                installer.node_runtime, "ensure_node", return_value={
                    "id": "host.node-runtime", "status": "warning",
                    "detail": "node install failed: offline",
                }
            ))
            result = installer.cmd_install(args)
        self.assertEqual(result["exit"], installer.EXIT_OK)
        self.assertEqual(
            [c for c in result["checks"] if c["id"].startswith(("environment", "host."))],
            [],
        )

    def test_dry_run_install_keeps_the_node_ensure_dry(self):
        args = SimpleNamespace(
            runtimes=["claude"], target=None, scope="global",
            plugin=False, dry_run=True, report_bundle_root=None,
        )
        driver = mock.Mock()
        driver.install.return_value = {"actions": [], "blocked": False}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                installer, "get_driver", return_value=driver
            ))
            stack.enter_context(mock.patch.object(
                installer.routing_config, "ensure", return_value={
                    "status": "would-create", "path": "/tmp/config",
                    "enabled": ["claude"],
                }))
            stack.enter_context(mock.patch.object(
                installer.report_bundle_config, "ensure", return_value={
                    "status": "would-create", "path": "/tmp/report-bundle.json",
                    "root": "/tmp/reports",
                }))
            stack.enter_context(mock.patch.object(
                installer.compute_hosts_config, "ensure", return_value={
                    "status": "would-create", "path": "/tmp/compute-hosts.yaml",
                }))
            stack.enter_context(mock.patch.object(
                installer.bootstrap, "install_launchers", return_value=[]
            ))
            stack.enter_context(mock.patch.object(
                installer.host_probes, "run", return_value=[]
            ))
            ensure = stack.enter_context(mock.patch.object(
                installer.node_runtime, "ensure_node", return_value={
                    "id": "host.node-runtime", "status": "would-install",
                    "detail": "no node >= 20.9.0 on PATH",
                }
            ))
            result = installer.cmd_install(args)
        ensure.assert_called_once_with(dry_run=True)
        self.assertEqual(result["exit"], installer.EXIT_OK)


if __name__ == "__main__":
    unittest.main(verbosity=2)
