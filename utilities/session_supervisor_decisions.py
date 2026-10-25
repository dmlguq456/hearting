#!/usr/bin/env python3
"""The supervisor decisions both session supervisors share.

`claude-session-supervisor.py` (Claude and OpenCode owners) and
`codex-app-server-supervisor.py` drive their runtimes' turns differently --
that part is translation and stays in each. What they decide about a
continuation (admit it against the budget, grant the one reserved terminal
hand-off turn) and about a joined child (advance its route stage) was kept
as hand-synchronized copies, "mirrored from" one another in their docstrings
(audit §4 #8, A3 row A4). Those decisions live here once.

Each supervisor passes its own `emit` so a caller that replaces the module's
`emit` (the tests) still sees every event.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from typing import Any, Callable

import dispatch_budget_record as budget_record
import dispatch_stage_advance as stage_advance
import owner_route_binding
from dispatch_continuation_budget import AdmitVerdict, ContinuationLedger


class SupervisorError(RuntimeError):
    """A session supervisor could not preserve its completion contract."""


OWNER_TURN_IDLE_SECONDS = 7200.0


class OwnerTurnActivity:
    """Shared idle-window decision for an ordinary supervised owner turn.

    Transports provide payload progress and a conservative, exact activity
    observer. Unknown samples discard accumulated quiet time; a later known
    quiet sample starts a fresh full window. Sampling is throttled separately
    from transport polling so App Server queue bookkeeping cannot drive a broad
    process scan on every event.
    """

    def __init__(self, observer, *, window_seconds=OWNER_TURN_IDLE_SECONDS,
                 sample_seconds=1.0, clock=time.monotonic, bind_session=None):
        self.observer = observer
        self.window_seconds = float(window_seconds)
        self.sample_seconds = float(sample_seconds)
        self.clock = clock
        self._bind_session = bind_session
        self.quiet_since = None
        self.next_sample = None
        self.last_uncertain = True

    def bind_session(self, session_id: str) -> None:
        if self._bind_session is not None:
            self._bind_session(session_id)

    def expired(self, *, payload_progress=False, now=None) -> bool:
        stamp = self.clock() if now is None else float(now)
        new_sample = self.next_sample is None or stamp >= self.next_sample
        if new_sample:
            self.next_sample = stamp + self.sample_seconds
            try:
                observation = self.observer()
                uncertain = bool(observation.uncertain)
                active = bool(observation.active)
            except Exception:
                uncertain, active = True, False
            self.last_uncertain = uncertain
            if uncertain:
                self.quiet_since = None
            elif payload_progress or active:
                self.quiet_since = stamp
            elif self.quiet_since is None:
                self.quiet_since = stamp
        elif self.last_uncertain:
            self.quiet_since = None
        elif payload_progress:
            self.quiet_since = stamp
        if self.quiet_since is None:
            return False
        return stamp - self.quiet_since >= self.window_seconds


def refresh_owner_route(args) -> None:
    """Follow the same owner's adopted continuation before selecting children.

    Launch arguments stay pinned to the original route outside this controller.
    Using that old tuple here dropped BC's review child after a route advance
    (2026-10-08), and turned a wait into a terminal contract failure.
    """
    binding, _status = owner_route_binding.resolve_owner_route_lifecycle(
        Path(args.jobs), owner_attempt_id=args.parent_attempt_id)
    if binding is not None:
        args.route_file = binding.route_file
        args.route_id = binding.route_id
        args.route_hash = binding.route_hash


def apply_notice(prompt: str, notice: str) -> str:
    """Attach an SD-116 (b)/(c) budget notice outside any prompt's receipt
    JSON. Used for start-retry prompts that carry no receipt, so
    a `notice=` keyword on `completion_prompt()` alone would not reach them
    (amendment A3)."""

    return f"{prompt}\n{notice}" if notice else prompt


def admit_continuation(
    ledger: ContinuationLedger, state_root, *, parent_attempt_id: str,
    route_id: str, route_hash: str, ordinal: int, purpose: str, stalled: bool,
    warning_threshold: int = 3, emit: Callable[[dict], None],
) -> tuple[AdmitVerdict, str]:
    """SD-116 §13.34.4-(2): try the atomic reservation write first, then feed
    its outcome into `ledger.admit()` as `reservation_ok` -- this is what
    lets a forced write failure exercise the admission equation's false
    branch (D47-3) instead of an in-process counter that is always true. On
    refusal, a budget-exhausted warning record always precedes the caller's
    `SupervisorError` (D47-5); a warning-write failure is typed and never
    spends budget or kills the owner (D47-10).

    Returns `(verdict, notice)` rather than widening the frozen
    `AdmitVerdict` dataclass with a new field -- `notice` is `""` unless this
    admit is the one that just crossed the warning threshold (SD-116 (b));
    the caller attaches it to whatever prompt text it is about to send,
    outside any receipt `compact` JSON."""

    klass = "reserved" if purpose == "terminal-handoff" else ("stall" if stalled else "gross")
    reservation_ok, _detail = budget_record.reserve(
        state_root, parent_attempt_id=parent_attempt_id, route_id=route_id,
        route_hash=route_hash, ordinal=ordinal, purpose=purpose, klass=klass,
        remaining={
            "gross_remaining": ledger.gross_remaining,
            "stall_remaining": ledger.stall_remaining,
            "reserved_remaining": ledger.reserved_remaining,
        },
    )
    verdict = ledger.admit(purpose=purpose, stalled=stalled, reservation_ok=reservation_ok)
    notice = ""
    if not verdict.admitted:
        try:
            budget_record.record_warning(
                state_root, parent_attempt_id=parent_attempt_id,
                reason="continuation-budget-exhausted",
                remaining={
                    "gross_remaining": verdict.gross_remaining,
                    "stall_remaining": verdict.stall_remaining,
                    "reserved_remaining": verdict.reserved_remaining,
                },
            )
        except Exception:
            emit({
                "type": "dispatch.supervisor.continuation-budget-warning-unrecorded",
                "parent_attempt_id": parent_attempt_id,
                "reason": "continuation-budget-exhausted",
            })
    elif verdict.gross_remaining <= warning_threshold:
        reason = "continuation-budget-warning"
        try:
            already = budget_record.warning_already_emitted(
                state_root, parent_attempt_id=parent_attempt_id, reason=reason,
            )
        except Exception:
            already = True
        if not already:
            try:
                recorded, _detail = budget_record.record_warning(
                    state_root, parent_attempt_id=parent_attempt_id, reason=reason,
                    remaining={
                        "gross_remaining": verdict.gross_remaining,
                        "stall_remaining": verdict.stall_remaining,
                        "reserved_remaining": verdict.reserved_remaining,
                    },
                )
            except Exception:
                recorded = "continuation-budget-warning-unrecorded"
            if recorded:
                emit({
                    "type": "dispatch.supervisor.continuation-budget-warning-unrecorded",
                    "parent_attempt_id": parent_attempt_id,
                    "reason": reason,
                })
            try:
                notice = budget_record.render_notice(
                    "budget-warning",
                    remaining=verdict.gross_remaining,
                    threshold=warning_threshold,
                )
            except Exception:
                notice = ""
    return verdict, notice


def seal_terminal_handoff_or_raise(
    ledger: ContinuationLedger, state_root, *, args: argparse.Namespace,
    ordinal: int, failure_reason: str, terminal_handoff_issued: list[bool],
    admit: Callable[..., tuple[AdmitVerdict, str]],
) -> str:
    """SD-116 (c): an ordinary/redelivery admit refusal no longer kills the
    owner immediately. It gets exactly one `purpose="terminal-handoff"` admit
    against the sealed reserve (`ContinuationLedger` already enforces this is
    spendable only once the reserve is nonzero) and, on success, a single
    `budget-exhausted` cleanup turn. `terminal_handoff_issued` bounds this to
    once per owner lifetime even if `reserved_remaining` somehow stayed above
    zero (SD-116 (c): "무한 연장 금지"). If the reserve is already spent or
    already used this lifetime, the original `SupervisorError` still fires --
    D47-6's floor-path guarantee (`reserved_remaining >= 1`) is what makes
    this reachable at all on the floor path too."""

    if terminal_handoff_issued[0] or ledger.reserved_remaining <= 0:
        raise SupervisorError(failure_reason)
    verdict, _notice = admit(
        ledger, state_root,
        parent_attempt_id=args.parent_attempt_id,
        route_id=args.route_id, route_hash=args.route_hash,
        ordinal=ordinal, purpose="terminal-handoff", stalled=False,
        warning_threshold=args.continuation_warning_threshold,
    )
    if not verdict.admitted:
        raise SupervisorError(failure_reason)
    terminal_handoff_issued[0] = True
    try:
        exhausted_notice = budget_record.render_notice(
            "budget-exhausted", remaining=0,
            threshold=args.continuation_warning_threshold,
        )
    except Exception:
        exhausted_notice = "[continuation-budget-exhausted] remaining=0."
    return apply_notice(
        "This is the final continuation turn granted from the reserved "
        "budget. No further continuation will be granted after this one.",
        exhausted_notice,
    )


def attempt_stage_advance(
    args: argparse.Namespace,
    current_rows: list[object],
    new_attempts: set[str],
    delivery_timing: dict[str, Any] | None = None,
    *, emit: Callable[[dict], None], default_harness: str,
) -> dict[str, Any] | None:
    """SD-110: best-effort runtime-owned advance for each just-joined
    route-bound child, immediately before the existing SD-78
    `terminal_route_completion` decision. `parked` phase and the
    delivered-outbox intersection stay this supervisor's own predicates
    (§13.32.1-(2)7) -- the core never recomputes them, it only consumes the
    booleans this function derives.

    `delivery_timing` (this join round's own `last_child_terminal_ns` /
    `join_completed_ns`, already stamped by the ordinary SD-109 join loop) is
    reused as the canary's timing basis (block 6, checklist 6.1) -- an
    advanced outcome stamps `next_stage_start_ns` fresh and leaves
    `same_thread_resume_ns`/`exact_harvest_ns` explicitly `null` (no model
    turn occurred). A refusal never carries timing; it has no "next stage".

    Off by default (`--enable-stage-advance`): with the flag unset this
    function is a no-op and the existing delivery path is byte-identical, the
    same construction block 3 already proved for the receipt itself. A
    refusal -- of ANY kind, including an unexpected exception from the real
    services boundary -- never propagates; it is exactly as inert as not
    having called this function at all, because every refusal reason already
    means "perform today's unchanged delivery" (§13.32.1-(4)).

    §13.32.1-(2)6/(3)B: `receipt_schema_negotiated=3` and "the model-facing
    delivery actually carries the `stage_advance` v3 block" are ONE
    negotiation decision, not two independently-toggled ones -- an advance
    the model is never told about is exactly the state (3)B forbids. This
    function's own `--enable-stage-advance` gate (above) is the only thing
    that lets `receipt_schema_negotiated` become anything other than 2, and
    `receipt_with_stage_advance` no longer takes an independent `negotiated`
    flag at all -- it derives the same fact from whether the returned record
    itself carries `outcome == "advanced"`, which is unreachable unless this
    gate already fired. The durable
    `stage_advance_record_v1` for the first `outcome == "advanced"` boundary
    this round is returned so the caller can do exactly that; `None` when
    nothing advanced (including when the flag is off, in which case this
    function is the byte-identical no-op described above).
    """

    if not getattr(args, "enable_stage_advance", False):
        return None
    if not args.route_file or not args.route_id or not args.route_hash:
        return None
    advanced_record: dict[str, Any] | None = None
    open_attempt_ids = frozenset(
        getattr(row, "attempt_id", "")
        for row in current_rows
        if getattr(row, "status", "") in {"open", "running"}
    ) - {""}
    open_children = bool(open_attempt_ids)
    by_attempt = {row.attempt_id: row for row in current_rows}
    for attempt_id in sorted(new_attempts):
        row = by_attempt.get(attempt_id)
        if row is None:
            continue
        metadata = getattr(row, "metadata", {}) or {}
        node = metadata.get("route_node")
        if (
            not node
            or getattr(row, "status", "") != "done"
            or metadata.get("route_id") != args.route_id
            or metadata.get("route_hash") != args.route_hash
        ):
            continue
        request = stage_advance.StageAdvanceRequest(
            jobs=Path(args.jobs),
            route_file=Path(args.route_file),
            predecessor_node=node,
            predecessor_terminal_attempt_id=attempt_id,
            parent_attempt_id=args.parent_attempt_id,
            supervisor_phase="running-turn" if open_children else "parked",
            delivered_open_attempt_ids=open_attempt_ids,
            receipt_schema_negotiated=3,
            harness=getattr(args, "runtime_harness", default_harness),
            worktree=args.worktree,
        )
        try:
            result = stage_advance.coordinate_stage_advance(
                request, stage_advance.RealStageAdvanceServices()
            )
        except Exception as exc:  # advance is optional; never break delivery
            emit(
                {
                    "type": "dispatch.supervisor.stage-advance-refused",
                    "parent_attempt_id": args.parent_attempt_id,
                    "advance_mode": "runtime-deterministic",
                    "route_hash": args.route_hash,
                    "predecessor_node": node,
                    "successor_node": None,
                    "outcome": "refused",
                    "reason": getattr(exc, "reason", type(exc).__name__),
                }
            )
            continue
        timing = delivery_timing or {}
        event = stage_advance.stage_advance_event_fields(
            route_hash=args.route_hash,
            predecessor_node=node,
            result=result,
            last_child_terminal_ns=timing.get("last_child_terminal_ns"),
            join_completed_ns=timing.get("join_completed_ns"),
            next_stage_start_ns=(
                time.monotonic_ns() if result.outcome == "advanced" else None
            ),
        )
        event["parent_attempt_id"] = args.parent_attempt_id
        emit(event)
        if result.outcome == "advanced" and advanced_record is None and result.record_path is not None:
            try:
                advanced_record = json.loads(
                    result.record_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                advanced_record = None
    return advanced_record
