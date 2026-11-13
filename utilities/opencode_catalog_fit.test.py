#!/usr/bin/env python3
"""OpenCode launch fits the saved model/variant to the service catalog.

Three real configurations drive the whole settings path: the fixture
``models.conf`` is read by ``resolve_model_settings`` and the launch argv is
read back from ``shell_command``. The catalog and the availability evidence are
fixtures; saved settings are never rewritten.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import os
import shlex
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "opencode_catalog_fit_wrapper", ROOT / "adapters" / "opencode" / "bin" / "dispatch-headless.py")
WRAPPER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(WRAPPER)
sys.path.insert(0, str(ROOT / "utilities"))
import model_config  # noqa: E402

SHIPPED_CONF = ROOT / "adapters" / "opencode" / "config" / "models.conf"

CATALOG_VARIANTS = {
    "opencode-go/muse-spark-1.3-contributor": ("minimal", "low", "medium", "high", "xhigh"),
    "opencode-go/deepseek-v4.1-flash": ("low", "high", "max"),
    "opencode/deepseek-v4.1-flash": ("low", "high", "max"),
    "fixture/gemini-4-argon": ("low", "medium", "high"),
}
FULL_CATALOG = {"state": "available", "source": "fixture",
                "models": tuple(CATALOG_VARIANTS), "variants": CATALOG_VARIANTS}
SINGLE_PROVIDER_CATALOG = {
    "state": "available", "source": "fixture",
    "models": ("opencode-go/muse-spark-1.3-contributor", "opencode-go/deepseek-v4.1-flash",
               "fixture/gemini-4-argon"),
    "variants": {key: value for key, value in CATALOG_VARIANTS.items()
                 if key != "opencode/deepseek-v4.1-flash"},
}


def eligibility(model, **_kwargs):
    # opencode-go is usable; the paid provider is unverified and not automatic.
    eligible = model.startswith("opencode-go/")
    return {"state": "usable" if eligible else "unknown", "automatic_eligible": eligible,
            **({} if eligible else {"reason": "provider-model-unverified"})}


def selection(**values):
    return SimpleNamespace(
        inherit_model_settings=False, model_profile=values.get("profile"),
        model_role=values.get("role"), model=values.get("model"), variant=values.get("variant"),
        registered_worker=1, dispatch_depth=1, worker_type=values.get("worker_type", "owner"),
        capacity_retry=0, route_file=None, worktree=None)


class CatalogFitTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = mock.patch.dict(os.environ, {
            "AGENT_HOME": str(ROOT), "HOME": str(self.tmp),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_DATA_HOME": str(self.tmp / "data"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "OPENCODE_RUNTIME_VERSION": "2.0.32",
        })
        env.start()
        self.addCleanup(env.stop)
        self.write_models_conf({})

    def write_models_conf(self, replacements):
        lines = []
        remaining = dict(replacements)
        for line in SHIPPED_CONF.read_text(encoding="utf-8").splitlines():
            key = line.partition("=")[0]
            if key in remaining:
                line = f"{key}={remaining.pop(key)}"
            lines.append(line)
        self.assertFalse(remaining, f"keys not in the shipped conf: {sorted(remaining)}")
        path = model_config.user_path("opencode", environ=os.environ)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def resolve(self, catalog, **values):
        with mock.patch.object(WRAPPER.opencode_runtime, "read_model_catalog",
                               return_value=catalog), \
             mock.patch.object(WRAPPER.opencode_runtime, "model_availability",
                               side_effect=eligibility):
            return WRAPPER.resolve_model_settings(selection(**values))

    def argv_model(self, settings):
        args = SimpleNamespace(
            jobs_path=str(self.tmp / "jobs.log"), attempt_id="att-fit-fixture",
            worktree=str(self.tmp), agent="build", route_file=None, route_id=None,
            route_hash=None, route_node=None, owner_route_binding=None,
            dispatch_depth=2, worker_type="stage", intensity="standard",
            resolved_model_settings=settings)
        command = WRAPPER.shell_command(args, self.tmp / "prompt.txt", self.tmp / "log.txt")
        tokens = shlex.split(command)
        self.assertNotIn("--variant", tokens)
        return tokens[tokens.index("--model") + 1]

    def test_light_muse_spark_max_runs_at_the_highest_observed_value(self):
        self.write_models_conf({
            "CFG_TIER_LIGHT_MODEL": "opencode-go/muse-spark-1.3-contributor",
            "CFG_TIER_LIGHT_VARIANT": "max"})
        settings = self.resolve(FULL_CATALOG, profile="light")
        self.assertEqual(self.argv_model(settings),
                         "opencode-go/muse-spark-1.3-contributor#xhigh")
        self.assertEqual(WRAPPER.catalog_fit_fields(settings), {
            "variant_fit_from": "max", "catalog_fit_observer": "resolver-catalog"})

    def test_mini_bare_deepseek_takes_the_eligible_provider_and_fits_the_variant(self):
        self.write_models_conf({
            "CFG_TIER_MINI_MODEL": "deepseek-v4.1-flash", "CFG_TIER_MINI_VARIANT": "xhigh",
            "CFG_MODEL_PROFILE_MINI": "mini:xhigh"})
        settings = self.resolve(FULL_CATALOG, profile="mini", worker_type="lifecycle", role="lifecycle helper")
        self.assertEqual(self.argv_model(settings), "opencode-go/deepseek-v4.1-flash#high")
        self.assertEqual(WRAPPER.catalog_fit_fields(settings), {
            "model_fit_from": "deepseek-v4.1-flash", "variant_fit_from": "xhigh",
            "catalog_fit_observer": "resolver-catalog"})
        self.assertEqual(settings["source"], "profile")

    def test_mini_bare_deepseek_with_a_single_provider_needs_no_eligibility(self):
        self.write_models_conf({
            "CFG_TIER_MINI_MODEL": "deepseek-v4.1-flash", "CFG_TIER_MINI_VARIANT": "xhigh",
            "CFG_MODEL_PROFILE_MINI": "mini:xhigh"})
        with mock.patch.object(WRAPPER.opencode_runtime, "read_model_catalog",
                               return_value=SINGLE_PROVIDER_CATALOG), \
             mock.patch.object(WRAPPER.opencode_runtime, "model_availability",
                               side_effect=eligibility):
            settings = WRAPPER.resolve_model_settings(selection(profile="mini", worker_type="lifecycle", role="lifecycle helper"))
        self.assertEqual(self.argv_model(settings), "opencode-go/deepseek-v4.1-flash#high")

    def test_mini_bare_deepseek_stays_as_saved_when_no_provider_is_eligible(self):
        self.write_models_conf({
            "CFG_TIER_MINI_MODEL": "deepseek-v4.1-flash", "CFG_TIER_MINI_VARIANT": "xhigh",
            "CFG_MODEL_PROFILE_MINI": "mini:xhigh"})
        with mock.patch.object(WRAPPER.opencode_runtime, "read_model_catalog",
                               return_value=FULL_CATALOG), \
             mock.patch.object(WRAPPER.opencode_runtime, "model_availability",
                               return_value={"state": "unknown", "automatic_eligible": False}):
            settings = WRAPPER.resolve_model_settings(selection(profile="mini", worker_type="lifecycle", role="lifecycle helper"))
        self.assertEqual(self.argv_model(settings), "deepseek-v4.1-flash#xhigh")
        self.assertEqual(WRAPPER.catalog_fit_fields(settings), {})

    def test_argon_xhigh_runs_at_high(self):
        self.write_models_conf({
            "CFG_TIER_BALANCED_DEEP_MODEL": "fixture/gemini-4-argon",
            "CFG_TIER_BALANCED_DEEP_VARIANT": "xhigh"})
        settings = self.resolve(FULL_CATALOG, profile="deep")
        self.assertEqual(self.argv_model(settings), "fixture/gemini-4-argon#high")
        self.assertEqual(WRAPPER.catalog_fit_fields(settings), {
            "variant_fit_from": "xhigh", "catalog_fit_observer": "resolver-catalog"})

    def test_explicit_pair_is_fitted_and_a_supported_pair_leaves_no_receipt_keys(self):
        settings = self.resolve(
            FULL_CATALOG, model="fixture/gemini-4-argon", variant="max")
        self.assertEqual(self.argv_model(settings), "fixture/gemini-4-argon#high")
        self.assertEqual(settings["source"], "explicit")
        supported = self.resolve(
            FULL_CATALOG, model="fixture/gemini-4-argon", variant="medium")
        self.assertEqual(self.argv_model(supported), "fixture/gemini-4-argon#medium")
        self.assertEqual(WRAPPER.catalog_fit_fields(supported), {})
        self.assertFalse({"model_fit_from", "variant_fit_from", "catalog_fit_observer"}
                         & set(supported))

    def test_unobserved_catalog_and_off_ladder_values_run_as_requested(self):
        unknown = {"state": "unknown", "source": "fixture", "models": (), "variants": {}}
        settings = self.resolve(unknown, model="fixture/gemini-4-argon", variant="xhigh")
        self.assertEqual(self.argv_model(settings), "fixture/gemini-4-argon#xhigh")
        self.assertEqual(WRAPPER.catalog_fit_fields(settings), {})
        off_ladder = self.resolve(
            FULL_CATALOG, model="fixture/gemini-4-argon", variant="auto")
        self.assertEqual(self.argv_model(off_ladder), "fixture/gemini-4-argon#auto")
        self.assertEqual(WRAPPER.catalog_fit_fields(off_ladder), {})

    def test_fitted_model_still_passes_the_headless_main_session_gate(self):
        with mock.patch.object(
                WRAPPER, "_model_policy",
                return_value={"CFG_MAIN_SESSION_ONLY_MODELS": "flash"}):
            with self.assertRaises(WRAPPER.ModelSelectionError) as refused:
                self.resolve(FULL_CATALOG, model="deepseek-v4.1-flash", variant="high")
        self.assertEqual(refused.exception.reason, "headless-main-session-only-model")

    def test_saved_settings_files_are_not_rewritten(self):
        self.write_models_conf({
            "CFG_TIER_LIGHT_MODEL": "opencode-go/muse-spark-1.3-contributor",
            "CFG_TIER_LIGHT_VARIANT": "max"})
        path = model_config.user_path("opencode", environ=os.environ)
        before = path.read_bytes()
        self.resolve(FULL_CATALOG, profile="light")
        self.assertEqual(path.read_bytes(), before)

    def test_v1_runtime_keeps_the_variant_flag_and_ignores_the_catalog(self):
        self.write_models_conf({
            "CFG_TIER_LIGHT_MODEL": "opencode-go/muse-spark-1.3-contributor",
            "CFG_TIER_LIGHT_VARIANT": "max"})
        with mock.patch.dict(os.environ, {"OPENCODE_RUNTIME_VERSION": "1.19.0"}), \
             mock.patch.object(WRAPPER.opencode_runtime, "read_model_catalog",
                               side_effect=AssertionError("V1 reads no catalog")):
            settings = WRAPPER.resolve_model_settings(selection(profile="light"))
            args = SimpleNamespace(
                jobs_path=str(self.tmp / "jobs.log"), attempt_id=None, worktree=str(self.tmp),
                agent="build", route_file=None, route_id=None, route_hash=None,
                route_node=None, owner_route_binding=None, dispatch_depth=2,
                worker_type="stage", intensity="standard", resolved_model_settings=settings)
            command = WRAPPER.shell_command(args, self.tmp / "prompt.txt", self.tmp / "log.txt")
        self.assertIn("--variant max", command)
        self.assertEqual(WRAPPER.catalog_fit_fields(settings), {})


if __name__ == "__main__":
    unittest.main()
