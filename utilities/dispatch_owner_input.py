"""Durable owner input, consumed only by the existing execution supervisor.

This module owns admission and delivery receipts, never execution, retry or
completion policy. Files are private and serialized independently of jobs.lock.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

from dispatch_completion_join import JoinContractError, exact_attempt_row
from dispatch_contract import supervisor_lease_is_held, supervisor_lease_path
import route_authority


PLACEHOLDER_THREAD = "pending-native-session"
ROOT = Path(__file__).resolve().parents[1]


class InputError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _target(jobs, attempt):
    row = exact_attempt_row(Path(jobs), attempt)
    meta = row.metadata
    if meta.get("worker_type") != "owner" or meta.get("dispatch_depth") != "1":
        error = InputError("correction-target-not-owner")
        if meta.get("worker_type") == "frame":
            # A frame leg has no owner to receive a correction; the runtime continues the leg.
            error.retry_when = "A frame leg is continued by the runtime; run resume_command."
        raise error
    keys = ("attempt_id", "harness", "owner_route_id", "owner_route_hash",
            "route_id", "route_hash", "parent_sid", "supervisor_lease_nonce")
    return row, _digest({key: meta.get(key, "") for key in keys})


def _path(jobs, attempt):
    return supervisor_lease_path(jobs, attempt).with_suffix(".input.json")


@contextmanager
def _locked(jobs, attempt, *, create=False):
    path = _path(jobs, attempt)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    elif not os.path.lexists(path):
        # No state means no supervisor ever accepted input; do not create a lock for it.
        raise InputError("owner-input-unsupported")
    if not path.parent.exists():
        raise InputError("owner-input-unsupported")
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if path.is_symlink():
            raise InputError("owner-input-state-unsafe")
        value = json.loads(path.read_text()) if path.exists() else None
        if value is not None and (value.get("schema_version") != 1 or
                                  value.get("attempt_id") != attempt):
            raise InputError("owner-input-state-invalid")
        yield path, value
    finally:
        os.close(fd)


def _write(path, value):
    fd, name = tempfile.mkstemp(prefix=".owner-input-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _transition(item, state, **evidence):
    item.update(state=state, **evidence)
    item["events"].append({"state": state, "time": time.time(), **evidence})


def _preconsumer(value):
    """Registered state that no consumer has bound yet (no generation, placeholder thread)."""
    return "generation" not in value and value.get("thread_id") == PLACEHOLDER_THREAD


def _reachable(row, value, live):
    """A live consumer or a still-open row waiting for its first consumer can take input."""
    return live or (row.status in {"open", "running"} and _preconsumer(value))


def _bound(value, target):
    return (isinstance(value, dict) and value.get("target") == target
            and isinstance(value.get("generation"), str) and bool(value.get("generation"))
            and isinstance(value.get("thread_id"), str)
            and value.get("thread_id") not in {"", PLACEHOLDER_THREAD})


def _recovery_result(state, reason="", **evidence):
    result = {"consumer_recovery": state if state != "not-started" else "not-started:" + reason}
    result.update({key: value for key, value in evidence.items() if value not in (None, "")})
    return result


def _answers_blocked_owner(row, live, jobs=None):
    choice = route_authority.decide_continuation(action="owner-input", jobs=jobs,
        status=row.status, metadata=row.metadata, facts={"reachable": live})
    return choice.allowed and choice.state == "retained"


def _owner_phase(attempt):
    """Read only the exact validated supervisor phase when its path is supplied."""
    raw = os.environ.get("AGENT_DISPATCH_COMPLETION_STATE_FILE")
    if not raw:
        return "unknown"
    try:
        from dispatch_completion_join import read_supervisor_phase_state
        state = read_supervisor_phase_state(Path(raw), attempt)
        return state.phase if state is not None else "unknown"
    except (OSError, ValueError, TypeError):
        return "unknown"


def bound_input_thread(jobs, attempt):
    """Return the native thread only for the exact open attempt's bound channel."""
    try:
        row, target = _target(jobs, attempt)
        with _locked(jobs, attempt) as (_, value):
            row, target = _target(jobs, attempt)
            if (row.status in {"open", "running"} and value is not None
                    and _bound(value, target)):
                return value["thread_id"]
    except (InputError, JoinContractError, OSError, ValueError, KeyError, TypeError):
        return None
    return None


