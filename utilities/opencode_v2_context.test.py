#!/usr/bin/env python3
"""V2 model-limit projection tests using the native API response contract."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import opencode_context_window as context


def model_info(*, context_limit=900000, input_limit=600000, output_limit=32000,
               enabled=True, status="active", model_id="catalog-name",
               provider="fixture", transport_id="provider-model", variants=None):
    limits = {"context": context_limit, "output": output_limit}
    if input_limit is not ...:
        limits["input"] = input_limit
    row = {
        "id": model_id,
        "modelID": transport_id,
        "providerID": provider,
        "name": "Fixture model",
        "status": status,
        "enabled": enabled,
        "limit": limits,
    }
    if variants is not None:
        row["variants"] = variants
    return row


class OpenCodeV2ContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opencode-v2-context-")
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)
        self.location = {"directory": str(self.cwd)}

    def model_response(self, *rows):
        return {"location": self.location, "data": list(rows)}

    def default_response(self, row):
        return {"location": self.location, "data": row}

    def test_v2_uses_exact_catalog_key_native_provider_and_reobserves_cap(self):
        selected = model_info(context_limit=900000, input_limit=700000,
                              output_limit=32000, model_id="friendly", transport_id="real-api-id")
        applied = model_info(context_limit=650000, input_limit=500000,
                             output_limit=18000, model_id="friendly", transport_id="real-api-id")
        config = {
            "model": "fixture/friendly#quality",
            "provider": {"fixture": {"models": {"friendly": {"limit": {
                "context": 650000, "input": 500000, "output": 18000,
            }}}}},
            "providers": {"fixture": {"settings": {"baseURL": "http://provider.invalid"},
                "models": {"friendly": {"modelID": "real-api-id", "limit": {
                    "context": 850000, "input": 650000, "output": 24000,
                }, "variants": {"quality": {"reasoningEffort": "high"}}}}}},
            "plugin": ["retained-plugin"],
            "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
        }
        responses = [self.model_response(selected), self.model_response(applied)]
        with mock.patch.object(context, "_installed_version",
                               side_effect=AssertionError("launch version was re-probed")), \
                mock.patch.object(context, "_api_json", side_effect=responses) as api:
            launch = context.prepare_launch_context(
                model="fixture/friendly#quality", variant="quality",
                context_window="800000", config_content=json.dumps(config),
                cwd=str(self.cwd), environ={"OPENCODE_RUNTIME_VERSION": "2.0.32"},
                command="opencode-fixture",
            )

        self.assertEqual([call.args[0] for call in api.call_args_list], [
            "/api/model", "/api/model",
        ])
        self.assertEqual(launch["model"], "fixture/friendly#quality")
        self.assertEqual(launch["variant"], "quality")
        self.assertEqual(launch["catalog_limits"], {"context": None, "input": None, "output": None})
        self.assertEqual(launch["configured_limits"], {"context": None, "input": None, "output": None})
        self.assertEqual(launch["native_limits"], {"context": 900000, "input": 700000, "output": 32000})
        self.assertEqual(launch["applied_limits"], {"context": 650000, "input": 500000, "output": 18000})
        self.assertEqual(launch["final_limits"], launch["applied_limits"])
        self.assertEqual(launch["native_application"],
                         "standalone-api:OPENCODE_CONFIG_CONTENT:providers-model-limit")
        self.assertEqual(launch["execution_mode"], "standalone")
        self.assertEqual(launch["runtime_target"], "standalone-per-command")
        self.assertIsNone(launch["server_identity"])
        self.assertEqual(launch["location_identity"], str(self.cwd))
        self.assertEqual(launch["config_identity"], hashlib.sha256(
            launch["config_content"].encode("utf-8")).hexdigest())
        result_config = json.loads(launch["config_content"])
        self.assertEqual(result_config["provider"], config["provider"])
        self.assertEqual(result_config["providers"]["fixture"]["models"]["friendly"]["limit"], {
            "context": 650000, "input": 500000, "output": 18000,
        })
        self.assertEqual(result_config["providers"]["fixture"]["models"]["friendly"]["modelID"],
                         "real-api-id")
        self.assertEqual(result_config["plugin"], config["plugin"])
        self.assertEqual(result_config["permissions"], config["permissions"])
        self.assertEqual(launch["compaction_policy"]["state"], "unknown")
        self.assertEqual(launch["compaction_policy"]["reason"],
                         "native-v2-compaction-behavior-unverified")

    def test_v2_inherit_reads_default_and_allows_only_limit_changes(self):
        before = model_info(context_limit=900000, input_limit=500000,
                            model_id="default", transport_id="default-api-id")
        after = model_info(context_limit=750000, input_limit=500000,
                           model_id="default", transport_id="default-api-id")
        config = {"model": "fixture/default", "providers": {"fixture": {
            "models": {"default": {"limit": {"context": 900000, "input": 500000,
                                                    "output": 32000}}}}}}
        responses = [self.default_response(before), self.default_response(after)]
        with mock.patch.object(context, "_installed_version", return_value="2.0.32"), \
                mock.patch.object(context, "_api_json", side_effect=responses):
            launch = context.prepare_launch_context(
                model="inherit", context_window="750000", config_content=json.dumps(config),
                cwd=str(self.cwd), environ={}, command="opencode-fixture",
            )
        self.assertEqual(launch["model"], "fixture/default")
        self.assertEqual(launch["final_limits"]["context"], 750000)

    def test_v2_requires_exact_enabled_model_and_valid_limits(self):
        cases = [
            (self.model_response(), "missing or ambiguous"),
            (self.model_response(model_info(), model_info()), "missing or ambiguous"),
            (self.model_response(model_info(enabled=False)), "disabled or malformed"),
            (self.model_response(model_info(status="unknown")), "disabled or malformed"),
            (self.model_response(model_info(context_limit=True)), "limit.context"),
            (self.model_response(model_info(input_limit=-1)), "limit.input"),
            (self.model_response(model_info(output_limit=None)), "limit.output"),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(
                    context.OpenCodeContextError, expected):
                context._v2_model_info(payload, cwd=str(self.cwd), model="fixture/catalog-name")
        optional_input = context._v2_model_info(
            self.model_response(model_info(input_limit=...)), cwd=str(self.cwd),
            model="fixture/catalog-name",
        )
        self.assertIsNone(optional_input["limits"]["input"])

    def test_v2_retries_one_initial_snapshot_before_plugin_settlement(self):
        before = model_info(context_limit=900000, input_limit=..., output_limit=8192)
        after = model_info(context_limit=800000, input_limit=..., output_limit=8192)
        config = {"providers": {"fixture": {"models": {"catalog-name": {
            "limit": {"context": 900000, "output": 8192}}}}}}
        responses = [self.model_response(), self.model_response(before),
                     self.model_response(after)]
        with mock.patch.object(context, "_installed_version", return_value="2.0.32"), \
                mock.patch.object(context, "_api_json", side_effect=responses) as api:
            launch = context.prepare_launch_context(
                model="fixture/catalog-name#quality", context_window="800000",
                config_content=json.dumps(config), cwd=str(self.cwd), environ={},
                command="opencode-fixture",
            )
        self.assertEqual(api.call_count, 3)
        self.assertEqual(launch["variant"], "quality")
        self.assertEqual(launch["applied_limits"], {
            "context": 800000, "input": None, "output": 8192,
        })

    def test_v2_rejects_an_observed_unsupported_variant_before_launch(self):
        variants = [{"id": "low", "settings": {}}, {"id": "medium"}, {"id": "high"}]
        row = model_info(model_id="gemini-4-argon", transport_id="argon", variants=variants)
        config = {"providers": {"fixture": {"models": {"gemini-4-argon": {
            "limit": {"context": 900000, "output": 32000}}}}}}
        with mock.patch.object(context, "_installed_version", return_value="2.0.32"), \
                mock.patch.object(context, "_api_json",
                                  side_effect=[self.model_response(row)]) as api:
            with self.assertRaisesRegex(
                    context.OpenCodeContextError,
                    r"Variant unavailable for fixture/gemini-4-argon: xhigh "
                    r"\(지원: low, medium, high\)"):
                context.prepare_launch_context(
                    model="fixture/gemini-4-argon#xhigh", variant="xhigh",
                    context_window="800000", config_content=json.dumps(config),
                    cwd=str(self.cwd), environ={"OPENCODE_RUNTIME_VERSION": "2.0.32"},
                    command="opencode-fixture")
        self.assertEqual(api.call_count, 1)

    def test_v2_observed_supported_variants_launch_and_unobserved_rows_stay_unchanged(self):
        variants = [{"id": "low"}, {"id": "medium"}, {"id": "high"}]
        before = model_info(model_id="gemini-4-argon", transport_id="argon", variants=variants)
        after = model_info(model_id="gemini-4-argon", transport_id="argon",
                           context_limit=800000, variants=variants)
        config = {"providers": {"fixture": {"models": {"gemini-4-argon": {
            "limit": {"context": 900000, "output": 32000}}}}}}
        with mock.patch.object(context, "_installed_version", return_value="2.0.32"), \
                mock.patch.object(context, "_api_json", side_effect=[
                    self.model_response(before), self.model_response(after)]):
            launch = context.prepare_launch_context(
                model="fixture/gemini-4-argon#high", variant="high",
                context_window="800000", config_content=json.dumps(config),
                cwd=str(self.cwd), environ={"OPENCODE_RUNTIME_VERSION": "2.0.32"},
                command="opencode-fixture")
        self.assertEqual(launch["variant"], "high")
        self.assertEqual(launch["final_limits"]["context"], 800000)

        glm_variants = [{"id": "low"}, {"id": "high"}, {"id": "max"}]
        glm_before = model_info(model_id="glm-5.3-flash", transport_id="glm",
                                variants=glm_variants)
        glm_after = model_info(model_id="glm-5.3-flash", transport_id="glm",
                               context_limit=800000, variants=glm_variants)
        glm_config = {"providers": {"fixture": {"models": {"glm-5.3-flash": {
            "limit": {"context": 900000, "output": 32000}}}}}}
        with mock.patch.object(context, "_installed_version", return_value="2.0.32"), \
                mock.patch.object(context, "_api_json", side_effect=[
                    self.model_response(glm_before), self.model_response(glm_after)]):
            launch = context.prepare_launch_context(
                model="fixture/glm-5.3-flash#max", variant="max",
                context_window="800000", config_content=json.dumps(glm_config),
                cwd=str(self.cwd), environ={"OPENCODE_RUNTIME_VERSION": "2.0.32"},
                command="opencode-fixture")
        self.assertEqual(launch["variant"], "max")

        plain_before = model_info(model_id="plain", transport_id="plain")
        plain_after = model_info(model_id="plain", transport_id="plain", context_limit=800000)
        plain_config = {"providers": {"fixture": {"models": {"plain": {
            "limit": {"context": 900000, "output": 32000}}}}}}
        with mock.patch.object(context, "_installed_version", return_value="2.0.32"), \
                mock.patch.object(context, "_api_json", side_effect=[
                    self.model_response(plain_before), self.model_response(plain_after)]):
            launch = context.prepare_launch_context(
                model="fixture/plain#xhigh", variant="xhigh",
                context_window="800000", config_content=json.dumps(plain_config),
                cwd=str(self.cwd), environ={"OPENCODE_RUNTIME_VERSION": "2.0.32"},
                command="opencode-fixture")
        self.assertEqual(launch["variant"], "xhigh")

    def test_v2_rejects_wrong_location(self):
        wrong_location = {"location": {"directory": str(self.cwd.parent)},
                          "data": [model_info()]}
        with self.assertRaisesRegex(context.OpenCodeContextError, "differs from launch directory"):
            context._v2_model_info(wrong_location, cwd=str(self.cwd), model="fixture/catalog-name")

    def test_current_v2_catalog_resolves_without_reading_a_retained_models_dev_cache(self):
        row = model_info(model_id="current-name", transport_id="upstream/current", input_limit=...)
        after = model_info(model_id="current-name", transport_id="upstream/current",
                           context_limit=800000, input_limit=...)
        config = {"providers": {"fixture": {"models": {"current-name": {
            "modelID": "upstream/current", "limit": {"context": 900000, "output": 32000}}}}}}
        with mock.patch.object(context, "_native_limits", side_effect=AssertionError("stale v1 cache read")), \
                mock.patch.object(context, "_api_json", side_effect=[self.model_response(row),
                                                                     self.model_response(after)]):
            launch = context.prepare_launch_context(
                model="fixture/current-name", context_window="800000",
                config_content=json.dumps(config), cwd=str(self.cwd),
                environ={"OPENCODE_RUNTIME_VERSION": "2.0.32"}, command="opencode-fixture")
        self.assertEqual(launch["model"], "fixture/current-name")
        self.assertEqual(launch["final_limits"]["context"], 800000)
        with self.assertRaisesRegex(context.OpenCodeContextError, "missing or ambiguous"):
            context._v2_model_info(self.model_response(row), cwd=str(self.cwd),
                                   model="fixture/retired-name")

    def test_reused_shared_server_ignoring_client_environment_fails_closed(self):
        row = model_info(context_limit=900000)
        unchanged = model_info(context_limit=900000)
        config = {"providers": {"fixture": {"models": {"catalog-name": {
            "limit": {"context": 900000, "output": 32000}}}}}}
        responses = [self.model_response(row), self.model_response(unchanged)]
        with mock.patch.object(context, "_installed_version", return_value="2.0.32"), \
                mock.patch.object(context, "_api_json", side_effect=responses) as api:
            with self.assertRaisesRegex(context.OpenCodeContextError, "exceeds applied cap"):
                context.prepare_launch_context(
                    model="fixture/catalog-name", context_window="800000",
                    config_content=json.dumps(config), cwd=str(self.cwd), environ={},
                    command="opencode-fixture",
                )
        self.assertEqual(api.call_count, 2)
        before_env = api.call_args_list[0].kwargs["env"]
        after_env = api.call_args_list[1].kwargs["env"]
        self.assertNotEqual(before_env["OPENCODE_CONFIG_CONTENT"],
                            after_env["OPENCODE_CONFIG_CONTENT"])

    def test_api_probe_uses_the_supported_standalone_native_target(self):
        with mock.patch.object(context, "_run", return_value='{"location":{},"data":[]}') as run:
            context._api_json("/api/model", cwd=str(self.cwd), env={}, command="opencode-fixture")
        self.assertEqual(run.call_args.args[0], [
            "opencode-fixture", "api", "--standalone", "GET", "/api/model",
        ])

    def test_v2_rejects_a_final_api_limit_above_the_applied_cap(self):
        before = model_info(context_limit=900000, input_limit=600000, output_limit=16000)
        unchanged = model_info(context_limit=900000, input_limit=600000, output_limit=16000)
        config = {"providers": {"fixture": {"models": {"catalog-name": {
            "limit": {"context": 900000, "input": 600000, "output": 16000}}}}}}
        responses = [self.model_response(before), self.model_response(unchanged)]
        with mock.patch.object(context, "_installed_version", return_value="2.0.32"), \
                mock.patch.object(context, "_api_json", side_effect=responses):
            with self.assertRaisesRegex(context.OpenCodeContextError, "exceeds applied cap"):
                context.prepare_launch_context(
                    model="fixture/catalog-name", context_window="800000",
                    config_content=json.dumps(config), cwd=str(self.cwd), environ={},
                    command="opencode-fixture",
                )

    def test_version_probe_uses_shared_parser_for_v_prefixed_output(self):
        completed = subprocess.CompletedProcess(
            ["opencode-fixture", "--version"], 0, "opencode v2.0.32\n", "",
        )
        with mock.patch.object(context.subprocess, "run", return_value=completed):
            self.assertEqual(context._installed_version(
                "opencode-fixture", cwd=str(self.cwd), env={}), "2.0.32")


if __name__ == "__main__":
    unittest.main()
