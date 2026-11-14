#!/usr/bin/env python3
"""Typed supervisor terminal classification and exact-attempt reconciliation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable

import opencode_server_log
from dispatch_contract import reconcile_attempt_terminal
from route_authority import HANDOFF_RE, handoff_search, pass_blocker_violation


CLASSIFIER_SOURCE = "supervisor-terminal-v1"
# CodexErrorInfo discriminants that mean the same thing as the Claude/OpenCode
# 429 envelope -- everything else (notably `serverOverloaded`, transient
# overload rather than a rate/usage limit) falls through to the shared
# regex/status classification below instead of being trusted blindly.
_CODEX_CAPACITY_STRUCTURED_CODES = frozenset({"usageLimitExceeded", "rateLimitExceeded"})
_CODEX_AUTH_STRUCTURED_CODE = "unauthorized"
_MAX_TAIL_BYTES = 1024 * 1024
# Trailing-block anchor: the one envelope pattern every terminal reader shares
# (route_authority.HANDOFF_RE), so no surface reads a child as finished while
# another calls it malformed.
_HANDOFF_RE = HANDOFF_RE
_CAPACITY_RE = re.compile(
    r"(?:reached|hit) your .{0,80}limit|"
    r"session limit|usage limit|weekly limit|rate limit(?:ed)?|"
    r"model.{0,80}at capacity|insufficient quota",
    re.I,
)
_AUTH_RE = re.compile(
    r"authentication_error|invalid api key|not logged in|unauthorized|forbidden",
    re.I,
)
# A 401/403 whose body is the edge network's block or challenge page: the
# provider's API never saw the request, so no key or account was judged. Seen
# 2026-11-13 on eight Claude owners within two minutes, the whole envelope being
# "Failed to authenticate. API Error: 403 Attention Required! | Cloudflare".
# Only these page titles count; any other 401/403 body stays an auth stop.
_EDGE_BLOCK_RE = re.compile(
    r"Attention Required! \| Cloudflare|<title>\s*Just a moment\.\.\.\s*</title>",
    re.I,
)
EDGE_BLOCK_REASON = "runtime-edge-block-envelope"
_PROTOCOL_REASON_RE = re.compile(
    r"missing|protocol|schema|shape|json|eof|request-failed|result-invalid|"
    r"thread-start|turn-start-response|join-receipt|contract",
    re.I,
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_PERMISSION_AUTO_REJECT_RE = re.compile(
    r"permission requested:.*auto-rejecting", re.I
)
_TRUNCATION_TAIL_LINES = 25


def _terminal_field(value: object, limit: int = 240) -> str:
    return re.sub(r"[\t\r\n,]+", " ", str(value or ""))[:limit]


@dataclass(frozen=True)
class SupervisorTerminal:
    note: str
    failure_class: str
    terminal_event: str
    reconcile_reason: str
    process_exit: str
    api_status: str = ""
    # Set only by missing_result_terminal when an OpenCode server-log read
    # upgrades a missing result to a typed capacity/auth verdict -- the path
    # to the evidence a human or later reader would need to check the read.
    capacity_log: str = ""
    # Set only for an escaped terminal-commit error. The dedicated evidence
    # keys are registered terminal diagnostics, never route authority.
    commit_detail: str = ""
    commit_slot: str = ""

    def evidence(self) -> dict[str, str]:
        values = {
            "classifier_source": CLASSIFIER_SOURCE,
            "detected_by": "completion-supervisor",
            "failure_class": self.failure_class,
            "terminal_event": self.terminal_event,
            "reconcile_reason": _terminal_field(self.reconcile_reason),
            "process_exit": self.process_exit,
        }
        if self.api_status:
            values["api_status"] = self.api_status
        if self.capacity_log:
            values["capacity_log"] = self.capacity_log
        if self.commit_detail:
            values["terminal_commit_detail"] = _terminal_field(self.commit_detail)
        if self.commit_slot:
            values["terminal_commit_slot"] = _terminal_field(self.commit_slot)
        return values


def _bounded_strings(value: object) -> list[str]:
    strings: list[str] = []
    pending = [value]
    while pending and len(strings) < 64:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item[:64])
        elif isinstance(item, str):
            strings.append(item[:4096])
    return strings


def _api_status(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    pending: list[dict[str, Any]] = [value]
    while pending:
        item = pending.pop()
        for key, raw in item.items():
            if isinstance(raw, dict):
                pending.append(raw)
            elif key in {"api_error_status", "status", "status_code", "http_status"}:
                normalized = str(raw).strip()
                if normalized.isdigit():
                    return normalized
    return ""


def _handoff_terminal(text: object, *, event: str, process_exit: int) -> SupervisorTerminal:
    # search, not fullmatch — the pattern anchors the block to the end of the
    # message, so a prepended sentence is ignored rather than fatal. A
    # trailing fence or up to two sentences are tolerated the same way (G1).
    match = handoff_search(text)
    if match is None:
        return SupervisorTerminal(
            "dead-contract",
            "contract",
            event,
            "final-handoff-invalid",
            str(process_exit),
        )
    verdict = match["verdict"]
    blocker = match["blocker"]
    violation = pass_blocker_violation(verdict, blocker)
    if violation:
        return SupervisorTerminal(
            "dead-contract",
            "contract",
            event,
            violation,
            str(process_exit),
        )
    if verdict == "PASS":
        return SupervisorTerminal(
            "completed-supervisor",
            "pass",
            event,
            "exact-final-handoff",
            str(process_exit),
        )
    if verdict == "FAIL":
        return SupervisorTerminal(
            "dead-worker-fail",
            "fail",
            event,
            "worker-reported-fail",
            str(process_exit),
        )
    return SupervisorTerminal(
        "dead-worker-blocked",
        "blocked",
        event,
        "worker-reported-blocked",
        str(process_exit),
    )


def classify_claude_result(result: dict[str, Any], process_exit: int) -> SupervisorTerminal:
    """Compatibility entry for the Claude native result adapter."""
    return classify_session_result(result, process_exit, runtime="claude")


def classify_runtime_failure(
    runtime: str,
    *,
    event: str,
    process_exit: int,
    status: str = "",
    text: str = "",
    structured_code: str = "",
) -> SupervisorTerminal:
    """Shared failure classifier for every runtime's non-success envelope.

    `structured_code` (a CodexErrorInfo discriminant, or any exact-match
    typed reason a caller already knows) is checked first, alongside `status`
    (an HTTP status code, e.g. extracted from a nested `httpStatusCode`).
    Falling through to the `text` regex match keeps this byte-identical to
    the pre-extraction claude/opencode behaviour for callers that pass no
    structured evidence at all.
    """
    if status == "429" or structured_code in _CODEX_CAPACITY_STRUCTURED_CODES:
        return SupervisorTerminal(
            "dead-capacity",
            "capacity",
            event,
            "runtime-capacity-envelope",
            str(process_exit),
            status,
        )
    auth_status = status in {"401", "403"} or structured_code == _CODEX_AUTH_STRUCTURED_CODE
    if auth_status and _EDGE_BLOCK_RE.search(text):
        # A runtime end, so the supervisors' bounded transient retry and the
        # existing runtime-death continuation (`correct`, `start --route`) apply.
        return SupervisorTerminal(
            "dead-runtime-error",
            "runtime",
            event,
            EDGE_BLOCK_REASON,
            str(process_exit),
            status,
        )
    if auth_status:
        return SupervisorTerminal(
            "dead-auth",
            "auth",
            event,
            "runtime-auth-envelope",
            str(process_exit),
            status,
        )
    # A surviving `structured_code` here is a non-account code (the capacity
    # codes returned above), so it is authoritative about the envelope's class
    # and its free text must not be read as account evidence. Without this
    # guard, `serverOverloaded` (transient congestion) carries diagnostic text
    # like "Selected model is at capacity"; `_CAPACITY_RE`'s
    # `model.{0,80}at capacity` alternative flips it to `dead-capacity`, and a
    # consumer blocks that verdict for an hour.
    if not structured_code and _CAPACITY_RE.search(text):
        return SupervisorTerminal(
            "dead-capacity",
            "capacity",
            event,
            "runtime-capacity-envelope",
            str(process_exit),
            status,
        )
    if _AUTH_RE.search(text):
        return SupervisorTerminal(
            "dead-auth",
            "auth",
            event,
            "runtime-auth-envelope",
            str(process_exit),
            status,
        )
    return SupervisorTerminal(
        "dead-runtime-error",
        "runtime",
        event,
        "runtime-error-envelope",
        str(process_exit),
        status,
    )


def classify_session_result(
    result: dict[str, Any], process_exit: int, *, runtime: str
) -> SupervisorTerminal:
    """Classify the portable result produced by a native CLI session driver."""
    if runtime not in {"claude", "opencode"}:
        raise ValueError("session-runtime-unsupported")
    event = f"{runtime}-result"
    is_error = result.get("is_error") is True
    subtype = result.get("subtype")
    if process_exit == 0 and not is_error and subtype in {None, "success"}:
        return _handoff_terminal(
            result.get("result"), event=event, process_exit=process_exit
        )

    status = _api_status(result)
    text = "\n".join(_bounded_strings(result))
    return classify_runtime_failure(
        runtime, event=event, process_exit=process_exit, status=status, text=text
    )


def classify_codex_result(final_text: object, process_exit: int = 0) -> SupervisorTerminal:
    if process_exit != 0:
        return SupervisorTerminal(
            "dead-runtime-exit",
            "runtime",
            "turn.completed",
            "app-server-nonzero-exit",
            str(process_exit),
        )
    return _handoff_terminal(
        final_text, event="turn.completed", process_exit=process_exit
    )


def completed_owner_assessment(terminal, text, *, jobs=None, attempt_id=None):
    """Keep an exact audit's negative judgment without declaring its work dead."""
    if (terminal.note != "dead-worker-fail" or terminal.process_exit != "0"
            or not jobs or not attempt_id):
        return terminal
    try:
        from dispatch_completion_join import exact_attempt_row
        from codex_dispatch_terminal import inspect_terminal_value, inspect_terminal_attempt
        row = exact_attempt_row(Path(jobs), attempt_id)
        if row.status not in {"open", "running"}:
            return terminal
        meta = row.metadata
        context = dict(worktree=row.raw.split("\t")[3], artifact_root_metadata=meta.get("artifact_root"),
                       worker_type=meta.get("worker_type"), artifact_binding=meta)
        if text is None:
            parsed = inspect_terminal_attempt(meta.get("log_file"), **context)
        else:
            match = handoff_search(text)
            if not match or match["verdict"] != "FAIL":
                return terminal
            parsed = inspect_terminal_value({"state": "valid", "verdict": "FAIL", "blocker": match["blocker"],
                "artifact": match["artifact"], "artifact_state": "unchecked", "source": "exact-owner-handoff",
                **({"disposition": match["disposition"]} if match.get("disposition") else {})}, **context)
        if "completed-audit-with-findings" not in str(parsed.get("envelope_normalized") or "").split(","):
            return terminal
        return replace(terminal, note="completed-supervisor", failure_class="pass",
                       reconcile_reason="completed-audit-with-findings")
    except (OSError, ValueError, TypeError, RuntimeError):
        return terminal