def _public(value, *, live=False, reachable=None, owner_phase=None):
    reachable = live if reachable is None else reachable
    phase = owner_phase or _owner_phase(value["attempt_id"])
    timing = ("active-turn" if value["transport"] == "codex-active-turn" and phase == "running-turn"
              else "phase-unavailable" if value["transport"] == "codex-active-turn" and phase == "unknown"
              else "next-owner-turn")
    lead = {"active-turn": ("A queued correction goes into the owner's running turn; if that turn "
                            "refuses it, it waits for the next owner turn."),
            "phase-unavailable": "Queued corrections may wait for the next owner turn."}.get(
                timing, "Queued corrections are delayed until the next owner turn.")
    notice = (lead + " A parked owner gets its next turn once the children running now finish or "
              "require attention; a serial sub-session chain starts no further sub-session while a "
              "correction waits. A bound correction is never replayed after a sending or "
              "delivery-unknown receipt; consumer recovery requires the effective parent and "
              "positive quiescence evidence.")
    if timing == "phase-unavailable":
        notice = ("Delivery timing is unknown because the exact owner phase is unavailable. " + notice)
    return {"attempt_id": value["attempt_id"], "thread_id": value["thread_id"],
            "transport": value["transport"], "accepting": value["accepting"] and reachable,
            "supervisor_live": live, "owner_phase": phase,
            "delivery_timing": timing, "delivery_notice": notice,
            "requests": [{key: item[key] for key in item if key != "text"}
                         for item in value["requests"]],
            "applied": "not-verified",
            "next_step": "Inspect delivery receipts; do not replay an unknown send or restart the owner."}


def inspect(jobs, attempt):
    row, target = _target(jobs, attempt)
    with _locked(jobs, attempt) as (_, value):
        if value is None:
            raise InputError("owner-input-unsupported")
        if value["target"] != target:
            raise InputError("owner-input-target-changed")
        live = supervisor_lease_is_held(jobs, row.metadata)
        reachable = _reachable(row, value, live)
        result = _public(value, live=live, reachable=reachable)
        for item in result["requests"]:
            item["delivery_observation"] = (
                "delivery-unknown" if not reachable and item["state"] == "sending" else
                "undelivered" if not reachable and item["state"] == "queued" else item["state"])
        if not reachable:
            # Observation does not rewrite uncertain transport history.
            result["next_step"] = (
                "If this open owner's input is bound to its native thread, the route's effective "
                "parent can use normal start to restore the same attempt; inspect the exact delivery "
                "receipt and never replay a sending or delivery-unknown correction."
            )
        return result


