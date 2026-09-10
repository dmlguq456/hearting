#!/usr/bin/env python3
import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest import mock

from model_profile import load_config, resolve_profile


ROOT = Path(__file__).resolve().parents[1]
SELECTOR = ROOT / "utilities" / "dispatch-owner.py"

import json  # noqa: E402
_OWNER_SPEC = importlib.util.spec_from_file_location("dispatch_owner", SELECTOR)
OWNER = importlib.util.module_from_spec(_OWNER_SPEC)
_OWNER_SPEC.loader.exec_module(OWNER)


# The owner/headless selector proves Claude's session-resume support by
# probing `claude --help`, and refuses (`claude-session-resume-indeterminate`,
# exit 69) rather than guess when the binary is absent. That refusal is
# correct; asserting a successful selection in an environment without the CLI
# is not. CI has no `claude`, so these cases reported a missing runtime as a
# product failure (2026-09-10). Skip instead -- the way the guard suite
# already skips its codex runtime discovery.
CLAUDE_CLI = shutil.which("claude")
NEEDS_CLAUDE_CLI = "no claude binary: the selector's session-resume probe cannot be proven here"

class DispatchOwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.jobs = self.home / "jobs.log"
        self.jobs.touch()
        (self.home / ".gitconfig").write_text(
            f"[safe]\n\tdirectory = {ROOT}\n", encoding="utf-8"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, owners="claude"):
        path = self.home / "dispatch-defaults.yaml"
        path.write_text(
            "schema_version: 1\n"
            f"depth1_owner: [{owners}]\n"
            "opencode:\n  relief_only: true\n"
            "capabilities:\n",
            encoding="utf-8",
        )
        return path

    def balanced_config(self):
        path = self.home / "dispatch-defaults-v2.yaml"
        path.write_text(
            "schema_version: 2\n"
            "depth1_owner: [claude, codex, opencode]\n"
            "opencode:\n  relief_only: false\n"
            "allocation:\n"
            "  strategy: least-recent-attempts\n"
            "  window: 30\n"
            "capabilities:\n",
            encoding="utf-8",
        )
        return path

    def balanced_quality_config(self):
        path = self.home / "dispatch-defaults-balanced-quality.yaml"
        path.write_text(
            "schema_version: 3\n"
            "harnesses:\n  enabled: [claude, codex, opencode]\n"
            "profiles:\n"
            "  deep:\n    primary: [claude, codex]\n    relief: []\n    last_resort: [opencode]\n    promote_relief_below: 0\n"
            "  balanced-deep:\n    primary: [claude, codex]\n    relief: []\n    last_resort: [opencode]\n    promote_relief_below: 0\n"
            "  light:\n    primary: [claude, codex, opencode]\n    relief: []\n    last_resort: []\n    promote_relief_below: 0\n"
            "  mini:\n    primary: [claude, codex, opencode]\n    relief: []\n    last_resort: []\n    promote_relief_below: 0\n"
            "allocation:\n  strategy: balanced\n  window: 30\n  usage_gate_used_percent: 90\n"
            "capabilities:\n",
            encoding="utf-8",
        )
        return path

    def quality_config(self):
        path = self.home / "dispatch-defaults-v3.yaml"
        path.write_text(
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
            "capabilities:\n",
            encoding="utf-8",
        )
        return path

    def run_owner(self, owners="claude", extra=(), config=None, *, model_profile="deep", env_extra=None):
        log_dir = self.home / "logs"
        args = [
            sys.executable, str(SELECTOR), "--dry-run", "--worktree", str(ROOT), "--slug", "owner-test",
            "--capability", "autopilot-code", "--capability-mode", "debug", "--qa", "standard",
            "--intensity", "standard", "--dispatch-depth", "1", "--worker-type", "owner",
            "--assigned-contract", "autopilot-code", "--owner", "autopilot-code",
            "--model-profile", model_profile, "--jobs", str(self.jobs), "--log-dir", str(log_dir),
            *extra,
        ]
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("AGENT_DISPATCH_")
            and not key.startswith("AGENT_OWNER_ROUTE_")
            and not key.startswith("AGENT_ROUTE_")
            and not key.startswith("AGENT_ARTIFACT_")
            # A registered worker inherits its own session markers; the selector
            # under test must see one unambiguous caller harness.
            and key not in ("CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID")
        }
        env.update({
            "AGENT_HOME": str(self.home / "agent-home"),
            "HOME": str(self.home),
            "DISPATCH_DEFAULTS_CONFIG": str(config or self.config(owners)),
            "CODEX_DISPATCH_MODEL": "interactive-inheritance-must-not-leak",
            "CODEX_DISPATCH_MODEL_PROFILE": "interactive-profile-must-not-leak",
            "CODEX_HOME": str(self.home / "codex-home"),
            "CLAUDE_CONFIG_DIR": str(self.home / "claude-home"),
            "HARNESS_CAPACITY_SCORES": "claude:80,codex:80,opencode:80",
            "AGENT_CODEX_MANAGED_GATEWAY": "0",
            "AGENT_CODEX_MANAGED_PARENT_RUNTIME": "",
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": str(ROOT),
        })
        env.update(env_extra or {})
        return subprocess.run(args, text=True, capture_output=True, env=env)

    def base_argv(self, extra=(), jobs=None, log_dir=None):
        return [
            "--dry-run", "--worktree", str(ROOT), "--slug", "owner-test",
            "--capability", "autopilot-code", "--capability-mode", "debug", "--qa", "standard",
            "--intensity", "standard", "--dispatch-depth", "1", "--worker-type", "owner",
            "--assigned-contract", "autopilot-code", "--owner", "autopilot-code",
            "--model-profile", "deep", "--jobs", str(jobs or self.jobs),
            "--log-dir", str(log_dir or (self.home / "logs")),
            *extra,
        ]

    def base_env(self, config=None, owners="claude"):
        return {
            "AGENT_HOME": str(self.home / "agent-home"),
            "HOME": str(self.home),
            "DISPATCH_DEFAULTS_CONFIG": str(config or self.config(owners)),
            "CODEX_DISPATCH_MODEL": "interactive-inheritance-must-not-leak",
            "CODEX_DISPATCH_MODEL_PROFILE": "interactive-profile-must-not-leak",
            "CODEX_HOME": str(self.home / "codex-home"),
            "CLAUDE_CONFIG_DIR": str(self.home / "claude-home"),
            "HARNESS_CAPACITY_SCORES": "claude:80,codex:80,opencode:80",
            "AGENT_CODEX_MANAGED_GATEWAY": "0",
            "AGENT_CODEX_MANAGED_PARENT_RUNTIME": "",
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": str(ROOT),
        }

    def _load_selector_module(self):
        spec = importlib.util.spec_from_file_location(
            f"dispatch_owner_under_test_{id(self)}", SELECTOR
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _snapshot_side_effects(self):
        jobs_hash = hashlib.sha256(self.jobs.read_bytes()).hexdigest() if self.jobs.exists() else None
        watched = [self.home / "agent-home", self.home / "logs"]
        files = set()
        for base in watched:
            if base.exists():
                files.update(str(p.relative_to(self.home)) for p in base.rglob("*") if p.is_file())
        return jobs_hash, files

    def run_owner_in_process(self, argv, env_overrides):
        """Invoke main() in-process with a sentinel that fails loudly if the
        wrapper subprocess (adapters/*/bin/dispatch-headless.py) is ever
        invoked -- a stronger proof than the selector's own printed
        `child_spawned=0` claim."""

        module = self._load_selector_module()
        wrapper_calls = []
        real_run = module.subprocess.run

        def sentinel(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and "dispatch-headless.py" in str(cmd[0]):
                wrapper_calls.append(cmd)
                raise AssertionError(f"wrapper invoked unexpectedly: {cmd}")
            return real_run(cmd, *args, **kwargs)

        module.subprocess.run = sentinel
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, env_overrides, clear=False):
            with contextlib.redirect_stdout(stdout):
                rc = module.main(argv)
        return rc, stdout.getvalue(), wrapper_calls

    def assert_model_map(self, result, adapter):
        conf = ROOT / "adapters" / adapter / "config" / "models.conf"
        expected = resolve_profile(adapter, conf, "deep")
        restricted = load_config(conf).get("CFG_MAIN_SESSION_ONLY_MODELS", "").split()
        if adapter == "claude" and expected["model"] in restricted:
            # Only a config that declares the deep model main-session-only is a
            # typed pre-launch refusal; the shipped default (2026-09-08) declares none.
            self.assertEqual(result.returncode, 64, result.stdout + result.stderr)
            self.assertIn("reason=headless-main-session-only-model", result.stdout)
            self.assertIn("child_spawned=0", result.stdout)
            return
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"adapter={adapter}", result.stdout)
        self.assertIn(f"model={expected['model']}", result.stdout)
        budget_key = "reasoning" if expected["budget_kind"] == "effort" and adapter == "codex" else expected["budget_kind"]
        self.assertIn(f"{budget_key}={expected['budget']}", result.stdout)

    @unittest.skipUnless(CLAUDE_CLI, NEEDS_CLAUDE_CLI)
    def test_configured_claude_selects_claude_wrapper_and_adapter_model_config(self):
        self.assert_model_map(self.run_owner(), "claude")

    def test_configured_codex_selects_codex_wrapper_and_adapter_model_config(self):
        self.assert_model_map(self.run_owner("codex"), "codex")

    @unittest.skipUnless(CLAUDE_CLI, NEEDS_CLAUDE_CLI)
    def test_each_adapter_derives_model_and_budget_from_its_models_conf(self):
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter):
                self.assert_model_map(self.run_owner(adapter), adapter)

    def test_no_cross_harness_model_alias_leakage(self):
        result = self.run_owner("codex")
        self.assert_model_map(result, "codex")
        self.assertNotIn("interactive-inheritance", result.stdout)

    @unittest.skipUnless(CLAUDE_CLI, NEEDS_CLAUDE_CLI)
    def test_schema_v2_balances_repeated_owner_attempts_across_three_harnesses(self):
        config = self.balanced_config()
        selected = []
        for index in range(6):
            result = self.run_owner(config=config, model_profile="balanced-deep")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            match = next(
                line.split("=", 1)[1]
                for line in result.stdout.splitlines()
                if line.startswith("adapter=")
            )
            selected.append(match)
            with self.jobs.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"2026-08-09T00:00:{index:02d}Z\tdone\t/repo\t/wt\towner\t"
                    "attempt_schema_version=2,registered_worker=1,"
                    f"attempt_id=att-balanced-{index:04d},harness={match}\n"
                )
            self.assertIn("allocation_strategy=least-recent-attempts", result.stdout)
            self.assertIn("allocation_window=30", result.stdout)
        self.assertEqual(
            selected,
            ["claude", "codex", "opencode", "claude", "codex", "opencode"],
        )

    @unittest.skipUnless(CLAUDE_CLI, NEEDS_CLAUDE_CLI)
    def test_balanced_recent_count_rotation_is_even_across_three_harnesses(self):
        config = self.balanced_quality_config()
        selected = []
        for index in range(3):
            result = self.run_owner(config=config, model_profile="light")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            selected.append(next(line.split("=", 1)[1] for line in result.stdout.splitlines()
                                 if line.startswith("adapter=")))
            with self.jobs.open("a", encoding="utf-8") as handle:
                handle.write(f"2026-08-09T00:00:{index:02d}Z\tdone\t/repo\t/wt\towner\t"
                             f"attempt_schema_version=2,registered_worker=1,attempt_id=att-r{index},harness={selected[-1]}\n")
        self.assertEqual(selected, ["claude", "codex", "opencode"])
        self.assertIn("allocation_strategy=balanced", result.stdout)

    def test_balanced_owner_prefers_an_ungated_last_resort_over_a_gated_primary(self):
        # B-1: the balanced usage gate is a cross-band partition, so a gated
        # primary must not win over an ungated last_resort.
        result = self.run_owner(
            config=self.balanced_quality_config(),
            model_profile="deep",
            env_extra={"HARNESS_CAPACITY_SCORES": "claude:5,codex:5,opencode:80"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("adapter=opencode", result.stdout)
        self.assertIn("quality_band=last_resort", result.stdout)

    def test_explicit_target_still_beats_the_usage_gate(self):
        result = self.run_owner(
            "claude", ("--adapter", "codex"),
            config=self.balanced_quality_config(),
            model_profile="deep",
            env_extra={"HARNESS_CAPACITY_SCORES": "claude:5,codex:5,opencode:80"},
        )
        self.assert_model_map(result, "codex")
        self.assertIn("quality_band=explicit", result.stdout)

    def test_balanced_owner_uses_global_headroom_when_all_candidates_are_gated(self):
        result = self.run_owner(
            config=self.balanced_quality_config(),
            model_profile="deep",
            env_extra={"HARNESS_CAPACITY_SCORES": "claude:4,codex:1,opencode:9"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("adapter=opencode", result.stdout)
        self.assertIn("quality_band=last_resort", result.stdout)

    @unittest.skipUnless(CLAUDE_CLI, NEEDS_CLAUDE_CLI)
    def test_opencode_is_light_peer_but_deep_last_resort(self):
        config = self.balanced_quality_config()
        light = self.run_owner(config=config, model_profile="light")
        self.assertEqual(light.returncode, 0, light.stdout + light.stderr)
        self.assertIn("adapter=claude", light.stdout)
        # Both judgment profiles use the primary peer band; use the delegated-eligible one.
        deep = self.run_owner(config=config, model_profile="balanced-deep")
        self.assertEqual(deep.returncode, 0, deep.stdout + deep.stderr)
        self.assertIn("quality_band=primary", deep.stdout)
        self.assertNotIn("adapter=opencode", deep.stdout)

    def test_schema_v3_capacity_orders_quality_peers_but_not_opencode(self):
        result = self.run_owner(
            config=self.quality_config(),
            env_extra={"HARNESS_CAPACITY_SCORES": "claude:40,codex:80,opencode:100"},
        )
        self.assert_model_map(result, "codex")
        self.assertIn("quality_band=primary", result.stdout)
        self.assertIn("capacity_headroom.opencode=100.0", result.stdout)

    def test_schema_v3_light_promotes_opencode_only_below_threshold(self):
        result = self.run_owner(
            config=self.quality_config(),
            model_profile="light",
            env_extra={
                "HARNESS_CAPACITY_SCORES": "claude:20,codex:30,opencode:90"
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("adapter=opencode", result.stdout)
        self.assertIn("quality_band=relief", result.stdout)
        self.assertIn("relief_promoted=1", result.stdout)

    def test_schema_v3_light_keeps_primary_when_headroom_is_healthy(self):
        result = self.run_owner(
            config=self.quality_config(),
            model_profile="light",
            env_extra={"HARNESS_CAPACITY_SCORES": "claude:20,codex:80"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("adapter=codex", result.stdout)
        self.assertIn("quality_band=primary", result.stdout)
        self.assertIn("relief_promoted=0", result.stdout)

    def test_caller_runtime_is_distinct_from_selected_owner_adapter(self):
        module = self._load_selector_module()
        self.assertEqual(
            module._caller_harness({"CODEX_THREAD_ID": "thread-codex"}),
            "codex",
        )
        self.assertEqual(
            module._caller_harness(
                {
                    "AGENT_DISPATCH_CALLER_HARNESS": "claude",
                    "CODEX_THREAD_ID": "stale-codex-value",
                }
            ),
            "claude",
        )

    def test_ambiguous_caller_runtime_fails_closed(self):
        module = self._load_selector_module()
        with self.assertRaises(module.OwnerError) as raised:
            module._caller_harness(
                {
                    "CODEX_THREAD_ID": "thread-codex",
                    "CLAUDE_CODE_SESSION_ID": "session-claude",
                }
            )
        self.assertEqual(str(raised.exception), "caller-harness-ambiguous")

    def test_explicit_adapter_beats_config(self):
        result = self.run_owner("claude", ("--adapter", "codex"))
        self.assert_model_map(result, "codex")
        self.assertIn("selection_source=explicit", result.stdout)

    def test_depth_one_affinity_and_new_fields_are_auditable(self):
        config = self.balanced_quality_config()
        text = config.read_text(encoding="utf-8").replace(
            "usage_gate_used_percent: 90", "usage_gate_used_percent: 90\n  depth_affinity:\n    owner: claude\n    worker: codex\n  depth_affinity_weight: 0.65\n  usage_headroom_exponent: 2")
        config.write_text(text, encoding="utf-8")
        result = self.run_owner(config=config)
        self.assertIn("depth_affinity=owner:claude,worker:codex", result.stdout)
        self.assertIn("depth_affinity_weight=0.65", result.stdout)
        self.assertIn("usage_headroom_exponent=2", result.stdout)

    def test_explicit_adapter_beats_depth_affinity(self):
        config = self.balanced_quality_config()
        text = config.read_text(encoding="utf-8").replace(
            "usage_gate_used_percent: 90", "usage_gate_used_percent: 90\n  depth_affinity:\n    owner: claude\n    worker: codex\n  depth_affinity_weight: 0.65\n  usage_headroom_exponent: 2")
        config.write_text(text, encoding="utf-8")
        result = self.run_owner(config=config, extra=("--adapter", "codex"))
        self.assertIn("adapter=codex", result.stdout)
        self.assertIn("selection_source=explicit", result.stdout)

    def test_explicit_opencode_relief_path_is_authorized(self):
        # SD-66 relief-only: opencode is never a configured/default candidate,
        # but an explicit --adapter opencode is a documented relief path and
        # must clear the authorization gate (OPERATIONS §5.10 quick/relief).
        result = self.run_owner("claude", ("--adapter", "opencode"))
        self.assert_model_map(result, "opencode")
        self.assertIn("selection_source=explicit", result.stdout)
        self.assertIn("eligibility.opencode=", result.stdout)

    def test_opencode_never_selected_without_explicit_adapter(self):
        # Relief-only also means: with every configured candidate limited,
        # the unsealed last resort still never lands on opencode by itself.
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.jobs.write_text(
            "\n".join(
                f"{stamp}\tdone\trepo\t{ROOT}\tx\tnote=dead-session-limit,harness={h}"
                for h in ("claude", "codex")
            ) + "\n", encoding="utf-8",
        )
        result = self.run_owner()
        self.assertNotIn("adapter=opencode", result.stdout)

    def test_limited_configured_candidate_demotes_with_auditable_reason(self):
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.jobs.write_text(f"{stamp}\tdone\trepo\t{ROOT}\tx\tnote=dead-session-limit,harness=claude\n", encoding="utf-8")
        result = self.run_owner()
        self.assert_model_map(result, "codex")
        self.assertIn("selection_source=eligibility-fallback", result.stdout)
        self.assertIn("fallback.1=codex:configured-candidates-ineligible", result.stdout)
        self.assertIn("rejected.1=claude:usage-limited", result.stdout)

    def test_unknown_capacity_never_selects_an_automatic_recovery(self):
        self.jobs.unlink()
        result = self.run_owner(env_extra={"HARNESS_CAPACITY_SCORES": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("eligibility.claude=unknown", result.stdout)
        self.assertIn("capacity_headroom.claude=unknown", result.stdout)
        self.assertIn("reason=no-eligible-candidate", result.stdout)
        self.assertIn("child_spawned=0", result.stdout)

    @unittest.skipUnless(CLAUDE_CLI, NEEDS_CLAUDE_CLI)
    def test_explicit_adapter_can_override_unknown_capacity(self):
        self.jobs.unlink()
        result = self.run_owner(
            extra=("--adapter", "claude"),
            env_extra={"HARNESS_CAPACITY_SCORES": ""},
        )
        self.assert_model_map(result, "claude")
        self.assertIn("eligibility.claude=unknown", result.stdout)
        self.assertIn("selection_source=explicit", result.stdout)

    def test_route_user_disabled_harness_rejects_explicit_override_before_wrapper(self):
        route = self.home / "user-disabled-route.json"
        route.write_text(
            json.dumps({
                "effective_intensity": "standard",
                "dispatch_evidence": {"tuples": [
                    {
                        "parent_harness": "claude",
                        "status": "unsupported",
                        "failure_scope": "runtime-global",
                        "failure_class": "user-disabled",
                    },
                    {"parent_harness": "codex", "status": "supported"},
                ]},
            }),
            encoding="utf-8",
        )
        rc, stdout, calls = self.run_owner_in_process(
            self.base_argv(
                extra=("--route-evidence", str(route), "--adapter", "claude")
            ),
            self.base_env(config=self.quality_config()),
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("reason=explicit-adapter-outside-route-evidence", stdout)
        self.assertIn("child_spawned=0", stdout)
        self.assertEqual(calls, [])

    def test_malformed_yaml_fails_before_materialization(self):
        config = self.home / "bad.yaml"
        config.write_text("depth1_owner: [claude\n", encoding="utf-8")
        before = self._snapshot_side_effects()
        rc, stdout, calls = self.run_owner_in_process(
            self.base_argv(), self.base_env(config=config)
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("child_spawned=0", stdout)
        self.assertNotIn("check=ok", stdout)
        self.assertEqual(calls, [])
        self.assertEqual(self._snapshot_side_effects(), before)

    def test_empty_owner_list_fails_before_materialization(self):
        before = self._snapshot_side_effects()
        rc, stdout, calls = self.run_owner_in_process(
            self.base_argv(), self.base_env(config=self.config(""))
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("child_spawned=0", stdout)
        self.assertNotIn("check=ok", stdout)
        self.assertEqual(calls, [])
        self.assertEqual(self._snapshot_side_effects(), before)

    def test_unknown_owner_harness_fails_before_materialization(self):
        before = self._snapshot_side_effects()
        rc, stdout, calls = self.run_owner_in_process(
            self.base_argv(), self.base_env(config=self.config("opencode"))
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("child_spawned=0", stdout)
        self.assertNotIn("check=ok", stdout)
        self.assertEqual(calls, [])
        self.assertEqual(self._snapshot_side_effects(), before)

    def test_forbidden_concrete_or_runtime_policy_selection_fails_before_materialization(self):
        for option in (
            ("--model", "not-a-portable-profile"),
            ("--inherit-model-settings",),
            ("--completion-delivery", "poll"),
            ("--allow-unmanaged-parent-poll",),
        ):
            with self.subTest(option=option):
                before = self._snapshot_side_effects()
                rc, stdout, calls = self.run_owner_in_process(
                    self.base_argv(extra=option), self.base_env()
                )
                self.assertNotEqual(rc, 0)
                self.assertIn("forbidden-flag", stdout)
                self.assertNotIn("check=ok", stdout)
                self.assertEqual(calls, [])
                self.assertEqual(self._snapshot_side_effects(), before)

    def test_managed_parent_rejects_explicit_split_registry_before_usage_or_wrapper(self):
        canonical = self.home / "canonical" / "jobs.log"
        canonical.parent.mkdir()
        canonical.touch()
        before = self._snapshot_side_effects()
        rc, stdout, calls = self.run_owner_in_process(
            self.base_argv(),
            {
                **self.base_env(),
                "AGENT_CODEX_MANAGED_GATEWAY": "1",
                "AGENT_CODEX_MANAGED_PARENT_RUNTIME": "codex",
                "AGENT_DISPATCH_JOBS": str(canonical),
            },
        )
        self.assertEqual(rc, 65)
        self.assertIn("reason=managed-parent-registry-immutable", stdout)
        self.assertIn("child_spawned=0", stdout)
        self.assertEqual(calls, [])
        self.assertEqual(self._snapshot_side_effects(), before)

    def test_managed_parent_accepts_realpath_alias_of_canonical_registry(self):
        canonical = self.home / "canonical" / "jobs.log"
        canonical.parent.mkdir()
        canonical.touch()
        alias = self.home / "jobs-alias.log"
        alias.symlink_to(canonical)
        selected = OWNER._authoritative_jobs(
            {"--jobs": str(alias)},
            {
                "AGENT_CODEX_MANAGED_GATEWAY": "1",
                "AGENT_CODEX_MANAGED_PARENT_RUNTIME": "codex",
                "AGENT_DISPATCH_JOBS": str(canonical),
            },
        )
        self.assertEqual(selected, str(canonical))

    def test_interactive_claude_parent_may_not_start_into_another_registry(self):
        # rewake review R3 M1: the parent's asyncRewake hook trusts one registry;
        # an explicit --jobs elsewhere is refused before spawn, typed and hinted.
        canonical = self.home / "canonical" / "jobs.log"
        canonical.parent.mkdir(exist_ok=True)
        canonical.touch()
        other = self.home / "elsewhere.log"
        other.touch()
        claude = {"AGENT_DISPATCH_CALLER_HARNESS": "claude", "AGENT_DISPATCH_JOBS": str(canonical)}
        self.assertEqual(OWNER._authoritative_jobs({"--jobs": str(canonical)}, claude), str(canonical))
        self.assertEqual(OWNER._authoritative_jobs({}, claude), str(canonical))
        with self.assertRaises(OWNER.OwnerError) as refused:
            OWNER._authoritative_jobs({"--jobs": str(other)}, claude)
        self.assertEqual(str(refused.exception), "explicit-jobs-outside-parent-registry")
        self.assertIn("AGENT_DISPATCH_JOBS", OWNER.hint_for("explicit-jobs-outside-parent-registry"))
        # no inherited registry: only the installed canonical one is accepted
        with mock.patch.object(OWNER, "_canonical_jobs", return_value=str(canonical)):
            self.assertEqual(OWNER._authoritative_jobs({"--jobs": str(canonical)},
                                                       {"AGENT_DISPATCH_CALLER_HARNESS": "claude"}), str(canonical))
            with self.assertRaises(OWNER.OwnerError):
                OWNER._authoritative_jobs({"--jobs": str(other)}, {"AGENT_DISPATCH_CALLER_HARNESS": "claude"})
        # an unmanaged codex caller keeps the previous behaviour
        self.assertEqual(OWNER._authoritative_jobs({"--jobs": str(other)},
                                                   {"AGENT_DISPATCH_CALLER_HARNESS": "codex"}), str(other))

    def test_an_unusable_inherited_registry_refuses_the_claude_launch_before_spawn(self):
        # rewake review R4 M1: the hook trusts nothing when AGENT_DISPATCH_JOBS is
        # set but unusable (symlink, empty, absent, not regular), so the selector
        # refuses the same states -- whether or not an explicit --jobs names the
        # symlink's real file.
        canonical = self.home / "canonical" / "jobs.log"
        canonical.parent.mkdir(exist_ok=True)
        canonical.touch()
        alias = self.home / "alias.log"
        alias.symlink_to(canonical)
        for label, inherited, explicit in (
            ("symlink + realpath explicit", str(alias), str(canonical)),
            ("symlink, no explicit", str(alias), ""),
            ("empty, no explicit", "", ""),
            ("absent file", str(self.home / "absent.log"), ""),
            ("directory", str(self.home), str(self.home)),
        ):
            values = {"--jobs": explicit} if explicit else {}
            env = {"AGENT_DISPATCH_CALLER_HARNESS": "claude", "AGENT_DISPATCH_JOBS": inherited}
            with self.subTest(label=label), self.assertRaises(OWNER.OwnerError) as refused:
                OWNER._authoritative_jobs(values, env)
            self.assertEqual(str(refused.exception), "inherited-registry-unusable")
        self.assertIn("AGENT_DISPATCH_JOBS", OWNER.hint_for("inherited-registry-unusable"))
        self.assertIn("SESSION", OWNER.hint_for("inherited-registry-unusable"))  # R5 m1: a per-command change cannot help
        # top review M2: with no inherited variable, a canonical path that exists
        # as a symlink is refused too; an absent canonical file (first run) is not
        link_canonical = self.home / "canonical-link.log"
        link_canonical.symlink_to(canonical)
        with mock.patch.object(OWNER, "_canonical_jobs", return_value=str(link_canonical)):
            with self.assertRaises(OWNER.OwnerError) as refused:
                OWNER._authoritative_jobs({}, {"AGENT_DISPATCH_CALLER_HARNESS": "claude"})
            self.assertEqual(str(refused.exception), "canonical-registry-unusable")
        with mock.patch.object(OWNER, "_canonical_jobs", return_value=str(self.home / "not-yet" / "jobs.log")):
            self.assertEqual(OWNER._authoritative_jobs({}, {"AGENT_DISPATCH_CALLER_HARNESS": "claude"}), "")
        self.assertIn("symlink", OWNER.hint_for("canonical-registry-unusable"))
        # the same symlink is fine for an unmanaged codex caller, and a managed codex
        # parent still accepts its realpath alias
        self.assertEqual(OWNER._authoritative_jobs({}, {"AGENT_DISPATCH_CALLER_HARNESS": "codex",
                                                        "AGENT_DISPATCH_JOBS": str(alias)}), str(alias))

    def test_no_eligible_candidate_fails_without_wrapper_or_process(self):
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.jobs.write_text(
            "\n".join(
                f"{stamp}\tdone\trepo\t{ROOT}\tx\tnote=dead-session-limit,harness={h}"
                for h in ("claude", "codex")
            ) + "\n", encoding="utf-8"
        )
        before = self._snapshot_side_effects()
        rc, stdout, calls = self.run_owner_in_process(self.base_argv(), self.base_env())
        self.assertNotEqual(rc, 0)
        self.assertIn("reason=no-eligible-candidate", stdout)
        self.assertIn("child_spawned=0", stdout)
        self.assertNotIn("check=ok", stdout)
        self.assertEqual(calls, [])
        self.assertEqual(self._snapshot_side_effects(), before)


class RouteEvidenceOwnerHarnessTest(unittest.TestCase):
    """--route-evidence binds the adapter cascade to the probed harnesses.

    Without it, a usage-limited configured owner falls through to another
    harness and every dispatch-depth-2 launch then fails
    `dispatch-evidence-parent-runtime-mismatch` -- the 2026-08-04 incident with
    the harness field substituted for the transport field.
    """

    def _route(self, payload):
        path = Path(tempfile.mkdtemp()) / "route.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_standard_route_reports_its_sealed_parent_harnesses(self):
        path = self._route({"effective_intensity": "standard", "dispatch_evidence": {"tuples": [
            {"parent_harness": "claude", "status": "supported"},
            {"parent_harness": "codex", "status": "unsupported"},
        ]}})
        self.assertEqual(OWNER._sealed_owner_harnesses(path), {"claude"})

    def test_standard_route_exposes_sealed_owner_policy(self):
        policy = {"primary": ["claude", "codex"], "relief": ["opencode"],
                  "last_resort": [], "promote_relief_below": 35}
        path = self._route({"effective_intensity": "standard",
                            "owner_harness_policy": policy,
                            "dispatch_allocation": {"strategy": "capacity-aware", "window": 30,
                                                    "harness_order": ["claude", "codex", "opencode"]},
                            "dispatch_evidence": {"tuples": [
                                {"parent_harness": "claude", "status": "supported"},
                                {"parent_harness": "codex", "status": "supported"},
                            ]}})
        context = OWNER._sealed_owner_context(path)
        self.assertEqual(context["policy"], policy)
        self.assertEqual(context["allocation"]["strategy"], "capacity-aware")

    def test_worktree_local_unsupported_never_selects_an_owner_fallback(self):
        path = self._route({"effective_intensity": "standard", "dispatch_evidence": {"tuples": [
            {"parent_harness": "codex", "status": "unsupported",
             "failure_scope": "exact-worktree", "retry_on_isolated_worktree": 1},
            {"parent_harness": "claude", "status": "supported"},
        ]}})
        with self.assertRaises(OWNER.OwnerError) as caught:
            OWNER._sealed_owner_harnesses(path)
        self.assertEqual(
            str(caught.exception),
            "route-evidence-exact-worktree-reprobe-required",
        )

    def test_user_disabled_harness_is_not_an_automatic_or_explicit_fallback(self):
        path = self._route({
            "effective_intensity": "standard",
            "dispatch_evidence": {"tuples": [
                {
                    "parent_harness": "claude",
                    "status": "unsupported",
                    "failure_scope": "runtime-global",
                    "failure_class": "user-disabled",
                },
                {"parent_harness": "codex", "status": "supported"},
            ]},
        })
        self.assertEqual(OWNER._sealed_owner_harnesses(path), {"codex"})

    def test_quick_route_uses_its_registered_headless_candidates(self):
        # quick seals no depth-2 tuples; reading `dispatch_evidence` here would
        # report "no supported owner harness" for a perfectly valid route.
        path = self._route({"effective_intensity": "quick", "dispatch_evidence": None,
                            "registered_headless_candidates": [
                                {"harness": "codex", "status": "supported"},
                                {"harness": "claude", "status": "unsupported"}]})
        self.assertEqual(OWNER._sealed_owner_harnesses(path), {"codex"})

    def test_direct_route_has_no_owner_to_bind(self):
        path = self._route({"effective_intensity": "direct", "dispatch_evidence": None})
        with self.assertRaises(OWNER.OwnerError) as caught:
            OWNER._sealed_owner_harnesses(path)
        self.assertEqual(str(caught.exception), "route-evidence-direct-route-has-no-owner")

    def test_owner_route_env_is_exported_for_every_intensity(self):
        """quick used to skip these, leaving its owner to guess the route file path."""
        binding = SimpleNamespace(
            route_file="/w/.agent_reports/.runtime/routes/rt-abc123.json",
            route_id="rt-abc123", route_hash="sha256:abc123",
        )
        env = OWNER.export_owner_route_env({}, binding)
        self.assertEqual(env["AGENT_OWNER_ROUTE_FILE"], binding.route_file)
        self.assertEqual(env["AGENT_OWNER_ROUTE_ID"], binding.route_id)
        self.assertEqual(env["AGENT_OWNER_ROUTE_HASH"], binding.route_hash)

    def test_quick_forwards_the_route_but_must_not_also_export_the_env(self):
        """Both signals at once is what the adapters refuse.

        The adapters read "env binding present" as "this is a standard+ owner"
        and raise `owner-route-binding-tuple-invalid` if a route file argument
        comes with it, so quick must pick exactly one channel. v2.109.2 set
        both and every quick owner died at launch.
        """
        source = Path(OWNER.__file__).read_text(encoding="utf-8")
        body = source.split("if route_data.get(\"effective_intensity\") == \"quick\":", 1)[1]
        quick, standard = body.split("else:", 1)
        self.assertIn('"--route-file", binding.route_file', quick)
        code = "\n".join(
            line for line in quick.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn("export_owner_route_env(", code)
        self.assertIn("export_owner_route_env(child_env, binding)", standard)

    def test_selector_only_option_never_reaches_the_wrapper(self):
        _, _, forwarded, evidence, _ = OWNER._parse([
            "--route-evidence", "/tmp/r.json", "--worktree", "/w", "--slug", "s",
            "--capability", "autopilot-code", "--capability-mode", "dev", "--qa", "standard",
            "--intensity", "standard", "--dispatch-depth", "1", "--worker-type", "owner",
            "--assigned-contract", "autopilot-code", "--owner", "autopilot-code",
            "--model-profile", "deep", "--dry-run"])
        self.assertEqual(evidence, "/tmp/r.json")
        self.assertNotIn("--route-evidence", forwarded)
        self.assertNotIn("/tmp/r.json", forwarded)



class RegisteredReviewerLaunchTest(unittest.TestCase):
    """SD-OPEN-40: a depth-1 independent reviewer may be a review worker.

    Before this, `dispatch-owner.py` refused every tuple but
    `--dispatch-depth 1 --worker-type owner`, and stage dispatch only launches a
    review worker that is bound to a route node. An ad-hoc independent review --
    the kind a session runs on its own branch -- therefore had exactly one
    reachable shape: `worker_type=owner`. That is the self-declaration
    SD-OPEN-41(b)'s marker gate has to downgrade, so the degraded path was the
    only path, and "degraded" would have described normal practice rather than
    an exception.
    """

    _BASE = [
        "--worktree", "/w", "--slug", "s", "--capability", "autopilot-code",
        "--capability-mode", "dev", "--qa", "standard", "--intensity", "standard",
        "--dispatch-depth", "1", "--assigned-contract", "autopilot-code",
        "--owner", "autopilot-code", "--model-profile", "deep", "--dry-run",
    ]

    def _parse(self, *extra):
        return OWNER._parse([*self._BASE, *extra])

    def test_a_review_tuple_launches_when_it_names_a_catalog_unit(self):
        _, values, forwarded, _, _ = self._parse(
            "--worker-type", "review", "--unit", "qa/code-review")
        self.assertEqual(values["--worker-type"], "review")
        # The unit has to reach the wrapper: it is what selects the persona the
        # reviewer reads, and it is the field the completion gate later checks.
        self.assertIn("--unit", forwarded)
        self.assertIn("qa/code-review", forwarded)

    def test_a_frame_launch_needs_all_four_artifact_scope_variables(self):
        # OPERATIONS §5.10b used to ask depth-0, in prose, to export all four
        # before every frame launch. The launch checks it now: any subset is
        # refused at the caller, naming exactly what is missing.
        four = {name: "/fixture/" + name.lower() for name in OWNER._FRAME_ARTIFACT_ENV}
        for dropped in OWNER._FRAME_ARTIFACT_ENV:
            with self.subTest(dropped=dropped), mock.patch.dict(os.environ, four):
                del os.environ[dropped]
                with self.assertRaises(OWNER.OwnerError) as caught:
                    self._parse("--worker-type", "frame", "--unit", "plan/frame")
                self.assertEqual(str(caught.exception), "frame-artifact-scope-missing:" + dropped)
        with mock.patch.dict(os.environ, four):
            _, values, _, _, _ = self._parse("--worker-type", "frame", "--unit", "plan/frame")
        self.assertEqual(values["--worker-type"], "frame")
        # owner and review launches are untouched by the frame-only check
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in OWNER._FRAME_ARTIFACT_ENV:
                os.environ.pop(name, None)
            self._parse("--worker-type", "review", "--unit", "qa/code-review")

    def test_a_review_tuple_without_a_unit_is_refused(self):
        # `worker_type=review` with no unit reaches the mode contract as
        # `missing-dispatch-worker-mode` after the process is already
        # committed; refusing here keeps the failure at the caller.
        with self.assertRaises(OWNER.OwnerError) as caught:
            self._parse("--worker-type", "review")
        self.assertIn("review-worker-unit-required", str(caught.exception))

    def test_a_review_worker_may_not_borrow_the_owner_unit(self):
        for unit, expected in (
            ("_kernel/owner", "review-worker-unit-reserved"),
            ("_kernel/resource", "review-worker-unit-reserved"),
            ("Not A Unit", "invalid-review-worker-unit"),
        ):
            with self.subTest(unit):
                with self.assertRaises(OWNER.OwnerError) as caught:
                    self._parse("--worker-type", "review", "--unit", unit)
                self.assertIn(expected, str(caught.exception))

    def test_a_route_bound_review_node_is_not_launched_from_here(self):
        # One node, one launch path. Stage dispatch owns a review node with its
        # binding; accepting route evidence here would let two paths claim it.
        with self.assertRaises(OWNER.OwnerError) as caught:
            self._parse("--worker-type", "review", "--unit", "qa/code-review",
                        "--route-evidence", "/tmp/r.json")
        self.assertIn("review-worker-route-evidence-unsupported", str(caught.exception))

    def test_every_other_worker_type_is_still_refused(self):
        for worker_type in ("stage", "support", "conductor", ""):
            with self.subTest(worker_type):
                with self.assertRaises(OWNER.OwnerError) as caught:
                    self._parse("--worker-type", worker_type or "-",
                                "--unit", "qa/code-review")
                self.assertIn("owner-tuple-required", str(caught.exception))

    def test_the_owner_tuple_is_unchanged(self):
        _, values, _, _, _ = self._parse("--worker-type", "owner")
        self.assertEqual(values["--worker-type"], "owner")
        with self.assertRaises(OWNER.OwnerError) as caught:
            self._parse("--worker-type", "owner", "--unit", "qa/code-review")
        self.assertIn("invalid-owner-unit", str(caught.exception))
        # depth is still pinned for both types
        for worker_type, unit in (("owner", None), ("review", "qa/code-review")):
            with self.subTest(worker_type):
                extra = ["--worker-type", worker_type]
                if unit:
                    extra += ["--unit", unit]
                argv = [a for a in self._BASE]
                argv[argv.index("--dispatch-depth") + 1] = "2"
                with self.assertRaises(OWNER.OwnerError) as caught:
                    OWNER._parse([*argv, *extra])
                self.assertIn("owner-tuple-required", str(caught.exception))

    def test_a_unit_must_be_in_the_catalog_not_merely_shaped_like_one(self):
        # Round 1 (10b): the comment says "catalog persona" while the regex
        # accepted any lowercase pair, so `foo/bar` passed.
        with self.assertRaises(OWNER.OwnerError) as caught:
            self._parse("--worker-type", "review", "--unit", "foo/bar")
        self.assertIn("unknown-review-worker-unit", str(caught.exception))
        # and a real one still passes
        _, values, _, _, _ = self._parse(
            "--worker-type", "review", "--unit", "qa/code-review")
        self.assertEqual(values["--unit"], "qa/code-review")

    def test_an_equal_form_flag_reaches_the_wrapper_exactly_once(self):
        # Round 1 (10): the equal-form branch appended and then fell through to
        # the unconditional append, so every `--flag=value` was forwarded twice.
        # Pre-existing for `_REQUIRED`, and this branch widened it to `--unit`.
        # Harmless while all three wrappers parse these as plain `store`, and one
        # `action="append"` away from not being. Nothing pinned `forwarded`.
        argv = [
            "--worktree=/w", "--slug=s", "--capability=autopilot-code",
            "--capability-mode=dev", "--qa=standard", "--intensity=standard",
            "--dispatch-depth=1", "--worker-type=review",
            "--unit=qa/code-review", "--assigned-contract=autopilot-code",
            "--owner=autopilot-code", "--model-profile=deep", "--dry-run",
        ]
        _, _, forwarded, _, _ = OWNER._parse(argv)
        flags = [a.split("=", 1)[0] for a in forwarded if a.startswith("--")]
        duplicates = sorted({f for f in flags if flags.count(f) > 1})
        self.assertEqual(duplicates, [], f"forwarded twice: {duplicates}")
        for arg in argv:
            self.assertIn(arg, forwarded)

    def test_the_split_form_is_unchanged_by_that_fix(self):
        # The split form already `continue`d; the fix must not disturb it.
        # Count FLAGS, not tokens: distinct split-form flags legitimately share
        # a value (`--capability` and `--assigned-contract` are both
        # `autopilot-code`, `--qa` and `--intensity` both `standard`), so a
        # token-level uniqueness assertion fails on correct output.
        _, values, forwarded, _, _ = self._parse(
            "--worker-type", "review", "--unit", "qa/code-review")
        self.assertEqual(values["--worker-type"], "review")
        flags = [a.split("=", 1)[0] for a in forwarded if a.startswith("--")]
        duplicates = sorted({f for f in flags if flags.count(f) > 1})
        self.assertEqual(duplicates, [], f"forwarded twice: {duplicates}")



class RouteDerivedOwnerTupleTest(unittest.TestCase):
    """--route-evidence fills the owner tuple the route already states.

    Measured 2026-09-09 (surface-reduction cycle, priority 4): launching one
    review owner took a 14-flag command whose values were all readable from
    the sealed route, and the selector refused 32 times in one session for
    argument mistakes it could have resolved itself. A full explicit tuple is
    still parsed as before; only a gap opens the route.
    """

    def _route(self, **override):
        payload = {
            "effective_intensity": "quick", "slug": "review-r1", "capability": "autopilot-code",
            "capability_mode": "audit", "cwd": "/w/tree", "owner_model_profile": "balanced-deep",
            "registered_headless_candidates": [{"harness": "codex", "status": "supported"}],
        }
        payload.update(override)
        path = Path(tempfile.mkdtemp()) / "route.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_prompt_only_launch_derives_the_whole_owner_tuple(self):
        path = self._route()
        _, values, forwarded, evidence, derived = OWNER._parse(
            ["--start", "--route-evidence", path, "--prompt-file", "/p.md"])
        self.assertEqual(evidence, path)
        self.assertEqual(values["--worktree"], "/w/tree")
        self.assertEqual(values["--slug"], "review-r1")
        self.assertEqual(values["--capability"], "autopilot-code")
        self.assertEqual(values["--capability-mode"], "audit")
        self.assertEqual(values["--intensity"], "quick")
        self.assertEqual(values["--model-profile"], "balanced-deep")
        self.assertEqual(values["--dispatch-depth"], "1")
        self.assertEqual(values["--worker-type"], "owner")
        self.assertEqual(values["--owner"], "autopilot-code")
        self.assertEqual(values["--assigned-contract"], "autopilot-code")
        self.assertEqual(sorted(derived), sorted(list(OWNER._ROUTE_FIELDS) + ["--assigned-contract", "--dispatch-depth", "--owner", "--worker-type"]))
        # every derived value reaches the wrapper as a real flag pair
        for flag in derived:
            self.assertIn(flag, forwarded)
            self.assertEqual(forwarded[forwarded.index(flag) + 1], values[flag])
        self.assertNotIn("--route-evidence", forwarded)
        self.assertNotIn("--qa", values)  # the wrapper derives it from --intensity

    def test_explicit_flags_win_and_are_not_re_forwarded(self):
        path = self._route()
        _, values, forwarded, _, derived = OWNER._parse(
            ["--start", "--route-evidence", path, "--slug", "my-name", "--model-profile", "deep", "--prompt-file", "/p.md"])
        self.assertEqual(values["--slug"], "my-name")
        self.assertEqual(values["--model-profile"], "deep")
        self.assertNotIn("--slug", derived)
        self.assertNotIn("--model-profile", derived)
        self.assertEqual(forwarded.count("--slug"), 1)
        self.assertEqual(forwarded.count("--model-profile"), 1)

    def test_a_sealed_field_that_contradicts_the_route_is_refused_typed(self):
        path = self._route()
        for flag, wrong in (("--capability", "autopilot-research"), ("--capability-mode", "dev"),
                            ("--intensity", "standard"), ("--worktree", "/elsewhere")):
            with self.subTest(flag=flag):
                with self.assertRaises(OWNER.OwnerError) as caught:
                    OWNER._parse(["--start", "--route-evidence", path, flag, wrong, "--prompt-file", "/p.md"])
                self.assertEqual(str(caught.exception), f"route-evidence-arg-mismatch:{flag}")

    def test_the_same_worktree_spelled_differently_is_not_a_mismatch(self):
        path = self._route()
        _, values, _, _, _ = OWNER._parse(
            ["--start", "--route-evidence", path, "--worktree", "/w/./tree/", "--prompt-file", "/p.md"])
        self.assertEqual(values["--worktree"], "/w/./tree/")

    def test_a_full_explicit_tuple_never_opens_the_route_file(self):
        # Compatibility: the pure-parse callers pass a path that does not exist.
        base = RegisteredReviewerLaunchTest._BASE
        _, values, _, evidence, derived = OWNER._parse(
            [*base, "--worker-type", "owner", "--route-evidence", "/nonexistent/route.json"])
        self.assertEqual(evidence, "/nonexistent/route.json")
        self.assertEqual(derived, [])
        self.assertEqual(values["--qa"], "standard")

    def test_a_direct_route_cannot_launch_an_owner(self):
        path = self._route(effective_intensity="direct")
        with self.assertRaises(OWNER.OwnerError) as caught:
            OWNER._parse(["--start", "--route-evidence", path, "--prompt-file", "/p.md"])
        self.assertEqual(str(caught.exception), "route-evidence-direct-route-has-no-owner")

    def test_a_route_lacking_a_field_still_reports_exactly_that_gap(self):
        path = self._route(owner_model_profile=None)
        with self.assertRaises(OWNER.OwnerError) as caught:
            OWNER._parse(["--start", "--route-evidence", path, "--prompt-file", "/p.md"])
        self.assertEqual(str(caught.exception), "missing-required:--model-profile")

    def test_an_unreadable_route_is_typed_not_a_traceback(self):
        with self.assertRaises(OWNER.OwnerError) as caught:
            OWNER._parse(["--start", "--route-evidence", "/nonexistent/route.json", "--prompt-file", "/p.md"])
        self.assertTrue(str(caught.exception).startswith("route-evidence-unreadable:"))

    def test_a_review_worker_does_not_borrow_the_owner_tuple_from_a_route(self):
        path = self._route()
        with self.assertRaises(OWNER.OwnerError) as caught:
            OWNER._parse(["--start", "--route-evidence", path, "--worker-type", "review",
                          "--unit", "qa/code-review", "--prompt-file", "/p.md"])
        # nothing was derived: the very first refusal is the plain missing list
        self.assertTrue(str(caught.exception).startswith("missing-required:"))

    def test_without_route_evidence_the_full_tuple_is_still_required(self):
        with self.assertRaises(OWNER.OwnerError) as caught:
            OWNER._parse(["--start", "--prompt-file", "/p.md"])
        self.assertTrue(str(caught.exception).startswith("missing-required:"))
        self.assertIn("--qa", str(caught.exception))


class RefusalHintTest(unittest.TestCase):
    """Every typed refusal that has a known next step prints it as `hint=`."""

    def _run(self, argv):
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_DISPATCH_")}
        proc = subprocess.run([sys.executable, str(SELECTOR), *argv], text=True,
                              capture_output=True, env=env, check=False)
        return proc.returncode, proc.stdout

    def test_missing_required_prints_the_short_and_long_forms(self):
        rc, out = self._run(["--start", "--prompt-file", "/p.md"])
        self.assertEqual(rc, 65)
        self.assertIn("reason=missing-required:", out)
        self.assertRegex(out, r"(?m)^hint=with --route-evidence <route\.json> pass only --prompt-file")
        self.assertIn("child_spawned=0", out)
        # the hint follows the receipt so existing consumers see the same prefix
        self.assertLess(out.index("child_spawned=0"), out.index("hint="))

    def test_direct_route_hint_names_the_shapes_that_have_an_owner(self):
        path = Path(tempfile.mkdtemp()) / "route.json"
        path.write_text(json.dumps({"effective_intensity": "direct"}), encoding="utf-8")
        rc, out = self._run(["--start", "--route-evidence", str(path), "--prompt-file", "/p.md"])
        self.assertEqual(rc, 65)
        self.assertIn("reason=route-evidence-direct-route-has-no-owner", out)
        self.assertIn("hint=a direct route runs inline; compose --shape solo", out)

    def test_a_route_sealed_for_an_excluded_harness_names_the_policy(self):
        # 2026-09-10: a route sealed for a harness the user's policy omits was
        # refused `no-eligible-route-evidence-candidate`, whose hint blames a
        # usage limit, gating, or capacity -- while the same receipt printed
        # `eligibility.<harness>=ok`. Name the real reason instead.
        route = {
            "effective_intensity": "quick", "slug": "policy-excluded",
            "capability": "autopilot-code", "capability_mode": "audit",
            "cwd": "/w/tree", "owner_model_profile": "balanced-deep",
            "owner_harness_policy": {"primary": ["claude"], "relief": [],
                                     "last_resort": [], "promote_relief_below": 0},
            "dispatch_allocation": {"strategy": "balanced", "window": 30,
                                    "harness_order": ["claude", "codex", "opencode"]},
            "registered_headless_candidates": [{"harness": "opencode", "status": "supported"}],
        }
        path = Path(tempfile.mkdtemp()) / "route.json"
        path.write_text(json.dumps(route), encoding="utf-8")
        rc, out = self._run(["--start", "--route-evidence", str(path), "--prompt-file", "/p.md"])
        self.assertEqual(rc, 65)
        self.assertIn("reason=route-evidence-candidates-outside-policy", out)
        self.assertIn("configured_candidates=\n", out + "\n")     # nothing was a candidate
        self.assertRegex(out, r"(?m)^hint=the route sealed no harness this user's policy admits")
        self.assertIn("enables claude for this model profile", out)  # the substitution ran
        self.assertNotIn("<policy-harnesses>", out)
        self.assertIn("child_spawned=0", out)
        # a route sealed for a harness the policy *does* admit keeps the old
        # verdict when that harness is merely unusable
        route["registered_headless_candidates"] = [{"harness": "claude", "status": "supported"}]
        path.write_text(json.dumps(route), encoding="utf-8")
        rc2, out2 = self._run(["--start", "--route-evidence", str(path), "--prompt-file", "/p.md"])
        self.assertNotIn("route-evidence-candidates-outside-policy", out2)

    def test_every_hint_key_is_a_reason_the_selector_can_emit(self):
        # Astra final review m2: searching the whole source was a tautology,
        # because the keys appear in the _HINTS table itself. Search only the
        # code outside that table.
        src = Path(SELECTOR).read_text(encoding="utf-8")
        head, _, rest = src.partition("_HINTS = {")
        _, _, tail = rest.partition("\n}\n")
        code = head + tail
        for key in OWNER._HINTS:
            self.assertIn(key, code, key)

    def test_the_long_form_hint_lists_every_required_flag(self):
        # Astra final review M1: the hint omitted --qa, so a caller who typed
        # exactly what it said was refused again with the same hint.
        hint = OWNER.hint_for("missing-required:--qa")
        for flag in sorted(OWNER._REQUIRED):
            self.assertIn(flag, hint, flag)
        _, values, _, _, derived = OWNER._parse(
            ["--dry-run", "--worktree", "/w", "--slug", "s", "--capability", "autopilot-code",
             "--capability-mode", "dev", "--qa", "standard", "--intensity", "quick", "--dispatch-depth", "1",
             "--worker-type", "owner", "--owner", "autopilot-code", "--assigned-contract", "autopilot-code",
             "--model-profile", "balanced-deep"])
        self.assertEqual(values["--qa"], "standard")
        self.assertEqual(derived, [])

    def test_unknown_reason_prints_no_hint_line(self):
        self.assertEqual(OWNER.hint_for("something-new"), "")
        self.assertTrue(OWNER.hint_for("missing-required:--qa"))


def _isolated_env(extra):
    """The parent runtime's session markers must not reach the selector under
    test: with both Claude and Codex markers inherited it refuses
    `caller-harness-ambiguous` (Astra final review m1)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AGENT_DISPATCH_", "AGENT_OWNER_ROUTE_", "AGENT_ROUTE_", "CODEX_DISPATCH_", "CLAUDE_DISPATCH_"))
           and k not in ("CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID")}
    env.update(extra)
    return env


class RouteDefaultsReceiptTest(unittest.TestCase):
    """The launch receipt names the flags the route supplied, before the wrapper runs."""

    def _quick_route(self):
        path = Path(tempfile.mkdtemp()) / "route.json"
        path.write_text(json.dumps({
            "effective_intensity": "quick", "slug": "probe", "capability": "autopilot-code",
            "capability_mode": "audit", "cwd": str(ROOT), "owner_model_profile": "balanced-deep",
            "registered_headless_candidates": [{"harness": "codex", "status": "supported"}],
            "owner_harness_policy": {"primary": ["codex"], "relief": [], "last_resort": [], "promote_relief_below": 0},
            "dispatch_allocation": {"strategy": "balanced", "window": 30, "harness_order": ["codex"],
                                    "usage_gate_used_percent": 90},
        }), encoding="utf-8")
        return path

    def test_receipt_prints_route_defaults_and_forwards_them_to_the_wrapper(self):
        from unittest import mock
        from contextlib import redirect_stdout
        path = self._quick_route()
        jobs = path.parent / "jobs.log"; jobs.touch()
        calls = []
        binding = SimpleNamespace(route_file=str(path), route_id="rt-x", route_hash="sha256:x",
                                  route_node="one-shot", registry_digest="sha256:r",
                                  write_scope="source-scoped", completion_gate="quick-complete")
        buf = io.StringIO()
        with mock.patch.object(OWNER.subprocess, "run", side_effect=lambda cmd, **kw: (calls.append(cmd), SimpleNamespace(returncode=0))[1]), \
             mock.patch.object(OWNER, "_usage", return_value={"claude": "ok", "codex": "ok", "opencode": "ok"}), \
             mock.patch.object(OWNER._capacity, "capacity_scores", return_value={"claude": 80.0, "codex": 80.0, "opencode": 80.0}), \
             mock.patch.object(OWNER, "derive_quick_owner_binding", return_value=binding), \
             mock.patch.dict(os.environ, _isolated_env({"AGENT_DISPATCH_JOBS": str(jobs)}), clear=True), \
             redirect_stdout(buf):
            rc = OWNER.main(["--dry-run", "--route-evidence", str(path), "--prompt-text", "probe"])
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"(?m)^route_defaults=--worktree,--slug,--capability,--capability-mode,--intensity,--model-profile,--dispatch-depth,--worker-type,--owner,--assigned-contract$")
        self.assertLess(out.index("status=eligible"), out.index("route_defaults="))
        self.assertEqual(len(calls), 1)
        cmd = calls[0]
        self.assertTrue(str(cmd[0]).endswith("adapters/codex/bin/dispatch-headless.py"))
        for flag, value in (("--worktree", str(ROOT)), ("--slug", "probe"), ("--capability", "autopilot-code"),
                            ("--capability-mode", "audit"), ("--intensity", "quick"), ("--model-profile", "balanced-deep"),
                            ("--dispatch-depth", "1"), ("--worker-type", "owner"), ("--owner", "autopilot-code"),
                            ("--assigned-contract", "autopilot-code"), ("--route-file", str(path))):
            self.assertIn(flag, cmd, flag)
            self.assertEqual(cmd[cmd.index(flag) + 1], value, flag)
        self.assertNotIn("--qa", cmd)
        self.assertNotIn("--route-evidence", cmd)

    def test_receipt_says_none_when_the_caller_spelled_out_the_tuple(self):
        from unittest import mock
        from contextlib import redirect_stdout
        path = self._quick_route()
        jobs = path.parent / "jobs.log"; jobs.touch()
        binding = SimpleNamespace(route_file=str(path), route_id="rt-x", route_hash="sha256:x",
                                  route_node="one-shot", registry_digest="sha256:r",
                                  write_scope="source-scoped", completion_gate="quick-complete")
        buf = io.StringIO()
        with mock.patch.object(OWNER.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
             mock.patch.object(OWNER, "_usage", return_value={"claude": "ok", "codex": "ok", "opencode": "ok"}), \
             mock.patch.object(OWNER._capacity, "capacity_scores", return_value={"claude": 80.0, "codex": 80.0, "opencode": 80.0}), \
             mock.patch.object(OWNER, "derive_quick_owner_binding", return_value=binding), \
             mock.patch.dict(os.environ, _isolated_env({"AGENT_DISPATCH_JOBS": str(jobs)}), clear=True), \
             redirect_stdout(buf):
            rc = OWNER.main(["--dry-run", "--route-evidence", str(path), "--worktree", str(ROOT), "--slug", "probe",
                             "--capability", "autopilot-code", "--capability-mode", "audit", "--qa", "standard",
                             "--intensity", "quick", "--dispatch-depth", "1", "--worker-type", "owner",
                             "--owner", "autopilot-code", "--assigned-contract", "autopilot-code",
                             "--model-profile", "balanced-deep", "--prompt-text", "probe"])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertRegex(buf.getvalue(), r"(?m)^route_defaults=none$")



class TopProfileOwnerTupleTest(unittest.TestCase):
    """Field-level refusal only; the accepted path is exercised on a real
    compiled route in utilities/profile_demand.test.py (review R1 M2)."""

    def _route(self, **override):
        payload = {
            "effective_intensity": "quick", "slug": "review-top", "capability": "autopilot-code",
            "capability_mode": "audit", "cwd": "/w/tree", "owner_model_profile": "top",
            "registered_headless_candidates": [{"harness": "codex", "status": "supported"}],
        }
        payload.update(override)
        path = Path(tempfile.mkdtemp()) / "route.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_top_reaches_the_wrapper_only_as_the_routes_derivation(self):
        # top review B1: the flag is never the door
        _, values, forwarded, _, derived = OWNER._parse(
            ["--start", "--route-evidence", self._route(), "--prompt-file", "/p.md"])
        self.assertEqual(values["--model-profile"], "top")
        self.assertIn("--model-profile", derived)
        for argv in (["--start", "--route-evidence", self._route(), "--model-profile", "top", "--prompt-file", "/p.md"],
                     ["--start", "--worktree", "/w", "--slug", "s", "--capability", "autopilot-code",
                      "--capability-mode", "audit", "--qa", "standard", "--intensity", "standard",
                      "--dispatch-depth", "1", "--worker-type", "owner", "--owner", "autopilot-code",
                      "--assigned-contract", "autopilot-code", "--model-profile", "top", "--prompt-file", "/p.md"]):
            with self.subTest(argv=argv[:4]), self.assertRaises(OWNER.OwnerError) as refused:
                OWNER._parse(argv)
            self.assertEqual(str(refused.exception), "profile-top-route-required")
        self.assertIn("--route-evidence", OWNER.hint_for("profile-top-route-required"))

    def test_an_unknown_profile_is_still_refused_and_the_hint_names_top(self):
        with self.assertRaises(OWNER.OwnerError) as refused:
            OWNER._parse(["--start", "--route-evidence", self._route(owner_model_profile="summit"),
                          "--prompt-file", "/p.md"])
        self.assertEqual(str(refused.exception), "invalid-model-profile")
        self.assertIn("top", OWNER.hint_for("invalid-model-profile"))

if __name__ == "__main__":
    unittest.main()
