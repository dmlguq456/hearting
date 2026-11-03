"""The managed Codex footer apply adds current identity without replacing user settings."""
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "adapters/codex/bin/apply-tui-config.sh"
FRAGMENT = ROOT / "adapters/codex/config/tui-statusline.toml"


class ApplyTuiConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-tui-apply-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.codex_home = self.root / "codex"
        self.home.mkdir()
        self.codex_home.mkdir()
        self.config = self.codex_home / "config.toml"

    def apply_config(self):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.home),
            "CODEX_HOME": str(self.codex_home),
            "AGENT_HOME": str(ROOT),
            "LC_ALL": "C.UTF-8",
        }
        return subprocess.run([str(SCRIPT)], env=env, capture_output=True,
                              text=True, check=False)

    def test_custom_footer_and_unrelated_values_are_preserved(self):
        custom = ["project-name", "git-branch", "context-used", "model-with-reasoning",
                  "weekly-limit", "context-window-size", "approval-mode", "fast-mode",
                  "thread-title"]
        self.config.write_text(
            'daemon_auto_start = false\n'
            '[tui]\n'
            'status_line = [\n' + "".join(f'  "{item}",\n' for item in custom) + ']\n'
            'status_line_use_colors = false\n'
            'notifications = false\n'
            '\n[[mcp_servers.fixture]]\n'
            'command = "keep-this"\n',
            encoding="utf-8",
        )

        result = self.apply_config()
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(parsed["tui"]["status_line"], ["thread-id", *custom])
        self.assertIs(parsed["tui"]["status_line_use_colors"], False)
        self.assertIs(parsed["tui"]["notifications"], False)
        self.assertIs(parsed["daemon_auto_start"], False)
        self.assertEqual(parsed["mcp_servers"]["fixture"][0]["command"], "keep-this")

    def test_symlinked_config_keeps_link_and_updates_target(self):
        target = self.root / "shared" / "config.toml"
        target.parent.mkdir()
        original = ('model = "preserve"\n[tui]\n'
                    'status_line = ["project-name", "thread-id", "custom"]\n'
                    'status_line_use_colors = false\n').encode()
        target.write_bytes(original)
        link_target = os.path.relpath(target, self.codex_home)
        self.config.symlink_to(link_target)

        first = self.apply_config()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(os.readlink(self.config), link_target)
        self.assertEqual((self.codex_home / "config.toml.pre-harness-tui").read_bytes(), original)
        parsed = tomllib.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(parsed["model"], "preserve")
        self.assertEqual(parsed["tui"]["status_line"],
                         ["thread-id", "project-name", "custom"])
        self.assertIs(parsed["tui"]["status_line_use_colors"], False)
        once = target.read_bytes()

        second = self.apply_config()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("changed=no", second.stdout)
        self.assertEqual(target.read_bytes(), once)
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(os.readlink(self.config), link_target)

    def test_existing_thread_id_is_not_duplicated_and_second_apply_is_noop(self):
        self.config.write_text(
            'daemon_auto_start = false\n[tui]\n'
            'status_line = ["thread-id", "custom-item"]\n'
            'status_line_use_colors = false\n',
            encoding="utf-8",
        )
        first = self.apply_config()
        self.assertEqual(first.returncode, 0, first.stderr)
        first_text = self.config.read_text(encoding="utf-8")
        second = self.apply_config()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("changed=no", second.stdout)
        self.assertEqual(self.config.read_text(encoding="utf-8"), first_text)
        self.assertEqual(tomllib.loads(first_text)["tui"]["status_line"],
                         ["thread-id", "custom-item"])

    def test_new_config_uses_managed_defaults_with_thread_id_first(self):
        result = self.apply_config()
        self.assertEqual(result.returncode, 0, result.stderr)
        defaults = tomllib.loads(FRAGMENT.read_text(encoding="utf-8"))["tui"]
        applied = tomllib.loads(self.config.read_text(encoding="utf-8"))["tui"]
        self.assertEqual(applied, defaults)
        self.assertEqual(applied["status_line"][0], "thread-id")

    def test_thread_id_moves_first_and_other_items_keep_order(self):
        for items in (("project-name", "thread-title", "custom-item", "thread-id"),
                      ("project-name", "thread-id", "thread-title", "custom-item"),
                      ("project-name", "thread-title", "custom-item", "thread-id", "thread-id")):
            with self.subTest(items=items):
                self.config.write_text('[tui]\nstatus_line = ' + repr(list(items)).replace("'", '"')
                                       + '\nstatus_line_use_colors = false\n')
                result = self.apply_config()
                self.assertEqual(result.returncode, 0, result.stderr)
                applied = tomllib.loads(self.config.read_text())["tui"]
                self.assertEqual(applied["status_line"],
                                 ["thread-id"] + [item for item in items if item != "thread-id"])
                self.assertIs(applied["status_line_use_colors"], False)

    def test_explicit_empty_and_invalid_config_are_handled_without_loss(self):
        self.config.write_text('[tui]\nstatus_line = []\nstatus_line_use_colors = false\n')
        result = self.apply_config()
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = tomllib.loads(self.config.read_text())["tui"]
        self.assertEqual(parsed["status_line"], ["thread-id"])
        self.assertIs(parsed["status_line_use_colors"], False)

        for invalid in ("[tui\nstatus_line = [\"custom\"]\n", "[tui]\nstatus_line = 7\n"):
            with self.subTest(invalid=invalid):
                before = invalid.encode()
                self.config.write_bytes(before)
                result = self.apply_config()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.config.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