def submit(jobs, attempt, text, request_id=None):
    if not text.strip() or len(text.encode()) > 65536:
        raise InputError("correction-text-empty-or-oversized")
    row, target = _target(jobs, attempt)
    digest = _digest(text)
    request_id = request_id or "input-" + _digest([attempt, digest])[:32]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
        raise InputError("correction-id-invalid")
    moved = move_parked_consumer(jobs, row, target, request_id)
    if (row.status in {"open", "running"} and not supervisor_lease_is_held(jobs, row.metadata)
            and "consumer_release_moved_from" not in moved):
        # Settlement takes the jobs lock before input admission takes its own
        # lock. Storage failures leave the same open row available for retry.
        from dispatch_completion_join import settle_finished_attempt
        settle_finished_attempt(Path(jobs), row)
    with _locked(jobs, attempt) as (path, value):
        if value is None:
            raise InputError("owner-input-unsupported")
        # Decide on the row as it is now, under the input lock: the owner may have
        # ended (BLOCKED) since the first read, and an answer must then be kept.
        row, target = _target(jobs, attempt)
        if value["target"] != target:
            raise InputError("owner-input-target-changed")
        duplicate_item = None
        for item in value["requests"]:
            if item["id"] == request_id:
                if item["digest"] != digest:
                    raise InputError("correction-id-content-conflict")
                duplicate_item = item
                break
        live = supervisor_lease_is_held(jobs, row.metadata)
        if duplicate_item is not None:
            result = {**_public(value, live=live, reachable=_reachable(row, value, live)),
                      "request_id": request_id, "duplicate": True,
                      **_retained_fields(duplicate_item)}
            queued = duplicate_item.get("state") == "queued"
        else:
            choice = route_authority.decide_continuation(action="owner-input", jobs=jobs,
                status=row.status, metadata=row.metadata,
                facts={"accepting": value["accepting"], "reachable": _reachable(row, value, live),
                       "bound": _bound(value, target), "lease_held": live,
                       "recoverable": False})
            if not choice.allowed:
                error = InputError("owner-input-unavailable-retain-correction")
                error.retry_when, error.next_action = choice.retry_when, choice.next_action
                raise error
            retain = choice.state == "retained"
            item = {"id": request_id, "digest": digest, "text": text, "events": [],
                    "source_session": route_authority.correction_source_session()}
            _transition(item, "retained" if retain else "queued")
            value["requests"].append(item)
            _write(path, value)
            result = {**_public(value, live=live, reachable=_reachable(row, value, live)),
                      "request_id": request_id, "duplicate": False, **_retained_fields(item)}
            queued = not retain

        bound = _bound(value, target)
        pending_recovery = (queued and bound and row.status in {"open", "running"}
                            and not live)
        unresolved_pending = queued and not _reachable(row, value, live)
        if live and queued and bound and row.status in {"open", "running"}:
            result.update(_recovery_result("live"))
        elif queued and not bound and not live:
            # A never-bound channel is consumed by the first normal launch.
            pass
        elif not queued and duplicate_item is not None and duplicate_item.get("state") in {
                "sending", "delivery-unknown"}:
            result.update(_recovery_result("not-started", "delivery-unknown-no-replay"))

    # The input lock is deliberately released before any registry, process or
    # adapter work. The adapter transaction rechecks identity under jobs.lock.
    result.update(moved)
    if pending_recovery:
        result.update(recover_consumer(jobs, attempt))
    if (unresolved_pending and result.get("consumer_recovery") not in {"live", "respawned"}
            and unresolved(jobs, attempt)):
        materialize_unresolved(jobs, attempt)
    if result.get("consumer_recovery") == "awaiting-parent":
        result["next_step"] = (
            "The correction is stored once. The route's effective parent can use normal start "
            "to restore this same owner's consumer; do not start a replacement."
        )
    return result


def _respawn_adapter(harness):
    """The adapter's same-attempt recovery, or None when that adapter has none."""
    if harness not in {"claude", "codex", "opencode"}:
        return None
    adapter_path = ROOT / "adapters" / harness / "bin" / "dispatch-headless.py"
    if adapter_path.is_symlink() or not adapter_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        f"hearting_{harness}_dispatch_headless_recovery", adapter_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return getattr(module, "respawn_supervised_owner", None)


def _installed_release():
    import peer_obligations
    return peer_obligations.installed_release(ROOT)


def _consumer_release(metadata):
    """Release root of the exact governed consumer process, from its own argv."""
    from dispatch_contract import process_observation
    pid, start = metadata.get("pid", ""), metadata.get("pid_start", "")
    if not pid.isdigit() or not start:
        return None
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    visibility, actual, state = process_observation(int(pid))
    if visibility != "present" or actual != start or state == "Z":
        return None
    for raw in argv[1:3]:
        script = Path(os.fsdecode(raw))
        if (script.is_absolute() and script.name == "model-worker-governor.py"
                and script.parent.name == "utilities"):
            return script.parent.parent.resolve()
    return None


