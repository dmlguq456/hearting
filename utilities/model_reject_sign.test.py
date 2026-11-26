#!/usr/bin/env python3
"""A confirmed model refusal is named in the terminal sign and leaves one per-model line.

Invented envelopes only, an isolated state home and usage-state directory, no real account. The
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
import subprocess
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
import model_outcome_record as M  # noqa: E402
capacity_probe = _load("capacity_probe", ROOT / "utilities" / "harness-capacity.py")

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




REGISTRY_ROW = (
    "2026-08-06T00:00:00Z\topen\t{repo}\t{repo}\tstage\t"
    "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
    "execution_surface=registered-headless,registered_worker=1,"
    "fallback_hop=same-harness-headless,harness={harness},"
    "attempt_id={attempt},parent_attempt_id=att-parent,model={model}{extra}\n"
)

REFUSED_CLAUDE = lambda model: T.classify_claude_result(  # noqa: E731
    claude_result(404, "API Error: 404 " + NOT_FOUND % model), 1)
REFUSED_CODEX = T.codex_turn_failure_terminal({"codex_error_info": "modelNotFound"})
PASSED = T.SupervisorTerminal("completed-supervisor", "pass", "claude-result", "exact-final-handoff", "0")


class Isolated(unittest.TestCase):
    """An isolated state home and usage-cache directory; rows are closed through the real registry."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / "repo").mkdir()
        self.jobs = self.base / "jobs.log"
        self.usage = self.base / "usage"
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base / "state"),
                                               "FLEET_USAGE_STATE_DIR": str(self.usage)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.count = 0

    def open_row(self, harness, model, scope="", source=""):
        self.count += 1
        attempt = f"att-{self.count}"
        extra = f",quota_scope={scope},quota_scope_kind=claude-subscription-v2" if scope else ""
        extra += f",model_source={source}" if source else ""
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write(REGISTRY_ROW.format(repo=self.base / "repo", harness=harness,
                                             attempt=attempt, model=model, extra=extra))
        return attempt

    def close(self, harness, model, terminal, scope="", source=""):
        return T.reconcile_supervisor_terminal(
            self.jobs, self.open_row(harness, model, scope, source), terminal)

    def lines(self, harness):
        path = Path(M.state_dir()) / f"{harness}.json"
        return json.loads(path.read_text())["providers"] if path.exists() else {}

    def state(self, harness, model, scope="unscoped"):
        return M.read(harness, harness, model, scope)["state"]

    def usage_cache_files(self):
        return sorted(p.name for p in self.usage.glob("*")) if self.usage.exists() else []


