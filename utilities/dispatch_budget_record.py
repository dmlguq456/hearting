"""SD-116 continuation-budget durable record leaf module (plan.md §5.4).

A dependency leaf like `dispatch_launch_tuple.py`: imports only
`dispatch_contract`'s resolvers and nothing else `dispatch_*`.

Records live under `<dispatch_state_root>/supervisor-budget/<parent_attempt_id>.jsonl`
(append-only, flock). Three `record_kind`s share this ledger --
`reservation`, `warning`, `refusal` -- and are a separate vocabulary from
the delivery receipt's `state`/`required_action`/`reason` enums (D47-8):
receipt bytes never change because of this module.

`reserve()` is a CAS append keyed on `(parent_attempt_id, ordinal, purpose)`:
this module deliberately never uses an in-process counter as the sole
admission evidence, because an in-process counter is always true and could
never exercise D47-3's false branch under a forced write failure. `purpose`
is part of the CAS key (impl-review round 1 finding 1) so that a
`terminal-handoff` reservation at the same `ordinal` as the `ordinary`
reservation it follows is never rejected as a duplicate of that unrelated
purpose -- the two reservations are distinct admission decisions even when
the caller reuses the turn's `ordinal` for both.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dispatch_contract import resolve_agent_home  # noqa: E402
from dispatch_contract import resolve_dispatch_state_root  # noqa: E402

SCHEMA_VERSION = 1
RECORD_KINDS = frozenset({"reservation", "warning", "refusal"})
PURPOSES = frozenset({"ordinary", "terminal-handoff"})
CLASSES = frozenset({"gross", "stall", "reserved"})
REFUSAL_REASONS = frozenset({
    "continuation-reserved-scope-violation",
    "continuation-budget-unavailable",
    "continuation-admission-refused",
})
# SD-116 (b)/(c): `warning` record reasons. Distinct from `REFUSAL_REASONS`
# above -- both are `record_kind`-scoped vocabularies, never the delivery
# receipt's `state`/`required_action`/`reason` enums (D47-8).
WARNING_REASONS = frozenset({
    "continuation-budget-exhausted",
    "continuation-budget-warning",
})
_LOCK_DEADLINE_SECONDS = 0.25
_NOTICE_KINDS = frozenset({"budget-warning", "budget-exhausted"})


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_id(row: dict) -> str:
    payload = {key: value for key, value in row.items() if key != "event_id"}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _ledger_path(state_root, parent_attempt_id: str) -> Path:
    return Path(state_root) / "supervisor-budget" / f"{parent_attempt_id}.jsonl"


def _parse_lines(text: str) -> list:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _with_lock(path: Path, fn):
    os.makedirs(path.parent, exist_ok=True)
    lock_path = Path(str(path) + ".lock")
    deadline = time.monotonic() + _LOCK_DEADLINE_SECONDS
    with open(str(lock_path), "a+") as lock:
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError) as exc:
                if getattr(exc, "errno", None) not in (errno.EACCES, errno.EAGAIN) or time.monotonic() >= deadline:
                    return None
                time.sleep(0.005)
        try:
            return fn()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def read_rows(state_root, parent_attempt_id: str) -> tuple:
    path = _ledger_path(state_root, parent_attempt_id)
    if not path.is_file():
        return ()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    return tuple(
        row for row in _parse_lines(text)
        if isinstance(row, dict) and row.get("schema_version") == SCHEMA_VERSION
    )


def _append(path: Path, row: dict) -> bool:
    try:
        payload = (_canonical(row) + "\n").encode("utf-8")
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def reserve(
    state_root, *, parent_attempt_id, route_id, route_hash, ordinal, purpose,
    klass, remaining, terminal_claim_id="", prompt_intent_digest="", now=None,
) -> tuple:
    """CAS append. An ordinary duplicate `(parent_attempt_id, ordinal,
    purpose)` is `reservation-lost`; the sole exception is an exact terminal
    claim/prompt replay after a process crash.  This means the caller's
    ordinary admission is never granted twice, which is what makes
    `atomic_reservation_succeeds` forceable to False for D47-3's negative
    branch. `purpose` is part of the key so a `terminal-handoff` reservation
    sharing its `ordinal` with the `ordinary` reservation it follows is not
    mistaken for a duplicate of that other purpose (impl-review round 1
    finding 1)."""

    if purpose not in PURPOSES or klass not in CLASSES:
        return (False, f"reservation-invalid:purpose={purpose!r},class={klass!r}")
    path = _ledger_path(state_root, parent_attempt_id)

    def _do():
        for existing in read_rows(state_root, parent_attempt_id):
            if (
                existing.get("record_kind") == "reservation"
                and existing.get("ordinal") == ordinal
                and existing.get("purpose") == purpose
            ):
                # A terminal-handoff process may die after this durable CAS
                # and before its claim record advances.  The exact claim and
                # prompt digest make that one reservation replayable without
                # relaxing ordinary-continuation duplicate rejection.
                if (
                    purpose == "terminal-handoff"
                    and terminal_claim_id
                    and prompt_intent_digest
                    and existing.get("route_id") == route_id
                    and existing.get("route_hash") == route_hash
                    and existing.get("class") == klass
                    and existing.get("terminal_claim_id") == terminal_claim_id
                    and existing.get("prompt_intent_digest") == prompt_intent_digest
                ):
                    return (True, "reservation-replayed")
                return (False, "reservation-lost")
        row = {
            "schema_version": SCHEMA_VERSION,
            "record_kind": "reservation",
            "parent_attempt_id": parent_attempt_id,
            "route_id": route_id,
            "route_hash": route_hash,
            "ordinal": ordinal,
            "purpose": purpose,
            "class": klass,
            "gross_remaining": remaining.get("gross_remaining", 0),
            "stall_remaining": remaining.get("stall_remaining", 0),
            "reserved_remaining": remaining.get("reserved_remaining", 0),
            "recorded_at": _rfc3339(_now(now)),
        }
        if purpose == "terminal-handoff":
            if not terminal_claim_id or not prompt_intent_digest:
                return (False, "reservation-invalid:terminal-intent-required")
            row["terminal_claim_id"] = terminal_claim_id
            row["prompt_intent_digest"] = prompt_intent_digest
        row["event_id"] = _event_id(row)
        if not _append(path, row):
            return (False, "reservation-unrecorded:write-failed")
        return (True, "")

    result = _with_lock(path, _do)
    if result is None:
        return (False, "reservation-unrecorded:lock-unavailable")
    return result


def _record_event(state_root, *, parent_attempt_id, record_kind, reason, remaining, now=None) -> tuple:
    path = _ledger_path(state_root, parent_attempt_id)
    row = {
        "schema_version": SCHEMA_VERSION,
        "record_kind": record_kind,
        "parent_attempt_id": parent_attempt_id,
        "reason": reason,
        "gross_remaining": remaining.get("gross_remaining", 0),
        "stall_remaining": remaining.get("stall_remaining", 0),
        "reserved_remaining": remaining.get("reserved_remaining", 0),
        "recorded_at": _rfc3339(_now(now)),
    }
    row["event_id"] = _event_id(row)

    def _do():
        unrecorded = f"continuation-budget-{record_kind}-unrecorded"
        if not _append(path, row):
            return (unrecorded, "write-failed")
        return ("", "")

    result = _with_lock(path, _do)
    if result is None:
        return (f"continuation-budget-{record_kind}-unrecorded", "lock-unavailable")
    return result


def record_warning(state_root, *, parent_attempt_id, reason, remaining, now=None) -> tuple:
    if reason not in WARNING_REASONS:
        return ("continuation-budget-warning-unrecorded", f"unknown-reason:{reason!r}")
    return _record_event(
        state_root, parent_attempt_id=parent_attempt_id, record_kind="warning",
        reason=reason, remaining=remaining, now=now,
    )


def warning_already_emitted(state_root, *, parent_attempt_id, reason) -> bool:
    """SD-116 (b): "exactly once" is judged from the durable record itself,
    never an in-process flag -- the same CAS-over-counter reasoning `reserve()`
    above already documents (a process-local flag is always trustworthy and
    could never exercise a genuine duplicate-suppression check)."""

    for row in read_rows(state_root, parent_attempt_id):
        if row.get("record_kind") == "warning" and row.get("reason") == reason:
            return True
    return False


def render_notice(kind: str, *, remaining: int, threshold: int) -> str:
    """Pure prose renderer shared by the Claude and Codex supervisors (SD-116
    (b)/(c)). Never touches receipt bytes -- callers attach the returned
    string outside any `compact` receipt JSON (D47-8)."""

    if kind not in _NOTICE_KINDS:
        raise ValueError(f"unknown notice kind: {kind!r}")
    if kind == "budget-warning":
        return (
            "[continuation-budget-warning] remaining={remaining} "
            "(warning threshold={threshold}). This owner's ordinary "
            "continuation budget is running low. Recommendation: wrap up "
            "outstanding work now, prefer a partial report over further "
            "exploration, and be ready to end the attempt as BLOCKED if the "
            "budget is exhausted before the work completes."
        ).format(remaining=remaining, threshold=threshold)
    return (
        "[continuation-budget-exhausted] remaining=0. This is the final "
        "reserved turn: no further continuation will be granted after this "
        "one. Use this turn only to record a partial report or hand off "
        "cleanly; do not start new work."
    )


def record_refusal(state_root, *, parent_attempt_id, reason, remaining, now=None) -> tuple:
    if reason not in REFUSAL_REASONS:
        return ("continuation-budget-refusal-unrecorded", f"unknown-reason:{reason!r}")
    return _record_event(
        state_root, parent_attempt_id=parent_attempt_id, record_kind="refusal",
        reason=reason, remaining=remaining, now=now,
    )
