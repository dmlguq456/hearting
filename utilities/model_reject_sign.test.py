#!/usr/bin/env python3
"""A confirmed model refusal is named in the terminal sign and leaves one per-model line.

Invented envelopes only, an isolated usage-state directory, no real account. The
sign keeps `dead-runtime-error` / `runtime` (so every reader of that note acts as
before); only the reason column names the refusal. Nothing here may change a
retry, a replacement, a failover or which harness is chosen.
"""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "utilities", ROOT / "tools"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


T = _load("dispatch_supervisor_terminal", ROOT / "utilities" / "dispatch_supervisor_terminal.py")
from fleet.collectors import usage_cache  # noqa: E402
import dispatch_contract  # noqa: E402

NOT_FOUND = '{"type":"error","error":{"type":"not_found_error","message":"model: %s"},"request_id":"req_x"}'
SELECTED = ("There's an issue with the selected model (%s). It may not exist or you may not have "
            "access to it. Run --model to pick a different model.")


def claude_result(status, text):
    return {"type": "result", "subtype": "success", "is_error": True,
            "api_error_status": status, "result": text}


def columns(terminal):
    return (terminal.note, terminal.failure_class, terminal.terminal_event,
            terminal.reconcile_reason, terminal.process_exit, terminal.api_status)


class ClassifierTest(unittest.TestCase):
    def test_confirmed_refusals_keep_the_runtime_sign_and_name_the_model(self):
        cases = {
            "claude 404 not_found_error": (
                T.classify_session_result(claude_result(404, "API Error: 404 " + NOT_FOUND % "claude-x-1"),
                                          1, runtime="claude"), "runtime-model-rejected:claude-x-1", "404"),
            "claude selected-model message": (
                T.classify_claude_result(claude_result(None, SELECTED % "claude-x-1"), 1),
                "runtime-model-rejected:claude-x-1", ""),
            "claude selected-model with 404": (
                T.classify_claude_result(claude_result(404, SELECTED % "claude-x-1[1m]"), 1),
                "runtime-model-rejected:claude-x-1[1m]", "404"),
            "codex typed code": (
                T.codex_turn_failure_terminal({"codex_error_info": "modelNotFound",
                                               "message": "Access to this deployment ended."}),
                "runtime-model-rejected", ""),
            "codex wire spelling": (
                T.codex_turn_failure_terminal({"codex_error_info": "model_not_found"}),
                "runtime-model-rejected", ""),
        }
        for label, (terminal, reason, status) in cases.items():
            with self.subTest(label):
                self.assertEqual((terminal.note, terminal.failure_class, terminal.process_exit,
                                  terminal.reconcile_reason, terminal.api_status),
                                 ("dead-runtime-error", "runtime", terminal.process_exit, reason, status))

    def test_the_post_exit_log_reader_agrees_with_the_live_classifier(self):
        payload = {"type": "dispatch.supervisor.turn.failed", "codex_error_info": "modelNotFound",
                   "message": "gone", "additional_details": ""}
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            log.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            from_log = T.classify_supervisor_log(log, "codex")
        self.assertEqual(from_log, T.codex_turn_failure_terminal(payload))
        self.assertEqual(from_log.reconcile_reason, "runtime-model-rejected")

    def test_everything_else_keeps_its_sign_exactly(self):
        rt = ("dead-runtime-error", "runtime", "x", "runtime-error-envelope", "1")
        cap = ("dead-capacity", "capacity", "x", "runtime-capacity-envelope", "1")
        auth = ("dead-auth", "auth", "x", "runtime-auth-envelope", "1")
        edge = ("dead-runtime-error", "runtime", "x", T.EDGE_BLOCK_REASON, "1")
        table = [  # (runtime, status, text, structured_code, expected six columns)
            ("claude", "429", "rate_limit_error: model overloaded", "", (*cap, "429")),
            ("claude", "", "Selected model is at capacity", "", (*cap, "")),
            ("claude", "", "You've reached your usage limit for this model", "", (*cap, "")),
            ("claude", "403", "API Error: 403 " + NOT_FOUND % "claude-x-1", "", (*auth, "403")),
            ("claude", "401", SELECTED % "claude-x-1", "", (*auth, "401")),
            ("claude", "429", SELECTED % "claude-x-1", "", (*cap, "429")),
            ("claude", "403", "Attention Required! | Cloudflare", "", (*edge, "403")),
            ("claude", "", SELECTED % "claude-x-1" + " forbidden", "", (*auth, "")),
            ("claude", "404", "The requested resource could not be found", "", (*rt, "404")),
            ("claude", "404", '{"type":"not_found_error","message":"The requested resource"}', "", (*rt, "404")),
            ("claude", "404", "model not found", "", (*rt, "404")),
            ("claude", "", "model: claude-x-1", "", (*rt, "")),
            ("claude", "500", SELECTED % "claude-x-1", "", (*rt, "500")),
            ("claude", "529", "overloaded_error", "", (*rt, "529")),
            ("claude", "503", "upstream connect error", "", (*rt, "503")),
            ("claude", "", "", "", (*rt, "")),
            ("codex", "", "Selected model is at capacity", "serverOverloaded", (*rt, "")),
            ("codex", "", "model_not_found", "badRequest", (*rt, "")),
            ("codex", "", "model_not_found: The model does not exist", "", (*rt, "")),
            ("codex", "", SELECTED % "gpt-x", "", (*rt, "")),
            ("codex", "", "", "usageLimitExceeded", (*cap, "")),
            ("codex", "", "", "unauthorized", (*auth, "")),
            ("codex", "404", "model_not_found", "", (*rt, "404")),
            ("codex", "", "", "", (*rt, "")),
            # OpenCode keeps its own classification and record untouched.
            ("opencode", "", SELECTED % "m-1", "", (*rt, "")),
            ("opencode", "404", "API Error: 404 " + NOT_FOUND % "m-1", "", (*rt, "404")),
            ("opencode", "", "", "modelNotFound", (*rt, "")),
        ]
        for runtime, status, text, code, expected in table:
            with self.subTest(runtime=runtime, status=status, text=text[:30], code=code):
                got = T.classify_runtime_failure(runtime, event="x", process_exit=1, status=status,
                                                 text=text, structured_code=code)
                self.assertEqual(columns(got), expected)
                self.assertEqual((got.capacity_log, got.commit_detail, got.commit_slot), ("", "", ""))

    def test_the_reason_is_a_registry_safe_field_even_for_an_odd_model_name(self):
        text = SELECTED % "x,y z\tq"  # not a model name: no name, so no refusal is read
        got = T.classify_claude_result(claude_result(404, text), 1)
        self.assertEqual(got.reconcile_reason, "runtime-error-envelope")
        long_name = "m" * 200
        got = T.classify_claude_result(claude_result(404, SELECTED % long_name), 1)
        self.assertEqual(got.reconcile_reason, "runtime-error-envelope")
        evidence = T.classify_claude_result(claude_result(404, SELECTED % "claude-x-1"), 1).evidence()
        self.assertLessEqual(set(evidence), dispatch_contract.ATTEMPT_TERMINAL_EVIDENCE_KEYS)

    def test_no_reader_matches_the_old_reason_text_exactly(self):
        # The sign changes only its reason column. This keeps that safe: no
        # source outside the classifier may compare against the old reason.
        hits = []
        for base in ("utilities", "tools", "adapters", "hooks", "core", "capabilities"):
            for path in (ROOT / base).rglob("*"):
                if (path.suffix not in {".py", ".sh", ".js", ".json", ".tsv"} or not path.is_file()
                        or ".test." in path.name or path.name.endswith(".test.sh")):
                    continue
                if "runtime-error-envelope" in path.read_text(encoding="utf-8", errors="replace"):
                    hits.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(hits, ["utilities/dispatch_supervisor_terminal.py"])