class RecordTest(Isolated):
    def test_a_confirmed_refusal_leaves_one_line_on_the_launched_model_and_account(self):
        self.assertEqual(self.close("claude", "claude-x-1", REFUSED_CLAUDE("claude-x-1"), "scope-a"), "closed")
        self.assertEqual(len(self.lines("claude")), 1)
        (row,) = self.lines("claude").values()
        self.assertEqual({k: row[k] for k in ("provider", "model", "account_scope", "state", "reason", "source")},
                         {"provider": "claude", "model": "claude-x-1", "account_scope": "scope-a",
                          "state": "unavailable", "reason": "model-unavailable", "source": "registry-close"})
        self.assertIsInstance(row["observed_at"], float)
        self.assertEqual(self.state("claude", "claude-x-1", "scope-a"), "unavailable")

    def test_codex_takes_the_same_path_and_is_unscoped(self):
        self.close("codex", "gpt-x-1", REFUSED_CODEX)
        (row,) = self.lines("codex").values()
        self.assertEqual((row["provider"], row["model"], row["account_scope"], row["state"]),
                         ("codex", "gpt-x-1", "unscoped", "unavailable"))

    def test_claude_records_with_no_usage_cache_at_all(self):
        # The record has a file of its own; the usage cache and its credential binding are not needed.
        self.close("claude", "claude-x-1", REFUSED_CLAUDE("claude-x-1"), "scope-a")
        self.assertEqual(self.state("claude", "claude-x-1", "scope-a"), "unavailable")
        self.assertEqual(self.usage_cache_files(), [])

    def test_a_refusal_that_points_at_another_model_records_nothing(self):
        self.close("claude", "claude-x-1", REFUSED_CLAUDE("claude-other-2"), "scope-a")
        self.close("claude", "opus", REFUSED_CLAUDE("claude-opus-9"), "scope-a")  # alias vs resolved: cannot tell
        self.assertEqual(self.lines("claude"), {})

    def test_two_accounts_never_share_a_line(self):
        self.close("claude", "claude-x-1", REFUSED_CLAUDE("claude-x-1"), "scope-a")
        self.assertEqual(self.state("claude", "claude-x-1", "scope-b"), "unknown")
        self.close("claude", "claude-x-1", PASSED, "scope-b")
        self.assertEqual(self.state("claude", "claude-x-1", "scope-a"), "unavailable")
        self.assertEqual(self.state("claude", "claude-x-1", "scope-b"), "unknown")

    def test_a_later_answer_lifts_the_refusal_and_otherwise_writes_nothing(self):
        path = Path(M.state_dir()) / "claude.json"
        with mock.patch.object(M, "_edit", wraps=M._edit) as edit:
            self.close("claude", "claude-x-1", PASSED, "scope-a")  # nothing to lift: no lock, no write
        self.assertEqual(edit.call_count, 0)
        self.assertFalse(path.exists())
        self.close("claude", "claude-x-1", REFUSED_CLAUDE("claude-x-1"), "scope-a")
        self.assertEqual(self.state("claude", "claude-x-1", "scope-a"), "unavailable")
        self.close("claude", "claude-x-1", PASSED, "scope-a")
        self.assertEqual(self.state("claude", "claude-x-1", "scope-a"), "usable")
        # A handed-off FAIL or BLOCKED is the model answering too.
        for ending in (T.SupervisorTerminal("dead-worker-fail", "fail", "claude-result", "worker-reported-fail", "0"),
                       T.SupervisorTerminal("dead-worker-blocked", "blocked", "claude-result",
                                            "worker-reported-blocked", "0")):
            self.close("claude", "claude-x-1", REFUSED_CLAUDE("claude-x-1"), "scope-a")
            self.close("claude", "claude-x-1", ending, "scope-a")
            self.assertEqual(self.state("claude", "claude-x-1", "scope-a"), "usable")

    def test_other_failures_and_other_harnesses_leave_no_line(self):
        self.close("claude", "claude-x-1", T.classify_claude_result(claude_result(429, "rate_limit_error model"), 1), "s")
        self.close("claude", "claude-x-1", T.classify_claude_result(claude_result(500, "API Error: 500"), 1), "s")
        shaped = T.SupervisorTerminal("dead-runtime-error", "runtime", "x", "runtime-model-rejected:m-1", "1")
        self.close("opencode", "p/m-1", shaped)  # OpenCode records through its own path
        self.assertFalse((Path(M.state_dir())).exists())
        self.assertEqual(self.usage_cache_files(), [])

    def test_a_record_that_cannot_be_kept_never_changes_the_close(self):
        for patched in ("record_unavailable", "note_row_closed"):
            with self.subTest(patched), mock.patch.object(M, patched, side_effect=RuntimeError("boom")):
                attempt = self.open_row("codex", "gpt-x-1")
                self.assertEqual(T.reconcile_supervisor_terminal(self.jobs, attempt, REFUSED_CODEX), "closed")
                fields = [l for l in self.jobs.read_text().splitlines() if f"attempt_id={attempt}," in l][0].split("\t")
                meta = dispatch_contract.parse_registry_metadata(fields[5])
                self.assertEqual((fields[1], meta["note"], meta["failure_class"], meta["reconcile_reason"]),
                                 ("done", "dead-runtime-error", "runtime", "runtime-model-rejected"))

    def test_the_lines_change_no_selection_or_display_reader(self):
        import dispatch_capacity_evidence as E
        self.close("claude", "claude-x-1", REFUSED_CLAUDE("claude-x-1"), "scope-a")
        self.close("codex", "gpt-x-1", REFUSED_CODEX)
        for harness in ("claude", "codex"):
            self.assertIsNone(E.model_hold(harness, "deep"))
        source = (ROOT / "tools" / "fleet" / "collectors" / "__init__.py").read_text()
        self.assertEqual(re.findall(r"usage_cache\.provider_records\(([^,)]*)", source), ['"opencode"'])

    def test_the_one_reader_hands_opencode_to_its_own(self):
        usage_cache.record_provider("opencode", "p", "m", "a", "unavailable", reason="model-unavailable", now=100)
        self.assertEqual(M.read("opencode", "p", "m", "a", now=200)["state"], "unavailable")
        self.assertEqual(M.read("opencode", "p", "other", "a", now=200)["state"], "unknown")
        self.assertEqual(M.read("claude", "claude", "m", "a")["state"], "unknown")


