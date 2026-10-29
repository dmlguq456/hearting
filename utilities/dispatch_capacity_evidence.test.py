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


_STATE_TMP = None
_STATE_OLD = None


def setUpModule():
    # The shared-gauge reader consults Fleet's live usage cache; keep these
    # tests off the developer's real ~/.local/state store.
    global _STATE_TMP, _STATE_OLD
    _STATE_TMP = tempfile.TemporaryDirectory()
    _STATE_OLD = os.environ.get("FLEET_USAGE_STATE_DIR")
    os.environ["FLEET_USAGE_STATE_DIR"] = _STATE_TMP.name


def tearDownModule():
    if _STATE_OLD is None:
        os.environ.pop("FLEET_USAGE_STATE_DIR", None)
    else:
        os.environ["FLEET_USAGE_STATE_DIR"] = _STATE_OLD
    _STATE_TMP.cleanup()


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

    def test_opencode_hard_hold_requires_every_automatic_model_to_be_unavailable(self):
        import model_profile
        import opencode_runtime
        selected = {"model": "opencode-go/model", "budget": "high"}

        def state(candidates):
            with mock.patch.object(model_profile, "resolve_runtime_profile",
                                   return_value=(selected, object())), \
                 mock.patch.object(opencode_runtime, "automatic_candidates",
                                   return_value=candidates):
                return Q._opencode_model_state("balanced", self.env)

        held = state([
            {"model": "opencode-go/model", "state": "unavailable"},
            {"model": "opencode/beam-free", "state": "unavailable"},
            {"model": "openai/gpt-6.1-sol", "state": "unavailable"},
        ])
        self.assertTrue(held.startswith("limited(provider-model-unavailable:"), held)
        self.assertIn("opencode-go-unavailable", held)
        # Go is probed before placement, so its unknown stays automatic.
        self.assertIsNone(state([
            {"model": "opencode-go/model", "state": "unknown"},
            {"model": "openai/gpt-6.1-sol", "state": "unavailable"},
        ]))
        self.assertIsNone(state([
            {"model": "opencode-go/model", "state": "unavailable"},
            {"model": "openai/gpt-6.1-sol", "state": "usable"},
        ]))

    def test_opencode_unverified_provider_is_left_out_without_a_pause(self):
        """The 2026-10-20 replacement owner: deep resolved to an OpenAI model on
        OpenCode with no evidence at all, and automatic placement chose it."""
        import model_profile
        import opencode_runtime
        selected = {"model": "openai/gpt-6.1-sol", "budget": "xhigh"}
        candidates = [
            {"model": "openai/gpt-6.1-sol", "state": "unknown",
             "reason": "provider-model-unverified"},
            {"model": "opencode-go/model", "state": "unavailable",
             "reason": "subscription-missing"},
        ]
        with mock.patch.object(model_profile, "resolve_runtime_profile",
                               return_value=(selected, object())), \
             mock.patch.object(opencode_runtime, "automatic_candidates",
                               return_value=candidates):
            held = Q._opencode_model_state("deep", self.env)
            self.assertEqual(Q.model_hold("opencode", "deep", env=self.env), held)
        self.assertEqual(held, "limited(provider-model-unverified:"
                               "openai-provider-model-unverified+opencode-go-subscription-missing)")
        # Every automatic consumer reads any `limited(...)` as "not this harness".
        capacity = load("unverified_capacity", "harness-capacity.py")
        self.assertTrue(capacity._limited(held))
        self.assertIsNone(Q.model_hold("claude", "deep", env=self.env))
        self.assertIsNone(Q.model_hold("opencode", None, env=self.env))

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

    def test_usable_codex_credits_do_not_lift_native_claude_rejection(self):
        from fleet.collectors import usage_cache
        with mock.patch.dict(os.environ, self.env):
            usage_cache.record("codex", {"windows": [["7d", 100, self.now + 86400]],
                                         "credits": {"has_credits": True}}, now=self.now)
            # Exercise the credit exception rather than this class's manual score.
            with mock.patch.dict(os.environ, {"HARNESS_CAPACITY_SCORES": ""}):
                states = Q.usage_states(self.jobs, now=self.now)
                self.assertEqual(states["codex"], "ok")
                self.assertTrue(states["claude"].startswith("limited("))
                self.assertEqual(Q.harness_hold(self.jobs, "claude", now=self.now)["until_epoch"],
                                 self.now + 86400)
                self.assertEqual(Q.active_limits(self.jobs, now=self.now)["claude"]["scope_authority"],
                                 "launch-bound")

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
            self.assertEqual(len(assignments), 2)
            self.assertTrue({a[1] for a in assignments} <= {"codex", "opencode"})
            self.assertNotIn("claude", {a[1] for a in assignments})
            self.assertEqual(independence, "persona")
            self.assertIn("claude", diagnostic["family_exclusions"])

    def test_reset_expiry_and_new_week_do_not_carry_old_rejection(self):
        self.assertIn("claude", Q.active_limits(self.jobs, now=self.now + 1, env=self.env))
        self.assertEqual(Q.active_limits(self.jobs, now=self.now + 86400, env=self.env), {})
        self.assertEqual(Q.active_limits(self.jobs, now=self.now + 9 * 86400, env=self.env), {})

    def test_included_credit_rejection_blocks_only_the_launched_model_until_reset(self):
        self.rows[0]["rate_limit_info"].update(rateLimitType="seven_day_overage_included",
            overageStatus="rejected", overageDisabledReason="org_level_disabled",
            unifiedWindows={"seven_day": {"utilization": .99},
                            "seven_day_overage_included": {"utilization": 1}})
        self.write()
        self.jobs.write_text(self.jobs.read_text().replace("model=claude-opus-4-6", "model=credit-model"))
        before = self.jobs.read_bytes(), self.log.read_bytes()
        models = {"claude": "credit-model"}
        states = Q.usage_states(self.jobs, models=models, env=self.env)
        self.assertTrue(states["claude"].startswith("limited("), states)
        for requested in ({}, {"claude": "another-model"}):
            self.assertEqual(Q.active_limits(self.jobs, models=requested, env=self.env), {})
        self.assertEqual(Q.active_limits(self.jobs, models=models, now=self.now + 86400, env=self.env), {})
        self.assertEqual(before, (self.jobs.read_bytes(), self.log.read_bytes()))
        # A different account, an unbound model, and paid overage never gain
        # blocking authority from this model-specific subscription event.
        self.config.write_text(json.dumps({"oauthAccount": {"accountUuid": "other", "organizationUuid": "org-a"}}))
        self.assertEqual(Q.active_limits(self.jobs, models=models, env=self.env), {})
        for changes in ({"overageStatus": "allowed"}, {"isUsingOverage": True}):
            rows = copy.deepcopy(self.rows)
            rows[0]["rate_limit_info"].update(changes)
            self.assertIsNone(Q.native_quota(rows, observed_at=self.now, requested_model="credit-model"))
        self.assertIsNone(Q.native_quota(self.rows, observed_at=self.now))

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

    # -- an owner that died at a usage limit (`dead-capacity`) is a hold on its harness ----
    OBSERVED = int(datetime(2026, 9, 29, 17, 0, tzinfo=timezone.utc).timestamp())  # 02:00 in Asia/Seoul

    def write_capacity(self, lines, *, observed=None, scope=True, note="dead-capacity"):
        observed = self.OBSERVED if observed is None else observed
        self.log.write_text("".join(lines))
        stamp = datetime.fromtimestamp(observed, timezone.utc).isoformat().replace("+00:00", "Z")
        metadata = {"attempt_id": self.attempt, "harness": "claude", "note": note, "failure_class": "capacity",
                    "log_file": str(self.log), "model": "claude-opus-4-6", "worker_type": "owner"}
        if scope:
            metadata.update(Q.launch_scope("claude", self.env))
        self.jobs.write_text(f"{stamp}\tdone\t/repo\t/work\tjob\t" + ",".join(f"{k}={v}" for k, v in metadata.items()) + "\n")

    @staticmethod
    def result_line(text):
        return json.dumps({"type": "result", "is_error": True, "api_error_status": 429, "session_id": "s",
                           "result": text}) + "\n"

    def state(self, now):
        return Q.usage_states(self.jobs, now=now, env=self.env)["claude"]

    def test_dead_capacity_result_text_reset_limits_until_reset(self):
        self.write_capacity([self.result_line("You've hit your session limit \u00b7 resets 3am (Asia/Seoul)")])
        reset = int(datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc).timestamp())  # 03:00 KST
        self.assertTrue(self.state(self.OBSERVED + 1800).startswith("limited("))
        hold = Q.harness_hold(self.jobs, "claude", now=self.OBSERVED + 1800, env=self.env)
        self.assertEqual(hold["until_epoch"], reset)
        self.assertEqual(self.state(reset + 60), "ok")
        self.assertIsNone(Q.harness_hold(self.jobs, "claude", now=reset + 60, env=self.env))
        self.assertIsNone(Q.harness_hold(self.jobs, "codex", now=self.OBSERVED + 1800, env=self.env))

    def test_dead_capacity_without_reset_uses_unknown_window(self):
        self.write_capacity([self.result_line("You've hit your limit")])
        self.assertEqual(self.state(self.OBSERVED + 1800), "limited(unknown-reset)")
        self.assertEqual(Q.harness_hold(self.jobs, "claude", now=self.OBSERVED + 1800, env=self.env)["until_epoch"],
                         self.OBSERVED + 3600)
        self.assertEqual(self.state(self.OBSERVED + 3601), "ok")

    def test_dead_capacity_other_quota_scope_is_ignored(self):
        self.write_capacity([self.result_line("limit \u00b7 resets 3am (Asia/Seoul)")])
        self.config.write_text(json.dumps({"oauthAccount": {"accountUuid": "other", "organizationUuid": "org-a"}}))
        self.assertEqual(self.state(self.OBSERVED + 60), "ok")
        self.assertIsNone(Q.harness_hold(self.jobs, "claude", now=self.OBSERVED + 60, env=self.env))

    def test_dead_capacity_reads_only_last_result_line_in_tail(self):
        future = "limit \u00b7 resets 2099-01-01 00:00 (UTC)"
        past = "limit \u00b7 resets 2020-01-01 00:00 (UTC)"
        # an earlier result and a line beyond the tail are not the attempt's outcome
        filler = json.dumps({"type": "assistant", "text": "x" * (Q.TAIL_BYTES + 1000)}) + "\n"
        self.write_capacity([self.result_line(future), filler, self.result_line(past)])
        self.assertEqual(self.state(self.OBSERVED + 60), "ok")
        self.write_capacity([self.result_line(future), json.dumps({"type": "assistant"}) + "\n",
                             self.result_line("You've hit your limit")])
        self.assertEqual(self.state(self.OBSERVED + 60), "limited(unknown-reset)")

    def test_dead_capacity_unmatched_reset_text_uses_unknown_window(self):
        for text in ("limit \u00b7 resets Oct 1, 3am (UTC)", "limit \u00b7 resets in 2 hours"):
            with self.subTest(text=text):
                self.write_capacity([self.result_line(text)])
                self.assertEqual(self.state(self.OBSERVED + 60), "limited(unknown-reset)")

    def test_timezone_reaches_date_only_as_an_environment_value(self):
        marker = self.home / "injected"
        self.write_capacity([self.result_line(f"limit \u00b7 resets 3am (Asia/Seoul; touch {marker})")])
        self.assertTrue(self.state(self.OBSERVED + 60).startswith("limited("))
        self.assertFalse(marker.exists())

    def test_dead_capacity_that_passed_is_not_a_hold(self):
        self.write_capacity([self.result_line("limit \u00b7 resets 3am (Asia/Seoul)")])
        self.jobs.write_text(self.jobs.read_text().replace("failure_class=capacity", "failure_class=pass"))
        self.assertEqual(self.state(self.OBSERVED + 60), "ok")


