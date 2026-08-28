#!/usr/bin/env python3
"""Typed supervisor terminal classification and exact-attempt reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from dispatch_contract import reconcile_attempt_terminal


CLASSIFIER_SOURCE = "supervisor-terminal-v1"
_MAX_TAIL_BYTES = 1024 * 1024
# Trailing-block anchor, kept in step with codex_dispatch_terminal._HANDOFF_RE —
# the two must accept the same envelopes, or one surface reads a child as
# finished while the other calls it malformed.
_HANDOFF_RE = re.compile(
    r"(?:\A|\n)artifact: [^\n]+\n"
    r"verdict: (?P<verdict>PASS|FAIL|BLOCKED)\n"
    r"blocker: (?P<blocker>[^\n]+)\Z"
)
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


@dataclass(frozen=True)
class SupervisorTerminal:
    note: str
    failure_class: str
    terminal_event: str
    reconcile_reason: str
    process_exit: str
    api_status: str = ""

    def evidence(self) -> dict[str, str]:
        values = {
            "classifier_source": CLASSIFIER_SOURCE,
            "detected_by": "completion-supervisor",
            "failure_class": self.failure_class,
            "terminal_event": self.terminal_event,
            "reconcile_reason": self.reconcile_reason,
            "process_exit": self.process_exit,
        }
        if self.api_status:
            values["api_status"] = self.api_status
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
    # message, so a prepended sentence is ignored rather than fatal.
    match = _HANDOFF_RE.search(text.strip()) if isinstance(text, str) else None
    if match is None:
        return SupervisorTerminal(
            "dead-contract",
            "contract",
            event,
            "final-handoff-invalid",
            str(process_exit),
        )
    verdict = match.group("verdict")
    blocker = match.group("blocker")
    if verdict == "PASS" and blocker != "none":
        return SupervisorTerminal(
            "dead-contract",
            "contract",
            event,
            "pass-blocker-not-none",
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


def classify_claude_result(
    result: dict[str, Any], process_exit: int
) -> SupervisorTerminal:
    """Classify one final Claude print-mode result without scanning prose success."""

    event = "claude-result"
    is_error = result.get("is_error") is True
    subtype = result.get("subtype")
    if process_exit == 0 and not is_error and subtype in {None, "success"}:
        return _handoff_terminal(
            result.get("result"), event=event, process_exit=process_exit
        )

    status = _api_status(result)
    text = "\n".join(_bounded_strings(result))
    if status == "429":
        return SupervisorTerminal(
            "dead-capacity",
            "capacity",
            event,
            "runtime-capacity-envelope",
            str(process_exit),
            status,
        )
    if status in {"401", "403"}:
        return SupervisorTerminal(
            "dead-auth",
            "auth",
            event,
            "runtime-auth-envelope",
            str(process_exit),
            status,
        )
    if _CAPACITY_RE.search(text):
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
) -> str:
    # SD-111 P2 trigger 1: this module cannot import
    # dispatch_completion_join.materialize_after_terminal_close (circular --
    # dispatch_completion_join -> codex_dispatch_terminal ->
    # dispatch_supervisor_terminal), so every caller of this function must
    # call it itself when the return value is "closed".
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


def classify_supervisor_log(path: str | Path | None, harness: str) -> SupervisorTerminal:
    """Classify a finished owner's exact log for the post-exit watcher."""

    if not path:
        return classify_supervisor_error(harness, "terminal-log-missing")
    try:
        rows, _raw_lines = _tail_rows(Path(path))
    except OSError:
        return classify_supervisor_error(harness, "terminal-log-unreadable")
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
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        event = row.get("type")
        if event == "result":
            return classify_claude_result(row, 0)
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
            return classify_supervisor_error(
                harness, str(row.get("reason") or "supervisor-error")
            )
    return classify_supervisor_error(harness, "terminal-event-missing")