class AnswerTest(Isolated):
    """Which closed rows count as the model answering, and which model a row names."""

    def refuse_then(self, harness, model, ending, scope=""):
        refused = REFUSED_CLAUDE(model) if harness == "claude" else REFUSED_CODEX
        self.close(harness, model, refused, scope)
        self.close(harness, model, ending, scope)
        return self.state(harness, model, scope or "unscoped")

    def test_an_inherited_launch_records_nothing_but_the_sign_still_names_the_model(self):
        refused = REFUSED_CLAUDE("claude-x-1")
        self.assertEqual(refused.reconcile_reason, "runtime-model-rejected:claude-x-1")
        # The launch's own provenance says no model was settled, whatever the model column holds.
        self.close("claude", "claude-x-1", refused, "scope-a", source="inherit")
        self.close("codex", "gpt-x-1", REFUSED_CODEX, source="inherit")
        self.assertFalse(Path(M.state_dir()).exists())

    def test_a_settled_model_is_recorded_whatever_it_is_called(self):
        # `model_source` says the name was chosen, so a name that merely looks like a placeholder is real.
        for source in ("explicit", "profile", "pin"):
            self.close("codex", "default", REFUSED_CODEX, source=source)
            self.assertEqual(self.state("codex", "default"), "unavailable", source)

    def test_without_provenance_the_placeholder_names_still_skip(self):
        for name in ("inherit", "Inherit", "default", "runtime-default", "unknown"):
            self.close("claude", name, REFUSED_CLAUDE(name), "scope-a")
            self.close("codex", name, REFUSED_CODEX)
        self.assertFalse(Path(M.state_dir()).exists())
        self.close("codex", "gpt-x-1", REFUSED_CODEX)
        self.assertEqual(list(self.lines("codex")), ['["codex","gpt-x-1","unscoped"]'])

    def test_a_handoff_that_a_matched_block_breaks_is_the_model_answering(self):
        bad_pass = T.classify_claude_result(
            {"type": "result", "subtype": "success",
             "result": "artifact: -\nverdict: PASS\nblocker: still broken"}, 0)
        self.assertEqual((bad_pass.note, bad_pass.reconcile_reason), ("dead-contract", "pass-blocker-not-none"))
        self.assertEqual(self.refuse_then("claude", "claude-x-1", bad_pass, "scope-a"), "usable")
        self.assertEqual(self.refuse_then("codex", "gpt-x-1", T.classify_codex_result("finished\n", 0)),
                         "unavailable")  # prose without a handoff: see the next test

    def test_a_completion_with_no_reply_text_is_not_an_answer(self):
        for label, ending in (
                ("claude result without text", T.classify_claude_result({"type": "result", "subtype": "success"}, 0)),
                ("codex completion without text", T.classify_codex_result(None, 0))):
            with self.subTest(label):
                self.assertEqual((ending.note, ending.reconcile_reason), ("dead-contract", "final-handoff-invalid"))
        self.assertEqual(self.refuse_then(
            "claude", "claude-x-1", T.classify_claude_result({"type": "result", "subtype": "success"}, 0), "s"),
            "unavailable")
        self.assertEqual(self.refuse_then("codex", "gpt-x-1", T.classify_codex_result(None, 0)), "unavailable")

    def test_an_end_that_does_not_show_the_model_answered_keeps_the_refusal(self):
        ends = {
            "capacity": T.classify_claude_result(claude_result(429, "rate_limit_error"), 1),
            "auth": T.classify_claude_result(claude_result(401, "authentication_error"), 1),
            "runtime error": T.classify_claude_result(claude_result(500, "API Error: 500"), 1),
            "process exit": T.classify_codex_result("x", 1),
            "supervisor error": T.classify_supervisor_error("claude", "terminal-event-missing"),
            "protocol": T.classify_supervisor_error("claude", "missing-protocol-json"),
        }
        self.close("claude", "claude-x-1", REFUSED_CLAUDE("claude-x-1"), "scope-a")
        for label, terminal in ends.items():
            with self.subTest(label):
                self.close("claude", "claude-x-1", terminal, "scope-a")
                self.assertEqual(self.state("claude", "claude-x-1", "scope-a"), "unavailable")