def move_parked_consumer(jobs, row, target, request_id, *, session=None, spawn=None, settle=2.0):
    """Stop a parked consumer of an earlier release so recovery restores it on this one.

    A consumer keeps the code it started with, including the resource launcher.
    After an install, the effective parent's next correction moves a parked
    consumer (no turn running, no input being sent, no launch pending) to the
    installed release: only its own session stops, registered payloads keep
    running, and the existing same-attempt recovery restores it. Anything that
    recovery could not restore leaves the consumer as it is.
    """
    from dispatch_completion_join import read_supervisor_phase_state
    from dispatch_contract import DispatchContractError
    import route_parent_close

    attempt = row.attempt_id
    try:
        metadata = dict(row.metadata)
        if (row.status not in {"open", "running"}
                or metadata.get("launch_claimed") != "1" or metadata.get("launch_started") != "1"
                or metadata.get("parent_close_requested") == "1"
                or route_parent_close.row_requested(metadata, jobs)
                or not supervisor_lease_is_held(jobs, metadata)):
            return {}
        installed = _installed_release()
        previous = _consumer_release(metadata)
        if (installed is None or previous is None or previous == installed
                or ROOT.resolve() != installed):
            return {}
        caller = session
        if caller is None:
            try:
                caller = route_authority.caller_identity()[1]
            except (DispatchContractError, OSError, ValueError, TypeError):
                caller = ""
        if not route_authority.owns(metadata, caller, jobs):
            return {}
        lease_path = supervisor_lease_path(jobs, attempt)
        if metadata.get("supervisor_lease_file") != str(lease_path):
            return {}

        def parked(value):
            state = read_supervisor_phase_state(lease_path.with_suffix(".json"), attempt)
            return (state is not None and state.phase == "parked"
                    and _bound(value, target)
                    and (state.resource or {}).get("session_id") == value["thread_id"]
                    and not any(item.get("state") == "sending" or item.get("id") == request_id
                                for item in value["requests"]))

        resources = route_parent_close.owner_resources(metadata, jobs)
        if resources is None or any(
                (resource.get("row") or {}).get("launch_state") in {"queued", "claimed"}
                for resource in resources if resource.get("kind") == "resource"):
            return {}
        consumer_session = route_parent_close.owner_consumer_session(metadata, resources)
        if consumer_session is None:
            return {}
        spawn = spawn or _respawn_adapter(metadata.get("harness"))
        if spawn is None:
            return {}
        expected = (metadata.get("pid", ""), metadata.get("pid_start", ""))
        if spawn(jobs, attempt, expected_identity=expected,
                 check_only=True).get("state") != "ready":
            return {}
        with _locked(jobs, attempt) as (_, value):
            current, _ = _target(jobs, attempt)
            if (value is None or value.get("target") != target or not parked(value)
                    or (current.metadata.get("pid"), current.metadata.get("pid_start")) != expected):
                return {}
            # Holding the input lock keeps the old consumer from taking new input.
            stopped = route_parent_close.stop_owner_consumer(metadata, resources, consumer_session)
        deadline = time.monotonic() + settle
        while stopped and supervisor_lease_is_held(jobs, metadata) and time.monotonic() < deadline:
            time.sleep(0.05)
        if not stopped or supervisor_lease_is_held(jobs, metadata):
            return {"consumer_release_move": "stop-incomplete", "consumer_release": str(previous)}
        return {"consumer_release_moved_from": str(previous)}
    except (InputError, JoinContractError, OSError, ValueError, KeyError, TypeError):
        return {}


