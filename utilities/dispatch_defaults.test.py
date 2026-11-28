#!/usr/bin/env python3
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
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

    def write_policy(self, td, allocation_lines):
        path = Path(td) / "dispatch-defaults.yaml"
        path.write_text(
            "schema_version: 3\nharnesses:\n  enabled: [claude, codex]\nprofiles:\n"
            + "".join(
                f"  {profile}:\n    primary: [claude, codex]\n    relief: []\n    last_resort: []\n    promote_relief_below: 0\n"
                for profile in ("deep", "balanced-deep", "light", "mini")
            )
            + "allocation:\n  strategy: balanced\n  window: 30\n" + allocation_lines + "capabilities:\n",
            encoding="utf-8",
        )
        return path

    def test_an_empty_flow_mapping_reads_as_an_empty_mapping(self):
        # 2026-11-27: a hand-cleared `depth_affinity: {}` read as the string
        # "{}" and made the whole user policy invalid for every dispatch.
        for text in ("{}", "{ }", "{ \t}", "{}   # cleared"):
            parsed = D.parse_yaml_subset(f"allocation:\n  depth_affinity: {text}\n")
            self.assertEqual(parsed["allocation"]["depth_affinity"], {}, text)
        self.assertEqual(D.parse_yaml_subset("profiles:\n  deep: []\n")["profiles"]["deep"], [])
        with tempfile.TemporaryDirectory() as td:
            path = self.write_policy(td, "  depth_affinity: {}\n  depth_affinity_weight: 0.85\n")
            config, warnings = D.load_validated(str(path), D.default_topology_path())
            self.assertEqual(warnings, [])
            self.assertEqual(D.query_allocation(config)["depth_affinity"], {})
            result = subprocess.run(
                [sys.executable, str(MODULE), "allocation", "--config", str(path)],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("depth_affinity=\n", result.stdout)

    def validate_cli(self, path):
        return subprocess.run(
            [sys.executable, str(MODULE), "validate", "--config", str(path)],
            text=True, capture_output=True,
        )

    def test_an_empty_mapping_in_a_scalar_cell_is_named_not_a_traceback(self):
        # `{}` reads as a mapping; a scalar or required cell holding one must
        # fail with the cell's existing error (exit 65), like the string "{}"
        # did before, not with a TypeError from a set lookup.
        cases = [
            (lambda text: text.replace("schema_version: 3\n", "schema_version: {}\n"),
             "schema_version must be an integer, got {}"),
            (lambda text: text.replace("  strategy: balanced\n", "  strategy: {}\n"),
             "allocation.strategy must be one of"),
            (lambda text: text.replace("capabilities:\n", "capabilities:\n  autopilot-code:\n    frame: {}\n"),
             "invalid affinity value for autopilot-code.frame: {}"),
            (lambda text: text.replace("  window: 30\n", "  window: 30\n  depth_affinity:\n    owner: {}\n"),
             "allocation.depth_affinity values must name known harnesses"),
        ]
        capmap = D.load_topology_capabilities(D.default_topology_path())
        with tempfile.TemporaryDirectory() as td:
            path = self.write_policy(td, "")
            valid = path.read_text(encoding="utf-8")
            for edit, fragment in cases:
                text = edit(valid)
                self.assertNotEqual(text, valid, fragment)
                errors = D.validate(D.parse_yaml_subset(text), capmap)
                self.assertTrue(any(fragment in error for error in errors), (fragment, errors))
                path.write_text(text, encoding="utf-8")
                result = self.validate_cli(path)
                self.assertEqual(result.returncode, 65, result.stdout + result.stderr)
                self.assertIn(fragment, result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_an_unhashable_schema_version_reaches_the_wrong_value_branch_in_queries(self):
        # The settings editor reads the previous applied values before the
        # validator runs. A hand-written `schema_version: {}` or `[]` must take
        # the same wrong-value branch as `schema_version: "x"`, not TypeError
        # from a set lookup; every hashable value keeps its previous answer.
        allocation = {"allocation": {"strategy": "balanced", "window": 30}}
        wrong = D.query_allocation(dict(allocation, schema_version="x"))
        for bad in ({}, []):
            self.assertEqual(D.query_allocation(dict(allocation, schema_version=bad)), wrong)
        self.assertEqual(D.query_owners({"schema_version": 3, "harnesses": {"enabled": ["codex"]}}), ["codex"])
        self.assertEqual(D.query_owners({"schema_version": "3", "depth1_owner": ["claude"]}), ["claude"])
        self.assertEqual(D.query_owners({"schema_version": True, "depth1_owner": ["claude"]}), ["claude"])
        self.assertEqual(D.query_owners({"depth1_owner": ["claude"]}), ["claude"])

    def test_a_line_indented_under_an_empty_mapping_is_refused_with_its_line(self):
        allocation = "schema_version: 3\nallocation:\n  strategy: balanced\n  depth_affinity: {}\n"
        # A sibling at the same depth, a blank line and comments (even an
        # indented one) read normally after `key: {}`.
        parsed = D.parse_yaml_subset(allocation + "\n    # nothing to add\n  # note\n  window: 30\ncapabilities: {}\n")
        self.assertEqual(parsed["allocation"], {"strategy": "balanced", "depth_affinity": {}, "window": 30})
        self.assertEqual(parsed["capabilities"], {})
        # A deeper line no longer lands in the enclosing mapping.
        for nested in ("    window: 30\n", "\n    window: 30\n"):
            with self.assertRaises(D.DefaultsConfigError) as caught:
                D.parse_yaml_subset(allocation + nested)
            lineno = (allocation + nested).splitlines().index("    window: 30") + 1
            self.assertIn(f"line {lineno}: ", str(caught.exception))
            self.assertIn("empty `{}` mapping", str(caught.exception))
        with tempfile.TemporaryDirectory() as td:
            path = self.write_policy(td, "  depth_affinity: {}\n")
            text = path.read_text(encoding="utf-8").replace("  window: 30\n", "").replace(
                "  depth_affinity: {}\n", "  depth_affinity: {}\n    window: 30\n")
            path.write_text(text, encoding="utf-8")
            result = self.validate_cli(path)
            self.assertEqual(result.returncode, 65, result.stdout + result.stderr)
            self.assertIn("is indented under an empty `{}` mapping", result.stderr)

    def test_a_wrong_optional_allocation_key_still_fails_the_whole_file(self):
        # The `{}` read changes nothing else: a wrong optional allocation value
        # still fails the file and names every error, as on main.
        with tempfile.TemporaryDirectory() as td:
            path = self.write_policy(
                td, "  depth_affinity_weight: 1.2\n  depth_affinity:\n    owner: claude\n    worker: gemini\n")
            with self.assertRaises(D.DefaultsConfigError) as caught:
                D.load_validated(str(path), D.default_topology_path())
            self.assertIn("allocation.depth_affinity values must name known harnesses", str(caught.exception))
            self.assertIn("allocation.depth_affinity_weight must be a number from 0.0 to 1.0", str(caught.exception))
            result = self.validate_cli(path)
            self.assertEqual(result.returncode, 65, result.stdout + result.stderr)
            self.assertNotIn("ignored", result.stdout + result.stderr)

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

    # --- field rows: `allocation.harness_weights.<field>` overrides one field's default row ---

    TABLE = json.loads(Path(D.default_topology_path()).read_text(encoding="utf-8"))["field_harness_weights"]

    def _balanced(self, weights=None):
        config = self.config()
        config["allocation"]["strategy"] = "balanced"
        if weights is not None:
            config["allocation"]["harness_weights"] = weights
        return config

    def test_field_blocks_and_numbers_validate_beside_harness_names(self):
        capmap = D.load_topology_capabilities(D.default_topology_path())
        config = self._balanced({"opencode": 0.4, "design": {"codex": 1.0, "claude": 0.8}, "docs": 1.0})
        self.assertEqual(D.validate(config, capmap), [])
        parsed = D.parse_yaml_subset(
            "allocation:\n  harness_weights:\n    opencode: 0.4\n    design:\n      codex: 1.0\n"
            "      claude: 0.8\n    docs: 1.0\n")
        self.assertEqual(parsed["allocation"]["harness_weights"],
                         {"opencode": 0.4, "design": {"codex": 1.0, "claude": 0.8}, "docs": 1.0})

    def test_field_rows_reject_what_harness_weights_rejects(self):
        capmap = D.load_topology_capabilities(D.default_topology_path())
        cases = [
            ({"design": 0}, "harness_weights.design must be a number"),
            ({"design": 1.5}, "harness_weights.design must be a number"),
            ({"design": True}, "harness_weights.design must be a number"),
            ({"design": {"codex": 0}}, "harness_weights.design.codex must be a number"),
            ({"design": {"codex": 1.2}}, "harness_weights.design.codex must be a number"),
            ({"design": {"codex": True}}, "harness_weights.design.codex must be a number"),
            ({"design": {"gemini": 0.5}}, "harness_weights.design names an unknown harness"),
            ({"gemini": 0.5}, "harness_weights names an unknown harness or field"),
            ({"painting": 0.5}, "harness_weights names an unknown harness or field"),
            ({"painting": {"codex": 0.5}}, "harness_weights names an unknown harness or field"),
        ]
        for weights, fragment in cases:
            errors = D.validate(self._balanced(weights), capmap)
            self.assertTrue(any(fragment in error for error in errors), (weights, errors))

    def test_the_sealed_flat_harness_weights_leave_the_field_entries_out(self):
        config = self._balanced({"opencode": 0.4, "design": {"codex": 1.0}, "docs": 1.0})
        self.assertEqual(D.query_allocation(config)["harness_weights"], {"opencode": 0.4})
        only_fields = D.query_allocation(self._balanced({"design": {"codex": 1.0}}))
        self.assertNotIn("harness_weights", only_fields)
        self.assertNotIn("field_weights", only_fields)  # the row is sealed per route, never here

    def test_normalizing_drops_a_disabled_harness_but_keeps_field_entries(self):
        config = self._balanced({"opencode": 0.4, "design": {"codex": 1.0}})
        config["harnesses"]["enabled"] = ["claude", "codex"]
        weights = D.normalize_policy(config)["allocation"]["harness_weights"]
        self.assertEqual(weights, {"design": {"codex": 1.0}})

    def test_the_applied_row_is_the_default_row_plus_the_user_override(self):
        row = lambda config, field: D.query_field_weights(config, field, self.TABLE)  # noqa: E731
        order = ["claude", "codex", "opencode"]
        self.assertEqual(row(self._balanced(), "code"), {"claude": 0.95, "codex": 0.85, "opencode": 1.0})
        self.assertEqual(list(row(self._balanced(), "code")), order)
        # a block sets only the cells it names; the rest keep the default row
        got = row(self._balanced({"design": {"codex": 1.0, "claude": 0.8}}), "design")
        self.assertEqual(got, {"claude": 0.8, "codex": 1.0, "opencode": 0.85})
        # a number sets all three cells, and equal cells cancel in the share: no row at all, whatever the number
        self.assertIsNone(row(self._balanced({"docs": 0.7}), "docs"))
        self.assertIsNone(row(self._balanced({"docs": 1.0}), "docs"))
        self.assertIsNone(row(self._balanced({"design": {"claude": 0.5, "codex": 0.5, "opencode": 0.5}}), "design"))
        # one cell off the others keeps the whole row
        self.assertEqual(row(self._balanced({"design": {"claude": 0.5, "codex": 0.5}}), "design"),
                         {"claude": 0.5, "codex": 0.5, "opencode": 0.85})
        # admin/other have no default row (1.0) until the user gives them one
        self.assertIsNone(row(self._balanced(), "admin"))
        self.assertEqual(row(self._balanced({"admin": {"codex": 0.5}}), "admin"),
                         {"claude": 1.0, "codex": 0.5, "opencode": 1.0})
        # an override of another field, a harness-name entry, and no field leave a row alone
        self.assertEqual(row(self._balanced({"design": 0.5, "opencode": 0.4}), "code"),
                         {"claude": 0.95, "codex": 0.85, "opencode": 1.0})
        self.assertIsNone(row(self._balanced(), None))
        self.assertIsNone(D.query_field_weights(self._balanced(), "code", None))

    def test_the_display_reader_keeps_a_switched_off_row_and_names_the_overridden_cells(self):
        row = lambda config, field: D.field_weight_row(config, field, self.TABLE)  # noqa: E731
        config = self._balanced({"design": {"codex": 1.0}, "docs": 0.7, "opencode": 0.4})
        self.assertEqual(row(config, "design"), ({"claude": 1.0, "codex": 1.0, "opencode": 0.85}, ("codex",)))
        cells, named = row(config, "docs")
        self.assertEqual((cells, named), ({"claude": 0.7, "codex": 0.7, "opencode": 0.7},
                                          ("claude", "codex", "opencode")))
        self.assertTrue(D.field_weights_off(config, cells))
        self.assertEqual(row(config, "code"), ({"claude": 0.95, "codex": 0.85, "opencode": 1.0}, ()))
        self.assertFalse(D.field_weights_off(config, row(config, "code")[0]))
        config["allocation"]["strategy"] = "capacity-aware"
        self.assertEqual(row(config, "code"), (None, ()))

    def test_a_row_equal_across_the_enabled_harnesses_is_neutral_too(self):
        # `research` is claude 0.9 / codex 1.0 / opencode 1.0: with claude disabled the choosable
        # cells are equal and the row cancels; a differing enabled cell keeps it.
        config = self._balanced()
        config["harnesses"]["enabled"] = ["codex", "opencode"]
        self.assertIsNone(D.query_field_weights(config, "research", self.TABLE))
        config["harnesses"]["enabled"] = ["claude", "codex"]
        self.assertEqual(D.query_field_weights(config, "research", self.TABLE),
                         {"claude": 0.9, "codex": 1.0, "opencode": 1.0})

    def test_the_validator_reads_field_names_from_the_registry_it_was_given(self):
        import tempfile
        capmap = D.load_topology_capabilities(D.default_topology_path())
        config = self._balanced({"painting": 0.5})
        self.assertTrue(any("unknown harness or field" in e for e in D.validate(config, capmap)))
        registry = json.loads(Path(D.default_topology_path()).read_text(encoding="utf-8"))
        registry["fields"] = list(registry["fields"]) + ["painting"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "topologies.json"
            path.write_text(json.dumps(registry), encoding="utf-8")
            self.assertEqual(D.validate(config, capmap, str(path)), [])
            cfg = Path(tmp) / "defaults.yaml"
            cfg.write_text("schema_version: 3\nharnesses:\n  enabled: [claude, codex, opencode]\n"
                           "profiles:\n"
                           + "".join(f"  {n}:\n    primary: [claude, codex]\n    relief: [opencode]\n"
                                     "    last_resort: []\n    promote_relief_below: 35\n"
                                     for n in ("deep", "balanced-deep", "light", "mini"))
                           + "allocation:\n  strategy: balanced\n  window: 30\n  harness_weights:\n"
                           "    painting: 0.5\ncapabilities:\n", encoding="utf-8")
            loaded, _warnings = D.load_validated(str(cfg), str(path))
            self.assertEqual(loaded["allocation"]["harness_weights"], {"painting": 0.5})
            with self.assertRaises(D.DefaultsConfigError):
                D.load_validated(str(cfg), D.default_topology_path())

    def test_only_the_balanced_strategy_reads_a_field_row(self):
        for strategy in ("capacity-aware", "least-recent-attempts"):
            config = self._balanced(); config["allocation"]["strategy"] = strategy
            self.assertIsNone(D.query_field_weights(config, "code", self.TABLE), strategy)
        self.assertIsNone(D.query_field_weights({"schema_version": 1}, "code", self.TABLE))

    def test_the_shipped_table_names_declared_fields_and_depth_profiles_only(self):
        fields = D.field_vocabulary()
        self.assertTrue(set(self.TABLE["weights"]) <= set(fields), fields)
        self.assertEqual(self.TABLE["checked_on"], "2026-11-20")
        self.assertTrue(all(0.0 < v <= 1.0 for row in self.TABLE["weights"].values() for v in row.values()))
        self.assertTrue(set(sum(self.TABLE["basis"]["profiles"].values(), [])) <= set(D.MODEL_PROFILES))

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

    V3_TEXT = (
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

    def v3_config(self):
        return D.parse_yaml_subset(self.V3_TEXT)

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

    # (text appended to a v3 file, expected mode, a warning expected)
    USER_RULES_TABLE = (
        ("", "carry", False),
        ("user_rules:\n", "carry", False),
        ("user_rules:\n  opencode: carry\n", "carry", False),
        ("user_rules:\n  opencode: Carry\n", "carry", False),
        ("user_rules:\n  opencode: on\n", "carry", False),
        ("user_rules:\n  opencode: true\n", "carry", False),
        ("user_rules:\n  opencode: True\n", "carry", False),
        ("user_rules:\n  opencode: yes\n", "carry", False),
        ("user_rules:\n  opencode: off\n", "off", False),
        ("user_rules:\n  opencode: OFF\n", "off", False),
        ("user_rules:\n  opencode: 'off'\n", "off", False),
        ("user_rules:\n  opencode: false\n", "off", False),
        ("user_rules:\n  opencode: no\n", "off", False),
        ("user_rules:\n  opencode: off  # stop\n", "off", False),
        ("user_rules:\n  opencode: of\n", "off", True),
        ("user_rules:\n  opencode: 0\n", "off", True),
        ("user_rules:\n  opencode:\n", "off", True),
        ("user_rules:\n  opencode: off\n  claude: off\n", "off", True),
        ("user_rules: opencode: off\n", "off", True),     # the one-liner the old notice showed
        ("user_rules: {opencode: off}\n", "off", True),
        ("user_rules: off\n", "off", True),
        ("user_rules: true\n", "carry", True),
        ("user_rules.opencode: off\n", "off", True),
        ("user_rules.opencode: on\n", "carry", True),
        ("user_rules:\n  opencode: on\nuser_rules.opencode: off\n", "carry", True),
    )

    def test_section_and_dotted_forms_that_disagree_are_named(self):
        config = D.parse_yaml_subset(self.V3_TEXT + "user_rules:\n  opencode: on\n"
                                     "user_rules.opencode: off\n")
        mode, warnings = D.user_rules_opencode(config)
        self.assertEqual(mode, "carry")
        self.assertTrue(any("disagree" in warning for warning in warnings), warnings)

    def test_tab_indentation_is_a_parse_error_and_off_still_holds(self):
        text = "schema_version: 4\nuser_rules:\n\topencode: off\n"
        with self.assertRaises(D.DefaultsConfigError):
            D.parse_yaml_subset(text)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dispatch-defaults.yaml"
            path.write_text(text)
            self.assertEqual(D.load_user_rules_opencode(str(path)), "off")

    def test_user_rules_values_share_one_table_and_never_invalidate_routing(self):
        for text, expected, warns in self.USER_RULES_TABLE:
            with self.subTest(text=text):
                config = D.parse_yaml_subset(self.V3_TEXT + text)
                self.assertEqual(D.query_user_rules_opencode(config), expected)
                self.assertEqual(D.validate(config, self.capmap), [])
                mode, warnings = D.user_rules_opencode(config)
                self.assertEqual(mode, expected)
                self.assertEqual(bool(warnings), warns, warnings)
                self.assertEqual(any(w.startswith("user_rules") for w in D.policy_warnings(config)),
                                 warns)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dispatch-defaults.yaml"
            path.write_text(Path(D.SHIPPED_CONFIG_PATH).read_text() + "user_rules: opencode: off\n")
            config, warnings = D.load_validated(str(path), D.default_topology_path())
            self.assertTrue(D.query_owners(config))  # routing still loads
            self.assertTrue(any(w.startswith("user_rules") for w in warnings), warnings)
            self.assertEqual(D.load_user_rules_opencode(str(path)), "off")

    def test_user_rules_switch_reads_only_its_own_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dispatch-defaults.yaml"
            path.write_text("schema_version: 4\nunknown_section: 1\nuser_rules:\n  opencode: off\n")
            self.assertEqual(D.load_user_rules_opencode(str(path)), "off")
            # A parse error elsewhere keeps an off switch off.
            broken = ("schema_version: 4\nheadless:\n   no_progress_minutes: 30\n"
                      "user_rules:\n  opencode: off\n")
            path.write_text(broken)
            with self.assertRaises(D.DefaultsConfigError):
                D.parse_yaml_subset(broken)
            self.assertEqual(D.load_user_rules_opencode(str(path)), "off")
            path.write_text("schema_version: 4\n odd: indentation\nuser_rules: off\n")
            self.assertEqual(D.load_user_rules_opencode(str(path)), "off")
            path.write_text("schema_version: 4\n odd: indentation\n")
            self.assertEqual(D.load_user_rules_opencode(str(path)), "carry")
            self.assertEqual(D.load_user_rules_opencode(str(Path(temporary) / "absent.yaml")), "carry")

    def test_a_nested_opencode_key_does_not_override_off_in_a_rejected_file(self):
        # 2026-11-28: a line under `key: {}` fails the parse, and the line scan
        # then read `note:` / `opencode: carry` as the switch, carrying the
        # user's rules into OpenCode despite an explicit off.
        broken = (self.V3_TEXT.replace("schema_version: 3\n", "schema_version: 4\n")
                  .replace("  window: 30\n", "  depth_affinity: {}\n    window: 30\n"))
        deep_line = broken.splitlines().index("    window: 30") + 1
        nested = "  note:\n    opencode: carry\n"
        cases = (
            ("user_rules:\n  opencode: off\n" + nested, "off"),
            ("user_rules.opencode: off\nuser_rules:\n" + nested, "off"),
            ("user_rules: opencode: off\n", "off"),
            ("user_rules:\n" + nested + "  opencode: off\n", "off"),
            ("user_rules:\n  opencode: on\n  note:\n    opencode: off\n", "carry"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dispatch-defaults.yaml"
            for switch, expected in cases:
                with self.subTest(switch=switch):
                    path.write_text(broken + switch, encoding="utf-8")
                    result = subprocess.run(
                        [sys.executable, str(MODULE), "validate", "--config", str(path)],
                        text=True, capture_output=True)
                    self.assertEqual(result.returncode, 65, result.stdout + result.stderr)
                    self.assertIn(f"line {deep_line}: ", result.stderr)
                    self.assertEqual(D.load_user_rules_opencode(str(path)), expected)

    def test_user_config_path_follows_the_override(self):
        self.assertEqual(D.user_config_path({"DISPATCH_DEFAULTS_CONFIG": "/x/defaults.yaml"}),
                         "/x/defaults.yaml")
        self.assertEqual(D.user_config_path({"XDG_CONFIG_HOME": "/cfg"}),
                         "/cfg/hearting/dispatch-defaults.yaml")

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

    def test_baseline_cell_holding_a_mapping_or_list_is_dropped_like_a_wrong_value(self):
        # A shipped baseline cell that parses as `{}` or `[]` is outside
        # AFFINITY_VALUES exactly like an unknown string, and the documented
        # path for that is to drop it, never to raise TypeError.
        config = D.parse_yaml_subset(self.sparse_v3_text())
        capmap = D.load_topology_capabilities(D.default_topology_path())
        wrong = D.merge_capability_baseline(
            config, capmap, baseline={"autopilot-code": {"frame": "unknown"}}
        )["capabilities"]
        for bad in ({}, []):
            merged = D.merge_capability_baseline(
                config, capmap, baseline={"autopilot-code": {"frame": bad}}
            )
            self.assertEqual(merged["capabilities"], wrong)
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
