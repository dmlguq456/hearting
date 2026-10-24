#!/usr/bin/env python3
"""Token self-regulation Phase 0-2 regression tests."""

import json
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet.collectors import claude, codex, opencode  # noqa: E402
from fleet.model import Session  # noqa: E402
from fleet.token_budget import (  # noqa: E402
    DIRECTIVE_TEXTS,
    codex_context_used_pct,
    context_policy_snapshot,
    context_policy_used_pct,
    find_codex_rollout,
    parse_codex_token_count,
    policy_band,
    telemetry_from_codex_session,
)
from fleet.token_accounting import (  # noqa: E402
    MAX_DIRECTORY_BYTES,
    MAX_FILE_BYTES,
    MAX_FILES,
    ZERO_REASONS,
    accounting_dir,
    accounting_path,
    empty_aggregate,
    read_accounting,
    record_accounting,
    reduce_accounting,
    session_digest,
)


def _repo_root():
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "core" / "CORE.md").is_file():
            return candidate
    raise RuntimeError("repository root not found")


ROOT = _repo_root()
CLI = ROOT / "utilities" / "token-budget.py"
SID = "12345678-1234-1234-1234-123456789abc"


def token_line(active=82_000, window=112_000, total=150_000, timestamp=None):
    event = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": active - 1_000,
                    "cached_input_tokens": 100,
                    "output_tokens": 900,
                    "reasoning_output_tokens": 100,
                    "total_tokens": active,
                },
                "total_token_usage": {
                    "input_tokens": 120_000,
                    "cached_input_tokens": 70_000,
                    "output_tokens": 20_000,
                    "reasoning_output_tokens": 10_000,
                    "total_tokens": total,
                },
                "model_context_window": window,
            },
        },
    }
    if timestamp is not None:
        event["timestamp"] = timestamp
    return json.dumps(event)