def recover_consumer(jobs, attempt, *, session=None, spawn=None):
    """Restore a positively quiescent Codex input consumer on the same attempt."""
    from dispatch_contract import (
        attempt_governed_process_quiescence, supervisor_lease_is_held,
        supervisor_lease_path, DispatchContractError,
    )
    from dispatch_completion_join import read_supervisor_phase_state

    try:
        row, target = _target(jobs, attempt)
        with _locked(jobs, attempt) as (_, value):
            row, target = _target(jobs, attempt)
            if value is None or value.get("target") != target:
                return _recovery_result("not-started", "owner-input-target-changed")
            if row.status not in {"open", "running"}:
                return _recovery_result("not-started", "owner-not-open")
            if not _bound(value, target):
                return _recovery_result("not-started", "owner-input-not-bound")
            thread_id = value["thread_id"]
            queued = any(item.get("state") == "queued" for item in value.get("requests", []))
            accepting = value.get("accepting") is True
            metadata = dict(row.metadata)
        if not queued:
            return _recovery_result("not-started", "no-queued-correction")
        import route_parent_close
        if (metadata.get("parent_close_requested") == "1"
                or route_parent_close.row_requested(metadata, jobs)):
            return _recovery_result("not-started", "cancelled-by-parent")
        if supervisor_lease_is_held(jobs, metadata):
            return (_recovery_result("live") if accepting else
                    _recovery_result("not-started", "supervisor-live-not-accepting"))

        caller = session
        if caller is None:
            try:
                caller = route_authority.caller_identity()[1]
            except (DispatchContractError, OSError, ValueError, TypeError):
                caller = ""
        if not route_authority.owns(metadata, caller, jobs):
            return _recovery_result("awaiting-parent")
        if metadata.get("launch_claimed") != "1" or metadata.get("launch_started") != "1":
            return _recovery_result("not-started", "owner-launch-not-claimed")
        spawn = spawn or _respawn_adapter(metadata.get("harness"))
        if spawn is None:
            return _recovery_result("not-started", "consumer-respawn-unrealized-" +
                                    str(metadata.get("harness") or "unknown"))

        process = attempt_governed_process_quiescence(metadata)
        if process.state != "quiescent":
            return _recovery_result("not-started", "owner-process-" + process.state,
                                    detail=process.reason)
        try:
            continuation, preserved = route_parent_close.owner_continuation_processes(metadata, jobs)
        except (OSError, ValueError, KeyError, TypeError):
            return _recovery_result("not-started", "owner-continuation-unverifiable")
        if (continuation.state != "quiescent"
                or continuation.reason != "registered-resources-preserved"
                or not preserved):
            reason = (continuation.reason if continuation.state == "unverifiable"
                      else "owner-continuation-" + continuation.state)
            return _recovery_result("not-started", reason)

        lease_path = supervisor_lease_path(jobs, attempt)
        if metadata.get("supervisor_lease_file") != str(lease_path):
            return _recovery_result("not-started", "supervisor-lease-path-invalid")
        state = read_supervisor_phase_state(lease_path.with_suffix(".json"), attempt)
        resource_session = (state.resource or {}).get("session_id") if state else None
        if not resource_session or resource_session != thread_id:
            return _recovery_result("not-started", "owner-input-thread-mismatch")

        decision = route_authority.decide_continuation(
            action="owner-input", jobs=jobs, status=row.status, metadata=metadata,
            facts={"bound": True, "queued": True, "thread_match": True,
                   "lease_held": False, "caller_owns": True,
                   "harness": metadata.get("harness"), "respawn_realized": True,
                   "accepting": accepting, "reachable": False, "recoverable": True,
                   "process": process, "owner_continuation": continuation},
        )
        if not decision.allowed or decision.state != "recover":
            return _recovery_result("not-started", decision.reason or decision.detail or
                                    "owner-input-recovery-not-authorized")

        try:
            outcome = spawn(jobs, attempt,
                            expected_identity=(metadata.get("pid", ""), metadata.get("pid_start", "")))
        except DispatchContractError as exc:
            if exc.reason == "attempt-respawn-identity-changed":
                current, _ = _target(jobs, attempt)
                if (current.metadata.get("pid"), current.metadata.get("pid_start")) != (
                        metadata.get("pid"), metadata.get("pid_start")):
                    return _recovery_result("live") if supervisor_lease_is_held(
                        jobs, current.metadata) else _recovery_result("not-started", "consumer-respawn-raced")
            return _recovery_result("not-started", exc.reason, detail=exc.detail)
        except (OSError, ValueError, TypeError, ImportError) as exc:
            return _recovery_result("not-started", "consumer-respawn-" + type(exc).__name__)
        if not isinstance(outcome, dict):
            return _recovery_result("not-started", "consumer-respawn-result-invalid")
        recovery = outcome.get("consumer_recovery") or outcome.get("state")
        if recovery == "respawned":
            return _recovery_result("respawned",
                pid=outcome.get("pid"), pid_start=outcome.get("pid_start"),
                owner_identity_lineage=outcome.get("owner_identity_lineage"),
                launch_home=outcome.get("launch_home"),
                respawn_release=outcome.get("respawn_release"))
        reason = outcome.get("reason") or str(recovery or "consumer-respawn-not-started")
        if reason.startswith("not-started:"):
            reason = reason.split(":", 1)[1]
        return _recovery_result("not-started", reason,
                                detail=outcome.get("detail"),
                                launch_home=outcome.get("launch_home"),
                                respawn_release=outcome.get("respawn_release"))
    except (InputError, JoinContractError, OSError, ValueError, KeyError, TypeError) as exc:
        return _recovery_result("not-started", "consumer-recovery-" + type(exc).__name__)


def _retained_fields(item):
    if item["state"] != "retained":
        return {}
    return {"retained": True,
            "next_step": "The owner had ended; "
                "this answer is kept for it. Starting the route continues the work in a replacement "
                "owner that receives this answer first."}


# Input no owner turn consumed: kept for an ended owner, or queued/undelivered when the owner
# ended before its supervisor could hand it over. `delivery-unknown` may have been delivered.
UNCONSUMED_STATES = frozenset({"retained", "queued", "undelivered"})


