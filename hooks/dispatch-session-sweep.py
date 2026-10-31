#!/usr/bin/env python3
"""SD-111 P4 -- Claude carrier 2: durable-session-activation sweep.

Registered on ``SessionStart`` and ``UserPromptSubmit`` only (never ``Stop``,
§13.33.1-(5) / the 2026-07-10 measurement, CC #38651).

2026-08-29 decision (supersedes the A-21 "surface nothing" slice for Claude,
PRD correction pending): this carrier now DELIVERS. Claude is
measured-unsupported for a session-generation proof, so the claim is made
without one and the accepted trade is bounded at-least-once re-delivery over
never-delivered. For every record addressed to this session that is pending
or lease-expired, one bounded receipt line is injected as
``additionalContext``. A real UserPromptSubmit acknowledges after rendering;
SessionStart only observes and reconnects the existing courier, since startup
context does not itself start inference. The Claude Code `asyncRewake` hook only
wakes an idle session on exit code 2 and its exit-0 output waits for the next
user interaction, so this sweep is the path that guarantees a completion is
seen at the latest on the user's next prompt.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

# The pass deadline starts before the module-level contract/sweep imports: under
# load their import time is part of the configured prompt-hook budget.
_STARTED_NS = time.monotonic_ns()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utilities"))
from dispatch_contract import dispatch_state_roots, resolve_agent_home  # noqa: E402
from dispatch_session_sweep import (  # noqa: E402
    ACTION_BUDGET_SECONDS,
    activate,
    ack_delivered,
    delivery_context,
    request_obligation_reconciliation,
    sweep_deliver,
)

RECIPIENT_KIND = "claude-parent-runtime"
SESSION_GENERATION = "unsupported"


def _session_id(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("session_id")
    return value if isinstance(value, str) and value else None


def _event_name(payload: object) -> str:
    if isinstance(payload, dict):
        value = payload.get("hook_event_name")
        if isinstance(value, str) and value:
            return value
    return "UserPromptSubmit"


def _pass_start_ns() -> int:
    """The pre-import start for the real hook process, call time for importers.

    A test imports this module once and runs cases minutes later; charging that
    whole gap to one simulated pass would make every later case look expired.
    """
    return _STARTED_NS if __name__ == "__main__" else time.monotonic_ns()


def main() -> int:
    # One pass-wide deadline for discovery, claiming, rendering and ACK inside
    # the configured 10 s prompt hook; the rest is the render/ACK reserve. It is
    # derived from the pre-import start so import time is charged too.
    deadline_ns = _pass_start_ns() + int(ACTION_BUDGET_SECONDS * 1_000_000_000)
    try:
        payload = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError):
        return 0
    session_id = _session_id(payload)
    if session_id is None:
        return 0
    try:
        return _process_session(payload, session_id, deadline_ns)
    finally:
        # The hook owns one detached request after delivery work. This covers
        # empty context, SessionStart, workers, and root-resolution failures.
        request_obligation_reconciliation(lifecycle_root=Path(__file__).resolve().parents[1])


def _process_session(payload: object, session_id: str, deadline_ns: int | None = None) -> int:
    if deadline_ns is None:
        deadline_ns = _pass_start_ns() + int(ACTION_BUDGET_SECONDS * 1_000_000_000)
    try:
        agent_home = resolve_agent_home()
        roots = dispatch_state_roots(agent_home)
    except Exception:  # noqa: BLE001 -- fail-open, never block the session
        return 0
    seen: set[Path] = set()
    batches: list[tuple[Path, list[dict]]] = []
    receipt_observer = None
    if _event_name(payload) == "UserPromptSubmit" and isinstance(payload, dict):
        transcript_path = payload.get("transcript_path", payload.get("transcriptPath"))
        cwd = payload.get("cwd")
        if isinstance(transcript_path, str) and isinstance(cwd, str):
            runtime_home = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
            cached_signature = object()
            cached_evidence = None

            def observe_receipt(_root, _storage, _record):
                nonlocal cached_signature, cached_evidence
                try:
                    info = Path(transcript_path).stat()
                    signature = (info.st_size, info.st_mtime_ns)
                except OSError:
                    signature = None
                if signature != cached_signature:
                    try:
                        from dispatch_recipient_receipt import claude_transcript_evidence
                        cached_evidence = claude_transcript_evidence(
                            transcript_path=transcript_path, session_id=session_id, cwd=cwd,
                            expected_cwd=os.getcwd(), runtime_home=runtime_home)
                    except (OSError, RuntimeError, TypeError, ValueError):
                        cached_evidence = None
                    cached_signature = signature
                return cached_evidence

            receipt_observer = observe_receipt
    from dispatch_notice_state import NoticeReadContext
    notice_context = NoticeReadContext()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        if time.monotonic_ns() >= deadline_ns:
            # The pass budget is spent. Rendering and ACK still run for what
            # this pass already holds; later roots stay pending for the next prompt.
            break
        if not Path(root).is_dir():
            # A read-only fallback root (legacy <agent-home>/.dispatch) that
            # does not exist holds nothing to sweep; touching it would create it.
            continue
        try:
            if _event_name(payload) == "SessionStart":
                records = activate(root, RECIPIENT_KIND, session_id, request_recovery=False,
                                   notice_context=notice_context)
            else:
                records, _entries = sweep_deliver(
                    root, RECIPIENT_KIND, session_id,
                    notice_context=notice_context, deadline_ns=deadline_ns,
                    **({"receipt_observer": receipt_observer} if receipt_observer else {}))
        except Exception:  # noqa: BLE001 -- fail-open (§13.33.1-(3))
            continue
        if records:
            batches.append((root, records))
    # Gate notices reach the session here too (SD-123 (8)(b) carrier 2): whether the
    # `asyncRewake` hook process survives an interrupt or a compaction is unmeasured
    # (SD-OPEN-29/32), so the next prompt is the required fallback.
    #
    # The same activation also reconnects retained registered-batch observers:
    # lock-guarded, completed duties are never replayed and the original
    # assignment is never relaunched. The common request below is the single
    # recovery entry for this hook invocation.
    from dispatch_registry_cache import registry_read_scope
    with registry_read_scope():
        # Rendering happens after the claims and is not an authority decision:
        # one observation covers the delivery text and every followup.
        context = delivery_context(batches)
    if not context:
        return 0
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": _event_name(payload),
                    "additionalContext": context,
                }
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )
    sys.stdout.flush()
    if _event_name(payload) == "UserPromptSubmit":
        for root, records in batches:
            try:
                ack_delivered(root, session_id, records, acked_by=f"session-sweep:{session_id}")
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
