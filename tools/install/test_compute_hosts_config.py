import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compute_hosts_config  # noqa: E402
import user_config  # noqa: E402


class ComputeHostsConfigTests(unittest.TestCase):
    def env(self, root):
        return mock.patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
             "XDG_STATE_HOME": str(root / "state")},
            clear=True,
        )

    def test_seed_once_template_then_preserve(self):
        with tempfile.TemporaryDirectory() as td, self.env(Path(td)):
            first = compute_hosts_config.ensure()
            path = Path(first["path"])
            self.assertEqual(first["status"], "created")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            original = path.read_bytes()
            self.assertIn("gpu-a:", original.decode())
            self.assertIn("gpu-b:", original.decode())
            self.assertEqual(compute_hosts_config.ensure()["status"], "preserved")
            self.assertEqual(path.read_bytes(), original)

    def test_states_missing_template_invalid_valid(self):
        with tempfile.TemporaryDirectory() as td, self.env(Path(td)):
            self.assertEqual(compute_hosts_config.validate()["status"], "missing")
            self.assertTrue(compute_hosts_config.validate()["ok"])
            compute_hosts_config.ensure()
            report = compute_hosts_config.validate()
            self.assertEqual((report["status"], report["ok"]), ("template", True))
            self.assertIn("edit", report["detail"])
            path = compute_hosts_config.config_path()
            with path.open("a", encoding="utf-8") as handle:
                handle.write("  gpu-a:\n    ssh_host: local\n")
            report = compute_hosts_config.validate()
            self.assertEqual((report["status"], report["ok"]), ("invalid", False))
            with path.open("a", encoding="utf-8") as handle:
                handle.write("run_root: /shared/runs\n")
            report = compute_hosts_config.validate()
            self.assertEqual((report["status"], report["hosts"]), ("valid", ["gpu-a"]))

    def test_dry_run_and_env_override(self):
        with tempfile.TemporaryDirectory() as td, self.env(Path(td)):
            plan = compute_hosts_config.ensure(dry_run=True)
            self.assertEqual(plan["status"], "would-create")
            self.assertFalse(Path(plan["path"]).exists())
            override = Path(td) / "elsewhere.yaml"
            with mock.patch.dict(os.environ, {"COMPUTE_HOSTS_CONFIG": str(override)}):
                self.assertEqual(Path(compute_hosts_config.ensure()["path"]), override)
                self.assertEqual(compute_hosts_config.validate()["status"], "template")

    def test_registry_reads_every_surface_and_only_invalid_fails(self):
        with tempfile.TemporaryDirectory() as td, self.env(Path(td)):
            rows = {row["id"]: row for row in user_config.status()}
            self.assertEqual(
                set(rows),
                {"compute-hosts", "dispatch-defaults", "report-bundle", "memory-sync",
                 "models-conf.claude", "models-conf.codex", "models-conf.opencode",
                 "claude-settings.defaultMode"})
            self.assertEqual(rows["compute-hosts"]["status"], "missing")
            self.assertTrue(rows["compute-hosts"]["ok"])
            self.assertEqual(rows["memory-sync"]["status"], "absent")
            self.assertTrue(rows["memory-sync"]["ok"])
            self.assertFalse(rows["dispatch-defaults"]["ok"])
            self.assertEqual(rows["models-conf.opencode"]["status"], "shipped-default")
            text = "\n".join(user_config.lines(user_config.status()))
            self.assertIn("seeded by: harness install", text)
            self.assertIn("harness memory join", text)


if __name__ == "__main__":
    unittest.main()