class LegacyQuotaReasonTests(unittest.TestCase):
    """Historical text markers describe quota, not context/config discovery."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs.log"
        self.now = 1793262000
        self.env = {"HOME": self.tmp.name}
        self.gauge = mock.patch.object(Q, "gauge_limits", return_value={})
        self.gauge.start()
        self.addCleanup(self.gauge.stop)

    def row(self, harness, note, *, reset="-", failure_class=None):
        stamp = datetime.fromtimestamp(self.now, timezone.utc).isoformat().replace("+00:00", "Z")
        meta = f"harness={harness},attempt_id=att-legacy,note={note},reset={reset}"
        if failure_class is not None:
            meta += f",failure_class={failure_class}"
        self.jobs.write_text(f"{stamp}\tdone\t/repo\t/work\tx\t{meta}\n")

    def test_unavailable_and_prefix_matches_are_not_quota_holds(self):
        for harness in Q.HARNESSES:
            for note in ("dead-opencode-context-limit-unavailable", "dead-config-unavailable",
                         "dead-context-limit-unavailable", "dead-model-info-timeout",
                         "dead-usage-limit-unavailable", "dead-session-limit-config-unavailable"):
                with self.subTest(harness=harness, note=note):
                    self.row(harness, note)
                    self.assertEqual(Q.usage_states(self.jobs, now=self.now, env=self.env)[harness], "ok")
                    self.assertIsNone(Q.harness_hold(self.jobs, harness, now=self.now, env=self.env))

    def test_exact_quota_reasons_keep_unknown_reset_hold_and_expiry(self):
        for harness in Q.HARNESSES:
            for note in ("dead-limit", "dead-session-limit", "dead-usage-limit",
                         "dead-rate-limit", "dead-quota-limit", "dead-capacity"):
                with self.subTest(harness=harness, note=note):
                    self.row(harness, note)
                    held = Q.harness_hold(self.jobs, harness, now=self.now, env=self.env)
                    self.assertEqual(held, {"until_epoch": self.now + 3600, "label": "unknown-reset"})
                    self.assertIsNone(Q.harness_hold(self.jobs, harness, now=self.now + 3600, env=self.env))

    def test_explicit_reset_and_pass_capacity_meaning_are_preserved(self):
        reset = datetime.fromtimestamp(self.now + 120, timezone.utc).isoformat().replace("+00:00", "Z")
        self.row("opencode", "dead-usage-limit", reset=reset)
        self.assertEqual(Q.harness_hold(self.jobs, "opencode", now=self.now, env=self.env)["until_epoch"], self.now + 120)
        self.assertIsNone(Q.harness_hold(self.jobs, "opencode", now=self.now + 120, env=self.env))
        self.row("opencode", "dead-capacity", failure_class="pass")
        self.assertIsNone(Q.harness_hold(self.jobs, "opencode", now=self.now, env=self.env))

    def test_old_persisted_false_hold_is_rebuilt_without_rewriting_registry(self):
        note = "dead-opencode-context-limit-unavailable"
        self.row("opencode", note)
        original = self.jobs.read_bytes()
        with mock.patch.object(Q, "_settled", return_value=True), \
             mock.patch.object(Q, "CACHE_SCHEMA", 1), \
             mock.patch.object(Q, "_LEGACY_QUOTA_NOTES", Q._LEGACY_QUOTA_NOTES | {note}):
            old = Q._snapshot(self.jobs, self.now)
            self.assertEqual(old["legacy"][0]["n"], note)
            Q._persist(old)
        self.assertEqual(json.loads(Q._cache_path(self.jobs).read_text())["schema"], 1)
        with mock.patch.object(Q, "_build_snapshot", wraps=Q._build_snapshot) as rebuild:
            self.assertIsNone(Q.harness_hold(self.jobs, "opencode", now=self.now, env=self.env))
            self.assertEqual(rebuild.call_count, 1)
        self.assertEqual(self.jobs.read_bytes(), original)


class EvidenceCacheTests(unittest.TestCase):
    """The disk cache must never change an answer: every check compares it with a direct computation."""

    setUp = QuotaEvidenceTests.setUp
    write = QuotaEvidenceTests.write

    def cache_file(self):
        return Path(f"{self.jobs}.capacity-cache.json")

    def settle(self, *paths, extra_ns=0):
        """Backdate mtimes: the cache does not trust a file written within the last 100ms."""
        for path in paths or (self.jobs, self.log):
            stat = path.stat()
            old = time.time_ns() - 5_000_000_000 + extra_ns
            os.utime(path, ns=(stat.st_atime_ns, old))

    def direct(self, fn, *args, **kwargs):
        with mock.patch.object(Q, "_DISK_CACHE", False):
            return fn(*args, **kwargs)

    def answers(self, now, **kwargs):
        env = kwargs.pop("env", self.env)
        return (Q._usage(self.jobs, now=now, env=env, **kwargs),
                Q.observations(self.jobs, now=now, env=env),
                Q.active_limits(self.jobs, now=now, env=env, **{k: v for k, v in kwargs.items() if k == "models"}))

    def assert_same_as_direct(self, now, **kwargs):
        cached = self.answers(now, **kwargs)
        again = self.answers(now, **kwargs)
        self.assertEqual(cached, self.direct(self.answers, now, **kwargs))
        self.assertEqual(cached, again)

    def native_reads(self):
        return mock.patch.object(Q, "_native_rows", wraps=Q._native_rows)

    def test_cache_holds_only_minimal_private_evidence_beside_jobs_log(self):
        self.settle()
        Q.usage_states(self.jobs, now=self.now + 10, env=self.env)
        cache = self.cache_file()
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
        self.assertLess(cache.stat().st_size, 20_000)
        text = cache.read_text()
        data = json.loads(text)
        self.assertEqual(set(data), {"schema", "jobs", "key", "horizon", "native", "legacy"})
        # the evidence is the two log rows `native_quota` reads, not the jobs row or the log tail
        for leaked in ("/repo", "launch_outcome", "governed-process-group-drained"):
            self.assertNotIn(leaked, text)
        self.assertEqual(len(data["native"][0]["e"]), 2)
        self.assertEqual(sorted(p.name for p in self.home.glob("jobs.log*")),
                         ["jobs.log", "jobs.log.capacity-cache.json",
                          "jobs.log.capacity-cache.json.lock"])

    def test_concurrent_shared_miss_builds_once(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading

        self.write(scope=False)
        self.settle()
        barrier = threading.Barrier(8)
        original = Q._build_snapshot

        def counted(*args, **kwargs):
            time.sleep(0.03)
            return original(*args, **kwargs)

        with mock.patch.object(Q, "_build_snapshot", side_effect=counted) as build:
            def consume(_):
                barrier.wait(timeout=2)
                return Q.usage_states(self.jobs, now=self.now + 10, env=self.env)

            with ThreadPoolExecutor(max_workers=8) as pool:
                answers = list(pool.map(consume, range(8)))
        self.assertEqual(len(set(tuple(sorted(x.items())) for x in answers)), 1)
        self.assertEqual(build.call_count, 1)

    def test_same_answers_with_and_without_the_cache_across_now_scope_and_model(self):
        other = {**self.env}
        for label, scoped in (("scoped", True), ("unscoped", False)):
            self.write(scope=scoped)
            self.settle()
            for now in (self.now - 10, self.now, self.now + 1, self.now + 86399, self.now + 86400,
                        self.now + 86401, self.now + 8 * 86400 - 1, self.now + 8 * 86400 + 1,
                        self.now + 9 * 86400):
                for models in ({}, {"claude": "claude-opus-4-6"}, {"claude": "claude-sonnet-4-6"}):
                    with self.subTest(label=label, now=now - self.now, models=models):
                        self.assert_same_as_direct(now, models=models)
            # another account never matches, whatever is cached
            self.config.write_text(json.dumps({"oauthAccount": {"accountUuid": "other", "organizationUuid": "org-a"}}))
            self.assert_same_as_direct(self.now + 10)
            self.config.write_text(json.dumps({"oauthAccount": {"accountUuid": "account-a", "organizationUuid": "org-a"}}))

    def test_model_scoped_credit_rejection_matches_the_direct_answer(self):
        self.rows[0]["rate_limit_info"].update(rateLimitType="seven_day_overage_included",
            overageStatus="rejected", unifiedWindows={"seven_day_overage_included": {"utilization": 1}})
        self.write()
        self.settle()
        for now in (self.now, self.now + 86400):
            for models in ({}, {"claude": "claude-opus-4-6"}, {"claude": "other"}):
                with self.subTest(now=now - self.now, models=models):
                    self.assert_same_as_direct(now, models=models)

    def test_legacy_reset_and_unknown_window_boundaries_match_the_direct_answer(self):
        observed = self.now
        for label, text in (("clock", "limit \u00b7 resets 3am (Asia/Seoul)"), ("unknown", "You've hit your limit"),
                            ("dated", "limit \u00b7 resets 2099-01-01 00:00 (UTC)")):
            stamp = datetime.fromtimestamp(observed, timezone.utc).isoformat().replace("+00:00", "Z")
            attempt = "att-legacy-" + label
            log = self.home / f"job.{attempt}.claude.jsonl"
            log.write_text(json.dumps({"type": "result", "is_error": True, "api_error_status": 429,
                                       "session_id": "s", "result": text}) + "\n")
            self.jobs.write_text(f"{stamp}\tdone\t/repo\t/work\tjob\tattempt_id={attempt},harness=claude,"
                                 f"note=dead-capacity,failure_class=capacity,log_file={log}\n")
            self.settle(self.jobs, log)
            for delta in (-5, 0, 1, 1800, 3599, 3600, 3601, 86400, 40 * 86400):
                for window in (60, 1):
                    with self.subTest(label=label, delta=delta, window=window):
                        cached = Q._usage(self.jobs, now=observed + delta, env=self.env, unknown_window_min=window)
                        self.assertEqual(cached, self.direct(Q._usage, self.jobs, now=observed + delta,
                                                             env=self.env, unknown_window_min=window))

    def test_evidence_pair_answers_like_the_full_log_tail(self):
        base = self.rows
        other_session = {**base[0], "session_id": "sid-b"}
        earlier_result = {"type": "result", "session_id": "sid-a", "is_error": False}
        cases = {
            "plain": base,
            "noise": [{"type": "assistant"}, base[0], {"type": "assistant"}, base[1]],
            "other-session-event-last": [base[0], other_session, base[1]],
            "event-before-previous-result": [base[0], earlier_result, base[1]],
            "event-after-terminal": [base[1], base[0]],
            "no-event": [base[1]],
            "accepted-event-supersedes": [base[0], {**base[0], "rate_limit_info": {
                **base[0]["rate_limit_info"], "status": "allowed"}}, base[1]],
            "two-events-latest-wins": [{**base[0], "rate_limit_info": {
                **base[0]["rate_limit_info"], "resetsAt": self.now + 3600}}, base[0], base[1]],
        }
        for name, rows in cases.items():
            for model in (None, "claude-opus-4-6"):
                pair = Q._evidence_pair(rows, self.now, model)
                for now in (self.now - 100, self.now, self.now + 86399, self.now + 86400, self.now + 9 * 86400):
                    with self.subTest(case=name, model=model, now=now - self.now):
                        expected = Q.native_quota(rows, observed_at=self.now, now=now, requested_model=model)
                        actual = Q.native_quota(pair, observed_at=self.now, now=now, requested_model=model) if pair else None
                        self.assertEqual(expected, actual)

    def test_warm_call_reads_neither_jobs_log_nor_any_log_tail(self):
        self.settle()
        first = Q._usage(self.jobs, now=self.now + 10, env=self.env)
        real = Path.read_text

        def only_the_cache(path, *args, **kwargs):
            self.assertNotEqual(path, self.jobs, "jobs.log re-read")
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", autospec=True, side_effect=only_the_cache), \
                self.native_reads() as reads:
            second = Q._usage(self.jobs, now=self.now + 10, env=self.env)
            Q.observations(self.jobs, now=self.now + 10, env=self.env)
        self.assertEqual(first, second)
        reads.assert_not_called()

    def test_one_call_reads_jobs_log_once(self):
        self.settle()
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as reads:
            Q._usage(self.jobs, now=self.now + 10, env=self.env)
        self.assertEqual([c.args[0] for c in reads.call_args_list if c.args[0] == self.jobs], [self.jobs])

    def test_jobs_log_append_rewrite_and_replace_show_on_the_next_read(self):
        self.settle()
        self.assertIn("claude", Q.active_limits(self.jobs, now=self.now + 10, env=self.env))
        # append a second failed attempt; its log is new
        attempt = "att-second"
        log = self.home / f"job.{attempt}.claude.jsonl"
        log.write_text("".join(json.dumps(r) + "\n" for r in self.rows))
        line = self.jobs.read_text().replace(self.attempt, attempt).replace(str(self.log), str(log))
        with self.jobs.open("a") as handle:
            handle.write(line)
        self.assertEqual([o["attempt_id"] for o in Q.observations(self.jobs, now=self.now + 10, env=self.env)],
                         [self.attempt, attempt])
        # same-size in-place rewrite that only moves mtime_ns
        self.settle(self.jobs, extra_ns=0)
        self.assertEqual(len(Q.observations(self.jobs, now=self.now + 10, env=self.env)), 2)
        before = self.jobs.stat()
        self.jobs.write_text(self.jobs.read_text().replace("harness=claude", "harness=cloude", 1))
        os.utime(self.jobs, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        self.assertEqual(self.jobs.stat().st_size, before.st_size)
        self.assertEqual([o["attempt_id"] for o in Q.observations(self.jobs, now=self.now + 10, env=self.env)],
                         [attempt])
        # atomic replace with the same size and the same mtime: only the inode differs
        stat = self.jobs.stat()
        replacement = self.home / "jobs.new"
        replacement.write_text(self.jobs.read_text().replace("harness=cloude", "harness=claude", 1))
        os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        os.replace(replacement, self.jobs)
        self.assertEqual(len(Q.observations(self.jobs, now=self.now + 10, env=self.env)), 2)
        self.assertEqual(Q.observations(self.jobs, now=self.now + 10, env=self.env),
                         self.direct(Q.observations, self.jobs, now=self.now + 10, env=self.env))

    def test_only_the_changed_reference_log_is_extracted_again(self):
        attempt = "att-second"
        log = self.home / f"job.{attempt}.claude.jsonl"
        log.write_text("".join(json.dumps(r) + "\n" for r in self.rows))
        with self.jobs.open("a") as handle:
            handle.write(self.jobs.read_text().replace(self.attempt, attempt).replace(str(self.log), str(log)))
        self.settle(self.jobs, self.log, log)
        self.assertEqual(len(Q.observations(self.jobs, now=self.now + 10, env=self.env)), 2)
        with self.native_reads() as reads:
            Q.observations(self.jobs, now=self.now + 10, env=self.env)
        reads.assert_not_called()
        # a newer successful result ends the second attempt's rejection; the first is untouched
        with log.open("a") as handle:
            handle.write(json.dumps({"type": "result", "session_id": "sid-a", "is_error": False}) + "\n")
        with self.native_reads() as reads:
            found = Q.observations(self.jobs, now=self.now + 10, env=self.env)
        self.assertEqual(reads.call_count, 1)
        self.assertEqual([o["attempt_id"] for o in found], [self.attempt])
        self.assertEqual(found, self.direct(Q.observations, self.jobs, now=self.now + 10, env=self.env))

    def test_a_missing_reference_log_appearing_later_is_picked_up(self):
        self.log.rename(self.home / "moved.jsonl")
        self.settle(self.jobs)
        self.assertEqual(Q.observations(self.jobs, now=self.now + 10, env=self.env), [])
        self.assertEqual(Q.observations(self.jobs, now=self.now + 10, env=self.env), [])
        (self.home / "moved.jsonl").rename(self.log)
        self.assertEqual(len(Q.observations(self.jobs, now=self.now + 10, env=self.env)), 1)
        self.log.chmod(0)
        try:
            self.assertEqual(Q.observations(self.jobs, now=self.now + 10, env=self.env),
                             self.direct(Q.observations, self.jobs, now=self.now + 10, env=self.env))
        finally:
            self.log.chmod(0o600)
        self.assertEqual(len(Q.observations(self.jobs, now=self.now + 10, env=self.env)), 1)

    def test_unsettled_files_are_not_published_as_cache_keys(self):
        Q.usage_states(self.jobs, now=self.now + 10, env=self.env)  # everything was just written
        self.assertFalse(self.cache_file().exists())

    def test_caller_registry_lines_win_over_the_disk_cache(self):
        self.settle()
        Q.usage_states(self.jobs, now=self.now + 10, env=self.env)
        stored = self.cache_file().read_bytes()
        self.assertEqual(Q.observations(self.jobs, now=self.now + 10, env=self.env, registry_lines=[]), [])
        self.assertEqual(Q.active_limits(self.jobs, now=self.now + 10, env=self.env, registry_lines=[]), {})
        self.assertEqual(len(Q.observations(self.jobs, now=self.now + 10, env=self.env)), 1)
        self.assertEqual(stored, self.cache_file().read_bytes())

    def test_damaged_or_foreign_cache_files_fall_back_to_the_direct_answer(self):
        self.settle()
        expected = self.direct(self.answers, self.now + 10)
        good = None
        for name, content in (
            ("garbage", "not json{"),
            ("empty", ""),
            ("list", "[]"),
            ("schema", None),
            ("truncated", None),
            ("bad-record", None),
        ):
            Q.usage_states(self.jobs, now=self.now + 10, env=self.env)
            good = good or json.loads(self.cache_file().read_text())
            data = json.loads(json.dumps(good))
            if name == "schema":
                data["schema"] = 99
                content = json.dumps(data)
            elif name == "truncated":
                content = json.dumps(good)[:40]
            elif name == "bad-record":
                data["native"][0]["e"] = [1, 2]
                content = json.dumps(data)
            self.cache_file().write_text(content)
            with self.subTest(name=name):
                self.assertEqual(self.answers(self.now + 10), expected)
                json.loads(self.cache_file().read_text())  # rewritten as a valid cache

    def test_cache_for_a_later_now_is_not_used_for_an_earlier_one(self):
        self.settle()
        Q.usage_states(self.jobs, now=self.now + 9 * 86400, env=self.env)
        self.assert_same_as_direct(self.now + 10)

    def test_unwritable_directory_and_failed_replace_still_answer_directly(self):
        self.settle()
        expected = self.direct(self.answers, self.now + 10)
        with mock.patch.object(os, "replace", side_effect=OSError("read-only")):
            self.assertEqual(self.answers(self.now + 10), expected)
        self.assertFalse(self.cache_file().exists())
        self.assertEqual([p.name for p in self.home.glob("*.tmp")], [])
        self.home.chmod(0o500)
        try:
            self.assertEqual(self.answers(self.now + 10), expected)
        finally:
            self.home.chmod(0o700)

    def test_a_change_during_the_read_is_answered_directly_and_not_cached(self):
        self.settle()
        real = Path.read_text
        state = {"raced": False}

        def racing(path, *args, **kwargs):
            text = real(path, *args, **kwargs)
            if path == self.jobs and not state["raced"]:
                state["raced"] = True
                with self.jobs.open("a") as handle:
                    handle.write("\n")
            return text

        with mock.patch.object(Path, "read_text", autospec=True, side_effect=racing):
            first = Q.observations(self.jobs, now=self.now + 10, env=self.env)
        self.assertEqual(len(first), 1)
        self.assertFalse(self.cache_file().exists())
        self.settle(self.jobs)
        self.assertEqual(Q.observations(self.jobs, now=self.now + 10, env=self.env), first)

    def test_unreadable_jobs_log_keeps_the_direct_contract(self):
        missing = self.home / "absent.log"
        self.assertEqual(Q.observations(missing, env=self.env), [])
        self.assertEqual(Q._usage(missing, env=self.env), (dict.fromkeys(Q.HARNESSES, "unknown"), {}))
        self.assertFalse(Path(f"{missing}.capacity-cache.json").exists())

    def test_stable_stale_miss_reuses_postlock_earlier_without_third_cache_parse(self):
        # Stable stale-cache miss: jobs.log changed after the cache was published,
        # but the reference log tail did not. Pre-lock and post-lock `_load_cache`
        # checks must stay, while the fresh jobs builder must reuse the already
        # validated earlier evidence without a third cache parse.
        self.settle()
        first = Q.observations(self.jobs, now=self.now + 10, env=self.env)
        self.assertEqual(len(first), 1)
        self.assertTrue(self.cache_file().exists())
        with self.jobs.open("a") as handle:
            handle.write("\n")  # same rows, new jobs stat -> stale cache miss
        self.settle(self.jobs)
        with mock.patch.object(Q, "_load_cache", wraps=Q._load_cache) as loads, \
                mock.patch.object(Q, "_build_snapshot", wraps=Q._build_snapshot) as build, \
                self.native_reads() as reads:
            found = Q.observations(self.jobs, now=self.now + 10, env=self.env)
        self.assertEqual(found, first)
        self.assertEqual(found, self.direct(Q.observations, self.jobs, now=self.now + 10, env=self.env))
        # Pre-lock + post-lock only; the fresh build must not parse the cache again.
        self.assertEqual(loads.call_count, 2)
        self.assertEqual(build.call_count, 1)
        previous = build.call_args[0][2] if len(build.call_args[0]) > 2 else build.call_args[1].get("previous")
        self.assertTrue(previous, "fresh build must receive the validated earlier evidence")
        # The unchanged reference log tail is not re-extracted.
        reads.assert_not_called()


FAKE_CLAUDE = r"""#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_PROBE_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
mode, reset = os.environ.get("FAKE_PROBE_MODE", "allowed"), int(os.environ.get("FAKE_PROBE_RESET", "0"))
window = os.environ.get("FAKE_PROBE_WINDOW", "five_hour")
emit = lambda row: print(json.dumps(row))
emit({"type": "system", "subtype": "init"})
if mode == "hang":
    import time
    time.sleep(5)