def codex_turn_failure_terminal(payload: dict[str, Any]) -> SupervisorTerminal:
    """Classify a `dispatch.supervisor.turn.failed` payload.

    One function, two callers: the live event loop (codex-app-server-
    supervisor.py) calls this the moment it raises `TurnFailed`, and
    `classify_supervisor_log`'s codex branch below calls it again on the same
    logged payload after the process has exited. Sharing this function is
    what guarantees they agree -- there is deliberately no second,
    independent parse of `codex_error_info` anywhere else.
    """
    codex_error_info = payload.get("codex_error_info")
    structured_code, status = "", ""
    if isinstance(codex_error_info, str):
        structured_code = codex_error_info
    elif isinstance(codex_error_info, dict):
        kind = codex_error_info.get("kind")
        http_status = codex_error_info.get("http_status")
        structured_code = kind if isinstance(kind, str) else ""
        status = str(http_status) if isinstance(http_status, int) else ""
    text = "\n".join(
        str(payload.get(key) or "") for key in ("message", "additional_details")
    )
    return classify_runtime_failure(
        "codex",
        event="turn.failed",
        process_exit=70,
        status=status,
        text=text,
        structured_code=structured_code,
    )


_MISSING_RESULT_NOTE = "dead-missing-result"
_MISSING_RESULT_RECONCILE_REASON = "governed-process-group-drained"


