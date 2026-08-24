#!/usr/bin/env python3
"""Own registered-attempt title/NOW refresh independently of Fleet.

The adapter wrappers start one supervisor while the governed worker is still
behind its launch fence.  The supervisor follows only the exact attempt log and
exact governed PID/start identity, schedules initial/periodic/final refreshes,
and records observational state.  It has no completion or signal authority.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from dispatch_contract import (  # noqa: E402
    DispatchContractError,
    PID_HOST_NAMESPACE_PROOF,
    annotate_attempt_row_if,
    parse_registry_metadata,
    process_launch_identity,
    process_namespace_identity,
    process_observation,
)


ATTEMPT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
HARNESSES = {"claude", "codex", "opencode"}
OWNER_SCHEMA = 1
OWNER_KIND = "dispatch-v1"
DEFAULT_POLL = 2.0
DEFAULT_INITIAL_DELAY = 3.0
DEFAULT_PERIODIC_DEBOUNCE = 90
DEFAULT_FINAL_GRACE = 75.0
DEFAULT_LOG_QUIET = 1.0
SESSION_ANNOUNCE_SCAN = 1 << 16


def summary_sid(attempt_id: str) -> str:
    if not ATTEMPT_RE.fullmatch(attempt_id or ""):
        raise ValueError("invalid attempt id")
    return "dispatch-" + attempt_id


def _state_root() -> Path:
    explicit = os.environ.get("FLEET_TITLE_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    xdg = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return (xdg / "agent-fleet" / "titles").resolve()


def owner_root() -> Path:
    return _state_root() / ".dispatch-owners"


def owner_state_path(harness: str, attempt_id: str) -> Path:
    _validate_identity(harness, attempt_id)
    return owner_root() / harness / f"{attempt_id}.json"


def owner_lock_path(harness: str, attempt_id: str) -> Path:
    return owner_state_path(harness, attempt_id).with_suffix(".lock")


def ensure_lock_path(harness: str, attempt_id: str) -> Path:
    return owner_state_path(harness, attempt_id).with_suffix(".ensure.lock")


def _validate_identity(harness: str, attempt_id: str) -> None:
    if harness not in HARNESSES:
        raise ValueError("unsupported harness")
    if not ATTEMPT_RE.fullmatch(attempt_id or ""):
        raise ValueError("invalid attempt id")


def _validate_transcript(path: str | Path, harness: str, attempt_id: str) -> Path:
    _validate_identity(harness, attempt_id)
    target = Path(path).expanduser().resolve()
    expected = f".{attempt_id}.{harness}.jsonl"
    if not target.is_absolute() or not target.name.endswith(expected):
        raise ValueError("attempt log identity mismatch")
    if not target.parent.is_dir():
        raise ValueError("attempt log directory missing")
    return target


def _validate_prompt(path: str | Path | None, log_path: Path) -> Path | None:
    if not path:
        return None
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("prompt path is not a regular file")
    target = candidate.resolve()
    expected = log_path.name[:-len(".jsonl")] + ".prompt.txt"
    if target.parent != log_path.parent or target.name != expected:
        raise ValueError("prompt path identity mismatch")
    return target


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _proc_live(pid: int, expected_start: str) -> tuple[bool, str]:
    visibility, actual_start, state = process_observation(pid)
    if visibility != "present":
        return False, visibility
    if actual_start != expected_start:
        return False, "pid-reused"
    if state == "Z":
        return False, "zombie"
    return True, "live"


def _owner_live(metadata: dict[str, str]) -> bool:
    raw_pid = metadata.get("summary_owner_pid", "")
    expected = metadata.get("summary_owner_pid_start", "")
    harness = metadata.get("harness") or metadata.get("child_harness") or ""
    attempt_id = metadata.get("attempt_id", "")
    raw_state = metadata.get("summary_state_file", "")
    observer_namespace = metadata.get("summary_owner_pid_observer_ns", "")
    if not raw_pid.isdigit() or not expected or not raw_state or not observer_namespace:
        return False
    try:
        expected_path = owner_state_path(harness, attempt_id)
        state_path = Path(raw_state).expanduser().resolve()
        if state_path != expected_path or state_path.stat().st_size > 16384:
            return False
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(state, dict) or any((
        state.get("schema_version") != OWNER_SCHEMA,
        state.get("status") != "active",
        state.get("attempt_id") != attempt_id,
        state.get("harness") != harness,
        state.get("pid") != int(raw_pid),
        state.get("proc_start") != expected,
        state.get("observer_namespace") != observer_namespace,
    )):
        return False
    current_namespace = process_namespace_identity()
    if current_namespace == observer_namespace:
        return _proc_live(int(raw_pid), expected)[0]
    host_pid = metadata.get("summary_owner_pid_host", "")
    host_start = metadata.get("summary_owner_pid_host_start", "")
    if (
        host_pid.isdigit()
        and host_start
        and current_namespace == metadata.get("summary_owner_pid_host_ns")
        and metadata.get("summary_owner_pid_host_proof") == PID_HOST_NAMESPACE_PROOF
    ):
        return _proc_live(int(host_pid), host_start)[0]
    return False


def _owner_env() -> dict[str, str]:
    env = dict(os.environ)
    env["AGENT_SESSION_ROLE"] = "worker"
    env["AGENT_SUMMARY_OWNER"] = "dispatch"
    return env


def launch_summary_owner(
    *, attempt_id: str, harness: str, transcript: str | Path,
    target_pid: int, target_start: str, prompt_path: str | Path | None = None,
) -> dict[str, str]:
    """Launch and prove one exact-attempt supervisor; return registry metadata."""

    log_path = _validate_transcript(transcript, harness, attempt_id)
    prompt = _validate_prompt(prompt_path, log_path)
    if target_pid <= 0 or not target_start:
        raise ValueError("exact target identity required")
    live, reason = _proc_live(target_pid, target_start)
    if not live:
        raise ValueError(f"target identity is not live: {reason}")
    state_path = owner_state_path(harness, attempt_id)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        sys.executable, str(Path(__file__).resolve()), "supervise",
        "--attempt-id", attempt_id,
        "--harness", harness,
        "--transcript", str(log_path),
        "--pid", str(target_pid),
        "--proc-start", target_start,
    ]
    if prompt is not None:
        argv += ["--prompt", str(prompt)]
    process = subprocess.Popen(
        argv,
        cwd=str(ROOT),
        env=_owner_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 2.0
    owner_identity: dict[str, str] = {}
    while time.monotonic() < deadline:
        owner_identity = process_launch_identity(process.pid)
        owner_start = owner_identity.get("pid_start", "")
        if owner_start:
            try:
                current = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                current = {}
            if current.get("pid") == process.pid and current.get("status") == "active":
                break
        if process.poll() is not None:
            raise ValueError("summary owner exited before activation")
        time.sleep(0.02)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        raise ValueError("summary owner activation timeout")
    metadata = {
        "summary_owner": OWNER_KIND,
        "summary_sid": summary_sid(attempt_id),
        "summary_owner_pid": str(process.pid),
        "summary_owner_pid_start": owner_start,
        "summary_state_file": str(state_path),
    }
    for key in (
        "pid_scope", "pid_host", "pid_host_start", "pid_host_ns",
        "pid_observer_ns", "pid_host_proof",
    ):
        value = owner_identity.get(key)
        if value:
            metadata["summary_owner_" + key] = value
    return metadata


def _read_sidecar(harness: str, sid: str) -> dict[str, Any]:
    try:
        from fleet import titles

        value = titles.read(sid, harness=harness) or {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _log_signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return 0, 0


def _claude_projects_root() -> Path:
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return Path(home) / "projects"


def _encoded_cwd(cwd: str) -> str:
    # projects dir encoding: '/', '.', '_' -> '-' (same rule as the fleet collector).
    return "".join("-" if ch in "/._" else ch for ch in cwd)


def announced_session(
    path: Path, scan: int = SESSION_ANNOUNCE_SCAN,
) -> tuple[bool, dict[str, str] | None]:
    """Return ``(decided, announcement)`` for the Claude supervisor's session row.

    A supervised owner's attempt log is a receipt log: control rows plus one final
    ``result``, with model text deliberately withheld.  ``dispatch.supervisor.session``
    names the runtime session whose own transcript is the real summary input.

    The summary owner is launched while the governed worker is still behind its
    launch fence, so an empty log means "not yet", not "not supervised" — caching
    that as unsupervised would pin the follower to the receipt log for the whole
    run.  The announcement is emitted before the first turn, so a first row that is
    anything else is a decisive negative.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(max(1, int(scan)))
    except OSError:
        return False, None
    for raw in head.split(b"\n"):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            # A torn trailing line is not evidence either way; keep waiting.
            return False, None
        if not isinstance(row, dict) or row.get("type") != "dispatch.supervisor.session":
            return True, None
        session_id = row.get("session_id")
        if isinstance(session_id, str) and ATTEMPT_RE.fullmatch(session_id):
            return True, {"session_id": session_id, "cwd": row.get("cwd") or ""}
        return True, None
    return False, None


