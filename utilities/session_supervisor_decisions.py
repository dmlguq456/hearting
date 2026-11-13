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
import copy
import json
from pathlib import Path
import time
from typing import Any, Callable

import dispatch_budget_record as budget_record
import dispatch_completion_join as completion_join
import dispatch_resource_wait as resource_wait
import dispatch_stage_advance as stage_advance
import owner_route_binding
from dispatch_continuation_budget import AdmitVerdict, ContinuationLedger
# The owner idle window and the foreground launcher's no-progress window are
# one decision; it lives beside the observer both of them sample.
from foreground_activity import OWNER_TURN_IDLE_SECONDS  # noqa: F401
from foreground_activity import ActivityWindow as OwnerTurnActivity  # noqa: F401

from dispatch_supervision import StopWaiting

# Registered supervisors share the bounded, no-progress transient retry chain
# used by the interactive Codex capacity continuation.
from dispatch_attempt_policy import TRANSIENT_RETRY_DELAYS


def transient_retry_delay(error_info: object, failures: int, *, registered: bool = True) -> int | None:
    """Return the next bounded retry delay for an exact structured overload.

    Registered work excludes 429 because that remains an account/usage
    terminal. Raw app-server and normalized rollout spellings are accepted;
    malformed or ambiguous values fail closed.
    """
    if not isinstance(failures, int) or isinstance(failures, bool) or failures < 0:
        return None
    kind, status = "", None
    if error_info in ("server_overloaded", "serverOverloaded"):
        kind = "serverOverloaded"
    elif isinstance(error_info, dict) and len(error_info) == 1:
        kind, detail = next(iter(error_info.items()))
        if not isinstance(kind, str):
            return None
        if isinstance(detail, dict):
            status = detail.get("httpStatusCode", detail.get("http_status_code",
                     detail.get("http_status")))
    elif isinstance(error_info, dict) and set(error_info).issubset({"kind", "http_status"}):
        kind = error_info.get("kind", "")
        status = error_info.get("http_status")
    else:
        return None
    if status is not None and (not isinstance(status, int) or isinstance(status, bool)):
        return None
    if kind in {"server_overloaded", "serverOverloaded"}:
        pass
    elif kind in {"response_too_many_failed_attempts", "responseTooManyFailedAttempts"}:
        if not isinstance(status, int) or isinstance(status, bool):
            return None
        accepted = {503} if registered else {429, 503}
        if status not in accepted:
            return None
    else:
        return None
    return TRANSIENT_RETRY_DELAYS[failures] if failures < len(TRANSIENT_RETRY_DELAYS) else None


EDGE_BLOCK_CONTINUE_PROMPT = (
    "The previous turn stopped on a transient service block (an HTTP 403 edge page, not an "
    "account or key rejection). Continue the same work from where it stopped; preserve active "
    "children.")


def edge_block_retry_delay(terminal: object, failures: int) -> int | None:
    """The next bounded retry delay for a turn the edge network blocked, or None.

    The classifier names the block page (`EDGE_BLOCK_REASON`); every other
    terminal keeps its existing handling. Same schedule as the structured
    overload retry above."""
    from dispatch_supervisor_terminal import EDGE_BLOCK_REASON
    if (getattr(terminal, "reconcile_reason", None) != EDGE_BLOCK_REASON
            or not isinstance(failures, int) or isinstance(failures, bool) or failures < 0):
        return None
    return TRANSIENT_RETRY_DELAYS[failures] if failures < len(TRANSIENT_RETRY_DELAYS) else None


def wait_transient_retry(delay: float, *, control, emit: Callable[[dict], None], common: dict,
                         clock: Callable[[], float] = time.monotonic,
                         sleep: Callable[[float], None] = time.sleep) -> str:
    """Wait out one retry delay; a pending owner correction ends it early.

    Returns "owner-input" or "deadline"."""
    deadline = clock() + delay
    emit({"type": "dispatch.supervisor.transient-wait.started", **common,
          "deadline_monotonic": deadline})
    outcome = "deadline"
    while clock() < deadline:
        if control is not None and control.pending():
            outcome = "owner-input"
            break
        sleep(min(0.25, max(0.0, deadline - clock())))
    emit({"type": "dispatch.supervisor.transient-wait.ended", **common, "outcome": outcome})
    return outcome


class SupervisorError(RuntimeError):
    """A session supervisor could not preserve its completion contract."""


def inherit_continuation_ledger(ledger: ContinuationLedger, state_root,
                                parent_attempt_id: str) -> tuple[int, bool]:
    """Replay this attempt's durable reservations without writing new rows."""
    rows = budget_record.read_rows(state_root, parent_attempt_id)
    next_ordinal = 0
    terminal_handoff_issued = False
    for row in rows:
        if row.get("record_kind") != "reservation":
            continue
        ordinal = row.get("ordinal")
        purpose = row.get("purpose")
        klass = row.get("class")
        if (not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0
                or row.get("parent_attempt_id") != parent_attempt_id
                or purpose not in {"ordinary", "terminal-handoff"}
                or klass not in {"gross", "stall", "reserved"}
                or (purpose == "terminal-handoff") != (klass == "reserved")
                or (purpose == "ordinary" and klass == "reserved")):
            raise SupervisorError("continuation-ledger-inheritance-invalid")
        verdict = ledger.admit(purpose=purpose, stalled=klass == "stall", reservation_ok=True)
        if not verdict.admitted:
            raise SupervisorError("continuation-ledger-inheritance-invalid")
        next_ordinal = max(next_ordinal, ordinal + 1)
        terminal_handoff_issued = terminal_handoff_issued or purpose == "terminal-handoff"
    return next_ordinal, terminal_handoff_issued


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