def missing_result_terminal(metadata: dict[str, Any]) -> SupervisorTerminal:
    """Classify a detached attempt closed with no result envelope.

    Only OpenCode has a durable per-session server log outside the attempt's
    own (possibly absent) output; reading it -- through the single
    exact-session-bound `opencode_server_log.session_error` seam -- can
    upgrade the verdict to a typed capacity/auth close with `capacity_log`
    evidence. Every other harness, or an unreadable/ambiguous/non-capacity
    read, keeps the conservative `dead-missing-result` note every writer used
    before this function existed: no evidence means no guess (LOOP §4).
    """
    if metadata.get("harness") == "opencode":
        found = opencode_server_log.session_error(metadata)
        if found is not None:
            error_text, log_path = found
            terminal = classify_runtime_failure(
                "opencode", event="opencode-server-log", process_exit=70, text=error_text
            )
            if terminal.failure_class in {"capacity", "auth"}:
                return replace(terminal, capacity_log=str(log_path))
    return SupervisorTerminal(
        _MISSING_RESULT_NOTE,
        "protocol",
        "dispatch-reap-missing-result",
        _MISSING_RESULT_RECONCILE_REASON,
        "0",
    )


def classify_terminal_commit_error(
    code: str,
    detail: str,
    *,
    terminal_slot: str = "",
) -> SupervisorTerminal:
    """Classify an escaped `dispatch_terminal_commit.TerminalCommitError`.

    Both session supervisors' top-level `except Exception` used to collapse
    this into the generic `supervisor-internal-TerminalCommitError` note,
    discarding `code`/`detail` -- the defect that let an owner die with
    every route node already PASS and no diagnosable reason on the row.
    `code` is one of `dispatch_terminal_commit.TERMINAL_REASONS`' nine
    closed values; importing that module here to assert membership would
    reintroduce the eager import this classifier exists to avoid, so the
    caller (which already did an `isinstance` check against the real class
    to get here) is trusted to pass its own `exc.code`/`exc.detail`.
    """
    return SupervisorTerminal(
        "dead-terminal-commit",
        "runtime",
        "dispatch.supervisor.error",
        code,
        "70",
        commit_detail=_terminal_field(detail),
        commit_slot=_terminal_field(terminal_slot),
    )


