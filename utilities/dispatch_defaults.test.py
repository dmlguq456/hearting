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
            ("owner_order", "claude", "owner_order"),
            ("owner_order", ["claude", "claude"], "owner_order"),
            ("owner_order", ["gemini"], "owner_order"),
            ("owner_order", [["claude"]], "owner_order"),
            ("depth_affinity_weight", 1.2, "depth_affinity_weight"),
            ("usage_headroom_exponent", 0, "usage_headroom_exponent"),
            ("depth_affinity", {"stage": "codex"}, "depth_affinity keys"),
            ("depth_affinity", {"owner": "gemini"}, "known harnesses"),
            ("harness_weights", {"opencode": 0}, "harness_weights.opencode"),
            ("harness_weights", {"opencode": 1.5}, "harness_weights.opencode"),
            ("harness_weights", {"opencode": True}, "harness_weights.opencode"),
            ("harness_weights", {"gemini": 0.5}, "unknown harness"),
            ("harness_weights", "opencode: 0.3", "block mapping"),
            ("depth_affinity_weight", True, "depth_affinity_weight"),
            ("usage_headroom_exponent", True, "usage_headroom_exponent"),
        ]
        for key, value, fragment in cases:
            config = self.config(); config["harnesses"]["enabled"] = ["claude", "codex"]
            config["allocation"][key] = value
            errors = D.validate(config, capmap)
            self.assertTrue(any(fragment in error for error in errors), (key, errors))

    def test_a_harness_left_out_of_a_profile_is_normal_not_a_finding(self):
        # "This profile does not use that harness" (for example OpenCode absent
        # from deep) is a valid choice, never an error or a warning.
        config = self.config()
        config["profiles"]["light"]["relief"] = []
        capmap = D.load_topology_capabilities(D.default_topology_path())
        self.assertEqual(D.validate(config, capmap), [])
        self.assertEqual(D.policy_warnings(config), [])
        self.assertEqual(D.normalize_policy(config)["profiles"]["light"]["relief"], [])

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

    def test_opencode_in_a_deep_primary_band_warns_but_validates(self):
        capmap = D.load_topology_capabilities(D.default_topology_path())
        for band in ("deep", "balanced-deep"):
            config = self.config()
            config["profiles"][band]["primary"] = ["claude", "codex", "opencode"]
            config["profiles"][band]["last_resort"] = []
            self.assertEqual(D.validate(config, capmap), [])
            warnings = D.policy_warnings(config)
            self.assertTrue(
                any(band in w and "includes opencode" in w for w in warnings), warnings
            )
            # W3 changes nothing: the user's band is followed as written.
            self.assertEqual(
                D.normalize_policy(config)["profiles"][band]["primary"],
                ["claude", "codex", "opencode"],
            )
        config = self.config()
        config["profiles"]["light"]["primary"] = ["claude", "codex", "opencode"]
        config["profiles"]["light"]["relief"] = []
        self.assertEqual(D.validate(config, capmap), [])
        self.assertEqual(D.policy_warnings(config), [])

    def test_empty_deep_primary_band_warns_and_names_the_peer_gate_outcome(self):
        capmap = D.load_topology_capabilities(D.default_topology_path())
        for band in ("deep", "balanced-deep"):
            config = self.config()
            config["profiles"][band]["relief"] = ["claude", "codex"]
            config["profiles"][band]["primary"] = []
            self.assertEqual(D.validate(config, capmap), [])
            warnings = D.policy_warnings(config)
            self.assertTrue(
                any("is empty" in w and "sole-gate-non-peer-harness" in w for w in warnings),
                warnings,
            )
        # `light` may legitimately empty its primary band; it is not a
        # quality-peer band.
        config = self.config()
        config["profiles"]["light"]["relief"] = ["claude", "codex", "opencode"]
        config["profiles"]["light"]["primary"] = []
        self.assertEqual(D.validate(config, capmap), [])
        self.assertEqual(D.policy_warnings(config), [])

    def test_disjoint_deep_primary_bands_warn_and_derive_an_empty_peer_set(self):
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
        self.assertEqual(D.validate(config, capmap), [])
        warnings = D.policy_warnings(config)
        self.assertTrue(any("share no harness" in w for w in warnings), warnings)
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
        # one shared harness is enough; asymmetric bands stay legal and quiet
        config["profiles"]["balanced-deep"] = {
            "primary": ["claude", "codex"], "relief": [], "last_resort": ["opencode"],
            "promote_relief_below": threshold,
        }
        self.assertEqual(D.validate(config, capmap), [])
        self.assertEqual(D.policy_warnings(config), [])

    def test_opencode_only_setup_is_valid_without_warnings(self):
        text = (
            "schema_version: 3\n"
            "harnesses:\n  enabled: [opencode]\n"
            "profiles:\n"
            + "".join(
                f"  {name}:\n    primary: [opencode]\n    relief: []\n"
                "    last_resort: []\n    promote_relief_below: 0\n"
                for name in ("deep", "balanced-deep", "balanced", "light", "mini")
            )
            + "allocation:\n  strategy: balanced\n  window: 30\ncapabilities:\n"
        )
        config = D.parse_yaml_subset(text)
        capmap = D.load_topology_capabilities(D.default_topology_path())
        self.assertEqual(D.validate(config, capmap), [])
        self.assertEqual(D.policy_warnings(config), [])
        self.assertEqual(D.normalize_policy(config), config)
        self.assertEqual(D.query_owners(config), ["opencode"])
        for profile in ("deep", "balanced-deep", "balanced", "light", "mini"):
            with self.subTest(profile=profile):
                self.assertEqual(D.query_profile_policy(config, profile)["primary"], ["opencode"])

    def test_recommendation_findings_are_normalized_not_rejected(self):
        capmap = D.load_topology_capabilities(D.default_topology_path())
        config = self.config()
        config["allocation"]["owner_order"] = ["claude", "opencode", "codex"]
        config["harnesses"]["enabled"] = ["claude", "opencode"]
        # W1: a band naming a disabled harness; W2: a harness in two bands.
        config["profiles"]["deep"]["primary"] = ["claude", "codex"]
        config["profiles"]["deep"]["relief"] = ["claude"]
        # W5: a profile holding no enabled harness at all.
        config["profiles"]["mini"] = {
            "primary": ["codex"], "relief": [], "last_resort": [],
            "promote_relief_below": 0,
        }
        # W6: affinity / depth_affinity / harness_weights naming a disabled harness.
        config["capabilities"] = {"autopilot-code": {"execute": "codex"}}
        config["allocation"]["depth_affinity"] = {"owner": "codex", "worker": "claude"}
        config["allocation"]["harness_weights"] = {"codex": 0.5, "opencode": 0.3}
        self.assertEqual(D.validate(config, capmap), [])
        warnings = D.policy_warnings(config)
        joined = "\n".join(warnings)
        self.assertIn("profiles.deep.primary names disabled harness 'codex': ignored", joined)
        self.assertIn("repeats 'claude' in primary and relief", joined)
        self.assertIn("profiles.mini holds no enabled harness", joined)
        self.assertIn("affinity autopilot-code.execute targets disabled harness 'codex'", joined)
        self.assertIn("allocation.depth_affinity.owner names disabled harness 'codex'", joined)
        self.assertIn("allocation.harness_weights.codex names a disabled harness", joined)
        normalized = D.normalize_policy(config)
        self.assertEqual(normalized["profiles"]["deep"]["primary"], ["claude"])
        self.assertEqual(normalized["profiles"]["deep"]["relief"], [])
        self.assertEqual(normalized["profiles"]["mini"]["last_resort"], ["claude", "opencode"])
        self.assertEqual(normalized["capabilities"]["autopilot-code"], {})
        self.assertEqual(normalized["allocation"]["depth_affinity"], {"worker": "claude"})
        self.assertEqual(normalized["allocation"]["harness_weights"], {"opencode": 0.3})
        self.assertEqual(normalized["allocation"]["owner_order"], ["claude", "opencode"])
        self.assertEqual(D.query_allocation(normalized)["owner_order"], ["claude", "opencode"])
        # The input is never mutated; normalizing is idempotent.
        self.assertEqual(config["profiles"]["deep"]["primary"], ["claude", "codex"])
        self.assertEqual(D.normalize_policy(normalized), normalized)
        # Structural damage is still an error.
        broken = self.config()
        broken["profiles"]["deep"]["primary"] = ["claude", "gemini"]
        self.assertTrue(any("unknown harness" in e for e in D.validate(broken, capmap)))
        broken = self.config()
        broken["profiles"]["deep"]["primary"] = "claude"
        self.assertTrue(any("must be a list" in e for e in D.validate(broken, capmap)))

    def test_load_validated_returns_warnings_and_a_normalized_config(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "dispatch-defaults.yaml"
            path.write_text(
                "schema_version: 3\n"
                "harnesses:\n  enabled: [claude, codex, opencode]\n"
                "profiles:\n"
                "  deep:\n    primary: [claude, codex, opencode]\n    relief: []\n"
                "    last_resort: []\n    promote_relief_below: 0\n"
                "  balanced-deep:\n    primary: [claude, codex]\n    relief: []\n"
                "    last_resort: [opencode]\n    promote_relief_below: 0\n"
                "  light:\n    primary: [claude, codex, opencode]\n    relief: []\n"
                "    last_resort: []\n    promote_relief_below: 0\n"
                "  mini:\n    primary: [claude, codex, opencode]\n    relief: []\n"
                "    last_resort: []\n    promote_relief_below: 0\n"
                "allocation:\n  strategy: balanced\n  window: 30\n"
                "  harness_weights:\n    opencode: 0.3\n"
                "capabilities:\n",
                encoding="utf-8",
            )
            config, warnings = D.load_validated(path, D.default_topology_path())
            self.assertTrue(any("profiles.deep.primary includes opencode" in w for w in warnings), warnings)
            self.assertEqual(D.load_and_validate(path, D.default_topology_path()), config)
            self.assertEqual(D.query_allocation(config)["harness_weights"], {"opencode": 0.3})
            result = subprocess.run(
                [sys.executable, str(MODULE), "validate", "--config", str(path)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("warning=profiles.deep.primary includes opencode", result.stdout)
            allocation = subprocess.run(
                [sys.executable, str(MODULE), "allocation", "--config", str(path)],
                capture_output=True, text=True,
            )
            self.assertIn("harness_weights=opencode:0.3", allocation.stdout)
            # A structurally broken file is still refused (exit 65).
            path.write_text("schema_version: 3\nmystery: 1\n", encoding="utf-8")
            broken = subprocess.run(
                [sys.executable, str(MODULE), "validate", "--config", str(path)],
                capture_output=True, text=True,
            )
            self.assertEqual(broken.returncode, 65)

    def test_harness_weights_absent_leaves_the_allocation_bytes_unchanged(self):
        config = self.config()
        self.assertNotIn("harness_weights", D.query_allocation(config))
        config["allocation"]["harness_weights"] = {"opencode": 0.3}
        self.assertEqual(D.query_allocation(config)["harness_weights"], {"opencode": 0.3})

    def test_harness_weights_parses_only_as_a_block_mapping(self):
        block = D.parse_yaml_subset("allocation:\n  harness_weights:\n    opencode: 0.3\n")
        self.assertEqual(block["allocation"]["harness_weights"], {"opencode": 0.3})
        inline = D.parse_yaml_subset("allocation:\n  harness_weights: {opencode: 0.3}\n")
        self.assertIsInstance(inline["allocation"]["harness_weights"], str)
        capmap = D.load_topology_capabilities(D.default_topology_path())
        config = self.config()
        config["allocation"]["harness_weights"] = inline["allocation"]["harness_weights"]
        self.assertTrue(any("block mapping" in e for e in D.validate(config, capmap)))

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
        self.assertEqual(D.query_small_work_confirmation(config), "notice")

    def test_worker_account_is_optional_and_queries_only_the_named_harness(self):
        config = self.v4_config()
        self.assertEqual(D.validate(config, self.capmap), [])
        self.assertIsNone(D.query_worker_account(config, "claude"))
        config["worker_account"] = {
            "claude": "~/.claude-work",
            "codex": "/accounts/codex",
            "opencode": "accounts/opencode-data",
        }
        self.assertEqual(D.validate(config, self.capmap), [])
        self.assertEqual(D.query_worker_account(config, "claude"), "~/.claude-work")
        self.assertEqual(D.query_worker_account(config, "codex"), "/accounts/codex")
        self.assertEqual(D.query_worker_account(config, "opencode"), "accounts/opencode-data")
        self.assertIsNone(D.query_worker_account(config, "unknown"))

    def test_worker_account_requires_schema_v4_and_valid_harness_paths(self):
        config = self.v3_config()
        config["worker_account"] = {"claude": "/account"}
        self.assertTrue(any("worker_account requires schema_version 4" in error
                            for error in D.validate(config, self.capmap)))

        for value, expected in (
            ([], "worker_account must be a mapping"),
            ({"future": "/account"}, "worker_account names an unknown harness"),
            ({"claude": "   "}, "worker_account.claude must be a non-empty path string"),
            ({"codex": None}, "worker_account.codex must be a non-empty path string"),
        ):
            with self.subTest(value=value):
                config = self.v4_config()
                config["worker_account"] = value
                self.assertTrue(any(expected in error for error in D.validate(config, self.capmap)))

    def test_default_config_path_uses_the_passed_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            passed_home = root / "passed-home"
            ambient_home = root / "ambient-home"
            passed_path = passed_home / ".config/hearting/dispatch-defaults.yaml"
            ambient_path = ambient_home / ".config/hearting/dispatch-defaults.yaml"
            passed_path.parent.mkdir(parents=True)
            ambient_path.parent.mkdir(parents=True)
            passed_path.write_text("passed\n", encoding="utf-8")
            ambient_path.write_text("ambient\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": str(ambient_home),
                                              "XDG_CONFIG_HOME": str(ambient_home / ".config")},
                                 clear=False):
                self.assertEqual(
                    Path(D.default_config_path(environ={"HOME": str(passed_home)})),
                    passed_path,
                )
                self.assertEqual(
                    Path(D.default_config_path(environ={})),
                    Path(D.SHIPPED_CONFIG_PATH),
                )

    def test_small_work_confirmation_is_validated_and_queried(self):
        """SD-136: `confirmation.small_work` (notice|card) rides the v4 block."""
        config = self.v4_config()
        config["confirmation"] = {"mode": "hybrid", "small_work": "card"}
        self.assertEqual(D.validate(config, self.capmap), [])
        self.assertEqual(D.query_small_work_confirmation(config), "card")
        config["confirmation"] = {"small_work": "notice"}
        self.assertEqual(D.validate(config, self.capmap), [])
        self.assertEqual(D.query_confirmation_mode(config), "hybrid")
        self.assertEqual(D.query_small_work_confirmation(config), "notice")
        config["confirmation"] = {"small_work": "never"}
        errors = D.validate(config, self.capmap)
        self.assertTrue(any("confirmation.small_work must be one of" in e for e in errors), errors)
        self.assertEqual(D.query_small_work_confirmation(config), "notice")
        shipped = D.parse_yaml_subset(
            (Path(D.__file__).resolve().parents[1] / "profiles" / "dispatch-defaults.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(D.query_small_work_confirmation(shipped), "notice")

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

    def test_steward_policy_defaults_match_the_shipped_file(self):
        defaults = {
            "child_permission_mode": "bypass",
            "check_interval_seconds": 7200,
            "max_supervisors_per_repo": 2,
        }
        self.assertEqual(D.query_steward_policy({}), defaults)
        self.assertEqual(D.query_steward_policy(self.v3_config()), defaults)
        with open(D.SHIPPED_CONFIG_PATH, encoding="utf-8") as f:
            shipped = D.parse_yaml_subset(f.read())
        self.assertEqual(D.validate(shipped, self.capmap), [])
        self.assertEqual(D.query_steward_policy(shipped), defaults)
        self.assertEqual(shipped["steward"]["check_interval_seconds"], 7200)
        self.assertEqual(shipped["steward"]["max_supervisors_per_repo"], 2)

    def steward_errors(self, key, value):
        config = self.v4_config()
        config["steward"] = {key: value}
        return D.validate(config, self.capmap)

    def test_check_interval_seconds_bounds(self):
        message = "steward.check_interval_seconds must be an integer from 600 to 86400"
        for bad in (599, 86401, 0, -1, True, False, "7200", 7200.0):
            errors = self.steward_errors("check_interval_seconds", bad)
            self.assertIn(message, errors, bad)
        for good in (600, 3600, 86400):
            self.assertEqual(self.steward_errors("check_interval_seconds", good), [], good)
            config = self.v4_config()
            config["steward"] = {"check_interval_seconds": good}
            self.assertEqual(D.query_steward_policy(config)["check_interval_seconds"], good)

    def test_max_supervisors_per_repo_bounds(self):
        message = "steward.max_supervisors_per_repo must be an integer from 1 to 8"
        for bad in (0, 9, -1, True, False, "2", 2.0):
            errors = self.steward_errors("max_supervisors_per_repo", bad)
            self.assertIn(message, errors, bad)
        for good in (1, 3, 8):
            self.assertEqual(self.steward_errors("max_supervisors_per_repo", good), [], good)
            config = self.v4_config()
            config["steward"] = {"max_supervisors_per_repo": good}
            self.assertEqual(D.query_steward_policy(config)["max_supervisors_per_repo"], good)

    def test_invalid_steward_values_never_leak_through_the_query(self):
        config = self.v4_config()
        config["steward"] = {"check_interval_seconds": 5, "max_supervisors_per_repo": True}
        policy = D.query_steward_policy(config)
        self.assertEqual(policy["check_interval_seconds"], 7200)
        self.assertEqual(policy["max_supervisors_per_repo"], 2)

    def write_user_config(self, path, interval, limit):
        shipped = Path(D.SHIPPED_CONFIG_PATH).read_text(encoding="utf-8")
        shipped = shipped.replace("check_interval_seconds: 7200", f"check_interval_seconds: {interval}")
        shipped = shipped.replace("max_supervisors_per_repo: 2", f"max_supervisors_per_repo: {limit}")
        path.write_text(shipped, encoding="utf-8")

    def test_load_steward_policy_reads_back_the_written_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dispatch-defaults.yaml"
            self.write_user_config(path, 3600, 3)
            policy, warnings = D.load_steward_policy(config_path=str(path))
            self.assertEqual(warnings, [])
            self.assertEqual(policy["check_interval_seconds"], 3600)
            self.assertEqual(policy["max_supervisors_per_repo"], 3)
            # a rewrite is picked up by the next call (no cached value)
            self.write_user_config(path, 1800, 4)
            policy, warnings = D.load_steward_policy(config_path=str(path))
            self.assertEqual(warnings, [])
            self.assertEqual(policy["check_interval_seconds"], 1800)
            self.assertEqual(policy["max_supervisors_per_repo"], 4)
            # the DISPATCH_DEFAULTS_CONFIG override is the default reader path
            with mock.patch.dict(os.environ, {"DISPATCH_DEFAULTS_CONFIG": str(path)}):
                policy, warnings = D.load_steward_policy()
            self.assertEqual((policy["check_interval_seconds"], warnings), (1800, []))

    def test_load_steward_policy_falls_back_with_one_warning(self):
        defaults = D.query_steward_policy({})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dispatch-defaults.yaml"
            path.write_text("schema_version: 4\nmystery: [\n", encoding="utf-8")
            policy, warnings = D.load_steward_policy(config_path=str(path))
            self.assertEqual(policy, defaults)
            self.assertEqual(len(warnings), 1)
            self.assertTrue(warnings[0].startswith("steward-settings-unreadable: "), warnings)
            # an out-of-range value makes the file invalid -> defaults + warning
            self.write_user_config(path, 599, 2)
            policy, warnings = D.load_steward_policy(config_path=str(path))
            self.assertEqual(policy, defaults)
            self.assertEqual(len(warnings), 1)
            policy, warnings = D.load_steward_policy(config_path=str(Path(tmp) / "missing.yaml"))
            self.assertEqual(policy, defaults)
            self.assertEqual(len(warnings), 1)

    def test_steward_cli_prints_the_three_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dispatch-defaults.yaml"
            self.write_user_config(path, 3600, 3)
            result = subprocess.run(
                [sys.executable, str(MODULE), "steward", "--config", str(path)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.splitlines(),
                [
                    "child_permission_mode=bypass",
                    "check_interval_seconds=3600",
                    "max_supervisors_per_repo=3",
                ],
            )
            self.write_user_config(path, 599, 3)
            broken = subprocess.run(
                [sys.executable, str(MODULE), "steward", "--config", str(path)],
                capture_output=True, text=True,
            )
            self.assertEqual(broken.returncode, 65)


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



class TopExceptionPolicyTest(DispatchDefaultsV3Tests):
    def test_top_borrows_the_deep_bands_and_never_has_its_own_section(self):
        config = self.config()
        self.assertEqual(D.query_profile_policy(config, "top"), D.query_profile_policy(config, "deep"))
        self.assertNotIn("top", D.MODEL_PROFILES)
        config["profiles"]["top"] = dict(config["profiles"]["deep"])
        errors = D.validate(config, D.load_topology_capabilities(D.default_topology_path()))
        self.assertTrue(any("top" in str(e) for e in errors), errors)


class NoProgressDefaultsTests(DispatchDefaultsV3Tests):
    def test_no_progress_normalizer_default_clamp_and_invalid_values(self):
        self.assertEqual(D.normalize_no_progress_minutes(None)[0], 30)
        self.assertEqual(D.normalize_no_progress_minutes(0), (0.0, None))
        self.assertEqual(D.normalize_no_progress_minutes(1.25), (1.25, None))
        self.assertEqual(D.normalize_no_progress_minutes(-1)[0], 0)
        self.assertEqual(D.normalize_no_progress_minutes(1441)[0], 1440)
        for value in (True, "30", float("nan"), float("inf")):
            normalized, warning = D.normalize_no_progress_minutes(value)
            self.assertEqual(normalized, 30)
            self.assertTrue(warning)

    def test_no_progress_policy_is_optional_and_permission_shape_is_unchanged(self):
        config = self.config()
        self.assertEqual(D.query_no_progress_minutes(config), 30)
        self.assertEqual(D.query_headless_policy(config), {
            "claude_permission_mode": "bypass", "source": "shipped-default"
        })
        config["headless"] = {"no_progress_minutes": 0}
        warnings = D.policy_warnings(config)
        normalized = D.normalize_policy(config)
        self.assertEqual(D.query_no_progress_minutes(normalized), 0)
        self.assertFalse(any("no_progress_minutes" in w for w in warnings))


class D5CurrentEnabledTest(unittest.TestCase):
    def test_current_ceiling_intersects_without_filling_bands_or_mutating_sealed_values(self):
        import copy
        for version in (3, 4):
            config = DispatchDefaultsV3Tests().config()
            config["schema_version"] = version
            config["harnesses"]["enabled"] = ["claude", "opencode"]
            sealed = {"primary": ["codex", "claude"], "relief": ["opencode"], "last_resort": [], "promote_relief_below": 25}
            before = copy.deepcopy((config, sealed))
            allowed = D.current_enabled_harnesses(config)
            policy = D.restrict_harness_policy(sealed, allowed)
            self.assertEqual(policy, {**sealed, "primary": ["claude"]})
            self.assertEqual((config, sealed), before)
            self.assertEqual(D.restrict_harness_policy(sealed, [])['primary'], [])
        for version in (1, 2):
            self.assertEqual(set(D.current_enabled_harnesses({"schema_version": version, "depth1_owner": ["claude"]})), D.DISPATCHABLE_HARNESSES)

    def test_unreadable_or_invalid_current_defaults_cannot_become_all_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "policy.yaml"
            for text in (None, "schema_version: 4\nmystery: 1\n"):
                if text is not None:
                    path.write_text(text)
                with mock.patch.dict(os.environ, {"DISPATCH_DEFAULTS_CONFIG": str(path)}), self.assertRaises(D.DefaultsConfigError):
                    D.current_enabled_harnesses()

if __name__ == "__main__":
    unittest.main()
