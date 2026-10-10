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


if __name__ == "__main__":
    unittest.main()
