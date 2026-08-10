#!/usr/bin/env python3
from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "runtime_model_config", ROOT / "utilities" / "model_config.py"
)
assert SPEC and SPEC.loader
config = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = config
SPEC.loader.exec_module(config)


BASE = (
    'CFG_MODEL_PROFILE_DEEP=deep:xhigh\n'
    'CFG_TIER_DEEP_MODEL=shipped-model\n'
    'CFG_TIER_DEEP_EFFORT=xhigh\n'
)


class ModelConfigTest(unittest.TestCase):
    def make_root(self, adapter="claude", shipped=BASE):
        root = Path(tempfile.mkdtemp())
        shipped_path = root / "adapters" / adapter / "config" / "models.conf"
        shipped_path.parent.mkdir(parents=True)
        shipped_path.write_text(shipped, encoding="utf-8")
        return root

    def test_each_runtime_home_is_independent(self):
        for adapter, variable, suffix in (
            ("claude", "CLAUDE_CONFIG_DIR", "claude"),
            ("codex", "CODEX_HOME", "codex"),
            ("opencode", "XDG_CONFIG_HOME", "opencode"),
        ):
            with self.subTest(adapter=adapter):
                environ = {"HOME": "/tmp/isolated-home", variable: f"/tmp/{suffix}"}
                expected = Path(f"/tmp/{suffix}")
                if adapter == "opencode":
                    expected /= "opencode"
                self.assertEqual(config.user_path(adapter, environ=environ), expected / "agent-config/models.conf")

    def test_valid_user_file_wins_as_a_complete_file(self):
        root = self.make_root()
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(BASE + 'CFG_USER_EXTRA="literal"\n', encoding="utf-8")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual(receipt.source, "user")
        self.assertEqual(receipt.reason, "user-valid")
        self.assertEqual(values["CFG_USER_EXTRA"], "literal")

    def test_missing_incomplete_malformed_and_unsafe_user_files_fallback_whole_file(self):
        for text, reason in ((None, "user-missing"), ("CFG_TIER_DEEP_MODEL=user-only\n", "user-incomplete"),
                             ("CFG_MODEL_PROFILE_DEEP=bad+syntax\n", "user-malformed"),
                             ("$(touch sentinel)\n" + BASE, "user-malformed")):
            with self.subTest(reason=reason):
                root = self.make_root()
                home = root / "home"
                user = home / "agent-config" / "models.conf"
                if text is not None:
                    user.parent.mkdir(parents=True)
                    user.write_text(text, encoding="utf-8")
                values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
                self.assertEqual(receipt.reason, reason)
                self.assertEqual(values["CFG_TIER_DEEP_MODEL"], "shipped-model")

    def test_quoted_comments_extra_keys_and_no_merge(self):
        root = self.make_root()
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text('CFG_MODEL_PROFILE_DEEP="deep:xhigh" # comment\nCFG_TIER_DEEP_MODEL="user-model ; $(touch sentinel) `echo nope` O\'Brien"\nCFG_TIER_DEEP_EFFORT=xhigh\n', encoding="utf-8")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual(receipt.source, "user")
        self.assertIn("$(touch sentinel)", values["CFG_TIER_DEEP_MODEL"])
        self.assertNotIn("CFG_UNDECLARED", values)

    def test_shipped_failure_is_fatal(self):
        root = self.make_root(shipped="$(bad)\n")
        with self.assertRaises(config.ShippedConfigError):
            config.resolve_config("claude", runtime=root / "home", source_root=root)

    def test_user_symlink_is_rejected_and_falls_back(self):
        root = self.make_root()
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.symlink_to(root / "adapters" / "claude" / "config" / "models.conf")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual(receipt.source, "shipped")
        self.assertEqual(receipt.reason, "user-unreadable")
        self.assertEqual(values["CFG_TIER_DEEP_MODEL"], "shipped-model")

    def test_bridge_quotes_metacharacters_and_receipt(self):
        root = self.make_root()
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(BASE.replace("shipped-model", '"$(touch sentinel); `echo bad` O\'Brien"'), encoding="utf-8")
        receipt = root / "receipt.json"
        command = [str(ROOT / "utilities" / "model-config.sh"), "--adapter", "claude", "--runtime-home", str(home), "--source-root", str(root), "--receipt-fd", "3"]
        result = subprocess.run(shlex.join(command) + f" 3>{shlex.quote(str(receipt))}", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CFG_TIER_DEEP_MODEL='$(touch sentinel); `echo bad` O'\"'\"'Brien'", result.stdout)
        self.assertEqual(json.loads(receipt.read_text())["source"], "user")
        self.assertFalse((root / "sentinel").exists())


if __name__ == "__main__":
    unittest.main()
