#!/usr/bin/env python3
import copy
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import dispatch_capacity_evidence as Q

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "utilities" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QuotaEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.runtime = self.home / ".claude"
        self.runtime.mkdir()
        self.config = self.home / ".claude.json"
        self.config.write_text(json.dumps({"oauthAccount": {"accountUuid": "account-a", "organizationUuid": "org-a"}}))
        self.env = {"HOME": str(self.home), "PATH": os.environ["PATH"], "AGENT_HOME": str(ROOT),
                    "HARNESS_CAPACITY_SCORES": "claude:99,codex:61,opencode:100",
                    "AGENT_DISPATCH_JOBS": str(self.home / "jobs.log")}
        self.now = int(time.time())
        self.jobs = self.home / "jobs.log"
        self.attempt = "att-native-quota"
        self.log = self.home / f"job.{self.attempt}.claude.jsonl"
        self.rows = [
            {"type": "rate_limit_event", "session_id": "sid-a", "rate_limit_info": {
                "status": "rejected", "rateLimitType": "seven_day", "resetsAt": self.now + 86400,
                "unifiedWindows": {"seven_day": {"utilization": 1}}, "isUsingOverage": False}},
            {"type": "result", "session_id": "sid-a", "is_error": True,
             "subtype": "success", "terminal_reason": "api_error", "api_error_status": 429},
        ]
        self.write()

    def write(self, *, scope=True, harness="claude", note="dead-launch-exit-1"):
        self.log.write_text("".join(json.dumps(r) + "\n" for r in self.rows))
        stamp = datetime.fromtimestamp(self.now, timezone.utc).isoformat().replace("+00:00", "Z")
        metadata = {"attempt_id": self.attempt, "harness": harness, "note": note,
                    "log_file": str(self.log), "model": "claude-opus-4-6", "launch_outcome": "governed-process-group-drained"}
        if scope:
            metadata.update(Q.launch_scope(harness, self.env))
        self.jobs.write_text(f"{stamp}\tdone\t/repo\t/work\tjob\t" + ",".join(f"{k}={v}" for k,v in metadata.items()) + "\n")

    def states(self):
        result = subprocess.run([str(ROOT / "utilities/usage-check.sh"), "--jobs", str(self.jobs)],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split() for line in result.stdout.splitlines())

    def test_actual_usage_consumer_overrides_positive_gauge_without_mutating_evidence(self):
        before = (self.jobs.read_bytes(), self.log.read_bytes(), self.config.read_bytes())
        states = self.states()
        self.assertTrue(states["claude"].startswith("limited("), states)
        self.assertEqual((states["codex"], states["opencode"]), ("ok", "ok"))
        capacity = load("quota_capacity", "harness-capacity.py")
        policy = {"primary": ["claude", "codex"], "relief": [], "last_resort": [], "promote_relief_below": 0}
        for strategy in ("balanced", "capacity-aware", "least-recent-attempts"):
            with self.subTest(strategy=strategy):
                selected = capacity.select(policy, states, dict.fromkeys(Q.HARNESSES, 0), Q.HARNESSES,
                                           {"claude": 99, "codex": 61, "opencode": 100}, strategy=strategy)
                self.assertEqual(selected[:2], ("codex", "primary"))
        self.assertEqual(before, (self.jobs.read_bytes(), self.log.read_bytes(), self.config.read_bytes()))

    def test_gauge_and_batch_selection_consume_the_same_scoped_feedback(self):
        capacity = load("quota_gauge", "harness-capacity.py")
        batch = load("quota_batch", "dispatch-batch.py")
        from types import SimpleNamespace
        nodes = [{"id": name, "model_profile": "light"} for name in ("first", "second")]
        route = {"dispatch_allocation": {"strategy": "balanced", "window": 30}}
        selection = SimpleNamespace(fallback_hop="same-harness-headless", ordinal=1)
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(capacity.capacity_scores()["claude"], 0.0)
            with mock.patch.object(batch.DISPATCH_NODE, "resolve_checked_tuple", return_value=selection):
                assignments, independence, diagnostic = batch.assign_harnesses(route, nodes, jobs=self.jobs, allow_degraded=False)
            self.assertEqual({a[1] for a in assignments}, {"codex", "opencode"})
            self.assertEqual(independence, "cross-harness")
            self.assertIn("claude", diagnostic["family_exclusions"])

    def test_reset_expiry_and_new_week_do_not_carry_old_rejection(self):
        self.assertIn("claude", Q.active_limits(self.jobs, now=self.now + 1, env=self.env))
        self.assertEqual(Q.active_limits(self.jobs, now=self.now + 86400, env=self.env), {})
        self.assertEqual(Q.active_limits(self.jobs, now=self.now + 9 * 86400, env=self.env), {})

    def test_account_org_runtime_and_auth_provider_are_separate_scopes(self):
        original = self.config.read_text()
        for identity in ({"accountUuid": "other", "organizationUuid": "org-a"},
                         {"accountUuid": "account-a", "organizationUuid": "other"}):
            self.config.write_text(json.dumps({"oauthAccount": identity}))
            self.assertEqual(Q.active_limits(self.jobs, env=self.env), {})
        self.config.write_text(original)
        for key, value in (("CLAUDE_CONFIG_DIR", str(self.home / "other")),
                           ("ANTHROPIC_API_KEY", "fixture-key"), ("CLAUDE_CODE_OAUTH_TOKEN", "different-token")):
            self.assertEqual(Q.active_limits(self.jobs, env={**self.env, key: value}), {})
        self.assertNotIn("account-a", json.dumps(Q.launch_scope("claude", self.env)))

    def test_same_account_in_another_runtime_home_shares_quota(self):
        other = self.home / "another-runtime"
        other.mkdir()
        (other / ".claude.json").write_text(self.config.read_text())
        env = {**self.env, "CLAUDE_CONFIG_DIR": str(other)}
        self.assertEqual(Q.launch_scope("claude", self.env), Q.launch_scope("claude", env))
        self.assertIn("claude", Q.active_limits(self.jobs, env=env))
        (other / ".claude.json").write_text(json.dumps({"oauthAccount": {
            "accountUuid": "other-account", "organizationUuid": "org-a"}}))
        self.assertEqual(Q.active_limits(self.jobs, env=env), {})

    def test_published_v1403_scope_remains_valid_in_its_original_runtime_home(self):
        old = Q.digest(["claude-subscription-v1", str(self.runtime.resolve()),
                        {"accountUuid": "account-a", "organizationUuid": "org-a"}])
        current = Q.launch_scope("claude", self.env)
        self.jobs.write_text(self.jobs.read_text().replace(current["quota_scope"], old)
                            .replace("quota_scope_kind=claude-subscription-v2", "quota_scope_kind=claude-subscription-v1"))
        self.assertIn("claude", Q.active_limits(self.jobs, env=self.env))
        self.assertTrue(Q.usage_states(self.jobs, env=self.env)["claude"].startswith("limited("))
        self.config.write_text(json.dumps({"oauthAccount": {"accountUuid": "other", "organizationUuid": "org-a"}}))
        self.assertEqual(Q.active_limits(self.jobs, env=self.env), {})

    def test_old_unscoped_attempt_is_diagnostic_and_cannot_be_rebound_retroactively(self):
        self.write(scope=False)
        self.assertEqual(Q.active_limits(self.jobs, env=self.env), {})
        evidence = Q.observations(self.jobs, env=self.env)[0]
        self.assertEqual((evidence["scope_authority"], evidence["window"], evidence["reset_epoch"]),
                         ("unbound", "seven_day", self.now + 86400))

    def test_legacy_text_marker_cannot_override_a_scoped_model_or_account(self):
        self.rows[0]["rate_limit_info"]["rateLimitType"] = "seven_day_opus"
        self.write(note="dead-usage-limit")
        self.assertEqual(self.states()["claude"], "ok")  # no requested model, not a whole-harness block
        self.rows[0]["rate_limit_info"]["rateLimitType"] = "seven_day"
        self.write(note="dead-usage-limit")
        self.config.write_text(json.dumps({"oauthAccount": {"accountUuid": "other", "organizationUuid": "org-a"}}))
        self.assertEqual(self.states()["claude"], "ok")

    def test_legacy_clock_does_not_reappear_daily_after_a_weekly_event_ages_out(self):
        current = self.now
        self.now -= 10 * 86400
        self.rows[0]["rate_limit_info"]["resetsAt"] = self.now + 86400
        self.write(note="dead-usage-limit")
        self.jobs.write_text(self.jobs.read_text().rstrip() + ",reset=11pm\n")
        self.assertEqual(Q.usage_states(self.jobs, now=current, env=self.env)["claude"], "ok")

    def test_model_specific_limit_never_disables_other_models(self):
        self.rows[0]["rate_limit_info"]["rateLimitType"] = "seven_day_opus"
        self.write()
        self.assertIn("claude", Q.active_limits(self.jobs, models={"claude": "claude-opus-4-6"}, env=self.env))
        for model in ("claude-sonnet-4-6", "claude-haiku-4-5", "unknown"):
            self.assertEqual(Q.active_limits(self.jobs, models={"claude": model}, env=self.env), {})
        self.assertEqual(Q.active_limits(self.jobs, env=self.env), {})

    def test_generic_429_auth_errors_wrong_session_and_overage_are_not_weekly_quota(self):
        original = copy.deepcopy(self.rows)
        cases = [lambda r: r.pop(0), lambda r: r[1].update(api_error_status=401),
                 lambda r: r[1].update(session_id="other"), lambda r: r[1].update(is_error=False),
                 lambda r: r[0]["rate_limit_info"].update(status="allowed"),
                 lambda r: r[0]["rate_limit_info"].update(rateLimitType="overage"),
                 lambda r: r[0]["rate_limit_info"].update(isUsingOverage=True),
                 lambda r: r[0]["rate_limit_info"].update(resetsAt=self.now + 100 * 86400),
                 lambda r: r[0]["rate_limit_info"].update(resetsAt=float("nan")),
                 lambda r: r[0].update(rate_limit_info=[])]
        for index, mutate in enumerate(cases):
            with self.subTest(case=index):
                rows = copy.deepcopy(original)
                mutate(rows)
                self.assertIsNone(Q.native_quota(rows, observed_at=self.now))
        for harness in ("codex", "opencode"):
            self.write(harness=harness)
            self.assertEqual(Q.active_limits(self.jobs, env=self.env), {})

    def test_old_turn_or_newer_accepted_event_never_overrides_current_outcome(self):
        rows = copy.deepcopy(self.rows)
        rows.append({**rows[-1], "api_error_status": 500})
        self.assertIsNone(Q.native_quota(rows, observed_at=self.now))
        rows = copy.deepcopy(self.rows)
        rows.insert(1, {**rows[0], "rate_limit_info": {"status": "allowed"}})
        self.assertIsNone(Q.native_quota(rows, observed_at=self.now))

    def test_harvest_exposes_quota_not_envelope_contract_violation(self):
        terminal = load("quota_terminal", "codex_dispatch_terminal.py")
        result = terminal.inspect_terminal_attempt(self.log, worktree=self.home, artifact_root_metadata=self.home)
        self.assertEqual(result["blocker_reason"], "capacity")
        self.assertEqual((result["quota_window"], result["quota_reset_epoch"]),
                         ("seven_day", str(self.now + 86400)))

    def test_d2_order_and_same_harness_retry_consume_scoped_rejection(self):
        fallback = load("quota_fallback", "stage-dispatch-fallback.py")
        allocation = {"strategy": "balanced", "window": 30, "harness_order": list(Q.HARNESSES)}
        route = {"route_id": "r", "dispatch_allocation": allocation}
        candidates = [{"child_harness": h, "parent_harness": "codex", "parent_transport": "headless",
                       "parent_sandbox": "workspace-write", "launch_authority": "conductor", "status": "supported"}
                      for h in Q.HARNESSES]
        node = {"id": "test", "model_profile": "light", "harness_policy": {
            "primary": ["claude", "codex"], "relief": [], "last_resort": [], "promote_relief_below": 0},
            "fallback_hops": [{"fallback_hop": "same-harness-headless", "ordinal": 1, "candidates": candidates}]}
        with mock.patch.dict(os.environ, self.env, clear=True):
            ordered, context = fallback.ordered_fallback_hops(route, node, self.jobs)
            self.assertEqual(ordered[0]["candidates"][0]["child_harness"], "codex")
            claude = next(h["candidates"][0] for h in ordered if h["candidates"][0]["child_harness"] == "claude")
            self.assertIn("_allocation_skip", claude)
            self.assertEqual(context["limited"], ["claude"])
            args = type("Args", (), {"jobs": self.jobs, "capacity_model": "claude-sonnet-4-6",
                                      "capacity_effort": "high", "capacity_reasoning": None, "capacity_variant": None})()
            with mock.patch.object(fallback, "capacity_context", return_value={"retries": [], "cooled": []}), \
                 mock.patch.object(fallback, "allowed_capacity_settings", return_value=True), \
                 mock.patch.object(fallback, "wrapper_command") as spawn:
                state, detail, reason = fallback.capacity_retry(args, route, node, candidates[0], 1,
                                                               {"model": "claude-opus-4-6"}, [])
                self.assertEqual(state, "descend")
                self.assertIn("capacity-quota-until-", reason)
                spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
