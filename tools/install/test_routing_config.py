import importlib
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


if __name__ == "__main__":
    unittest.main()
