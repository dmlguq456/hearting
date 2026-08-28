#!/usr/bin/env python3
"""Bounded per-attempt heartbeat and progress watchdog (SD-58)."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import glob
import json
import hashlib
import os
from pathlib import Path
import signal
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "utilities")]
from tools.fleet.model import (  # noqa: E402
    ATTEMPT_CLASSIFIER_SOURCE, PROGRESS_PHASES, classify_attempt_evidence,
    deterministic_progress_fingerprint,
)
from dispatch_contract import (  # noqa: E402
    DispatchContractError,
    anchored_capacity_failure,
    attempt_process_quiescence,
    authoritative_process_identities,
    close_attempt_row_if,
    dispatch_state_root,
    process_start_ticks,
    process_state,
    resolve_agent_home,
    validate_attempt_metadata,
)
from codex_dispatch_terminal import inspect_terminal_log  # noqa: E402
from dispatch_completion_join import materialize_after_terminal_close  # noqa: E402

KINDS = {"registry", "tool", "file", "artifact", "test", "terminal"}


def defer_terminal_until_quiescent(state, metadata):
    """Keep semantic terminal evidence cached while withholding fallback readiness."""

    if not state.get("terminal_action"):
        return state
    process = attempt_process_quiescence(metadata)
    if process.state == "quiescent":
        return state
    visible = dict(state)
    visible["semantic_terminal_action"] = state["terminal_action"]
    visible["terminal_action"] = ""
    visible["process_state"] = process.state
    visible["process_reason"] = process.reason
    visible["action"] = (
        "draining" if process.state == "live" else "fail-closed-process-unverifiable"
    )
    return visible


def meta(pipe):
    return dict(part.split("=", 1) for part in pipe.split(",") if "=" in part)


def exact_row(jobs, attempt_id):
    found = None
    if jobs.is_file():
        for line in jobs.read_text(encoding="utf-8", errors="replace").splitlines():
            fields = line.split("\t")
            if len(fields) == 6 and meta(fields[5]).get("attempt_id") == attempt_id:
                found = (fields, meta(fields[5]))
    return found


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def state_paths(state_root, attempt):
    name = attempt.replace("/", "_")
    return (state_root / "heartbeats" / f"{name}.json",
            state_root / "watchdog" / f"{name}.json",
            state_root / "watchdog" / f"{name}.lock")


def verification_lease(args, now):
    """Return one exact live runtime verification lease, else ``None``."""

    path = (
        dispatch_state_root(args.jobs)
        / "verification-leases"
        / f"{args.attempt_id.replace('/', '_')}.json"
    )
    value = read_json(path)
    digest = value.get("command_digest")
    pid = value.get("pid")
    pgid = value.get("pgid")
    deadline = value.get("deadline")
    if (
        value.get("schema_version") != 1
        or value.get("attempt_id") != args.attempt_id
        or value.get("route_id") != args.route_id
        or value.get("route_node") != args.route_node
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
        or not isinstance(pid, int)
        or not isinstance(pgid, int)
        or pid <= 0
        or pgid != pid
        or not isinstance(deadline, (int, float))
        or deadline < now
        or process_start_ticks(pid) != str(value.get("pid_start") or "")
        or process_state(pid) == "Z"
        or verification_process_digest(pid) != digest
    ):
        return None
    try:
        if os.getpgid(pid) != pgid:
            return None
    except OSError:
        return None
    return value


def verification_process_digest(pid):
    """Hash the exact live argv without trusting the lease's copied command."""

    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes().rstrip(b"\0")
    except OSError:
        return ""
    if not raw:
        return ""
    return hashlib.sha256(raw).hexdigest()


def require_row(args):
    row = exact_row(args.jobs, args.attempt_id)
    if row is None:
        raise DispatchContractError("progress-attempt-missing", args.attempt_id)
    if row[1].get("route_id") != args.route_id or row[1].get("route_node") != args.route_node:
        raise DispatchContractError("progress-route-mismatch", args.attempt_id)
    validate_attempt_metadata(row[1])
    return row