if mode.startswith("event-"):
    emit({"type": "rate_limit_event", "rate_limit_info": {
        "status": mode.removeprefix("event-"), "rateLimitType": window,
        "isUsingOverage": mode == "event-overage"}})
if mode in ("allowed", "rejected", "overloaded"):
    emit({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed" if mode == "overloaded" else mode, "rateLimitType": window, "resetsAt": reset,
        "isUsingOverage": False}})
if mode == "overloaded":
    emit({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429, "result": "Overloaded"})
if mode == "allowed":
    emit({"type": "result", "subtype": "success", "is_error": False, "api_error_status": None, "result": "OK"})
elif mode == "rejected":
    emit({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429, "result": "limit"})
elif mode == "auth":
    emit({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401, "result": "Invalid"})
"""


class ClaudeLimitRecheckTests(unittest.TestCase):
    """A recorded Claude limit stops holding once the login a worker would use can run."""

    OBSERVED = int(datetime(2026, 9, 29, 17, 0, tzinfo=timezone.utc).timestamp())  # 02:00 in Asia/Seoul
    LIMIT_RESET = OBSERVED + 3600  # "resets 3am (Asia/Seoul)"
    PROBE_RESET = OBSERVED + 7200

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / ".claude").mkdir()
        self.credentials = self.home / ".claude" / ".credentials.json"
        self.credentials.write_text("{}")
        (self.home / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"accountUuid": "account-b", "organizationUuid": "org-a"}}))
        self.fake = self.home / "fake-claude"
        self.fake.write_text(FAKE_CLAUDE)
        self.fake.chmod(0o755)
        self.probe_log = self.home / "probe.log"
        self.env = {"HOME": str(self.home), "PATH": os.environ["PATH"], "FAKE_PROBE_LOG": str(self.probe_log),
                    "FAKE_PROBE_MODE": "allowed", "FAKE_PROBE_RESET": str(self.PROBE_RESET),
                    "HEARTING_CAPACITY_PROBE_CLI": str(self.fake)}
        self.jobs = self.home / "jobs.log"
        self.jobs.write_text("")
        # The 2026-10-13 shape: an unscoped legacy marker, recorded before another account signed in.
        self.marker(self.OBSERVED, "att-dead")

    def marker(self, observed, attempt, reset_text="resets 3am (Asia/Seoul)"):
        log = self.home / f"job.{attempt}.claude.jsonl"
        log.write_text(json.dumps({"type": "result", "is_error": True, "api_error_status": 429, "session_id": "s",
                                   "result": f"You've hit your limit · {reset_text}"}) + "\n")
        self.row(observed, {"attempt_id": attempt, "harness": "claude", "note": "dead-capacity",
                            "failure_class": "capacity", "log_file": str(log), "model": "claude-opus-4-6",
                            "worker_type": "owner"})

    def native(self, observed, attempt, window, reset, model="claude-opus-4-6"):
        log = self.home / f"job.{attempt}.claude.jsonl"
        info = {"status": "rejected", "rateLimitType": window, "resetsAt": reset, "isUsingOverage": False}
        if window == "seven_day_overage_included":
            info.update(overageStatus="rejected", unifiedWindows={window: {"utilization": 1}})
        log.write_text("".join(json.dumps(row) + "\n" for row in (
            {"type": "rate_limit_event", "session_id": attempt, "rate_limit_info": info},
            {"type": "result", "session_id": attempt, "is_error": True, "subtype": "success",
             "api_error_status": 429})))
        self.row(observed, {"attempt_id": attempt, "harness": "claude", "note": "dead-launch-exit-1",
                            "log_file": str(log), "model": model, **Q.launch_scope("claude", self.env)})

    def row(self, observed, metadata):
        stamp = datetime.fromtimestamp(observed, timezone.utc).isoformat().replace("+00:00", "Z")
        with self.jobs.open("a") as handle:
            handle.write(f"{stamp}\tdone\t/repo\t/work\tjob\t"
                         + ",".join(f"{k}={v}" for k, v in metadata.items()) + "\n")

    def probes(self):
        return [json.loads(line) for line in self.probe_log.read_text().splitlines()] if self.probe_log.exists() else []

    def state(self, now, **kwargs):
        return Q.usage_states(self.jobs, now=now, env=self.env, **kwargs)["claude"]

    def test_a_launch_decision_lifts_a_limit_the_current_login_no_longer_has(self):
        now = self.OBSERVED + 600
        self.assertTrue(self.state(now).startswith("limited("))
        self.assertEqual(self.probes(), [])  # a reader that is not a launch decision never probes
        self.assertEqual(self.state(now, recheck=True), "ok")
        self.assertEqual(len(self.probes()), 1)
        self.assertEqual(self.probes()[0][:len(Q.PROBE_ARGS)], list(Q.PROBE_ARGS))
        # An all-model window is answered by Haiku, regardless of the worker profile.
        self.assertEqual(self.probes()[0][-2:], ["--model", "haiku"])
        self.assertEqual(self.state(now + 60), "ok")  # every reader honors the stored answer
        self.assertIsNone(Q.harness_hold(self.jobs, "claude", now=now + 60, env=self.env, recheck=True))
        self.assertEqual(len(self.probes()), 1)  # an answer that lifts the limit needs no new probe

    def test_an_answer_for_any_model_lifts_all_model_evidence_for_every_reader(self):
        now = self.OBSERVED + 600
        opus = {"claude": "claude-opus-4-6"}
        self.assertEqual(Q.usage_states(self.jobs, models=opus, now=now, env=self.env, recheck=True)["claude"], "ok")
        self.assertEqual(self.probes()[0][-2:], ["--model", "haiku"])  # not the Opus the launch asked for
        # A reader that asks about no model (gauges, Fleet, usage-check without a profile) sees the lift too.
        self.assertEqual(self.state(now + 60), "ok")
        self.native(now + 30, "att-five-hour", "five_hour", self.OBSERVED + 4 * 3600)
        self.assertIn("claude", Q.active_limits(self.jobs, now=now + 60, env=self.env))
        self.jobs.write_text("")
        self.native(self.OBSERVED, "att-five-hour", "five_hour", self.OBSERVED + 4 * 3600)
        self.assertEqual(Q.active_limits(self.jobs, now=now + 60, env=self.env), {})

    def test_a_limit_observed_after_a_passing_probe_is_not_hidden_behind_an_older_window(self):
        self.jobs.write_text("")
        self.native(self.OBSERVED, "att-week", "seven_day", self.OBSERVED + 5 * 86400)
        now = self.OBSERVED + 600
        self.assertEqual(Q.active_limits(self.jobs, now=now, env=self.env, recheck=True), {})
        # A five-hour death after the probe; the older weekly entry still has the later reset.
        self.native(self.OBSERVED + 2400, "att-five-hour", "five_hour", self.OBSERVED + 5 * 3600)
        for later in (self.OBSERVED + 2460, self.OBSERVED + 2 * 3600):
            self.assertIn("claude", Q.active_limits(self.jobs, now=later, env=self.env))
            self.assertTrue(self.state(later).startswith("limited("))

    def test_a_rejected_probe_holds_claude_until_its_own_reset_and_no_longer(self):
        self.env["FAKE_PROBE_MODE"] = "rejected"
        now = self.OBSERVED + 600
        self.assertEqual(self.state(now, recheck=True), f"limited({Q._iso(self.PROBE_RESET)})")
        self.assertEqual(len(self.probes()), 1)
        # Past the recorded reset the probe's later reset still holds Claude ...
        hold = Q.harness_hold(self.jobs, "claude", now=self.LIMIT_RESET + 60, env=self.env)
        self.assertEqual(hold["until_epoch"], self.PROBE_RESET)
        self.assertIn("claude", Q.active_limits(self.jobs, now=self.LIMIT_RESET + 60, env=self.env))
        # ... and once that passes it holds nothing, with no reset in the past.
        self.assertEqual(self.state(self.PROBE_RESET + 1), "ok")
        self.assertEqual(Q.active_limits(self.jobs, now=self.PROBE_RESET + 1, env=self.env), {})

    def test_a_rejection_on_a_shorter_window_does_not_end_a_longer_one(self):
        self.jobs.write_text("")
        week_reset = self.OBSERVED + 5 * 86400
        self.native(self.OBSERVED, "att-week", "seven_day", week_reset)
        self.env.update(FAKE_PROBE_MODE="rejected", FAKE_PROBE_WINDOW="five_hour",
                        FAKE_PROBE_RESET=str(self.OBSERVED + 3600))
        now = self.OBSERVED + 600
        self.assertEqual(Q.active_limits(self.jobs, now=now, env=self.env, recheck=True)["claude"]["reset_epoch"],
                         week_reset)
        self.assertIn("claude", Q.active_limits(self.jobs, now=self.OBSERVED + 3700, env=self.env))

    def test_a_rejection_whose_reset_passed_lifts_an_older_limit(self):
        self.env.update(FAKE_PROBE_MODE="rejected", FAKE_PROBE_RESET=str(self.OBSERVED + 900))
        now = self.OBSERVED + 600
        self.assertEqual(self.state(now, recheck=True), f"limited({Q._iso(self.OBSERVED + 900)})")
        self.assertEqual(self.state(self.OBSERVED + 960), "ok")  # recorded reset (3am) not reached yet

    def test_one_probe_per_interval_while_claude_is_held(self):
        self.env["FAKE_PROBE_MODE"] = "rejected"
        now = self.OBSERVED + 600
        self.state(now, recheck=True)
        self.state(now + Q.PROBE_INTERVAL - 1, recheck=True)
        self.assertEqual(len(self.probes()), 1)
        self.env["FAKE_PROBE_MODE"] = "allowed"
        self.assertEqual(self.state(now + Q.PROBE_INTERVAL, recheck=True), "ok")
        self.assertEqual(len(self.probes()), 2)

    def test_a_probe_that_says_nothing_about_quota_changes_nothing_and_waits_the_interval(self):
        now = self.OBSERVED + 600
        before = self.state(now)
        later = now + Q.PROBE_INTERVAL
        for step, mode in enumerate(("auth", "silent", "missing")):
            self.env["FAKE_PROBE_MODE"] = mode
            self.env["HEARTING_CAPACITY_PROBE_CLI"] = (
                str(self.home / "missing-cli") if mode == "missing" else str(self.fake))
            at = now + step * Q.PROBE_INTERVAL
            self.assertEqual(self.state(at, recheck=True), before)
            self.assertEqual(self.state(at + 60, recheck=True), before)  # not one minute later
            self.assertEqual(self.state(at + Q.PROBE_INTERVAL - 1, recheck=True), before)
            self.assertEqual(len(self.probes()), min(step + 1, 2))  # the missing CLI never starts
        self.assertEqual(len(self.probes()), 2)
        self.env.update(HEARTING_CAPACITY_PROBE_CLI=str(self.fake), FAKE_PROBE_MODE="allowed")
        self.assertEqual(self.state(later + 2 * Q.PROBE_INTERVAL, recheck=True), "ok")
        self.assertEqual(len(self.probes()), 3)

    def test_a_damaged_stored_answer_is_ignored(self):
        now = self.OBSERVED + 600
        before = self.state(now)
        scope = Q._login_scope(self.env)
        model = "haiku"
        for damaged in ({"at": now, "ok": False, "reset_epoch": "soon"}, {"at": now, "ok": False, "reset_epoch": 1e300},
                        {"at": now, "ok": False, "reset_epoch": now + 99999, "ok_at": "then"},
                        {"at": "now", "ok": True}, {"at": now, "ok": "yes"}, "not a dict"):
            with self.subTest(damaged=damaged):
                Q._probe_path(self.jobs).write_text(json.dumps(
                    {"schema": Q.PROBE_SCHEMA, "probes": {f"{scope}:{model}": damaged}}))
                self.assertEqual(self.state(now), before)
                self.assertTrue(Q.harness_hold(self.jobs, "claude", now=now, env=self.env)["label"])
        Q._probe_path(self.jobs).write_text("{not json")
        self.assertEqual(self.state(now), before)

    def test_the_cli_authenticates_so_a_missing_credential_file_does_not_stop_the_recheck(self):
        self.credentials.unlink()
        self.assertEqual(self.state(self.OBSERVED + 600, recheck=True), "ok")
        self.assertEqual(len(self.probes()), 1)

    def test_an_unresolved_login_is_rechecked_as_unscoped_and_never_borrows_an_account(self):
        now = self.OBSERVED + 600
        (self.home / ".claude.json").write_text("{}")  # no account to name
        self.assertIsNone(Q.launch_scope("claude", self.env).get("quota_scope"))
        self.assertTrue(self.state(now).startswith("limited("))
        self.assertEqual(self.state(now, recheck=True), "ok")  # the unscoped legacy marker is lifted
        self.assertEqual(len(self.probes()), 1)
        self.assertEqual(self.state(now + 60), "ok")
        # The answer belongs to "unscoped": once an account resolves it is not that account's.
        scoped = json.loads(Q._probe_path(self.jobs).read_text())["probes"]
        self.assertEqual(list(scoped), [f"{Q.UNSCOPED}:haiku"])
        (self.home / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"accountUuid": "account-b", "organizationUuid": "org-a"}}))
        self.assertTrue(self.state(now + 120).startswith("limited("))
        # A limit bound to an account is neither read away nor borrowed once that account is not resolved.
        self.jobs.write_text("")
        self.native(now - 60, "att-scoped", "five_hour", self.OBSERVED + 4 * 3600)
        self.env["FAKE_PROBE_MODE"] = "rejected"
        self.assertIn("claude", Q.active_limits(self.jobs, now=now + 120, env=self.env, recheck=True))
        probed = len(self.probes())
        (self.home / ".claude.json").write_text("{}")
        self.assertEqual(Q.active_limits(self.jobs, now=now + 700, env=self.env, recheck=True), {})
        self.assertEqual(len(self.probes()), probed)  # nothing unscoped holds: no probe either

    def test_no_probe_for_api_or_provider_configuration(self):
        self.env["ANTHROPIC_API_KEY"] = "key-value"
        now = self.OBSERVED + 600
        self.assertTrue(self.state(now, recheck=True).startswith("limited("))
        self.assertEqual(self.probes(), [])

    def test_a_weekly_hold_lifted_by_a_probe_is_not_revived_by_a_later_five_hour_rejection(self):
        self.jobs.write_text("")
        week_reset, five_reset = self.OBSERVED + 5 * 86400, self.OBSERVED + 3 * 3600
        self.native(self.OBSERVED, "att-week", "seven_day", week_reset)
        now = self.OBSERVED + 600
        self.assertEqual(Q.active_limits(self.jobs, now=now, env=self.env, recheck=True), {})  # success lifts the week
        self.native(now + 300, "att-five-hour", "five_hour", five_reset)
        later = now + Q.PROBE_INTERVAL
        self.env.update(FAKE_PROBE_MODE="rejected", FAKE_PROBE_WINDOW="five_hour", FAKE_PROBE_RESET=str(five_reset))
        for reader in (lambda: Q.active_limits(self.jobs, now=later, env=self.env, recheck=True)["claude"]["reset_epoch"],
                       lambda: Q.active_limits(self.jobs, now=later + 1, env=self.env)["claude"]["reset_epoch"],
                       lambda: Q.harness_hold(self.jobs, "claude", now=later + 1, env=self.env)["until_epoch"]):
            self.assertEqual(reader(), five_reset)  # never the week the first probe lifted
        stored = json.loads(Q._probe_path(self.jobs).read_text())["probes"]
        self.assertIn(now, [a["at"] for a in stored[f"{Q._login_scope(self.env)}:haiku"]["answers"] if a["ok"]])  # the answer kept beside the rejection
        self.assertEqual([argv[-1] for argv in self.probes()], ["haiku", "haiku"])
        # The rejection's reset passes: nothing holds, and the old week does not come back.
        self.assertEqual(self.state(five_reset + 1), "ok")
        self.assertEqual(Q.active_limits(self.jobs, now=five_reset + 1, env=self.env), {})

    def test_every_window_that_holds_is_answered_in_turn(self):
        self.jobs.write_text("")
        self.native(self.OBSERVED, "att-week", "seven_day", self.OBSERVED + 5 * 86400)
        self.native(self.OBSERVED, "att-opus-week", "seven_day_opus", self.OBSERVED + 86400)
        opus, now = {"claude": "claude-opus-4-6"}, self.OBSERVED + 600
        self.assertIn("claude", Q.active_limits(self.jobs, models=opus, now=now, env=self.env, recheck=True))
        self.assertEqual(self.probes()[-1][-2:], ["--model", "haiku"])  # the all-model window first
        self.assertEqual(Q.active_limits(self.jobs, models=opus, now=now + 1, env=self.env, recheck=True), {})
        self.assertEqual([argv[-1] for argv in self.probes()], ["haiku", "opus"])

    def test_a_limit_observed_after_a_passing_probe_holds_again(self):
        now = self.OBSERVED + 600
        self.assertEqual(self.state(now, recheck=True), "ok")
        self.marker(now + 60, "att-dead-2")
        self.assertTrue(self.state(now + 120, recheck=True).startswith("limited("))
        self.assertEqual(len(self.probes()), 1)  # still inside the interval
        self.assertEqual(self.state(now + Q.PROBE_INTERVAL, recheck=True), "ok")
        self.assertEqual(len(self.probes()), 2)

    def test_answers_belong_to_the_login_that_got_them(self):
        now = self.OBSERVED + 600
        self.assertEqual(self.state(now, recheck=True), "ok")
        (self.home / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"accountUuid": "account-c", "organizationUuid": "org-a"}}))
        self.assertTrue(self.state(now + 60).startswith("limited("))
        self.env["FAKE_PROBE_MODE"] = "rejected"
        self.assertEqual(self.state(now + 60, recheck=True), f"limited({Q._iso(self.PROBE_RESET)})")
        self.assertEqual(len(self.probes()), 2)

    def test_another_harness_decision_never_waits_on_a_claude_probe(self):
        now = self.OBSERVED + 600
        self.assertIsNone(Q.harness_hold(self.jobs, "codex", now=now, env=self.env, recheck=True))
        self.assertEqual(self.probes(), [])

    def test_a_probe_already_running_elsewhere_is_not_repeated(self):
        import fcntl
        lock = os.open(f"{Q._probe_path(self.jobs)}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        self.assertTrue(self.state(self.OBSERVED + 600, recheck=True).startswith("limited("))
        self.assertEqual(self.probes(), [])

    def test_a_model_scoped_limit_is_lifted_only_by_that_model(self):
        self.jobs.write_text("")
        self.native(self.OBSERVED, "att-opus-week", "seven_day_opus", self.OBSERVED + 86400)
        now, opus, sonnet = self.OBSERVED + 600, {"claude": "claude-opus-4-6"}, {"claude": "claude-sonnet-4-6"}
        self.assertEqual(Q.active_limits(self.jobs, models=sonnet, now=now, env=self.env, recheck=True), {})
        self.assertEqual(self.probes(), [])  # the Sonnet family was never limited
        self.assertIn("claude", Q.active_limits(self.jobs, models=opus, now=now, env=self.env))
        self.assertNotIn("claude", Q.active_limits(self.jobs, models=opus, now=now, env=self.env, recheck=True))
        self.assertEqual(self.probes()[0][-2:], ["--model", "opus"])

    def test_a_rejection_on_one_family_does_not_hold_another(self):
        self.env.update(FAKE_PROBE_MODE="rejected", FAKE_PROBE_WINDOW="seven_day_opus")
        now, opus = self.OBSERVED + 600, {"claude": "claude-opus-4-6"}
        self.assertTrue(Q.usage_states(self.jobs, models=opus, now=now, env=self.env,
                                       recheck=True)["claude"].startswith("limited("))
        sonnet = {"claude": "claude-sonnet-4-6"}
        later = self.LIMIT_RESET + 60  # the legacy marker no longer holds anyone
        self.assertEqual(Q.usage_states(self.jobs, models=sonnet, now=later, env=self.env)["claude"], "ok")
        self.assertTrue(Q.usage_states(self.jobs, models=opus, now=later, env=self.env)["claude"].startswith("limited("))

    def test_an_answer_on_one_family_does_not_open_another_familys_window(self):
        self.jobs.write_text("")
        opus_reset = self.OBSERVED + 4 * 86400
        self.native(self.OBSERVED, "att-opus-week", "seven_day_opus", opus_reset)
        now, opus, sonnet = self.OBSERVED + 600, {"claude": "claude-opus-4-6"}, {"claude": "claude-sonnet-4-6"}
        # Sonnet is not held by the Opus week: nothing to probe, nothing held.
        self.assertEqual(Q.usage_states(self.jobs, models=sonnet, now=now, env=self.env, recheck=True)["claude"], "ok")
        self.assertEqual(self.probes(), [])
        # An answer through another family's key leaves the Opus week held.
        Q._write_probe(self.jobs, f"{Q._login_scope(self.env)}:sonnet",
                       {"at": now, "ok": True, "reset_epoch": None, "model": "claude-sonnet-4-6",
                        "model_scope": "all", "tried": now}, now)
        self.assertEqual(Q.usage_states(self.jobs, models=opus, now=now + 60, env=self.env)["claude"],
                         f"limited({Q._iso(opus_reset)})")
        self.assertEqual(Q.active_limits(self.jobs, models=opus, now=now + 60, env=self.env)["claude"]["reset_epoch"],
                         opus_reset)
        self.assertEqual(self.state(now + 60), "ok")  # a reader with no model is held by neither

    def test_a_429_that_is_not_a_quota_rejection_is_no_answer(self):
        self.env["FAKE_PROBE_MODE"] = "overloaded"
        now = self.OBSERVED + 600
        before = self.state(now)
        self.assertEqual(self.state(now, recheck=True), before)
        self.assertEqual(len(self.probes()), 1)
        self.assertEqual(self.state(self.LIMIT_RESET + 60), "ok")  # it never holds Claude past the marker

    def test_a_probe_that_times_out_waits_the_full_interval(self):
        self.env["FAKE_PROBE_MODE"] = "hang"
        now = self.OBSERVED + 600
        before = self.state(now)
        with mock.patch.object(Q, "PROBE_TIMEOUT", 0.5):
            self.assertEqual(self.state(now, recheck=True), before)
            self.env["FAKE_PROBE_MODE"] = "allowed"
            self.assertEqual(self.state(now + Q.PROBE_INTERVAL - 1, recheck=True), before)
            self.assertEqual(self.state(now + Q.PROBE_INTERVAL, recheck=True), "ok")
        self.assertEqual(len(self.probes()), 2)

    def test_all_model_probe_ignores_the_configured_mini_model(self):
        with mock.patch.object(Q, "_limit_models", return_value={"claude": "claude-opus-4-6"}):
            self.assertEqual(self.state(self.OBSERVED + 600, recheck=True), "ok")
        self.assertEqual(self.probes()[0][-2:], ["--model", "haiku"])

    def test_family_windows_select_the_limited_family_alias(self):
        for family in ("opus", "sonnet"):
            with self.subTest(family=family):
                self.jobs.write_text("")
                Q._probe_path(self.jobs).unlink(missing_ok=True)
                model = f"claude-{family}-4-6"
                self.native(self.OBSERVED, f"att-{family}", f"seven_day_{family}", self.PROBE_RESET, model=model)
                self.assertEqual(Q.active_limits(self.jobs, models={"claude": model},
                                                now=self.OBSERVED + 600, env=self.env, recheck=True), {})
                self.assertEqual(self.probes()[-1][-2:], ["--model", family])

    def test_auto_resume_needs_the_source_model_to_clear_a_family_hold(self):
        import capacity_auto_resume as resume
        self.jobs.write_text("")
        now = self.OBSERVED + 600
        self.native(self.OBSERVED, "att-held-opus", "seven_day_opus", self.PROBE_RESET)
        record = {"cause": "capacity", "harness": "claude", "jobs": str(self.jobs), "source_attempt_id": ""}
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(Q.time, "time", return_value=now):
            self.assertEqual(Q.usage_states(self.jobs)["claude"], "ok")  # incomplete view for a paused model
            self.assertFalse(resume._cleared(record))
            self.assertFalse(resume._cleared({**record, "source_attempt_id": "att-missing"}))
            self.assertEqual(self.probes(), [])
            self.assertTrue(resume._cleared({**record, "source_attempt_id": "att-held-opus"}))
        self.assertEqual([a[-1] for a in self.probes()], ["opus"])

    def test_later_rejection_does_not_discard_another_windows_rejection(self):
        now = self.OBSERVED + 600
        week_reset, five_reset = now + 5 * 86400, now + 4000
        self.env.update(FAKE_PROBE_MODE="rejected", FAKE_PROBE_WINDOW="seven_day",
                        FAKE_PROBE_RESET=str(week_reset))
        self.assertEqual(self.state(now, recheck=True), f"limited({Q._iso(week_reset)})")
        self.env.update(FAKE_PROBE_WINDOW="five_hour", FAKE_PROBE_RESET=str(five_reset))
        self.assertEqual(self.state(now + 600, recheck=True), f"limited({Q._iso(week_reset)})")
        self.assertEqual(self.state(five_reset + 1), f"limited({Q._iso(week_reset)})")

    def test_positive_event_only_responses_clear_the_limit(self):
        for mode in ("event-allowed", "event-allowed_warning", "event-overage"):
            with self.subTest(mode=mode):
                Q._probe_path(self.jobs).unlink(missing_ok=True)
                self.env["FAKE_PROBE_MODE"] = mode
                self.assertEqual(self.state(self.OBSERVED + 600, recheck=True), "ok")

    def test_failed_attempt_reservation_does_not_launch(self):
        with mock.patch.object(Q, "_write_probe", return_value=False):
            self.assertTrue(self.state(self.OBSERVED + 600, recheck=True).startswith("limited("))
        self.assertEqual(self.probes(), [])

    def test_interrupted_attempt_still_spends_the_interval(self):
        now = self.OBSERVED + 600
        with mock.patch.object(Q, "_probe_once", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.state(now, recheck=True)
        self.assertTrue(self.state(now + 599, recheck=True).startswith("limited("))
        self.assertEqual(self.probes(), [])
        self.assertEqual(self.state(now + 600, recheck=True), "ok")
        self.assertEqual(len(self.probes()), 1)

    def test_no_probe_without_serialization(self):
        with mock.patch.object(Q, "fcntl", None):
            self.assertTrue(self.state(self.OBSERVED + 600, recheck=True).startswith("limited("))
        self.assertEqual(self.probes(), [])

    def test_family_aliases_share_the_interval_and_exact_models_stay_exact(self):
        self.jobs.write_text("")
        now = self.OBSERVED + 600
        old, new = "claude-opus-4-5", "claude-opus-4-6"
        self.native(self.OBSERVED, "att-old-model", "seven_day_overage_included", now + 5000, model=old)
        self.env["FAKE_PROBE_MODE"] = "auth"
        self.assertIn("claude", Q.active_limits(self.jobs, models={"claude": old}, now=now, env=self.env, recheck=True))
        self.native(now + 1, "att-new-model", "seven_day_overage_included", now + 5000, model=new)
        self.assertIn("claude", Q.active_limits(self.jobs, models={"claude": new}, now=now + 599, env=self.env, recheck=True))
        self.assertEqual([a[-1] for a in self.probes()], [old])
        self.env["FAKE_PROBE_MODE"] = "allowed"
        self.assertEqual(Q.active_limits(self.jobs, models={"claude": new}, now=now + 600, env=self.env, recheck=True), {})
        self.assertEqual([a[-1] for a in self.probes()], [old, new])
        self.assertIn("claude", Q.active_limits(self.jobs, models={"claude": old}, now=now + 601, env=self.env))

    def test_later_same_family_success_preserves_the_earlier_exact_model_success(self):
        self.jobs.write_text("")
        now = self.OBSERVED + 600
        old, new = "claude-opus-4-5", "claude-opus-4-6"
        for model, attempt in ((old, "att-old-model"), (new, "att-new-model")):
            self.native(self.OBSERVED, attempt, "seven_day_overage_included", now + 5000, model=model)
        self.assertIn("claude", Q.active_limits(self.jobs, models={"claude": old}, now=now, env=self.env))
        self.assertEqual(Q.active_limits(self.jobs, models={"claude": old}, now=now, env=self.env, recheck=True), {})
        self.assertEqual(Q.active_limits(self.jobs, models={"claude": new}, now=now + 600, env=self.env, recheck=True), {})
        self.assertEqual(Q.active_limits(self.jobs, models={"claude": old}, now=now + 601, env=self.env), {})
        self.assertEqual([a[-1] for a in self.probes()], [old, new])

    def test_success_for_a_long_legacy_marker_survives_another_accounts_later_check(self):
        self.jobs.write_text("")
        self.marker(self.OBSERVED, "att-long", "resets 2099-01-01 00:00 (UTC)")
        now = self.OBSERVED + 600
        self.assertEqual(self.state(now, recheck=True), "ok")
        identity = self.home / ".claude.json"
        original = identity.read_text()
        identity.write_text(json.dumps({"oauthAccount": {"accountUuid": "account-c", "organizationUuid": "org-a"}}))
        self.assertEqual(self.state(now + 9 * 86400, recheck=True), "ok")
        identity.write_text(original)
        self.assertEqual(self.state(now + 9 * 86400 + 1), "ok")

    def test_usage_check_recheck_flag_is_the_launch_decision(self):
        def states(*flags):
            done = subprocess.run([str(ROOT / "utilities/usage-check.sh"), "--jobs", str(self.jobs), *flags],
                                  env=self.env, text=True, capture_output=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            return dict(line.split() for line in done.stdout.splitlines())
        self.jobs.write_text("")
        self.marker(time.time() - 60, "att-recent", reset_text="resets 2099-01-01 00:00 (UTC)")
        self.assertTrue(states()["claude"].startswith("limited("))
        self.assertEqual(self.probes(), [])
        self.assertEqual(states("--recheck")["claude"], "ok")
        self.assertEqual(len(self.probes()), 1)


class SharedGaugeHoldTests(unittest.TestCase):
    """Backlog E: a cached 100% window is a hard hold, read by the one capacity judge."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.jobs = self.home / "jobs.log"
        self.jobs.write_text("")
        self.usage = self.home / "usage"
        self.now = int(time.time())
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "AGENT_HOME": str(ROOT),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "FLEET_USAGE_STATE_DIR": str(self.usage),
            "HARNESS_CAPACITY_SCORES": "",
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        if str(ROOT / "tools") not in sys.path:
            sys.path.insert(0, str(ROOT / "tools"))

    def record(self, harness, payload, age=5):
        from fleet.collectors import usage_cache
        usage_cache.record(harness, payload, now=self.now - age)

    def readers(self):
        states = Q.usage_states(self.jobs, now=self.now)
        hold = Q.harness_hold(self.jobs, "codex", now=self.now)
        limits = Q.active_limits(self.jobs, now=self.now)
        return states, hold, limits

    def test_codex_full_weekly_window_holds_all_three_readers(self):
        reset = self.now + 86400
        self.record("codex", {"windows": [["7d", 100, reset]]})
        states, hold, limits = self.readers()
        self.assertEqual(states["codex"], f"limited({Q._iso(reset)})")
        self.assertEqual(hold, {"until_epoch": reset, "label": Q._iso(reset)})
        self.assertEqual(limits["codex"]["reset_epoch"], reset)
        self.assertEqual(limits["codex"]["scope_authority"], "gauge")
        self.assertEqual(limits["codex"]["model_scope"], "all")

    def test_credit_matrix_reaches_usage_hold_and_active_limit_readers(self):
        reset = self.now + 86400
        cases = [({"credits": {"has_credits": True}, "spend_control_reached": None}, True),
                 ({"credits": {"has_credits": True}}, True),
                 ({"credits": {"has_credits": False, "unlimited": True}, "spend_control_reached": False}, True),
                 ({"credits": {"has_credits": False, "unlimited": False}}, False),
                 ({"credits": {"has_credits": True}, "spend_control_reached": True}, False),
                 ({"credits": {"has_credits": True}, "spend_control_reached": "false"}, False),
                 ({"credits": {"has_credits": "true", "unlimited": 1}}, False),
                 ({"credits": {"balance": "56446.1701575000"}}, False),
                 ({"credits": None}, False), ({}, False)]
        for metadata, usable in cases:
            with self.subTest(metadata=metadata):
                self.record("codex", {"windows": [["7d", 100, reset]], **metadata})
                states, hold, limits = self.readers()
                if usable:
                    self.assertEqual((states["codex"], hold, limits.get("codex")), ("ok", None, None))
                else:
                    self.assertEqual(states["codex"], f"limited({Q._iso(reset)})")
                    self.assertEqual(hold["until_epoch"], reset)
                    self.assertEqual(limits["codex"]["scope_authority"], "gauge")

    def test_ninety_nine_past_reset_and_stale_cache_stay_ok(self):
        future = self.now + 86400
        self.record("codex", {"windows": [["7d", 99, future]]})
        states, hold, limits = self.readers()
        self.assertEqual(states["codex"], "ok")
        self.assertIsNone(hold)
        self.assertNotIn("codex", limits)
        self.record("codex", {"windows": [["7d", 100, self.now - 60]]})
        states, hold, limits = self.readers()
        self.assertEqual((states["codex"], hold, limits.get("codex")), ("ok", None, None))
        self.record("codex", {"windows": [["7d", 100, future]]}, age=4000)
        states, hold, limits = self.readers()
        self.assertEqual((states["codex"], hold, limits.get("codex")), ("ok", None, None))

    def test_credits_do_not_override_model_eligibility(self):
        import model_profile
        import opencode_runtime
        self.record("codex", {"windows": [["7d", 100, self.now + 86400]],
                              "credits": {"has_credits": True}})
        with mock.patch.object(model_profile, "resolve_runtime_profile", return_value=(
                {"model": "opencode-go/model", "budget": "high"}, object())), \
                mock.patch.object(opencode_runtime, "automatic_candidates", return_value=[
                    {"model": "opencode-go/model", "state": "unavailable"}]):
            states = Q.usage_states(self.jobs, profile="deep", now=self.now)
        self.assertEqual(states["codex"], "ok")
        self.assertTrue(states["opencode"].startswith("limited(provider-model-unavailable:"), states)

    def test_opencode_full_rl_window_holds_all_three_readers(self):
        reset = self.now + 86400
        self.record("opencode", {"rl_windows": [["mo", 100, reset]]})
        states = Q.usage_states(self.jobs, now=self.now)
        hold = Q.harness_hold(self.jobs, "opencode", now=self.now)
        limits = Q.active_limits(self.jobs, now=self.now)
        self.assertEqual(states["opencode"], f"limited({Q._iso(reset)})")
        self.assertEqual(hold, {"until_epoch": reset, "label": Q._iso(reset)})
        self.assertEqual(limits["opencode"]["reset_epoch"], reset)

    def test_claude_cache_windows_stay_native_only(self):
        self.record("claude", {"windows": [["7d", 100, self.now + 86400]]})
        states = Q.usage_states(self.jobs, now=self.now)
        self.assertEqual(states["claude"], "ok")
        self.assertIsNone(Q.harness_hold(self.jobs, "claude", now=self.now))
        self.assertNotIn("claude", Q.active_limits(self.jobs, now=self.now))

    def test_manual_score_replaces_the_gauge(self):
        self.record("codex", {"windows": [["7d", 100, self.now + 86400]]})
        with mock.patch.dict(os.environ, {"HARNESS_CAPACITY_SCORES": "codex:0"}):
            states = Q.usage_states(self.jobs, now=self.now)
            self.assertEqual(states["codex"], "ok")
            self.assertIsNone(Q.harness_hold(self.jobs, "codex", now=self.now))

    def test_later_legacy_marker_reset_wins_over_the_gauge(self):
        stamp = datetime.fromtimestamp(self.now, timezone.utc).isoformat().replace("+00:00", "Z")
        self.jobs.write_text(
            f"{stamp}\tdone\t/r\t/w\tjob\tattempt_id=att-gauge,harness=codex,"
            "note=dead-session-limit,reset=2099-01-01T00:00:00Z\n")
        self.record("codex", {"windows": [["7d", 100, self.now + 86400]]})
        self.assertEqual(Q.usage_states(self.jobs, now=self.now)["codex"],
                         "limited(2099-01-01T00:00:00Z)")
        self.assertEqual(Q.harness_hold(self.jobs, "codex", now=self.now)["until_epoch"],
                         Q._text_reset_epoch("2099-01-01T00:00:00Z", self.now, os.environ))
        self.record("codex", {"windows": [["7d", 100, self.now + 86400]],
                              "credits": {"has_credits": True}})
        states, hold, limits = self.readers()
        self.assertEqual(states["codex"], "limited(2099-01-01T00:00:00Z)")
        self.assertEqual(hold["until_epoch"], Q._text_reset_epoch("2099-01-01T00:00:00Z", self.now, os.environ))
        self.assertNotIn("codex", limits)  # legacy hold survives independently of active gauge items

    def test_later_gauge_reset_wins_over_the_legacy_marker(self):
        stamp = datetime.fromtimestamp(self.now, timezone.utc).isoformat().replace("+00:00", "Z")
        marker_reset = Q._iso(self.now + 3600)
        self.jobs.write_text(
            f"{stamp}\tdone\t/r\t/w\tjob\tattempt_id=att-gauge,harness=codex,"
            f"note=dead-session-limit,reset={marker_reset}\n")
        gauge_reset = self.now + 86400
        self.record("codex", {"windows": [["7d", 100, gauge_reset]]})
        self.assertEqual(Q.usage_states(self.jobs, now=self.now)["codex"],
                         f"limited({Q._iso(gauge_reset)})")
        self.assertEqual(Q.harness_hold(self.jobs, "codex", now=self.now)["until_epoch"], gauge_reset)

    def test_broken_capacity_reader_keeps_todays_answer(self):
        self.record("codex", {"windows": [["7d", 100, self.now + 86400]]})
        with mock.patch.object(Q, "_capacity_reader", side_effect=RuntimeError("missing")):
            states = Q.usage_states(self.jobs, now=self.now)
            self.assertEqual(states["codex"], "ok")
            self.assertIsNone(Q.harness_hold(self.jobs, "codex", now=self.now))
            self.assertNotIn("codex", Q.active_limits(self.jobs, now=self.now))


if __name__ == "__main__":
    unittest.main()