def session_transcript(announced: dict[str, str]) -> Path | None:
    """Resolve one exact runtime transcript for an announced child session.

    Exact ``<session id>.jsonl`` only.  A missing transcript returns None rather
    than borrowing a same-cwd neighbour, which would stamp another session's text
    onto this attempt's NOW line.
    """
    session_id = announced.get("session_id") or ""
    if not ATTEMPT_RE.fullmatch(session_id):
        return None
    root = _claude_projects_root()
    cwd = announced.get("cwd") or ""
    candidates = []
    if cwd:
        candidates.append(root / _encoded_cwd(cwd) / f"{session_id}.jsonl")
    # The runtime may resolve the project dir from a realpath that differs from the
    # launch cwd (symlinked worktrees), so fall back to an exact session-id match.
    candidates.extend(sorted(root.glob(f"*/{session_id}.jsonl")))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _summary_source(log_path: Path, cache: dict[str, Any]) -> Path | None:
    """Return the file whose bytes feed this attempt's title/NOW refresh.

    Supervised owners follow the announced child transcript and wait for it: the
    receipt log holds no conversational text, so refreshing against it would burn
    the cursor and record an empty title instead of degrading honestly.  Only
    settled answers are cached, so an announcement that has not been written yet is
    re-checked on the next poll instead of being frozen into the wrong source.
    """
    if "source" in cache:
        return cache["source"]
    if "announced" not in cache:
        decided, announced = announced_session(log_path)
        if not decided:
            return None
        cache["announced"] = announced
    announced = cache["announced"]
    if announced is None:
        cache["source"] = log_path
        return log_path
    transcript = session_transcript(announced)
    if transcript is not None:
        cache["source"] = transcript
    return transcript


