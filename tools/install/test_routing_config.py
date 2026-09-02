import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import routing_config  # noqa: E402
import bootstrap  # noqa: E402
import paths  # noqa: E402

_DEFAULTS_SPEC = importlib.util.spec_from_file_location(
    "dispatch_defaults_for_routing_config_test",
    HERE.parents[1] / "utilities" / "dispatch-defaults.py",
)
DEFAULTS = importlib.util.module_from_spec(_DEFAULTS_SPEC)
_DEFAULTS_SPEC.loader.exec_module(DEFAULTS)


class RoutingConfigInstallTests(unittest.TestCase):
    def test_bootstrap_includes_mem_launcher(self):
        names = [row["name"] for row in bootstrap.install_launchers(dry_run=True)]
        self.assertIn("mem", names)

    def test_create_once_and_preserve_user_edits(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}, clear=False
        ), mock.patch.object(routing_config.shutil, "which", return_value="/bin/runtime"):
            first = routing_config.ensure(["claude", "codex", "opencode"])
            path = Path(first["path"])
            self.assertEqual(first["status"], "created")
            text = path.read_text(encoding="utf-8")
            self.assertIn("primary: [claude, codex, opencode]", text)
            self.assertIn("strategy: balanced", text)
            self.assertIn("usage_gate_used_percent: 85", text)
            self.assertIn("owner: claude", text)
            self.assertIn("worker: codex", text)
            self.assertIn("depth_affinity_weight: 0.65", text)
            self.assertIn("usage_headroom_exponent: 2", text)
            self.assertTrue(routing_config.validate()["ok"])
            path.write_text(text + "# user edit\n", encoding="utf-8")
            second = routing_config.ensure(["claude"])
            self.assertEqual(second["status"], "preserved")
            self.assertTrue(path.read_text(encoding="utf-8").endswith("# user edit\n"))

    def test_validate_reports_drift_for_a_preserved_legacy_strategy(self):
        # DP-23: install never rewrites the user file, so a decision that only
        # reached the shipped template (balanced-first, 2026-08-13) stays
        # invisible unless validate() says so. Valid + warnings => "drift".
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}, clear=False
        ), mock.patch.object(routing_config.shutil, "which", return_value="/bin/runtime"):
            created = routing_config.ensure(["claude", "codex"])
            path = Path(created["path"])
            fresh = routing_config.validate()
            self.assertEqual(fresh["status"], "valid")
            self.assertEqual(fresh["warnings"], [])
            text = path.read_text(encoding="utf-8").replace(
                "strategy: balanced", "strategy: capacity-aware"
            )
            path.write_text(text, encoding="utf-8")
            drifted = routing_config.validate()
            self.assertEqual(drifted["status"], "drift")
            self.assertTrue(drifted["ok"])
            joined = " ".join(drifted["warnings"])
            self.assertIn("allocation.strategy=capacity-aware differs from shipped default balanced", joined)
            self.assertIn("allocation.usage_headroom_exponent is inert", joined)
            self.assertIn("allocation.depth_affinity_weight is inert", joined)
            self.assertEqual(drifted["detail"], "; ".join(drifted["warnings"]))
            # The registry surfaces the same state with a distinct mark and the detail line.
            import user_config
            rows = user_config.status(["dispatch-defaults"])
            self.assertEqual(rows[0]["status"], "drift")
            self.assertTrue(rows[0]["ok"])
            rendered = user_config.lines(rows)
            self.assertTrue(rendered[0].startswith("! dispatch-defaults"), rendered)
            self.assertIn("drift warnings", rendered[0])
            self.assertTrue(any("usage_headroom_exponent is inert" in line for line in rendered[1:]), rendered)

    def test_render_omits_a_depth_affinity_cell_for_a_disabled_harness(self):
        claude_only = routing_config.render(["claude"])
        self.assertIn("owner: claude", claude_only)
        self.assertNotIn("worker: codex", claude_only)
        codex_only = routing_config.render(["codex"])
        self.assertIn("worker: codex", codex_only)
        self.assertNotIn("owner: claude", codex_only)
        both = routing_config.render(["claude", "codex"])
        self.assertIn("owner: claude", both)
        self.assertIn("worker: codex", both)
        # A cell naming a disabled harness would fail validate(); each rendering
        # must therefore still be a valid config on its own.
        capmap = DEFAULTS.load_topology_capabilities(DEFAULTS.default_topology_path())
        for text in (claude_only, codex_only, both):
            self.assertEqual(
                DEFAULTS.validate(DEFAULTS.parse_yaml_subset(text), capmap), []
            )

    def test_single_opencode_install_skips_invalid_user_policy(self):
        with self.assertRaisesRegex(ValueError, "quality-peer runtime"):
            routing_config.render(["opencode"])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}, clear=False
        ), mock.patch.object(
            routing_config.shutil, "which",
            side_effect=lambda name: "/bin/opencode" if name == "opencode" else None,
        ):
            result = routing_config.ensure(["opencode"])
            path = Path(result["path"])
            self.assertEqual(result["status"], "skipped-no-quality-peer")
            self.assertEqual(result["enabled"], ["opencode"])
            self.assertFalse(path.exists())

    def test_rendered_config_still_answers_the_shipped_capability_baseline(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}, clear=False
        ), mock.patch.object(routing_config.shutil, "which", return_value="/bin/runtime"):
            os.environ.pop("DISPATCH_DEFAULTS_CONFIG", None)
            created = routing_config.ensure(["claude", "codex", "opencode"])
            config = DEFAULTS.load_and_validate(
                created["path"], DEFAULTS.default_topology_path()
            )
        self.assertEqual(
            DEFAULTS.query_stage_affinity(config, "autopilot-code", "execute"), "diverse"
        )

    def test_doctor_reports_valid_for_a_sparse_rendered_config(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}, clear=False
        ), mock.patch.object(routing_config.shutil, "which", return_value="/bin/runtime"):
            os.environ.pop("DISPATCH_DEFAULTS_CONFIG", None)
            routing_config.ensure(["claude", "codex"])
            self.assertTrue(routing_config.validate()["ok"])

    def test_doctor_still_reports_invalid_for_a_broken_user_config(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}, clear=False
        ):
            os.environ.pop("DISPATCH_DEFAULTS_CONFIG", None)
            path = Path(tmp) / "hearting" / "dispatch-defaults.yaml"
            path.parent.mkdir()
            path.write_text("schema_version: 3\nharnesses:\n  enabled: []\n", encoding="utf-8")
            result = routing_config.validate()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid")


