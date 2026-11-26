"""Interactive Codex question feature activation stays within a temporary CODEX_HOME."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/install"))
import runtime_activation


class CodexQuestionConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-question-config-")
        self.home = Path(self.temp.name) / "codex-home"
        self.home.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def activate(self, initial):
        config = self.home / "config.toml"
        if initial is not None:
            config.write_text(initial, encoding="utf-8")
        old = runtime_activation._config_path
        runtime_activation._config_path = lambda *_args, **_kwargs: config
        try:
            result = runtime_activation._merge_codex_identity_config(ROOT, None, "global")
        finally:
            runtime_activation._config_path = old
        return config, result

    def test_existing_features_table_gets_only_the_question_key_and_deactivates_cleanly(self):
        original = '[features]\napps = false\n\n[model]\nname = "fixture"\n'
        config, result = self.activate(original)
        self.assertEqual(result["conflicts"], [])
        parsed = tomllib.loads(config.read_text(encoding="utf-8"))
        self.assertIs(parsed["features"]["default_mode_request_user_input"], True)
        self.assertFalse(parsed["features"]["apps"])
        self.assertEqual(parsed["model"]["name"], "fixture")
        old = runtime_activation._config_path
        runtime_activation._config_path = lambda *_args, **_kwargs: config
        try:
            runtime_activation._unmerge_codex_identity_config(
                {"managed_config": {"codex_identity": result["managed_config"]}}, "global"
            )
        finally:
            runtime_activation._config_path = old
        self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_existing_true_needs_no_feature_receipt(self):
        config, result = self.activate('[features]\ndefault_mode_request_user_input = true\n')
        self.assertEqual(result["conflicts"], [])
        self.assertIsNone(result["managed_config"]["feature"])
        self.assertEqual(tomllib.loads(config.read_text())["features"]["default_mode_request_user_input"], True)

    def test_feature_conflicts_preserve_user_bytes_and_merge_removable_identity(self):
        cases = (
            '[features]\ndefault_mode_request_user_input = false\n',
            '[features]\ndefault_mode_request_user_input = "true"\n',
            'features = { apps = false }\n',
            'features.apps = false\n',
            'features = false\n',
        )
        for original in cases:
            with self.subTest(original=original):
                self.home = Path(self.temp.name) / ("home-" + str(len(original)) + str(hash(original)))
                self.home.mkdir()
                config, result = self.activate(original)
                self.assertEqual(result["conflicts"], ["features.default_mode_request_user_input"])
                merged = config.read_text(encoding="utf-8")
                self.assertIn(original, merged)
                self.assertIn(runtime_activation.CODEX_IDENTITY_BEGIN, merged)
                self.assertIsNone(result["managed_config"]["feature"])
                old = runtime_activation._config_path
                runtime_activation._config_path = lambda *_args, **_kwargs: config
                try:
                    runtime_activation._unmerge_codex_identity_config(
                        {"managed_config": {"codex_identity": result["managed_config"]}}, "global"
                    )
                finally:
                    runtime_activation._config_path = old
                self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_a_repeated_activation_over_its_own_output_is_byte_identical(self):
        config, first = self.activate('model = "fixture"\n\n[features]\napps = false\n')
        once = config.read_bytes()
        old = runtime_activation._config_path
        runtime_activation._config_path = lambda *_args, **_kwargs: config
        try:
            second = runtime_activation._merge_codex_identity_config(
                ROOT, {"managed_config": {"codex_identity": first["managed_config"]}}, "global"
            )
        finally:
            runtime_activation._config_path = old
        self.assertEqual(config.read_bytes(), once)
        self.assertEqual(second["managed_config"], first["managed_config"])


if __name__ == "__main__":
    unittest.main()