def retained(jobs, attempt):
    """Answers no owner turn consumed, oldest first. Read-only.

    Once the owner ended (BLOCKED, or a readable FAIL) these are the answers its continuation receives
    (dispatch_replacement 'corrected'), including one queued just before it ended."""
    path = _path(jobs, attempt)
    if path.is_symlink():
        return []
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("attempt_id") != attempt:
        return []
    return [{"id": item["id"], "digest": item["digest"], "text": item["text"]}
            for item in value.get("requests", [])
            if isinstance(item, dict) and item.get("state") in UNCONSUMED_STATES]


def blocked_owner_answers(jobs, attempt):
    """The unconsumed answers of an owner that ended waiting for a person, read now; [] otherwise."""
    try:
        row, _ = _target(jobs, attempt)
    except (InputError, JoinContractError, OSError, ValueError, KeyError):
        return []
    if not _answers_blocked_owner(row, supervisor_lease_is_held(jobs, row.metadata), jobs):
        return []
    return retained(jobs, attempt)


def initialize_owner_input(jobs, attempt, transport):
    """Open input admission for a registered owner before its consumer exists.

    Idempotent: an existing state is only identity-checked, never rewritten, except
    that a state no consumer has bound follows a relaunched row (same attempt, new
    lease nonce) so input queued before the relaunch is kept.
    """
    row, target = _target(jobs, attempt)
    if row.status not in {"open", "running"}:
        raise InputError("owner-input-unavailable-retain-correction")
    with _locked(jobs, attempt, create=True) as (path, value):
        if value is not None:
            if value["target"] != target:
                if not _preconsumer(value):
                    raise InputError("owner-input-target-changed")
                value["target"] = target
                _write(path, value)
            return
        _write(path, {"schema_version": 1, "attempt_id": attempt, "target": target,
                      "requests": [], "thread_id": PLACEHOLDER_THREAD,
                      "transport": transport, "accepting": True})


