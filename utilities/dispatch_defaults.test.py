#!/usr/bin/env python3
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


MODULE = Path(__file__).with_name("dispatch-defaults.py")
SPEC = importlib.util.spec_from_file_location("dispatch_defaults_under_test", MODULE)
D = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(D)


class DispatchDefaultsV3Tests(unittest.TestCase):
    def config(self):
        return D.parse_yaml_subset(
            "schema_version: 3\n"
            "harnesses:\n  enabled: [claude, codex, opencode]\n"
            "profiles:\n"
            "  deep:\n    primary: [claude, codex]\n    relief: []\n"
            "    last_resort: [opencode]\n    promote_relief_below: 0\n"
            "  balanced-deep:\n    primary: [claude, codex]\n    relief: []\n"
            "    last_resort: [opencode]\n    promote_relief_below: 0\n"
            "  light:\n    primary: [claude, codex]\n    relief: [opencode]\n"
            "    last_resort: []\n    promote_relief_below: 35\n"
            "  mini:\n    primary: [claude, codex]\n    relief: [opencode]\n"
            "    last_resort: []\n    promote_relief_below: 35\n"
            "allocation:\n  strategy: capacity-aware\n  window: 30\n"
            "capabilities:\n"
        )

    def test_repo_v3_policy_validates(self):
        config = self.config()
        self.assertEqual(D.validate(config, D.load_topology_capabilities(D.default_topology_path())), [])
        self.assertEqual(D.query_profile_policy(config, "deep")["primary"], ["claude", "codex"])
        self.assertEqual(D.query_profile_policy(config, "light")["relief"], ["opencode"])

    def test_each_enabled_harness_must_appear_once_per_profile(self):
        config = self.config()
        config["profiles"]["light"]["relief"] = []
        errors = D.validate(config, D.load_topology_capabilities(D.default_topology_path()))
        self.assertTrue(any("every enabled harness exactly once" in error for error in errors))

    def test_user_local_config_precedes_repo_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hearting" / "dispatch-defaults.yaml"
            path.parent.mkdir()
            path.write_text("schema_version: 3\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}, clear=False):
                os.environ.pop("DISPATCH_DEFAULTS_CONFIG", None)
                self.assertEqual(Path(D.default_config_path()), path)


if __name__ == "__main__":
    unittest.main()