def terminal_commit_error_event(
    code: str,
    detail: str,
    *,
    terminal_slot: str = "",
) -> dict[str, str]:
    """The shared `dispatch.supervisor.error` payload for the case above.

    One builder for both harness supervisors (LOOP SS3: no per-harness
    copy-paste of the field set a later reader has to reconcile).
    """
    event = {"type": "dispatch.supervisor.error", "reason": _terminal_field(code),
             "detail": _terminal_field(detail)}
    if terminal_slot:
        event["terminal_slot"] = _terminal_field(terminal_slot)
    return event


def classify_escaped_terminal_commit_error(exc, *, route_file, route_id, owner_attempt_id):
    """Classify only a TerminalCommitError from an already-loaded module."""
    module = sys.modules.get("dispatch_terminal_commit")
    error_type = getattr(module, "TerminalCommitError", None) if module is not None else None
    if error_type is None or not isinstance(exc, error_type):
        return None
    evidence = module.terminal_commit_error_evidence(
        exc, route_file=route_file, route_id=route_id, owner_attempt_id=owner_attempt_id)
    terminal = classify_terminal_commit_error(
        evidence.get("code", getattr(exc, "code", type(exc).__name__)),
        evidence.get("detail", str(exc)), terminal_slot=evidence.get("terminal_slot", ""))
    event = terminal_commit_error_event(
        evidence.get("code", getattr(exc, "code", type(exc).__name__)),
        evidence.get("detail", str(exc)), terminal_slot=evidence.get("terminal_slot", ""))
    return terminal, event


