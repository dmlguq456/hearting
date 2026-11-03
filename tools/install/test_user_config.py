import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import claude_settings_config  # noqa: E402
import routing_config  # noqa: E402
import user_config  # noqa: E402


class UserConfigRegistryTests(unittest.TestCase):
    def _env(self, root):
        return mock.patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
             "XDG_STATE_HOME": str(root / "state"), "CLAUDE_CONFIG_DIR": str(root / "claude")},
            clear=True,
        )

    def test_unmanaged_permission_settings_are_not_in_the_registry(self):
        ids = [surface["id"] for surface in user_config.SURFACES]
        self.assertIn("session-tidy", ids)
        self.assertFalse([i for i in ids if i.startswith("claude-settings")], ids)

    def test_fresh_install_status_has_no_permission_drift_line(self):
        # The managed install merges hooks/statusLine/env/autoMemoryEnabled and never
        # writes permissions, so neither an unset nor a user-chosen permission mode
        # may surface as a `!` drift row in `config status`.
        for permissions in (None, {"defaultMode": "bypassPermissions"}):
            with self.subTest(permissions=permissions), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                settings = root / "claude" / "settings.json"
                settings.parent.mkdir(parents=True)
                data = {"hooks": {}, "autoMemoryEnabled": False}
                if permissions:
                    data["permissions"] = permissions
                settings.write_text(json.dumps(data), encoding="utf-8")
                before = settings.read_bytes()
                with self._env(root):
                    rendered = "\n".join(user_config.lines(user_config.status()))
                self.assertNotIn("defaultMode", rendered)
                self.assertNotIn("permissions", rendered)
                self.assertNotIn("!", rendered)
                self.assertEqual(settings.read_bytes(), before)

    def test_drift_in_a_managed_surface_is_still_reported(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}, clear=False
        ), mock.patch.object(routing_config.shutil, "which", return_value="/bin/runtime"):
            path = Path(routing_config.ensure(["claude", "codex"])["path"])
            path.write_text(
                path.read_text(encoding="utf-8").replace("strategy: balanced", "strategy: capacity-aware"),
                encoding="utf-8",
            )
            rows = user_config.status(["dispatch-defaults"])
            self.assertEqual(rows[0]["status"], "drift")
            self.assertTrue(user_config.lines(rows)[0].startswith("! dispatch-defaults"))

    def test_settings_module_only_migrates_retired_hooks(self):
        self.assertFalse(hasattr(claude_settings_config, "validate"))
        data = {"permissions": {"defaultMode": "plan"}, "hooks": {}}
        self.assertFalse(claude_settings_config.remove_retired_hooks(data))
        self.assertEqual(data["permissions"], {"defaultMode": "plan"})


if __name__ == "__main__":
    unittest.main()
