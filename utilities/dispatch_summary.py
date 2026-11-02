#!/usr/bin/env python3
"""Own registered-attempt title/NOW refresh independently of Fleet.

The adapter wrappers start one supervisor while the governed worker is still
behind its launch fence.  The supervisor follows only the exact attempt log and
exact governed PID/start identity, schedules initial/periodic/final refreshes,
and records observational state.  It has no completion or signal authority.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
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

from claude_project_dir import resolve_session_transcript  # noqa: E402
from dispatch_contract import (  # noqa: E402
    DispatchContractError,
    PID_HOST_NAMESPACE_PROOF,
    REVIEW_GOVERNED_LEASE_KIND,
    REVIEW_GOVERNED_LEASE_NONCE_RE,
    annotate_attempt_row_if,
    parse_registry_metadata,
    process_launch_identity,
    process_namespace_identity,
    process_observation,
    recorded_pid_namespace_state,
    review_governed_lease_path,
    review_governed_lease_payload,
)


ATTEMPT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
HARNESSES = {"claude", "codex", "opencode"}
OWNER_SCHEMA = 1
OWNER_KIND = "dispatch-v1"
DEFAULT_POLL = 2.0
DEFAULT_INITIAL_DELAY = 3.0
DEFAULT_PERIODIC_DEBOUNCE = 600
PERIODIC_ATTEMPT_INTERVAL = 15.0
DEFAULT_FINAL_GRACE = 75.0
DEFAULT_LOG_QUIET = 1.0
SESSION_ANNOUNCE_SCAN = 1 << 16
_CLAUDE_MODEL_ID = re.compile(r"^claude-(?:opus|sonnet|haiku|fable)-\d+(?:-\d+)*(?:-\d{8})?$")


def _reported_claude_model(event: dict[str, Any]) -> str | None:
    """Accept a concrete runtime id, never the requested family alias."""
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        candidate = event.get("model")
    elif kind == "assistant" and isinstance(event.get("message"), dict):
        candidate = event["message"].get("model")
    elif kind == "assistant":
        candidate = event.get("model")
    else:
        return None
    return candidate if isinstance(candidate, str) and _CLAUDE_MODEL_ID.fullmatch(candidate) else None


def _record_resolved_model(source: Path, jobs: Path, attempt_id: str,
                           cursor: dict[str, Any]) -> bool:
    """Scan new complete stream rows and stamp the first reported model once."""
    if cursor.get("source") != source:
        cursor.clear()
        cursor.update(source=source, offset=0)
    try:
        with source.open("rb") as stream:
            stream.seek(cursor["offset"])
            while True:
                start = stream.tell()
                raw = stream.readline()
                if not raw or not raw.endswith(b"\n"):
                    cursor["offset"] = start
                    return False
                cursor["offset"] = stream.tell()
                try:
                    event = json.loads(raw)
                except (UnicodeDecodeError, ValueError):
                    continue
                model = _reported_claude_model(event) if isinstance(event, dict) else None
                if model:
                    # Terminal rows may already have sealed delivery revisions.
                    # Leave a late model unresolved rather than changing that receipt.
                    recorded = annotate_attempt_row_if(
                        jobs, attempt_id, {"resolved_model": model},
                        lambda fields: not parse_registry_metadata(fields[5]).get("resolved_model"),
                    )
                    if not recorded:
                        cursor["offset"] = start
                    return recorded
    except OSError:
        return False


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


def summary_writable_dirs() -> tuple[Path, ...]:
    """Resolve only the directories used by the title producer, without creating them."""
    title_root = _state_root()
    xdg_state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    if os.environ.get("FLEET_TITLE_STATE_DIR"):
        workdir = title_root / "fleet-title-workdir"
    else:
        workdir = xdg_state / "hearting" / "fleet-title-workdir"
    provider_order = xdg_state / "agent-fleet" / "provider-order"

    resolved = []
    seen = set()
    for path in (title_root, workdir, provider_order):
        path = path.expanduser().resolve()
        if path not in seen:
            seen.add(path)
            resolved.append(path)
    return tuple(resolved)


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


def owner_process_state(metadata: dict[str, str]) -> str:
    """Three-way view of the same owner evidence `_owner_live` reads.

    ``live``    the declared owner is provably running;
    ``dead``    the declared owner is provably gone (absent, a reused pid or a zombie);
    ``absent``  the row declares no summary owner;
    ``unknown`` declared, but unreadable, malformed, or not observable from here.

    `_owner_live` folds the last two into False, which is wrong for a caller that
    must not act on a guess.
    """

    declared = ("summary_owner_pid", "summary_owner_pid_start",
                "summary_state_file", "summary_owner_pid_observer_ns")
    if not any(metadata.get(key) for key in declared):
        return "absent"
    if _owner_live(metadata):
        return "live"
    raw_pid = metadata.get("summary_owner_pid", "")
    expected = metadata.get("summary_owner_pid_start", "")
    observer_namespace = metadata.get("summary_owner_pid_observer_ns", "")
    if not raw_pid.isdigit() or not expected or not observer_namespace:
        return "unknown"
    current_namespace = process_namespace_identity()
    if current_namespace is None:
        return "unknown"
    if current_namespace == observer_namespace:
        pid, start = int(raw_pid), expected
    elif (
        metadata.get("summary_owner_pid_host", "").isdigit()
        and metadata.get("summary_owner_pid_host_start")
        and current_namespace == metadata.get("summary_owner_pid_host_ns")
        and metadata.get("summary_owner_pid_host_proof") == PID_HOST_NAMESPACE_PROOF
    ):
        pid, start = int(metadata["summary_owner_pid_host"]), metadata["summary_owner_pid_host_start"]
    elif recorded_pid_namespace_state(observer_namespace) == "extinct":
        # A process never leaves its PID namespace: once that namespace has left the
        # host, the owner cannot be running anywhere.
        return "dead"
    else:
        return "unknown"
    visibility, actual_start, state = process_observation(pid)
    if visibility == "missing":
        return "dead"
    if visibility != "present":
        return "unknown"
    if actual_start != start or state == "Z":
        return "dead"
    # The exact process is alive although its state file no longer vouches for it.
    return "unknown"


def _read_owner_state(path: Path) -> tuple[dict[str, Any] | None, str]:
    """Read one owner record without following links or trusting unbounded input."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None, "state-file-missing"
    except OSError:
        return None, "state-file-unreadable"
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_size > 16384):
            return None, "state-file-untrusted"
        try:
            chunks = []
            remaining = 16385
            while remaining:
                chunk = os.read(fd, min(4096, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > 16384:
                return None, "state-file-oversized"
            state = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return None, "state-file-unreadable"
        return (state, "") if isinstance(state, dict) else (None, "state-file-invalid")
    finally:
        os.close(fd)


def _owner_state_binding(metadata: dict[str, str]):
    """Read the exact recorded owner state without substituting this process' root."""
    attempt_id = metadata.get("attempt_id", "")
    harness = metadata.get("harness") or metadata.get("child_harness") or ""
    raw = metadata.get("summary_state_file", "")
    pid = metadata.get("summary_owner_pid", "")
    start = metadata.get("summary_owner_pid_start", "")
    namespace = metadata.get("summary_owner_pid_observer_ns", "")
    sid = summary_sid(attempt_id) if ATTEMPT_RE.fullmatch(attempt_id or "") else None
    if (metadata.get("summary_owner") != OWNER_KIND or harness not in HARNESSES or not sid
            or metadata.get("summary_sid") != sid or not pid.isdigit() or not start or not namespace
            or not raw or not Path(raw).is_absolute()):
        return None, "identity-incomplete"
    try:
        path = Path(raw)
        resolved = path.resolve(strict=True)
        if str(resolved) != str(path) or path.parent.name != harness:
            return None, "state-path-conflict"
        owners_dir = path.parent.parent
        state_root = owners_dir.parent
        expected = owners_dir / harness / f"{attempt_id}.json"
        if (owners_dir.name != ".dispatch-owners" or expected != path
                or not state_root.is_dir() or state_root.is_symlink()):
            return None, "state-path-conflict"
        parent_stat = path.parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != os.geteuid():
            return None, "state-directory-untrusted"
    except FileNotFoundError:
        return None, "state-file-missing"
    except OSError:
        return None, "state-file-unreadable"
    state, read_reason = _read_owner_state(path)
    if state is None:
        return None, read_reason
    if not isinstance(state, dict) or any((
        state.get("schema_version") != OWNER_SCHEMA,
        state.get("attempt_id") != attempt_id,
        state.get("harness") != harness,
        state.get("sid") != sid,
        state.get("pid") != int(pid),
        state.get("proc_start") != start,
        state.get("observer_namespace") != namespace,
    )):
        return None, "state-identity-conflict"
    expected_home = metadata.get("runtime_home", "")
    # An owner state written before `runtime_home` existed has no such key: it
    # claims no home, so it cannot conflict. A recorded value must still match.
    if expected_home and "runtime_home" in state and state.get("runtime_home") != expected_home:
        return None, "state-home-conflict"
    expected_transcript = metadata.get("log_file", "")
    if expected_transcript:
        try:
            if Path(state.get("transcript", "")).resolve(strict=False) != Path(expected_transcript).resolve(strict=False):
                return None, "state-transcript-conflict"
        except (OSError, ValueError):
            return None, "state-transcript-conflict"
    for key, state_key in (("pid", "target_pid"), ("pid_start", "target_proc_start")):
        if metadata.get(key) and str(state.get(state_key, "")) != metadata[key]:
            return None, "state-target-conflict"
    return (path, state, harness, attempt_id, sid, state_root), ""


def _probe_owner_lock(path: Path) -> str:
    """held | free | missing | unknown for the existing exact owner lock."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unknown"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            return "unknown"
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return "held"
        except OSError:
            return "unknown"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "free"
    finally:
        os.close(fd)


MAX_PROVIDER_SCAN_PIDS = 65536


def _walk_provider_argv(proc_root: Path) -> tuple[tuple[str, ...], str]:
    """(`--lockdir` argument values of this user's live processes, incomplete reason).

    A process owned by another user is skipped only when its `status` was read:
    the provider is started by this user's summary owner. The user is taken from
    the `Uid:` line, not the `/proc/<pid>` directory owner, which is root for a
    non-dumpable process. Any read that fails for another reason, or an empty
    argv on a live process, leaves the reason non-empty so the caller keeps.
    """
    me = os.geteuid()
    try:
        names = [name for name in os.listdir(proc_root) if name.isdigit()]
    except OSError as exc:
        return (), f"proc-unreadable-{exc.errno or 'error'}"
    if len(names) > MAX_PROVIDER_SCAN_PIDS:
        return (), "proc-limit"
    tokens: list[str] = []
    reason = ""
    for name in names:
        base = proc_root / name
        try:
            status = (base / "status").read_bytes().decode("utf-8", "replace")
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ESRCH):
                reason = reason or "status-unreadable"
            continue
        uids: set[int] = set()
        zombie = False
        for line in status.splitlines():
            if line.startswith("Uid:"):
                uids = {int(item) for item in line[4:].split() if item.isdigit()}
            elif line.startswith("State:"):
                zombie = line[6:].strip().startswith("Z")
        if me not in uids or zombie:
            continue
        try:
            raw = (base / "cmdline").read_bytes()
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ESRCH):
                reason = reason or "cmdline-unreadable"
            continue
        if not raw:
            reason = reason or "cmdline-empty"
            continue
        argv = raw.decode("utf-8", "replace").split("\0")
        for index, item in enumerate(argv):
            if item == "--lockdir" and index + 1 < len(argv):
                tokens.append(argv[index + 1])
            elif item.startswith("--lockdir="):
                tokens.append(item[len("--lockdir="):])
    return tuple(tokens), reason


class _ProviderScan:
    """One lazily taken walk, shared by every lockdir judged in one retention pass."""

    def __init__(self) -> None:
        self._walk: tuple[tuple[str, ...], str] | None = None

    def get(self) -> tuple[tuple[str, ...], str]:
        if self._walk is None:
            self._walk = _walk_provider_argv(Path("/proc"))
        return self._walk


_PROVIDER_SCAN: contextvars.ContextVar = contextvars.ContextVar(
    "dispatch_summary_provider_scan", default=None)


@contextlib.contextmanager
def provider_reference_scan_scope():
    """Read the process table at most once for every lockdir judged inside this scope.

    Separate from `dispatch_contract.process_table_scan_scope`, whose single walk
    reads `stat`/`environ` for every observer in the system: a lockdir judgement
    needs `status` and `cmdline` of this user's processes only, and only when a
    provider lockdir exists, so the walk is taken on first use. A nested scope
    joins the enclosing pass. Leave the scope before a final re-check.
    """
    if _PROVIDER_SCAN.get() is not None:
        yield
        return
    token = _PROVIDER_SCAN.set(_ProviderScan())
    try:
        yield
    finally:
        _PROVIDER_SCAN.reset(token)


def _provider_lockdir_reference(lockdir: Path, proc_root: Path = Path("/proc")) -> tuple[str, str]:
    """referenced | unreferenced | unknown, with the reason, for one provider lockdir."""
    scan = _PROVIDER_SCAN.get()
    if scan is not None and Path(proc_root) == Path("/proc"):
        tokens, reason = scan.get()
    else:
        tokens, reason = _walk_provider_argv(Path(proc_root))
    wanted = os.path.normpath(str(lockdir))
    try:
        info = os.stat(lockdir)
        ident = (info.st_dev, info.st_ino)
    except OSError:
        ident = None
    for token in tokens:
        if os.path.normpath(token) == wanted:
            return "referenced", "argv-path"
        try:
            found = os.stat(token)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (OSError, ValueError):
            reason = reason or "token-unreadable"
            continue
        if ident is not None and (found.st_dev, found.st_ino) == ident:
            return "referenced", "argv-inode"
    return ("unknown", reason) if reason else ("unreferenced", "")


def home_consumer_evidence(metadata: dict[str, str]) -> tuple[str, str]:
    """Whether this exact attempt's summary owner/provider can still read its HOME.

    This is a resource-use decision, separate from the process-death classifier.
    The owner lock covers native transcript resolution and remains held while the
    owner waits for its asynchronous provider. The provider's existing per-sid
    lockdir extends that use after the owner unlocks, for as long as a process
    of this user names that lockdir in its `--lockdir` argument.
    """
    declared = any(metadata.get(key) for key in (
        "summary_owner", "summary_sid", "summary_owner_pid", "summary_state_file"))
    if not declared:
        return "absent", "no-summary-owner"
    binding, detail = _owner_state_binding(metadata)
    if binding is None:
        return "unknown", detail
    state_path, _state, harness, attempt_id, sid, state_root = binding
    owner_lock = _probe_owner_lock(state_path.with_suffix(".lock"))
    if owner_lock == "held":
        return "live", "owner-lock-held"
    if owner_lock != "free":
        return "unknown", "owner-lock-" + owner_lock
    provider_parent = state_root / harness
    provider_lock = provider_parent / (".lock-" + sid)
    try:
        parent_stat = provider_parent.stat(follow_symlinks=False)
    except FileNotFoundError:
        return "absent", "provider-state-absent"
    except OSError:
        return "unknown", "provider-state-unreadable"
    if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != os.geteuid():
        return "unknown", "provider-state-untrusted"
    try:
        provider_stat = provider_lock.stat(follow_symlinks=False)
    except FileNotFoundError:
        return "absent", "owner-and-provider-locks-free"
    except OSError:
        return "unknown", "provider-lock-unreadable"
    if stat.S_ISDIR(provider_stat.st_mode) and provider_stat.st_uid == os.geteuid():
        # The lockdir is created before spawn and the provider's argv names it
        # (`--lockdir`) for as long as the provider can read. Judge by that
        # argv; a leftover directory no process names holds nothing, whatever
        # its age. A process table that cannot be read completely keeps.
        host_proof = (
            metadata.get("summary_owner_pid_host", "").isdigit()
            and metadata.get("summary_owner_pid_host_proof") == PID_HOST_NAMESPACE_PROOF
            and process_namespace_identity() == metadata.get("summary_owner_pid_host_ns"))
        if (process_namespace_identity() != metadata.get("summary_owner_pid_observer_ns", "")
                and not host_proof):
            return "unknown", "provider-scan-namespace-unverifiable"
        found, reason = _provider_lockdir_reference(provider_lock)
        if found == "referenced":
            return "live", "provider-lockdir-present"
        if found == "unreferenced":
            return "absent", "provider-lockdir-unreferenced"
        return "unknown", "provider-lockdir-" + reason
    return "unknown", "provider-lock-untrusted"


def home_consumer_for_identity(attempt_id: str, harness: str,
                               runtime_home: str | Path) -> tuple[str, str]:
    """Resolve summary use for a no-row HOME from the existing exact attempt state."""
    try:
        state_path = owner_state_path(harness, attempt_id)
        provider_parent = _state_root() / harness
        provider_lock = provider_parent / (".lock-" + summary_sid(attempt_id))
    except (ValueError, OSError):
        return "unknown", "owner-state-path-invalid"
    state, state_reason = _read_owner_state(state_path)
    if state is None and state_reason == "state-file-missing":
        try:
            state_path.with_suffix(".lock").lstat()
            return "unknown", "owner-state-missing-lock-remains"
        except FileNotFoundError:
            pass
        except OSError:
            return "unknown", "owner-lock-unreadable"
        try:
            provider_lock.lstat()
            return "unknown", "provider-state-without-owner"
        except FileNotFoundError:
            return "absent", "owner-state-absent"
        except OSError:
            return "unknown", "provider-lock-unreadable"
    if state is None:
        return "unknown", state_reason
    if not isinstance(state.get("runtime_home"), str):
        return "unknown", "owner-state-home-missing"
    try:
        if Path(state["runtime_home"]).resolve(strict=False) != Path(runtime_home).resolve(strict=False):
            return "absent", "owner-state-other-home"
    except (OSError, ValueError):
        return "unknown", "owner-state-home-invalid"
    metadata = {
        "summary_owner": OWNER_KIND,
        "summary_sid": summary_sid(attempt_id),
        "summary_owner_pid": str(state.get("pid", "")),
        "summary_owner_pid_start": str(state.get("proc_start", "")),
        "summary_owner_pid_observer_ns": str(state.get("observer_namespace", "")),
        "summary_state_file": str(state_path),
        "attempt_id": attempt_id,
        "harness": harness,
        "runtime_home": str(state["runtime_home"]),
        "log_file": str(state.get("transcript", "")),
    }
    return home_consumer_evidence(metadata)


def _owner_env() -> dict[str, str]:
    env = dict(os.environ)
    env["AGENT_SESSION_ROLE"] = "worker"
    env["AGENT_SUMMARY_OWNER"] = "dispatch"
    return env


def launch_summary_owner(
    *, attempt_id: str, harness: str, transcript: str | Path,
    target_pid: int, target_start: str, prompt_path: str | Path | None = None,
    review_artifact_root: str | Path | None = None,
    review_cycle_id: str | None = None,
    review_lease_nonce: str | None = None,
    runtime_home: str | Path | None = None,
    attempt_cwd: str | Path | None = None,
) -> dict[str, str]:
    """Launch and prove one exact-attempt supervisor; return registry metadata."""

    log_path = _validate_transcript(transcript, harness, attempt_id)
    prompt = _validate_prompt(prompt_path, log_path)
    if target_pid <= 0 or not target_start:
        raise ValueError("exact target identity required")
    review_values = (review_artifact_root, review_cycle_id, review_lease_nonce)
    if any(value is not None for value in review_values):
        if not all(value is not None for value in review_values):
            raise ValueError("review governed lease tuple is incomplete")
        if REVIEW_GOVERNED_LEASE_NONCE_RE.fullmatch(str(review_lease_nonce)) is None:
            raise ValueError("review governed lease nonce is invalid")
        review_governed_lease_path(
            str(review_artifact_root), str(review_cycle_id), attempt_id
        )
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
    if runtime_home is not None:
        argv += ["--runtime-home", str(runtime_home)]
    if attempt_cwd is not None:
        argv += ["--attempt-cwd", str(attempt_cwd)]
    if review_artifact_root is not None:
        argv += [
            "--review-artifact-root", str(review_artifact_root),
            "--review-cycle-id", str(review_cycle_id),
            "--review-lease-nonce", str(review_lease_nonce),
        ]
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
    if review_artifact_root is not None:
        metadata.update({
            "review_governed_lease": REVIEW_GOVERNED_LEASE_KIND,
            "review_governed_lease_nonce": str(review_lease_nonce),
        })
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


def _claude_projects_root(home: str | Path | None = None) -> Path:
    home = home or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return Path(home) / "projects"


def _encoded_cwd(cwd: str) -> str:
    return encode_project_dir(cwd)


def announced_session(
    path: Path, scan: int = SESSION_ANNOUNCE_SCAN,
) -> tuple[bool, dict[str, str] | None]:
    """Return ``(decided, announcement)`` for a supervised attempt's source.

    A supervised owner's attempt log is a receipt log: control rows plus one final
    ``result``, with model text deliberately withheld.  ``dispatch.supervisor.session``
    names the runtime session whose own transcript is the real summary input.

    The summary owner is launched while the governed worker is still behind its
    launch fence, so an empty log means "not yet", not "not supervised" — caching
    that as unsupervised would pin the follower to the receipt log for the whole
    run. Supervisor control rows may precede either a session announcement or native
    Codex conversation rows. Only actual user/assistant text makes an unannounced
    supervised log its own source; tool, usage, and lifecycle rows do not. A first
    native stream row without supervisor controls remains a one-shot transcript.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(max(1, int(scan)))
    except OSError:
        return False, None
    found: dict[str, str] | None = None
    saw_supervisor = False
    saw_native_stream = False
    saw_conversation = False
    supervisor_terminal_seen = False
    claude_terminal_seen = False
    # Only complete JSONL rows are evidence. A partial live tail cannot erase
    # the announcement or conversation already present in complete rows.
    complete = head.rsplit(b"\n", 1)[0] if head and not head.endswith(b"\n") else head
    rows = complete.split(b"\n")
    try:
        from fleet.refresh_title import _delta_text
    except Exception:
        _delta_text = None
    for raw in rows:
        if not raw.strip():
            continue
        try:
            row = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            # Malformed complete data is not converted into a successful source.
            return (True, {"invalid": "malformed-supervisor-log"}) if saw_supervisor else (False, None)
        if isinstance(row, dict) and str(row.get("type") or "").startswith("dispatch.supervisor."):
            saw_supervisor = True
            supervisor_terminal_seen = supervisor_terminal_seen or (
                row.get("type") == "dispatch.supervisor.terminal")
            if row.get("type") != "dispatch.supervisor.session":
                continue
            if found is not None:
                return True, {"invalid": "multiple-supervisor-session-events"}
            session_id = row.get("session_id")
            cwd = row.get("cwd")
            if not (isinstance(session_id, str) and ATTEMPT_RE.fullmatch(session_id)
                    and isinstance(cwd, str) and os.path.isabs(cwd)):
                return True, {"invalid": "invalid-supervisor-session-event"}
            found = {"session_id": session_id, "cwd": cwd}
            if row.get("runtime"):
                found["runtime"] = row["runtime"]
            if row.get("runtime") == "opencode":
                found["parent_attempt_id"] = row.get("parent_attempt_id") or ""
            continue
        if not isinstance(row, dict):
            return (True, {"invalid": "invalid-supervisor-session-event"}) if saw_supervisor else (True, None)
        saw_native_stream = True
        claude_terminal_seen = claude_terminal_seen or row.get("type") == "result"
        if saw_supervisor and _delta_text is not None:
            serialized = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            saw_conversation = saw_conversation or bool(_delta_text(serialized, harness="codex"))
    if found is not None:
        return True, found
    if saw_conversation:
        return True, None
    if saw_supervisor:
        if supervisor_terminal_seen or claude_terminal_seen:
            return True, {"invalid": "missing-supervisor-session-event"}
        return False, None
    return (True, None) if saw_native_stream else (False, None)


def session_transcript(announced: dict[str, str], *, expected_cwd: str | None = None,
                       runtime_home: str | Path | None = None) -> Path | None:
    """Resolve one exact runtime transcript for an announced child session.

    Exact ``<session id>.jsonl`` only.  A missing transcript returns None rather
    than borrowing a same-cwd neighbour, which would stamp another session's text
    onto this attempt's NOW line.
    """
    effective_home = str(runtime_home) if runtime_home is not None else str(
        Path(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")))
    expected = str(expected_cwd if expected_cwd is not None else announced.get("cwd") or "")
    path, _reason = resolve_session_transcript(
        announced.get("session_id") or "", announced.get("cwd") or "",
        expected_cwd=expected, runtime_home=effective_home)
    return path


def _summary_source(log_path: Path, cache: dict[str, Any], *,
                    runtime_home: str | Path | None = None,
                    attempt_cwd: str | Path | None = None) -> Path | None:
    """Return the file whose bytes feed this attempt's title/NOW refresh.

    Supervised owners follow an announced child transcript when present. Codex native
    conversation rows can make the exact attempt log the source when no announcement
    exists; control, tool, and usage rows cannot. Only settled answers are cached, so
    an announcement that has not been written yet is re-checked on the next poll.
    """
    if "source" in cache:
        return cache["source"]
    if "announced" not in cache:
        decided, announced = announced_session(log_path)
        if not decided:
            return None
        cache["announced"] = announced
    announced = cache["announced"]
    if isinstance(announced, dict) and announced.get("invalid"):
        cache["source_reason"] = announced["invalid"]
        return None
    if announced is None:
        cache["source"] = log_path
        return log_path
    if announced.get("runtime") == "opencode":
        jobs = os.environ.get("AGENT_DISPATCH_JOBS")
        match = re.search(r"\.(att-[A-Za-z0-9_-]+)\.opencode\.jsonl$", log_path.name)
        if not jobs or not match:
            return None
        if announced.get("parent_attempt_id") != match[1]:
            return None
        row = _read_exact_row(Path(jobs), match[1])
        metadata = row[1] if row else {}
        runtime = metadata.get("opencode_runtime_dir")
        if (metadata.get("harness") != "opencode" or metadata.get("log_file") != str(log_path)
                or not runtime or not os.path.isabs(runtime)
                or not re.fullmatch(r"ses_[A-Za-z0-9]+", announced["session_id"])):
            return None
        db = Path(runtime) / "data" / "opencode" / "opencode.db"
        if db.is_file():
            cache.update(source=db, refresh_source={"kind": "opencode-db", "db_path": str(db),
                                                   "session_id": announced["session_id"]})
            return db
        return None
    transcript = session_transcript(announced, expected_cwd=str(attempt_cwd) if attempt_cwd else None,
                                    runtime_home=runtime_home)
    if transcript is not None:
        cache.update(source=transcript, refresh_source={
            "kind": "claude-transcript",
            "cursor_kind": "claude-transcript-v1:" + str(transcript.resolve()),
        })
    return transcript


def _refresh(
    harness: str, sid: str, transcript: Path, *,
    phase: str, debounce: int, priority: bool, prompt_path: Path | None = None,
    refresh_source: dict | None = None, runtime_home: str | Path | None = None,
) -> bool:
    try:
        from fleet import refresh_title

        source_kw = {"refresh_source": refresh_source} if refresh_source else {}
        home_kw = {"runtime_home": str(runtime_home)} if runtime_home is not None else {}
        return bool(refresh_title.maybe_spawn(
            harness=harness,
            sid=sid,
            transcript=str(transcript),
            debounce=debounce,
            priority=priority,
            quota_class=phase if phase in {"initial", "final"} else None,
            prompt_path=str(prompt_path) if prompt_path else None,
            **source_kw,
            **home_kw,
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
    review_artifact_root: str | Path | None = None,
    review_cycle_id: str | None = None,
    review_lease_nonce: str | None = None,
    runtime_home: str | Path | None = None,
    attempt_cwd: str | Path | None = None,
) -> int:
    """Follow one governed process and maintain its attempt-scoped summary."""

    log_path = _validate_transcript(transcript, harness, attempt_id)
    prompt = _validate_prompt(prompt_path, log_path)
    sid = summary_sid(attempt_id)
    lock_path = owner_lock_path(harness, attempt_id)
    state_path = owner_state_path(harness, attempt_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+", encoding="utf-8")
    review_fd: int | None = None
    home_read_scope = None
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        jobs_env = os.environ.get("AGENT_DISPATCH_JOBS")
        if runtime_home is not None and jobs_env:
            try:
                from dispatch_contract import dispatch_state_root
                jobs_path = Path(jobs_env).expanduser().resolve()
                state_root = dispatch_state_root(jobs_path)
                workers = Path(os.path.realpath(state_root / "homes" / "workers"))
                home = Path(runtime_home).resolve(strict=False)
                if home.parent == workers:
                    from worker_home_retention import worker_home_read_admission
                    home_read_scope = worker_home_read_admission(
                        jobs_path, home, None, attempt_id, harness)
                    if not home_read_scope.__enter__():
                        home_read_scope.__exit__(None, None, None)
                        home_read_scope = None
                        return 0
            except (OSError, RuntimeError, ValueError):
                if home_read_scope is not None:
                    home_read_scope.__exit__(None, None, None)
                    home_read_scope = None
                return 0
        review_values = (review_artifact_root, review_cycle_id, review_lease_nonce)
        if any(value is not None for value in review_values):
            if not all(value is not None for value in review_values):
                raise ValueError("review governed lease tuple is incomplete")
            nonce = str(review_lease_nonce)
            if REVIEW_GOVERNED_LEASE_NONCE_RE.fullmatch(nonce) is None:
                raise ValueError("review governed lease nonce is invalid")
            review_path = review_governed_lease_path(
                str(review_artifact_root), str(review_cycle_id), attempt_id
            )
            review_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            flags = (
                os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            review_fd = os.open(review_path, flags, 0o600)
            if not os.path.isfile(review_path) or not stat.S_ISREG(os.fstat(review_fd).st_mode):
                raise ValueError("review governed lease is not regular")
            try:
                fcntl.flock(review_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("review governed lease is already held") from exc
            payload = review_governed_lease_payload(
                attempt_id, str(review_cycle_id), nonce
            )
            os.ftruncate(review_fd, 0)
            os.pwrite(review_fd, payload, 0)
            os.fsync(review_fd)
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
        if runtime_home is not None:
            state["runtime_home"] = str(runtime_home)
        if attempt_cwd is not None:
            state["attempt_cwd"] = str(attempt_cwd)
        if review_fd is not None:
            state.update(
                review_governed_lease=REVIEW_GOVERNED_LEASE_KIND,
                review_cycle_id=str(review_cycle_id),
            )
        _atomic_write(state_path, state)

        first_eligible = time.monotonic() + max(0.0, initial_delay)
        initial_requested = False
        next_periodic_at = 0.0
        source_cache: dict[str, Any] = {}
        model_cursor: dict[str, Any] = {}
        jobs_env = os.environ.get("AGENT_DISPATCH_JOBS")
        jobs_path = Path(jobs_env).expanduser().resolve() if jobs_env else None
        model_recorded = False
        source = log_path
        while True:
            live, terminal_reason = _proc_live(target_pid, target_start)
            resolved = _summary_source(log_path, source_cache, runtime_home=runtime_home,
                                       attempt_cwd=attempt_cwd)
            if resolved is not None and resolved != source:
                source = resolved
                state.update(summary_source=str(source))
                _atomic_write(state_path, state)
            if harness == "claude" and jobs_path and resolved is not None and not model_recorded:
                model_recorded = _record_resolved_model(source, jobs_path, attempt_id, model_cursor)
            size = _log_signature(source)[0] if resolved is not None else 0
            if size and time.monotonic() >= first_eligible:
                previous = _read_sidecar(harness, sid)
                if not initial_requested and not previous.get("summary"):
                    initial_requested = _refresh(
                        harness, sid, source, phase="initial", debounce=0, priority=True, prompt_path=prompt,
                        refresh_source=source_cache.get("refresh_source"), runtime_home=runtime_home)
                    if initial_requested:
                        state.update(last_refresh_phase="initial", last_refresh_at=time.time())
                        _atomic_write(state_path, state)
                else:
                    # The periodic refresh only changes anything after the 600s debounce,
                    # so trying it on every 2s poll is wasted probing; space the attempts.
                    now_m = time.monotonic()
                    if now_m >= next_periodic_at:
                        next_periodic_at = now_m + PERIODIC_ATTEMPT_INTERVAL
                        if _refresh(
                            harness, sid, source, phase="periodic",
                            debounce=periodic_debounce, priority=False, prompt_path=prompt,
                            refresh_source=source_cache.get("refresh_source"), runtime_home=runtime_home,
                        ):
                            state.update(last_refresh_phase="periodic", last_refresh_at=time.time())
                            _atomic_write(state_path, state)
            if not live:
                break
            time.sleep(max(0.05, poll))

        # Give late supervisor announcements/transcripts the existing bounded final
        # settling window. Never substitute the control receipt for supervised input.
        source = _summary_source(log_path, source_cache, runtime_home=runtime_home,
                                 attempt_cwd=attempt_cwd)
        # Codex native streams can be empty at process exit while their receipt
        # is still the canonical source. The no-fallback rule is specific to a
        # supervised Claude receipt, which carries no conversation bytes.
        if source is None and harness != "claude":
            source = log_path
            source_cache["source"] = source
        source_wait_deadline = time.monotonic() + max(0.0, final_grace)
        while source is None and time.monotonic() < source_wait_deadline:
            time.sleep(min(max(0.05, poll), 0.25))
            source = _summary_source(log_path, source_cache, runtime_home=runtime_home,
                                     attempt_cwd=attempt_cwd)
        if source is None:
            state.update(status="degraded", terminal_reason=terminal_reason,
                         summary_source_reason=source_cache.get("source_reason", "summary_source_unavailable"),
                         final_refresh_started=False, final_refresh_complete=False,
                         finished_at=time.time())
            _atomic_write(state_path, state)
            return 0
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
        if harness == "claude" and jobs_path and not model_recorded:
            _record_resolved_model(source, jobs_path, attempt_id, model_cursor)

        final_size = _log_signature(source)[0]
        final_deadline = time.monotonic() + max(0.0, final_grace)
        final_started = False
        final_complete = False
        while final_size and time.monotonic() < final_deadline:
            sidecar = _read_sidecar(harness, sid)
            offset = sidecar.get("offset") if isinstance(sidecar.get("offset"), int) else 0
            db_source = source_cache.get("refresh_source")
            db_fresh = False
            if db_source:
                from fleet.refresh_title import _opencode_source_signatures
                stamps = _opencode_source_signatures(str(source))
                source_ts = max((info[4] / 1e9 for info in stamps.values() if info), default=float("inf"))
                db_fresh = (sidecar.get("ts") or 0) >= source_ts
            expected_cursor = db_source.get("cursor_kind") if isinstance(db_source, dict) else None
            cursor_matches = not expected_cursor or sidecar.get("cursor_kind") == expected_cursor
            if cursor_matches and (db_fresh if db_source and db_source.get("kind") == "opencode-db" else offset >= final_size) and sidecar.get("summary"):
                final_complete = True
                break
            if not final_started:
                final_started = _refresh(
                    harness, sid, source, phase="final", debounce=0, priority=True, prompt_path=prompt,
                    refresh_source=source_cache.get("refresh_source"), runtime_home=runtime_home)
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
        if review_fd is not None:
            try:
                fcntl.flock(review_fd, fcntl.LOCK_UN)
            finally:
                os.close(review_fd)
        if home_read_scope is not None:
            home_read_scope.__exit__(None, None, None)
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
                runtime_home=metadata.get("runtime_home"),
                attempt_cwd=fields[3],
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
    supervise_parser.add_argument("--runtime-home")
    supervise_parser.add_argument("--attempt-cwd")
    supervise_parser.add_argument("--poll", type=float, default=DEFAULT_POLL)
    supervise_parser.add_argument("--initial-delay", type=float, default=DEFAULT_INITIAL_DELAY)
    supervise_parser.add_argument("--periodic-debounce", type=int, default=DEFAULT_PERIODIC_DEBOUNCE)
    supervise_parser.add_argument("--final-grace", type=float, default=DEFAULT_FINAL_GRACE)
    supervise_parser.add_argument("--log-quiet", type=float, default=DEFAULT_LOG_QUIET)
    supervise_parser.add_argument("--review-artifact-root")
    supervise_parser.add_argument("--review-cycle-id")
    supervise_parser.add_argument("--review-lease-nonce")
    ensure_parser = commands.add_parser("ensure")
    from dispatch_contract import inherited_jobs_argument
    ensure_parser.add_argument("--jobs", **inherited_jobs_argument())
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
        runtime_home=args.runtime_home,
        attempt_cwd=args.attempt_cwd,
        review_artifact_root=args.review_artifact_root,
        review_cycle_id=args.review_cycle_id,
        review_lease_nonce=args.review_lease_nonce,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"dispatch-summary: {exc}", file=sys.stderr)
        raise SystemExit(70)