class DownstreamPathTest(unittest.TestCase):
    """The new reason takes the same retry / replacement / failover path as `runtime-error-envelope`."""

    def meta(self, reason):
        return {"attempt_schema_version": "2", "dispatch_depth": "1", "transport": "headless",
                "execution_surface": "registered-headless", "registered_worker": "1",
                "fallback_hop": "same-harness-headless", "attempt_id": "att-x", "worker_type": "owner",
                "harness": "claude", "launch_started": "1", "note": "dead-runtime-error",
                "failure_class": "runtime", "terminal_event": "claude-result",
                "process_exit": "1", "reconcile_reason": reason, "api_status": "404"}

    def test_runtime_end_predicates_and_replacement_kind_do_not_see_the_reason(self):
        import dispatch_replacement as R
        old = self.meta("runtime-error-envelope")
        new = self.meta("runtime-model-rejected:claude-x-1")
        self.assertEqual(T.runtime_ended_without_result(old), T.runtime_ended_without_result(new))
        self.assertTrue(T.runtime_ended_without_result(new))
        self.assertEqual(R.death_kind(["now", "done"], old), R.death_kind(["now", "done"], new))
        self.assertEqual(R.death_kind(["now", "done"], new), "runtime")

    def test_transient_retry_schedule_is_keyed_on_status_and_kind_not_on_the_reason(self):
        import session_supervisor_decisions as D
        refused = T.codex_turn_failure_terminal({"codex_error_info": "modelNotFound"})
        plain = T.codex_turn_failure_terminal({"codex_error_info": "other"})
        # Only the edge-block reason opens the retry; the new reason does not, as before.
        for failures in (0, 1, 2):
            self.assertIsNone(D.edge_block_retry_delay(refused, failures))
            self.assertEqual(D.edge_block_retry_delay(refused, failures),
                             D.edge_block_retry_delay(plain, failures))
        # The structured overload schedule reads the code, which the refusal does not share.
        self.assertIsNone(D.transient_retry_delay("modelNotFound", 0))
        self.assertEqual(D.transient_retry_delay("modelNotFound", 0), D.transient_retry_delay("other", 0))
        self.assertIsNotNone(D.transient_retry_delay("serverOverloaded", 0))