def proc_evidence(metadata):
    candidates = authoritative_process_identities(metadata)
    selected = None
    fallback = None
    for identity in candidates:
        actual = process_start_ticks(identity.pid) or ""
        alive = bool(actual) and process_state(identity.pid) != "Z"
        evidence = (identity, actual, alive, bool(alive and actual == identity.expected_start))
        if fallback is None:
            fallback = evidence
        if evidence[3]:
            selected = evidence
            break
    selected = selected or fallback
    if selected is None:
        pid, expected, actual, alive, start_match, source = None, "", "", None, None, None
    else:
        identity, actual, alive, start_match = selected
        pid, expected, source = identity.pid, identity.expected_start, identity.source
    raw_local = metadata.get("pid", "")
    raw_host = metadata.get("pid_host", "")
    return {
        "pid": pid,
        "proc_start": expected,
        "actual_proc_start": actual,
        "pid_alive": alive,
        "proc_start_match": start_match,
        "pid_scope": metadata.get("pid_scope"),
        "pid_authoritative": selected is not None,
        "pid_identity_source": source,
        "pid_local": int(raw_local) if raw_local.isdigit() else None,
        "pid_local_start": metadata.get("pid_start"),
        "pid_host": int(raw_host) if raw_host.isdigit() else None,
        "pid_host_start": metadata.get("pid_host_start"),
        "pid_host_ns": metadata.get("pid_host_ns"),
        "pid_ns": metadata.get("pid_ns"),
        "pid_observer_ns": metadata.get("pid_observer_ns"),
        "pid_host_proof": metadata.get("pid_host_proof"),
        "pgid": metadata.get("pgid"),
    }


_SIGNAL_IDENTITY_KEYS = (
    "pid", "pid_start", "pid_scope", "pid_host", "pid_host_start",
    "pid_host_ns", "pid_ns", "pid_observer_ns", "pid_host_proof", "pgid",
)


def _same_signal_identity(current, signalled):
    return all(current.get(key, "") == signalled.get(key, "") for key in _SIGNAL_IDENTITY_KEYS)


def signal_authoritative_process_group(args, sig):
    """Revalidate the exact row, PID/start and group leader immediately before killpg.

    Heartbeat state is intentionally absent from this path.  It can keep a
    namespace-local attempt visible in UI, but only namespace-bound process
    evidence grants signal authority.
    """
    fields, metadata = require_row(args)
    if fields[1] not in {"open", "running"}:
        raise DispatchContractError("progress-signal-row-terminal", args.attempt_id)
    process = attempt_process_quiescence(metadata)
    if process.state == "quiescent":
        raise ProcessLookupError(process.reason)
    if process.state != "live":
        raise DispatchContractError("progress-signal-identity-unverifiable", process.reason)

    for identity in authoritative_process_identities(metadata):
        pid = identity.pid
        if process_start_ticks(pid) != identity.expected_start or process_state(pid) == "Z":
            continue
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            continue
        if pgid != pid:
            continue
        recorded_pgid = metadata.get("pgid", "")
        if identity.source == "local" and recorded_pgid:
            if not recorded_pgid.isdigit() or int(recorded_pgid) != pgid:
                continue
        # Final adjacent check closes the gap between candidate selection and signal.
        if (
            process_start_ticks(pid) != identity.expected_start
            or process_state(pid) == "Z"
            or os.getpgid(pid) != pid
        ):
            continue
        os.killpg(pid, sig)
        return pid, metadata
    raise DispatchContractError("progress-signal-group-unverifiable", args.attempt_id)