class StoreIsolationTest(Isolated):
    """Nothing the model record does opens or changes a usage cache file."""

    def spy(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.usage.glob("*.json")}

    def test_no_model_record_action_touches_a_usage_cache_file(self):
        for harness in ("claude", "codex"):
            usage_cache._write(harness, {"windows": [["5h", 1], ["7d", 1]]}, 100, 100,
                               **({"credential_generation": "g"} if harness == "claude" else {}))
        import capacity_probe
        control = {h: capacity_probe._cached_score(h, 201, 900) for h in ("claude", "codex")}
        before = self.spy()
        opened = []
        real_open = open

        def watching(path, *args, **kwargs):
            if str(path).startswith(str(self.usage)):
                opened.append(str(path))
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", watching):
            self.close("claude", "claude-x-1", REFUSED_CLAUDE("claude-x-1"), "scope-a")   # refusal
            self.close("codex", "gpt-x-1", REFUSED_CODEX)
            self.close("claude", "claude-x-1", PASSED, "scope-a")                         # lift
            M.read("claude", "claude", "claude-x-1", "scope-a")                           # read
            M.record_unavailable("codex", "codex", "gpt-x-2", "unscoped", deadline=time.monotonic() - 1)  # dropped
            holder = os.open(str(Path(M.state_dir()) / ".codex.lock"), os.O_RDWR)
            fcntl.flock(holder, fcntl.LOCK_EX)                                             # contention
            try:
                with mock.patch.object(M, "LOCK_WAIT", 0.05):
                    M.note_row_closed({"harness": "codex", "model": "gpt-x-1", "model_source": "explicit",
                                       "note": "dead-runtime-error", "failure_class": "runtime",
                                       "reconcile_reason": "runtime-model-rejected"})
            finally:
                os.close(holder)
            with mock.patch.object(M.tempfile, "mkstemp", side_effect=OSError("disk")):    # failure
                M.record_unavailable("codex", "codex", "gpt-x-3", "unscoped")
        self.assertEqual(opened, [])
        self.assertEqual(self.spy(), before)
        self.assertEqual({h: capacity_probe._cached_score(h, 201, 900) for h in ("claude", "codex")}, control)
        self.assertNotEqual(control["codex"], (None, None))

    def test_a_usage_writer_between_record_actions_is_always_current(self):
        usage_cache._write("codex", {"windows": [["5h", 1], ["7d", 1]]}, 100, 100)
        self.close("codex", "gpt-x-1", REFUSED_CODEX)
        usage_cache._write("codex", {"windows": [["5h", 99], ["7d", 99]]}, 200, 200)
        usage_cache.record_attempt("codex", now=210)
        self.close("codex", "gpt-x-2", REFUSED_CODEX)
        self.close("codex", "gpt-x-1", PASSED)
        item = json.loads((self.usage / "codex.json").read_text())
        self.assertEqual(item["payload"], {"windows": [["5h", 99], ["7d", 99]]})
        self.assertEqual((item["fetched_at"], item["attempted_at"]), (200, 210))
        self.assertEqual(item.get("providers", {}), {})  # the usage file never holds a model line
        self.assertEqual((self.state("codex", "gpt-x-1"), self.state("codex", "gpt-x-2")),
                         ("usable", "unavailable"))
        self.assertEqual(capacity_probe._cached_score("codex", 211, 900), (1.0, "cache:11s"))


def _helper_script():
    return r'''
import os, sys, time
sys.path.insert(0, sys.argv[1])
import model_outcome_record as M
mode, model, mark = sys.argv[2], sys.argv[3], sys.argv[4]
if mode == "hold":
    real = os.replace
    def replace(src, dst):
        open(mark + ".ready", "w").close()
        while not os.path.exists(mark + ".go"):
            time.sleep(0.01)
        return real(src, dst)
    M.os.replace = replace
started = time.monotonic()
kept = M.record_unavailable("codex", "codex", model, "unscoped", "model-unavailable", source="t")
print("%s %.3f" % (kept, time.monotonic() - started))
'''