# A supervisor parked on model children looks at the resources its owner holds this
# often. It is the existing join split into shorter timeouts, not a timer or a poller.
RESOURCE_ATTENTION_CHECK_SECONDS = 30.0


class ResourceAttentionReady(StopWaiting):
    """A held resource has a receipt for this owner while its model children are still parked."""

    def __init__(self, prompt: str):
        super().__init__("resource-attention-ready")
        self.prompt = prompt


PARKED_CORRECTION_PROMPT = (
    "Continue the same work using the pending user correction; preserve active children. "
    "Your registered children are still running and were not delivered: after applying the "
    "correction, end the turn with `runtime_wait: registered-children` to keep waiting for them.")


class CorrectionReady(ResourceAttentionReady):
    """A user correction waits for this owner while its model children are still parked.

    Caught where a resource receipt is: the children stay undelivered and are parked
    again after the owner's turn, and the loop's own `control.prepare` attaches the
    correction text. No resource outbox exists, so no continuation is spent on it."""

    def __init__(self):
        super().__init__(PARKED_CORRECTION_PROMPT)


def join_with_resource_attention(
    args, state_path, control, delivered: set[str],
    join: Callable[[Any, set[str]], dict], *,
    check_seconds: float = RESOURCE_ATTENTION_CHECK_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[set[str]], dict]:
    """Wrap `join(args, attempts)` so a correction or a resource's attention is not held
    behind model children.

    The same total wait (`args.join_timeout`) runs in `check_seconds` pieces. Between
    pieces a pending user correction (`control.pending()`) raises `CorrectionReady`, and an
    owner holding resources gets one look at them (`dispatch_resource_wait.observe_once`);
    a receipt raises `ResourceAttentionReady`. Either way the model children are not
    delivered and keep running, so the next loop parks them again.

    Holding nothing and not having read it yet differ: a holder that could not be read
    (`HolderUnobserved`) is read again at each checkpoint; once it reads as holding
    nothing, the checkpoints only look for a correction.
    """
    def holds_resources() -> bool | None:
        try:
            return resource_wait.context(args, control) is not None
        except resource_wait.HolderUnobserved:
            return None  # could not look yet: never read as "holds nothing"
        except Exception:  # the old behavior: no look at all, never a new failure here
            return False

    def correction_pending() -> bool:
        try:
            return bool(control is not None and control.pending())
        except Exception:  # an unreadable channel is looked at again at the next checkpoint
            return False

    def run(attempts: set[str]) -> dict:
        total = float(args.join_timeout)
        if total <= 0:
            return join(args, attempts)
        holds = holds_resources()
        deadline = clock() + total
        while True:
            step = max(min(check_seconds, deadline - clock()), 1.0)
            piece = copy.copy(args)
            piece.join_timeout = step
            receipt = join(piece, attempts)
            if receipt.get("state") != "timeout":
                return receipt
            if correction_pending():
                raise CorrectionReady()
            if holds is None:
                holds = holds_resources()
            if not holds:
                if clock() >= deadline:
                    return receipt
                continue
            state = completion_join.read_supervisor_phase_state(state_path, args.parent_attempt_id)
            resource = dict(state.resource) if state and state.resource else {
                "session_id": control.thread_id, "delivered": [], "outbox": None}
            if resource["session_id"] == control.thread_id and not resource["outbox"]:
                try:
                    prompt = resource_wait.observe_once(args, state_path, control, delivered, resource)
                except (completion_join.JoinContractError, OSError, ValueError):
                    prompt = None  # looked at again at the next checkpoint
                after = completion_join.read_supervisor_phase_state(state_path, args.parent_attempt_id)
                if isinstance(prompt, str) and after is not None and (after.resource or {}).get("outbox"):
                    raise ResourceAttentionReady(prompt)
            if clock() >= deadline:
                return receipt
    return run


def recovered_resource_wait(args, state_path, control, delivered: set[str], emit,
                            join: Callable[[Any, set[str]], dict]) -> str | None:
    """A restarted supervisor's first wait over the resource state it recovered.

    It waits where the loop would: on the held resources alone when no model child is
    left to join, else on those children with the resources looked at between join
    pieces (`join_with_resource_attention`, the loop's own park), so a child that already
    finished is not held behind a long resource run. Returns the resource receipt's
    prompt, or None (nothing outstanding, or the children are ready first); the loop
    then continues as for any restart and delivers the children itself.
    """
    jobs = Path(args.jobs)
    rows = completion_join.current_children(jobs, args.parent_attempt_id,
                                            route_id=args.route_id, route_hash=args.route_hash)
    undelivered = [row for row in rows if row.attempt_id not in delivered]
    children = set(completion_join.partition_runtime_wait_children(
        jobs, args.parent_attempt_id, undelivered, {row.attempt_id for row in undelivered}).joinable)
    if not children:
        return resource_wait.wait(args, state_path, control, delivered, emit)

    def park():
        from dispatch_supervision import wait_for_batch
        from dispatch_replacement import advance_batch, adopt_receipt
        try:
            receipt = wait_for_batch(
                join=join_with_resource_attention(args, state_path, control, delivered, join),
                attempts=set(children), jobs=jobs, parent_attempt_id=args.parent_attempt_id,
                emit=emit,
                replacement_checkpoint=lambda selected: advance_batch(jobs, selected)
                    if not control.pending() else (selected, [], []))
        except ResourceAttentionReady as ready:
            return ready.prompt
        _joined, replaced = adopt_receipt(jobs, set(children), receipt)
        delivered.update(replaced)
        return None

    return resource_wait.wait(args, state_path, control, delivered, emit, park=park)