def scoped_file_signature(metadata, worktree=""):
    """Bounded signature of declared write-scope paths; never reads file prose."""
    entries = []
    roots = []
    for raw_root in (worktree, metadata.get("artifact_root", "")):
        root = Path(raw_root)
        if root.is_absolute() and root not in roots:
            roots.append(root)
    declared = metadata.get("write_scope", "").split(";")
    route_file = Path(metadata.get("route_file", ""))
    if route_file.is_absolute() and route_file.is_file():
        cycle_root = route_file.parent.parent if route_file.parent.name == "_internal" else route_file.parent
        if cycle_root not in roots:
            roots.append(cycle_root)
        try:
            route = json.loads(route_file.read_text(encoding="utf-8"))
            node = next((item for item in route.get("nodes", [])
                         if item.get("id") == metadata.get("route_node")), {})
            declared.extend(str(item) for item in node.get("outputs", []))
        except (OSError, TypeError, ValueError):
            pass
    for raw in declared:
        raw = raw.strip()
        if not raw:
            continue
        relative = not Path(raw).is_absolute()
        if relative and ".." in Path(raw).parts:
            continue
        patterns = [raw] if not relative else [str(root / raw) for root in roots]
        for pattern in patterns:
            candidates = glob.iglob(pattern, recursive=True) if glob.has_magic(pattern) else iter((pattern,))
            for candidate in candidates:
                item = Path(candidate)
                if item.is_dir() and not glob.has_magic(pattern):
                    nested = item.rglob("*")
                else:
                    nested = (item,)
                for item in nested:
                    if not item.is_file():
                        continue
                    if relative:
                        try:
                            resolved = item.resolve(strict=False)
                            inside = False
                            for root in roots:
                                try:
                                    resolved.relative_to(root.resolve(strict=False))
                                    inside = True
                                    break
                                except ValueError:
                                    continue
                            if not inside:
                                continue
                        except OSError:
                            continue
                    try:
                        stat = item.stat()
                    except OSError:
                        continue
                    entries.append((str(item), stat.st_size, stat.st_mtime_ns))
                    if len(entries) >= 256:
                        break
                if len(entries) >= 256:
                    break
            if len(entries) >= 256:
                break
        if len(entries) >= 256:
            break
    if not entries:
        return ""
    return hashlib.sha256(json.dumps(sorted(entries), separators=(",", ":")).encode()).hexdigest()


def inspect(args, now):
    fields, metadata = require_row(args)
    heartbeat, _, _ = state_paths(dispatch_state_root(args.jobs), args.attempt_id)
    verdict = classify_attempt_evidence({
        **proc_evidence(metadata), "attempt_id": args.attempt_id,
        "route_id": args.route_id, "route_node": args.route_node,
        "registry_transition": {"status": fields[1], "note": metadata.get("note", "")},
        "artifact_signature": metadata.get("artifact_sha256") or None,
        "file_signature": scoped_file_signature(metadata, fields[3]),
        "heartbeat": read_json(heartbeat) or None,
    }, now)
    if verdict is None:
        raise DispatchContractError("progress-exact-identity-required", args.attempt_id)
    verdict["row_status"] = fields[1]
    return verdict


def heartbeat(args, now):
    require_row(args)
    if args.phase not in PROGRESS_PHASES or args.kind not in KINDS or not args.evidence:
        raise DispatchContractError("progress-evidence-invalid", "phase/kind/evidence")
    hb_path, _, lock_path = state_paths(dispatch_state_root(args.jobs), args.attempt_id)
    with locked(lock_path):
        old = read_json(hb_path)
        if old.get("phase") in PROGRESS_PHASES and PROGRESS_PHASES.index(args.phase) < PROGRESS_PHASES.index(old["phase"]):
            raise DispatchContractError("progress-phase-regression", f"{old['phase']}->{args.phase}")
        evidence_digest = deterministic_progress_fingerprint({
            "attempt_id": args.attempt_id, "route_id": args.route_id,
            "route_node": args.route_node, args.kind: args.evidence,
            "heartbeat": {"phase": args.phase, "evidence": args.evidence},
        })
        if old.get("phase") == args.phase and old.get("evidence_digest") == evidence_digest:
            return old
        value = {"schema_version": 1, "attempt_id": args.attempt_id,
                 "route_id": args.route_id, "route_node": args.route_node,
                 "phase": args.phase, "sequence": int(old.get("sequence", 0)) + 1,
                 "kind": args.kind, "evidence": args.evidence[:512],
                 "evidence_digest": evidence_digest, "updated_at": now}
        write_json(hb_path, value)
        return value


