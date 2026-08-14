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

    def test_ac9_opencode_rejected_in_deep_quality_bands(self):
        # AC 9 band placement gate: opencode in deep/balanced-deep primary is a
        # validation error; opencode in light.primary stays legal.
        capmap = D.load_topology_capabilities(D.default_topology_path())
        for band in ("deep", "balanced-deep"):
            config = self.config()
            config["profiles"][band]["primary"] = ["claude", "codex", "opencode"]
            config["profiles"][band]["last_resort"] = []
            errors = D.validate(config, capmap)
            self.assertTrue(
                any(band in error and "must not include opencode" in error for error in errors),
                f"missing AC 9 rejection for {band}: {errors}",
            )
        config = self.config()
        config["profiles"]["light"]["primary"] = ["claude", "codex", "opencode"]
        config["profiles"]["light"]["relief"] = []
        self.assertEqual(D.validate(config, capmap), [])

    def test_ac10_quality_peer_set_follows_the_config(self):
        # AC 10: the quality-peer derivation is config-driven, never hardcoded.
        # Moving a family out of a deep band moves the derived set with it.
        self.assertEqual(
            D.query_profile_policy(self.config(), "deep")["primary"], ["claude", "codex"]
        )
        self.assertEqual(
            D.query_profile_policy(self.config(), "balanced-deep")["primary"], ["claude", "codex"]
        )
        peer = importlib.util.spec_from_file_location(
            "dispatch_quality_peer_under_test",
            Path(__file__).with_name("dispatch_quality_peer.py"),
        )
        QP = importlib.util.module_from_spec(peer)
        peer.loader.exec_module(QP)
        config = self.config()
        by_profile = {name: D.query_profile_policy(config, name) for name in ("deep", "balanced-deep", "light")}
        self.assertEqual(QP.quality_peer_families(by_profile), frozenset({"claude", "codex"}))
        config["profiles"]["deep"]["primary"] = ["claude"]
        config["profiles"]["deep"]["relief"] = ["codex", "opencode"]
        config["profiles"]["deep"]["last_resort"] = []
        by_profile = {name: D.query_profile_policy(config, name) for name in ("deep", "balanced-deep", "light")}
        self.assertEqual(QP.quality_peer_families(by_profile), frozenset({"claude"}))
        self.assertIsNone(QP.quality_peer_families(None))


class ShippedBaselineMergeTests(unittest.TestCase):
    def sparse_v3_text(self):
        return (
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

    def _write(self, tmp, text):
        path = Path(tmp) / "dispatch-defaults.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_sparse_user_config_still_answers_the_shipped_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, self.sparse_v3_text())
            config = D.load_and_validate(str(path), D.default_topology_path())
        self.assertEqual(D.query_stage_affinity(config, "autopilot-code", "frame"), "diverse")
        self.assertEqual(D.query_affinity(config, "autopilot-code", "frame"), "diverse")

    def test_user_cell_wins_and_siblings_fall_back_to_the_baseline(self):
        text = self.sparse_v3_text().replace(
            "capabilities:\n", "capabilities:\n  autopilot-code:\n    frame: claude\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, text)
            config = D.load_and_validate(str(path), D.default_topology_path())
        self.assertEqual(D.query_stage_affinity(config, "autopilot-code", "frame"), "claude")
        self.assertEqual(D.query_stage_affinity(config, "autopilot-code", "plan"), "diverse")

    def test_baseline_cell_naming_a_disabled_harness_is_dropped(self):
        config = D.parse_yaml_subset(self.sparse_v3_text())
        config["harnesses"]["enabled"] = ["claude"]
        capmap = D.load_topology_capabilities(D.default_topology_path())
        merged = D.merge_capability_baseline(
            config, capmap, baseline={"autopilot-code": {"frame": "codex"}}
        )
        self.assertNotIn("frame", merged["capabilities"].get("autopilot-code", {}))

    def test_baseline_cell_for_an_unknown_capability_or_stage_is_dropped(self):
        config = D.parse_yaml_subset(self.sparse_v3_text())
        capmap = D.load_topology_capabilities(D.default_topology_path())
        merged = D.merge_capability_baseline(
            config, capmap,
            baseline={
                "unknown-capability": {"frame": "diverse"},
                "autopilot-code": {"unknown-stage": "diverse"},
            },
        )
        self.assertNotIn("unknown-capability", merged["capabilities"])
        self.assertNotIn("unknown-stage", merged["capabilities"].get("autopilot-code", {}))

    def test_shipped_baseline_is_not_merged_into_itself(self):
        config = D.load_and_validate(D.SHIPPED_CONFIG_PATH, D.default_topology_path())
        with open(D.SHIPPED_CONFIG_PATH, encoding="utf-8") as f:
            parsed = D.parse_yaml_subset(f.read())
        self.assertEqual(config, parsed)

    def test_shipped_baseline_validates_against_the_current_topology(self):
        capmap = D.load_topology_capabilities(D.default_topology_path())
        with open(D.SHIPPED_CONFIG_PATH, encoding="utf-8") as f:
            parsed = D.parse_yaml_subset(f.read())
        self.assertEqual(D.validate(parsed, capmap), [])

    def test_invalid_user_config_still_fails_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "schema_version: 3\nharnesses:\n  enabled: []\n")
            with self.assertRaises(D.DefaultsConfigError):
                D.load_and_validate(str(path), D.default_topology_path())


if __name__ == "__main__":
    unittest.main()