def _refresh(
    harness: str, sid: str, transcript: Path, *,
    phase: str, debounce: int, priority: bool, prompt_path: Path | None = None,
) -> bool:
    try:
        from fleet import refresh_title

        return bool(refresh_title.maybe_spawn(
            harness=harness,
            sid=sid,
            transcript=str(transcript),
            debounce=debounce,
            priority=priority,
            quota_class=phase if phase in {"initial", "final"} else None,
            prompt_path=str(prompt_path) if prompt_path else None,
        ))
    except Exception:
        return False


def supervise(
    *, attempt_id: str, harness: str, transcript: str | Path,
    target_pid: int, target_start: str, prompt_path: str | Path | None = None,
    poll: float = DEFAULT_POLL,
    initial_delay: float = DEFAULT_INITIAL_DELAY,
    periodic_debounce: int = DEFAULT_PERIODIC_DEBOUNCE,
    final_grace: float = DEFAULT_FINAL_GRACE,
    log_quiet: float = DEFAULT_LOG_QUIET,
) -> int:
    """Follow one governed process and maintain its attempt-scoped summary."""

    log_path = _validate_transcript(transcript, harness, attempt_id)
    prompt = _validate_prompt(prompt_path, log_path)
    sid = summary_sid(attempt_id)
    lock_path = owner_lock_path(harness, attempt_id)
    state_path = owner_state_path(harness, attempt_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        _, self_start, _ = process_observation(os.getpid())
        started_at = time.time()
        state: dict[str, Any] = {
            "schema_version": OWNER_SCHEMA,
            "status": "active",
            "attempt_id": attempt_id,
            "harness": harness,
            "sid": sid,
            "transcript": str(log_path),
            "prompt_path": str(prompt) if prompt else None,
            "target_pid": target_pid,
            "target_proc_start": target_start,
            "pid": os.getpid(),
            "proc_start": self_start,
            "observer_namespace": process_namespace_identity(),
            "started_at": started_at,
            "last_refresh_phase": None,
        }
        _atomic_write(state_path, state)

        first_eligible = time.monotonic() + max(0.0, initial_delay)
        initial_requested = False
        source_cache: dict[str, Any] = {}
        source = log_path
        while True:
            live, terminal_reason = _proc_live(target_pid, target_start)
            resolved = _summary_source(log_path, source_cache)
            if resolved is not None and resolved != source:
                source = resolved
                state.update(summary_source=str(source))
                _atomic_write(state_path, state)
            size = _log_signature(source)[0] if resolved is not None else 0
            if size and time.monotonic() >= first_eligible:
                previous = _read_sidecar(harness, sid)
                if not initial_requested and not previous.get("summary"):
                    initial_requested = _refresh(
                        harness, sid, source, phase="initial", debounce=0, priority=True, prompt_path=prompt)
                    if initial_requested:
                        state.update(last_refresh_phase="initial", last_refresh_at=time.time())
                        _atomic_write(state_path, state)
                else:
                    if _refresh(
                        harness, sid, source, phase="periodic",
                        debounce=periodic_debounce, priority=not previous.get("summary"), prompt_path=prompt,
                    ):
                        state.update(last_refresh_phase="periodic", last_refresh_at=time.time())
                        _atomic_write(state_path, state)
            if not live:
                break
            time.sleep(max(0.05, poll))

        # The child transcript, not the receipt log, is what settles for a supervised
        # owner; both resolve through the same source so the quiet window watches the
        # bytes the final refresh will actually read.
        source = _summary_source(log_path, source_cache) or source
        quiet_since = time.monotonic()
        last_signature = _log_signature(source)
        quiet_deadline = time.monotonic() + max(log_quiet * 4, 2.0)
        while time.monotonic() < quiet_deadline:
            time.sleep(min(max(0.05, poll), 0.25))
            signature = _log_signature(source)
            if signature != last_signature:
                last_signature = signature
                quiet_since = time.monotonic()
            if time.monotonic() - quiet_since >= max(0.0, log_quiet):
                break

        final_size = _log_signature(source)[0]
        final_deadline = time.monotonic() + max(0.0, final_grace)
        final_started = False
        final_complete = False
        while final_size and time.monotonic() < final_deadline:
            sidecar = _read_sidecar(harness, sid)
            offset = sidecar.get("offset") if isinstance(sidecar.get("offset"), int) else 0
            if offset >= final_size and sidecar.get("summary"):
                final_complete = True
                break
            if not final_started:
                final_started = _refresh(
                    harness, sid, source, phase="final", debounce=0, priority=True, prompt_path=prompt)
                if final_started:
                    state.update(last_refresh_phase="final", last_refresh_at=time.time())
                    _atomic_write(state_path, state)
            time.sleep(min(max(0.05, poll), 0.5))
        state.update(
            status="terminal" if final_complete or not final_size else "degraded",
            terminal_reason=terminal_reason,
            final_refresh_started=final_started,
            final_refresh_complete=final_complete,
            finished_at=time.time(),
        )
        _atomic_write(state_path, state)
        return 0
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()


def _read_exact_row(jobs: Path, attempt_id: str) -> tuple[list[str], dict[str, str]] | None:
    matches: list[tuple[list[str], dict[str, str]]] = []
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("attempt_id") == attempt_id:
            matches.append((fields, metadata))
    return matches[0] if len(matches) == 1 else None


def ensure_attempt_owner(jobs: str | Path, attempt_id: str) -> dict[str, Any]:
    """Reattach one missing owner for a live exact attempt, idempotently."""

    if not ATTEMPT_RE.fullmatch(attempt_id or ""):
        return {"state": "skipped", "reason": "invalid-attempt-id"}
    jobs_path = Path(jobs).expanduser().resolve()
    row = _read_exact_row(jobs_path, attempt_id)
    if row is None:
        return {"state": "skipped", "reason": "attempt-row-not-unique"}
    fields, metadata = row
    harness = metadata.get("harness") or metadata.get("child_harness") or ""
    try:
        lock_path = ensure_lock_path(harness, attempt_id)
    except ValueError as exc:
        return {"state": "skipped", "reason": str(exc)}
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as ensure_lock:
        try:
            fcntl.flock(ensure_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "existing", "reason": "ensure-in-progress"}
        row = _read_exact_row(jobs_path, attempt_id)
        if row is None:
            return {"state": "skipped", "reason": "attempt-row-not-unique"}
        fields, metadata = row
        if fields[1] not in {"open", "running"}:
            return {"state": "skipped", "reason": "attempt-terminal"}
        if _owner_live(metadata):
            return {"state": "existing", "reason": "owner-live"}
        raw_pid, target_start = metadata.get("pid", ""), metadata.get("pid_start", "")
        transcript = metadata.get("log_file", "")
        if not raw_pid.isdigit() or not target_start or not transcript:
            return {"state": "skipped", "reason": "attempt-identity-incomplete"}
        live, reason = _proc_live(int(raw_pid), target_start)
        if not live:
            return {"state": "skipped", "reason": f"worker-{reason}"}
        retained_prompt = None
        try:
            state = json.loads(owner_state_path(harness, attempt_id).read_text(encoding="utf-8"))
            retained_prompt = _validate_prompt(state.get("prompt_path"), _validate_transcript(transcript, harness, attempt_id))
        except (OSError, ValueError, json.JSONDecodeError, AttributeError):
            retained_prompt = None
        try:
            values = launch_summary_owner(
                attempt_id=attempt_id,
                harness=harness,
                transcript=transcript,
                target_pid=int(raw_pid),
                target_start=target_start,
                prompt_path=retained_prompt,
            )
        except (OSError, ValueError) as exc:
            return {"state": "failed", "reason": str(exc)}

        expected_pid = raw_pid
        expected_start = target_start

        def still_missing(fresh_fields: list[str]) -> bool:
            fresh = parse_registry_metadata(fresh_fields[5])
            return (
                fresh_fields[1] in {"open", "running"}
                and fresh.get("pid") == expected_pid
                and fresh.get("pid_start") == expected_start
                and not _owner_live(fresh)
            )

        try:
            updated = annotate_attempt_row_if(jobs_path, attempt_id, values, still_missing)
        except DispatchContractError as exc:
            updated = False
            reason = exc.reason
        else:
            reason = "reattached" if updated else "revalidation-veto"
        if not updated:
            return {"state": "skipped", "reason": reason}
        return {"state": "started", "reason": reason, **values}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    supervise_parser = commands.add_parser("supervise")
    supervise_parser.add_argument("--attempt-id", required=True)
    supervise_parser.add_argument("--harness", required=True, choices=sorted(HARNESSES))
    supervise_parser.add_argument("--transcript", required=True)
    supervise_parser.add_argument("--pid", required=True, type=int)
    supervise_parser.add_argument("--proc-start", required=True)
    supervise_parser.add_argument("--prompt")
    supervise_parser.add_argument("--poll", type=float, default=DEFAULT_POLL)
    supervise_parser.add_argument("--initial-delay", type=float, default=DEFAULT_INITIAL_DELAY)
    supervise_parser.add_argument("--periodic-debounce", type=int, default=DEFAULT_PERIODIC_DEBOUNCE)
    supervise_parser.add_argument("--final-grace", type=float, default=DEFAULT_FINAL_GRACE)
    supervise_parser.add_argument("--log-quiet", type=float, default=DEFAULT_LOG_QUIET)
    ensure_parser = commands.add_parser("ensure")
    ensure_parser.add_argument("--jobs", required=True)
    ensure_parser.add_argument("--attempt-id", required=True)
    args = parser.parse_args(argv)
    if args.command == "ensure":
        result = ensure_attempt_owner(args.jobs, args.attempt_id)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["state"] in {"existing", "started", "skipped"} else 1
    return supervise(
        attempt_id=args.attempt_id,
        harness=args.harness,
        transcript=args.transcript,
        target_pid=args.pid,
        target_start=args.proc_start,
        prompt_path=args.prompt,
        poll=args.poll,
        initial_delay=args.initial_delay,
        periodic_debounce=args.periodic_debounce,
        final_grace=args.final_grace,
        log_quiet=args.log_quiet,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"dispatch-summary: {exc}", file=sys.stderr)
        raise SystemExit(70)