def capacity_log_evidence(state_root, slug, metadata):
    """Return a bounded log path only for an anchored terminal capacity line."""
    log_dir = state_root / "logs"
    exact = Path(metadata.get("log_file", ""))
    paths = [exact] if exact.is_absolute() else sorted(log_dir.glob(f"{slug}.*"))
    for path in paths:
        if not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                try:
                    handle.seek(-8192, os.SEEK_END)
                except OSError:
                    handle.seek(0)
                tail = handle.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        if anchored_capacity_failure(tail):
            return path
    return None


def watchdog(args, now):
    fields, metadata = require_row(args)
    verdict = inspect(args, now)
    hb_path, wd_path, lock_path = state_paths(dispatch_state_root(args.jobs), args.attempt_id)
    hb = read_json(hb_path)
    background = verification_lease(args, now)
    fingerprint = deterministic_progress_fingerprint({
        "attempt_id": args.attempt_id, "route_id": args.route_id,
        "route_node": args.route_node, "heartbeat": hb or None,
        "registry_transition": {
            "status": fields[1], "note": metadata.get("note", ""),
        },
        "file_signature": scoped_file_signature(metadata, fields[3]),
        "artifact_signature": metadata.get("artifact_sha256") or None,
        "background_verification": (
            {
                "command_digest": background.get("command_digest"),
                "pid": background.get("pid"),
                "pid_start": background.get("pid_start"),
                "deadline": background.get("deadline"),
            }
            if background
            else None
        ),
    })
    with locked(lock_path):
        state = read_json(wd_path)
        capacity_path = capacity_log_evidence(dispatch_state_root(args.jobs), fields[4], metadata)
        if metadata.get("note") == "dead-capacity" or capacity_path is not None:
            closed = metadata.get("note") == "dead-capacity"
            exact = inspect(args, now)
            if not closed and not args.apply:
                state.update({"action": "would-close-capacity", "failure_class": "capacity",
                              "capacity_log": str(capacity_path or ""), "observed_at": now})
                write_json(wd_path, state)
                return state
            signalled_metadata = metadata
            if not closed:
                try:
                    _, signalled_metadata = signal_authoritative_process_group(args, signal.SIGINT)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    state.update({"action": "fail-closed-capacity-signal-denied",
                                  "observed_at": now})
                    write_json(wd_path, state)
                    return state
                except DispatchContractError as error:
                    state.update({"action": "fail-closed-capacity-identity",
                                  "process_reason": error.reason,
                                  "observed_at": now})
                    write_json(wd_path, state)
                    return state

            if not closed:
                def still_exact(row_fields):
                    row_meta = meta(row_fields[5])
                    return bool(
                        row_fields[1] in {"open", "running"}
                        and row_meta.get("route_id") == args.route_id
                        and row_meta.get("route_node") == args.route_node
                        and _same_signal_identity(row_meta, signalled_metadata)
                    )

                closed = close_attempt_row_if(
                    args.jobs, args.attempt_id, "dead-capacity", still_exact,
                    evidence={"failure_class": "capacity",
                              "detected_by": "progress-watchdog",
                              "capacity_log": str(capacity_path or "")},
                )
                if closed:
                    materialize_after_terminal_close(args.jobs, args.attempt_id)
                if not closed:
                    refreshed = exact_row(args.jobs, args.attempt_id)
                    closed = bool(refreshed and refreshed[1].get("note") == "dead-capacity")
            if not closed:
                state.update({"action": "fail-closed-capacity-identity",
                              "observed_at": now})
                write_json(wd_path, state)
                return state
            state.update({"schema_version": 1, "attempt_id": args.attempt_id,
                          "route_id": args.route_id, "route_node": args.route_node,
                          "classifier_source": ATTEMPT_CLASSIFIER_SOURCE,
                          "action": "dead-capacity", "terminal_action": "dead-capacity",
                          "failure_class": "capacity", "model": metadata.get("model", "unknown"),
                          "capacity_log": str(capacity_path or ""), "observed_at": now,
                          "fingerprint": fingerprint})
            write_json(wd_path, state)
            return defer_terminal_until_quiescent(state, metadata)
        if state.get("terminal_action"):
            return defer_terminal_until_quiescent(state, metadata)
        previous = state.get("fingerprint", "")
        last_progress = float(state.get("last_progress_at", hb.get("updated_at", now)))
        quiet = int(state.get("quiet_windows", 0))
        if previous and fingerprint and fingerprint != previous:
            quiet, last_progress = 0, now
            state["last_window_at"] = now
        if fields[1] not in {"open", "running"}:
            state.update({"action": "registry-terminal", "terminal_action": "registry-terminal",
                          "observed_at": now, "verdict": verdict["state"]})
            write_json(wd_path, state)
            return defer_terminal_until_quiescent(state, metadata)
        if background is not None:
            state.update(
                {
                    "schema_version": 1,
                    "attempt_id": args.attempt_id,
                    "route_id": args.route_id,
                    "route_node": args.route_node,
                    "classifier_source": ATTEMPT_CLASSIFIER_SOURCE,
                    "fingerprint": fingerprint,
                    "last_progress_at": now,
                    "last_window_at": now,
                    "quiet_windows": int(state.get("quiet_windows", 0)),
                    "warning": 0,
                    "observed_at": now,
                    "verdict": verdict["state"],
                    "action": "background-active",
                    "verification_command_digest": background["command_digest"],
                    "verification_deadline": background["deadline"],
                }
            )
            write_json(wd_path, state)
            return state
        if verdict["state"] == "dead":
            terminal = inspect_terminal_log(metadata.get("log_file"))
            terminal_note = terminal.get("failure_note") if terminal else ""
            if terminal_note and args.apply:
                def still_exact(fields):
                    row_meta = meta(fields[5])
                    return (
                        fields[1] in {"open", "running"}
                        and row_meta.get("pid") == metadata.get("pid")
                        and row_meta.get("pid_start") == metadata.get("pid_start")
                        and row_meta.get("log_file") == metadata.get("log_file")
                    )

                closed = close_attempt_row_if(
                    args.jobs,
                    args.attempt_id,
                    terminal_note,
                    still_exact,
                    evidence={
                        "detected_by": "progress-terminal-handoff",
                        "failure_class": terminal["failure_class"],
                        "terminal_event": terminal["terminal_event"],
                        "log_file": terminal["log_file"],
                    },
                )
                if closed:
                    materialize_after_terminal_close(args.jobs, args.attempt_id)
                if not closed:
                    refreshed = exact_row(args.jobs, args.attempt_id)
                    closed = bool(
                        refreshed and refreshed[1].get("note") == terminal_note
                    )
                if not closed:
                    state.update({
                        "action": "fail-closed-terminal-identity",
                        "observed_at": now,
                    })
                    write_json(wd_path, state)
                    return state
                state.update({
                    "action": terminal_note,
                    "terminal_action": terminal_note,
                    "observed_at": now,
                    "verdict": "dead",
                    "failure_class": terminal["failure_class"],
                    "classifier_source": ATTEMPT_CLASSIFIER_SOURCE,
                    "fingerprint": fingerprint,
                })
                write_json(wd_path, state)
                return defer_terminal_until_quiescent(state, metadata)
            # Natural child exit is a terminal observation, not a watchdog
            # identity failure. Harvest owns the success/failure verdict.
            state.update({"action": "process-exited", "terminal_action": "process-exited",
                          "observed_at": now, "verdict": "dead",
                          "classifier_source": ATTEMPT_CLASSIFIER_SOURCE,
                          "fingerprint": fingerprint})
            write_json(wd_path, state)
            return defer_terminal_until_quiescent(state, metadata)
        if now - float(state.get("last_window_at", last_progress)) >= args.progress_window_seconds:
            quiet += 1; state["last_window_at"] = now
        state.update({"schema_version": 1, "attempt_id": args.attempt_id,
                      "route_id": args.route_id, "route_node": args.route_node,
                      "classifier_source": ATTEMPT_CLASSIFIER_SOURCE,
                      "fingerprint": fingerprint, "last_progress_at": last_progress,
                      "quiet_windows": quiet, "warning": int(quiet >= 1),
                      "observed_at": now, "verdict": verdict["state"]})
        if quiet >= args.watchdog_max_windows:
            exact = inspect(args, now)  # immediate identity revalidation
            if exact["state"] != "working" or exact.get("pid_authoritative") is not True:
                state["action"] = "fail-closed-identity"
            elif not args.apply:
                state["action"] = "would-interrupt"
            else:
                try:
                    signalled_pid, signalled_metadata = signal_authoritative_process_group(
                        args, signal.SIGINT
                    )
                except ProcessLookupError:
                    state.update({"action": "process-exited",
                                  "terminal_action": "process-exited"})
                    write_json(wd_path, state)
                    return state
                except PermissionError:
                    state["action"] = "fail-closed-signal-denied"
                    write_json(wd_path, state)
                    return state
                except DispatchContractError as error:
                    state["action"] = "fail-closed-identity"
                    state["process_reason"] = error.reason
                    write_json(wd_path, state)
                    return state

                def still_exact(row_fields):
                    row_meta = meta(row_fields[5])
                    return bool(
                        row_fields[1] in {"open", "running"}
                        and row_meta.get("route_id") == args.route_id
                        and row_meta.get("route_node") == args.route_node
                        and _same_signal_identity(row_meta, signalled_metadata)
                    )

                closed = close_attempt_row_if(
                    args.jobs, args.attempt_id, "dead-no-progress", still_exact,
                    evidence={"classifier_source": ATTEMPT_CLASSIFIER_SOURCE,
                              "watchdog_windows": str(quiet)},
                )
                if not closed:
                    state["action"] = "fail-closed-row-changed"
                else:
                    materialize_after_terminal_close(args.jobs, args.attempt_id)
                    state["action"] = "interrupted"
                    state["terminal_action"] = "dead-no-progress"
                    state["signalled_pid"] = signalled_pid
        else:
            state["action"] = "warning" if quiet else "observe"
        write_json(wd_path, state)
        return defer_terminal_until_quiescent(state, metadata)