class PureTelemetryTest(unittest.TestCase):

    def test_display_percent_uses_native_trigger_and_keeps_raw_capacity_separate(self):
        policy = context_policy_snapshot(
            source="claude-autocompact-state", runtime_version="2.1.299",
            configured_request_tokens=1_000_000, effective_window_tokens=900_000,
            applied_trigger_tokens=800_000, enabled=True, enforced=True,
            session_id=SID)
        self.assertEqual(context_policy_used_pct(400_000, policy), 50)
        self.assertEqual(policy["effective_window_tokens"], 900_000)
        self.assertEqual(policy["configured_request_tokens"], 1_000_000)
        self.assertEqual(context_policy_used_pct(0, policy), 0)
        self.assertEqual(context_policy_used_pct(799_999, policy), 99)
        self.assertEqual(context_policy_used_pct(800_000, policy), 100)
        self.assertEqual(context_policy_used_pct(900_000, policy), 100)

    def test_disabled_unknown_and_zero_trigger_never_invent_a_percentage(self):
        disabled = context_policy_snapshot(
            source="native", disabled=True, applied_trigger_tokens=0, session_id=SID)
        unknown = context_policy_snapshot(
            source="native", applied_trigger_tokens=800_000, session_id=SID)
        zero_trigger = context_policy_snapshot(
            source="native", applied_trigger_tokens=0,
            enabled=True, enforced=True, session_id=SID)
        self.assertEqual((disabled["state"], context_policy_used_pct(0, disabled)),
                         ("disabled", None))
        self.assertEqual((unknown["state"], context_policy_used_pct(1, unknown)),
                         ("unknown", None))
        self.assertEqual((zero_trigger["state"], context_policy_used_pct(0, zero_trigger)),
                         ("applied", None))

    def test_codex_formula_boundaries(self):
        self.assertEqual(codex_context_used_pct(81_000, 112_000), 69)
        self.assertEqual(codex_context_used_pct(82_000, 112_000), 70)
        self.assertEqual(codex_context_used_pct(96_000, 112_000), 84)
        self.assertEqual(codex_context_used_pct(97_000, 112_000), 85)
        self.assertEqual([policy_band(value) for value in (69, 70, 84, 85)],
                         ["normal", "tight", "tight", "critical"])

    def test_codex_last_and_total_are_separate(self):
        telemetry = parse_codex_token_count(token_line(), session_id=SID)
        self.assertEqual(telemetry.status, "observed")
        self.assertEqual(telemetry.active_context_tokens, 82_000)
        self.assertEqual(telemetry.context_window_tokens, 112_000)
        self.assertEqual(telemetry.context_used_pct, 70)
        self.assertEqual(telemetry.session_input_tokens, 120_000)
        self.assertEqual(telemetry.session_cached_input_tokens, 70_000)
        self.assertEqual(telemetry.session_output_tokens, 20_000)
        self.assertEqual(telemetry.session_reasoning_output_tokens, 10_000)
        self.assertEqual(telemetry.session_total_tokens, 150_000)

    def test_native_incomplete_keeps_context_baseline_and_advances_cumulative_usage(self):
        event = json.loads(token_line(active=12_250, window=258_400, total=13_490))
        info = event["payload"]["info"]
        info["last_token_usage"] = {
            "input_tokens": 12_000, "cached_input_tokens": 3_000,
            "output_tokens": 250, "reasoning_output_tokens": 30,
            "total_tokens": 12_250,
        }
        info["total_token_usage"] = {
            "input_tokens": 13_200, "cached_input_tokens": 3_400,
            "output_tokens": 290, "reasoning_output_tokens": 35,
            "total_tokens": 13_490,
        }
        telemetry = parse_codex_token_count(json.dumps(event), session_id=SID)
        self.assertEqual((telemetry.status, telemetry.active_context_tokens), ("observed", 12_250))
        self.assertEqual(telemetry.context_used_pct, 0)
        self.assertEqual((telemetry.session_input_tokens, telemetry.session_total_tokens),
                         (13_200, 13_490))

    def test_first_native_incomplete_keeps_zero_context_separate_from_recorded_usage(self):
        event = json.loads(token_line(active=0, window=258_400, total=1_240))
        info = event["payload"]["info"]
        info["last_token_usage"] = {
            "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            "reasoning_output_tokens": 0, "total_tokens": 0,
        }
        info["total_token_usage"] = {
            "input_tokens": 1_200, "cached_input_tokens": 400, "output_tokens": 40,
            "reasoning_output_tokens": 5, "total_tokens": 1_240,
        }
        telemetry = parse_codex_token_count(json.dumps(event), session_id=SID)
        self.assertEqual((telemetry.status, telemetry.active_context_tokens,
                          telemetry.session_input_tokens, telemetry.session_total_tokens),
                         ("observed", 0, 1_200, 1_240))
        self.assertEqual(telemetry.context_used_pct, 0)

    def test_native_incomplete_with_null_info_is_unknown(self):
        event = json.loads(token_line())
        event["payload"]["info"] = None
        telemetry = parse_codex_token_count(json.dumps(event), session_id=SID)
        self.assertEqual((telemetry.status, telemetry.reason),
                         ("unknown", "missing-token-info"))

    def test_truthy_non_object_usage_is_unknown_instead_of_raising(self):
        for field in ("last_token_usage", "total_token_usage"):
            for malformed in ("not-an-object", [1], 7):
                with self.subTest(field=field, malformed=malformed):
                    event = json.loads(token_line())
                    event["payload"]["info"][field] = malformed
                    telemetry = parse_codex_token_count(json.dumps(event), session_id=SID)
                    self.assertEqual((telemetry.status, telemetry.reason),
                                     ("unknown", "malformed-token-usage"))

    def test_malformed_and_missing_are_unknown(self):
        self.assertEqual(parse_codex_token_count("not-json").status, "unknown")
        missing = parse_codex_token_count(json.dumps({"payload": {}}))
        self.assertEqual((missing.status, missing.reason),
                         ("unknown", "not-token-count"))

    def test_nonfinite_and_boolean_counters_are_unknown(self):
        for value in (float("nan"), float("inf"), True):
            with self.subTest(value=value):
                event = json.loads(token_line())
                event["payload"]["info"]["last_token_usage"]["total_tokens"] = value
                parsed = parse_codex_token_count(json.dumps(event), session_id=SID)
                self.assertIsNone(parsed.active_context_tokens)
                self.assertIsNone(parsed.context_used_pct)

    def test_exact_session_lookup_rejects_ambiguous_and_stale(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home) / "sessions"
            first = root / "2026" / "07" / "13" / f"rollout-a-{SID}.jsonl"
            first.parent.mkdir(parents=True)
            first.write_text(
                token_line() + "\n" +
                json.dumps({"payload": {"type": "user_message", "text": "token_count"}}) + "\n",
                encoding="utf-8",
            )
            path, reason = find_codex_rollout(Path(home), SID)
            self.assertEqual((path, reason), (first, "ok"))

            os.utime(first, (time.time() - 20, time.time() - 20))
            stale = telemetry_from_codex_session(
                SID, codex_home=home, max_age_seconds=10, now=time.time())
            self.assertEqual((stale.status, stale.reason), ("unknown", "stale-signal"))

            second = root / "2026" / "07" / "14" / f"rollout-b-{SID}.jsonl"
            second.parent.mkdir(parents=True)
            second.write_text(token_line() + "\n", encoding="utf-8")
            self.assertEqual(find_codex_rollout(Path(home), SID)[1], "ambiguous-session")

    def test_staleness_uses_event_timestamp_before_fresh_file_mtime(self):
        with tempfile.TemporaryDirectory() as home:
            rollout = (Path(home) / "sessions" / "2026" / "07" / "13" /
                       f"rollout-a-{SID}.jsonl")
            rollout.parent.mkdir(parents=True)
            rollout.write_text(
                token_line(timestamp="2020-01-01T00:00:00Z") + "\n",
                encoding="utf-8",
            )
            stale = telemetry_from_codex_session(
                SID, codex_home=home, max_age_seconds=10, now=1_577_836_900)
            self.assertEqual((stale.status, stale.reason, stale.signal_age_seconds),
                             ("unknown", "stale-signal", 100))


