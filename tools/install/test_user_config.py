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
import paths  # noqa: E402


class ClaudeSettingsConfigTests(unittest.TestCase):
    def _write(self, path, default_mode):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"permissions": {"defaultMode": default_mode}}),
            encoding="utf-8",
        )

    def test_equal_default_modes_are_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "template" / "settings.json"
            user = Path(tmp) / "claude-home" / "settings.json"
            self._write(template, "auto")
            self._write(user, "auto")
            with mock.patch.object(claude_settings_config, "template_path", return_value=template), \
                 mock.patch.object(claude_settings_config, "user_path", return_value=user):
                result = claude_settings_config.validate()
            self.assertEqual(result["status"], "valid")
            self.assertTrue(result["ok"])
            self.assertEqual(result["path"], str(user))

    def test_different_default_modes_are_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "template" / "settings.json"
            user = Path(tmp) / "claude-home" / "settings.json"
            self._write(template, "auto")
            self._write(user, "bypassPermissions")
            with mock.patch.object(claude_settings_config, "template_path", return_value=template), \
                 mock.patch.object(claude_settings_config, "user_path", return_value=user):
                result = claude_settings_config.validate()
            self.assertEqual(result["status"], "drift")
            self.assertTrue(result["ok"])
            self.assertIn("auto", result["detail"])
            self.assertIn("bypassPermissions", result["detail"])

    def test_user_file_absent_is_absent_and_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "template" / "settings.json"
            user = Path(tmp) / "claude-home" / "settings.json"
            self._write(template, "auto")
            with mock.patch.object(claude_settings_config, "template_path", return_value=template), \
                 mock.patch.object(claude_settings_config, "user_path", return_value=user):
                result = claude_settings_config.validate()
            self.assertEqual(result["status"], "absent")
            self.assertTrue(result["ok"])

    def test_malformed_user_file_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "template" / "settings.json"
            user = Path(tmp) / "claude-home" / "settings.json"
            self._write(template, "auto")
            user.parent.mkdir(parents=True, exist_ok=True)
            user.write_text("{not json", encoding="utf-8")
            with mock.patch.object(claude_settings_config, "template_path", return_value=template), \
                 mock.patch.object(claude_settings_config, "user_path", return_value=user):
                result = claude_settings_config.validate()
            self.assertEqual(result["status"], "invalid")
            self.assertFalse(result["ok"])

    def test_status_never_writes_or_touches_the_user_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "template" / "settings.json"
            user = Path(tmp) / "claude-home" / "settings.json"
            self._write(template, "auto")
            self._write(user, "auto")
            before_bytes = user.read_bytes()
            before_mtime = user.stat().st_mtime_ns
            with mock.patch.object(claude_settings_config, "template_path", return_value=template), \
                 mock.patch.object(claude_settings_config, "user_path", return_value=user):
                claude_settings_config.validate()
            self.assertEqual(user.read_bytes(), before_bytes)
            self.assertEqual(user.stat().st_mtime_ns, before_mtime)

    def test_user_path_resolves_under_claude_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": tmp}, clear=False
        ):
            self.assertEqual(claude_settings_config.user_path(), Path(tmp) / "settings.json")

    def test_registered_in_user_config_surfaces(self):
        import user_config
        ids = [surface["id"] for surface in user_config.SURFACES]
        self.assertIn("claude-settings.defaultMode", ids)


if __name__ == "__main__":
    unittest.main()