class StoreLockTest(Isolated):
    """The record's own lock: a late writer is dropped, never racing; one wait per close."""

    def setUp(self):
        super().setUp()
        self.helper = self.base / "helper.py"
        self.helper.write_text(_helper_script())

    def child(self, mode, model, mark):
        env = {"PATH": os.environ.get("PATH", ""), "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"]}
        return subprocess.Popen([sys.executable, "-I", "-B", str(self.helper), str(ROOT / "utilities"),
                                 mode, model, str(mark)], env=env, stdout=subprocess.PIPE, text=True)

    def thread_owner(self, model, release_after):
        """A writer thread that holds the lock and is stopped just before os.replace."""
        entered, release = threading.Event(), threading.Event()
        real = os.replace

        def replace(src, dst):
            if threading.current_thread().name == "owner":
                entered.set()
                release.wait(10)
            return real(src, dst)

        results = []
        owner = threading.Thread(name="owner", target=lambda: results.append(
            M.record_unavailable("codex", "codex", model, "unscoped", "model-unavailable", source="t")))
        patcher = mock.patch.object(M.os, "replace", side_effect=replace)
        patcher.start()
        self.addCleanup(patcher.stop)
        owner.start()
        self.assertTrue(entered.wait(5))
        return owner, release, results

    def assert_intact(self, *models):
        raw = (Path(M.state_dir()) / "codex.json").read_text()
        item = json.loads(raw)  # whole and valid
        self.assertEqual(sorted(row["model"] for row in item["providers"].values()), sorted(models))

    def test_a_thread_owner_stopped_past_the_wait_costs_a_second_writer_one_wait_and_overwrites_nothing(self):
        owner, release, results = self.thread_owner("model-a", 0)
        started = time.monotonic()
        kept = M.record_unavailable("codex", "codex", "model-b", "unscoped", "model-unavailable")
        waited = time.monotonic() - started
        self.assertFalse(kept)
        self.assertGreaterEqual(waited, 0.95)
        self.assertLess(waited, 1.5)          # the wait is one LOCK_WAIT, not two
        release.set()
        owner.join(10)
        self.assertEqual(results, [True])
        self.assert_intact("model-a")          # the late one dropped its line; the owner's stands
        self.assertTrue(M.record_unavailable("codex", "codex", "model-c", "unscoped", "r"))
        self.assert_intact("model-a", "model-c")

    def test_a_process_owner_stopped_past_the_wait_costs_a_second_writer_one_wait_and_overwrites_nothing(self):
        mark = self.base / "mark"
        owner = self.child("hold", "model-a", mark)
        self.addCleanup(owner.kill)
        for _ in range(500):
            if (self.base / "mark.ready").exists():
                break
            time.sleep(0.01)
        self.assertTrue((self.base / "mark.ready").exists())
        for who in ("thread", "other process"):
            started = time.monotonic()
            if who == "thread":
                kept = M.record_unavailable("codex", "codex", "model-b", "unscoped", "r")
            else:
                other = self.child("write", "model-c", self.base / "other")
                kept = other.communicate(timeout=10)[0].split()[0] == "True"
            waited = time.monotonic() - started
            self.assertFalse(kept, who)
            self.assertGreaterEqual(waited, 0.95, who)
            self.assertLess(waited, 1.6, who)
        (self.base / "mark.go").write_text("")
        self.assertEqual(owner.communicate(timeout=10)[0].split()[0], "True")
        self.assertEqual(owner.returncode, 0)
        self.assert_intact("model-a")

    def test_two_different_models_recorded_in_a_short_contention_both_stay(self):
        owner, release, results = self.thread_owner("model-a", 0)
        done = []
        second = threading.Thread(target=lambda: done.append(
            M.record_unavailable("codex", "codex", "model-b", "unscoped", "r")))
        second.start()
        time.sleep(0.2)                         # well inside the wait
        self.assertEqual(done, [])              # it is waiting, not racing
        release.set()
        owner.join(10)
        second.join(10)
        self.assertEqual((results, done), ([True], [True]))
        self.assert_intact("model-a", "model-b")

    def test_two_processes_recording_different_models_at_once_both_stay(self):
        children = [self.child("write", f"model-{n}", self.base / f"m{n}") for n in range(4)]
        for child in children:
            self.assertEqual(child.communicate(timeout=20)[0].split()[0], "True")
        self.assert_intact(*[f"model-{n}" for n in range(4)])

    def test_a_close_waits_once_whatever_it_calls(self):
        meta = {"harness": "codex", "model": "model-a", "model_source": "explicit"}
        refused = {**meta, "note": "dead-runtime-error", "failure_class": "runtime",
                   "reconcile_reason": "runtime-model-rejected"}
        answered = {**meta, "note": "completed-supervisor", "failure_class": "pass"}
        M.record_unavailable("codex", "codex", "model-a", "unscoped", "r")
        holder = os.open(str(Path(M.state_dir()) / ".codex.lock"), os.O_RDWR)
        fcntl.flock(holder, fcntl.LOCK_EX)
        self.addCleanup(os.close, holder)
        for label, row in (("refusal", refused), ("lift", answered)):
            started = time.monotonic()
            self.assertFalse(M.note_row_closed(row))
            took = time.monotonic() - started
            self.assertGreaterEqual(took, 0.95, label)
            self.assertLess(took, 1.4, label)
        # An answer with no refusal on file takes no lock at all, even while it is held.
        started = time.monotonic()
        M.note_row_closed({**answered, "model": "model-none"})
        self.assertLess(time.monotonic() - started, 0.2)

    def test_a_forked_child_does_not_inherit_a_hold(self):
        M.record_unavailable("codex", "codex", "model-a", "unscoped", "r")
        lock = os.open(str(Path(M.state_dir()) / ".codex.lock"), os.O_RDWR)
        fcntl.flock(lock, fcntl.LOCK_EX)
        self.addCleanup(os.close, lock)
        read_end, write_end = os.pipe()
        pid = os.fork()
        if pid == 0:  # child: it holds nothing, so a competing parent keeps it out
            code = 3
            try:
                os.close(read_end)
                M.LOCK_WAIT = 0.2
                kept = M.record_unavailable("codex", "codex", "model-child", "unscoped", "r")
                os.write(write_end, b"kept" if kept else b"dropped")
                code = 0
            finally:
                os._exit(code)
        os.close(write_end)
        data = os.read(read_end, 16)
        os.close(read_end)
        _, status = os.waitpid(pid, 0)
        self.assertEqual((os.WEXITSTATUS(status), data), (0, b"dropped"))
        self.assert_intact("model-a")

    def test_a_file_a_newer_version_wrote_is_not_replaced(self):
        directory = Path(M.state_dir())
        directory.mkdir(parents=True)
        path = directory / "codex.json"
        for version in (2, 7):
            with self.subTest(version=version):
                theirs = json.dumps({"schema_version": version, "harness": "codex",
                                     "providers": {"theirs": {"model": "model-a", "state": "unavailable"}}})
                path.write_text(theirs)
                self.assertFalse(M.record_unavailable("codex", "codex", "model-b", "unscoped", "r"))
                self.assertFalse(M.lift_unavailable("codex", "codex", "model-a", "unscoped"))
                self.assertFalse(M.note_row_closed({"harness": "codex", "model": "model-b", "model_source": "explicit",
                                                    "note": "dead-runtime-error", "failure_class": "runtime",
                                                    "reconcile_reason": "runtime-model-rejected"}))
                self.assertEqual(path.read_text(), theirs)
                self.assertEqual(M.read("codex", "codex", "model-b", "unscoped")["state"], "unknown")
        # Only a larger whole number counts: anything else is replaced as before.
        for other in ('{"schema_version": 0, "harness": "codex", "providers": {}}',
                      '{"schema_version": "9", "harness": "codex", "providers": {}}',
                      '{"schema_version": true, "harness": "codex", "providers": {}}',
                      '{"harness": "codex", "providers": {}}',
                      '{"schema_version": 1, "harness": "claude", "providers": {}}', "[]", "{"):
            with self.subTest(other=other):
                path.write_text(other)
                self.assertTrue(M.record_unavailable("codex", "codex", "model-b", "unscoped", "r"))
                self.assertEqual(json.loads(path.read_text())["schema_version"], M.SCHEMA_VERSION)

    def test_a_broken_file_or_lock_never_raises_or_blocks(self):
        directory = Path(M.state_dir())
        directory.mkdir(parents=True)
        (directory / "codex.json").write_text("{")
        self.assertTrue(M.record_unavailable("codex", "codex", "model-a", "unscoped", "r"))
        self.assert_intact("model-a")
        (directory / "claude.json").write_text("[]")
        self.assertTrue(M.record_unavailable("claude", "claude", "model-a", "s", "r"))
        (directory / ".codex.lock").unlink()
        (directory / ".codex.lock").mkdir()      # a lock path that cannot be opened for locking
        started = time.monotonic()
        self.assertFalse(M.record_unavailable("codex", "codex", "model-b", "unscoped", "r"))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assert_intact("model-a")