REGISTRY_ROW = (
    "2026-08-06T00:00:00Z\topen\t{repo}\t{repo}\tstage\t"
    "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
    "execution_surface=registered-headless,registered_worker=1,"
    "fallback_hop=same-harness-headless,harness={harness},"
    "attempt_id={attempt},parent_attempt_id=att-parent,model={model}{extra}\n"
)


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.jobs = self.base / "jobs.log"
        (self.base / "repo").mkdir()
        self.state = self.base / "usage"
        patcher = mock.patch.dict(os.environ, {"FLEET_USAGE_STATE_DIR": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.attempts = 0

    def attempt(self, harness, model, scope=None):
        self.attempts += 1
        attempt = f"att-{self.attempts}"
        extra = f",quota_scope={scope},quota_scope_kind=claude-subscription-v2" if scope else ""
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write(REGISTRY_ROW.format(repo=self.base / "repo", harness=harness,
                                             attempt=attempt, model=model, extra=extra))
        return attempt

    def close(self, attempt, terminal):
        return T.reconcile_supervisor_terminal(self.jobs, attempt, terminal)

    def claude_cache(self, payload=None):
        usage_cache._write("claude", payload or {"five_hour": {"used": 3}}, 1000.0, 1000.0,
                           credential_generation="gen-1")
        return (self.state / "claude.json").read_bytes()

    def lines(self, harness):
        path = self.state / f"{harness}.json"
        return json.loads(path.read_text()).get("providers", {}) if path.exists() else {}

    def refusal(self, harness, model):
        if harness == "codex":
            return T.codex_turn_failure_terminal({"codex_error_info": "modelNotFound"})
        return T.classify_claude_result(claude_result(404, "API Error: 404 " + NOT_FOUND % model), 1)

    def passed(self):
        return T.SupervisorTerminal("completed-supervisor", "pass", "claude-result",
                                    "exact-final-handoff", "0")

    def test_a_confirmed_refusal_leaves_one_line_on_the_launched_model_and_account(self):
        self.claude_cache()
        attempt = self.attempt("claude", "claude-x-1", scope="scope-a")
        self.assertEqual(self.close(attempt, self.refusal("claude", "claude-x-1")), "closed")
        providers = self.lines("claude")
        self.assertEqual(len(providers), 1)
        (row,) = providers.values()
        self.assertEqual({k: row[k] for k in ("provider", "model", "account_scope", "state", "reason", "source")},
                         {"provider": "claude", "model": "claude-x-1", "account_scope": "scope-a",
                          "state": "unavailable", "reason": "model-unavailable",
                          "source": "supervisor-terminal"})
        self.assertEqual(usage_cache.read_provider("claude", "claude", "claude-x-1", "scope-a")["state"],
                         "unavailable")

    def test_codex_takes_the_same_path_and_is_unscoped(self):
        attempt = self.attempt("codex", "gpt-x-1")
        self.close(attempt, self.refusal("codex", "gpt-x-1"))
        (row,) = self.lines("codex").values()
        self.assertEqual((row["provider"], row["model"], row["account_scope"], row["state"]),
                         ("codex", "gpt-x-1", "unscoped", "unavailable"))
        # The usage refresh back-off (`attempted_at`) is not moved by a per-model line.
        self.assertEqual(json.loads((self.state / "codex.json").read_text())["attempted_at"], 0.0)

    def test_a_refusal_that_points_at_another_model_records_nothing(self):
        self.claude_cache()
        before = (self.state / "claude.json").read_bytes()
        attempt = self.attempt("claude", "claude-x-1", scope="scope-a")
        self.close(attempt, self.refusal("claude", "claude-other-2"))
        self.assertEqual((self.state / "claude.json").read_bytes(), before)
        attempt = self.attempt("claude", "opus", scope="scope-a")  # alias vs resolved name: cannot tell
        self.close(attempt, self.refusal("claude", "claude-opus-9"))
        self.assertEqual((self.state / "claude.json").read_bytes(), before)

    def test_two_accounts_never_share_a_line(self):
        self.claude_cache()
        self.close(self.attempt("claude", "claude-x-1", scope="scope-a"), self.refusal("claude", "claude-x-1"))
        self.assertEqual(usage_cache.read_provider("claude", "claude", "claude-x-1", "scope-b")["state"], "unknown")
        self.close(self.attempt("claude", "claude-x-1", scope="scope-b"), self.passed())
        self.assertEqual(usage_cache.read_provider("claude", "claude", "claude-x-1", "scope-a")["state"],
                         "unavailable")
        self.assertEqual(usage_cache.read_provider("claude", "claude", "claude-x-1", "scope-b")["state"], "unknown")

    def test_a_later_answer_from_the_same_model_and_account_lifts_the_refusal_without_rewriting_otherwise(self):
        self.claude_cache()
        path = self.state / "claude.json"
        # An answer with no refusal on file never rewrites the file.
        before = path.read_bytes()
        self.close(self.attempt("claude", "claude-x-1", scope="scope-a"), self.passed())
        self.assertEqual(path.read_bytes(), before)
        self.close(self.attempt("claude", "claude-x-1", scope="scope-a"), self.refusal("claude", "claude-x-1"))
        self.assertEqual(usage_cache.read_provider("claude", "claude", "claude-x-1", "scope-a")["state"],
                         "unavailable")
        self.close(self.attempt("claude", "claude-x-1", scope="scope-a"), self.passed())
        self.assertEqual(usage_cache.read_provider("claude", "claude", "claude-x-1", "scope-a")["state"],
                         "usable")
        # A handed-off FAIL is the model answering too.
        self.close(self.attempt("claude", "claude-x-1", scope="scope-a"), self.refusal("claude", "claude-x-1"))
        failed = T.SupervisorTerminal("dead-worker-fail", "fail", "claude-result", "worker-reported-fail", "0")
        self.close(self.attempt("claude", "claude-x-1", scope="scope-a"), failed)
        self.assertEqual(usage_cache.read_provider("claude", "claude", "claude-x-1", "scope-a")["state"],
                         "usable")

    def test_claude_cache_survives_the_line_and_its_own_writer_keeps_it(self):
        raw = self.claude_cache()
        credential = types.SimpleNamespace(generation="gen-1", reason=None)
        read_before = usage_cache._read("claude", 1010.0, credential=credential)
        self.close(self.attempt("claude", "claude-x-1", scope="scope-a"), self.refusal("claude", "claude-x-1"))
        item = json.loads((self.state / "claude.json").read_text())
        old = json.loads(raw)
        self.assertEqual({k: v for k, v in item.items() if k != "providers"},
                         {k: v for k, v in old.items() if k != "providers"})
        self.assertEqual(usage_cache._read("claude", 1010.0, credential=credential), read_before)
        # The lease-owned worker rewrites the file; the line stays and the payload is its own.
        usage_cache._write("claude", {"five_hour": {"used": 9}}, 2000.0, 2000.0, credential_generation="gen-1")
        self.assertEqual(usage_cache.read_provider("claude", "claude", "claude-x-1", "scope-a")["state"],
                         "unavailable")
        self.assertEqual(usage_cache._read("claude", 2010.0, credential=credential)["payload"],
                         {"five_hour": {"used": 9}})

    def test_claude_without_a_usage_cache_creates_no_state_file(self):
        self.close(self.attempt("claude", "claude-x-1", scope="scope-a"), self.refusal("claude", "claude-x-1"))
        self.assertFalse((self.state / "claude.json").exists())

    def test_other_failures_and_other_harnesses_leave_no_line(self):
        self.claude_cache()
        before = (self.state / "claude.json").read_bytes()
        capacity = T.classify_claude_result(claude_result(429, "rate_limit_error model"), 1)
        self.close(self.attempt("claude", "claude-x-1", scope="scope-a"), capacity)
        plain = T.classify_claude_result(claude_result(500, "API Error: 500"), 1)
        self.close(self.attempt("claude", "claude-x-1", scope="scope-a"), plain)
        self.assertEqual((self.state / "claude.json").read_bytes(), before)
        # OpenCode records through its own path; this hook never touches it.
        shaped = T.SupervisorTerminal("dead-runtime-error", "runtime", "x",
                                      "runtime-model-rejected:m-1", "1")
        self.close(self.attempt("opencode", "p/m-1"), shaped)
        self.assertFalse((self.state / "opencode.json").exists())

    def test_a_record_that_cannot_be_kept_never_changes_the_close(self):
        attempt = self.attempt("codex", "gpt-x-1")
        with mock.patch.object(usage_cache, "record_model_outcome", side_effect=RuntimeError("boom")):
            self.assertEqual(self.close(attempt, self.refusal("codex", "gpt-x-1")), "closed")
        fields = self.jobs.read_text().splitlines()[0].split("\t")
        meta = dispatch_contract.parse_registry_metadata(fields[5])
        self.assertEqual((fields[1], meta["note"], meta["failure_class"], meta["reconcile_reason"]),
                         ("done", "dead-runtime-error", "runtime", "runtime-model-rejected"))

    def test_the_lines_change_no_selection_or_display_reader(self):
        import dispatch_capacity_evidence as E
        self.claude_cache()
        self.close(self.attempt("claude", "claude-x-1", scope="scope-a"), self.refusal("claude", "claude-x-1"))
        self.close(self.attempt("codex", "gpt-x-1"), self.refusal("codex", "gpt-x-1"))
        for harness in ("claude", "codex"):
            self.assertIsNone(E.model_hold(harness, "deep"))
        # Only OpenCode's Fleet view reads provider lines, and it asks for its own file.
        source = (ROOT / "tools" / "fleet" / "collectors" / "__init__.py").read_text()
        self.assertEqual(re.findall(r"usage_cache\.provider_records\(([^,)]*)", source), ['"opencode"'])


class ShapeTest(unittest.TestCase):
    """What an envelope must say, and what it still may not, to be read as a refusal."""

    def read(self, runtime, status, text="", code=""):
        return T.classify_runtime_failure(runtime, event="x", process_exit=1, status=status,
                                          text=text, structured_code=code)

    def test_a_typed_codex_code_stands_only_with_a_status_that_does_not_contradict_it(self):
        for code in ("modelNotFound", "model_not_found"):
            for status in ("", "400", "404"):
                with self.subTest(code=code, status=status):
                    got = self.read("codex", status, code=code)
                    self.assertEqual((got.note, got.failure_class, got.reconcile_reason),
                                     ("dead-runtime-error", "runtime", "runtime-model-rejected"))
            # 5xx and 429 keep the sign they had; 401/403 are account answers, as before.
            for status, expected in (("500", ("dead-runtime-error", "runtime-error-envelope")),
                                     ("502", ("dead-runtime-error", "runtime-error-envelope")),
                                     ("503", ("dead-runtime-error", "runtime-error-envelope")),
                                     ("529", ("dead-runtime-error", "runtime-error-envelope")),
                                     ("429", ("dead-capacity", "runtime-capacity-envelope")),
                                     ("401", ("dead-auth", "runtime-auth-envelope")),
                                     ("403", ("dead-auth", "runtime-auth-envelope"))):
                with self.subTest(code=code, status=status):
                    got = self.read("codex", status, code=code)
                    self.assertEqual((got.note, got.reconcile_reason), expected)
        # Through the logged payload: the status rides next to the kind.
        got = T.codex_turn_failure_terminal(
            {"codex_error_info": {"kind": "modelNotFound", "http_status": 503}})
        self.assertEqual(got.reconcile_reason, "runtime-error-envelope")
        got = T.codex_turn_failure_terminal(
            {"codex_error_info": {"kind": "modelNotFound", "http_status": 404}})
        self.assertEqual(got.reconcile_reason, "runtime-model-rejected")

    def test_the_claude_404_object_reads_the_same_in_any_key_order(self):
        def envelope(obj):
            return "API Error: 404 " + json.dumps({"type": "error", "error": obj, "request_id": "r"})
        same = [
            {"type": "not_found_error", "message": "model: claude-x-1"},
            {"message": "model: claude-x-1", "type": "not_found_error"},
            {"type": "not_found_error", "request_id": "r", "message": "model: claude-x-1"},
            {"message": "model: claude-x-1", "extra": 1, "type": "not_found_error"},
        ]
        for obj in same:
            with self.subTest(obj=obj):
                got = self.read("claude", "404", envelope(obj))
                self.assertEqual(got.reconcile_reason, "runtime-model-rejected:claude-x-1")
        # A truncated log keeps reading as before (no closing braces at all).
        got = self.read("claude", "404", '..."message":"model: claude-x-1","type":"not_found_error"')
        self.assertEqual(got.reconcile_reason, "runtime-model-rejected:claude-x-1")
        # The two keys must belong to one object; a model-free message is no refusal.
        for text in (
                'API Error: 404 {"type":"not_found_error","detail":{"message":"model: claude-x-1"}}',
                'API Error: 404 {"message":"model: claude-x-1"} {"type":"not_found_error"}',
                'API Error: 404 {"type":"not_found_error","message":"The requested resource"}',
                'API Error: 404 {"type":"invalid_request_error","message":"model: claude-x-1"}'):
            with self.subTest(text=text):
                self.assertEqual(self.read("claude", "404", text).reconcile_reason,
                                 "runtime-error-envelope")
        # Other statuses never read it, in either order.
        reversed_text = envelope({"message": "model: claude-x-1", "type": "not_found_error"})
        for status in ("500", "400", "403"):
            self.assertNotEqual(self.read("claude", status, reversed_text).reconcile_reason,
                                "runtime-model-rejected:claude-x-1")

    def test_a_selected_model_sentence_quoted_inside_another_error_is_pinned_as_it_reads_today(self):
        # An envelope offers no ground to tell a quotation from the CLI's own sentence: the
        # result is one string and every string of the envelope is read together. The reading
        # is therefore left exactly as it was (not widened, not narrowed); these pin it.
        quote = '"' + SELECTED % "claude-x-1" + '"'
        cases = [
            # status decides first: none of these is ever read as a refusal
            ("claude", "500", "API Error: 500 upstream said " + quote, "runtime-error-envelope"),
            ("claude", "502", "API Error: 502 gateway relayed " + quote, "runtime-error-envelope"),
            ("claude", "529", "overloaded_error, last reply " + quote, "runtime-error-envelope"),
            ("claude", "429", "rate_limit_error, last reply " + quote, "runtime-capacity-envelope"),
            ("claude", "401", "authentication_error, last reply " + quote, "runtime-auth-envelope"),
            ("claude", "403", "Attention Required! | Cloudflare " + quote, T.EDGE_BLOCK_REASON),
            ("claude", "400", "invalid_request_error, relayed " + quote, "runtime-error-envelope"),
            # a quotation in an envelope with no status, or a 404, cannot be told apart
            ("claude", "", "Request failed; the log held " + quote, "runtime-model-rejected:claude-x-1"),
            ("claude", "404", "API Error: 404 route not found; the log held " + quote,
             "runtime-model-rejected:claude-x-1"),
            # capacity/auth words in the same text still win, as before
            ("claude", "", "usage limit reached " + quote, "runtime-capacity-envelope"),
            ("claude", "", "invalid api key " + quote, "runtime-auth-envelope"),
        ]
        for runtime, status, text, reason in cases:
            with self.subTest(status=status, text=text[:40]):
                self.assertEqual(self.read(runtime, status, text).reconcile_reason, reason)
        # Codex never reads the sentence at all, quoted or not.
        self.assertEqual(self.read("codex", "", "log held " + quote).reconcile_reason,
                         "runtime-error-envelope")


class AnswerAndPlaceholderTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / "repo").mkdir()
        self.jobs = self.base / "jobs.log"
        self.state = self.base / "usage"
        patcher = mock.patch.dict(os.environ, {"FLEET_USAGE_STATE_DIR": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.count = 0

    def close(self, harness, model, terminal, scope=""):
        self.count += 1
        attempt = f"att-{self.count}"
        extra = f",quota_scope={scope},quota_scope_kind=claude-subscription-v2" if scope else ""
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write(REGISTRY_ROW.format(repo=self.base / "repo", harness=harness,
                                             attempt=attempt, model=model, extra=extra))
        return T.reconcile_supervisor_terminal(self.jobs, attempt, terminal)

    def lines(self, harness):
        path = self.state / f"{harness}.json"
        return json.loads(path.read_text()).get("providers", {}) if path.exists() else {}

    def test_an_unsettled_model_records_nothing_but_the_sign_still_names_the_model(self):
        refused = T.classify_claude_result(
            claude_result(404, "API Error: 404 " + NOT_FOUND % "claude-x-1"), 1)
        self.assertEqual(refused.reconcile_reason, "runtime-model-rejected:claude-x-1")
        usage_cache._write("claude", {"used": 1}, 1000.0, 1000.0, credential_generation="g")
        before = (self.state / "claude.json").read_bytes()
        for placeholder in ("inherit", "Inherit", "default", "runtime-default", "unknown"):
            with self.subTest(placeholder):
                self.assertEqual(self.close("claude", placeholder, refused, "scope-a"), "closed")
                self.assertEqual(self.close("codex", placeholder,
                                            T.codex_turn_failure_terminal(
                                                {"codex_error_info": "modelNotFound"})), "closed")
        self.assertEqual((self.state / "claude.json").read_bytes(), before)
        self.assertFalse((self.state / "codex.json").exists())
        # A real name still records.
        self.close("codex", "gpt-x-1", T.codex_turn_failure_terminal({"codex_error_info": "modelNotFound"}))
        self.assertEqual(list(self.lines("codex")), ['["codex","gpt-x-1","unscoped"]'])

    def test_a_handoff_of_the_wrong_shape_is_the_model_answering_and_lifts_the_refusal(self):
        usage_cache._write("claude", {"used": 1}, 1000.0, 1000.0, credential_generation="g")
        refused = T.classify_claude_result(
            claude_result(404, "API Error: 404 " + NOT_FOUND % "claude-x-1"), 1)
        wrong_shape = T.classify_claude_result(
            {"type": "result", "subtype": "success", "result": "All done, no handoff block."}, 0)
        self.assertEqual((wrong_shape.note, wrong_shape.failure_class), ("dead-contract", "contract"))
        self.close("claude", "claude-x-1", refused, "scope-a")
        self.close("claude", "claude-x-1", wrong_shape, "scope-a")
        self.assertEqual(usage_cache.read_provider("claude", "claude", "claude-x-1", "scope-a")["state"],
                         "usable")
        # The same for a handoff that breaks the PASS rule, and for Codex.
        bad_pass = T.classify_claude_result(
            {"type": "result", "subtype": "success",
             "result": "artifact: -\nverdict: PASS\nblocker: still broken"}, 0)
        self.assertEqual(bad_pass.note, "dead-contract")
        self.close("claude", "claude-x-1", refused, "scope-a")
        self.close("claude", "claude-x-1", bad_pass, "scope-a")
        self.assertEqual(usage_cache.read_provider("claude", "claude", "claude-x-1", "scope-a")["state"],
                         "usable")
        self.close("codex", "gpt-x-1", T.codex_turn_failure_terminal({"codex_error_info": "modelNotFound"}))
        self.close("codex", "gpt-x-1", T.classify_codex_result("finished", 0))
        self.assertEqual(usage_cache.read_provider("codex", "codex", "gpt-x-1", "unscoped")["state"], "usable")

    def test_an_end_that_does_not_show_the_model_answered_keeps_the_refusal(self):
        usage_cache._write("claude", {"used": 1}, 1000.0, 1000.0, credential_generation="g")
        refused = T.classify_claude_result(
            claude_result(404, "API Error: 404 " + NOT_FOUND % "claude-x-1"), 1)
        ends = {
            "capacity": T.classify_claude_result(claude_result(429, "rate_limit_error"), 1),
            "auth": T.classify_claude_result(claude_result(401, "authentication_error"), 1),
            "runtime error": T.classify_claude_result(claude_result(500, "API Error: 500"), 1),
            "process exit": T.classify_codex_result("x", 1),
            "supervisor error": T.classify_supervisor_error("claude", "terminal-event-missing"),
            "protocol": T.classify_supervisor_error("claude", "missing-protocol-json"),
        }
        self.close("claude", "claude-x-1", refused, "scope-a")
        for label, terminal in ends.items():
            with self.subTest(label):
                self.close("claude", "claude-x-1", terminal, "scope-a")
                self.assertEqual(
                    usage_cache.read_provider("claude", "claude", "claude-x-1", "scope-a")["state"],
                    "unavailable")


def _opencode_steps():
    initial = [None, "{", "[]", '{"schema_version":1,"harness":"opencode","payload":{}}']
    ops = [
        lambda m: m.record_provider("opencode", "p", "m", "a", "unavailable",
                                    reason="model-unavailable", now=100, source="probe"),
        lambda m: m.record_attempt("opencode", now=110),
        lambda m: m._write("opencode", {"usage": 7}, 120, 120),
        lambda m: m.record_provider("opencode", "p", "m", "a", "usable", now=130,
                                    source="probe", credential_generation="g"),
    ]
    return initial, ops


# sha256[:16] of opencode.json after each of the four steps, from the code before the
# per-model line existed (four starting files: absent, broken, an array, a bare cache).
OPENCODE_BYTES_BEFORE = [
    ["bf426caa5cb3dbb6", "4b61625a120f9b39", "7f43c77fe0d77be8", "81775c0b2f141b12"],
    ["bf426caa5cb3dbb6", "4b61625a120f9b39", "7f43c77fe0d77be8", "81775c0b2f141b12"],
    ["bf426caa5cb3dbb6", "4b61625a120f9b39", "7f43c77fe0d77be8", "81775c0b2f141b12"],
    ["e103c9cafc98deec", "4b61625a120f9b39", "7f43c77fe0d77be8", "81775c0b2f141b12"],
]


class CacheLockTest(unittest.TestCase):
    """Every writer of one cache file reads, edits and replaces it inside one lock."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "usage"
        patcher = mock.patch.dict(os.environ, {"FLEET_USAGE_STATE_DIR": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Long enough that the lock, not its timeout, decides the order in these schedules.
        wait = mock.patch.object(usage_cache, "LOCK_WAIT", 10.0)
        wait.start()
        self.addCleanup(wait.stop)
        spec = importlib.util.spec_from_file_location("capacity_probe_x", ROOT / "utilities" / "harness-capacity.py")
        self.capacity = importlib.util.module_from_spec(spec)
        sys.modules["capacity_probe_x"] = self.capacity
        spec.loader.exec_module(self.capacity)

    def schedule(self, first, second):
        """Run `first` until it reaches os.replace, start `second`, then let both finish.

        Returns whether `second` finished while `first` was still held at the boundary."""
        entered, release = threading.Event(), threading.Event()
        real = os.replace
        errors = []

        def replace(src, dst):
            if threading.current_thread().name == "first" and not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("schedule timed out")
            return real(src, dst)

        def run(fn):
            def body():
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001
                    errors.append(repr(exc))
            return body

        a = threading.Thread(target=run(first), name="first")
        b = threading.Thread(target=run(second), name="second")
        with mock.patch.object(usage_cache.os, "replace", side_effect=replace):
            a.start()
            self.assertTrue(entered.wait(5))
            b.start()
            b.join(0.3)
            finished_early = not b.is_alive()
            release.set()
            a.join(10)
            b.join(10)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(errors, [])
        return finished_early

    def usage(self, harness="codex", used=99, at=200):
        payload = {"windows": [["5h", used], ["7d", used]]}
        return lambda: usage_cache._write(harness, payload, at, at)

    def record(self, harness="codex", model="model-x", state="unavailable", at=150):
        return lambda: usage_cache.record_model_outcome(harness, harness, model, "unscoped", state, now=at)

    def cache(self, harness="codex"):
        return json.loads((self.state / f"{harness}.json").read_text())

    def seed(self, used=1):
        usage_cache._write("codex", {"windows": [["5h", used], ["7d", used]]}, 100, 100)

    def test_a_per_model_line_does_not_roll_a_newer_usage_reading_back(self):
        self.seed()
        self.assertFalse(self.schedule(self.record(), self.usage()))
        item = self.cache()
        self.assertEqual(item["payload"], {"windows": [["5h", 99], ["7d", 99]]})
        self.assertEqual((item["fetched_at"], item["attempted_at"]), (200, 200))
        self.assertEqual(list(item["providers"]), ['["codex","model-x","unscoped"]'])

    def test_a_usage_writer_does_not_lose_a_per_model_line(self):
        self.seed()
        self.assertFalse(self.schedule(self.usage(), self.record()))
        item = self.cache()
        self.assertEqual(item["payload"], {"windows": [["5h", 99], ["7d", 99]]})
        self.assertEqual(list(item["providers"]), ['["codex","model-x","unscoped"]'])

    def test_the_selection_score_is_the_same_before_and_after_a_line_is_recorded(self):
        usage_cache._write("codex", {"windows": [["5h", 99], ["7d", 99]]}, 200, 200)
        control = self.capacity._cached_score("codex", 201, 900)
        self.seed()
        self.schedule(self.record(), self.usage())
        self.assertEqual(self.capacity._cached_score("codex", 201, 900), control)
        self.assertNotEqual(control, (None, None))

    def test_two_different_models_recorded_at_once_both_stay(self):
        self.seed()
        self.assertFalse(self.schedule(self.record(model="model-a"), self.record(model="model-b")))
        self.assertEqual(sorted(self.cache()["providers"]),
                         ['["codex","model-a","unscoped"]', '["codex","model-b","unscoped"]'])

    def test_lifting_a_refusal_and_the_failed_probe_note_do_not_undo_each_other(self):
        self.seed()
        self.record()()
        # a usage writer racing the lift
        self.assertFalse(self.schedule(self.record(state="usable", at=170), self.usage()))
        item = self.cache()
        self.assertEqual(item["payload"], {"windows": [["5h", 99], ["7d", 99]]})
        (row,) = item["providers"].values()
        self.assertEqual(row["state"], "usable")
        # the failed-probe note races a new refusal
        self.assertFalse(self.schedule(lambda: usage_cache.record_attempt("codex", now=300),
                                       self.record(model="model-c", at=310)))
        item = self.cache()
        self.assertEqual(item["attempted_at"], 300)
        self.assertEqual(item["payload"], {"windows": [["5h", 99], ["7d", 99]]})
        self.assertIn('["codex","model-c","unscoped"]', item["providers"])

    def test_claude_keeps_its_own_lock_and_the_line_with_the_workers_publication(self):
        usage_cache._write("claude", {"rl_5h": 1}, 100, 100, credential_generation="g")
        first = lambda: usage_cache.record_model_outcome("claude", "claude", "m", "a", "unavailable", now=150)
        second = lambda: usage_cache._write("claude", {"rl_5h": 99}, 200, 200, credential_generation="g")
        self.assertFalse(self.schedule(first, second))
        item = self.cache("claude")
        self.assertEqual((item["payload"], item["_credential_generation"]), ({"rl_5h": 99}, "g"))
        self.assertEqual(list(item["providers"]), ['["claude","m","a"]'])

    def test_a_nested_take_by_the_same_thread_is_the_same_hold(self):
        # The Claude worker takes the shared mutex, then writes; the write takes it again.
        with usage_cache._claude_mutex():
            usage_cache._write("claude", {"rl_5h": 5}, 100, 100, credential_generation="g")
            self.assertTrue(usage_cache.record_provider("claude", "claude", "m", "a", "unavailable", now=120))

    def test_a_lock_that_is_not_released_never_stalls_a_usage_writer_or_the_close(self):
        self.seed()
        holder = os.open(str(self.state / ".codex.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX)
        self.addCleanup(os.close, holder)
        with mock.patch.object(usage_cache, "LOCK_WAIT", 0.1):
            started = time.monotonic()
            usage_cache._write("codex", {"windows": [["5h", 7], ["7d", 7]]}, 200, 200)
            wrote = time.monotonic() - started
            started = time.monotonic()
            kept = usage_cache.record_model_outcome("codex", "codex", "model-x", "unscoped", "unavailable", now=150)
            dropped = time.monotonic() - started
            started = time.monotonic()
            usage_cache.record_attempt("codex", now=250)
            attempt = time.monotonic() - started
        # The usage publication happens as it did before the lock; the line is dropped.
        self.assertEqual(self.cache()["payload"], {"windows": [["5h", 7], ["7d", 7]]})
        self.assertFalse(kept)
        self.assertEqual(self.cache()["providers"], {})
        self.assertEqual(self.cache()["attempted_at"], 250)
        for took in (wrote, dropped, attempt):
            self.assertGreaterEqual(took, 0.09)
            self.assertLess(took, 1.5)
        # OpenCode's own recorder keeps writing, as it always did.
        with mock.patch.object(usage_cache, "LOCK_WAIT", 0.05):
            holder_oc = os.open(str(self.state / ".opencode.lock"), os.O_CREAT | os.O_RDWR, 0o600)
            self.addCleanup(os.close, holder_oc)
            fcntl.flock(holder_oc, fcntl.LOCK_EX)
            self.assertTrue(usage_cache.record_provider("opencode", "p", "m", "a", "unavailable", now=100))

    def test_a_success_with_no_refusal_on_file_takes_no_lock_and_writes_nothing(self):
        self.seed()
        before = (self.state / "codex.json").read_bytes()
        with mock.patch.object(usage_cache, "_cache_mutex", side_effect=AssertionError("locked")), \
                mock.patch.object(usage_cache.os, "replace", side_effect=AssertionError("wrote")):
            self.assertFalse(usage_cache.record_model_outcome(
                "codex", "codex", "model-x", "unscoped", "usable", now=160))
        self.assertEqual((self.state / "codex.json").read_bytes(), before)

    def test_opencode_bytes_are_those_of_the_code_before_the_lock(self):
        initial, ops = _opencode_steps()
        for number, start in enumerate(initial):
            with self.subTest(start=start):
                state = self.state / f"oc{number}"
                state.mkdir(parents=True)
                with mock.patch.dict(os.environ, {"FLEET_USAGE_STATE_DIR": str(state)}):
                    if start is not None:
                        (state / "opencode.json").write_text(start)
                    seen = []
                    for op in ops:
                        op(usage_cache)
                        seen.append(hashlib.sha256((state / "opencode.json").read_bytes()).hexdigest()[:16])
                self.assertEqual(seen, OPENCODE_BYTES_BEFORE[number])


class JoinRecordTest(unittest.TestCase):
    """A row the completion join closes carries the same record as one the supervisor closes."""

    rejected = {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 404,
                "result": 'API Error: 404 {"type":"not_found_error","message":"model: model-x"}'}
    passed = {"type": "result", "subtype": "success", "is_error": False,
              "result": "artifact: -\nverdict: PASS\nblocker: none"}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / "repo").mkdir()
        self.jobs, self.log = self.base / "jobs.log", self.base / "log"
        patcher = mock.patch.dict(os.environ, {"FLEET_USAGE_STATE_DIR": str(self.base / "usage"),
                                               "XDG_STATE_HOME": str(self.base / "state")})
        patcher.start()
        self.addCleanup(patcher.stop)
        import dispatch_completion_join as J
        self.J = J
        usage_cache._write("claude", {"used": 1}, 100, 100, credential_generation="g")
        self.meta = {"worker_type": "owner", "dispatch_depth": "1", "harness": "claude", "model": "model-x",
                     "model_profile": "deep", "attempt_id": "att-join", "owner_route_id": "rt-fixture",
                     "launch_started": "1", "supervisor_lease": "flock-v1",
                     "supervisor_lease_file": str(self.base / "lease"), "registered_worker": "1",
                     "attempt_schema_version": "2", "transport": "headless",
                     "execution_surface": "registered-headless", "fallback_hop": "same-harness-headless",
                     "pid": "999999999", "pid_start": "1", "log_file": str(self.log),
                     "quota_scope": "scope-a"}

    def open_row(self, log_row, meta=None):
        meta = {**self.meta, **(meta or {})}
        self.log.write_text(json.dumps(log_row) + "\n")
        self.jobs.write_text("2026-11-01T00:00:00Z\topen\t%s\t%s\tstage\t%s\n" % (
            self.base / "repo", self.base / "repo", ",".join(f"{k}={v}" for k, v in meta.items())))
        return self.J.exact_attempt_row(self.jobs, meta["attempt_id"])

    def settle(self, log_row):
        J = self.J
        row = self.open_row(log_row)
        quiescent = types.SimpleNamespace(state="quiescent", reason="fixture-exited")
        with mock.patch.object(dispatch_contract, "supervisor_lease_is_held", return_value=False), \
                mock.patch.object(J, "attempt_process_quiescence", return_value=quiescent), \
                mock.patch.object(J, "materialize_after_terminal_close", return_value=None):
            out = J.settle_exited_owner_terminal(row, jobs=self.jobs)
        return out, J.exact_attempt_row(self.jobs, "att-join")

    def state(self, scope="scope-a"):
        return usage_cache.read_provider("claude", "claude", "model-x", scope)["state"]

    def test_a_refusal_closed_by_the_join_is_recorded(self):
        out, row = self.settle(self.rejected)
        self.assertEqual((out, row.status, row.metadata["note"], row.metadata["reconcile_reason"]),
                         ("", "done", "dead-runtime-error", "runtime-model-rejected:model-x"))
        self.assertEqual(self.state(), "unavailable")
        self.assertEqual(self.state("scope-b"), "unknown")

    def test_a_success_closed_by_the_join_lifts_the_refusal_and_a_replay_leaves_it_lifted(self):
        usage_cache.record_model_outcome("claude", "claude", "model-x", "scope-a", "unavailable")
        self.assertEqual(self.state(), "unavailable")
        out, row = self.settle(self.passed)
        self.assertEqual((out, row.metadata["note"]), ("", "completed-supervisor"))
        self.assertEqual(self.state(), "usable")
        # Observing the same end again through the supervisor closes nothing and records nothing.
        with mock.patch.object(usage_cache, "record_model_outcome",
                               side_effect=AssertionError("recorded twice")):
            replay = T.reconcile_supervisor_terminal(
                self.jobs, "att-join", T.classify_claude_result(self.passed, 0))
        self.assertEqual(replay, "already-terminal")
        self.assertEqual(self.state(), "usable")

    def test_an_old_refusal_observed_again_after_a_newer_answer_does_not_come_back(self):
        usage_cache.record_model_outcome("claude", "claude", "model-x", "scope-a", "unavailable")
        self.settle(self.passed)
        self.assertEqual(self.state(), "usable")
        # The refused end of an attempt that was already closed is seen again.
        with mock.patch.object(usage_cache, "record_model_outcome",
                               side_effect=AssertionError("recorded twice")):
            again = T.reconcile_supervisor_terminal(
                self.jobs, "att-join", T.classify_claude_result(
                    {**self.rejected, "api_error_status": 404}, 1))
        self.assertIn(again, {"already-terminal", "terminal-conflict"})
        self.assertEqual(self.state(), "usable")

    def test_a_record_that_fails_never_changes_what_the_join_closed(self):
        with mock.patch.object(usage_cache, "record_model_outcome", side_effect=RuntimeError("boom")):
            out, row = self.settle(self.rejected)
        self.assertEqual((out, row.status, row.metadata["reconcile_reason"]),
                         ("", "done", "runtime-model-rejected:model-x"))

    def test_the_support_settlement_writes_the_same_record(self):
        J = self.J
        meta = {"worker_type": "worker"}
        for log_row, expected, prior in ((self.rejected, "unavailable", None), (self.passed, "usable", "unavailable")):
            if prior:
                usage_cache.record_model_outcome("claude", "claude", "model-x", "scope-a", prior)
            row = self.open_row(log_row, meta)
            terminal = T.classify_supervisor_log(self.log, "claude")
            classification = J.ExactReviewClassification(
                "done", terminal.reconcile_reason, terminal.note, "typed-close", terminal.evidence())
            identity = dispatch_contract.launched_attempt_identity(row.raw.split("\t"))
            with mock.patch.object(J, "_support_cas_matches", return_value=True), \
                    mock.patch.object(J, "materialize_after_terminal_close", return_value=None):
                out = J.apply_exact_route_free_support_classification(
                    row, jobs=self.jobs, classification=classification, selected_identity=identity,
                    expected_log_file=str(self.log), expected_pid=999999999, expected_pid_start="1",
                    expected_pgid=0)
            self.assertEqual(out, "support-settlement-committed")
            self.assertEqual(self.state(), expected)


if __name__ == "__main__":
    unittest.main()