class CollectorSemanticsTest(unittest.TestCase):

    def test_codex_collector_preserves_legacy_and_explicit_fields(self):
        sess = Session(harness="codex", pid=1, session_id=SID)
        codex._apply_token_count(sess, token_line())
        self.assertEqual(sess.tokens, 82_000)
        self.assertEqual(sess.active_context_tokens, 82_000)
        self.assertEqual(sess.session_total_tokens, 150_000)

    def test_claude_collector_maps_available_fields_only(self):
        sess = Session(harness="claude", pid=1)
        claude._apply_statusline(sess, {"context_window": {
            "used_percentage": 50,
            "context_window_size": 200_000,
            "current_usage": {
                "input_tokens": 10_000,
                "cache_creation_input_tokens": 2_000,
                "cache_read_input_tokens": 3_000,
            },
            "total_input_tokens": 90_000,
            "total_output_tokens": 10_000,
        }})
        self.assertEqual(sess.active_context_tokens, 15_000)
        self.assertEqual(sess.context_window_tokens, 200_000)
        self.assertIsNone(sess.context_display_window_tokens)
        self.assertIsNone(sess.ctx_pct)
        self.assertEqual(sess.context_policy["state"], "unknown")
        self.assertEqual(sess.context_policy["raw_percentage"], 50)
        self.assertEqual(sess.session_total_tokens, 100_000)
        self.assertIsNone(sess.session_cached_input_tokens)

    def test_opencode_collector_separates_active_and_cumulative(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "opencode.db")
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE session (id, slug, agent, model, cost, tokens_input, tokens_output, tokens_reasoning, time_updated, parent_id, directory, title)")
            con.execute("CREATE TABLE message (session_id, time_updated, data)")
            con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                "oc-1", "slug", "agent", json.dumps({"id": "model-x"}), 0.1,
                80_000, 10_000, 5_000, 1000, None, "/repo", "title"))
            con.execute("INSERT INTO message VALUES (?,?,?)", (
                "oc-1", 1000, json.dumps({"tokens": {
                    "input": 20_000, "cache": {"read": 4_000, "write": 1_000}}})))
            con.commit()
            con.close()
            models = os.path.join(tmp, "models.json")
            Path(models).write_text(json.dumps({"provider": {"models": {
                "model-x": {"limit": {"context": 100_000}}}}}), encoding="utf-8")
            old_db, old_models = os.environ.get("OPENCODE_DB"), os.environ.get("OPENCODE_MODELS")
            try:
                os.environ["OPENCODE_DB"] = db
                os.environ["OPENCODE_MODELS"] = models
                opencode._REG.update(ts=0.0, map=None)
                sess = Session(harness="opencode", pid=1, proc_start="1", cwd="/repo")
                with mock.patch.object(opencode, "session_of_process",
                                       return_value=("oc-1", "opencode-tui-selection")), \
                        mock.patch.object(opencode, "_proc_start_ticks", return_value="1"):
                    opencode.enrich(sess)
            finally:
                if old_db is None:
                    os.environ.pop("OPENCODE_DB", None)
                else:
                    os.environ["OPENCODE_DB"] = old_db
                if old_models is None:
                    os.environ.pop("OPENCODE_MODELS", None)
                else:
                    os.environ["OPENCODE_MODELS"] = old_models
            self.assertEqual(sess.active_context_tokens, 25_000)
            self.assertIsNone(sess.context_window_tokens)
            self.assertIsNone(sess.context_display_window_tokens)
            self.assertEqual(sess.ctx_pct, None)
            self.assertEqual(sess.context_policy["state"], "unknown")
            self.assertEqual(sess.session_total_tokens, 95_000)

    def test_zero_latest_request_is_not_replaced_by_cumulative_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "opencode.db")
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE session (id, slug, agent, model, cost, tokens_input, tokens_output, tokens_reasoning, time_updated, parent_id, directory, title)")
            con.execute("CREATE TABLE message (session_id, time_updated, data)")
            con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                "oc-zero", "slug", "agent", json.dumps({"id": "model-x"}), 0.1,
                80_000, 10_000, 5_000, 1000, None, "/repo", "title"))
            con.execute("INSERT INTO message VALUES (?,?,?)", (
                "oc-zero", 1000, json.dumps({"role": "assistant", "finish": "stop",
                                             "time": {"completed": 1}, "tokens": {
                    "input": 0, "output": 0, "cache": {"read": 0, "write": 0}}})))
            con.commit()
            con.close()
            models = os.path.join(tmp, "models.json")
            Path(models).write_text(json.dumps({"provider": {"models": {
                "model-x": {"limit": {"context": 100_000}}}}}), encoding="utf-8")
            old_db, old_models = os.environ.get("OPENCODE_DB"), os.environ.get("OPENCODE_MODELS")
            try:
                os.environ["OPENCODE_DB"] = db
                os.environ["OPENCODE_MODELS"] = models
                opencode._REG.update(ts=0.0, map=None, by_provider=None)
                sess = Session(harness="opencode", pid=1, proc_start="1", cwd="/repo")
                with mock.patch.object(opencode, "session_of_process",
                                       return_value=("oc-zero", "opencode-tui-selection")), \
                        mock.patch.object(opencode, "_proc_start_ticks", return_value="1"):
                    opencode.enrich(sess)
            finally:
                if old_db is None:
                    os.environ.pop("OPENCODE_DB", None)
                else:
                    os.environ["OPENCODE_DB"] = old_db
                if old_models is None:
                    os.environ.pop("OPENCODE_MODELS", None)
                else:
                    os.environ["OPENCODE_MODELS"] = old_models
            self.assertEqual(sess.active_context_tokens, 0)
            self.assertIsNone(sess.context_window_tokens)
            self.assertIsNone(sess.ctx_pct)

    def test_unfinished_zero_row_is_neither_a_reading_nor_cumulative_input(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE message (session_id, time_updated, data)")
        con.execute("INSERT INTO message VALUES (?,?,?)", (
            "s", 2, json.dumps({"role": "assistant", "time": {"created": 2}, "tokens": {
                "input": 0, "output": 0, "cache": {"read": 0, "write": 0}}})))
        self.assertIsNone(opencode._last_request_context(con, "s"))
        con.execute("INSERT INTO message VALUES (?,?,?)", (
            "s", 1, json.dumps({"role": "assistant", "finish": "stop", "tokens": {
                "input": 100, "output": 5, "cache": {"read": 20, "write": 0}}})))
        self.assertEqual(opencode._last_request_context(con, "s"), 125)
        con.close()


class TransitionPolicyTest(unittest.TestCase):

    def run_cli(self, state, pct, total, *extra):
        cmd = [sys.executable, str(CLI), "--adapter", "portable", "--session-id", SID,
               "--active-context-tokens", str(pct), "--context-window", "100",
               "--session-total-tokens", str(total), "--state-dir", state,
               "--format", "hook", *extra]
        return subprocess.run(cmd, text=True, capture_output=True, check=True).stdout

    def test_transition_only_and_byte_cap(self):
        with tempfile.TemporaryDirectory() as state:
            self.assertEqual(self.run_cli(state, 69, 100), "")
            self.assertEqual(len(os.listdir(state)), 1)
            tight = self.run_cli(state, 70, 110)
            self.assertIn("TOKEN_BUDGET=tight", tight)
            self.assertLessEqual(len(tight.encode("utf-8")), 240)
            self.assertEqual(tight.count("\n"), 1)
            self.assertEqual(self.run_cli(state, 75, 120), "")
            critical = self.run_cli(state, 85, 130)
            self.assertIn("TOKEN_BUDGET=critical", critical)
            self.assertLessEqual(len(critical.encode("utf-8")), 240)
            self.assertEqual(self.run_cli(state, 69, 140), "")
            self.assertIn("TOKEN_BUDGET=tight", self.run_cli(state, 70, 150))

    def test_native_and_counter_decrease_fail_open(self):
        with tempfile.TemporaryDirectory() as state:
            self.assertEqual(self.run_cli(state, 70, 100, "--native-active"), "")
            self.assertIn("TOKEN_BUDGET=tight", self.run_cli(state, 70, 110))
            self.assertEqual(self.run_cli(state, 85, 90), "")

            persisted = subprocess.run([
                sys.executable, str(CLI), "--adapter", "portable",
                "--session-id", SID, "--active-context-tokens", "85",
                "--context-window", "100", "--session-total-tokens", "90",
                "--state-dir", state, "--format", "json",
            ], text=True, capture_output=True, check=True)
            persisted_payload = json.loads(persisted.stdout)
            self.assertEqual((persisted_payload["status"],
                              persisted_payload["policy_state"]),
                             ("degraded", "unknown"))

        result = subprocess.run([
            sys.executable, str(CLI), "--adapter", "portable",
            "--active-context-tokens", "85", "--context-window", "100",
            "--session-total-tokens", "90", "--previous-session-total-tokens", "100",
            "--format", "json",
        ], text=True, capture_output=True, check=True)
        payload = json.loads(result.stdout)
        self.assertEqual((payload["status"], payload["policy_state"]),
                         ("degraded", "unknown"))

    def test_unwritable_state_fails_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "not-a-directory"
            state_file.write_text("occupied", encoding="utf-8")
            self.assertEqual(self.run_cli(str(state_file), 85, 100), "")

    def test_malformed_state_fails_open_without_reemitting(self):
        with tempfile.TemporaryDirectory() as state:
            path = Path(state) / f"{session_digest(SID)}.json"
            path.write_text("{malformed", encoding="utf-8")
            self.assertEqual(self.run_cli(state, 70, 100), "")
            result = subprocess.run([
                sys.executable, str(CLI), "--adapter", "portable",
                "--session-id", SID, "--active-context-tokens", "70",
                "--context-window", "100", "--session-total-tokens", "100",
                "--state-dir", state, "--format", "json",
            ], text=True, capture_output=True, check=True)
            payload = json.loads(result.stdout)
            self.assertEqual((payload["status"], payload["policy_state"], payload["reason"]),
                             ("degraded", "unknown", "malformed-transition-state"))

    def test_receipt_write_failure_preserves_phase1_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            receipt_target = Path(tmp) / "existing-directory"
            receipt_target.mkdir()
            env = os.environ.copy()
            env["AGENT_TOKEN_BUDGET_RESULT_PATH"] = str(receipt_target)
            result = subprocess.run([
                sys.executable, str(CLI), "--adapter", "portable",
                "--session-id", SID, "--active-context-tokens", "70",
                "--context-window", "100", "--session-total-tokens", "100",
                "--state-dir", str(state), "--format", "hook",
            ], text=True, capture_output=True, check=True, env=env)
            self.assertEqual(result.stdout, DIRECTIVE_TEXTS["tight-v1"] + "\n")

    def test_concurrent_transition_emits_once(self):
        with tempfile.TemporaryDirectory() as state:
            cmd = [sys.executable, str(CLI), "--adapter", "portable",
                   "--session-id", SID, "--active-context-tokens", "70",
                   "--context-window", "100", "--session-total-tokens", "100",
                   "--state-dir", state, "--format", "hook"]
            workers = [subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE) for _ in range(4)]
            outputs = []
            for worker in workers:
                stdout, stderr = worker.communicate(timeout=10)
                self.assertEqual((worker.returncode, stderr), (0, ""))
                outputs.append(stdout)
            self.assertEqual(sum("TOKEN_BUDGET=tight" in out for out in outputs), 1)