class OpenCodeUnchangedTest(unittest.TestCase):
    """OpenCode's record and reader are the code before this piece: same bytes, same time."""

    def test_opencode_bytes_are_those_of_the_code_before(self):
        initial, ops = _opencode_steps()
        with tempfile.TemporaryDirectory() as temp:
            for number, start in enumerate(initial):
                with self.subTest(start=start):
                    state = Path(temp) / f"oc{number}"
                    state.mkdir()
                    with mock.patch.dict(os.environ, {"FLEET_USAGE_STATE_DIR": str(state)}):
                        if start is not None:
                            (state / "opencode.json").write_text(start)
                        seen = []
                        for op in ops:
                            op(usage_cache)
                            seen.append(hashlib.sha256((state / "opencode.json").read_bytes()).hexdigest()[:16])
                    self.assertEqual(seen, OPENCODE_BYTES_BEFORE[number])

    def test_an_opencode_outcome_takes_no_lock_wait_even_with_the_old_lock_files_held(self):
        import opencode_runtime
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "usage"
            state.mkdir()
            holder = os.open(str(state / ".opencode.lock"), os.O_CREAT | os.O_RDWR)
            fcntl.flock(holder, fcntl.LOCK_EX)
            try:
                observation = types.SimpleNamespace(state="usable", scope="s", credential_generation="g")
                with mock.patch.dict(os.environ, {"FLEET_USAGE_STATE_DIR": str(state)}):
                    started = time.monotonic()
                    kept = opencode_runtime.record_model_outcome(
                        "opencode-go/m-1", success=True, account_observation=observation)
                    took = time.monotonic() - started
            finally:
                os.close(holder)
        self.assertTrue(kept)
        self.assertLess(took, 0.5)


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


