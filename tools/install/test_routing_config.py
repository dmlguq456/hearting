import importlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import routing_config  # noqa: E402
import bootstrap  # noqa: E402
import paths  # noqa: E402

_DEFAULTS_SPEC = importlib.util.spec_from_file_location(
    "dispatch_defaults_for_routing_config_test",
    HERE.parents[1] / "utilities" / "dispatch-defaults.py",
)
DEFAULTS = importlib.util.module_from_spec(_DEFAULTS_SPEC)
_DEFAULTS_SPEC.loader.exec_module(DEFAULTS)


class RoutingConfigInstallTests(unittest.TestCase):
    def test_bootstrap_includes_mem_launcher(self):
        names = [row["name"] for row in bootstrap.install_launchers(dry_run=True)]
        self.assertIn("mem", names)

    def test_create_once_and_preserve_user_edits(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}, clear=False
        ), mock.patch.object(routing_config.shutil, "which", return_value="/bin/runtime"):
            first = routing_config.ensure(["claude", "codex", "opencode"])
            path = Path(first["path"])
            self.assertEqual(first["status"], "created")
            text = path.read_text(encoding="utf-8")
            self.assertIn("primary: [claude, codex]", text)
            self.assertIn("relief: [opencode]", text)
            self.assertTrue(routing_config.validate()["ok"])
            path.write_text(text + "# user edit\n", encoding="utf-8")
            second = routing_config.ensure(["claude"])
            self.assertEqual(second["status"], "preserved")
            self.assertTrue(path.read_text(encoding="utf-8").endswith("# user edit\n"))

    def test_single_opencode_install_stays_usable(self):
        text = routing_config.render(["opencode"])
        self.assertIn("primary: [opencode]", text)
        self.assertIn("relief: []", text)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}, clear=False
        ):
            path = Path(tmp) / "hearting" / "dispatch-defaults.yaml"
            path.parent.mkdir()
            path.write_text(text, encoding="utf-8")
            self.assertTrue(routing_config.validate()["ok"])

    def test_rendered_config_still_answers_the_shipped_capability_baseline(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}, clear=False
        ), mock.patch.object(routing_config.shutil, "which", return_value="/bin/runtime"):
            os.environ.pop("DISPATCH_DEFAULTS_CONFIG", None)
            created = routing_config.ensure(["claude", "codex", "opencode"])
            config = DEFAULTS.load_and_validate(
                created["path"], DEFAULTS.default_topology_path()
            )
        self.assertEqual(
            DEFAULTS.query_stage_affinity(config, "autopilot-code", "execute"), "diverse"
        )

    def test_doctor_reports_valid_for_a_sparse_rendered_config(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}, clear=False
        ), mock.patch.object(routing_config.shutil, "which", return_value="/bin/runtime"):
            os.environ.pop("DISPATCH_DEFAULTS_CONFIG", None)
            routing_config.ensure(["claude", "codex"])
            self.assertTrue(routing_config.validate()["ok"])

    def test_doctor_still_reports_invalid_for_a_broken_user_config(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}, clear=False
        ):
            os.environ.pop("DISPATCH_DEFAULTS_CONFIG", None)
            path = Path(tmp) / "hearting" / "dispatch-defaults.yaml"
            path.parent.mkdir()
            path.write_text("schema_version: 3\nharnesses:\n  enabled: []\n", encoding="utf-8")
            result = routing_config.validate()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