class OwnerInput:
    def __init__(self, jobs, attempt, thread_id, transport, emit):
        self.jobs, self.attempt, self.emit = Path(jobs), attempt, emit
        self.generation = uuid.uuid4().hex
        _, target = _target(jobs, attempt)
        with _locked(jobs, attempt, create=True) as (path, value):
            if value is not None and value["target"] != target and not _preconsumer(value):
                raise InputError("owner-input-target-changed")
            value = value or {"schema_version": 1, "attempt_id": attempt,
                              "target": target, "requests": []}
            value["target"] = target
            first_binding = _preconsumer(value)
            for item in value["requests"]:
                if item["state"] == "sending":
                    _transition(item, "delivery-unknown", reason="supervisor-restarted")
                elif (item["state"] == "queued" and value.get("thread_id") != thread_id
                      and not first_binding):
                    _transition(item, "undelivered", reason="owner-thread-changed")
            value.update(thread_id=thread_id, transport=transport, accepting=True,
                         generation=self.generation)
            _write(path, value)
        self.thread_id = thread_id
        self.active_turn = ""
        self.prepared = []
        self.inflight = {}
        self.next_poll = 0.0
        self.notify_unresolved()

    @contextmanager
    def state(self):
        with _locked(self.jobs, self.attempt) as (path, value):
            if value is None or value["generation"] != self.generation:
                raise InputError("owner-input-consumer-changed")
            yield path, value

    def reopen(self):
        with self.state() as (path, value):
            value["accepting"] = True
            _write(path, value)

    def bind_initial_thread(self, thread):
        if not thread or thread == self.thread_id:
            return
        if self.thread_id != PLACEHOLDER_THREAD:
            raise InputError("owner-input-thread-changed")
        with self.state() as (path, value):
            value["thread_id"] = thread
            _write(path, value)
        self.thread_id = thread

    def pending(self):
        with self.state() as (_, value):
            return any(item["state"] == "queued" for item in value["requests"])

    def take(self, mode):
        with self.state() as (path, value):
            items = [item for item in value["requests"] if item["state"] == "queued"]
            for item in items:
                _transition(item, "sending", mode=mode, thread_id=self.thread_id)
            if items:
                _write(path, value)
            return items

    @staticmethod
    def text(items):
        if not items:
            return ""
        return ("\n<owner-corrections>\nUser corrections relayed by the parent/operator. "
                "Acknowledge what changes in the current work and preserve valid prior work; "
                "do not duplicate active children, forge completed gates, or widen permissions.\n" +
                "\n".join(json.dumps({"id": i["id"], "text": i["text"]}, ensure_ascii=False)
                          for i in items) + "\n</owner-corrections>")

    def prepare(self, prompt):
        items = self.take("next-turn")
        self.prepared = [i["id"] for i in items]
        return prompt + self.text(items)

    def record(self, ids, state, **evidence):
        if not ids:
            return
        with self.state() as (path, value):
            for item in value["requests"]:
                if item["id"] in ids:
                    _transition(item, state, **evidence)
            _write(path, value)
        self.emit({"type": "dispatch.supervisor.input", "attempt_id": self.attempt,
                   "request_ids": ids, "state": state, **evidence})

    def started(self, turn):
        self.active_turn = turn
        self.record(self.prepared, "accepted", thread_id=self.thread_id, turn_id=turn)
        self.prepared = []

    def completed(self, turn):
        with self.state() as (_, value):
            ids = [i["id"] for i in value["requests"]
                   if i["state"] == "accepted" and i.get("turn_id") == turn]
        self.record(ids, "turn-completed", turn_id=turn)
        self.active_turn = ""

    def tick(self, server):
        if not self.active_turn or self.inflight or time.monotonic() < self.next_poll:
            return
        self.next_poll = time.monotonic() + 0.2
        items = self.take("active-turn")
        if not items:
            return
        request = server.next_id
        server.next_id += 1
        self.inflight[request] = ([i["id"] for i in items], self.active_turn)
        server.send({"jsonrpc": "2.0", "id": request, "method": "turn/steer",
                     "params": {"threadId": self.thread_id, "expectedTurnId": self.active_turn,
                                "input": [{"type": "text", "text": self.text(items)}]}})

    def response(self, value):
        if value.get("id") not in self.inflight:
            return False
        ids, turn = self.inflight.pop(value["id"])
        if "error" in value:
            # Explicit rejection proves no delivery; retain for the next turn.
            self.record(ids, "queued", reason="native-steer-rejected")
            self.active_turn = ""
        elif value.get("result", {}).get("turnId") == turn:
            self.record(ids, "accepted", thread_id=self.thread_id, turn_id=turn)
        else:
            self.record(ids, "delivery-unknown", reason="native-steer-receipt-mismatch")
        return True

    def terminal_boundary(self):
        """Serialize last-input admission with closure, without rewriting results."""
        with self.state() as (path, value):
            if any(i["state"] == "queued" for i in value["requests"]):
                return False
            value["accepting"] = False
            _write(path, value)
            return True

    def close(self):
        try:
            with self.state() as (path, value):
                value["accepting"] = False
                for item in value["requests"]:
                    if item["state"] in {"queued", "sending"}:
                        _transition(item, "undelivered" if item["state"] == "queued" else "delivery-unknown",
                                    reason="supervisor-stopped")
                _write(path, value)
        except Exception as exc:
            # A receipt persistence fault cannot reverse committed execution.
            self.emit({"type": "dispatch.supervisor.input-notice-pending", "attempt_id": self.attempt,
                       "reason": type(exc).__name__})
        self.notify_unresolved()

    def notify_unresolved(self):
        materialize_unresolved(self.jobs, self.attempt)


def unresolved_revision(jobs, attempt):
    path = _path(jobs, attempt)
    if not path.exists() or path.is_symlink():
        return ""
    row, target = _target(jobs, attempt)
    live = supervisor_lease_is_held(jobs, row.metadata)
    with _locked(jobs, attempt) as (_, value):
        if value is None or value["target"] != target:
            return ""
        reachable = _reachable(row, value, live)
        items = [(i["id"], i["state"]) for i in value["requests"]
                 if i["state"] in {"delivery-unknown", "undelivered"}
                 or (not reachable and i["state"] in {"queued", "sending"})]
        return _digest(items) if items else ""


def unresolved(jobs, attempt):
    return bool(unresolved_revision(jobs, attempt))


def materialize_unresolved(jobs, attempt):
    """Reuse terminal-close and reconcile drivers after a supervisor crash."""
    try:
        if unresolved(jobs, attempt):
            from dispatch_supervision import materialize
            materialize(Path(jobs), {attempt}, reason="owner-input-undelivered")
    except Exception as exc:
        import sys
        sys.stderr.write(f"owner-input-notice-pending attempt_id={attempt} reason={type(exc).__name__}\n")