class ClosePathTest(Isolated):
    """Every close that ends a row on the model's answer keeps the same record, once."""

    rejected = {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 404,
                "result": 'API Error: 404 {"type":"not_found_error","message":"model: model-x"}'}
    passed = {"type": "result", "subtype": "success", "is_error": False,
              "result": "artifact: -\nverdict: PASS\nblocker: none"}
    failed = {"type": "result", "subtype": "success", "is_error": False,
              "result": "artifact: -\nverdict: FAIL\nblocker: tests failed"}

    def setUp(self):
        super().setUp()
        import dispatch_completion_join as J
        self.J = J
        self.log = self.base / "log"
        self.meta = {"worker_type": "owner", "dispatch_depth": "1", "harness": "claude", "model": "model-x",
                     "model_source": "profile", "model_profile": "deep", "attempt_id": "att-join",
                     "owner_route_id": "rt-fixture", "launch_started": "1", "supervisor_lease": "flock-v1",
                     "supervisor_lease_file": str(self.base / "lease"), "registered_worker": "1",
                     "attempt_schema_version": "2", "transport": "headless",
                     "execution_surface": "registered-headless", "fallback_hop": "same-harness-headless",
                     "pid": "999999999", "pid_start": "1", "log_file": str(self.log),
                     "quota_scope": "scope-a"}
        patcher = mock.patch.object(M, "note_row_closed", wraps=M.note_row_closed)
        self.noted = patcher.start()
        self.addCleanup(patcher.stop)

    def open_log_row(self, log_row, meta=None):
        meta = {**self.meta, **(meta or {})}
        self.log.write_text(json.dumps(log_row) + "\n")
        self.jobs.write_text("2026-11-01T00:00:00Z\topen\t%s\t%s\tstage\t%s\n" % (
            self.base / "repo", self.base / "repo", ",".join(f"{k}={v}" for k, v in meta.items())))
        return self.J.exact_attempt_row(self.jobs, meta["attempt_id"])

    def current(self):
        return self.J.exact_attempt_row(self.jobs, "att-join")

    def model(self):
        return self.state("claude", "model-x", "scope-a")

    def refuse(self):
        M.record_unavailable("claude", "claude", "model-x", "scope-a", "model-unavailable")
        self.assertEqual(self.model(), "unavailable")

    def settle(self, log_row):
        J = self.J
        row = self.open_log_row(log_row)
        quiescent = types.SimpleNamespace(state="quiescent", reason="fixture-exited")
        with mock.patch.object(dispatch_contract, "supervisor_lease_is_held", return_value=False), \
                mock.patch.object(J, "attempt_process_quiescence", return_value=quiescent), \
                mock.patch.object(J, "materialize_after_terminal_close", return_value=None):
            out = J.settle_exited_owner_terminal(row, jobs=self.jobs)
        return out, self.current()

    # -- the join's direct close and the support settlement -------------------------------------

    def test_a_refusal_closed_by_the_join_is_recorded(self):
        out, row = self.settle(self.rejected)
        self.assertEqual((out, row.status, row.metadata["note"], row.metadata["reconcile_reason"]),
                         ("", "done", "dead-runtime-error", "runtime-model-rejected:model-x"))
        self.assertEqual(self.model(), "unavailable")
        self.assertEqual(self.state("claude", "model-x", "scope-b"), "unknown")

    def test_a_success_closed_by_the_join_lifts_the_refusal_and_a_replay_leaves_it_lifted(self):
        self.refuse()
        out, row = self.settle(self.passed)
        self.assertEqual((out, row.metadata["note"]), ("", "completed-supervisor"))
        self.assertEqual(self.model(), "usable")
        calls = self.noted.call_count
        replay = T.reconcile_supervisor_terminal(self.jobs, "att-join", T.classify_claude_result(self.passed, 0))
        self.assertEqual(replay, "already-terminal")
        self.assertEqual((self.noted.call_count, self.model()), (calls, "usable"))

    def test_an_old_refusal_observed_again_after_a_newer_answer_does_not_come_back(self):
        self.refuse()
        self.settle(self.passed)
        calls = self.noted.call_count
        again = T.reconcile_supervisor_terminal(
            self.jobs, "att-join", T.classify_claude_result(self.rejected, 1))
        self.assertIn(again, {"already-terminal", "terminal-conflict"})
        self.assertEqual((self.noted.call_count, self.model()), (calls, "usable"))

    def test_a_record_that_fails_never_changes_what_the_join_closed(self):
        with mock.patch.object(M, "record_unavailable", side_effect=RuntimeError("boom")):
            out, row = self.settle(self.rejected)
        self.assertEqual((out, row.status, row.metadata["reconcile_reason"]),
                         ("", "done", "runtime-model-rejected:model-x"))

    def test_the_support_settlement_writes_the_same_record(self):
        J = self.J
        for log_row, expected, prior in ((self.rejected, "unavailable", False), (self.passed, "usable", True)):
            if prior:
                self.assertEqual(self.model(), "unavailable")
            row = self.open_log_row(log_row, {"worker_type": "worker"})
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
            self.assertEqual(self.model(), expected)

    # -- the closes that read the model's answer without the supervisor's classifier ------------

    def test_a_foreground_pass_closed_by_the_wrapper_lifts_the_refusal(self):
        J = self.J
        self.refuse()
        row = self.open_log_row(self.passed, {"worker_type": "worker"})
        classification = J.ExactReviewClassification("done", "exact-final-handoff", "completed-review",
                                                     "wrapper-pass")
        with mock.patch.object(J, "materialize_after_terminal_close", return_value=None):
            self.assertEqual(J.close_wrapper_pass(row, jobs=self.jobs, classification=classification), "")
        self.assertEqual((self.current().metadata["note"], self.model()), ("completed-review", "usable"))

    def test_a_foreground_fail_or_blocked_closed_from_the_log_lifts_the_refusal(self):
        import foreground_terminal as F
        for label, verdict in (("FAIL", "tests failed"), ("BLOCKED", "needs an answer")):
            with self.subTest(label):
                self.refuse()
                text = f"artifact: -\nverdict: {label}\nblocker: {verdict}"
                self.open_log_row({"type": "result", "subtype": "success", "is_error": False, "result": text},
                                  {"worker_type": "stage"})
                (self.base / ".agent_reports").mkdir(exist_ok=True)
                with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(self.base / ".agent_reports")}), \
                        mock.patch("dispatch_completion_join.materialize_after_terminal_close"):
                    settled = F.settle_foreground_exit(
                        self.jobs, "att-join", self.log, types.SimpleNamespace(failure=None, exit_code=0),
                        worktree=self.base / "repo", artifact_root=self.base / ".agent_reports",
                        worker_type="stage", legacy_close=lambda failure: None)
                self.assertTrue(settled["closed"], settled)
                self.assertEqual(self.model(), "usable")

    def test_a_foreground_process_failure_after_a_pass_is_not_an_answer(self):
        import foreground_terminal as F
        self.refuse()
        self.open_log_row(self.passed, {"worker_type": "stage"})
        (self.base / ".agent_reports").mkdir(exist_ok=True)
        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(self.base / ".agent_reports")}), \
                mock.patch("dispatch_completion_join.materialize_after_terminal_close"):
            settled = F.settle_foreground_exit(
                self.jobs, "att-join", self.log, types.SimpleNamespace(failure="nonzero-exit", exit_code=1),
                worktree=self.base / "repo", artifact_root=self.base / ".agent_reports",
                worker_type="stage", legacy_close=lambda failure: None)
        self.assertEqual((settled["note"], settled["closed"]), ("dead-nonzero-exit", True))
        self.assertEqual(self.model(), "unavailable")

    def test_the_route_completion_marker_close_hands_over_the_row_it_closed(self):
        # `capability-route complete` is exercised end to end by `MarkerModelRecordTest` in the route
        # suite; here, only that no helper re-reads the registry for the record any more.
        self.refuse()
        closed = {**self.meta, "note": "completed-marker", "failure_class": "pass"}
        self.assertTrue(M.note_row_closed(closed))
        self.assertEqual(self.model(), "usable")
        self.assertFalse(hasattr(M, "note_attempt_closed"))

    def test_the_close_primitives_are_the_only_callers_and_each_calls_it_once(self):
        source = (ROOT / "utilities" / "dispatch_contract_rows.py").read_text()
        self.assertEqual(source.count("_note_model_outcome(closed)"), 3)
        for path in (ROOT / "utilities").glob("*.py"):
            if ".test." in path.name or path.name in {"model_outcome_record.py", "dispatch_contract_rows.py",
                                                      "capability_route_completion.py"}:
                continue
            self.assertNotIn("note_row_closed", path.read_text(encoding="utf-8", errors="replace"), path.name)


if __name__ == "__main__":
    unittest.main()
