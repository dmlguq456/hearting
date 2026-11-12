#!/usr/bin/env python3
"""Watch one exact owner PID and reconcile its registry row after exit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from dispatch_contract import (
    PARENT_EXTINCTION_TERMINAL_STATUSES,
    SUPERVISOR_LEASE_KIND,
    dispatch_state_root,
    observed_supervised_owner_liveness,
    parse_registry_metadata,
    process_namespace_identity,
    process_start_ticks,
    remove_supervisor_lease,
    supervisor_lease_path,
)
from dispatch_completion_join import (
    materialize_after_terminal_close,
    read_supervisor_phase_state,
    remove_supervisor_state,
)
from dispatch_supervisor_terminal import (
    classify_supervisor_log,
    reconcile_supervisor_terminal,
)
from runtime_observation import file_generation


OPEN = {"open", "running"}
# A parent close normally marks the watched row, which changes jobs.log. This
# finite backstop keeps a close intent recorded only outside the registry
# (attachments and adopted advances) visible to an otherwise idle watcher.
CLOSE_INTENT_BACKSTOP_SECONDS = 30.0


def process_start(pid: int) -> str | None:
    return process_start_ticks(pid)


def attempt_record(
    jobs: Path, attempt_id: str
) -> tuple[str | None, dict[str, str]]:
    try:
        import dispatch_registry_cache
        with dispatch_registry_cache.registry_read_scope():
            view = dispatch_registry_cache.current_registry_view(jobs)
            if view._prefix_reuse_eligible:
                # Index lookup keeps the first-match meaning of the text scan
                # below without parsing or copying every row of a large registry.
                records = view.completion_attempt_records(attempt_id)
                if records:
                    fields = records[0].fields
                    meta = parse_registry_metadata(fields[5])
                    return (fields[1], meta) if meta.get("attempt_id") == attempt_id else (None, {})
                for fields, meta in view.select_rows(attempt_id=attempt_id):
                    if meta.get("attempt_id") == attempt_id:
                        return fields[1], parse_registry_metadata(fields[5])
                return None, {}
            lines = dispatch_registry_cache.registry_text_lines(jobs)
            if lines is None:
                lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, {}
    for line in lines:
        # Only a line containing the id can carry attempt_id=<id>; skip the
        # metadata parse of every other row of a large registry.
        if attempt_id not in line:
            continue
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        meta = parse_registry_metadata(fields[5])
        if meta.get("attempt_id") == attempt_id:
            return fields[1], meta
    return None, {}


class RegistryObservation:
    """Re-run registry judgments only when their inputs can have changed.

    The owner PID stays a per-interval ``/proc`` check. The watched row is
    re-read when the jobs.log generation changes; the parent-close judgment
    re-runs when that row changes or the close-intent backstop elapses.
    """

    def __init__(self, jobs: Path, attempt_id: str, *,
                 backstop: float = CLOSE_INTENT_BACKSTOP_SECONDS):
        self.jobs, self.attempt_id, self.backstop = jobs, attempt_id, backstop
        self.generation = None
        self.status: str | None = None
        self.metadata: dict[str, str] = {}
        self.close_requested = False
        self.judged_at: float | None = None

    def refresh(self, *, force: bool = False):
        import route_parent_close
        import dispatch_registry_cache
        with dispatch_registry_cache.registry_read_scope():
            return self._refresh_scoped(force=force, route_parent_close=route_parent_close)

    def _refresh_scoped(self, *, force: bool, route_parent_close):
        try:
            generation = file_generation(self.jobs)
        except OSError:
            generation = None
        row_changed = False
        if force or generation is None or generation != self.generation:
            self.generation = generation
            status, metadata = attempt_record(self.jobs, self.attempt_id)
            row_changed = (status, metadata) != (self.status, self.metadata)
            self.status, self.metadata = status, metadata
        now = time.monotonic()
        if (force or row_changed or self.judged_at is None
                or now - self.judged_at >= self.backstop):
            self.close_requested = route_parent_close.row_requested(self.metadata, self.jobs)
            self.judged_at = now
        return self.status, self.metadata, self.close_requested


def attempt_status(jobs: Path, attempt_id: str) -> str | None:
    return attempt_record(jobs, attempt_id)[0]


def follow_installed_release(args) -> None:
    """Once its attempt runs nothing, hand this watch to the installed release (same PID).

    A watch can outlive its attempt while a close or a recovery settles, and
    nothing else would ever move it off the release that started it. While the
    exact owner, or any process of its attempt, is still live, the attempt runs
    its own release's code and the watch stays with it, so one release's code
    settles the attempt (OPERATIONS §5.9a). It is called only before a wait,
    where nothing is half done: the registry row and the exact owner identity
    are the watch's only inputs, so the new code starts this same watch again
    from them. Without its original arguments (a direct call) or when nothing
    moved, the watch continues here.
    """
    argv = getattr(args, "release_argv", None)
    if argv is None:
        return
    try:
        from parent_next_directive import release_moved
        if not release_moved(Path(__file__).resolve().parents[1]):
            return
        if process_start(args.pid) == args.pid_start:
            return
        observed, _phase, _metadata = observed_owner_lifecycle(args)
        if observed is not None and observed.process_state != "quiescent":
            return
        import peer_obligations
        peer_obligations.follow_installed_release(__file__, argv)
    except Exception:  # noqa: BLE001 -- a failed move keeps watching with this code
        pass


def _run_registry(operation: str, args) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(Path(__file__).resolve().with_name("dispatch-registry.py")),
        operation,
        "--attempt", args.attempt_id,
        "--jobs", str(args.jobs),
        "--agent-home", str(args.agent_home),
        "--apply",
    ]
    if operation == "orphan-status":
        command.extend(("--pid", str(args.pid), "--pid-start", args.pid_start))
        if args.pid_observer_ns:
            command.extend(("--pid-observer-ns", args.pid_observer_ns))
    return subprocess.run(
        command,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )


def _remove_supervisor_state(args) -> None:
    if re.fullmatch(r"att-[A-Za-z0-9._-]{1,240}", args.attempt_id):
        state_root = dispatch_state_root(args.jobs)
        remove_supervisor_state(
            state_root
            / "supervisor-state"
            / f"{args.attempt_id}.json"
        )
        _status, metadata = attempt_record(args.jobs, args.attempt_id)
        lease = supervisor_lease_path(args.jobs, args.attempt_id)
        if (
            metadata.get("supervisor_lease") == SUPERVISOR_LEASE_KIND
            and metadata.get("supervisor_lease_file") == str(lease)
        ):
            remove_supervisor_lease(lease)


def observed_owner_lifecycle(args):
    """Return the shared exact owner verdict plus its durable phase."""

    status, metadata = attempt_record(args.jobs, args.attempt_id)
    if status is None:
        return None, "", metadata
    state = read_supervisor_phase_state(
        dispatch_state_root(args.jobs)
        / "supervisor-state"
        / f"{args.attempt_id}.json",
        args.attempt_id,
    )
    phase = state.phase if state is not None else ""
    observed = observed_supervised_owner_liveness(
        args.jobs,
        status,
        metadata,
        supervisor_phase=phase,
    )
    return observed, phase, metadata


def _owned_pending(args):
    from dispatch_supervision import _owned_rows, _pending
    rows = _owned_rows(Path(args.jobs), args.attempt_id)
    owned = {args.attempt_id} if args.attempt_id in rows else set()
    for aid in rows:
        if rows[aid][1].get("parent_attempt_id") == args.attempt_id:
            owned.add(aid)
    pending = bool(owned) and _pending(rows, sorted(owned), Path(args.jobs), reason="supervisor-exited")
    return owned, pending


def _finish_recovery(args, *, clock=time.monotonic, sleep=time.sleep) -> bool:
    """Retire state only after every owned execution is settled.

    A failed cleanup hands the exact obligation to the existing parent queue;
    preserving supervisor state makes a failed handback recoverable on restart.

    When every owned attempt ended normally with PASS and only Hearting's own
    settlement remains, the notice waits for the same settling ceiling the join
    uses (SETTLING_NOTICE_SECONDS), is never recorded when settlement finishes
    first, and past that ceiling is the ordinary attention notice. Anything else -- an open row, a live or unknown process --
    is noticed at once, as before.
    """
    from dispatch_completion_join import SETTLING_NOTICE_SECONDS
    from dispatch_supervision import materialize, settlement_only
    owned, pending = _owned_pending(args)
    settling_since = None
    while pending and settlement_only(Path(args.jobs), sorted(owned)):
        settling_since = clock() if settling_since is None else settling_since
        if clock() - settling_since >= SETTLING_NOTICE_SECONDS:
            break
        # The same cadence as a retained recovery retry; nothing is closed or signalled here.
        sleep(max(args.interval, 30.0))
        owned, pending = _owned_pending(args)
    if pending:
        materialize(Path(args.jobs), owned, reason="supervisor-exited")
        return False
    _remove_supervisor_state(args)
    return True


def reconcile_orphan_cascade(args) -> int:
    result = _run_registry("orphan-status", args)
    return 0 if _finish_recovery(args) else (result.returncode or 70)


def recover_waiting_continuation(args) -> bool | None:
    """Keep an adopted continuation's child when its waiting supervisor ended.

    The normal start path already reuses settled stages and claims one owner.
    Do not cascade these children before that path can consume their results.
    A real terminal handoff or an intentional parent close still wins.
    """
    import owner_route_binding as owner
    import route_parent_close
    from dispatch_completion_join import current_children
    from dispatch_supervision import _pending, _rows, materialize
    from dispatch_attempt_policy import readable_result, verdict_pass, terminal_conflict_pending

    status, metadata = attempt_record(args.jobs, args.attempt_id)
    if (status not in OPEN | {"done"}
            or metadata.get("worker_type") != "owner"
            or route_parent_close.row_requested(metadata, args.jobs)):
        return None
    if (readable_result(metadata) or verdict_pass(metadata) or terminal_conflict_pending(metadata)
            or metadata.get("failure_class") in {"cancelled", "capacity", "auth", "permission"}):
        return None
    state = read_supervisor_phase_state(
        dispatch_state_root(args.jobs) / "supervisor-state" / f"{args.attempt_id}.json",
        args.attempt_id)
    if state is None or state.phase not in {"parked", "recovery"}:
        return None
    observed, _phase, _meta = observed_owner_lifecycle(args)
    if observed is None or observed.process_state != "quiescent":
        return None
    terminal = classify_supervisor_log(metadata.get("log_file"), metadata.get("harness", "unknown"))
    if terminal.failure_class in {"pass", "fail", "blocked", "cancelled", "capacity", "auth", "permission"}:
        return None
    binding, binding_status = owner.resolve_owner_route_lifecycle(args.jobs, owner_attempt_id=args.attempt_id)
    if (binding is None or binding_status != "owner-route-advance-current"
            or binding.route_id == metadata.get("owner_route_id")):
        return None
    children = current_children(Path(args.jobs), args.attempt_id,
                                route_id=binding.route_id, route_hash=binding.route_hash)
    attempts = {child.attempt_id for child in children} - set(state.delivered_attempt_ids)
    if not attempts:
        return None
    while _pending(_rows(Path(args.jobs), attempts, require_unique=True),
                   sorted(attempts), Path(args.jobs)):
        _status, current = attempt_record(args.jobs, args.attempt_id)
        if _status not in OPEN | {"done"} or route_parent_close.row_requested(current, args.jobs):
            return None
        # Nothing is closed or started before this wait ends.
        follow_installed_release(args)
        time.sleep(args.interval)
    _status, current = attempt_record(args.jobs, args.attempt_id)
    if _status not in OPEN | {"done"} or route_parent_close.row_requested(current, args.jobs):
        return None
    if readable_result(current) or verdict_pass(current) or terminal_conflict_pending(current):
        return None
    terminal = classify_supervisor_log(current.get("log_file"), current.get("harness", "unknown"))
    if terminal.failure_class in {"pass", "fail", "blocked", "cancelled", "capacity", "auth", "permission"}:
        return None
    # Closing just this extinct owner leaves the settled children intact. The
    # current continuation has no owner yet; ordinary start supplies it.
    outcome = reconcile_supervisor_terminal(Path(args.jobs), args.attempt_id, terminal)
    if outcome not in {"closed", "already-terminal"}:
        materialize(Path(args.jobs), {args.attempt_id}, reason="supervisor-exited")
        return False
    from parent_next_directive import entrypoint
    result = subprocess.run([
        sys.executable, entrypoint(args.agent_home, "utilities/capability-route.py"),
        "start", "--route", binding.route_file, "--jobs", str(args.jobs),
    ], text=True, capture_output=True, check=False, timeout=300)
    receipts = []
    for line in result.stdout.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("route_id") == binding.route_id:
            receipts.append(value)
    if result.returncode == 0 and receipts and (
            receipts[-1].get("owner_started") or receipts[-1].get("state") == "completed"
            or receipts[-1].get("auto_resume")):
        _remove_supervisor_state(args)
        return True
    materialize(Path(args.jobs), {args.attempt_id}, reason="supervisor-exited")
    return False


def reconcile_exact_exit(args) -> int:
    """Close any exact dead owner, even when it failed before child launch.

    Preserve orphan semantics first so unfinished children are cascaded. For a
    registered supervisor, classify its terminal envelope next so capacity,
    authentication, and protocol failures retain their typed reason. Finally,
    use the general exact-PID reconciler for legacy rows without a supervisor
    envelope. Every transition remains exact-attempt and conditionally atomic.
    """

    observed, _phase, _metadata = observed_owner_lifecycle(args)
    if observed is not None and observed.process_state != "quiescent":
        _finish_recovery(args)
        return 70
    recovered = recover_waiting_continuation(args)
    if recovered is not None:
        return 0 if recovered else 70
    orphan_result = _run_registry("orphan-status", args)
    status, metadata = attempt_record(args.jobs, args.attempt_id)

    has_supervisor_envelope = bool(
        metadata.get("harness") or metadata.get("log_file")
    )
    if status in OPEN and has_supervisor_envelope:
        terminal = classify_supervisor_log(
            metadata.get("log_file"), metadata.get("harness", "unknown")
        )
        try:
            outcome = reconcile_supervisor_terminal(args.jobs, args.attempt_id, terminal)
        except Exception:
            _finish_recovery(args)
            return 70
        # SD-111 P2 trigger 1: dispatch_supervisor_terminal cannot import
        # dispatch_completion_join (circular), so its own docstring asks the
        # caller to close the gap when the row actually closed.
        if outcome == "closed":
            materialize_after_terminal_close(Path(args.jobs), args.attempt_id)
        status = attempt_status(args.jobs, args.attempt_id)

    exact_result = None
    if status in OPEN:
        exact_result = _run_registry("reconcile", args)
        status = attempt_status(args.jobs, args.attempt_id)

    if _finish_recovery(args):
        return 0
    if exact_result is not None and exact_result.returncode:
        return exact_result.returncode
    return orphan_result.returncode or 70


def watch(args) -> int:
    import route_parent_close
    registry = RegistryObservation(args.jobs, args.attempt_id)
    closing = False
    while True:
        # A close in progress is re-judged every interval, as before.
        status, metadata, close_requested = registry.refresh(force=closing)
        closing = close_requested
        if close_requested:
            result = route_parent_close.recover_attempt(args.jobs, metadata)
            if result and result.get("state") == "cancelled":
                _remove_supervisor_state(args)
                return 0
            follow_installed_release(args)
            time.sleep(args.interval)
            continue
        if status not in OPEN:
            # A terminal registry word can precede owner teardown (notably
            # Fleet kill). Keep the supervisor state and wait for the exact
            # recorded owner identity to disappear before cascading.
            if (
                status in PARENT_EXTINCTION_TERMINAL_STATUSES
                and process_start(args.pid) == args.pid_start
            ):
                time.sleep(args.interval)
                continue
            recovered = recover_waiting_continuation(args)
            if recovered is not None:
                return 0 if recovered else 70
            return (
                reconcile_orphan_cascade(args)
                if status in PARENT_EXTINCTION_TERMINAL_STATUSES
                else 0
            )
        if process_start(args.pid) != args.pid_start:
            observed, _phase, _metadata = observed_owner_lifecycle(args)
            if observed is not None and observed.process_state != "quiescent":
                time.sleep(args.interval)
                continue
            break
        time.sleep(args.interval)
    return reconcile_exact_exit(args)


def _resume_batch_duties(jobs) -> None:
    """Ask the shared runner to reconnect retained registered batch observers.

    The runner owns the lock-guarded, bounded pass: duties already observed are
    skipped, completed duties are never replayed, and the original assignment is
    never relaunched. This watcher only wakes it; every failure is swallowed and
    the next cycle retries.
    """
    try:
        import dispatch_batch_obligations as batch_obligations
    except Exception:
        return
    try:
        batch_obligations.request_runner(jobs)
    except Exception:
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    from dispatch_contract import inherited_jobs_argument
    parser.add_argument("--jobs", type=Path, **inherited_jobs_argument())
    parser.add_argument("--agent-home", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--pid-start", required=True)
    parser.add_argument("--interval", type=float, default=2.0)
    release_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(release_argv)
    # The same arguments start this watch again on the installed release.
    args.release_argv = release_argv
    if args.pid <= 0 or args.interval <= 0:
        parser.error("--pid and --interval must be positive")
    args.jobs = args.jobs.resolve()
    args.agent_home = args.agent_home.resolve()
    # Bind the exact PID/start extinction observed by this watcher to the
    # namespace in which those numbers were meaningful. The registry resolver
    # independently compares this value with both its current namespace and
    # the parent's recorded launch observer before consuming the proof.
    args.pid_observer_ns = process_namespace_identity() or ""
    while True:
        try:
            _resume_batch_duties(args.jobs)
            result = watch(args)
            _status, metadata = attempt_record(args.jobs, args.attempt_id)
            import route_parent_close
            if route_parent_close.row_requested(metadata, args.jobs):
                closing = route_parent_close.recover_attempt(args.jobs, metadata)
                if not closing or closing.get("state") != "cancelled":
                    follow_installed_release(args)
                    time.sleep(args.interval)
                    continue
            return result
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            print(f"orphan-recovery-retained attempt_id={args.attempt_id} reason={exc}", file=sys.stderr, flush=True)
            # A retained recovery is retried from the registry; newer code may settle it.
            follow_installed_release(args)
            time.sleep(max(args.interval, 30.0))


if __name__ == "__main__":
    raise SystemExit(main())