# The one observation that says "the runtime ended without the model's final
# result": the supervisor itself recorded the end. Harness-specific reason
# names (`terminal-event-missing`, `opencode-final-stop-missing`, ...) stay in
# `reconcile_reason` and are not judged. `tools/install/distribution.py` keeps
# a standalone copy of the same two columns. A provider's 401/403 refusal
# (`dead-auth`) is the runtime ending the attempt too: the model produced no
# result, so `correct` and `start --route` continue its work in the same
# lineage once the cause is cleared, instead of leaving a route with no way on.
SUPERVISOR_ERROR_EVENT = "dispatch.supervisor.error"
RUNTIME_END_FAILURE_CLASSES = frozenset({"runtime", "protocol"})
RUNTIME_END_NOTES = frozenset({"dead-runtime-exit", "dead-runtime-error", "dead-auth"})


def runtime_ended_without_result(meta) -> bool:
    """Whether the runtime, not the model, ended this attempt with no readable result."""
    from dispatch_attempt_policy import readable_result
    return bool(
        (meta.get("note") in RUNTIME_END_NOTES
         or (meta.get("terminal_event") == SUPERVISOR_ERROR_EVENT
             and meta.get("failure_class") in RUNTIME_END_FAILURE_CLASSES))
        and not readable_result(meta))


def classify_supervisor_error(
    runtime: str,
    reason: str,
    process_exit: int = 70,
) -> SupervisorTerminal:
    failure_class = "protocol" if _PROTOCOL_REASON_RE.search(reason) else "runtime"
    note = "dead-protocol" if failure_class == "protocol" else "dead-runtime-exit"
    return SupervisorTerminal(
        note,
        failure_class,
        "dispatch.supervisor.error",
        reason[:240].replace(",", ";"),
        str(process_exit),
    )


def classify_parent_deploy_wait_blocked(reason: str) -> SupervisorTerminal:
    """The typed parent's wait ended without an execution judgment."""
    return SupervisorTerminal("dead-worker-blocked", "blocked",
        "dispatch.supervisor.terminal", reason[:240].replace(",", ";"), "3")


def classify_supervisor_attention_terminal(runtime: str, reason: str) -> SupervisorTerminal:
    """A supervisor <-> guard contract failure: no admitted command satisfies
    a receipt the supervisor is about to (or has just) prescribed (D2a proof).

    Sealed only from that proof, never from an exhausted redelivery bound --
    see ``classify_supervisor_abandonment_terminal`` for that separate ground.

    ``runtime`` is unused today and kept only for signature parity with
    ``classify_supervisor_error``, so the three constructors stay callable
    through one shape.
    """

    return SupervisorTerminal(
        "owner-attention-unactionable",
        "protocol",
        "dispatch.supervisor.redelivery-suppressed",
        reason[:240].replace(",", ";"),
        "70",
    )


def classify_supervisor_abandonment_terminal(runtime: str, reason: str) -> SupervisorTerminal:
    """A policy stop: the receipt was proven satisfiable and the owner did
    not act within ``--max-identical-redeliveries`` (D2b bound).

    This claims no more than that. It is never sealed from a non-advancing
    row alone, and it must never be confused with
    ``classify_supervisor_attention_terminal``'s protocol-failure ground.

    ``runtime`` is unused today, for the same signature-parity reason.
    """

    return SupervisorTerminal(
        "owner-redelivery-abandoned",
        "runtime",
        "dispatch.supervisor.redelivery-suppressed",
        reason[:240].replace(",", ";"),
        "70",
    )