def main(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("operation", choices=("heartbeat", "inspect", "watchdog"))
    p.add_argument("--attempt-id", required=True); p.add_argument("--route-id", required=True)
    p.add_argument("--route-node", required=True); p.add_argument("--jobs", type=Path, required=True)
    p.add_argument("--agent-home", type=Path); p.add_argument("--phase", choices=PROGRESS_PHASES)
    p.add_argument("--kind", choices=sorted(KINDS)); p.add_argument("--evidence")
    p.add_argument("--progress-window-seconds", type=float, default=300.0)
    p.add_argument("--watchdog-max-windows", type=int, default=2)
    p.add_argument("--apply", action="store_true"); p.add_argument("--now", type=float, help=argparse.SUPPRESS)
    args = p.parse_args(argv[1:]); args.agent_home = (args.agent_home or resolve_agent_home()).resolve()
    args.jobs = args.jobs.resolve(); now = args.now if args.now is not None else time.time()
    try:
        result = heartbeat(args, now) if args.operation == "heartbeat" else inspect(args, now) if args.operation == "inspect" else watchdog(args, now)
    except (DispatchContractError, OSError, ValueError) as exc:
        print(f"check=failed\nreason={getattr(exc, 'reason', 'progress-error')}\ndetail={exc}"); return 65
    print("check=ok")
    for key, value in result.items():
        print(f"{key}={json.dumps(value, sort_keys=True, separators=(',', ':')) if isinstance(value, (dict, list)) else value}")
    return 0


if __name__ == "__main__": raise SystemExit(main(sys.argv))
