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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utilities"))
from dispatch_contract import dispatch_state_roots, resolve_agent_home  # noqa: E402
from dispatch_session_sweep import (  # noqa: E402
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


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError):
        return 0
    session_id = _session_id(payload)
    if session_id is None:
        return 0
    try:
        return _process_session(payload, session_id)
    finally:
        # The hook owns one detached request after delivery work. This covers
        # empty context, SessionStart, workers, and root-resolution failures.
        request_obligation_reconciliation(lifecycle_root=Path(__file__).resolve().parents[1])


def _process_session(payload: object, session_id: str) -> int:
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
                    notice_context=notice_context,
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