class ConfirmationModeDriftWarningTests(unittest.TestCase):
    """A5: declared-vs-sealed confirmation_mode drift, sourced from the
    newest `owner-route-bindings/*.json` record by its own `published_at`."""

    def test_no_binding_is_unknown_not_drift(self):
        with mock.patch.object(routing_config, "_newest_owner_route_binding", return_value=None):
            self.assertIsNone(routing_config.confirmation_mode_drift_warning("hybrid"))

    def test_binding_missing_route_file_key_is_unknown(self):
        with mock.patch.object(routing_config, "_newest_owner_route_binding", return_value={}):
            self.assertIsNone(routing_config.confirmation_mode_drift_warning("hybrid"))

    def test_unreadable_route_file_is_unknown(self):
        with mock.patch.object(
            routing_config, "_newest_owner_route_binding",
            return_value={"route_file": "/does/not/exist.json"},
        ):
            self.assertIsNone(routing_config.confirmation_mode_drift_warning("hybrid"))

    def test_legacy_route_without_confirmation_mode_key_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            route_file = Path(tmp) / "rt-legacy.json"
            route_file.write_text(json.dumps({"route_id": "rt-legacy"}), encoding="utf-8")
            with mock.patch.object(
                routing_config, "_newest_owner_route_binding",
                return_value={"route_file": str(route_file)},
            ):
                self.assertIsNone(routing_config.confirmation_mode_drift_warning("hybrid"))

    def test_matching_sealed_mode_is_not_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            route_file = Path(tmp) / "rt-a.json"
            route_file.write_text(json.dumps({"confirmation_mode": "hybrid"}), encoding="utf-8")
            with mock.patch.object(
                routing_config, "_newest_owner_route_binding",
                return_value={"route_file": str(route_file)},
            ):
                self.assertIsNone(routing_config.confirmation_mode_drift_warning("hybrid"))

    def test_differing_sealed_mode_is_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            route_file = Path(tmp) / "rt-b.json"
            route_file.write_text(json.dumps({"confirmation_mode": "both"}), encoding="utf-8")
            with mock.patch.object(
                routing_config, "_newest_owner_route_binding",
                return_value={"route_file": str(route_file)},
            ):
                warning = routing_config.confirmation_mode_drift_warning("hybrid")
            self.assertIsNotNone(warning)
            self.assertIn("hybrid", warning)
            self.assertIn("both", warning)

    def test_newest_binding_selected_by_published_at_not_filename_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindings_dir = Path(tmp) / "owner-route-bindings"
            bindings_dir.mkdir()
            old_route = Path(tmp) / "rt-old.json"
            old_route.write_text(json.dumps({"confirmation_mode": "hybrid"}), encoding="utf-8")
            new_route = Path(tmp) / "rt-new.json"
            new_route.write_text(json.dumps({"confirmation_mode": "both"}), encoding="utf-8")
            (bindings_dir / "a-first-alphabetically.json").write_text(
                json.dumps({"route_file": str(new_route), "published_at": 200.0}),
                encoding="utf-8",
            )
            (bindings_dir / "z-last-alphabetically.json").write_text(
                json.dumps({"route_file": str(old_route), "published_at": 100.0}),
                encoding="utf-8",
            )
            fake_dc = mock.Mock()
            fake_dc.resolve_agent_home.return_value = tmp
            fake_dc.resolve_dispatch_state_root.return_value = Path(tmp)
            with mock.patch.object(routing_config, "_dispatch_contract_module", return_value=fake_dc):
                binding = routing_config._newest_owner_route_binding()
            self.assertEqual(binding["route_file"], str(new_route))

    def test_empty_or_missing_bindings_dir_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_dc = mock.Mock()
            fake_dc.resolve_agent_home.return_value = tmp
            fake_dc.resolve_dispatch_state_root.return_value = Path(tmp)
            with mock.patch.object(routing_config, "_dispatch_contract_module", return_value=fake_dc):
                self.assertIsNone(routing_config._newest_owner_route_binding())

    def test_validate_surfaces_confirmation_mode_drift_as_warning(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}, clear=False
        ), mock.patch.object(routing_config.shutil, "which", return_value="/bin/runtime"):
            os.environ.pop("DISPATCH_DEFAULTS_CONFIG", None)
            routing_config.ensure(["claude", "codex"])
            route_file = Path(tmp) / "rt-drift.json"
            route_file.write_text(json.dumps({"confirmation_mode": "both"}), encoding="utf-8")
            with mock.patch.object(
                routing_config, "_newest_owner_route_binding",
                return_value={"route_file": str(route_file)},
            ):
                result = routing_config.validate()
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "drift")
            self.assertTrue(any("confirmation.mode" in w for w in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
