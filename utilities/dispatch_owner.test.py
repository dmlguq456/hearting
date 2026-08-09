#!/usr/bin/env python3
import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from model_profile import resolve_profile


ROOT = Path(__file__).resolve().parents[1]
SELECTOR = ROOT / "utilities" / "dispatch-owner.py"

import json  # noqa: E402
_OWNER_SPEC = importlib.util.spec_from_file_location("dispatch_owner", SELECTOR)
OWNER = importlib.util.module_from_spec(_OWNER_SPEC)
_OWNER_SPEC.loader.exec_module(OWNER)


class DispatchOwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.jobs = self.home / "jobs.log"
        self.jobs.touch()

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

    def run_owner(self, owners="claude", extra=(), config=None):
        log_dir = self.home / "logs"
        args = [
            sys.executable, str(SELECTOR), "--dry-run", "--worktree", str(ROOT), "--slug", "owner-test",
            "--capability", "autopilot-code", "--capability-mode", "debug", "--qa", "standard",
            "--intensity", "standard", "--dispatch-depth", "1", "--worker-type", "owner",
            "--assigned-contract", "autopilot-code", "--owner", "autopilot-code",
            "--model-profile", "deep", "--jobs", str(self.jobs), "--log-dir", str(log_dir),
            *extra,
        ]
        env = os.environ.copy()
        env.update({
            "AGENT_HOME": str(self.home / "agent-home"),
            "HOME": str(self.home),
            "DISPATCH_DEFAULTS_CONFIG": str(config or self.config(owners)),
            "CODEX_DISPATCH_MODEL": "interactive-inheritance-must-not-leak",
            "CODEX_DISPATCH_MODEL_PROFILE": "interactive-profile-must-not-leak",
        })
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
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected = resolve_profile(adapter, ROOT / "adapters" / adapter / "config" / "models.conf", "deep")
        self.assertIn(f"adapter={adapter}", result.stdout)
        self.assertIn(f"model={expected['model']}", result.stdout)
        budget_key = "reasoning" if expected["budget_kind"] == "effort" and adapter == "codex" else expected["budget_kind"]
        self.assertIn(f"{budget_key}={expected['budget']}", result.stdout)

    def test_configured_claude_selects_claude_wrapper_and_adapter_model_config(self):
        self.assert_model_map(self.run_owner(), "claude")

    def test_configured_codex_selects_codex_wrapper_and_adapter_model_config(self):
        self.assert_model_map(self.run_owner("codex"), "codex")

    def test_each_adapter_derives_model_and_budget_from_its_models_conf(self):
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter):
                self.assert_model_map(self.run_owner(adapter), adapter)

    def test_no_cross_harness_model_alias_leakage(self):
        result = self.run_owner("codex")
        self.assert_model_map(result, "codex")
        self.assertNotIn("interactive-inheritance", result.stdout)

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

    def test_unknown_state_with_no_jobs_file_selects_configured_claude(self):
        self.jobs.unlink()
        result = self.run_owner()
        self.assert_model_map(result, "claude")
        self.assertIn("eligibility.claude=unknown", result.stdout)
        self.assertIn("selection_source=configured-normal", result.stdout)
        self.assertNotIn("rejected.1=claude", result.stdout)

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

    def test_selector_only_option_never_reaches_the_wrapper(self):
        _, _, forwarded, evidence = OWNER._parse([
            "--route-evidence", "/tmp/r.json", "--worktree", "/w", "--slug", "s",
            "--capability", "autopilot-code", "--capability-mode", "dev", "--qa", "standard",
            "--intensity", "standard", "--dispatch-depth", "1", "--worker-type", "owner",
            "--assigned-contract", "autopilot-code", "--owner", "autopilot-code",
            "--model-profile", "deep", "--dry-run"])
        self.assertEqual(evidence, "/tmp/r.json")
        self.assertNotIn("--route-evidence", forwarded)
        self.assertNotIn("/tmp/r.json", forwarded)


if __name__ == "__main__":
    unittest.main()
