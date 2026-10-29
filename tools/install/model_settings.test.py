#!/usr/bin/env python3
"""Isolated user-owned model edits and the installed installer CLI surface."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "utilities"))
import installer
import fixture_env
import model_settings as settings
import model_config
import model_profile
import safe_fs


class ModelSettingsCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="model-settings-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = fixture_env.build_environment(self.root, ROOT)
        fixture_env.prepare_environment(self.env)
        self.home = Path(self.env["HOME"])
        self.state = Path(self.env["XDG_STATE_HOME"])
        self.env_patch = mock.patch.dict(os.environ, self.env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def user_path(self, adapter, runtime=None):
        return model_config.user_path(adapter, runtime=runtime, environ=self.env)

    def run_cli(self, *args):
        launcher = self.root / "bin" / "hearting"
        launcher.parent.mkdir(exist_ok=True)
        if not launcher.exists():
            launcher.symlink_to(ROOT / "tools/install/harness.sh")
        env = dict(os.environ)
        result = subprocess.run([str(launcher), "model", "set", *args], env=env,
                                text=True, capture_output=True, timeout=10)
        return result.returncode, result.stdout + result.stderr

    def run_get_cli(self, *args):
        launcher = self.root / "bin" / "hearting"
        launcher.parent.mkdir(exist_ok=True)
        if not launcher.exists():
            launcher.symlink_to(ROOT / "tools/install/harness.sh")
        result = subprocess.run([str(launcher), "model", "get", *args, "--json"],
                                env=dict(os.environ), text=True, capture_output=True, timeout=10)
        return result.returncode, result.stdout + result.stderr

    def test_context_window_set_get_and_legacy_file_bytes(self):
        import json
        for adapter in model_config.ADAPTERS:
            with self.subTest(adapter=adapter):
                path = self.user_path(adapter)
                path.parent.mkdir(parents=True, exist_ok=True)
                shipped = model_config.shipped_path(adapter).read_bytes()
                legacy = b"".join(line for line in shipped.splitlines(keepends=True)
                                   if not line.startswith(b"CFG_MODEL_CONTEXT_WINDOW="))
                path.write_bytes(legacy)
                before = model_config.parse_config(path)

                code, output = self.run_cli(adapter, "context-window", "800000", "--json")
                self.assertEqual(code, 0, output)
                result = json.loads(output)
                self.assertEqual(result["status"], "changed")
                self.assertEqual(result["changed_keys"], ["CFG_MODEL_CONTEXT_WINDOW"])
                self.assertEqual(Path(result["backup"]).read_bytes(), legacy)
                after = model_config.parse_config(path)
                self.assertEqual(after["CFG_MODEL_CONTEXT_WINDOW"], "800000")
                for key, value in before.items():
                    self.assertEqual(after[key], value, key)

                code, output = self.run_get_cli(adapter, "context-window")
                self.assertEqual(code, 0, output)
                query = json.loads(output)
                self.assertEqual(query["operation"], "model-context-window-get")
                self.assertEqual(query["context_window"], "800000")
                self.assertEqual(query["provenance"], "configured")
                self.assertEqual(query["source"], "user")
                self.assertFalse(query["measured_effective_capacity"])

                code, output = self.run_cli(adapter, "context-window", "0800000", "--json")
                self.assertEqual(code, 0, output)
                self.assertEqual(json.loads(output)["status"], "unchanged")

    def test_context_window_invalid_values_and_old_file_query_do_not_mutate(self):
        import json
        path = self.user_path("codex")
        path.parent.mkdir(parents=True, exist_ok=True)
        legacy = b"".join(line for line in model_config.shipped_path("codex").read_bytes().splitlines(keepends=True)
                           if not line.startswith(b"CFG_MODEL_CONTEXT_WINDOW="))
        path.write_bytes(legacy)
        for invalid in ("800k", "+800000", "99999", "1000001", "800000.0"):
            before = path.read_bytes()
            code, output = self.run_cli("codex", "context-window", invalid, "--json")
            self.assertEqual(code, 64, output)
            self.assertEqual(json.loads(output)["status"], "invalid-arguments")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob("models.conf.bak*")), [])

        code, output = self.run_get_cli("codex", "context-window")
        self.assertEqual(code, 0, output)
        query = json.loads(output)
        self.assertEqual(query["source"], "user")
        self.assertEqual(query["provenance"], "default")
        self.assertEqual(query["context_window"], "800000")

    def test_all_harness_tier_edits_preserve_budget_and_exact_backup(self):
        for adapter in model_config.ADAPTERS:
            with self.subTest(adapter=adapter):
                before = (model_config.shipped_path(adapter).read_bytes())
                home = model_config.runtime_home(adapter, environ=self.env)
                path = model_config.user_path(adapter, runtime=home)
                path.parent.mkdir(parents=True)
                path.write_bytes(before)
                target = "balanced-deep" if adapter == "opencode" else "deep"
                code, output = self.run_cli(adapter, "tier/" + target,
                                            "arbitrary-vendor/model-r7", "--json")
                self.assertEqual(code, 0, output)
                result = __import__("json").loads(output)
                values = model_config.parse_config(path, allow_symlink=False)
                tier_key = "BALANCED_DEEP" if adapter == "opencode" else "DEEP"
                suffix = "VARIANT" if adapter == "opencode" else "EFFORT"
                self.assertEqual(values[f"CFG_TIER_{tier_key}_MODEL"], "arbitrary-vendor/model-r7")
                self.assertEqual(values[f"CFG_TIER_{tier_key}_{suffix}"],
                                 model_config.parse_config(model_config.shipped_path(adapter))[f"CFG_TIER_{tier_key}_{suffix}"])
                self.assertEqual(Path(result["backup"]).read_bytes(), before)
                self.assertEqual(stat.S_IMODE(Path(result["backup"]).stat().st_mode),
                                 stat.S_IMODE(path.stat().st_mode))
                self.assertIn("#", path.read_text(encoding="utf-8"))
                resolved, receipt = model_config.resolve_config(adapter, runtime=home, source_root=ROOT)
                self.assertEqual(receipt.source, "user")
                self.assertEqual(resolved[f"CFG_TIER_{tier_key}_MODEL"], "arbitrary-vendor/model-r7")
                original = model_config.parse_config(model_config.shipped_path(adapter))
                for key, value in original.items():
                    if key != f"CFG_TIER_{tier_key}_MODEL":
                        self.assertEqual(values[key], value, key)
                consumer_env = dict(os.environ)
                for key in list(consumer_env):
                    if key.startswith(("AGENT_MODEL_", "AGENT_VARIANT_", "AGENT_REASONING_",
                                       "CLAUDE_MODEL_", "CLAUDE_EFFORT_",
                                       "CODEX_MODEL_", "CODEX_REASONING_")):
                        consumer_env.pop(key)
                script = "model-map.sh" if adapter == "claude" else "role-map.sh"
                mapped = subprocess.run([str(ROOT / "adapters" / adapter / "bin" / script),
                                         "deep maker"], env=consumer_env,
                                        text=True, capture_output=True, timeout=10)
                self.assertEqual(mapped.returncode, 0, mapped.stderr)
                consumed = dict(line.split("=", 1) for line in mapped.stdout.splitlines() if "=" in line)
                model_field = "exact_model_id" if adapter == "claude" else "model"
                budget_field = "variant" if adapter == "opencode" else "reasoning"
                self.assertEqual(consumed[model_field], "arbitrary-vendor/model-r7")
                self.assertEqual(consumed[budget_field], original[f"CFG_TIER_{tier_key}_{suffix}"])

    def test_declared_opencode_deep_tier_matches_roles_profiles_and_role_edits(self):
        import json
        path = self.user_path("opencode")
        path.parent.mkdir(parents=True)
        shipped = model_config.parse_config(model_config.shipped_path("opencode"))
        raw = settings._edit_bytes(model_config.shipped_path("opencode").read_bytes(), {
            "CFG_TIER_DEEP_MODEL": "arbitrary-provider/deep-r1",
            "CFG_TIER_DEEP_VARIANT": "xhigh",
            "CFG_MODEL_PROFILE_DEEP": "deep:xhigh",
            "CFG_MODEL_PROFILE_GRANULARITY_DEEP": "full",
        })
        path.write_bytes(raw)
        for role in ("deep maker", "deep reviewer", "deep orchestrator"):
            result = subprocess.run([str(ROOT / "adapters/opencode/bin/role-map.sh"), role],
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("model=arbitrary-provider/deep-r1", result.stdout)
            self.assertIn("variant=xhigh", result.stdout)
        code, out = self.run_cli("opencode", "role/deep maker", "arbitrary-provider/deep-r2", "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["target"]["tier"], "DEEP")
        values = model_config.parse_config(path)
        profile = model_profile.resolve_profile_values("opencode", values, "deep")
        self.assertEqual((profile["model"], profile["budget"], profile["granularity"]),
                         ("arbitrary-provider/deep-r2", "xhigh", "full"))
        self.assertEqual(values["CFG_TIER_BALANCED_DEEP_MODEL"], shipped["CFG_TIER_BALANCED_DEEP_MODEL"])
        self.assertEqual(path.with_name("models.conf.bak").read_bytes(), raw)

    def test_profile_edit_is_independent_and_omitted_budget_is_preserved(self):
        adapter, home = "codex", self.root / "codex"
        path = self.user_path(adapter, home)
        path.parent.mkdir(parents=True)
        path.write_bytes(model_config.shipped_path(adapter).read_bytes().replace(b"\n", b"\r\n"))
        original_bytes = path.read_bytes()
        original = model_config.parse_config(path)
        settings.set_model(adapter, "profile/light", "vendor-custom-light", runtime=home,
                           environ=self.env, source_root=ROOT)
        after = model_config.parse_config(path)
        updated_bytes = path.read_bytes()
        self.assertNotIn(b"\n", updated_bytes.replace(b"\r\n", b""))
        self.assertEqual(updated_bytes.count(b"\r\n"), original_bytes.count(b"\r\n"))
        self.assertEqual(updated_bytes.count(b"#"), original_bytes.count(b"#"))
        self.assertEqual(after["CFG_MODEL_PROFILE_LIGHT"],
                         f"model/vendor-custom-light:{original['CFG_MODEL_PROFILE_LIGHT'].split(':', 1)[1]}")
        for key, value in original.items():
            if key != "CFG_MODEL_PROFILE_LIGHT":
                self.assertEqual(after[key], value, key)
        selected, receipt = model_profile.resolve_runtime_profile(
            adapter, "light", runtime=home, source_root=ROOT)
        self.assertEqual(receipt.source, "user")
        self.assertEqual(selected["model"], "vendor-custom-light")

    def test_role_alias_updates_shared_tier_only_and_cli_json(self):
        adapter, home = "claude", self.root / "claude"
        before = model_config.parse_config(model_config.shipped_path(adapter))
        code, output = self.run_cli(adapter, "role/fast reviewer", "vendor-reviewer@high", "--json")
        self.assertEqual(code, 0, output)
        result = __import__("json").loads(output)
        self.assertEqual(result["target"]["kind"], "role")
        self.assertEqual(result["target"]["tier"], "LIGHT")
        self.assertTrue(result["target"]["shared"])
        after = model_config.parse_config(self.user_path(adapter, self.home / ".claude"))
        self.assertEqual(after["CFG_TIER_LIGHT_MODEL"], "vendor-reviewer")
        self.assertEqual(after["CFG_TIER_LIGHT_EFFORT"], "high")
        self.assertEqual(after["CFG_TIER_DEEP_EFFORT"], before["CFG_TIER_DEEP_EFFORT"])
        self.assertEqual(after["CFG_ROLES_LIGHT"], before["CFG_ROLES_LIGHT"])
        self.assertEqual(model_profile.resolve_runtime_profile(adapter, "light", runtime=self.home / ".claude", source_root=ROOT)[0]["model"],
                         "vendor-reviewer")

    def test_explicit_top_opt_in_from_old_user_copies(self):
        import json
        for adapter in model_config.ADAPTERS:
            with self.subTest(adapter=adapter):
                path = self.user_path(adapter)
                path.parent.mkdir(parents=True, exist_ok=True)
                original = b"".join(line for line in model_config.shipped_path(adapter).read_bytes().splitlines(keepends=True)
                                    if not line.startswith(b"CFG_MODEL_PROFILE_TOP="))
                if adapter == "opencode":
                    import re
                    original = re.sub(rb"(?m)^CFG_MODEL_PROFILE_DEEP=.*$",
                                      b"CFG_MODEL_PROFILE_DEEP=model/private-provider/previous:max", original)
                path.write_bytes(original)
                fallback = model_profile.resolve_profile_values(
                    adapter, model_config.parse_config(path), "top" if adapter == "opencode" else "deep")
                code, output = self.run_cli(adapter, "profile/top", "vendor-top", "--dry-run", "--json")
                self.assertEqual(code, 0, output)
                self.assertEqual(json.loads(output)["status"], "planned")
                self.assertEqual(path.read_bytes(), original)
                code, output = self.run_cli(adapter, "profile/top", "vendor-top", "--json")
                self.assertEqual(code, 0, output)
                result = json.loads(output)
                self.assertEqual(result["old_model"], None)
                self.assertEqual(Path(result["backup"]).read_bytes(), original)
                top = model_profile.resolve_profile_values(adapter, model_config.parse_config(path), "top")
                self.assertEqual((top["model"], top["budget"]), ("vendor-top", fallback["budget"]))
                code, output = self.run_cli(adapter, "profile/top", "vendor-top", "--json")
                self.assertEqual(code, 0, output)
                self.assertEqual(json.loads(output)["status"], "unchanged")
                code, output = self.run_cli(adapter, "profile/top", "vendor-next@high", "--json")
                self.assertEqual(code, 0, output)
                self.assertEqual(model_profile.resolve_profile_values(adapter, model_config.parse_config(path), "top")["budget"], "high")
                code, output = self.run_cli(adapter, "profile/top", "bad model", "--json")
                self.assertEqual(code, 64, output)

    def test_opencode_role_families_use_installed_cli_and_real_shell_consumer(self):
        import json
        path = self.user_path("opencode")
        code, output = self.run_cli("opencode", "role/fast reviewer",
                                    "arbitrary-role-model@high", "--dry-run", "--json")
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["target"]["tier"], "MINI")
        self.assertFalse(path.parent.exists())
        consumer_env = dict(os.environ)
        for key in list(consumer_env):
            if key.startswith(("AGENT_MODEL_", "AGENT_VARIANT_", "AGENT_EXTERNAL_")):
                consumer_env.pop(key)
        for role, family, tier in [("fast reviewer", "fast", "MINI"),
                                   ("fast implementer", "balanced", "LIGHT"),
                                   ("deep maker", "deep", "BALANCED_DEEP"),
                                   ("fast_fact-checker", "fast", "MINI"),
                                   ("deep-editor", "deep", "BALANCED_DEEP")]:
            with self.subTest(role=role):
                before = model_config.parse_config(path if path.exists() else
                                                  model_config.shipped_path("opencode"))
                model = "arbitrary-role-model-" + tier.lower()
                code, output = self.run_cli("opencode", "role/" + role, model + "@high", "--json")
                self.assertEqual(code, 0, output)
                result = json.loads(output)
                self.assertEqual(result["target"]["tier"], tier)
                after = model_config.parse_config(path)
                for key, value in before.items():
                    if key not in {f"CFG_TIER_{tier}_MODEL", f"CFG_TIER_{tier}_VARIANT"}:
                        self.assertEqual(after[key], value, key)
                mapped = subprocess.run([str(ROOT / "adapters/opencode/bin/role-map.sh"), role],
                                        env=consumer_env, text=True, capture_output=True, timeout=10)
                self.assertEqual(mapped.returncode, 0, mapped.stderr)
                values = dict(line.split("=", 1) for line in mapped.stdout.splitlines() if "=" in line)
                self.assertEqual(values["family"], family)
                self.assertEqual(values["model"], model)
                self.assertEqual(values["variant"], "high")
        before = path.read_bytes()
        backups = sorted(path.parent.glob("models.conf.bak*"))
        code, output = self.run_cli("opencode", "role/fast reviewer", "omitted-budget-model", "--json")
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["new_budget"], "high")
        before = path.read_bytes()
        backups = sorted(path.parent.glob("models.conf.bak*"))
        code, output = self.run_cli("opencode", "role/external adversary", "external-model", "--json")
        self.assertEqual(code, 64, output)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(path.parent.glob("models.conf.bak*")), backups)

    def test_dry_run_noop_invalid_and_malformed_do_not_mutate_or_create_state(self):
        missing_home = self.root / "dry"
        path = model_config.user_path("codex", runtime=missing_home)
        result = settings.set_model("codex", "deep", "new-model", runtime=missing_home,
                                    environ=self.env, source_root=ROOT, dry_run=True)
        self.assertEqual(result["status"], "planned")
        self.assertFalse(path.parent.exists())
        with self.assertRaises(settings.ModelSettingsError):
            settings.set_model("codex", "unknown-target", "new-model", runtime=missing_home,
                               environ=self.env, source_root=ROOT)
        self.assertFalse(path.parent.exists())

        malformed_home = self.root / "bad"
        malformed = model_config.user_path("claude", runtime=malformed_home)
        malformed.parent.mkdir(parents=True)
        raw = b"CFG_TIER_DEEP_MODEL=only-one-key\n# keep\n"
        malformed.write_bytes(raw)
        with self.assertRaisesRegex(settings.ModelSettingsError, "user config preserved"):
            settings.set_model("claude", "deep", "another-model", runtime=malformed_home,
                               environ=self.env, source_root=ROOT)
        self.assertEqual(malformed.read_bytes(), raw)

        code, output = self.run_cli("codex", "unknown-target", "model-x", "--json")
        self.assertEqual(code, 64)
        self.assertEqual(__import__("json").loads(output)["status"], "invalid-arguments")
        self.assertFalse(self.user_path("codex", self.home / ".codex").exists())

    def test_requested_missing_balanced_materializes_only_that_profile(self):
        adapter, home = "codex", self.root / "derived-balanced"
        shipped = model_config.parse_config(model_config.shipped_path(adapter))
        original = model_config.shipped_path(adapter).read_text(encoding="utf-8")
        raw = "".join(line for line in original.splitlines(keepends=True)
                      if not line.startswith(("CFG_MODEL_PROFILE_BALANCED=",
                                              "CFG_MODEL_PROFILE_GRANULARITY_BALANCED=")))
        path = self.user_path(adapter, home)
        path.parent.mkdir(parents=True)
        path.write_text(raw, encoding="utf-8")
        before_values, receipt = model_config.resolve_config(adapter, runtime=home, source_root=ROOT)
        self.assertEqual(receipt.source, "user")
        self.assertEqual(model_profile.resolve_profile_values(adapter, before_values, "balanced")["budget"], "high")
        settings.set_model(adapter, "profile/balanced", "custom-balanced", runtime=home,
                           environ=self.env, source_root=ROOT)
        after = model_config.parse_config(path)
        self.assertEqual(after["CFG_MODEL_PROFILE_BALANCED"], "model/custom-balanced:high")
        for key, value in shipped.items():
            if key not in {"CFG_MODEL_PROFILE_BALANCED", "CFG_MODEL_PROFILE_GRANULARITY_BALANCED"}:
                self.assertEqual(after[key], value, key)

    def test_noop_does_not_create_lock_backup_or_rewrite(self):
        adapter, home = "opencode", self.root / "opencode"
        path = self.user_path(adapter, home)
        path.parent.mkdir(parents=True)
        raw = model_config.shipped_path(adapter).read_bytes()
        path.write_bytes(raw)
        state_before = self.state.exists()
        current_model = model_config.parse_config(path)["CFG_TIER_LIGHT_MODEL"]
        result = settings.set_model(adapter, "tier/light", current_model,
                                    runtime=home, environ=self.env, source_root=ROOT)
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(list(path.parent.glob("models.conf.bak*")), [])
        self.assertEqual(self.state.exists(), state_before)

    def test_native_runtime_and_dispatch_policy_sentinels_are_untouched(self):
        for adapter in model_config.ADAPTERS:
            with self.subTest(adapter=adapter):
                runtime = {"claude": self.home / ".claude",
                           "codex": self.home / ".codex",
                           "opencode": self.home / ".config" / "opencode"}[adapter]
                native = runtime / ("settings.json" if adapter == "claude" else
                                    "config.toml" if adapter == "codex" else "opencode.json")
                native.parent.mkdir(parents=True, exist_ok=True)
                native.write_bytes(b"native sentinel\x00\n")
                policy = self.home / ".config/hearting/dispatch-defaults.yaml"
                policy.parent.mkdir(parents=True, exist_ok=True)
                policy.write_bytes(b"policy sentinel\n")
                native_before, policy_before = native.read_bytes(), policy.read_bytes()
                target = "deep" if adapter != "opencode" else "balanced-deep"
                settings.set_model(adapter, target, "independent-model", runtime=runtime,
                                   environ=self.env, source_root=ROOT)
                self.assertEqual(native.read_bytes(), native_before)
                self.assertEqual(policy.read_bytes(), policy_before)

    def test_cooperating_concurrent_profile_edits_compose(self):
        adapter, home = "codex", self.root / "codex-concurrent"
        outcomes, errors = [], []

        def edit(target, model):
            try:
                outcomes.append(settings.set_model(adapter, target, model, runtime=home,
                                                   environ=self.env, source_root=ROOT))
            except Exception as exc:  # retained for an assertion in the parent thread
                errors.append(exc)

        threads = [threading.Thread(target=edit, args=("profile/light", "vendor-light")),
                   threading.Thread(target=edit, args=("profile/mini", "vendor-mini"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads), "cooperating edit hung")
        self.assertEqual(errors, [])
        values = model_config.parse_config(self.user_path(adapter, home))
        self.assertEqual(values["CFG_MODEL_PROFILE_LIGHT"], "model/vendor-light:medium")
        self.assertEqual(values["CFG_MODEL_PROFILE_MINI"], "model/vendor-mini:low")

    def test_noncooperating_successor_is_not_overwritten_after_preimage_check(self):
        adapter, home = "claude", self.root / "successor"
        path = self.user_path(adapter, home)
        path.parent.mkdir(parents=True)
        path.write_bytes(model_config.shipped_path(adapter).read_bytes())
        successor = path.read_bytes().replace(b"CFG_TIER_DEEP_MODEL=opus", b"CFG_TIER_DEEP_MODEL=successor")
        original_atomic = safe_fs.atomic_write_bytes

        def race(auth, payload, mode, **kwargs):
            temporary = path.with_name("successor.tmp")
            temporary.write_bytes(successor)
            # destructive-ok: reason=simulate a noncooperating successor before the CAS check; boundary=two exact config leaves inside this test's temporary runtime home
            os.replace(temporary, path)
            return original_atomic(auth, payload, mode, **kwargs)

        with mock.patch.object(settings.safe_fs, "atomic_write_bytes", side_effect=race):
            with self.assertRaises(safe_fs.SafetyError):
                settings.set_model(adapter, "deep", "our-model", runtime=home,
                                   environ=self.env, source_root=ROOT)
        self.assertEqual(path.read_bytes(), successor)


if __name__ == "__main__":
    unittest.main()
