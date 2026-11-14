#!/usr/bin/env python3
"""Hermetic measurement of what Hearting puts into a supervising session's context.

One synthetic supervisor scenario per harness (Claude, Codex, OpenCode): ten
registered depth-1 owner completions -- seven ordinary, one review that ends on
a disposition verdict (``수정 후 병합``), one work that waited for CI before it
finished, one failure -- and five typed prompts. Each source tree is imported in
a fresh process with a throwaway HOME, so the same driver measures the frozen
base tree and the changed tree.

Every carried text is rendered by the tree's own carriers: the Claude
``asyncRewake`` receipt (``classified_receipt``), the Claude prompt sweep hook,
the PostToolUse/Stop tool-boundary carrier, the Codex native queue body, the
Codex prompt receipts and the OpenCode turn carrier (``delivery_context``). Only
the native transport is synthetic: the Claude transcript events the runtime
writes when a hook's output reaches the model, in the orders counted on a real
supervisor session (idle wake, a long tool call during which the notification
waits in Claude's queue, a typed prompt). Codex consumes its queue item; OpenCode
records the body it was given.

Measured per completion: characters Hearting carried into the model context
(per kind), how many carriers carried that completion, the turns it woke, and
whether finishing it needed a follow-up command. Per typed prompt: the
characters the prompt hooks added. Nothing here reads a real session, registry
or transcript.

Usage::

    python3 utilities/supervisor_context_fixture.py --source-root <tree> --output <json>
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest import mock


PARENT_SID = "ses-supervisor-fixture"
PARENT_ATTEMPT = "att-supervisor-fixture-parent"
RECIPIENT_KIND = {"claude": "claude-parent-runtime", "codex": "codex-native-queue",
                  "opencode": "opencode-turn"}
# The scenario: kind of each completion and how its notice reaches the model.
# Claude orders are the ones counted on a real supervisor session: "idle" (the
# rewake wakes a resting session, then a tool and the turn end follow), "mid" (it
# lands during a tool call longer than the rewake's claim lease, waits in Claude's
# queue and is absorbed at the next tool boundary), "prompt" (no rewake armed; the
# next typed prompt carries it), "busy" (it lands while the model writes a long
# reply, waits in the queue past the lease and then starts the next turn).
COMPLETIONS = (
    ("normal", "idle"), ("normal", "mid"), ("disposition", "idle"), ("normal", "busy"),
    ("ci-wait", "mid"), ("normal", "prompt"), ("failure", "idle"), ("normal", "mid"),
    ("normal", "busy"), ("normal", "prompt"),
)
PROMPTS_AFTER = {1: "P1", 3: "P2", 5: "P3", 7: "P4", 9: "P5"}
CLAIM_LEASE_GAP_NS = 31 * 1_000_000_000
HEX64 = re.compile(r"\b[0-9a-f]{64}\b")
MARKER = re.compile(r"HEARTING_RECEIPT_V1 (delivery-[A-Za-z0-9._-]+) [0-9a-f]{64}")
SUMMARY_HEADS = ("[완료 요약]", "[Completion summary]")


def _report(kind: str, index: int) -> str:
    pr = 4100 + index
    head = f"{index:02d}" + "ab" * 19
    pr_line = (f"PR: https://github.com/example/hearting/pull/{pr} · head `{head}` · CI 15/15 통과 · "
               "GitHub mergeable MERGEABLE")
    if kind == "disposition":
        return (f"최종 판정: 수정 후 병합 — head {head[:12]}\n\n"
                "## 결론\n- P1 하나: 시험이 실제 경로를 거치지 않는다.\n"
                "## 실행한 시험\n- python3 tools/run-tests.py --select utilities/x.test.py: 14 passed\n"
                "## 해결하지 못한 것\n- P1 수정 필요(작성자에게)\n\n" + pr_line + "\n")
    if kind == "failure":
        return ("최종 판정: 실패\n\n## 바꾼 것\n- utilities/y.py 고침 시도\n"
                "## 검증\n- python3 tools/run-tests.py --select utilities/y.test.py: 3 failed\n"
                "## 남은 것\n- 실패 3건 원인 미확인\n")
    extra = "\n## CI\n- 15/15 통과(대기 18분)\n" if kind == "ci-wait" else ""
    return (f"최종 판정: 완료 — PR #{pr} head {head[:12]}\n\n"
            "## 바꾼 것\n- utilities/z.py: 같은 줄을 접는다 (+12/-3)\n"
            "## 시험\n- python3 tools/run-tests.py --select utilities/z.test.py: 21 passed\n"
            "## 하지 않은 것\n- 설치\n" + extra + "\n" + pr_line + "\n")


def _final_text(kind: str, report: Path) -> str:
    if kind == "disposition":
        return f"artifact: {report}\nverdict: 수정 후 병합 — P1 하나\nblocker: none"
    if kind == "failure":
        return f"artifact: {report}\nverdict: FAIL\nblocker: 시험 3건 실패"
    return f"artifact: {report}\nverdict: PASS\nblocker: none"


class Scenario:
    def __init__(self, source: Path, base: Path, harness: str):
        self.source, self.base, self.harness = source, base, harness
        self.repo = base / "repo"
        self.reports = base / "reports" / ".agent_reports"
        self.state = base / "state" / "dispatch"
        self.jobs = self.state / "jobs.log"
        self.claude_home = base / "claude-config"
        for path in (self.repo, self.reports, self.state, self.claude_home):
            path.mkdir(parents=True, exist_ok=True)
        self.state.chmod(0o700)
        self.jobs.write_text("", encoding="utf-8")
        self.jobs.chmod(0o600)
        os.environ.update({"AGENT_DISPATCH_JOBS": str(self.jobs), "CLAUDE_CONFIG_DIR": str(self.claude_home),
                           "FLEET_NOW_LANG": "ko"})
        os.chdir(self.repo)
        self.clock = [1_000_000_000_000]
        self.records: dict[str, dict] = {}
        self.items: list[dict] = []
        self.transcript_events: list[dict] = []
        self.carried: list[dict] = []  # one row per text that reached the model

    # -- setup -----------------------------------------------------------------
    def rows(self) -> list[str]:
        return [line for line in self.jobs.read_text(encoding="utf-8").splitlines() if line]

    def write_row(self, index: int, status: str, metadata: dict):
        rows = self.rows()
        line = "\t".join(["2026-11-14T00:00:%02dZ" % index, status, str(self.repo), str(self.repo),
                          "owner", ",".join(f"{k}={v}" for k, v in metadata.items())])
        if index < len(rows):
            rows[index] = line
        else:
            rows.append(line)
        self.jobs.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def build(self):
        import route_identity
        import dispatch_pending_delivery as pending
        import dispatch_supervisor_terminal as terminal
        from dispatch_terminal_commit import terminal_slot
        for index, (kind, pattern) in enumerate(COMPLETIONS):
            attempt = f"att-scc-{self.harness}-{index:02d}" + "0" * 16
            route = {"capability": "autopilot-code", "capability_mode": "dev",
                     "work_request": {"text": f"합성 과제 {index}: 감독 문맥 측정용 작업을 끝낸다."},
                     "artifact_root": str(self.reports), "cwd": str(self.repo)}
            route["route_hash"] = route_identity.route_hash(route)
            route["route_id"] = route_identity.route_id_from_hash(route["route_hash"])
            route_file = self.reports / (route["route_id"] + ".json")
            route_file.write_text(json.dumps(route), encoding="utf-8")
            report = self.reports / f"cycle-{index:02d}" / "final_report.md"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(_report(kind, index), encoding="utf-8")
            text = _final_text(kind, report)
            log = self.base / f"owner-{index:02d}.jsonl"
            log.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                       "result": text}) + "\n", encoding="utf-8")
            delivery_id = f"delivery-scc-{self.harness}-{index:02d}"
            metadata = {
                "attempt_schema_version": "2", "attempt_id": attempt, "dispatch_depth": "1",
                "registered_worker": "1", "execution_surface": "registered-headless",
                "launch_claimed": "1", "worker_type": "owner", "harness": "claude",
                "owner_harness": "claude", "workflow_completion": "runtime-v1",
                "artifact_root": str(self.reports), "route_file": str(route_file),
                "route_hash": route["route_hash"], "route_id": route["route_id"],
                "route_node": "one-shot", "parent_sid": PARENT_SID,
                "parent_attempt_id": PARENT_ATTEMPT, "parent_harness": self.harness,
                "parent_completion_delivery": RECIPIENT_KIND[self.harness],
                "delivery_recipient_kind": RECIPIENT_KIND[self.harness],
                "delivery_intent": "1", "delivery_id": delivery_id, "log_file": str(log),
            }
            self.write_row(index, "open", metadata)
            raw = terminal.classify_session_result(
                {"subtype": "success", "is_error": False, "result": text}, 0, runtime="claude")
            outcome = terminal.completed_owner_assessment(raw, text, jobs=self.jobs, attempt_id=attempt)
            metadata.update(note=outcome.note, failure_class=outcome.failure_class,
                            reconcile_reason=outcome.reconcile_reason)
            success = outcome.failure_class == "pass" and outcome.note.startswith("completed")
            row_revision = f"{index:02d}" * 32
            metadata["delivery_row_revision"] = row_revision
            self.write_row(index, "done", metadata)
            if success:
                # The completion controller's own sealing step (cost is not measured here).
                import dispatch_terminal_commit as commit
                slot = terminal_slot(self.reports, route["route_id"], attempt)
                slot.mkdir(parents=True, exist_ok=True)
                request = SimpleNamespace(route_file=route_file, jobs=self.jobs, owner_attempt_id=attempt)
                with mock.patch.object(commit, "_completion_cost", return_value=None):
                    commit._seal_completion_summary(request, route, str(report), slot)
            classification = "success" if success else "attention"
            child = {"attempt_id": attempt, "status": "done", "readiness": "ready",
                     "reason": "terminal-complete" if success else "terminal-failure-or-unclosed",
                     "required_action": "advance-completed" if success else "inspect-done-failure",
                     "harness": "claude", "delivery_classification": classification}
            receipt = {"schema_version": 2, "state": "ready", "parent_attempt_id": PARENT_ATTEMPT,
                       "job_registry": str(self.jobs), "children": [child],
                       "delivery_classification": classification}
            metadata["delivery_receipt_digest"] = pending._canonical_receipt_digest(receipt)
            self.write_row(index, "done", metadata)
            self.items.append({"index": index, "kind": kind, "pattern": pattern, "attempt": attempt,
                               "delivery_id": delivery_id, "receipt": receipt, "success": success,
                               "note": outcome.note, "row_revision": row_revision,
                               "reason": outcome.reconcile_reason, "route_id": route["route_id"]})
            self.records[delivery_id] = dict(
                recipient_kind=RECIPIENT_KIND[self.harness], recipient_key=PARENT_SID,
                delivery_id=delivery_id, session_generation="", session_generation_supported="0",
                attempt_ids=[attempt], parent_attempt_id=PARENT_ATTEMPT, route_id=route["route_id"],
                route_node="one-shot", receipt=receipt,
                receipt_digest=pending._canonical_receipt_digest(receipt),
                row_revisions={attempt: row_revision})

    def materialize(self, item):
        import dispatch_pending_delivery as pending
        pending.create(self.state, **self.records[item["delivery_id"]])

    # -- carried text ----------------------------------------------------------
    def carry(self, carrier: str, text: str, *, wake: bool = False, extra_turn: bool = False,
              prompt: str | None = None):
        if not text and not prompt:
            return
        ids = sorted(set(MARKER.findall(text)))
        self.carried.append({"carrier": carrier, "text": text, "chars": len(text), "ids": ids,
                             "wake": wake, "extra_turn": extra_turn, "prompt": prompt})

    def advance(self, ns: int):
        self.clock[0] += ns

    # -- Claude native transcript -----------------------------------------------
    def transcript_path(self) -> Path:
        from claude_project_dir import encode_project_dir
        path = self.claude_home / "projects" / encode_project_dir(str(self.repo)) / (PARENT_SID + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def transcript(self, event: dict):
        event = {"sessionId": PARENT_SID, "timestamp": "2026-11-14T00:00:00Z", **event}
        self.transcript_events.append(event)
        with self.transcript_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    @staticmethod
    def notification(text: str) -> str:
        # Claude Code's shape: the notification block, then the hook output in a reminder.
        return ("<task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>\n"
                "<system-reminder>\nStop hook blocking error from command \"PostToolUse:Bash\": "
                + text + "\n</system-reminder>")

    def payload(self, event: str, **extra) -> dict:
        return {"session_id": PARENT_SID, "transcript_path": str(self.transcript_path()),
                "cwd": str(self.repo), "hook_event_name": event, "prompt_id": "prompt-fixture", **extra}


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------

def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _claude(scenario: Scenario) -> None:
    import dispatch_pending_delivery as pending
    source = scenario.source
    rewake = _load(source / "hooks" / "dispatch-owner-rewake.py", "scc_rewake")
    sweep_hook = _load(source / "hooks" / "dispatch-session-sweep.py", "scc_sweep_hook")
    pmr = _load(source / "hooks" / "peer-message-record.py", "scc_peer_message_record")
    scenario.transcript_path().write_text("", encoding="utf-8")

    def rewake_emit(item) -> str:
        launch = rewake.Launch(attempt_id=item["attempt"], jobs=scenario.jobs,
                               session_id=PARENT_SID, armed="registry")
        metadata = {"delivery_id": item["delivery_id"], "parent_sid": PARENT_SID}
        claim = rewake.claim_arm(scenario.jobs, item["attempt"], PARENT_SID, fresh=True)
        if not isinstance(claim, rewake.ArmClaim):
            return ""
        success = item["success"]
        delivery = SimpleNamespace(status="done", row_revision=item["row_revision"],
                                   row_digest="d" * 64, owned_children=0, quiescent=True,
                                   advanced=False)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(rewake, "_incarnation_binding_matches", return_value=True), \
             mock.patch.object(rewake, "_gate_notices", return_value=[]), \
             mock.patch.object(rewake, "settle_arm", return_value=True), \
             mock.patch.object(rewake, "_ended", side_effect=lambda _claim, code, **_kw: code), \
             mock.patch.object(rewake, "current_delivery_state", return_value=delivery), \
             mock.patch.object(rewake, "_completion_evidence_current", return_value=True), \
             mock.patch.object(rewake, "delivery_classification",
                               return_value="success" if success else "attention"), \
             mock.patch.object(rewake, "delivery_required_action",
                               return_value="advance-completed" if success else "inspect-done-failure"), \
             mock.patch.object(rewake.sys, "stdout", stdout), mock.patch.object(rewake.sys, "stderr", stderr):
            win = rewake._carrier_one_claim(launch, metadata, arm=claim)
            if not isinstance(win, getattr(rewake, "ClaimWin", ())):
                return ""
            state, message = rewake.classified_receipt(launch, "ready", "terminal-complete", source)
            if getattr(win, "members", ()):
                message = rewake._batch_message(launch, win, message)
            else:
                from dispatch_recipient_receipt import receipt_marker
                message += "\n" + receipt_marker(pending.read(scenario.state, PARENT_SID, win.delivery_id),
                                                 PARENT_SID)
            code = rewake._emit_with_gates(launch, claim, state, message, block=False, owner_closed=True)
        output = stderr.getvalue() or stdout.getvalue()
        if code != 2:
            return ""
        for member_id in ([member for member, _record in win.members] if getattr(win, "members", ())
                          else [win.delivery_id]):
            with contextlib.suppress(pending.PendingDeliveryError):
                pending.mark_sent_ambiguous(scenario.state, PARENT_SID, member_id, claim_owner=win.claim_owner)
        return output.strip()

    def prompt_hook(prompt: str, *, typed: bool) -> str:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            sweep_hook._process_session(scenario.payload("UserPromptSubmit", prompt=prompt), PARENT_SID)
        text = ""
        for line in stdout.getvalue().splitlines():
            with contextlib.suppress(ValueError, KeyError, TypeError):
                text += json.loads(line)["hookSpecificOutput"]["additionalContext"]
        if text:
            scenario.transcript({"type": "attachment", "attachment": {
                "type": "hook_additional_context", "hookEvent": "UserPromptSubmit", "content": [text]}})
        return text

    def tool_boundary() -> str:
        payload = scenario.payload("PostToolUse", tool_name="Bash")
        pmr.settle_stop_records(payload)
        kwargs = {"payload": payload} if "payload" in inspect.signature(pmr.tool_boundary_records).parameters else {}
        text, batches = pmr.tool_boundary_records(PARENT_SID, time.monotonic() + 6, **kwargs)
        pmr.settle_tool_boundary_records(PARENT_SID, batches, bool(text))
        if text:
            scenario.transcript({"type": "attachment", "attachment": {
                "type": "hook_additional_context", "hookEvent": "PostToolUse", "content": [text]}})
        return text

    def stop() -> str:
        class Out(io.StringIO):
            flushed = False

            def flush(self):
                self.flushed = True
        out = Out()
        pmr.handle_stop(scenario.payload("Stop"), out)
        text = ""
        for line in out.getvalue().splitlines():
            with contextlib.suppress(ValueError, KeyError, TypeError):
                body = json.loads(line)
                if body.get("decision") == "block":
                    text += body.get("reason", "")
        if text:
            scenario.transcript({"type": "user", "isMeta": True,
                                 "message": {"content": "Stop hook feedback:\n" + text}})
        return text

    def typed_prompt(label: str):
        prompt = f"{label}: 사람 입력 — 지금까지 끝난 것을 짧게 알려 줘."
        context = prompt_hook(prompt, typed=True)
        scenario.transcript({"type": "user", "message": {"content": prompt}})
        scenario.carry("prompt-hook", context, prompt=label)
        scenario.carry("prompt-card", _card_context(scenario, "claude", prompt), prompt=label)
        scenario.carry("stop", stop(), extra_turn=True)

    with mock.patch("time.monotonic_ns", side_effect=lambda: scenario.clock[0]):
        for index, item in enumerate(scenario.items):
            scenario.materialize(item)
            pattern = item["pattern"]
            if pattern == "prompt":
                # No rewake is armed (its window lapsed); the next typed prompt carries it.
                scenario.advance(CLAIM_LEASE_GAP_NS)
            elif pattern in {"idle", "busy"}:
                body = rewake_emit(item)
                scenario.carry("rewake", body, wake=True)
                note = scenario.notification(body)
                scenario.transcript({"type": "queue-operation", "operation": "enqueue", "content": note})
                if pattern == "busy":
                    scenario.advance(CLAIM_LEASE_GAP_NS)
                scenario.transcript({"type": "queue-operation", "operation": "dequeue"})
                # Claude Code runs the prompt hooks for the turn the notification starts.
                scenario.carry("prompt-hook(wake)", prompt_hook(note, typed=False))
                scenario.transcript({"type": "user", "origin": {"kind": "task-notification",
                                     "producer": "session-task"}, "promptSource": "system",
                                     "turnOrigin": "task_notification", "message": {"content": note}})
                scenario.advance(5 * 1_000_000_000)
                scenario.carry("tool-boundary", tool_boundary())
                scenario.carry("stop", stop(), extra_turn=True)
            else:  # mid: a tool call longer than the rewake's claim lease
                body = rewake_emit(item)
                scenario.carry("rewake", body)
                note = scenario.notification(body)
                scenario.transcript({"type": "queue-operation", "operation": "enqueue", "content": note})
                scenario.advance(CLAIM_LEASE_GAP_NS)
                scenario.carry("tool-boundary", tool_boundary())
                scenario.transcript({"type": "queue-operation", "operation": "remove", "content": note,
                                     "reason": "absorbed_mid_turn"})
                scenario.transcript({"type": "attachment", "attachment": {
                    "type": "queued_command", "commandMode": "task-notification", "prompt": note,
                    "origin": {"kind": "task-notification", "producer": "session-task"}}})
                scenario.advance(5 * 1_000_000_000)
                scenario.carry("tool-boundary", tool_boundary())
                scenario.carry("stop", stop(), extra_turn=True)
            if index in PROMPTS_AFTER:
                typed_prompt(PROMPTS_AFTER[index])


# --------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------

def _codex(scenario: Scenario) -> None:
    import dispatch_pending_delivery as pending
    import dispatch_session_sweep as sweep
    managed = _load(scenario.source / "utilities" / "codex-managed-completion.py", "scc_codex_managed")
    lifecycle = _load(scenario.source / "adapters" / "codex" / "hooks" / "userprompt-lifecycle.py",
                      "scc_codex_prompt")
    sent = []

    def send(_path, **kwargs):
        sent.append(kwargs)
        return {"status": "consumed", "turn_id": f"turn-{len(sent)}", "turn_status": "completed"}

    def prompt_receipts(label: str | None):
        batches, parts = lifecycle.native_queue_prompt_receipts(PARENT_SID)
        text = "\n".join(parts)
        for root, records in batches:
            sweep.ack_delivered(root, PARENT_SID, records, acked_by="fixture-codex-prompt")
        return text

    for index, item in enumerate(scenario.items):
        # The registry row must look like a launched Codex-parent completion for the queue path.
        scenario.materialize(item)
        if item["pattern"] == "prompt":
            pass  # no queue socket for this one: the next prompt carries it
        else:
            args = SimpleNamespace(queue_socket=Path("/tmp/scc-app-server.sock"), thread_id=PARENT_SID,
                                   jobs=scenario.jobs, timeout=2.0, delivery_retry_interval=0.01)
            before = len(sent)
            with mock.patch.object(managed.codex_queue_delivery, "_rpc", return_value={
                    "thread": {"id": PARENT_SID, "cwd": str(scenario.repo)}}), \
                 mock.patch.object(managed.codex_queue_delivery, "send_at_least_once", side_effect=send):
                with contextlib.suppress(pending.PendingDeliveryError):
                    managed._native_queue_delivery_once(args, item["receipt"], {item["attempt"]})
            for message in sent[before:]:
                scenario.carry("native-queue", message.get("message", ""), wake=True)
        if index in PROMPTS_AFTER:
            label = PROMPTS_AFTER[index]
            scenario.carry("prompt-hook", prompt_receipts(label), prompt=label)
            scenario.carry("prompt-card", _card_context(scenario, "codex", label), prompt=label)


# --------------------------------------------------------------------------
# OpenCode
# --------------------------------------------------------------------------

def _opencode(scenario: Scenario) -> None:
    import dispatch_session_sweep as sweep
    for index, item in enumerate(scenario.items):
        scenario.materialize(item)
        records, _count = sweep.sweep_deliver(scenario.state, "opencode-turn", PARENT_SID)
        text = sweep.delivery_context([(scenario.state, records)]) if records else ""
        if records:
            sweep.ack_delivered(scenario.state, PARENT_SID, records, acked_by="fixture-opencode")
        scenario.carry("opencode-turn", text, wake=item["pattern"] == "idle")
        if index in PROMPTS_AFTER:
            label = PROMPTS_AFTER[index]
            scenario.carry("prompt-card", _card_context(scenario, "opencode", label), prompt=label)


# --------------------------------------------------------------------------
# Typed-prompt context: what the session card hook adds on a typed prompt
# --------------------------------------------------------------------------

def _card_context(scenario: Scenario, harness: str, prompt: str) -> str:
    """The session card hook's prompt context with three open waits for this session.

    The three waits are ones whose producer gives no question (registered children
    waiting on the person), the shape that renders the same line each time.
    """
    import session_tidy
    import human_wait_state
    items = [{"id": f"wait-{n}", "source": "S1", "question": None, "stale": False, "since": 0,
              "owner": {"harness": harness, "session_id": f"child-{n}", "seat_key": f"seat-{n}"}}
             for n in range(3)]
    view = {"items": items, "by_session": {(harness, PARENT_SID): [item["id"] for item in items]}}
    emitted: list[str] = []
    with mock.patch.object(human_wait_state, "cached_view", return_value=view):
        with contextlib.suppress(Exception):
            session_tidy.run_hook(harness, "prompt", PARENT_SID, cwd=str(scenario.repo),
                                  prompt_text=prompt, emit=emitted.append)
    return "\n".join(text for text in emitted if text)


# --------------------------------------------------------------------------
# Totals
# --------------------------------------------------------------------------

_FIXED_PATTERNS = (
    ("receipt-hashes", re.compile(r"row_revision=[0-9a-f]{64} row_digest=[0-9a-f]{64} ")),
)


def _summarize(scenario: Scenario) -> dict:
    per = []
    for item in scenario.items:
        rows = [row for row in scenario.carried if item["delivery_id"] in row["ids"]]
        summary_copies = sum(row["text"].count(f"route {item['route_id']}") for row in rows)
        chars = sum(row["chars"] // max(1, len(row["ids"])) for row in rows)
        # A command this completion owes: in a text that carries it alone, or on its own
        # receipt line when one text carries several completions.
        command = r"observed-status|dispatch-harvest\.py|Inspect the exact|capability-route\.py start"
        follow_up = any(re.search(command, row["text"]) if len(row["ids"]) == 1 else
                        re.search(r"attempt_id=" + re.escape(item["attempt"]) + r"\b[^\n]*required_action=(?!advance-completed)",
                                  row["text"]) is not None
                        for row in rows)
        per.append({
            "index": item["index"], "kind": item["kind"], "pattern": item["pattern"],
            "note": item["note"], "reason": item["reason"],
            "carried": len(rows), "carriers": [row["carrier"] for row in rows],
            "summary_copies": summary_copies, "chars": chars,
            "hex64": sum(len(HEX64.findall(row["text"])) for row in rows),
            "wake_turns": sum(1 for row in rows if row["wake"]),
            "extra_turns": sum(1 for row in rows if row["extra_turn"]) + (2 if follow_up else 0),
            "follow_up_command": follow_up,
            "summary_has_verdict": any(re.search(r"판정[:：]|Verdict[:：]|verdict[:：]", row["text"]) for row in rows),
            "summary_has_pr": any(re.search(r"PR #?\d+|pull/\d+", row["text"]) for row in rows),
            "unextracted": sum(row["text"].count("확실히 추출하지 못함") for row in rows),
        })
    prompts = {}
    for row in scenario.carried:
        if row["prompt"] and not row["ids"]:
            entry = prompts.setdefault(row["prompt"], {"prompt": row["prompt"], "chars": 0, "parts": {}})
            entry["chars"] += row["chars"]
            entry["parts"][row["carrier"]] = entry["parts"].get(row["carrier"], 0) + row["chars"]
    prompts = list(prompts.values())
    unassigned = [row for row in scenario.carried if not row["ids"] and not row["prompt"]]
    return {"completions": per, "prompts": prompts,
            "unassigned_chars": sum(row["chars"] for row in unassigned),
            "texts": [{k: row[k] for k in ("carrier", "chars", "ids", "wake", "extra_turn", "prompt", "text")}
                      for row in scenario.carried]}


@contextlib.contextmanager
def _own_obligation_runners():
    """End only the obligation runners this measurement started before its root goes."""
    import peer_obligations
    existing = set(getattr(peer_obligations, "_RUNNERS", ()))
    try:
        yield
    finally:
        owned = [process for process in getattr(peer_obligations, "_RUNNERS", ()) if process not in existing]
        for process in owned:
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
        for process in owned:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _child(source: Path, output: Path, harness: str) -> int:
    sys.path.insert(0, str(source / "utilities"))
    sys.path.insert(0, str(source / "hooks"))
    sys.path.insert(0, str(source))
    with tempfile.TemporaryDirectory(prefix="scc-") as temporary, _own_obligation_runners():
        scenario = Scenario(source, Path(temporary), harness)
        scenario.build()
        {"claude": _claude, "codex": _codex, "opencode": _opencode}[harness](scenario)
        result = _summarize(scenario)
        result["harness"] = harness
    output.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


def run(source_root: Path, output_dir: Path, harnesses=("claude", "codex", "opencode")) -> dict:
    """Measure every harness for one source tree; returns ``{harness: result}``."""
    source_root = source_root.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for harness in harnesses:
        with tempfile.TemporaryDirectory(prefix="scc-home-") as temporary:
            temp = Path(temporary)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("HERDR_", "AGENT_DISPATCH", "CLAUDE_", "CODEX_", "OPENCODE_",
                                          "AGENT_SESSION", "HARNESS_"))}
            env.update({"HOME": str(temp / "home"), "XDG_CONFIG_HOME": str(temp / "xdg-config"),
                        "XDG_STATE_HOME": str(temp / "xdg-state"), "XDG_DATA_HOME": str(temp / "xdg-data"),
                        "XDG_CACHE_HOME": str(temp / "xdg-cache"), "PYTHONDONTWRITEBYTECODE": "1",
                        "AGENT_HOME": str(source_root), "TMPDIR": str(temp)})
            for name in ("home", "xdg-config", "xdg-state", "xdg-data", "xdg-cache"):
                (temp / name).mkdir()
            target = output_dir / f"{harness}.json"
            done = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", "--harness", harness,
                                   "--source-root", str(source_root), "--output", str(target)],
                                  cwd=str(temp), env=env, text=True, capture_output=True, timeout=600)
            if done.returncode:
                raise RuntimeError(f"{harness} measurement failed:\n{done.stdout[-2000:]}\n{done.stderr[-4000:]}")
            results[harness] = json.loads(target.read_text(encoding="utf-8"))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--harness", default="claude,codex,opencode")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        return _child(args.source_root.resolve(), args.output, args.harness)
    results = run(args.source_root, args.output, tuple(args.harness.split(",")))
    for harness, result in results.items():
        rows = result["completions"]
        print(json.dumps({"harness": harness,
                          "chars": [row["chars"] for row in rows],
                          "carried": [row["carried"] for row in rows],
                          "extra_turns": [row["extra_turns"] for row in rows],
                          "prompt_chars": [row["chars"] for row in result["prompts"]]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
