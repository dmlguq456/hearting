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

import importlib.util
import json
from pathlib import Path
import re
import tempfile
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


if __name__ == "__main__":
    unittest.main()
