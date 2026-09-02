#!/usr/bin/env python3
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
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

    def test_allocation_warnings_report_strategy_drift_and_inert_keys(self):
        # Shipped default is balanced; a user file left on capacity-aware with
        # the depth-affinity keys appended validates but must not stay silent.
        config = self.config()
        config["harnesses"]["enabled"] = ["claude", "codex"]
        config["allocation"].update({
            "usage_gate_used_percent": 85,
            "depth_affinity": {"owner": "claude", "worker": "codex"},
            "depth_affinity_weight": 0.65,
            "usage_headroom_exponent": 2,
        })
        self.assertEqual(D.shipped_allocation_strategy(), "balanced")
        warnings = D.allocation_warnings(config, "/tmp/user-owned.yaml")
        self.assertTrue(any("allocation.strategy=capacity-aware differs from shipped default balanced" in w for w in warnings), warnings)
        self.assertTrue(any(w.startswith("allocation.usage_headroom_exponent is inert") for w in warnings), warnings)
        self.assertTrue(any(w.startswith("allocation.depth_affinity_weight is inert") for w in warnings), warnings)
        self.assertTrue(any(w.startswith("allocation.usage_gate_used_percent is inert") for w in warnings), warnings)
        # Adopting the shipped strategy clears every finding at once.
        config["allocation"]["strategy"] = "balanced"
        self.assertEqual(D.allocation_warnings(config, "/tmp/user-owned.yaml"), [])
        # The shipped file itself is never reported as drifting from itself.
        self.assertEqual(D.allocation_warnings(config, D.SHIPPED_CONFIG_PATH), [])

    def test_validate_cli_prints_warnings_but_stays_valid(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "dispatch-defaults.yaml"
            path.write_text(
                "schema_version: 3\nharnesses:\n  enabled: [claude, codex]\nprofiles:\n"
                + "".join(
                    f"  {profile}:\n    primary: [claude, codex]\n    relief: []\n    last_resort: []\n    promote_relief_below: 0\n"
                    for profile in ("deep", "balanced-deep", "light", "mini")
                )
                + "allocation:\n  strategy: capacity-aware\n  window: 30\n  usage_headroom_exponent: 2\ncapabilities:\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(MODULE), "validate", "--config", str(path)],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            lines = result.stdout.splitlines()
            self.assertTrue(lines[0].endswith("is valid"), lines)
            warnings = [line for line in lines if line.startswith("warning=")]
            self.assertEqual(len(warnings), 2, lines)
            self.assertIn("warning=allocation.strategy=capacity-aware differs from shipped default balanced", warnings[0])
            self.assertEqual(warnings[1], "warning=allocation.usage_headroom_exponent is inert: ignored under capacity-aware")
            shipped = subprocess.run(
                [sys.executable, str(MODULE), "validate", "--config", str(D.SHIPPED_CONFIG_PATH)],
                text=True, capture_output=True,
            )
            self.assertEqual(shipped.returncode, 0, shipped.stderr)
            self.assertNotIn("warning=", shipped.stdout)

    def test_repo_v3_policy_validates(self):
        config = self.config()
        self.assertEqual(D.validate(config, D.load_topology_capabilities(D.default_topology_path())), [])
        self.assertEqual(D.query_profile_policy(config, "deep")["primary"], ["claude", "codex"])
        self.assertEqual(D.query_profile_policy(config, "light")["relief"], ["opencode"])

    def test_scalar_parses_decimal_weight_as_float(self):
        value = D.parse_yaml_subset("allocation:\n  depth_affinity_weight: 0.65\n")["allocation"]["depth_affinity_weight"]
        self.assertEqual(value, 0.65)
        self.assertIs(type(value), float)

    def test_allocation_validation_rejects_invalid_affinity_weight_and_exponent(self):
        capmap = D.load_topology_capabilities(D.default_topology_path())
        cases = [
            ("depth_affinity_weight", 1.2, "depth_affinity_weight"),
            ("usage_headroom_exponent", 0, "usage_headroom_exponent"),
            ("depth_affinity", {"stage": "codex"}, "depth_affinity keys"),
            ("depth_affinity", {"owner": "opencode"}, "enabled harnesses"),
            ("depth_affinity_weight", True, "depth_affinity_weight"),
            ("usage_headroom_exponent", True, "usage_headroom_exponent"),
        ]
        for key, value, fragment in cases:
            config = self.config(); config["harnesses"]["enabled"] = ["claude", "codex"]
            config["allocation"][key] = value
            errors = D.validate(config, capmap)
            self.assertTrue(any(fragment in error for error in errors), (key, errors))

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

    def test_headless_permission_posture_defaults_to_bypass_and_validates(self):
        # core/OPERATIONS.md §5.10 registered headless permission posture.
        capmap = D.load_topology_capabilities(D.default_topology_path())
        config = self.config()
        self.assertEqual(D.validate(config, capmap), [])
        self.assertEqual(D.query_headless_policy(config),
                         {"claude_permission_mode": "bypass", "source": "shipped-default"})
        config["headless"] = {"claude_permission_mode": "allowlist"}
        self.assertEqual(D.validate(config, capmap), [])
        self.assertEqual(D.query_headless_policy(config),
                         {"claude_permission_mode": "allowlist", "source": "config"})
        config["headless"] = {"claude_permission_mode": "yolo"}
        self.assertTrue(any("headless.claude_permission_mode" in e for e in D.validate(config, capmap)))
        config["headless"] = {"permission": "bypass"}
        self.assertTrue(any("unknown headless key" in e for e in D.validate(config, capmap)))
        config["headless"] = "bypass"
        self.assertTrue(any("headless must be a mapping" in e for e in D.validate(config, capmap)))

    def test_shipped_config_declares_the_bypass_posture_explicitly(self):
        shipped = D.parse_yaml_subset(Path(D.SHIPPED_CONFIG_PATH).read_text(encoding="utf-8"))
        self.assertEqual(D.query_headless_policy(shipped),
                         {"claude_permission_mode": "bypass", "source": "config"})

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

    def test_empty_deep_primary_band_is_rejected(self):
        # fm M3 / anchor M2: the quality-peer set is
        # `deep.primary & balanced-deep.primary`, so an EMPTY primary band makes
        # it empty and nullifies the very gate the config is supposed to define.
        # Such a config passed validation before -- every enabled harness still
        # appears exactly once, just in relief/last_resort -- so the rule has to
        # live here, where the band is defined, not at its two consumers.
        capmap = D.load_topology_capabilities(D.default_topology_path())
        for band in ("deep", "balanced-deep"):
            config = self.config()
            config["profiles"][band]["relief"] = ["claude", "codex"]
            config["profiles"][band]["primary"] = []
            errors = D.validate(config, capmap)
            # the coverage rule alone would have accepted this
            self.assertFalse(
                any("every enabled harness exactly once" in error for error in errors),
                errors,
            )
            self.assertTrue(
                any(band in error and "must name at least one harness" in error
                    for error in errors),
                f"empty {band}.primary accepted: {errors}",
            )
        # `light` may legitimately empty its primary band; it is not a
        # quality-peer band.
        config = self.config()
        config["profiles"]["light"]["relief"] = ["claude", "codex", "opencode"]
        config["profiles"]["light"]["primary"] = []
        self.assertEqual(D.validate(config, capmap), [])

    def test_disjoint_deep_primary_bands_are_rejected(self):
        # M6: rejecting the EMPTY band closed one spelling of the hole. The
        # quality-peer set is the INTERSECTION of the two bands, so two
        # non-empty but DISJOINT bands nullify it just as completely and pass
        # the coverage rule identically. After the AC 11 fix that is worse than
        # before: the derived set is an empty frozenset rather than None, so
        # `sole_gate` starts "ok", the gated list empties, and every
        # peer-bearing parallel group is refused route-wide with a message that
        # blames harness availability instead of this config.
        capmap = D.load_topology_capabilities(D.default_topology_path())
        config = self.config()
        threshold = config["profiles"]["deep"]["promote_relief_below"]
        config["profiles"]["deep"] = {
            "primary": ["claude"], "relief": ["codex"], "last_resort": ["opencode"],
            "promote_relief_below": threshold,
        }
        config["profiles"]["balanced-deep"] = {
            "primary": ["codex"], "relief": ["claude"], "last_resort": ["opencode"],
            "promote_relief_below": threshold,
        }
        errors = D.validate(config, capmap)
        # neither existing rule sees it: every harness still appears exactly
        # once, and neither band is empty
        self.assertFalse(
            any("every enabled harness exactly once" in error for error in errors),
            errors,
        )
        self.assertFalse(
            any("must name at least one harness" in error for error in errors), errors
        )
        self.assertTrue(
            any("must share at least one harness" in error for error in errors),
            f"disjoint deep bands accepted: {errors}",
        )
        # and this is what the accepted config would have derived
        peer = importlib.util.spec_from_file_location(
            "dispatch_quality_peer_under_test",
            Path(__file__).with_name("dispatch_quality_peer.py"),
        )
        QP = importlib.util.module_from_spec(peer)
        peer.loader.exec_module(QP)
        self.assertEqual(
            QP.quality_peer_families({
                "deep": config["profiles"]["deep"],
                "balanced-deep": config["profiles"]["balanced-deep"],
            }),
            frozenset(),
        )
        # one shared harness is enough; asymmetric bands stay legal
        config["profiles"]["balanced-deep"] = {
            "primary": ["claude", "codex"], "relief": [], "last_resort": ["opencode"],
            "promote_relief_below": threshold,
        }
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


class DispatchDefaultsV4Tests(unittest.TestCase):
    """SD-123/SD-122: schema v4 adds `confirmation.mode` and
    `steward.child_permission_mode`, additive over v3 -- a v3 config (or one
    missing these blocks entirely) must keep validating and the accessors
    must keep returning their defaults."""

    def v3_config(self):
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

    def v4_config(self):
        config = self.v3_config()
        config["schema_version"] = 4
        return config

    @property
    def capmap(self):
        return D.load_topology_capabilities(D.default_topology_path())

    def test_v3_config_without_new_blocks_validates_and_defaults(self):
        config = self.v3_config()
        self.assertEqual(D.validate(config, self.capmap), [])
        self.assertEqual(D.query_confirmation_mode(config), "hybrid")
        self.assertEqual(D.query_steward_child_permission_mode(config), "bypass")

    def test_v3_config_with_new_blocks_is_rejected(self):
        config = self.v3_config()
        config["confirmation"] = {"mode": "both"}
        errors = D.validate(config, self.capmap)
        self.assertTrue(any("confirmation requires schema_version 4" in e for e in errors), errors)
        config = self.v3_config()
        config["steward"] = {"child_permission_mode": "inherit"}
        errors = D.validate(config, self.capmap)
        self.assertTrue(any("steward requires schema_version 4" in e for e in errors), errors)

    def test_v4_config_validates_and_still_answers_v3_shaped_queries(self):
        config = self.v4_config()
        self.assertEqual(D.validate(config, self.capmap), [])
        self.assertEqual(D.query_owners(config), ["claude", "codex", "opencode"])
        self.assertEqual(D.query_profile_policy(config, "deep")["primary"], ["claude", "codex"])
        allocation = D.query_allocation(config)
        self.assertEqual(allocation["strategy"], "capacity-aware")

    def test_v4_config_without_new_blocks_defaults(self):
        config = self.v4_config()
        self.assertNotIn("confirmation", config)
        self.assertNotIn("steward", config)
        self.assertEqual(D.query_confirmation_mode(config), "hybrid")
        self.assertEqual(D.query_steward_child_permission_mode(config), "bypass")

    def test_absent_config_defaults(self):
        self.assertEqual(D.query_confirmation_mode({}), "hybrid")
        self.assertEqual(D.query_steward_child_permission_mode({}), "bypass")

    def test_every_valid_confirmation_mode_and_steward_mode_validates(self):
        for mode in ("hybrid", "both", "post-frame-only"):
            config = self.v4_config()
            config["confirmation"] = {"mode": mode}
            self.assertEqual(D.validate(config, self.capmap), [])
            self.assertEqual(D.query_confirmation_mode(config), mode)
        for mode in ("bypass", "inherit"):
            config = self.v4_config()
            config["steward"] = {"child_permission_mode": mode}
            self.assertEqual(D.validate(config, self.capmap), [])
            self.assertEqual(D.query_steward_child_permission_mode(config), mode)

    def test_every_invalid_enum_value_is_rejected(self):
        config = self.v4_config()
        config["confirmation"] = {"mode": "always"}
        errors = D.validate(config, self.capmap)
        self.assertTrue(any("confirmation.mode must be one of" in e for e in errors), errors)
        # an invalid value never falls back to the default silently
        self.assertEqual(D.query_confirmation_mode(config), "hybrid")

        config = self.v4_config()
        config["steward"] = {"child_permission_mode": "root"}
        errors = D.validate(config, self.capmap)
        self.assertTrue(any("steward.child_permission_mode must be one of" in e for e in errors), errors)
        self.assertEqual(D.query_steward_child_permission_mode(config), "bypass")

    def test_unknown_keys_inside_new_blocks_are_rejected(self):
        config = self.v4_config()
        config["confirmation"] = {"mode": "hybrid", "extra": 1}
        errors = D.validate(config, self.capmap)
        self.assertTrue(any("unknown confirmation key" in e for e in errors), errors)
        config = self.v4_config()
        config["steward"] = {"child_permission_mode": "bypass", "extra": 1}
        errors = D.validate(config, self.capmap)
        self.assertTrue(any("unknown steward key" in e for e in errors), errors)

    def test_shipped_baseline_is_schema_v4_and_validates(self):
        capmap = self.capmap
        with open(D.SHIPPED_CONFIG_PATH, encoding="utf-8") as f:
            parsed = D.parse_yaml_subset(f.read())
        self.assertEqual(parsed.get("schema_version"), 4)
        self.assertEqual(D.validate(parsed, capmap), [])
        self.assertEqual(D.query_confirmation_mode(parsed), "hybrid")
        self.assertEqual(D.query_steward_child_permission_mode(parsed), "bypass")


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
