#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


PROFILE = load("portable_model_profile", ROOT / "utilities" / "model_profile.py")
WRAPPERS = {
    adapter: load(
        f"{adapter}_profile_wrapper",
        ROOT / "adapters" / adapter / "bin" / "dispatch-headless.py",
    )
    for adapter in ("claude", "codex", "opencode")
}


def declared_point(config, profile):
    tier, budget = config[f"CFG_MODEL_PROFILE_{profile.upper().replace('-', '_')}"].split(":", 1)
    if tier.startswith("model/"):
        return tier[len("model/"):], budget
    key = tier.upper().replace("-", "_")
    return config[f"CFG_TIER_{key}_MODEL"], budget


def args(adapter: str, profile: str, **overrides):
    budget_key = {"claude": "effort", "codex": "reasoning", "opencode": "variant"}[adapter]
    values = {
        "model_profile": profile,
        "registered_worker": 1,
        "dispatch_depth": 2,
        "worker_type": "stage",
        "inherit_model_settings": False,
        "model_role": "fast implementer",
        "model": None,
        budget_key: None,
        "capacity_retry": 0,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class ModelProfileTest(unittest.TestCase):
    def test_pair_rotation_uses_declared_profiles_and_model_identity(self):
        for adapter in ("claude", "codex", "opencode"):
            config = PROFILE.load_config(ROOT / "adapters" / adapter / "config" / "models.conf")
            result = PROFILE.pair_model_rotation(adapter, config, "deep", "opus")
            self.assertIsNotNone(result, adapter)
            self.assertNotEqual(result["model"], "opus")
            self.assertIn(result["profile"], ("balanced", "light"))

    def test_pair_rotation_skips_same_model_profiles_and_does_not_enter_top(self):
        config = PROFILE.load_config(ROOT / "adapters" / "claude" / "config" / "models.conf")
        deep_model = PROFILE.resolve_profile_values("claude", config, "deep")["model"]
        result = PROFILE.pair_model_rotation("claude", config, "deep", deep_model)
        self.assertIsNotNone(result)
        self.assertEqual(result["profile"], "balanced")
        self.assertNotEqual(result["model"], deep_model)

    def test_pair_rotation_returns_none_when_all_lower_profiles_match(self):
        config = PROFILE.load_config(ROOT / "adapters" / "claude" / "config" / "models.conf")
        model = PROFILE.resolve_profile_values("claude", config, "deep")["model"]
        for profile in ("balanced-deep", "balanced", "light"):
            key = "CFG_MODEL_PROFILE_" + profile.upper().replace("-", "_")
            config[key] = "opus:medium"
        result = PROFILE.pair_model_rotation("claude", config, "deep", model)
        self.assertIsNone(result)

    def demand(self, judgment="predetermined", scope="short-local", **extra):
        value = {
            "schema_version": 1,
            "judgment_requirement": judgment,
            "execution_scope": scope,
            "judgment_reason": "approved judgment boundary",
            "execution_reason": "bounded execution scope",
            "evidence_refs": ["fixture:sd-88"],
        }
        value.update(extra)
        return value

    def test_two_axis_matrix_and_independent_floors(self):
        expected = {
            ("predetermined", "short-local"): "light",
            ("predetermined", "extended-multistep"): "balanced",
            ("important", "short-local"): "balanced-deep",
            ("important", "extended-multistep"): "balanced-deep",
            ("difficult-uncertain", "short-local"): "deep",
            ("difficult-uncertain", "extended-multistep"): "deep",
        }
        for axes, profile in expected.items():
            with self.subTest(axes=axes):
                result = PROFILE.resolve_profile_demand(self.demand(*axes))
                self.assertEqual(result["resolved_profile"], profile)
                self.assertTrue(result["demand_digest"])
        for axes in expected:
            for profile in PROFILE.PORTABLE_PROFILES:
                row = PROFILE.resolve_profile_demand(self.demand(*axes), explicit_profile=profile)
                self.assertEqual(row["resolved_profile"], profile)
                PROFILE.validate_profile_selection(row, self.demand(*axes), profile=profile)
        result = PROFILE.resolve_profile_demand(
            self.demand("important"), explicit_profile="deep"
        )
        self.assertIn("additional", result["reason"])

    def test_context_budget_is_independent_of_profile_model_and_effort(self):
        for adapter in ("claude", "codex", "opencode"):
            path = ROOT / "adapters" / adapter / "config" / "models.conf"
            values = PROFILE.load_config(path)
            values["CFG_MODEL_CONTEXT_WINDOW"] = "600000"
            for name in ("deep", "balanced-deep", "balanced", "light", "mini", "top"):
                with self.subTest(adapter=adapter, profile=name):
                    resolved = PROFILE.resolve_profile_values(adapter, values, name)
                    self.assertEqual(resolved["context_window"], "600000")
                    self.assertEqual(resolved["context_window_source"], "configured")
            values.pop("CFG_MODEL_CONTEXT_WINDOW")
            resolved = PROFILE.resolve_profile_values(adapter, values, "light")
            self.assertEqual(resolved["context_window"], "800000")
            self.assertEqual(resolved["context_window_source"], "default")

    def test_demand_reasons_and_axes_are_sealed(self):
        base = PROFILE.resolve_profile_demand(self.demand("important"))
        changed = PROFILE.resolve_profile_demand(
            self.demand("important", judgment_reason="new approved reason")
        )
        self.assertNotEqual(base["demand_digest"], changed["demand_digest"])
        for field, value in (("execution_scope", "unknown"), ("evidence_refs", [])):
            with self.subTest(field=field), self.assertRaises(PROFILE.ModelProfileError):
                PROFILE.resolve_profile_demand(self.demand("important", **{field: value}))

    def test_malformed_cfg_declarations_fail_loudly(self):
        cases = {
            "missing equals": "CFG_MODEL_PROFILE_GRANULARITY\n",
            "invalid key": "CFG_model_profile=full\n",
            "empty value": "CFG_MODEL_PROFILE_GRANULARITY=\n",
            "unsafe value": "CFG_MODEL_PROFILE_GRANULARITY=full+collapsed\n",
        }
        for label, config_text in cases.items():
            with tempfile.NamedTemporaryFile(
                "w", suffix=".conf", delete=False
            ) as handle:
                handle.write(config_text)
                path = handle.name
            try:
                with self.subTest(label=label), self.assertRaises(
                    PROFILE.ModelProfileError
                ) as caught:
                    PROFILE.load_config(path)
                self.assertIn("line 1", str(caught.exception))
            finally:
                Path(path).unlink()

    def test_unrelated_non_cfg_lines_remain_ignored(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as handle:
            handle.write("not a declaration\nOTHER=value\nCFG_MODEL_PROFILE_GRANULARITY=full\n")
            path = handle.name
        try:
            self.assertEqual(
                PROFILE.load_config(path),
                {"CFG_MODEL_PROFILE_GRANULARITY": "full"},
            )
        finally:
            Path(path).unlink()

    @staticmethod
    def _declared_point(config, profile):
        """Independent derivation of a profile's operating point from the raw
        CFG_ keys (tier -> CFG_TIER_<TIER>_MODEL / _EFFORT|_VARIANT), so the
        expectation follows the shipped file instead of a literal table."""
        return declared_point(config, profile)

    @staticmethod
    def _reachable_models(adapter, config):
        """Every concrete model this config can put in front of a worker: the declared
        tier models plus each of the five profiles' resolved model. A profile may name a
        model directly (`model/<id>:budget`), so scanning `CFG_TIER_*_MODEL` alone leaves
        a hole an edit can walk through (review MA-2a). Resolution goes through the values
        already parsed here, not through a second read of the file — one value, one source.
        """
        models = {value for key, value in config.items()
                  if key.endswith("_MODEL") and key != "CFG_TIER_TOP_MODEL"}
        for profile in ("deep", "balanced-deep", "balanced", "light", "mini"):
            models.add(PROFILE.resolve_profile_values(adapter, config, profile)["model"])
        return models

    @staticmethod
    def _restricted(model, aliases):
        """The predicate every launch surface actually uses: tokenize the id and test
        alias membership (`dispatch-headless.py`, `stage-dispatch-fallback.py`,
        `hooks/subagent-model-default.sh` all do exactly this). Exact string equality
        would see `model/alias:...` but not `model/vendor-alias-5-1:...`, which the
        wrapper still refuses at launch (review r2-1)."""
        tokens = set(re.split(r"[^a-z0-9]+", model.lower()))
        return any(alias.lower() in tokens for alias in aliases)

    def test_claude_shipped_default_is_the_user_profile_mapping(self):
        # 2026-09-09 user rule: the shipped default equals the user's runtime
        # mapping — the declared top model is reserved for the main session, so
        # both deep-side profiles follow this adapter's configured model and separate by effort only.
        config = PROFILE.load_config(ROOT / "adapters" / "claude" / "config" / "models.conf")
        main_only = config["CFG_MAIN_SESSION_ONLY_MODELS"].split()
        self.assertEqual(main_only, [config["CFG_TIER_TOP_MODEL"]])
        self.assertEqual(self._declared_point(config, "deep"),
                         (config["CFG_TIER_DEEP_MODEL"], config["CFG_TIER_DEEP_EFFORT"]))
        balanced_deep = self._declared_point(config, "balanced-deep")
        self.assertEqual(balanced_deep[0], config["CFG_TIER_DEEP_MODEL"])
        self.assertNotEqual(balanced_deep[1], self._declared_point(config, "deep")[1])
        cascade = [entry.split(":", 1)[0] for entry in config["CFG_TIER_DEEP_FAILOVER_CASCADE"].split()]
        self.assertEqual(cascade[0], config["CFG_TIER_DEEP_MODEL"])
        self.assertEqual(len(cascade), 2)
        self.assertEqual(cascade[0], config["CFG_TIER_DEEP_MODEL"])
        # The role mappers read CFG_TIER_DEEP_EFFORT directly while route-bound work
        # reads the profile's budget; a config where the two disagree silently makes
        # the README's `deep reviewer` row false, so pin them equal (review MI-4).
        self.assertEqual(config["CFG_TIER_DEEP_EFFORT"], "xhigh")
        self.assertEqual(config["CFG_TIER_DEEP_EFFORT"], self._declared_point(config, "deep")[1])
        # A main-session-only model may never appear in a dispatch-eligible tier, in the
        # cascade the fallback walks, or as ANY profile's resolved model — the last of
        # which is the `model/<id>:budget` profile form a tier-key scan cannot see
        # (review MA-2a).
        self.assertNotIn(config["CFG_TIER_DEEP_MODEL"], main_only)
        self.assertEqual([model for model in cascade if model in main_only], [])
        self.assertEqual(
            [m for m in self._reachable_models("claude", config) if self._restricted(m, main_only)],
            [],
        )
        # The one sanctioned exception (2026-09-09 decision, second half): the
        # `top` profile resolves to the main-only model on purpose, at the CLI's
        # highest effort, and nothing else reaches it.
        top = PROFILE.resolve_profile_values("claude", config, "top")
        declared_top = self._declared_point(config, "top")
        self.assertEqual((top["model"], top["budget"], top["tier"]),
                         (declared_top[0], declared_top[1], "top"))
        self.assertTrue(self._restricted(top["model"], main_only))
        self.assertEqual(config["CFG_TIER_DEEP_FAILOVER"], "light")
        points = {profile: self._declared_point(config, profile) for profile in ("deep", "balanced-deep", "balanced", "light", "mini")}
        self.assertNotEqual(points["deep"], points["balanced-deep"])  # two distinct operating points
        self.assertEqual(len(set(points.values())), 5)  # five distinct operating points

    def test_codex_shipped_default_is_the_user_profile_mapping(self):
        # Same 2026-09-09 rule on the Codex adapter: the configured top model
        # stays with the main session. Tier expectations below come from config.
        config = PROFILE.load_config(ROOT / "adapters" / "codex" / "config" / "models.conf")
        self.assertEqual(self._declared_point(config, "deep"),
                         (config["CFG_TIER_DEEP_MODEL"], config["CFG_TIER_DEEP_EFFORT"]))
        balanced_deep = self._declared_point(config, "balanced-deep")
        self.assertEqual(balanced_deep[0], config["CFG_TIER_DEEP_MODEL"])
        self.assertNotEqual(balanced_deep[1], self._declared_point(config, "deep")[1])
        cascade = [entry.split(":", 1)[0] for entry in config["CFG_TIER_DEEP_FAILOVER_CASCADE"].split()]
        self.assertEqual(cascade[0], config["CFG_TIER_DEEP_MODEL"])
        self.assertEqual(config["CFG_TIER_DEEP_EFFORT"], "xhigh")  # review MI-4, as above
        self.assertEqual(config["CFG_TIER_DEEP_EFFORT"], self._declared_point(config, "deep")[1])
        # Every way a model can be reached — tier keys, the cascade, and each profile's
        # RESOLVED model (which covers the `model/<id>:budget` form a key scan misses,
        # review MA-2a). `tools/check-model-config.py` separately refuses the literal
        # anywhere outside this file.
        reachable = set(cascade) | self._reachable_models("codex", config)
        restricted = config["CFG_MAIN_SESSION_ONLY_MODELS"].split()
        self.assertNotIn(config["CFG_TIER_DEEP_MODEL"], restricted)
        self.assertEqual([m for m in reachable if self._restricted(m, restricted)], [])
        top = PROFILE.resolve_profile_values("codex", config, "top")
        declared_top = self._declared_point(config, "top")
        self.assertEqual((top["model"], top["budget"], top["tier"]),
                         (declared_top[0], declared_top[1], "top"))

    def test_portable_profiles_resolve_to_declared_adapter_budgets(self):
        claude_config = PROFILE.load_config(ROOT / "adapters" / "claude" / "config" / "models.conf")
        codex_config = PROFILE.load_config(ROOT / "adapters" / "codex" / "config" / "models.conf")
        expected = {
            # Five profiles use the configured judgment and execution budgets.
            # Claude expectations derive from the shipped config (the user's
            # runtime mapping is the shipped default, 2026-09-09); the concrete
            # contract itself is asserted in
            # test_claude_shipped_default_is_the_user_profile_mapping.
            "claude": {
                profile: self._declared_point(claude_config, profile)
                for profile in ("deep", "balanced-deep", "balanced", "light", "mini")
            },
            # Codex expectations derive from its shipped config for the same
            # reason as Claude's; the concrete contract is asserted in
            # test_codex_shipped_default_is_the_user_profile_mapping.
            "codex": {
                profile: self._declared_point(codex_config, profile)
                for profile in ("deep", "balanced-deep", "balanced", "light", "mini")
            },
            # OpenCode declares three tiers (balanced-deep, light, mini) with a
            # verified variant each (2026-09-30 사용자 결정): `deep` collapses onto
            # balanced-deep (no deep tier) and `balanced` onto light.
            "opencode": {profile: self._declared_point(
                PROFILE.load_config(ROOT / "adapters" / "opencode" / "config" / "models.conf"), profile)
                for profile in ("deep", "balanced-deep", "balanced", "light", "mini")},
        }
        for adapter, profiles in expected.items():
            for profile, pair in profiles.items():
                with self.subTest(adapter=adapter, profile=profile):
                    resolved = PROFILE.resolve_profile(
                        adapter,
                        ROOT / "adapters" / adapter / "config" / "models.conf",
                        profile,
                    )
                    self.assertEqual((resolved["model"], resolved["budget"]), pair)

    def test_route_profile_is_primary_and_preserves_semantic_role(self):
        for adapter, wrapper in WRAPPERS.items():
            with self.subTest(adapter=adapter):
                resolved = wrapper.resolve_model_settings(args(adapter, "balanced-deep"))
                self.assertEqual(resolved["source"], "profile")
                self.assertEqual(resolved["role"], "fast implementer")
                self.assertEqual(resolved["profile"], "balanced-deep")
                self.assertNotEqual(resolved["model"], "inherit")

    def test_runtime_profile_prefers_complete_user_config(self):
        adapter = "codex"
        shipped = (ROOT / "adapters" / adapter / "config" / "models.conf").read_text()
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            user = home / "agent-config" / "models.conf"
            user.parent.mkdir()
            # Replace the line whatever the shipped model is, so a model refresh
            # cannot silently turn this into a no-op edit.
            user.write_text(
                re.sub(r"(?m)^CFG_TIER_DEEP_MODEL=.*$", "CFG_TIER_DEEP_MODEL=user/deep", shipped, count=1)
            )
            resolved, receipt = PROFILE.resolve_runtime_profile(
                adapter, "deep", runtime=home, source_root=ROOT
            )
            self.assertEqual(resolved["model"], "user/deep")
            self.assertEqual(receipt.source, "user")

    def test_owner_profile_does_not_need_a_stage_role(self):
        for adapter, wrapper in WRAPPERS.items():
            with self.subTest(adapter=adapter):
                resolved = wrapper.resolve_model_settings(args(
                    adapter, "deep", dispatch_depth=1, worker_type="owner", model_role=None
                ))
                self.assertEqual(resolved["role"], "_kernel/owner")
                self.assertEqual(resolved["profile"], "deep")

    def test_mini_is_denied_for_registered_substantive_topology(self):
        for adapter, wrapper in WRAPPERS.items():
            with self.subTest(adapter=adapter), self.assertRaises(wrapper.ModelSelectionError) as caught:
                wrapper.resolve_model_settings(args(adapter, "mini"))
            self.assertEqual(caught.exception.reason, "invalid-dispatch-model-profile")

    def test_concrete_override_requires_checked_capacity_retry(self):
        cases = {
            "claude": {"model": "synthetic-claude-model", "effort": "medium"},
            "codex": {"model": "synthetic-codex-model", "reasoning": "medium"},
            "opencode": {"model": "synthetic-opencode/model", "variant": "runtime-default"},
        }
        for adapter, concrete in cases.items():
            wrapper = WRAPPERS[adapter]
            with self.subTest(adapter=adapter), self.assertRaises(wrapper.ModelSelectionError) as caught:
                wrapper.resolve_model_settings(args(adapter, "deep", **concrete))
            self.assertEqual(caught.exception.reason, "model-profile-override-forbidden")
            resolved = wrapper.resolve_model_settings(
                args(adapter, "deep", capacity_retry=1, **concrete)
            )
            self.assertEqual(resolved["source"], "profile+capacity")
            self.assertEqual(resolved["context_window"], "800000")
            self.assertEqual(resolved["model"], concrete["model"])

    def test_context_fields_survive_role_explicit_pin_and_capacity_selection(self):
        budget_key = {"claude": "effort", "codex": "reasoning", "opencode": "variant"}
        for adapter, wrapper in WRAPPERS.items():
            def attach(settings, _harness, *, error):
                settings["context_window"] = "600000"
                settings["context_window_source"] = "configured"
                return settings

            with self.subTest(adapter=adapter), mock.patch.object(
                    wrapper.WRAPPER_COMMON, "attach_context_window", side_effect=attach):
                profile = wrapper.resolve_model_settings(args(adapter, "deep"))
                role = wrapper.resolve_model_settings(args(
                    adapter, "deep", model_profile=None, model_role="fast implementer"))
                explicit = wrapper.resolve_model_settings(args(
                    adapter, "deep", model_profile=None, model_role=None,
                    model="vendor/explicit-model", **{budget_key[adapter]: "high"}))
                capacity = wrapper.resolve_model_settings(args(
                    adapter, "deep", model="vendor/capacity-model",
                    **{budget_key[adapter]: "high", "capacity_retry": 1}))
                with mock.patch.object(wrapper, "route_selection_pin", return_value={
                        "status": "applied", "model": "vendor/pinned-model", "effort": "xhigh"}):
                    pin = wrapper.resolve_model_settings(args(adapter, "deep"))
                    pin_capacity = wrapper.resolve_model_settings(args(
                        adapter, "deep", model="vendor/retry-model",
                        **{budget_key[adapter]: "high", "capacity_retry": 1}))
                for resolved in (profile, role, explicit, capacity, pin, pin_capacity):
                    self.assertEqual(resolved["context_window"], "600000")
                    self.assertEqual(resolved["context_window_source"], "configured")
                self.assertEqual(profile["source"], "profile")
                self.assertEqual(role["source"], "role")
                self.assertEqual(explicit["source"], "explicit")
                self.assertEqual(capacity["source"], "profile+capacity")
                self.assertEqual(pin["source"], "pin")
                self.assertEqual(pin_capacity["source"], "pin+capacity")

    def test_claude_launch_budget_overrides_stale_env_without_changing_hook_settings(self):
        wrapper = WRAPPERS["claude"]
        env = {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "300000",
               "CLAUDE_CONFIG_DIR": "/runtime/claude",
               "CLAUDE_HOOK_SETTINGS": '{"PreToolUse":["user-guard"]}'}
        result = wrapper.apply_context_window_environment(
            env, {"context_window": "600000"})
        self.assertIs(result, env)
        self.assertEqual(env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "600000")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/runtime/claude")
        self.assertEqual(env["CLAUDE_HOOK_SETTINGS"], '{"PreToolUse":["user-guard"]}')
    def test_opencode_live_conf_collapses_deep_onto_balanced_deep_and_names_it(self):
        # 2026-09-30 사용자 결정: OpenCode declares no deep tier ("do not use it
        # for deep work"), so `deep` resolves onto balanced-deep and the
        # per-profile granularity key says so; balanced-deep itself is exact.
        conf = ROOT / "adapters" / "opencode" / "config" / "models.conf"
        balanced = PROFILE.resolve_profile("opencode", conf, "balanced-deep")
        self.assertEqual(balanced["tier"], "balanced-deep")
        config = PROFILE.load_config(conf)
        expected_model = PROFILE.resolve_profile_values("opencode", config, "balanced-deep")["model"]
        self.assertEqual(balanced["model"], expected_model)
        self.assertEqual(balanced["granularity"], "full")

        deep = PROFILE.resolve_profile("opencode", conf, "deep")
        self.assertEqual(deep["tier"], "balanced-deep")
        self.assertEqual(deep["model"], balanced["model"])
        self.assertEqual(deep["granularity"], "collapsed-deep-to-balanced-deep")
        self.assertNotIn("CFG_TIER_DEEP_MODEL", PROFILE.load_config(conf))

    def test_per_profile_granularity_key_supports_typed_demotion(self):
        # Mechanism guard for CFG_MODEL_PROFILE_GRANULARITY_<PROFILE>: an adapter
        # with a vacant tier may demote a profile and record it per-profile
        # without touching the file-wide granularity value.
        import os
        import tempfile
        conf_text = (
            "CFG_TIER_BALANCED_DEEP_MODEL=opencode-go/glm-5.2\n"
            "CFG_TIER_BALANCED_DEEP_VARIANT=runtime-default\n"
            "CFG_TIER_LIGHT_MODEL=opencode-go/deepseek-v4-flash\n"
            "CFG_TIER_LIGHT_VARIANT=runtime-default\n"
            "CFG_TIER_MINI_MODEL=opencode-go/deepseek-v4-flash\n"
            "CFG_TIER_MINI_VARIANT=runtime-default\n"
            "CFG_MODEL_PROFILE_DEEP=balanced-deep:runtime-default\n"
            "CFG_MODEL_PROFILE_BALANCED_DEEP=balanced-deep:runtime-default\n"
            "CFG_MODEL_PROFILE_LIGHT=light:runtime-default\n"
            "CFG_MODEL_PROFILE_MINI=mini:runtime-default\n"
            "CFG_MODEL_PROFILE_GRANULARITY=exact\n"
            "CFG_MODEL_PROFILE_GRANULARITY_DEEP=deep-vacant-demoted-to-balanced-deep\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as handle:
            handle.write(conf_text)
            path = handle.name
        try:
            deep = PROFILE.resolve_profile("opencode", path, "deep")
            self.assertEqual(deep["tier"], "balanced-deep")
            self.assertEqual(deep["model"], "opencode-go/glm-5.2")
            self.assertEqual(deep["granularity"], "deep-vacant-demoted-to-balanced-deep")
            balanced = PROFILE.resolve_profile("opencode", path, "balanced-deep")
            self.assertEqual(balanced["granularity"], "exact")
        finally:
            os.unlink(path)

    def test_claude_codex_granularity_unaffected_by_per_profile_key(self):
        for adapter, expected in {"claude": "full", "codex": "full"}.items():
            conf = ROOT / "adapters" / adapter / "config" / "models.conf"
            for profile in ("deep", "balanced-deep"):
                with self.subTest(adapter=adapter, profile=profile):
                    resolved = PROFILE.resolve_profile(adapter, conf, profile)
                    self.assertEqual(resolved["granularity"], expected)

    def test_opencode_passes_a_declared_variant_and_omits_runtime_default(self):
        wrapper = WRAPPERS["opencode"]

        def command_for(resolved):
            with tempfile.TemporaryDirectory() as temp_dir:
                return wrapper.shell_command(
                    argparse.Namespace(
                        resolved_model_settings=resolved,
                        worktree=temp_dir,
                        agent="build",
                    ),
                    Path(temp_dir) / "prompt.txt",
                    Path(temp_dir) / "worker.log",
                )

        resolved = wrapper.resolve_model_settings(args("opencode", "balanced-deep"))
        command = command_for(resolved)
        self.assertIn("--model", command)
        self.assertIn(f"--variant {resolved['variant']}", command)
        # `runtime-default` (a user copy that declares no variant) is still omitted.
        command = command_for({**resolved, "variant": "runtime-default"})
        self.assertIn("--model", command)
        self.assertNotIn("--variant", command)


class TopExceptionProfileTest(unittest.TestCase):
    """The `top` exception profile (2026-09-09 사용자 결정): one door above deep."""

    def demand(self, judgment):
        return {"schema_version": 1, "judgment_requirement": judgment, "execution_scope": "short-local",
                "judgment_reason": "final review of a structural hook change", "execution_reason": "one review",
                "evidence_refs": ["DESIGN.md"]}

    def test_top_is_known_but_not_portable(self):
        self.assertNotIn("top", PROFILE.PORTABLE_PROFILES)
        self.assertIn("top", PROFILE.KNOWN_PROFILES)
        self.assertEqual(PROFILE.EXCEPTION_PROFILES, ("top",))

    def test_each_adapter_declares_top_and_opencode_collapses_typed(self):
        for adapter in ("claude", "codex", "opencode"):
            with self.subTest(adapter=adapter):
                config = PROFILE.load_config(ROOT / "adapters" / adapter / "config" / "models.conf")
                resolved = PROFILE.resolve_profile(
                    adapter, ROOT / "adapters" / adapter / "config" / "models.conf", "top")
                declared = declared_point(config, "top")
                self.assertEqual((resolved["model"], resolved["budget"]), declared)
                self.assertEqual(resolved["tier"], config["CFG_MODEL_PROFILE_TOP"].split(":", 1)[0])
                self.assertEqual(resolved["granularity"], config.get("CFG_MODEL_PROFILE_GRANULARITY_TOP",
                                                                       config.get("CFG_MODEL_PROFILE_GRANULARITY")))

    def test_an_undeclared_top_profile_is_refused_typed_never_derived(self):
        config = PROFILE.load_config(ROOT / "adapters" / "claude" / "config" / "models.conf")
        without = {k: v for k, v in config.items() if not k.endswith("_TOP")}
        with self.assertRaises(PROFILE.ModelProfileError) as refused:
            PROFILE.resolve_profile_values("claude", without, "top")
        self.assertEqual(refused.exception.reason, "profile-top-undeclared")

    def test_explicit_top_never_comes_from_the_matrix(self):
        for judgment in ("predetermined", "important", "difficult-uncertain"):
            row = PROFILE.resolve_profile_demand(self.demand(judgment), explicit_profile="top")
            self.assertEqual((row["resolved_profile"], row["source"], row["reason"]),
                             ("top", "explicit", "explicit-top-exception"))
            PROFILE.validate_profile_selection(row, self.demand(judgment), profile="top")
            self.assertNotEqual(PROFILE.resolve_profile_demand(self.demand(judgment))["resolved_profile"], "top")
        row = PROFILE.resolve_profile_demand(None, explicit_profile="top")
        self.assertEqual(row["source"], "explicit")
        PROFILE.validate_profile_selection(row, profile="top")

    def test_top_is_limited_to_a_registered_depth_one_owner(self):
        PROFILE.validate_registered_profile("top", registered_worker=True, dispatch_depth=1, worker_type="owner")
        for kwargs in (dict(registered_worker=True, dispatch_depth=2, worker_type="stage"),
                       dict(registered_worker=True, dispatch_depth=2, worker_type="review"),
                       dict(registered_worker=True, dispatch_depth=1, worker_type="review"),
                       dict(registered_worker=False, dispatch_depth=1, worker_type="owner"),
                       dict(registered_worker=True, dispatch_depth=1, worker_type="support")):
            with self.subTest(**kwargs), self.assertRaises(PROFILE.ModelProfileError) as refused:
                PROFILE.validate_registered_profile("top", **kwargs)
            self.assertEqual(refused.exception.reason, "profile-top-depth-forbidden")

    def test_top_requires_the_route_that_sealed_it(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        PROFILE.require_top_route(None, profile="deep")  # other profiles need no route
        with self.assertRaises(PROFILE.ModelProfileError) as refused:
            PROFILE.require_top_route(None, profile="top")
        self.assertEqual(refused.exception.reason, "profile-top-route-required")
        route = tmp / "route.json"
        route.write_text('{"owner_model_profile": "balanced-deep"}', encoding="utf-8")
        with self.assertRaises(PROFILE.ModelProfileError) as mismatch:
            PROFILE.require_top_route(str(route), profile="top")
        self.assertEqual(mismatch.exception.reason, "profile-top-route-mismatch")
        route.write_text('{"owner_model_profile": "top"}', encoding="utf-8")
        PROFILE.require_top_route(str(route), profile="top")
        with self.assertRaises(PROFILE.ModelProfileError) as unreadable:
            PROFILE.require_top_route(str(tmp / "absent.json"), profile="top")
        self.assertEqual(unreadable.exception.reason, "profile-top-route-required")

    def test_a_named_node_is_checked_against_its_own_seal_not_the_owners(self):
        # The frame anchor reaches `top` while its owner sits at `deep`, so
        # checking the owner's seal for it would refuse a correctly compiled
        # route. `node=None` must keep meaning "check the owner".
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        route = tmp / "route.json"
        route.write_text(json.dumps({
            "owner_model_profile": "deep",
            "nodes": [{"id": "frame", "model_profile": "top"},
                      {"id": "frame-alternative", "model_profile": "deep"}],
        }), encoding="utf-8")
        # The node's own seal admits it.
        PROFILE.require_top_route(str(route), profile="top", node="frame")
        # The owner's seal is `deep`, so the owner door is still shut.
        with self.assertRaises(PROFILE.ModelProfileError) as owner_door:
            PROFILE.require_top_route(str(route), profile="top")
        self.assertEqual(owner_door.exception.reason, "profile-top-route-mismatch")
        # A node that exists but sealed something else is refused as a
        # mismatch, not silently admitted by its sibling's seal.
        with self.assertRaises(PROFILE.ModelProfileError) as sibling:
            PROFILE.require_top_route(str(route), profile="top", node="frame-alternative")
        self.assertEqual(sibling.exception.reason, "profile-top-route-mismatch")
        # A node the route never declared is a wiring bug and says so.
        with self.assertRaises(PROFILE.ModelProfileError) as unknown:
            PROFILE.require_top_route(str(route), profile="top", node="frame-contrarian")
        self.assertEqual(unknown.exception.reason, "profile-top-route-node-unknown")
        # Non-top profiles still need no route at all, node or not.
        PROFILE.require_top_route(None, profile="deep", node="frame")


class LowerLaunchSelectionReceiptTest(unittest.TestCase):
    """D2 seam: a node stays sealed at `top`; a launch at another profile passes `selection_receipt` only
    as the exact replacement a verified transition names. The claim itself is proven by
    `dispatch_replacement.read_profile_transition` (covered in dispatch_replacement.test.py and the
    per-adapter end-to-end cases); here the reader is the variable."""

    TRANSITION = {"from": "top", "to": "deep", "reason": "capacity", "ordinal": 1, "origin": "frame-rule"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        selection = PROFILE.resolve_profile_demand(None, explicit_profile="top")
        route = {"route_id": "rt-x", "route_hash": "sha256:x", "profile_selection_contract_version": 1,
                 "nodes": [{"id": "frame", "model_profile": "top", "profile_demand": None,
                            "profile_selection": selection}]}
        self.path = Path(self.tmp.name) / "route.json"
        self.path.write_text(json.dumps(route))
        self.selection = selection

    def launch(self, profile, **overrides):
        values = dict(route_file=str(self.path), route_node="frame", automatic_retry_of="att-source",
                      attempt_id="att-replacement", jobs="/tmp/never-read-jobs.log",
                      resolved_model_settings={"profile": profile})
        values.update(overrides)
        return argparse.Namespace(**values)

    def receipt(self, profile, reader, **overrides):
        from unittest import mock
        with mock.patch("dispatch_replacement.read_profile_transition", reader) as read:
            return PROFILE.selection_receipt(self.launch(profile, **overrides)), read

    def test_the_sealed_profile_never_asks_for_a_transition(self):
        from unittest import mock
        receipt, read = self.receipt("top", mock.Mock(side_effect=AssertionError("asked")))
        read.assert_not_called()
        self.assertEqual(receipt["profile_selection_source"], "explicit")

    def test_a_verified_transition_admits_the_lower_launch_and_keeps_the_sealed_selection(self):
        from unittest import mock
        receipt, read = self.receipt("deep", mock.Mock(return_value=dict(self.TRANSITION)))
        read.assert_called_once_with("/tmp/never-read-jobs.log", route=mock.ANY, node="frame",
                                    attempt_id="att-replacement")
        self.assertEqual(read.call_args.kwargs["route"]["route_id"], "rt-x")
        self.assertEqual(receipt["profile_selection_source"], "explicit")
        self.assertEqual(receipt["profile_selection_digest"], PROFILE._digest(self.selection))

    def test_a_lower_launch_without_a_verified_claim_is_refused(self):
        from unittest import mock
        cases = {
            "no claim": dict(reader=mock.Mock(return_value=None)),
            "claim for another target": dict(reader=mock.Mock(return_value={**self.TRANSITION, "to": "balanced-deep"})),
            "not an automatic retry": dict(reader=mock.Mock(return_value=dict(self.TRANSITION)),
                                           automatic_retry_of=None),
            "no registry named": dict(reader=mock.Mock(return_value=dict(self.TRANSITION)), jobs=None),
        }
        for label, case in cases.items():
            with self.subTest(label), self.assertRaises(PROFILE.ModelProfileError) as refused:
                self.receipt("deep", case.pop("reader"), **case)
            self.assertEqual(refused.exception.reason, "profile-selection-mismatch")

    def test_a_claim_that_fails_its_proof_refuses_typed(self):
        import dispatch_contract as DC
        from unittest import mock
        reader = mock.Mock(side_effect=DC.DispatchContractError("replacement-not-silent-death"))
        with self.assertRaises(PROFILE.ModelProfileError) as refused:
            self.receipt("deep", reader)
        self.assertEqual(refused.exception.reason, "profile-selection-mismatch")

    def test_verified_pair_context_keeps_the_seal_and_emits_diversity_receipt(self):
        selection = PROFILE.resolve_profile_demand(None, explicit_profile="balanced-deep")
        route = {"route_id": "rt-pair", "route_hash": "sha256:pair",
                 "profile_selection_contract_version": 1,
                 "nodes": [{"id": "plan-b", "model_profile": "balanced-deep",
                            "profile_demand": None, "profile_selection": selection}]}
        path = Path(self.tmp.name) / "pair-route.json"
        path.write_text(json.dumps(route))
        args = argparse.Namespace(
            route_file=str(path), route_node="plan-b",
            resolved_model_settings={"profile": "balanced"},
            pair_model_context={"status": "rotated", "sealed_profile": "balanced-deep",
                                "profile": "balanced", "sibling_model": "fixture-model"},
        )
        receipt = PROFILE.selection_receipt(args)
        self.assertEqual(receipt["profile_selection_source"], "explicit")
        self.assertEqual(receipt["model_diversity"], "rotated")
        self.assertEqual(receipt["model_source"], "profile+diversity")


class FrameTierLadder(unittest.TestCase):
    def test_the_ladder_maps_every_owner_profile_to_its_frame_pair(self):
        expected = {
            "top": {"anchor": "top", "others": "top"},
            "deep": {"anchor": "top", "others": "top"},
            "balanced-deep": {"anchor": "top", "others": "top"},
            "balanced": {"anchor": "top", "others": "top"},
            "light": {"anchor": "top", "others": "top"},
        }
        for owner, rungs in expected.items():
            with self.subTest(owner=owner):
                self.assertEqual(PROFILE.frame_profile_for_owner(owner), rungs)

    def test_every_portable_owner_profile_has_a_rung(self):
        # A missing rung would silently fall back to `light` for a real owner
        # profile, which is the quiet-wrong-answer failure the table exists to
        # prevent.
        for profile in PROFILE.PORTABLE_PROFILES:
            if profile == "mini":
                continue  # `mini` is never an owner profile
            with self.subTest(profile=profile):
                self.assertIn(profile, PROFILE.FRAME_PROFILE_LADDER)

    def test_an_unknown_or_absent_owner_profile_frames_conservatively(self):
        for value in (None, "", "not-a-profile"):
            with self.subTest(value=value):
                self.assertEqual(PROFILE.frame_profile_for_owner(value),
                                 PROFILE.FRAME_PROFILE_LADDER["light"])
                self.assertEqual(PROFILE.frame_profile_for_owner(value), {"anchor": "top", "others": "top"})

    def test_the_returned_mapping_cannot_mutate_the_table(self):
        rungs = PROFILE.frame_profile_for_owner("deep")
        rungs["anchor"] = "mini"
        self.assertEqual(PROFILE.FRAME_PROFILE_LADDER["deep"]["anchor"], "top")

    def test_frame_is_a_top_worker_type_and_the_anchor_demand_is_valid(self):
        self.assertEqual(PROFILE.TOP_WORKER_TYPES, frozenset({"owner", "frame"}))
        # A registered depth-1 frame worker may carry `top`; a depth-2 one
        # still may not.
        PROFILE.validate_registered_profile(
            "top", registered_worker=True, dispatch_depth=1, worker_type="frame")
        with self.assertRaises(PROFILE.ModelProfileError) as deep_frame:
            PROFILE.validate_registered_profile(
                "top", registered_worker=True, dispatch_depth=2, worker_type="frame")
        self.assertEqual(deep_frame.exception.reason, "profile-top-depth-forbidden")
        # The shape demand must actually resolve `top`, or the compiler's
        # anchor sealing would fail at every standard+ compile.
        selection = PROFILE.resolve_profile_demand(
            PROFILE.FRAME_ANCHOR_SHAPE_DEMAND, explicit_profile="top")
        self.assertEqual(selection["resolved_profile"], "top")
        self.assertEqual(selection["reason"], "explicit-top-exception")


class PriorFrameProfilePolicyTest(unittest.TestCase):
    """The one prior frame policy (v2.167.1-v2.169.0) a verifier still accepts for a sealed route.

    The literals below are the values read from `git show v2.169.0:utilities/model_profile.py`; they
    are spelled out here on purpose, so a later edit of the production table cannot move them."""

    LADDER = {
        "top": {"anchor": "top", "others": "deep"},
        "deep": {"anchor": "top", "others": "deep"},
        "balanced-deep": {"anchor": "deep", "others": "deep"},
        "balanced": {"anchor": "balanced-deep", "others": "balanced-deep"},
        "light": {"anchor": "balanced", "others": "balanced"},
    }
    EXECUTION_REASON = ("one direction brief, written once, with no multi-step execution of "
                        "its own")

    def test_the_prior_ladder_is_the_one_released_before_the_top_top_default(self):
        for owner, rungs in self.LADDER.items():
            with self.subTest(owner=owner):
                self.assertEqual(PROFILE.frame_profile_for_owner(owner, prior=True), rungs)

    def test_the_default_policy_is_unchanged_and_differs_from_the_prior_one(self):
        for owner in self.LADDER:
            with self.subTest(owner=owner):
                self.assertEqual(PROFILE.frame_profile_for_owner(owner),
                                 {"anchor": "top", "others": "top"})
        self.assertNotEqual(PROFILE.frame_profile_for_owner("deep"),
                            PROFILE.frame_profile_for_owner("deep", prior=True))

    def test_an_unknown_or_absent_owner_profile_takes_the_prior_light_rung(self):
        for value in (None, "", "not-a-profile"):
            with self.subTest(value=value):
                self.assertEqual(PROFILE.frame_profile_for_owner(value, prior=True),
                                 self.LADDER["light"])

    def test_the_returned_prior_mapping_cannot_mutate_the_table(self):
        rungs = PROFILE.frame_profile_for_owner("deep", prior=True)
        rungs["others"] = "mini"
        self.assertEqual(PROFILE.PRIOR_FRAME_PROFILE_LADDER["deep"]["others"], "deep")

    def test_the_prior_intrinsic_demand_differs_only_in_the_execution_reason(self):
        prior = PROFILE.frame_anchor_shape_demand(prior=True)
        current = PROFILE.frame_anchor_shape_demand()
        self.assertEqual(current, PROFILE.FRAME_ANCHOR_SHAPE_DEMAND)
        self.assertEqual(prior["execution_reason"], self.EXECUTION_REASON)
        self.assertNotEqual(prior["execution_reason"], current["execution_reason"])
        self.assertEqual({k: v for k, v in prior.items() if k != "execution_reason"},
                         {k: v for k, v in current.items() if k != "execution_reason"})
        prior["evidence_refs"].append("x")
        self.assertEqual(PROFILE.frame_anchor_shape_demand(prior=True)["evidence_refs"],
                         ["roles/units/plan/frame.md"])

    def test_the_prior_demand_resolves_top_and_hashes_differently(self):
        prior = PROFILE.frame_anchor_shape_demand(prior=True)
        selection = PROFILE.resolve_profile_demand(prior, explicit_profile="top")
        self.assertEqual(selection["resolved_profile"], "top")
        self.assertNotEqual(
            PROFILE.normalize_profile_demand(prior),
            PROFILE.normalize_profile_demand(PROFILE.FRAME_ANCHOR_SHAPE_DEMAND))


if __name__ == "__main__":
    unittest.main()
