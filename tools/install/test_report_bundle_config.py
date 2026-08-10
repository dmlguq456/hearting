import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_bundle_config  # noqa: E402
import installer  # noqa: E402


class ReportBundleConfigTests(unittest.TestCase):
    def env(self, root):
        return mock.patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data")},
            clear=True,
        )

    def test_create_once_and_preserve_bytes(self):
        with tempfile.TemporaryDirectory() as td, self.env(Path(td)):
            requested = Path(td) / "store"
            first = report_bundle_config.ensure(requested)
            path = Path(first["path"])
            original = path.read_bytes()
            self.assertEqual(first["status"], "created")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(requested.is_dir())
            second = report_bundle_config.ensure(Path(td) / "other")
            self.assertEqual(second["status"], "preserved")
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(second["root"], str(requested))

    def test_env_override_precedes_config(self):
        with tempfile.TemporaryDirectory() as td, self.env(Path(td)):
            report_bundle_config.ensure(Path(td) / "stored")
            override = Path(td) / "override"
            with mock.patch.dict(os.environ, {"REPORT_BUNDLE_ROOT": str(override)}):
                self.assertEqual(report_bundle_config.resolve(), override)

    def test_relative_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as td, self.env(Path(td)):
            with self.assertRaisesRegex(ValueError, "absolute path"):
                report_bundle_config.ensure("relative")

    def test_invalid_existing_config_is_preserved_and_rejected(self):
        with tempfile.TemporaryDirectory() as td, self.env(Path(td)):
            path = report_bundle_config.config_path()
            path.parent.mkdir(parents=True)
            path.write_text('{"schema_version":99}\n', encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "schema"):
                report_bundle_config.ensure(Path(td) / "store")
            self.assertEqual(path.read_bytes(), before)

    def test_dry_run_does_not_create_config_or_root(self):
        with tempfile.TemporaryDirectory() as td, self.env(Path(td)):
            root = Path(td) / "store"
            result = report_bundle_config.ensure(root, dry_run=True)
            self.assertEqual(result["status"], "would-create")
            self.assertFalse(report_bundle_config.config_path().exists())
            self.assertFalse(root.exists())

    def test_installer_install_initializes_requested_root(self):
        with tempfile.TemporaryDirectory() as td, self.env(Path(td)), mock.patch.object(
            installer, "get_driver"
        ) as get_driver, mock.patch.object(
            installer.routing_config, "ensure",
            return_value={"status": "would-create", "path": "/tmp/routing", "enabled": ["claude"]},
        ), mock.patch.object(installer.bootstrap, "install_launchers", return_value=[]):
            get_driver.return_value.install.return_value = {"actions": [], "blocked": False}
            requested = Path(td) / "bundles"
            args = installer.build_parser().parse_args([
                "install", "claude", "--dry-run", "--report-bundle-root", str(requested)
            ])
            result = installer.cmd_install(args)
            check = next(row for row in result["checks"] if row["id"] == "report-bundle-config.root")
            self.assertTrue(check["ok"])
            self.assertIn("root=" + str(requested), "\n".join(result["lines"]))
            self.assertFalse(requested.exists())

    def test_installer_verify_reports_bundle_config_failure(self):
        args = installer.build_parser().parse_args(["verify", "claude"])
        with mock.patch.object(installer, "get_driver") as get_driver, mock.patch.object(
            installer.verifier, "run", return_value=[]
        ), mock.patch.object(
            installer.routing_config, "validate",
            return_value={"ok": True, "status": "valid", "path": "/tmp/routing"},
        ), mock.patch.object(
            installer.report_bundle_config, "validate",
            return_value={"ok": False, "status": "invalid", "path": "/tmp/bundle"},
        ):
            result = installer.cmd_verify(args)
        self.assertEqual(result["exit"], installer.EXIT_VERIFY_FAIL)
        self.assertFalse(next(row for row in result["checks"] if row["id"] == "report-bundle-config.root")["ok"])


if __name__ == "__main__":
    unittest.main()