class AccountingTest(unittest.TestCase):

    def event(self, *, reason="normal", total=None, directive_id=None):
        if directive_id:
            text = DIRECTIVE_TEXTS[directive_id]
            return {
                "outcome": "emission", "zero_reason": None,
                "directive_id": directive_id,
                "directive_utf8_bytes": len(text.encode("utf-8")),
                "session_total_tokens": total,
            }
        return {
            "outcome": "zero", "zero_reason": reason,
            "directive_id": None, "directive_utf8_bytes": 0,
            "session_total_tokens": total,
        }

    def test_reducer_exact_identity_reasons_bytes_and_monotonic_samples(self):
        aggregate = empty_aggregate(adapter="codex", digest=session_digest(SID),
                                    observed_at="2026-07-13T00:00:00Z")
        events = [
            self.event(reason="normal", total=100),
            self.event(reason="unknown", total=110),
            self.event(reason="native", total=105),
            self.event(reason="same_band", total=None),
            self.event(reason="degraded", total=120),
            self.event(reason="timeout_or_error", total=None),
            self.event(directive_id="tight-v1", total=130),
        ]
        for index, event in enumerate(events):
            aggregate = reduce_accounting(
                aggregate, event, observed_at=f"2026-07-13T00:00:0{index}Z")
        self.assertEqual(aggregate["hook_invocations"], 7)
        self.assertEqual(aggregate["zero_injections"], 6)
        self.assertEqual(aggregate["emissions"], 1)
        self.assertEqual(set(aggregate["zero_reason_counts"]), set(ZERO_REASONS))
        self.assertTrue(all(value == 1 for value in aggregate["zero_reason_counts"].values()))
        self.assertEqual(aggregate["directive_utf8_bytes_total"],
                         len(DIRECTIVE_TEXTS["tight-v1"].encode("utf-8")))
        self.assertEqual(aggregate["observed_session_total_tokens_first"], 100)
        self.assertEqual(aggregate["observed_session_total_tokens_last"], 130)
        self.assertEqual(aggregate["observed_session_token_delta_monotonic"], 30)
        self.assertEqual(aggregate["counter_decrease_events"], 1)
        self.assertEqual(aggregate["unavailable_token_samples"], 2)
        self.assertNotIn("directive_exact_tokens", aggregate)

    def test_store_is_content_free_bounded_and_oldest_first(self):
        with tempfile.TemporaryDirectory() as state:
            raw_sid = "session-secret-12345678"
            self.assertTrue(record_accounting(
                raw_sid, self.event(directive_id="critical-v1", total=100),
                adapter="codex", state_dir=state,
                observed_at="2026-07-13T00:00:00Z"))
            path = accounting_path(raw_sid, state)
            body = path.read_text(encoding="utf-8")
            self.assertLessEqual(len(body.encode("utf-8")), MAX_FILE_BYTES)
            for forbidden in (raw_sid, "prompt", "response", "transcript",
                              DIRECTIVE_TEXTS["critical-v1"]):
                self.assertNotIn(forbidden, body)
            self.assertEqual(path.name, session_digest(raw_sid) + ".json")

            for index in range(MAX_FILES + 1):
                self.assertTrue(record_accounting(
                    f"bounded-session-{index:04d}", self.event(reason="normal", total=index),
                    adapter="codex", state_dir=state,
                    observed_at=f"2026-07-14T00:{index // 60:02d}:{index % 60:02d}Z"))
            files = list(accounting_dir(state).glob("*.json"))
            self.assertLessEqual(len(files), MAX_FILES)
            self.assertLessEqual(sum(path.stat().st_size for path in files), MAX_DIRECTORY_BYTES)
            self.assertFalse(accounting_path(raw_sid, state).exists())

    def test_store_rejects_extra_fields_and_serializes_concurrent_updates(self):
        with tempfile.TemporaryDirectory() as state:
            self.assertTrue(record_accounting(
                SID, self.event(reason="normal", total=100),
                adapter="codex", state_dir=state))
            path = accounting_path(SID, state)
            poisoned = json.loads(path.read_text(encoding="utf-8"))
            poisoned["prompt"] = "must-not-persist"
            path.write_text(json.dumps(poisoned), encoding="utf-8")
            self.assertIsNone(read_accounting(SID, adapter="codex", state_dir=state))
            self.assertTrue(record_accounting(
                SID, self.event(reason="normal", total=110),
                adapter="codex", state_dir=state))
            aggregate = read_accounting(SID, adapter="codex", state_dir=state)
            self.assertEqual(aggregate["hook_invocations"], 1)
            self.assertNotIn("prompt", path.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as state:
            event = self.event(reason="same_band", total=None)
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(
                    lambda _: record_accounting(
                        SID, event, adapter="codex", state_dir=state),
                    range(8),
                ))
            self.assertTrue(all(results))
            aggregate = read_accounting(SID, adapter="codex", state_dir=state)
            self.assertEqual((aggregate["hook_invocations"],
                              aggregate["zero_injections"],
                              aggregate["unavailable_token_samples"]),
                             (8, 8, 8))

    def test_writer_stamped_before_a_later_writer_cannot_rewind_last_observed(self):
        # Force the interleaving behind the flaky concurrent test: writer A
        # reads the clock, writer B stamps later but takes the lock first, then
        # A commits. A stamp taken outside the lock left last < first, which the
        # validator rejects, so the reader saw no record at all.
        import threading
        from fleet import token_accounting

        stamps = iter(f"2026-09-25T00:00:0{second}.000000Z" for second in range(10))
        clock_lock = threading.Lock()

        def ordered_clock():
            with clock_lock:
                return next(stamps)

        a_ready = threading.Event()
        b_done = threading.Event()
        real_lock = token_accounting.DirectoryLock

        class GatedLock(real_lock):
            def __enter__(self):
                if threading.current_thread().name == "writer-a":
                    a_ready.set()
                    b_done.wait(5)
                return super().__enter__()

        original_clock = token_accounting.utc_now
        token_accounting.utc_now = ordered_clock
        token_accounting.DirectoryLock = GatedLock
        try:
            with tempfile.TemporaryDirectory() as state:
                event = self.event(reason="same_band", total=None)
                results = {}

                def write(key):
                    results[key] = record_accounting(
                        SID, event, adapter="codex", state_dir=state)

                writer_a = threading.Thread(target=write, args=("a",), name="writer-a")
                writer_a.start()
                self.assertTrue(a_ready.wait(5))
                write("b")
                b_done.set()
                writer_a.join(5)
                self.assertEqual(results, {"a": True, "b": True})
                aggregate = read_accounting(SID, adapter="codex", state_dir=state)
                self.assertIsNotNone(aggregate)
                self.assertEqual(aggregate["hook_invocations"], 2)
                self.assertLessEqual(aggregate["first_observed_at"],
                                     aggregate["last_observed_at"])
        finally:
            token_accounting.utc_now = original_clock
            token_accounting.DirectoryLock = real_lock

    def test_explicit_older_observation_keeps_last_observed_monotonic(self):
        with tempfile.TemporaryDirectory() as state:
            event = self.event(reason="normal", total=None)
            self.assertTrue(record_accounting(
                SID, event, adapter="codex", state_dir=state,
                observed_at="2026-09-25T00:00:05.000000Z"))
            self.assertTrue(record_accounting(
                SID, event, adapter="codex", state_dir=state,
                observed_at="2026-09-25T00:00:01.000000Z"))
            aggregate = read_accounting(SID, adapter="codex", state_dir=state)
            self.assertIsNotNone(aggregate)
            self.assertEqual(aggregate["hook_invocations"], 2)
            self.assertEqual(aggregate["last_observed_at"], "2026-09-25T00:00:05.000000Z")

    def test_private_receipt_and_read_only_diagnostics(self):
        with tempfile.TemporaryDirectory() as state:
            receipt_path = Path(state) / "private" / "receipt.json"
            env = os.environ.copy()
            env["AGENT_TOKEN_BUDGET_RESULT_PATH"] = str(receipt_path)
            result = subprocess.run([
                sys.executable, str(CLI), "--adapter", "portable", "--session-id", SID,
                "--active-context-tokens", "70", "--context-window", "100",
                "--session-total-tokens", "100", "--state-dir", state,
                "--format", "hook",
            ], text=True, capture_output=True, check=True, env=env)
            self.assertEqual(result.stdout, DIRECTIVE_TEXTS["tight-v1"] + "\n")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["directive_id"], "tight-v1")
            self.assertEqual(receipt["directive_utf8_bytes"],
                             len(DIRECTIVE_TEXTS["tight-v1"].encode("utf-8")))
            self.assertNotIn("directive", " ".join(receipt.keys()).replace("directive_id", "").replace("directive_utf8_bytes", ""))

            self.assertTrue(record_accounting(
                SID, self.event(reason="same_band", total=100),
                adapter="portable", state_dir=state))
            before = read_accounting(SID, adapter="portable", state_dir=state)
            diagnostics = subprocess.run([
                sys.executable, str(CLI), "--adapter", "portable", "--session-id", SID,
                "--active-context-tokens", "75", "--context-window", "100",
                "--session-total-tokens", "100", "--state-dir", state,
                "--format", "json",
            ], text=True, capture_output=True, check=True)
            payload = json.loads(diagnostics.stdout)
            self.assertNotIn(SID, diagnostics.stdout)
            self.assertNotIn("session_id", payload)
            self.assertEqual(payload["accounting"]["hook_invocations"], 1)
            self.assertEqual(read_accounting(SID, adapter="portable", state_dir=state), before)
            diagnostic_kv = subprocess.run([
                sys.executable, str(CLI), "--adapter", "portable", "--session-id", SID,
                "--active-context-tokens", "75", "--context-window", "100",
                "--session-total-tokens", "100", "--state-dir", state,
                "--format", "kv",
            ], text=True, capture_output=True, check=True)
            self.assertNotIn(SID, diagnostic_kv.stdout)

    def test_lifecycle_timeout_records_exactly_one_zero(self):
        lifecycle_path = ROOT / "adapters" / "codex" / "hooks" / "userprompt-lifecycle.py"
        spec = importlib.util.spec_from_file_location("codex_userprompt_lifecycle_test", lifecycle_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        source = lifecycle_path.read_text(encoding="utf-8")
        exec(compile(source, str(lifecycle_path), "exec"), module.__dict__)
        with tempfile.TemporaryDirectory() as state:
            old = os.environ.get("XDG_STATE_HOME")
            os.environ["XDG_STATE_HOME"] = state
            try:
                module.run_preflight_result = lambda *args, **kwargs: module.PreflightResult(
                    returncode=-9, timed_out=True)
                self.assertEqual(module.token_budget_context("/tmp", SID), "")
            finally:
                if old is None:
                    os.environ.pop("XDG_STATE_HOME", None)
                else:
                    os.environ["XDG_STATE_HOME"] = old
            # The env is restored, so read through the explicit token-budget root.
            aggregate = read_accounting(
                SID, adapter="codex", state_dir=Path(state) / "hearting" / "token-budget")
            self.assertEqual(aggregate["hook_invocations"], 1)
            self.assertEqual(aggregate["zero_injections"], 1)
            self.assertEqual(aggregate["zero_reason_counts"]["timeout_or_error"], 1)

    def test_lifecycle_receipt_or_accounting_failure_preserves_delivered_bytes(self):
        lifecycle_path = ROOT / "adapters" / "codex" / "hooks" / "userprompt-lifecycle.py"
        spec = importlib.util.spec_from_file_location("codex_userprompt_lifecycle_receipt_test", lifecycle_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        source = lifecycle_path.read_text(encoding="utf-8")
        exec(compile(source, str(lifecycle_path), "exec"), module.__dict__)
        directive = DIRECTIVE_TEXTS["tight-v1"]
        module.run_preflight_result = lambda *args, **kwargs: module.PreflightResult(
            stdout=directive + "\n", returncode=0)
        events = []
        module.record_accounting = lambda sid, event, **kwargs: events.append(event) or True
        self.assertEqual(module.token_budget_context("/tmp", SID), directive)
        self.assertEqual((events[0]["outcome"], events[0]["directive_id"],
                          events[0]["directive_utf8_bytes"],
                          events[0]["session_total_tokens"]),
                         ("emission", "tight-v1", len(directive.encode("utf-8")), None))
        module.record_accounting = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("store"))
        self.assertEqual(module.token_budget_context("/tmp", SID), directive)

    def test_lifecycle_spawn_error_records_exactly_one_zero(self):
        lifecycle_path = ROOT / "adapters" / "codex" / "hooks" / "userprompt-lifecycle.py"
        spec = importlib.util.spec_from_file_location("codex_userprompt_lifecycle_spawn_test", lifecycle_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        source = lifecycle_path.read_text(encoding="utf-8")
        exec(compile(source, str(lifecycle_path), "exec"), module.__dict__)
        module.run_preflight_result = lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("spawn failed"))
        events = []
        module.record_accounting = lambda sid, event, **kwargs: events.append(event) or True
        self.assertEqual(module.token_budget_context("/tmp", SID), "")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], {
            "outcome": "zero", "zero_reason": "timeout_or_error",
            "directive_id": None, "directive_utf8_bytes": 0,
            "session_total_tokens": None,
        })


if __name__ == "__main__":
    unittest.main(verbosity=2)