def reconcile_supervisor_terminal(
    jobs: str | Path,
    attempt_id: str,
    terminal: SupervisorTerminal,
    *,
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    # A post-exit watcher can reach the same final handoff before the live
    # supervisor wrote its normalized event. Use the same exact open owner.
    terminal = completed_owner_assessment(terminal, None, jobs=jobs, attempt_id=attempt_id)
    # SD-111 P2 trigger 1: this module cannot import
    # dispatch_completion_join.materialize_after_terminal_close (circular --
    # dispatch_completion_join -> codex_dispatch_terminal ->
    # dispatch_supervisor_terminal), so every caller of this function must
    # call it itself when the return value is "closed".
    # Retain the exact writer input before a storage operation can fail.
    # Session drivers emit their final result only after this commit; native
    # turn output can also precede route/child checks or envelope conversion.
    if emit is not None:
        emit({"type": "dispatch.supervisor.terminal", "terminal": asdict(terminal)})
    return reconcile_attempt_terminal(
        Path(jobs),
        attempt_id,
        terminal.note,
        evidence=terminal.evidence(),
    )


def _tail_rows(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    size = path.stat().st_size
    start = max(0, size - _MAX_TAIL_BYTES)
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read()
    rows: list[dict[str, Any]] = []
    raw_lines: list[str] = []
    lines = data.splitlines()
    if start and lines:
        lines = lines[1:]
    for raw in lines:
        raw_lines.append(raw.decode("utf-8", "replace"))
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows, raw_lines


def opencode_last_step_finish_reason(rows: list[dict[str, Any]]) -> str | None:
    """Return the `part.reason` of the last `step_finish` row, or None if absent.

    Exposed as its own channel (separate from opencode_terminal_boundary) so a
    caller can record the observed reason as evidence even when it is not
    "stop" — the opencode `step_finish.reason` enum is not fully enumerated
    from observed traffic (only `stop`/`tool-calls` confirmed), so unknown
    values should stay visible rather than collapse into a generic failure.
    """
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        if row.get("type") == "step_finish":
            part = row.get("part")
            reason = part.get("reason") if isinstance(part, dict) else None
            return reason if isinstance(reason, str) else None
    return None


def opencode_terminal_boundary(
    rows: list[dict[str, Any]],
) -> tuple[int | None, str | None]:
    """Locate the opencode `run --format json` success terminal boundary.

    Looks at the last `step_finish` row only (not a backward search for any
    `reason=="stop"` row, to avoid mistaking a mid-stream stop for the final
    one). If its `part.reason == "stop"`, returns (that row's index, the
    `part.text` of the nearest preceding `type=="text"` row). Otherwise
    returns (None, None).
    """
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        if row.get("type") != "step_finish":
            continue
        part = row.get("part")
        reason = part.get("reason") if isinstance(part, dict) else None
        if reason != "stop":
            return (None, None)
        for prior in range(index - 1, -1, -1):
            if rows[prior].get("type") == "text":
                text_part = rows[prior].get("part")
                text = text_part.get("text") if isinstance(text_part, dict) else None
                return (index, text if isinstance(text, str) else None)
        return (index, None)
    return (None, None)


def opencode_truncation_evidence(raw_lines: list[str]) -> str:
    """Scan the retained tail's last 25 raw lines for an ANSI permission-reject line.

    opencode headless wrapper output interleaves non-JSON ANSI lines (e.g.
    `permission requested: external_directory (...); auto-rejecting`) with the
    JSON envelope stream. Strip ANSI escapes before matching; return the
    cleaned matching line, or "" if none found.
    """
    tail = raw_lines[-_TRUNCATION_TAIL_LINES:]
    for raw in tail:
        cleaned = _ANSI_RE.sub("", raw)
        if _PERMISSION_AUTO_REJECT_RE.search(cleaned):
            return cleaned.strip()
    return ""


def supervisor_terminal_from_event(row: dict[str, Any]) -> SupervisorTerminal | None:
    """Read the existing writer's retained input, never model result text."""
    if row.get("type") != "dispatch.supervisor.terminal":
        return None
    value = row.get("terminal")
    if not isinstance(value, dict):
        return None
    try:
        terminal = SupervisorTerminal(**value)
    except TypeError:
        return None
    return terminal if all(isinstance(field, str) for field in asdict(terminal).values()) else None


def classify_supervisor_log(path: str | Path | None, harness: str) -> SupervisorTerminal:
    """Classify a finished owner's exact log for the post-exit watcher."""

    if not path:
        return classify_supervisor_error(harness, "terminal-log-missing")
    try:
        rows, _raw_lines = _tail_rows(Path(path))
    except OSError:
        return classify_supervisor_error(harness, "terminal-log-unreadable")
    # All runtime-specific scans belong to the latest turn. An old stop,
    # failure, or result cannot finish a turn that started afterward.
    for index in range(len(rows) - 1, -1, -1):
        if rows[index].get("type") in {
                "dispatch.supervisor.turn.started", "dispatch.supervisor.turn-started"}:
            rows = rows[index + 1:]
            for raw_index in range(len(_raw_lines) - 1, -1, -1):
                try:
                    raw_row = json.loads(_raw_lines[raw_index])
                except ValueError:
                    continue
                if isinstance(raw_row, dict) and raw_row.get("type") in {
                        "dispatch.supervisor.turn.started", "dispatch.supervisor.turn-started"}:
                    _raw_lines = _raw_lines[raw_index + 1:]
                    break
            break
    for row in reversed(rows):
        terminal = supervisor_terminal_from_event(row)
        if terminal is not None:
            return terminal
    if harness == "opencode":
        # R1 (Gap 1): last step_finish.reason=="stop" is the exact opencode
        # success terminal. R1 must precede R2 (auto-reject) — once item 1(b)
        # (deny instead of ask) lands, "reject then recover to reason=stop"
        # becomes the normal path and must not be misclassified as a death.
        boundary_index, final_text = opencode_terminal_boundary(rows)
        if boundary_index is not None:
            return _handoff_terminal(
                final_text, event="step_finish.stop", process_exit=0
            )
        # R2 (item 1(a)): no stop boundary, but the retained tail shows a
        # permission auto-reject line -- the session died right after the
        # wrapper's headless "ask" rule auto-rejected an external_directory
        # request (see item 1(b): deny returns a structured tool error
        # instead and does not truncate the session; R2 stays as a typed
        # classification for whatever other cause still truncates the log).
        if opencode_truncation_evidence(_raw_lines):
            return SupervisorTerminal(
                "dead-permission-reject",
                "permission",
                "step_finish.truncated",
                "permission-auto-reject",
                "70",
            )
        # If R1/R2 do not match, fall through to the shared claude/codex loop
        # below (R3 dispatch.supervisor.error, else R4 terminal-event-missing);
        # opencode rows never match turn.completed/result so that loop is
        # harness-safe as-is.
    # A structured TurnFailed row is checked first, in its own pass: the
    # live raiser always emits a plain dispatch.supervisor.error *after* it
    # (same "app-server-turn-failed" reason, no structured detail), which
    # sits at a later index and would otherwise win the single backward scan
    # below and downgrade a capacity/auth answer to a generic dead-runtime-
    # exit. This row type never appears in a claude/opencode log (only
    # codex-app-server-supervisor.py emits it), so the pass is harness-safe.
    for index in range(len(rows) - 1, -1, -1):
        if rows[index].get("type") == "dispatch.supervisor.turn.failed":
            return codex_turn_failure_terminal(rows[index])
    settlement_failure = None
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        event = row.get("type")
        if settlement_failure is not None and event in {
                "dispatch.supervisor.turn.started", "dispatch.supervisor.turn-started"}:
            break  # an earlier turn's result cannot settle this failed turn
        if event == "result" and harness in {"claude", "opencode"}:
            return classify_session_result(row, 0, runtime=harness)
        if event == "turn.completed":
            final_text = None
            for prior in range(index - 1, -1, -1):
                item = rows[prior].get("item")
                if (
                    rows[prior].get("type") == "item.completed"
                    and isinstance(item, dict)
                    and item.get("type") == "agent_message"
                ):
                    final_text = item.get("text")
                    break
            return classify_codex_result(final_text)
        if event == "dispatch.supervisor.error":
            reason = str(row.get("reason") or "supervisor-error")
            if reason.startswith(("terminal-reconcile-failed-", "supervisor-finalize-state-",
                                  "supervisor-finalize-lease-")):
                # Failure to persist a result does not replace that result.
                # Retain the error only when no native terminal preceded it.
                if settlement_failure is None or reason.startswith("terminal-reconcile-failed-"):
                    settlement_failure = classify_supervisor_error(harness, reason)
                continue
            return classify_supervisor_error(
                harness, reason
            )
    if settlement_failure is not None:
        return settlement_failure
    if any(row.get("type") == "dispatch.supervisor.turn.completed" for row in rows):
        # The model finished but its owner exited before a final writer input.
        # Keep this an existing runtime continuation, not a model PASS or a
        # malformed-envelope verdict that would refuse the user's correction.
        return classify_supervisor_error(harness, "owner-exited-before-terminal-settlement")
    return classify_supervisor_error(harness, "terminal-event-missing")
