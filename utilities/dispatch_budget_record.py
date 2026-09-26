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
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dispatch_contract import resolve_agent_home  # noqa: E402
from dispatch_contract import resolve_dispatch_state_root  # noqa: E402
from dispatch_contract import verdict_pass  # noqa: E402

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

SUBMISSION_STATES = ("prepared", "intent-sealed", "submitted", "not-submitted", "submission-unknown")
SUBMISSION_EVIDENCE = ("transport-receipt", "target-turn-record", "pre-send-failure",
                       "reservation-unconverted", "transport-confirmed-refusal")
HANDOFF_STATUSES = frozenset(SUBMISSION_STATES)


class TerminalHandoffConflict(RuntimeError):
    """A durable owner/ordinal slot was claimed by a different identity."""


class SubmissionReconciliationError(ValueError):
    pass


def terminal_handoff_root(state_root, owner_attempt_id: str, ordinal: int) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(owner_attempt_id)) or int(ordinal) < 0:
        raise TerminalHandoffConflict("terminal-handoff-identity-invalid")
    return Path(state_root) / "terminal-handoffs" / "v1" / owner_attempt_id / str(int(ordinal))


def _handoff_write(path: Path, value: dict, *, exclusive: bool = False) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = (_canonical(value) + "\n").encode()
    fd, name = tempfile.mkstemp(prefix=".handoff-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _terminal_locked(state_root, owner, operation):
    # Same reservation lock; callers must not nest reserve() in this section.
    result = _with_lock(_ledger_path(state_root, owner), operation)
    if result is None:
        raise TerminalHandoffConflict("terminal-handoff-lock-unavailable")
    return result


def claim_terminal_handoff(state_root, *, owner_attempt_id, route_hash, child_attempt_ids,
                           continuation_ordinal=None, predecessor_claim_id=None):
    if continuation_ordinal is None:
        # Resolve a chain boundary from durable lineage, not a model-turn
        # counter (several SD-119 successors can run within one model turn).
        parent = terminal_handoff_root(state_root, owner_attempt_id, 0).parent
        existing = []
        for path in parent.glob("*/claim.json"):
            row = json.loads(path.read_text())
            if (row.get("route_hash") == route_hash
                    and row.get("child_attempt_ids") == sorted(str(x) for x in child_attempt_ids)
                    and row.get("predecessor_claim_id") == predecessor_claim_id):
                return row
            existing.append(int(row["continuation_ordinal"]))
        # Concurrent contenders use the same next slot and the CAS below;
        # a conflicting contender fails closed rather than changing identity.
        continuation_ordinal = max(existing, default=-1) + 1
    identity = [owner_attempt_id, route_hash, sorted(str(x) for x in child_attempt_ids), int(continuation_ordinal)]
    root = terminal_handoff_root(state_root, owner_attempt_id, continuation_ordinal)
    row = {"schema_version": 1, "contract": "terminal_handoff_claim_v1", "identity": identity,
           "owner_attempt_id": owner_attempt_id, "route_hash": route_hash,
           "child_attempt_ids": identity[2], "continuation_ordinal": int(continuation_ordinal),
           "status": "prepared", "predecessor_claim_id": predecessor_claim_id}
    row["claim_id"] = hashlib.sha256(_canonical(row).encode()).hexdigest()
    path = root / "claim.json"
    def publish():
        try:
            _handoff_write(path, row, exclusive=True)
            return row
        except FileExistsError:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing != row:
                raise TerminalHandoffConflict("terminal-handoff-conflict")
            return existing
    return _terminal_locked(state_root, owner_attempt_id, publish)


def convert_claim_to_prompt_intent(state_root, claim, *, prompt: str, cleanup_scope: dict,
                                 remaining: dict | None = None):
    root = terminal_handoff_root(state_root, claim["owner_attempt_id"], claim["continuation_ordinal"])
    digest = hashlib.sha256(prompt.encode()).hexdigest()
    scope_digest = hashlib.sha256(_canonical(cleanup_scope).encode()).hexdigest()
    intent_id = hashlib.sha256((claim["claim_id"] + digest + scope_digest).encode()).hexdigest()
    scope = dict(cleanup_scope, claim_id=claim["claim_id"], intent_id=intent_id)
    scope["scope_digest"] = hashlib.sha256(_canonical(scope).encode()).hexdigest()
    intent = dict(claim, status="intent-sealed", prompt_digest=digest,
                  intent_id=intent_id, cleanup_scope=scope)
    path = root / "prompt-intent.json"
    def convert():
        if json.loads((root / "claim.json").read_text()) != claim:
            raise TerminalHandoffConflict("terminal-handoff-conflict")
        if path.exists():
            if json.loads(path.read_text()) != intent:
                raise TerminalHandoffConflict("prompt-intent-conflict")
        else:
            # A new ordinal is not a new cleanup entitlement, even after an
            # unknown submission. Retain all prior evidence across restart.
            if any(root.parent.glob("*/prompt-intent.json")):
                raise TerminalHandoffConflict("terminal-handoff-already-converted")
            _handoff_write(path, intent, exclusive=True)
        scope_path = root / "cleanup-scope.json"
        if scope_path.exists():
            if json.loads(scope_path.read_text()) != scope:
                raise TerminalHandoffConflict("cleanup-scope-conflict")
        else:
            _handoff_write(scope_path, scope, exclusive=True)
        if remaining is not None:
            reservations = [r for r in read_rows(state_root, claim["owner_attempt_id"])
                            if r.get("record_kind") == "reservation"
                            and r.get("purpose") == "terminal-handoff"]
            if reservations:
                if len(reservations) != 1 or reservations[0].get("intent_id") != intent_id:
                    raise TerminalHandoffConflict("terminal-reservation-conflict")
            else:
                if remaining.get("reserved_remaining", 0) <= 0:
                    raise TerminalHandoffConflict("terminal-reserve-unavailable")
                row = dict(schema_version=SCHEMA_VERSION, record_kind="reservation",
                           parent_attempt_id=claim["owner_attempt_id"], route_id=scope["route_id"],
                           route_hash=claim["route_hash"], ordinal=claim["continuation_ordinal"],
                           purpose="terminal-handoff", **{"class": "reserved"},
                           intent_id=intent_id, recorded_at=_rfc3339(_now(None)), **remaining)
                row["event_id"] = _event_id(row)
                if not _append(_ledger_path(state_root, claim["owner_attempt_id"]), row):
                    raise TerminalHandoffConflict("terminal-reservation-unrecorded")
        return intent
    return _terminal_locked(state_root, claim["owner_attempt_id"], convert)


def begin_submission(state_root, intent):
    """Fence before transport. A crash after this point is never auto resent."""
    root = terminal_handoff_root(state_root, intent["owner_attempt_id"], intent["continuation_ordinal"])
    def begin():
        if json.loads((root / "prompt-intent.json").read_text()) != intent:
            raise TerminalHandoffConflict("prompt-intent-conflict")
        try:
            _handoff_write(root / "submission.json", dict(schema_version=1,
                           intent_id=intent["intent_id"], status="submission-unknown"), exclusive=True)
        except FileExistsError:
            return False
        return True
    return _terminal_locked(state_root, intent["owner_attempt_id"], begin)


def reconcile_submission(state_root, intent, *, evidence_kind, evidence):
    if evidence_kind not in SUBMISSION_EVIDENCE:
        return {"status": "submission-unknown", "recovery": "recovery-unavailable"}
    digest = intent.get("prompt_digest")
    bound = evidence.get("prompt_digest") if isinstance(evidence, dict) else None
    if bound != digest or evidence.get("intent_id") != intent.get("intent_id"):
        return {"status": "submission-unknown", "recovery": "recovery-unavailable"}
    submitted = evidence_kind in {"transport-receipt", "target-turn-record"}
    resolvable = evidence_kind in {"pre-send-failure", "reservation-unconverted", "transport-confirmed-refusal"}
    if not (submitted or resolvable):
        return {"status": "submission-unknown", "recovery": "recovery-unavailable"}
    root = terminal_handoff_root(state_root, intent["owner_attempt_id"], intent["continuation_ordinal"])
    record = dict(intent_id=intent["intent_id"], evidence_kind=evidence_kind, evidence=evidence)
    def preserve_evidence():
        path = root / "submission-evidence.json"
        try:
            _handoff_write(path, record, exclusive=True)
        except FileExistsError:
            if json.loads(path.read_text()) != record:
                raise TerminalHandoffConflict("submission-evidence-conflict")
        return record
    _terminal_locked(state_root, intent["owner_attempt_id"], preserve_evidence)
    return settle_submission(
        state_root, intent, "submitted" if submitted else "not-submitted",
        _reconciler_evidence=True,
    )


def recover_terminal_handoff(state_root, owner):
    """Reconstruct one intent and run the sole evidence-bound reconciler.

    No transport is called here. Absence of a submission receipt is unknown,
    even when the crash happened before the transport fence was published.
    """
    root = terminal_handoff_root(state_root, owner, 0).parent
    paths = list(root.glob("*/prompt-intent.json"))
    if not paths:
        return None
    if len(paths) != 1:
        raise TerminalHandoffConflict("multiple-cleanup-intents")
    path = paths[0]
    intent = json.loads(path.read_text())
    claim = json.loads((path.parent / "claim.json").read_text())
    if (intent.get("owner_attempt_id") != owner or intent.get("claim_id") != claim.get("claim_id")
            or intent.get("route_hash") != claim.get("route_hash")):
        raise TerminalHandoffConflict("terminal-handoff-conflict")
    evidence_path = path.parent / "submission-evidence.json"
    if evidence_path.exists():
        record = json.loads(evidence_path.read_text())
        if record.get("intent_id") != intent["intent_id"]:
            raise TerminalHandoffConflict("submission-evidence-conflict")
        reconcile_submission(state_root, intent, evidence_kind=record["evidence_kind"],
                             evidence=record["evidence"])
    charge = read_effective_charge(state_root, intent)
    status = "submitted" if charge == 1 else "not-submitted" if charge == 0 else "submission-unknown"
    return dict(intent=intent, claim=claim, status=status, effective_charge=charge)


def complete_terminal_handoff(state_root, claim, *, jobs, terminal_commit_id):
    """Close the immutable claim via its exact write-once completion record."""
    owner = claim["owner_attempt_id"]
    rows = []
    for line in Path(jobs).read_text().splitlines():
        fields = line.split("\t")
        if len(fields) == 6:
            meta = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
            if meta.get("attempt_id") == owner:
                rows.append((fields, meta))
    if (len(rows) != 1 or rows[0][0][1] != "done" or not verdict_pass(rows[0][1])
            or not terminal_commit_id):
        raise TerminalHandoffConflict("terminal-owner-not-reconciled")
    root = terminal_handoff_root(state_root, owner, claim["continuation_ordinal"])
    record = dict(status="completed", claim_id=claim["claim_id"], owner_attempt_id=owner,
                  route_hash=claim["route_hash"], terminal_commit_id=terminal_commit_id)
    def complete():
        if json.loads((root / "claim.json").read_text()) != claim:
            raise TerminalHandoffConflict("terminal-handoff-conflict")
        try:
            _handoff_write(root / "completion.json", record, exclusive=True)
        except FileExistsError:
            if json.loads((root / "completion.json").read_text()) != record:
                raise TerminalHandoffConflict("terminal-completion-conflict")
        return record
    return _terminal_locked(state_root, owner, complete)


def terminal_handoff_observation(state_root, claim):
    root = terminal_handoff_root(state_root, claim["owner_attempt_id"], claim["continuation_ordinal"])
    if json.loads((root / "claim.json").read_text()) != claim:
        raise TerminalHandoffConflict("terminal-handoff-conflict")
    path = root / "completion.json"
    if not path.exists():
        return dict(claim=claim, status="prepared")
    completion = json.loads(path.read_text())
    if (completion.get("status") != "completed" or completion.get("claim_id") != claim["claim_id"]
            or completion.get("route_hash") != claim["route_hash"]
            or completion.get("owner_attempt_id") != claim["owner_attempt_id"]
            or not completion.get("terminal_commit_id")):
        raise TerminalHandoffConflict("terminal-completion-conflict")
    return dict(claim=claim, status="completed", completion=completion)


def settle_submission(state_root, intent, status, *, _reconciler_evidence=False):
    if status not in {"submitted", "not-submitted", "submission-unknown"}:
        raise ValueError("submission-status-invalid")
    root = terminal_handoff_root(state_root, intent["owner_attempt_id"], intent["continuation_ordinal"])
    path = root / "submission.json"
    row = {"schema_version": 1, "intent_id": intent.get("intent_id", intent.get("claim_id")), "status": status}
    def settle():
        try:
            _handoff_write(path, row, exclusive=True)
            return row
        except FileExistsError:
            pass
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("intent_id") != row["intent_id"]:
            raise TerminalHandoffConflict("submission-reconcile-required")
        if existing.get("status") == "submission-unknown" and _reconciler_evidence:
            _handoff_write(path, row)
            return row
        return existing
    return _terminal_locked(state_root, intent["owner_attempt_id"], settle)


def read_effective_charge(state_root, intent):
    path = terminal_handoff_root(state_root, intent["owner_attempt_id"], intent["continuation_ordinal"]) / "submission.json"
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("intent_id") != intent.get("intent_id"):
            return None
        status = row.get("status")
    except (OSError, ValueError):
        return None
    return 1 if status == "submitted" else 0 if status == "not-submitted" else None


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
            written = 0
            while written < len(payload):
                count = os.write(fd, payload[written:])
                if count <= 0:
                    raise OSError("budget-record-short-write")
                written += count
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def reserve(
    state_root, *, parent_attempt_id, route_id, route_hash, ordinal, purpose,
    klass, remaining, now=None,
) -> tuple:
    """CAS append. A duplicate `(parent_attempt_id, ordinal, purpose)` is
    `reservation-lost` -- the caller's admission is never granted twice for
    the same ordinal and purpose, which is what makes
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
