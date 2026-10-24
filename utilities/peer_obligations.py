#!/usr/bin/env python3
"""Durable task-scoped peer follow-through and shared pane readiness inputs."""

from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Iterable

from dispatch_attempt_policy import (
    PaneObservation,
    RegisteredWorkObservation,
    completion_readiness,
    decide_attempt,
)

SCHEMA_VERSION = 1
RUNNER_LOCK_NAME = "runner-current.lock"
_RUNNER_CONTROL_SCHEMA = 1
_SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,160}\Z")
_MAX_RECORD_BYTES = 128 * 1024
_PENDING_STATES = frozenset({"pending", "unknown", "delivery-pending", "cleanup-pending"})
_DUTY_KINDS = frozenset({"watch", "message", "retire", "registered-batch",
                         "readiness-observation"})
_RUNNERS: list[subprocess.Popen] = []
_DISTRIBUTION_MODULES = {}
_MESSAGE_RECEIVER_PROVENANCE = "validated-peer-ref-receiver-v1"
_LEGACY_PENDING_RECEIPT_PROVENANCE = "validated-legacy-pending-receipt-v1"


def _message_watch_ids(refs):
    if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
        return None
    return [ref[len("peer-watch:"):]
            for ref in refs if ref.startswith("peer-watch:")]


def _message_intent_matches_transfer(record, message):
    if not isinstance(message, dict):
        return False
    intent = record.get("intent") or {}
    observation = record.get("observation") or {}
    ref = intent.get("ref") or observation.get("transfer_ref") or observation.get("ref")
    if ref != message.get("ref"):
        return False
    original_ref = intent.get("delay_notice_for")
    if original_ref and not intent.get("ref"):
        body = intent.get("body")
        return (isinstance(original_ref, str)
                and re.fullmatch(r"[0-9a-f]{32}", original_ref) is not None
                and record.get("id") == "delay-" + original_ref
                and isinstance(body, str) and bool(body)
                and intent.get("body_digest") == hashlib.sha256(body.encode("utf-8")).hexdigest()
                and message.get("source_sha256") == hashlib.sha256(
                    body.rstrip("\n").encode("utf-8")).hexdigest()
                and message.get("refs") == ["delay-notice:" + original_ref, original_ref])
    return (intent.get("body_digest") == message.get("body_sha256")
            and intent.get("refs", []) == message.get("refs", [])
            and intent.get("producer_binding") == message.get("producer_binding"))


def _with_current_message_watch_binding(message, binding, action, failure):
    refs = message.get("refs", []) if isinstance(message, dict) else None
    watch_ids = _message_watch_ids(refs)
    if watch_ids is None:
        return failure
    watches = binding.get("watches") if isinstance(binding, dict) else None
    if not watch_ids:
        return failure if isinstance(watches, list) and watches else action()
    if (not isinstance(watches, list)
            or [watch.get("watch_id") for watch in watches if isinstance(watch, dict)] != watch_ids):
        return failure
    try:
        import dispatch_seat_handover as handover
        guard = handover.watch_delivery_binding_lock(binding)
    except (ImportError, OSError, ValueError, RuntimeError):
        return failure
    with guard as current:
        if not current:
            return failure
        return action()


class ObligationError(ValueError):
    pass


def _code_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _distribution_module(root: Path):
    path = root / "tools" / "install" / "distribution.py"
    key = str(path.resolve(strict=True))
    module = _DISTRIBUTION_MODULES.get(key)
    if module is None:
        spec = importlib.util.spec_from_file_location("_peer_runner_distribution", key)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _DISTRIBUTION_MODULES[key] = module
    return module


def _release_root(root: Path, releases: Path) -> Path | None:
    try:
        resolved = root.resolve(strict=True)
        releases = releases.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if root.is_symlink() or resolved.parent != releases or not resolved.is_dir():
        return None
    required = (
        "RELEASE_VERSION",
        "harness-manifest.json",
        "core/CORE.md",
        "utilities/peer_obligations.py",
        "utilities/peer-steward.py",
    )
    return resolved if all((resolved / relative).is_file() for relative in required) else None


def _service_layout(root: Path):
    """Resolve the exact installer current pointer for a managed release root."""
    try:
        root = root.resolve(strict=True)
        distribution = _distribution_module(root)
        if distribution is None:
            return None
        data_root = Path(distribution.data_root()).expanduser()
        pointer = Path(distribution.current_path()).expanduser()
        if not data_root.is_absolute() or not pointer.is_absolute():
            return None
        data_root = Path(os.path.abspath(os.fspath(data_root)))
        pointer = Path(os.path.abspath(os.fspath(pointer)))
        releases = data_root / "releases"
        if pointer != data_root / "current" or releases.is_symlink():
            return None
        releases_real = releases.resolve(strict=True)
        if root.parent != releases_real or _release_root(root, releases_real) is None:
            return None
        return root, data_root, releases_real, pointer
    except (OSError, RuntimeError, ValueError, AttributeError):
        return None


def _service_environment_matches(root: Path, environ=None) -> bool:
    environ = os.environ if environ is None else environ
    for name in ("AGENT_HOME", "CLAUDE_HOME"):
        raw = environ.get(name)
        if not raw:
            continue
        try:
            if Path(raw).expanduser().resolve(strict=True) != root:
                return False
        except (OSError, RuntimeError, ValueError):
            return False
    return True


def validate_service_binding(service_current: str | Path | None, *, root=None) -> str | None:
    """Keep only this code root's lexical managed-current binding; never infer it."""
    if not service_current:
        return None
    try:
        code_root = Path(root).resolve(strict=True) if root is not None else _code_root()
        layout = _service_layout(code_root)
        if layout is None or not _service_environment_matches(code_root):
            return None
        raw = Path(service_current).expanduser()
        if not raw.is_absolute() or Path(os.path.abspath(os.fspath(raw))) != layout[3]:
            return None
        return str(layout[3])
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def managed_service_binding(source_root: str | Path | None) -> str | None:
    """Issue an internal lifecycle binding only for this packaged managed root."""
    if source_root is None:
        return None
    try:
        source = Path(source_root).expanduser().resolve(strict=True)
        if source != _code_root() or not _service_environment_matches(source):
            return None
        layout = _service_layout(source)
        return str(layout[3]) if layout is not None else None
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def active_service_root(service_current: str | Path | None, *, root=None) -> Path | None:
    """Resolve a validated binding's active release without comparing versions."""
    try:
        code_root = Path(root).resolve(strict=True) if root is not None else _code_root()
        binding = validate_service_binding(service_current, root=code_root)
        if binding is None:
            return None
        layout = _service_layout(code_root)
        if layout is None or layout[3] != Path(binding) or not layout[3].is_symlink():
            return None
        target = layout[3].resolve(strict=True)
        return _release_root(target, layout[2])
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def _service_exec_environment(source_root: Path, target_root: Path, environ=None) -> dict:
    child_env = dict(os.environ if environ is None else environ)
    source_root = source_root.resolve(strict=True)
    target_root = target_root.resolve(strict=True)
    layout = _service_layout(source_root)
    releases_root = layout[2] if layout is not None else None
    child_env["AGENT_HOME"] = str(target_root)
    if child_env.get("CLAUDE_HOME"):
        try:
            if Path(child_env["CLAUDE_HOME"]).expanduser().resolve(strict=True) == source_root:
                child_env["CLAUDE_HOME"] = str(target_root)
        except (OSError, RuntimeError, ValueError):
            pass
    pythonpath = child_env.get("PYTHONPATH")
    if pythonpath is not None:
        rewritten = []
        for entry in pythonpath.split(os.pathsep):
            if not entry:
                rewritten.append(entry)
                continue
            try:
                resolved = Path(entry).expanduser().resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                rewritten.append(entry)
                continue
            try:
                relative = resolved.relative_to(source_root)
            except ValueError:
                if releases_root is None:
                    rewritten.append(entry)
                    continue
                try:
                    relative_release = resolved.relative_to(releases_root)
                    if not relative_release.parts:
                        raise ValueError("not a release code path")
                    relative = Path(*relative_release.parts[1:]) if len(relative_release.parts) > 1 else Path()
                except (OSError, RuntimeError, ValueError):
                    rewritten.append(entry)
                else:
                    rewritten.append(str(target_root / relative))
            else:
                rewritten.append(str(target_root / relative))
        child_env["PYTHONPATH"] = os.pathsep.join(rewritten)
    return child_env


def service_script(relative_script: str | Path, service_current: str | Path | None = None,
                   *, root=None, environ=None):
    """Resolve a service script only through an explicitly validated binding.

    Fixed callers receive a script beneath their current code root. Lifecycle
    callers may pass the verified managed-current binding to select the active
    release and rebuild its service environment.
    """
    code_root = Path(root).resolve(strict=True) if root is not None else _code_root().resolve(strict=True)
    relative = Path(relative_script)
    if relative.is_absolute():
        relative = relative.resolve(strict=True).relative_to(code_root)
    if not relative.parts or ".." in relative.parts:
        raise ValueError("service-script-path-invalid")
    binding = validate_service_binding(service_current, root=code_root)
    target_root = active_service_root(binding, root=code_root) if binding else None
    target_root = target_root or code_root
    script = target_root / relative
    if not script.is_file():
        raise FileNotFoundError(script)
    environment = (_service_exec_environment(code_root, target_root, environ=environ)
                   if binding and target_root != code_root else None)
    return script, environment, binding


def _proc_start_identity(pid: int):
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2:].split()
        if not fields or fields[0] in {"Z", "X"}:
            return None
        return fields[19]
    except (OSError, ValueError, TypeError, IndexError):
        return None


def _process_holds_flock(pid: int, lock_path: Path, fd_number: str) -> bool:
    """Require the exact duty lock to be attached to this process's open fd."""
    try:
        info = lock_path.stat()
        device = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}"
        inode = str(info.st_ino)
        fdinfo = Path(f"/proc/{pid}/fdinfo/{fd_number}").read_text(encoding="ascii")
        for line in fdinfo.splitlines():
            fields = line.split()
            if (len(fields) >= 7 and fields[0] == "lock:" and fields[2] == "FLOCK"
                    and fields[6] == device + ":" + inode):
                return True
    except (OSError, ValueError, TypeError):
        return False
    return False


def replace_managed_observer(pid: int, pid_start: str | None, lock_path: str | Path,
                             relative_script: str | Path, required_arguments: Iterable[str],
                             service_current: str | Path, *, lock_fd_option: str,
                             root=None, timeout: float = 2.0,
                             exact_arguments: bool = False,
                             argument_variants: Iterable[Iterable[str]] = (),
                             service_current_option: str = "--service-current"):
    """Stop one exact old observer after proving its same-install duty lock.

    Returns ``current`` for an already-current observer, ``replaced`` after
    that exact process exits, and ``False`` when any part of the proof is
    unavailable. No duty data is changed here.
    """
    if pid_start is not None and (
            not isinstance(pid_start, str) or not pid_start.isdecimal()):
        return False
    if (isinstance(pid, bool) or not isinstance(pid, (int, str))
            or (isinstance(pid, str) and not pid.isdecimal())):
        return False
    try:
        code_root = Path(root).resolve(strict=True) if root is not None else _code_root().resolve(strict=True)
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            return False
        pid = int(pid)
        if pid <= 0:
            return False
        expected_start = pid_start
        lock = Path(lock_path).resolve(strict=True)
        relative = Path(relative_script)
        expected_args = list(map(str, required_arguments))
        expected_variants = [expected_args]
        if exact_arguments:
            expected_variants.extend(list(map(str, variant)) for variant in argument_variants)
        pidfd = os.pidfd_open(pid)
        try:
            def inspect():
                nonlocal expected_start
                # Resolve the managed target for every inspection. Activation
                # may change the current release between the initial check and
                # the pidfd signal boundary.
                binding = validate_service_binding(service_current, root=code_root)
                target_root = active_service_root(binding, root=code_root) if binding else None
                if target_root is None:
                    return None
                observed_start = _proc_start_identity(pid)
                if not observed_start:
                    return None
                if expected_start is None:
                    # PID-only historical rows may bind only to the process
                    # behind this newly opened pidfd. Subsequent inspections
                    # must retain that exact birth identity.
                    expected_start = observed_start
                elif observed_start != expected_start:
                    return None
                namespace = os.stat(f"/proc/{pid}/ns/pid").st_ino
                if namespace != os.stat("/proc/self/ns/pid").st_ino:
                    return None
                proc = Path("/proc") / str(pid)
                argv = proc.joinpath("cmdline").read_bytes().decode().rstrip("\0").split("\0")
                if len(argv) < 3:
                    return None
                script = Path(argv[1]).resolve(strict=True)
                source_root = script.parent.parent
                if (script != source_root / relative or _release_root(
                        source_root, _service_layout(code_root)[2]) is None):
                    return None
                source_layout = _service_layout(source_root)
                target_layout = _service_layout(code_root)
                if (source_layout is None or target_layout is None
                        or source_layout[1] != target_layout[1]
                        or source_layout[2] != target_layout[2]
                        or source_layout[3] != target_layout[3]):
                    return None
                lock_fd_number = None
                if exact_arguments:
                    duty_arguments = []
                    control_values = {}
                    control_options = {lock_fd_option, service_current_option}
                    index = 2
                    while index < len(argv):
                        option = argv[index]
                        if option in control_options:
                            if option in control_values or index + 1 >= len(argv):
                                return None
                            value = argv[index + 1]
                            if not value or value.startswith("--"):
                                return None
                            control_values[option] = value
                            index += 2
                            continue
                        duty_arguments.append(option)
                        index += 1
                    if duty_arguments not in expected_variants:
                        return None
                    lock_fd_number = control_values.get(lock_fd_option)
                    if lock_fd_number is None or not lock_fd_number.isdecimal():
                        return None
                    observed_binding = control_values.get(service_current_option)
                    if observed_binding is not None and (
                            validate_service_binding(observed_binding, root=code_root) != binding):
                        return None
                elif not any(argv[i:i + len(expected_args)] == expected_args
                             for i in range(2, len(argv) - len(expected_args) + 1)):
                    return None
                try:
                    env = {}
                    for item in proc.joinpath("environ").read_bytes().split(b"\0"):
                        if b"=" in item:
                            key, value = item.split(b"=", 1)
                            env[key.decode()] = value.decode(errors="replace")
                    # The launcher can inherit a different release path from
                    # the script's source root. Accept that provenance only
                    # when it resolves to this same managed installation.
                    for name in ("AGENT_HOME", "CLAUDE_HOME"):
                        if name not in env:
                            continue
                        raw = env.get(name)
                        if not raw:
                            return None
                        env_root = Path(raw).expanduser().resolve(strict=True)
                        env_layout = _service_layout(env_root)
                        if env_layout is None or env_layout[1:] != source_layout[1:]:
                            return None
                    if "HARNESS_DATA_ROOT" in env:
                        raw = env["HARNESS_DATA_ROOT"]
                        if not raw:
                            return None
                        observed_data_root = Path(raw).expanduser()
                    elif "XDG_DATA_HOME" in env:
                        raw = env["XDG_DATA_HOME"]
                        if not raw:
                            return None
                        observed_data_root = Path(raw).expanduser() / "hearting"
                    else:
                        home = env.get("HOME")
                        if not home:
                            return None
                        observed_data_root = (
                            Path(home).expanduser() / ".local" / "share" / "hearting")
                    if (not observed_data_root.is_absolute()
                            or Path(os.path.abspath(os.fspath(observed_data_root)))
                            != source_layout[1]):
                        return None
                except (OSError, RuntimeError, ValueError):
                    return None
                try:
                    if lock_fd_number is None:
                        fd_index = argv.index(lock_fd_option)
                        lock_fd_number = argv[fd_index + 1]
                    held_path = Path(os.path.realpath(proc / "fd" / lock_fd_number))
                except (ValueError, IndexError, OSError):
                    return None
                if held_path != lock or not _process_holds_flock(pid, lock, lock_fd_number):
                    return None
                if source_root == target_root:
                    return ("current", None)
                return ("legacy", None)

            status = inspect()
            if status is None:
                return False
            if status[0] == "current":
                return "current"
            # Recheck all evidence immediately before signaling the pidfd-bound
            # process. A stale PID or changed duty lock cannot authorize this.
            status = inspect()
            if status is None:
                return False
            if status[0] == "current":
                return "current"
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
            deadline = time.monotonic() + max(0.0, timeout)
            while time.monotonic() < deadline:
                if _proc_start_identity(pid) != expected_start:
                    return "replaced"
                time.sleep(0.02)
            return False
        finally:
            os.close(pidfd)
    except (OSError, RuntimeError, ValueError, TypeError, IndexError):
        return False


def exec_service_runner(lock_fd: int, state_root: str | Path, service_current: str | Path,
                        *, herdr_session: str | None = None) -> bool:
    """Replace this process with the same-install runner while retaining its flock."""
    was_inheritable = None
    try:
        source_root = _code_root().resolve(strict=True)
        binding = validate_service_binding(service_current, root=source_root)
        if binding is None:
            return False
        target_root = active_service_root(binding, root=source_root)
        if target_root is None or target_root == source_root:
            return False
        runner = target_root / "utilities" / "peer-steward.py"
        argv = [sys.executable, str(runner)]
        if herdr_session:
            argv.extend(("--herdr-session", herdr_session))
        argv.extend(("__obligation-runner", "--lock-fd", str(lock_fd),
                     "--state-root", str(Path(state_root).resolve(strict=True)),
                     "--service-current", binding))
        # Selection, strict root resolution and environment preparation are all
        # pre-exec work. Any OSError here is the same unsuccessful handoff as an
        # execve error: this process keeps its lock and retries on its next pass.
        child_env = _service_exec_environment(source_root, target_root)
        was_inheritable = os.get_inheritable(lock_fd)
        os.set_inheritable(lock_fd, True)
        os.execve(sys.executable, argv, child_env)
    except OSError:
        if was_inheritable is not None:
            try:
                os.set_inheritable(lock_fd, was_inheritable)
            except OSError:
                pass
        return False
    return True


def _runner_control_payload(service_current: str | Path, *, root=None) -> bytes | None:
    """Serialize only provenance freshly verified for this installed code root."""
    try:
        code_root = Path(root).resolve(strict=True) if root is not None else _code_root().resolve(strict=True)
        binding = validate_service_binding(service_current, root=code_root)
        layout = _service_layout(code_root)
        if binding is None or layout is None or binding != str(layout[3]):
            return None
        return json.dumps({"schema": _RUNNER_CONTROL_SCHEMA,
                           "install_data_root": str(layout[1]),
                           "service_current": binding},
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def _remembered_service_binding(lock_fd: int, *, root=None) -> str | None:
    """Read a lock payload only while holding its lock; malformed data is fixed."""
    try:
        code_root = Path(root).resolve(strict=True) if root is not None else _code_root().resolve(strict=True)
        raw = os.pread(lock_fd, 4097, 0)
        if not raw or len(raw) > 4096:
            return None
        value = json.loads(raw.decode("utf-8"))
        if (not isinstance(value, dict) or set(value) !=
                {"schema", "install_data_root", "service_current"}
                or type(value.get("schema")) is not int
                or value["schema"] != _RUNNER_CONTROL_SCHEMA
                or not isinstance(value.get("install_data_root"), str)
                or not isinstance(value.get("service_current"), str)):
            return None
        layout = _service_layout(code_root)
        binding = validate_service_binding(value["service_current"], root=code_root)
        if (layout is None or binding is None
                or value["install_data_root"] != str(layout[1])
                or binding != str(layout[3])):
            return None
        return binding
    except (OSError, RuntimeError, ValueError, TypeError, UnicodeError):
        return None


def _write_runner_control(lock_fd: int, payload: bytes) -> None:
    """Update the existing locked inode; never replace the runner lock file."""
    os.ftruncate(lock_fd, 0)
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        offset += os.pwrite(lock_fd, view[offset:], offset)
    os.fsync(lock_fd)


def peer_state_root() -> Path:
    # Lazy import keeps the shared store independent of peer-message's CLI load.
    import importlib.util

    path = Path(__file__).with_name("peer-message.py")
    spec = importlib.util.spec_from_file_location("_peer_obligations_message", path)
    if spec is None or spec.loader is None:
        raise ObligationError("peer-state-root-unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return Path(module.peer_state_root()).resolve()


def obligations_root(root: str | Path | None = None) -> Path:
    base = Path(root).expanduser().resolve() if root is not None else peer_state_root()
    if base.is_symlink():
        raise ObligationError("peer-state-root-symlink")
    directory = base / "peer-steward" / "obligations"
    if directory.exists() and directory.is_symlink():
        raise ObligationError("peer-obligations-root-symlink")
    return directory


def request_obligation_recovery(*, lifecycle_root: str | Path | None = None) -> bool:
    """Start the common recovery entry detached with its caller environment."""
    root = Path(lifecycle_root).expanduser().resolve(strict=False) if lifecycle_root is not None else _code_root()
    script = root / "utilities" / "peer-steward.py"
    command = [sys.executable, str(script), "__ensure-obligations"]
    if lifecycle_root is not None:
        command.extend(["--lifecycle-root", str(root)])
    try:
        subprocess.Popen(
            command,
            cwd=str(root),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _safe_id(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ObligationError("peer-obligation-id-invalid")
    return value


def _read_record(path: Path) -> dict | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(_MAX_RECORD_BYTES + 1)
        if len(raw) > _MAX_RECORD_BYTES:
            raise ObligationError("peer-obligation-record-oversized")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        if isinstance(exc, ObligationError):
            raise
        raise ObligationError("peer-obligation-record-invalid") from exc
    if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
            or not isinstance(value.get("id"), str) or not isinstance(value.get("intent"), dict)):
        raise ObligationError("peer-obligation-record-invalid")
    return value


def _write_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ObligationError("peer-obligation-path-symlink")
    fd, temporary_name = tempfile.mkstemp(prefix=".obligation-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def retain_registered_completion(jobs, attempt_id, session_id, carrier, *, holder=None,
                                 delivery_record=None, receiving_turn=None,
                                 request_context=None):
    """Retain an exact completion and, when supplied, its native receiving turn."""
    from dispatch_completion_join import JoinContractError, current_attempt_row
    from dispatch_seat_handover import effective_parent
    from harness_capabilities import HARNESSES, parent_completion
    try:
        row = current_attempt_row(Path(jobs), attempt_id)
    except JoinContractError:
        return
    harness = next((h for h in HARNESSES if parent_completion(h)["carrier"] == carrier), None)
    if (not harness or row is None or row.status not in {"open", "running", "done"}
            or row.metadata.get("parent_completion_delivery") != carrier
            or effective_parent(row.metadata, Path(jobs)) != session_id):
        return False
    receiving_observation = None
    if delivery_record is not None:
        import dispatch_pending_delivery as pending
        from dispatch_completion_join import pending_record_identity
        turn_keys = {"thread_id", "turn_id", "status"}
        if (not isinstance(receiving_turn, dict) or set(receiving_turn) != turn_keys
                or receiving_turn.get("thread_id") != session_id
                or not isinstance(receiving_turn.get("turn_id"), str)
                or not receiving_turn["turn_id"]
                or receiving_turn.get("status") not in {
                    "inProgress", "in_progress", "running", "interrupted", "completed", "complete"
                }):
            return False
        storage = delivery_record.get("_storage_key") or row.metadata.get("parent_sid")
        delivery_id = row.metadata.get("delivery_id", "")
        if (storage != row.metadata.get("parent_sid") or not delivery_id
                or delivery_record.get("delivery_id") != delivery_id
                or delivery_record.get("recipient_kind") != carrier
                or attempt_id not in (delivery_record.get("attempt_ids") or [])):
            return False
        try:
            root = Path(jobs).resolve(strict=True).parent
            exact = pending.read(root, storage, delivery_id)
            if (exact is None or exact.get("state") not in pending.OPEN_STATES
                    or exact.get("recipient_kind") != carrier
                    or exact.get("attempt_ids") != delivery_record.get("attempt_ids")
                    or exact.get("receipt_digest") != row.metadata.get("delivery_receipt_digest")
                    or exact.get("row_revisions", {}).get(attempt_id)
                        != row.metadata.get("delivery_row_revision")):
                return False
            route_id, route_node, parent_attempt_id = pending_record_identity(row.metadata, Path(jobs))
            exact_identity = pending.received_ack_identity(exact, storage, delivery_id)
            if (exact_identity.get("route_id") != route_id
                    or exact_identity.get("route_node") != route_node
                    or exact_identity.get("parent_attempt_id") != parent_attempt_id
                    or effective_parent(row.metadata, Path(jobs)) != session_id):
                return False
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            return False
        receiving_observation = {
            **receiving_turn, "delivery_id": delivery_id,
            "receipt_digest": row.metadata.get("delivery_receipt_digest"),
            "row_revision": row.metadata.get("delivery_row_revision"),
            "storage_recipient": storage, "route_id": route_id,
            "route_node": route_node, "parent_attempt_id": parent_attempt_id,
            "recipient_sid": session_id,
        }
    identity = {"jobs": str(Path(jobs).resolve()), "attempt_id": attempt_id,
                "session_id": session_id, "harness": harness,
                "server": os.environ.get("AGENT_HERDR_SESSION") or "default"}
    store = ObligationStore(Path(jobs).parent)
    duty = store.create(stable_duty_id("registered-batch", identity),
                        "registered-batch", identity, {"carrier": carrier})
    if duty.get("state") not in {"complete", "cancelled"}:
        if receiving_observation is not None:
            duty = store.retain_native_receiving_turn(
                duty["id"], receiving_observation,
                request_context=request_context, holder=holder)
            if duty is None:
                return False
        elif holder:
            observation = {**(duty.get("observation") or {}), "holder": list(holder)}
            store.update(duty["id"], observation=observation)
        ensure_normal_runner(Path(jobs).parent)
    return True


def retain_registered_notice(jobs, record):
    """Give a new parent handover to the existing runner, apart from old completion."""
    from dispatch_completion_join import current_attempt_row
    from dispatch_seat_handover import effective_parent
    from harness_capabilities import HARNESSES, parent_completion
    import dispatch_supervision as supervision
    jobs = Path(jobs).resolve(strict=True)
    receipt = record.get("receipt") or {}
    if (receipt.get("kind") != supervision.KIND
            or receipt.get("reason") not in {supervision.ANSWER_AWAITING_PARENT, "no-progress"}):
        return False
    row = current_attempt_row(jobs, receipt.get("owner_attempt_id", ""))
    if row is None:
        return False
    carrier = row.metadata.get("parent_completion_delivery")
    harness = next((h for h in HARNESSES if parent_completion(h)["carrier"] == carrier), None)
    storage = row.metadata.get("parent_sid", "")
    if not harness or not storage or record.get("recipient_kind") != carrier:
        return False
    supervision.validate_pending_record(record, jobs=jobs, expected_thread_id=storage,
        expected_epoch=1, expected_attempts={row.attempt_id},
        expected_sealed_batch_id=row.metadata.get("managed_sealed_batch_id", ""))
    identity = {"jobs": str(jobs), "attempt_id": row.attempt_id,
                "session_id": effective_parent(row.metadata, jobs), "harness": harness,
                "server": os.environ.get("AGENT_HERDR_SESSION") or "default"}
    store = ObligationStore(jobs.parent)
    duty = store.create(stable_duty_id("registered-batch", identity, record["delivery_id"]),
        "registered-batch", identity, {"carrier": carrier,
            "notice_delivery_id": record["delivery_id"], "notice_digest": record["receipt_digest"]})
    if duty.get("state") not in {"complete", "cancelled"}:
        ensure_normal_runner(jobs.parent)
    return True


def _registered_delivery_bindings(message, roots=None, *, received_only=False,
                                  require_current_recipient=True, read_context=None):
    if (received_only and (message.get("state") != "received"
            or message.get("receipt") not in {"exact-peer-ref", "exact-native-history"})):
        return
    if message.get("state") not in {"pending", "queued", "unverified", "received"}:
        return
    many = message.get("dispatch_notices")
    if many is not None:
        if (not isinstance(many, list) or not 1 < len(many) <= 24
                or message.get("dispatch_notice") is not None
                or any(not isinstance(item, dict) for item in many)
                or len({item.get("delivery_id") for item in many}) != len(many)):
            return
        for binding in many:
            member = {key: value for key, value in message.items()
                      if key not in {"dispatch_notice", "dispatch_notices"}}
            member["_dispatch_notice_batch"] = many
            yield from _registered_delivery_bindings(
                {**member, "dispatch_notice": binding}, roots,
                received_only=received_only,
                require_current_recipient=require_current_recipient,
                read_context=read_context)
        return
    binding = message.get("dispatch_notice") or {}
    expected_binding = {"duty_id", "jobs", "attempt_id", "carrier", "recipient_sid",
                        "storage_recipient", "delivery_id", "claim_owner"}
    if (not isinstance(binding, dict) or set(binding) not in (
                expected_binding, expected_binding | {"receipt_digest"})
            or binding.get("duty_id") not in (message.get("refs") or [])
            or "_dispatch_notice_batch" in message and (
                binding.get("attempt_id") not in (message.get("refs") or [])
                or binding.get("delivery_id") not in (message.get("refs") or []))
            or not isinstance(message.get("ref"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", message["ref"])):
        return
    from dispatch_completion_join import current_attempt_row
    import dispatch_pending_delivery as pending
    recipient = message.get("to") or {}
    jobs = Path(binding["jobs"])
    if not jobs.is_absolute() or jobs.is_symlink() or not jobs.is_file():
        return
    try:
        root = jobs.resolve(strict=True).parent
    except (OSError, RuntimeError, ValueError):
        return
    if roots is not None and root not in {Path(r).resolve() for r in roots}:
        return
    store = ObligationStore(root)
    duty = store.get(binding["duty_id"])
    intent = (duty or {}).get("intent") or {}
    identity = intent.get("identity") or {}
    if (intent.get("kind") != "registered-batch" or identity.get("jobs") != str(jobs)
            or identity.get("attempt_id") != binding.get("attempt_id")
            or binding.get("recipient_sid") != recipient.get("session_id")
            or intent.get("carrier") != binding.get("carrier")
            or (intent.get("notice_delivery_id")
                and intent["notice_delivery_id"] != binding.get("delivery_id"))
            or binding.get("claim_owner") not in {
                "retained-rewake:" + item.get("duty_id", "")
                for item in message.get("_dispatch_notice_batch", [binding])
                if isinstance(item, dict)}):
        return
    try:
        row = (current_attempt_row(jobs, binding["attempt_id"]) if read_context is None else
               current_attempt_row(jobs, binding["attempt_id"], read_context=read_context))
        storage, delivery_id = binding["storage_recipient"], binding["delivery_id"]
        record = pending.read(root, storage, delivery_id)
        if (row is None or not record or record.get("delivery_id") != delivery_id
                or row.attempt_id not in record.get("attempt_ids", [])
                or record.get("recipient_kind") != binding.get("carrier")
                or record.get("recipient_digest") != pending.recipient_digest(storage)
                or row.metadata.get("parent_sid") != storage
                or row.metadata.get("parent_completion_delivery") != binding.get("carrier")):
            return
        if (binding.get("receipt_digest") is not None
                and binding.get("receipt_digest") != record.get("receipt_digest")):
            return
        producer = message.get("producer_binding")
        sender = message.get("from") or {}
        if sender.get("harness") == "runtime":
            expected = {
                "schema": "registered-runtime-completion-v1",
                "duty_id": duty["id"], "jobs": str(jobs),
                "attempt_id": row.attempt_id, "carrier": binding["carrier"],
                "delivery_id": delivery_id, "receipt_digest": record.get("receipt_digest"),
                "recipient": {"harness": recipient.get("harness"),
                              "session_id": recipient.get("session_id")},
                "route_id": str(row.metadata.get("route_id") or ""),
                "route_node": str(row.metadata.get("route_node") or ""),
            }
            producer_matches = producer == expected
            producer_notices = (producer or {}).get("dispatch_notices") if isinstance(producer, dict) else None
            if producer_notices is not None:
                producer_matches = (
                    isinstance(producer_notices, list)
                    and producer.get("schema") == expected["schema"]
                    and producer.get("jobs") == str(jobs)
                    and producer.get("recipient") == expected["recipient"]
                    and any(item == {"duty_id": duty["id"], "attempt_id": row.attempt_id,
                                    "delivery_id": delivery_id,
                                    "storage_recipient": storage,
                                    "receipt_digest": record.get("receipt_digest"),
                                    "claim_owner": binding["claim_owner"]}
                            for item in producer_notices))
            if (not producer_matches or sender.get("session_id") not in (None, "")
                    or sender.get("name") != "Hearting runtime"
                    or not {duty["id"], row.attempt_id, delivery_id}.issubset(
                        set(message.get("refs") or []))):
                return
        elif producer is not None:
            return
        if intent.get("notice_delivery_id") and record.get("receipt_digest") != intent.get("notice_digest"):
            return
        from dispatch_seat_handover import effective_parent, effective_parent_harness
        parent = (effective_parent(row.metadata, jobs) if read_context is None else
                  effective_parent(row.metadata, jobs, read_context=read_context))
        if require_current_recipient:
            parent_harness = (effective_parent_harness(row.metadata, jobs)
                              if read_context is None else effective_parent_harness(
                                  row.metadata, jobs, read_context=read_context))
            expected_harness = (identity.get("harness", "") if parent == str(
                row.metadata.get("parent_sid") or "") else parent_harness or identity.get("harness", ""))
            if (parent != binding["recipient_sid"]
                    or expected_harness != recipient.get("harness")):
                return
        if (row.metadata.get("delivery_intent") == "1"
                and row.metadata.get("delivery_id") == delivery_id and (
                not row.metadata.get("delivery_receipt_digest")
                or record.get("receipt_digest") != row.metadata.get("delivery_receipt_digest")
                or not row.metadata.get("delivery_row_revision")
                or (record.get("row_revisions") or {}).get(row.attempt_id)
                    != row.metadata.get("delivery_row_revision"))):
            return
        identity = pending.received_ack_identity(record, storage, delivery_id)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return
    yield store, duty, row, record, root, storage, delivery_id


def registered_delivery_settled(message, *, roots=None, read_context=None):
    """Settle only when every exact binding in a retained delivery is terminal."""
    bindings = list(_registered_delivery_bindings(
        message, roots, require_current_recipient=False, read_context=read_context))
    declared = message.get("dispatch_notices")
    if declared is not None and (not isinstance(declared, list) or len(bindings) != len(declared)):
        return False
    return bool(bindings) and all(record["state"] in {"acked", "rejected", "expired"}
                                  for _store, _duty, _row, record, _root, _storage, _delivery_id
                                  in bindings)


def received_registered_ack(jobs, record, recipient_sid) -> bool:
    """Read an exact peer receipt without ACKing it or advancing any duty."""
    import dispatch_pending_delivery as pending
    actor = record.get("acked_by")
    if not isinstance(actor, str) or not actor.startswith("peer-received:"):
        return False
    ref = actor.removeprefix("peer-received:")
    if not re.fullmatch(r"[0-9a-f]{32}", ref):
        return False
    root = Path(jobs).resolve(strict=True).parent
    spec = importlib.util.spec_from_file_location("_registered_ack_reader", _code_root() / "utilities/peer-message.py")
    module = sys.modules.get(spec.name)
    if module is None:
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    try:
        message = module._read_pending(ref, roots=[root], state_root=root)
        if not message or (message.get("to") or {}).get("session_id") != recipient_sid:
            return False
        for _store, _duty, _row, exact, _root, storage, delivery_id in _registered_delivery_bindings(
                message, roots=[root], received_only=True):
            if pending.received_ack_identity(exact, storage, delivery_id) == pending.received_ack_identity(
                    record, storage, delivery_id):
                return True
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False
    return False


def acknowledge_registered_delivery(message, *, roots=None):
    """Settle only a delivery proven by this exact recipient-side peer receipt.

    Return true when the receipt is bound to the current delivery, even if the
    queue write needs a later retry. Callers use that fact to avoid preparing a
    duplicate transfer while preserving the open delivery for recovery.
    """
    if message.get("state") != "received":
        return False
    import dispatch_pending_delivery as pending
    matched = False
    for store, duty, row, record, root, storage, delivery_id in _registered_delivery_bindings(
            message, roots, received_only=True):
        matched = True
        settled = record
        if record["state"] in pending.OPEN_STATES:
            try:
                identity = pending.received_ack_identity(record, storage, delivery_id)
                settled = pending.ack_received(root, storage, delivery_id,
                    expected_identity=identity, acked_by="peer-received:" + message["ref"])
            except (OSError, ValueError, RuntimeError):
                continue
        if settled["state"] != "acked":
            continue
        terminal = (duty.get("intent", {}).get("notice_delivery_id") == delivery_id
                    or (row.status == "done" and row.metadata.get("delivery_id") == delivery_id))
        if duty.get("state") not in {"complete", "cancelled"}:
            try:
                store.update(duty["id"], state="complete" if terminal else "pending",
                             delivery="acknowledged", cleanup="complete" if terminal else "pending")
            except (OSError, ValueError, RuntimeError):
                pass
    return matched


class ObligationStore:
    """Atomic intent records; immutable intent is separate from mutable progress."""

    def __init__(self, root: str | Path | None = None):
        self.root = obligations_root(root)
        self._list_cache: dict[str, tuple[tuple[int, int, int, int, int], dict | None]] = {}
        self._discovery_metrics = {"stat_calls": 0, "json_reads": 0, "json_parses": 0}
        self._active_list_context = None

    def begin_read_pass(self) -> None:
        """Share one discovery snapshot among lists made during a runner pass."""
        self._active_list_context = {"paths": None, "records": {}}

    def end_read_pass(self) -> None:
        """Drop the pass snapshot so the next list observes external changes."""
        self._active_list_context = None

    def _record_path(self, duty_id: str) -> Path:
        return self.root / (_safe_id(duty_id) + ".json")

    def _lock(self, duty_id: str):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.root / (_safe_id(duty_id) + ".lock")
        if path.is_symlink():
            raise ObligationError("peer-obligation-lock-symlink")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def get(self, duty_id: str) -> dict | None:
        return _read_record(self._record_path(duty_id))

    @staticmethod
    def _list_signature(path: Path):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ObligationError("peer-obligation-record-invalid")
        if not stat.S_ISREG(info.st_mode):
            return None
        return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)

    def _read_list_record(self, path: Path):
        key = str(path)
        for _attempt in range(3):
            try:
                before = self._list_signature(path)
            except FileNotFoundError:
                self._list_cache.pop(key, None)
                return None
            self._discovery_metrics["stat_calls"] += 1
            if before is None:
                self._list_cache.pop(key, None)
                return None
            cached = self._list_cache.get(key)
            if cached is not None and cached[0] == before:
                return cached[1]

            self._list_cache.pop(key, None)
            self._discovery_metrics["json_reads"] += 1
            self._discovery_metrics["json_parses"] += 1
            record = _read_record(path)
            try:
                after = self._list_signature(path)
            except FileNotFoundError:
                self._list_cache.pop(key, None)
                return None
            self._discovery_metrics["stat_calls"] += 1
            if before == after:
                self._list_cache[key] = (after, copy.deepcopy(record))
                return record
        self._list_cache.pop(key, None)
        raise ObligationError("peer-obligation-list-unstable")

    def discovery_metrics(self) -> dict[str, int]:
        return dict(self._discovery_metrics)

    def reset_discovery_metrics(self) -> None:
        for key in self._discovery_metrics:
            self._discovery_metrics[key] = 0

    def list(self, *, states: Iterable[str] = _PENDING_STATES,
             kinds: Iterable[str] | None = None) -> list[dict]:
        if not self.root.is_dir():
            self._list_cache.clear()
            return []
        wanted = set(states)
        wanted_kinds = None if kinds is None else set(kinds)
        output = []
        context = self._active_list_context
        if context is None:
            context = {"paths": None, "records": {}}
        if context["paths"] is None:
            paths = sorted(self.root.glob("*.json"))
            context["paths"] = paths
            present = {str(path) for path in paths}
            for stale in set(self._list_cache).difference(present):
                self._list_cache.pop(stale, None)
        paths = context["paths"]
        for path in paths:
            key = str(path)
            if key not in context["records"]:
                context["records"][key] = self._read_list_record(path)
            record = context["records"][key]
            if (record is not None and record.get("state") in wanted
                    and (wanted_kinds is None
                         or (record.get("intent") or {}).get("kind") in wanted_kinds)):
                output.append(copy.deepcopy(record))
        return output

    def _invalidate_list_record(self, duty_id: str) -> None:
        path = self._record_path(duty_id)
        key = str(path)
        self._list_cache.pop(key, None)
        context = self._active_list_context
        if context is None:
            return
        context["records"].pop(key, None)
        if context["paths"] is not None and path.exists() and key not in {
                str(candidate) for candidate in context["paths"]}:
            context["paths"] = sorted([*context["paths"], path])

    def create(self, duty_id: str, kind: str, identity: dict, intent: dict) -> dict:
        duty_id = _safe_id(duty_id)
        if kind not in _DUTY_KINDS or not isinstance(identity, dict) or not isinstance(intent, dict):
            raise ObligationError("peer-obligation-intent-invalid")
        path = self._record_path(duty_id)
        fd = self._lock(duty_id)
        try:
            current = _read_record(path)
            immutable = {"kind": kind, "identity": identity, **intent}
            if current is not None:
                if current.get("intent") != immutable:
                    raise ObligationError("peer-obligation-intent-conflict")
                return current
            record = {"schema_version": SCHEMA_VERSION, "id": duty_id,
                      "intent": immutable, "state": "pending", "revision": 1,
                      "accepted_at": time.time(), "updated_at": time.time(),
                      "observation": None, "result": None, "delivery": "pending",
                      "cleanup": "pending"}
            _write_atomic(path, record)
            return record
        finally:
            os.close(fd)
            self._invalidate_list_record(duty_id)

    def retain_native_receiving_turn(self, duty_id: str, binding: dict, *,
                                     request_context: str | None = None,
                                     holder: list | None = None) -> dict | None:
        """Atomically retain a Codex receive binding and its optional hook prompt."""
        path = self._record_path(duty_id)
        fd = self._lock(duty_id)
        try:
            record = _read_record(path)
            if record is None:
                raise ObligationError("peer-obligation-missing")
            if record.get("state") in {"complete", "cancelled"}:
                return record
            observation = dict(record.get("observation") or {})
            existing_binding = observation.get("native_receiving_turn")
            if existing_binding is not None and existing_binding != binding:
                return None
            observation["native_receiving_turn"] = binding
            if holder:
                observation["holder"] = list(holder)

            context_applied = False
            if request_context is not None:
                bound_context = {
                    "source": "codex-userprompt-submit",
                    "thread_id": binding["thread_id"],
                    "turn_id": binding["turn_id"],
                    "delivery_id": binding["delivery_id"],
                    "receipt_digest": binding["receipt_digest"],
                    "row_revision": binding["row_revision"],
                }
                if isinstance(request_context, str) and request_context:
                    candidate_context = {**bound_context, "status": "available",
                                         "text": request_context}
                else:
                    candidate_context = {**bound_context, "status": "unavailable",
                                         "reason": "hook-prompt-empty-or-invalid"}
                existing_context = observation.get("native_request_context")
                existing_valid = (isinstance(existing_context, dict)
                                  and existing_context.get("status") == "available"
                                  and isinstance(existing_context.get("text"), str))
                incoming_valid = candidate_context["status"] == "available"
                applied = existing_context is None or (incoming_valid and not existing_valid)
                if applied:
                    observation["native_request_context"] = candidate_context
                    context_applied = True
                elif incoming_valid and existing_valid and existing_context != candidate_context:
                    observation["native_request_context_conflict"] = True

                proposed = {**record, "observation": observation}
                serialized_size = len(json.dumps(
                    proposed, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode("utf-8")) + 1
                # Reserve one maximum-size queue body plus observation metadata.
                if (serialized_size > _MAX_RECORD_BYTES - 20 * 1024
                        and applied and incoming_valid):
                    observation["native_request_context"] = {
                        **bound_context, "status": "unavailable",
                        "reason": "hook-prompt-exceeds-obligation-record-limit",
                    }

            proposed = {**record, "observation": observation}
            serialized_size = len(json.dumps(
                proposed, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")) + 1
            if serialized_size > _MAX_RECORD_BYTES:
                # Optional prompt context must never make an otherwise readable
                # duty exceed the existing record limit.
                observation.pop("native_request_context_conflict", None)
                if context_applied:
                    observation.pop("native_request_context", None)
                proposed["observation"] = observation
                serialized_size = len(json.dumps(
                    proposed, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode("utf-8")) + 1
                if serialized_size > _MAX_RECORD_BYTES:
                    return None
            if observation != record.get("observation"):
                record["observation"] = observation
                record["revision"] = int(record.get("revision", 0)) + 1
                record["updated_at"] = time.time()
                _write_atomic(path, record)
            return record
        finally:
            os.close(fd)
            self._invalidate_list_record(duty_id)

    def prepare_native_continuation(self, duty_id: str, *, continuation_id: str,
                                    source_turn_id: str, source: dict,
                                    message: str) -> tuple[dict, dict]:
        """Persist the first body for this continuation ID under the duty lock."""
        path = self._record_path(duty_id)
        fd = self._lock(duty_id)
        try:
            record = _read_record(path)
            if record is None:
                raise ObligationError("peer-obligation-missing")
            if record.get("state") in {"complete", "cancelled"}:
                raise ObligationError("native-continuation-duty-settled")
            observation = dict(record.get("observation") or {})
            prepared = observation.get("continuation_prepared")
            if (isinstance(prepared, dict) and prepared.get("id") == continuation_id
                    and isinstance(prepared.get("message"), str)):
                return record, prepared
            prepared = {"id": continuation_id, "source_turn_id": source_turn_id,
                        "message": message}
            observation.update({
                "continuation_id": continuation_id,
                "continuation_source_turn": source_turn_id,
                "continuation_source": source,
                "continuation_prepared": prepared,
                "reason": "native-continuation-prepared",
            })
            proposed = {**record, "observation": observation,
                        "state": "pending", "delivery": "pending"}
            size = len(json.dumps(proposed, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")).encode("utf-8")) + 1
            if size > _MAX_RECORD_BYTES:
                raise ObligationError("native-continuation-preparation-exceeds-record-limit")
            record["observation"] = observation
            record["state"] = "pending"
            record["delivery"] = "pending"
            record["revision"] = int(record.get("revision", 0)) + 1
            record["updated_at"] = time.time()
            _write_atomic(path, record)
            return record, prepared
        finally:
            os.close(fd)
            self._invalidate_list_record(duty_id)

    def update(self, duty_id: str, *, state: str | None = None,
               observation: dict | None = None, result: str | None = None,
               delivery: str | None = None, cleanup: str | None = None,
               observer_error: str | None = None,
               expected_phases: set[str] | None = None,
               skip_unchanged: bool = False) -> dict:
        if state is not None and state not in _PENDING_STATES | {"complete", "cancelled"}:
            raise ObligationError("peer-obligation-state-invalid")
        path = self._record_path(duty_id)
        fd = self._lock(duty_id)
        try:
            record = _read_record(path)
            if record is None:
                raise ObligationError("peer-obligation-missing")
            if expected_phases is not None and (
                    record.get("state") in {"complete", "cancelled"}
                    or (record.get("observation") or {}).get("phase", "waiting")
                    not in expected_phases):
                return record
            if observer_error is not None:
                # Error observation cannot undo a claimed side effect or reopen
                # a fulfilled duty. Merge the checkpoint under the same lock.
                if record.get("state") in {"complete", "cancelled"}:
                    return record
                state = "unknown"
                observation = {**(record.get("observation") or {}),
                               "reason": observer_error}
            if observation is not None:
                current_observation = record.get("observation") or {}
                observation = dict(observation)
                for key in ("native_receiving_turn", "native_request_context",
                            "native_request_context_conflict"):
                    if key in current_observation:
                        observation[key] = current_observation[key]
                current_prepared = current_observation.get("continuation_prepared")
                if isinstance(current_prepared, dict):
                    observation["continuation_prepared"] = current_prepared
                    for key in ("continuation_id", "continuation_source_turn",
                                "continuation_source"):
                        if key in current_observation:
                            observation[key] = current_observation[key]
            if result is not None and record.get("result") not in {None, result}:
                raise ObligationError("peer-obligation-result-conflict")
            changed = False
            if state is not None and record.get("state") != state:
                record["state"] = state
                changed = True
            if observation is not None and record.get("observation") != observation:
                record["observation"] = observation
                changed = True
            if result is not None and record.get("result") != result:
                record["result"] = result
                changed = True
            if delivery is not None and record.get("delivery") != delivery:
                record["delivery"] = delivery
                changed = True
            if cleanup is not None and record.get("cleanup") != cleanup:
                record["cleanup"] = cleanup
                changed = True
            if skip_unchanged and not changed:
                return record
            record["revision"] = int(record.get("revision", 0)) + 1
            record["updated_at"] = time.time()
            _write_atomic(path, record)
            return record
        finally:
            os.close(fd)
            self._invalidate_list_record(duty_id)

    def update_message_checkpoint(self, duty_id: str, *, expected_revision: int,
                                  state: str | None = None,
                                  observation: dict | None = None,
                                  result: str | None = None,
                                  delivery: str | None = None,
                                  cleanup: str | None = None,
                                  skip_unchanged: bool = False) -> dict:
        """Apply one message observation only to the row this pass inspected."""
        if state is not None and state not in _PENDING_STATES | {"complete", "cancelled"}:
            raise ObligationError("peer-obligation-state-invalid")
        path = self._record_path(duty_id)
        fd = self._lock(duty_id)
        try:
            record = _read_record(path)
            if record is None:
                raise ObligationError("peer-obligation-missing")
            if (record.get("intent", {}).get("kind") != "message"
                    or record.get("state") in {"complete", "cancelled"}
                    or record.get("revision") != expected_revision):
                return record
            if result is not None and record.get("result") not in {None, result}:
                raise ObligationError("peer-obligation-result-conflict")
            if observation is not None:
                current = record.get("observation") or {}
                observation = dict(observation)
                for key in ("native_receiving_turn", "native_request_context",
                            "native_request_context_conflict", "continuation_prepared",
                            "continuation_id", "continuation_source_turn",
                            "continuation_source"):
                    if key in current:
                        observation[key] = current[key]
            changed = False
            for key, value in (("state", state), ("observation", observation),
                               ("result", result), ("delivery", delivery),
                               ("cleanup", cleanup)):
                if value is not None and record.get(key) != value:
                    record[key] = value
                    changed = True
            if skip_unchanged and not changed:
                return record
            record["revision"] = int(record.get("revision", 0)) + 1
            record["updated_at"] = time.time()
            _write_atomic(path, record)
            return record
        finally:
            os.close(fd)
            self._invalidate_list_record(duty_id)

    def acknowledge_message_receipt(self, duty_id: str, message: dict,
                                    recipient: dict) -> bool:
        """Attach an exact receiver receipt without reopening a terminal duty."""
        if not isinstance(message, dict) or message.get("state") != "received":
            return False
        ref = message.get("ref")
        body_digest = message.get("body_sha256")
        if not isinstance(ref, str) or not isinstance(body_digest, str):
            return False
        proof = message.get("exact_receiver_receipt")
        binding = proof.get("recipient_binding") if isinstance(proof, dict) else None
        effective = proof.get("effective_recipient") if isinstance(proof, dict) else None
        if (not isinstance(proof, dict)
                or (proof.get("receipt_type"), proof.get("provenance")) not in {
                    ("exact-peer-ref", _MESSAGE_RECEIVER_PROVENANCE),
                    ("exact-peer-ref", _LEGACY_PENDING_RECEIPT_PROVENANCE),
                    ("exact-native-history", "validated-codex-native-history-v1"),
                    ("exact-native-history", "validated-opencode-native-history-v1")}
                or message.get("receipt") != proof.get("receipt_type")
                or proof.get("transfer_ref") != ref
                or proof.get("transfer_body_sha256") != body_digest
                or not isinstance(binding, dict)
                or not isinstance(effective, dict)
                or any(effective.get(key) != recipient.get(key)
                       for key in ("harness", "session_id"))
                or (binding.get("recipient") or {}) != effective):
            return False
        path = self._record_path(duty_id)
        if not path.exists():
            return False

        def mutate():
            fd = self._lock(duty_id)
            try:
                record = _read_record(path)
                if record is None:
                    return False
                intent = record.get("intent") or {}
                observation = record.get("observation") or {}
                expected_sender = intent.get("from") or {}
                saved_sender = message.get("from") or {}
                sender_keys = ("harness", "session_id", "name") if "name" in expected_sender \
                    else ("harness", "session_id")
                if (intent.get("kind") != "message" or intent.get("handoff_acceptance")
                        or not _message_intent_matches_transfer(record, message)
                        or (expected_sender and any(expected_sender.get(key) != saved_sender.get(key)
                                                    for key in sender_keys))
                        or intent.get("producer_binding") != message.get("producer_binding")):
                    return False
                if proof.get("receipt_type") == "exact-native-history":
                    native_id = proof.get("actual_message_id")
                    if (not isinstance(native_id, str) or not native_id
                            or message.get("actual_message_id") != native_id):
                        return False
                original = intent.get("to") or {}
                saved_target = message.get("to") or {}
                target_keys = ("harness", "session_id", "pane_id")
                if any(original.get(key) != saved_target.get(key) for key in target_keys):
                    return False
                watch_ids = _message_watch_ids(message.get("refs", []))
                if watch_ids is None:
                    return False
                if not watch_ids:
                    if original.get("harness") != recipient.get("harness"):
                        return False
                    pane_bound = original.get("session_id") is None
                    if pane_bound:
                        if (not original.get("pane_id")
                                or recipient.get("pane_id") != original.get("pane_id")
                                or not isinstance(recipient.get("session_id"), str)
                                or not recipient.get("session_id")):
                            return False
                    elif original.get("session_id") != recipient.get("session_id"):
                        return False
                elif (not isinstance(binding.get("watches"), list)
                        or [watch.get("watch_id") for watch in binding["watches"]
                            if isinstance(watch, dict)] != watch_ids):
                    return False
                received = {
                    "ref": ref,
                    "body_digest": body_digest,
                    "recipient": {key: recipient.get(key) for key in target_keys},
                    "receipt": message.get("receipt"),
                    "provenance": proof.get("provenance"),
                    "recipient_binding": copy.deepcopy(binding),
                    "received_at": message.get("received_at"),
                }
                if proof.get("notice_body_sha256") is not None:
                    received["notice_body_sha256"] = proof["notice_body_sha256"]
                updated_observation = dict(observation)
                if updated_observation.get("receiver_receipt") == received \
                        and record.get("delivery") == "acknowledged":
                    return True
                updated_observation["receiver_receipt"] = received
                record["observation"] = updated_observation
                record["delivery"] = "acknowledged"
                if record.get("state") not in {"complete", "cancelled"}:
                    if record.get("result") not in {None, "received"}:
                        return False
                    record["state"] = "complete"
                    record["result"] = "received"
                    record["cleanup"] = "complete"
                record["revision"] = int(record.get("revision", 0)) + 1
                record["updated_at"] = time.time()
                _write_atomic(path, record)
                return True
            finally:
                os.close(fd)
                self._invalidate_list_record(duty_id)

        return _with_current_message_watch_binding(message, binding, mutate, False)

    def settle_message_transport(self, duty_id: str, message: dict, binding: dict) -> bool:
        """Close transport only after the exact current watch holder durably ACKed."""
        if (not isinstance(message, dict) or message.get("state") != "received"
                or message.get("receipt") != "watch-receipt-displayed"):
            return False
        ref, body_digest = message.get("ref"), message.get("body_sha256")
        if not isinstance(ref, str) or not isinstance(body_digest, str):
            return False
        refs = message.get("refs")
        watch_ids = _message_watch_ids(refs)
        watches = binding.get("watches") if isinstance(binding, dict) else None
        recipient = binding.get("recipient") if isinstance(binding, dict) else None
        if (not watch_ids or not isinstance(watches, list) or not isinstance(recipient, dict)
                or [watch.get("watch_id") for watch in watches if isinstance(watch, dict)] != watch_ids):
            return False
        saved_binding = message.get("watch_receipt_binding")
        if saved_binding is not None and saved_binding != binding:
            return False
        path = self._record_path(duty_id)
        if not path.exists():
            return False

        def mutate():
            try:
                import dispatch_seat_handover as handover
                if not handover.watch_acknowledgements_valid(binding):
                    return False
            except (ImportError, OSError, ValueError, RuntimeError):
                return False
            fd = self._lock(duty_id)
            try:
                record = _read_record(path)
                if record is None:
                    return False
                intent = record.get("intent") or {}
                observation = record.get("observation") or {}
                if (intent.get("kind") != "message" or intent.get("handoff_acceptance")
                        or not _message_intent_matches_transfer(record, message)):
                    return False
                expected_sender, saved_sender = intent.get("from") or {}, message.get("from") or {}
                sender_keys = ("harness", "session_id", "name") if "name" in expected_sender \
                    else ("harness", "session_id")
                if expected_sender and any(expected_sender.get(key) != saved_sender.get(key)
                                           for key in sender_keys):
                    return False
                if intent.get("producer_binding") != message.get("producer_binding"):
                    return False
                original, saved_target = intent.get("to") or {}, message.get("to") or {}
                if any(original.get(key) != saved_target.get(key)
                       for key in ("harness", "session_id", "pane_id")):
                    return False
                if (not isinstance(recipient.get("harness"), str)
                        or not isinstance(recipient.get("session_id"), str)
                        or not recipient.get("session_id")):
                    return False
                if record.get("delivery") == "acknowledged":
                    return record.get("state") in {"complete", "cancelled"}
                receipt = {
                    "ref": ref,
                    "recipient": dict(recipient),
                    "receipt": "watch-receipt-displayed",
                    "recipient_binding": copy.deepcopy(binding),
                    "received_at": message.get("received_at"),
                }
                updated_observation = dict(observation)
                if (updated_observation.get("watch_transport_receipt") == receipt
                        and record.get("delivery") == "not-required"):
                    return True
                updated_observation["watch_transport_receipt"] = receipt
                record["observation"] = updated_observation
                record["delivery"] = "not-required"
                record["cleanup"] = "complete"
                if record.get("state") not in {"complete", "cancelled"}:
                    record["state"] = "complete"
                record["revision"] = int(record.get("revision", 0)) + 1
                record["updated_at"] = time.time()
                _write_atomic(path, record)
                return True
            finally:
                os.close(fd)
                self._invalidate_list_record(duty_id)

        return _with_current_message_watch_binding(message, binding, mutate, False)

    def cancel_message_delivery(self, duty_id: str, *, expected_revision: int,
                                ref: str, body_digest: str, identity: dict,
                                message: dict, evidence: dict) -> dict | None:
        """Close only the exact still-current message delivery proven impossible."""
        path = self._record_path(duty_id)

        def mutate():
            fd = self._lock(duty_id)
            try:
                record = _read_record(path)
                if record is None:
                    return None
                intent = record.get("intent") or {}
                observation = record.get("observation") or {}
                if (record.get("state") in {"complete", "cancelled"}
                        or record.get("revision") != expected_revision
                        or intent.get("kind") != "message"
                        or intent.get("handoff_acceptance")
                        or ref != (message or {}).get("ref")
                        or body_digest != (message or {}).get("body_sha256")
                        or not _message_intent_matches_transfer(record, message)
                        or intent.get("identity") != identity):
                    return record
                if (observation.get("native_receiving_turn")
                        or observation.get("continuation_prepared")
                        or record.get("delivery") == "acknowledged"):
                    return record
                original = intent.get("to") or {}
                saved_target = (message or {}).get("to") or {}
                if any(original.get(key) != saved_target.get(key)
                       for key in ("harness", "session_id", "pane_id")):
                    return record
                if evidence.get("server") != (identity.get("server") or "default"):
                    return record
                if evidence.get("kind") not in {"recipient-session-absent", "first-input-process-dead"}:
                    return record
                endpoint = identity.get("server") or "default"
                recipient_binding = evidence.get("recipient_binding")
                effective = ((recipient_binding or {}).get("recipient")
                             if isinstance(recipient_binding, dict) else None)
                watch_ids = _message_watch_ids((message or {}).get("refs", []))
                if watch_ids is None:
                    return record
                if watch_ids:
                    if (not isinstance(recipient_binding, dict)
                            or [watch.get("watch_id") for watch in recipient_binding.get("watches", [])
                                if isinstance(watch, dict)] != watch_ids):
                        return record
                else:
                    effective = (message or {}).get("bound_to") or original
                if not isinstance(effective, dict):
                    return record
                if evidence.get("kind") == "recipient-session-absent":
                    if (evidence.get("census_complete") is not True
                            or evidence.get("session_absent") is not True
                            or evidence.get("census_endpoint") != endpoint
                            or evidence.get("session_id") != effective.get("session_id")
                            or evidence.get("harness") != effective.get("harness")):
                        return record
                else:
                    proof = (message or {}).get("first_input_process")
                    if (evidence.get("process_disposition") != "dead"
                            or evidence.get("process") != proof):
                        return record
                    if (effective.get("session_id") is not None
                            and (evidence.get("census_complete") is not True
                                 or evidence.get("session_absent") is not True
                                 or evidence.get("census_endpoint") != endpoint
                                 or evidence.get("recipient_session_id") != effective.get("session_id"))):
                        return record
                updated_observation = dict(observation)
                updated_observation.update({
                    "ref": ref,
                    "reason": evidence["kind"],
                    "terminal_evidence": dict(evidence),
                })
                record["state"] = "cancelled"
                record["delivery"] = "failed"
                record["cleanup"] = "complete"
                record["observation"] = updated_observation
                record["revision"] = int(record.get("revision", 0)) + 1
                record["updated_at"] = time.time()
                _write_atomic(path, record)
                return record
            finally:
                os.close(fd)
                self._invalidate_list_record(duty_id)

        binding = evidence.get("recipient_binding") if isinstance(evidence, dict) else None
        return _with_current_message_watch_binding(message, binding, mutate, None)

    def claim_phase(self, duty_id: str, expected: set[str], new_phase: str,
                    *, state: str = "pending", extra: dict | None = None) -> dict | None:
        """Publish one side-effect phase exactly once under the duty lock."""
        if state not in _PENDING_STATES | {"complete", "cancelled"}:
            raise ObligationError("peer-obligation-state-invalid")
        path = self._record_path(duty_id)
        fd = self._lock(duty_id)
        try:
            record = _read_record(path)
            if record is None:
                return None
            if record.get("state") in {"complete", "cancelled"}:
                return None
            observation = record.get("observation") or {}
            if observation.get("phase", "waiting") not in expected:
                return None
            observation = {**observation, **(extra or {}), "phase": new_phase}
            record["observation"] = observation
            record["state"] = state
            record["revision"] = int(record.get("revision", 0)) + 1
            record["updated_at"] = time.time()
            _write_atomic(path, record)
            return record
        finally:
            os.close(fd)
            self._invalidate_list_record(duty_id)


def stable_duty_id(kind: str, identity: dict, discriminator: str = "") -> str:
    if kind not in _DUTY_KINDS:
        raise ObligationError("peer-obligation-kind-invalid")
    raw = json.dumps([kind, identity, discriminator], sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return kind + "-" + hashlib.sha256(raw).hexdigest()[:32]


def _handoff_legacy_runner(store: ObligationStore, lock_path: Path, *,
                          reconciliation_required=False, service_current=None):
    """Replace only an exact observer whose old code cannot process this duty.

    Accepted work stays in the existing store; the same flock fences its next
    observer. A pidfd prevents a recycled PID from ever receiving the signal.
    """
    has_batch = any(d.get("intent", {}).get("kind") == "registered-batch" for d in store.list())
    if not has_batch and not reconciliation_required:
        return False
    if not service_current:
        return False
    state_root = str(store.root.parents[1])
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        pidfd = None
        try:
            pidfd = os.pidfd_open(int(proc.name))
            argv = (proc / "cmdline").read_bytes().decode().rstrip("\0").split("\0")
            if (len(argv) < 3 or Path(argv[1]).name != "peer-steward.py"
                    or argv[2] != "__obligation-runner"
                    or argv[argv.index("--state-root") + 1] != state_root):
                continue
            lock_fd = argv[argv.index("--lock-fd") + 1]
            if (proc / "fd" / lock_fd).resolve() != lock_path.resolve():
                continue
            result = replace_managed_observer(
                int(proc.name), _proc_start_identity(int(proc.name)) or "", lock_path,
                "utilities/peer-steward.py",
                ("__obligation-runner", "--lock-fd", lock_fd,
                 "--state-root", state_root),
                service_current, lock_fd_option="--lock-fd",
            )
            if result in {"current", "replaced"}:
                return result
        except (OSError, ValueError, IndexError, UnicodeError):
            continue
        finally:
            if pidfd is not None:
                os.close(pidfd)
    return False


def _ensure_runner(root, *, service_current=None, use_remembered=False,
                   remember_binding=False, reconciliation_required=False):
    store = ObligationStore(root)
    duties = store.list()
    lock_path = store.root / RUNNER_LOCK_NAME
    if not duties and not remember_binding and not reconciliation_required:
        if not use_remembered or not lock_path.exists():
            return False
    store.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise ObligationError("peer-obligation-runner-lock-symlink")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handoff = _handoff_legacy_runner(
                store, lock_path, reconciliation_required=reconciliation_required,
                service_current=service_current)
            if handoff == "current":
                return True
            if handoff != "replaced":
                return not reconciliation_required
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(0.02)
        code_root = _code_root()
        binding = validate_service_binding(service_current, root=code_root)
        if use_remembered:
            binding = _remembered_service_binding(fd, root=code_root)
        elif remember_binding and binding:
            payload = _runner_control_payload(binding, root=code_root)
            if payload is None:
                binding = None
            else:
                _write_runner_control(fd, payload)
        if not duties and not reconciliation_required:
            return False
        os.set_inheritable(fd, True)
        active_root = active_service_root(binding, root=code_root) if binding else None
        runner_root = active_root or code_root
        runner = runner_root / "utilities" / "peer-steward.py"
        state_root = Path(root).resolve() if root else peer_state_root()
        child_env = (_service_exec_environment(code_root, runner_root) if binding else None)
        argv = [sys.executable, str(runner), "__obligation-runner", "--lock-fd", str(fd),
                "--state-root", str(state_root)]
        if binding:
            argv.extend(("--service-current", binding))
        _RUNNERS[:] = [process for process in _RUNNERS if process.poll() is None]
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, pass_fds=(fd,), start_new_session=True, env=child_env,
        )
        _RUNNERS.append(process)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        os.close(fd)


def ensure_runner(root: str | Path | None = None, *,
                  service_current: str | Path | None = None,
                  reconciliation_required=False) -> bool:
    """Start a fixed-root runner unless an explicit internal binding is supplied."""
    return _ensure_runner(root, service_current=service_current,
                          reconciliation_required=reconciliation_required)


def ensure_normal_runner(root: str | Path | None = None, *, lifecycle_root=None,
                         reconciliation_required=False) -> bool:
    """Ensure a producer runner using only verified lifecycle provenance.

    Lifecycle callbacks may establish the binding while holding the existing
    runner lock, including when no duty currently needs a process. Ordinary
    producers reuse only a validated binding already in that lock's payload.
    """
    if lifecycle_root is not None:
        binding = managed_service_binding(lifecycle_root)
        return _ensure_runner(root, service_current=binding,
                              remember_binding=binding is not None,
                              reconciliation_required=reconciliation_required)
    return _ensure_runner(root, use_remembered=True,
                          reconciliation_required=reconciliation_required)


def request_dispatch_reconciliation(state_root: str | Path) -> bool:
    """Request the existing runner through detached recovery for a carrier.

    Compatibility roots remain read-only. Worker and subsession carriers use
    this same entry even when they have no session-projection hook.
    """
    from dispatch_contract import dispatch_state_roots, resolve_agent_home
    home = resolve_agent_home()
    roots = dispatch_state_roots(home)
    root = Path(state_root).resolve(strict=False)
    if not roots or root != Path(roots[0]).resolve(strict=False):
        return False
    if not (root / "jobs.log").is_file():
        return False
    # Dispatch storage and peer storage may be separate roots. Preserve the
    # common detached request rather than creating another store under jobs or
    # running runner setup on the carrier's activation path.
    return request_obligation_recovery(lifecycle_root=home)


@contextmanager
def legacy_runner_lock(store: ObligationStore):
    """Serialize retire side effects with a runner from before input separation."""
    path = store.root / "runner.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def pane_readiness(
    *,
    server: str,
    pane: str,
    harness: str,
    session_id: str,
    pid_birth: str,
    identity_verified: bool,
    native_turn: str,
    form_state: str = "unknown",
    draft_state: str = "unknown",
    bound_work: tuple[RegisteredWorkObservation, ...] = (),
    bindings_state: str = "observed",
    provenance: tuple[tuple[str, str], ...] = (),
    purpose: str = "completion",
):
    observation = PaneObservation(
        server=server, pane=pane, harness=harness, session_id=session_id,
        pid_birth=pid_birth, identity_verified=identity_verified,
        native_turn=native_turn, form_state=form_state, draft_state=draft_state,
        bound_work=bound_work, bindings_state=bindings_state, provenance=provenance,
    )
    return completion_readiness(observation, purpose=purpose)


def execution_attempts_for_processes(processes: Iterable[dict]) -> tuple[str, ...]:
    """Read only the exact foreground processes' existing dispatch tags."""
    from dispatch_contract import ATTEMPT_DESCENDANT_ENV, _runtime_ancestry_proc_stat

    attempts = set()
    key = ATTEMPT_DESCENDANT_ENV.encode() + b"="
    for process in processes:
        try:
            pid = int(process["pid"])
            before = _runtime_ancestry_proc_stat(pid)
            if before is None:
                continue
            try:
                raw = (Path("/proc") / str(pid) / "environ").read_bytes()
            except OSError as exc:
                if before == _runtime_ancestry_proc_stat(pid):
                    raise ObligationError("pane-execution-unavailable") from exc
                continue
            if before != _runtime_ancestry_proc_stat(pid):
                continue
            for item in raw.split(b"\0"):
                if item.startswith(key):
                    attempt = item[len(key):].decode("utf-8")
                    if _SAFE_ID.fullmatch(attempt):
                        attempts.add(attempt)
        except ObligationError:
            raise
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            continue
    return tuple(sorted(attempts))


def bound_work_for_pane(pane: str, harness: str, session_id: str, *,
                        jobs: str | Path | None = None,
                        execution_attempt_ids: tuple[str, ...] = (),
                        include_parent: bool = True,
                        read_context=None,
                        registry_append_only: bool = False,
                        authority_out: dict | None = None,
                        selected_rows: list[dict] | tuple[dict, ...] | None = None,
                        expected_handover_keys: list | tuple | None = None,
                        ) -> tuple[tuple[RegisteredWorkObservation, ...], str]:
    """Read exact executor, registered-parent or handed-over attempt bindings.

    Input readiness considers executor bindings only, so it passes
    ``include_parent=False`` and skips observing every parented attempt.
    """
    from dispatch_contract import (
        observed_attempt_liveness,
        parse_registry_metadata,
        resolve_agent_home,
        resolve_dispatch_state_root,
    )
    from dispatch_terminal_commit import owner_completion_state
    from dispatch_seat_handover import owns as seat_owns, effective_parent_harness
    from dispatch_registry_cache import registry_view

    if jobs is None:
        state_root = resolve_dispatch_state_root(resolve_agent_home())
        jobs_path = state_root / "jobs.log"
    else:
        jobs_path = Path(jobs).expanduser().resolve()
        state_root = jobs_path.parent
    if selected_rows is None:
        try:
            context_view = getattr(read_context, "registry_view", None)
            view = (context_view(jobs_path, append_only=registry_append_only)
                    if callable(context_view)
                    else registry_view(jobs_path, append_only=registry_append_only))
            if view is not None:
                view.require_complete_text()
        except FileNotFoundError:
            view = None
        except (OSError, UnicodeError):
            return (), "unknown"
        if view is None:
            return (), "observed"
        if view.malformed_rows:
            return (), "unknown"
    else:
        view = None
        if not isinstance(selected_rows, (list, tuple)):
            return (), "unknown"
        normalized_rows = []
        for row in selected_rows:
            if (not isinstance(row, dict)
                    or not isinstance(row.get("status"), str)
                    or not isinstance(row.get("metadata"), dict)
                    or not isinstance(row.get("completion_metadata"), dict)
                    or not isinstance(row.get("fields"), (list, tuple))):
                return (), "unknown"
            normalized_rows.append({
                "status": row["status"],
                "metadata": row["metadata"],
                "completion_metadata": row["completion_metadata"],
                "fields": list(row["fields"]),
            })
        try:
            expected_keys = ([] if expected_handover_keys is None else
                             [list(key) for key in expected_handover_keys])
        except (TypeError, ValueError):
            return (), "unknown"
        import shared_readiness
        if not shared_readiness.valid_watch_authority(
                {"rows": normalized_rows, "handover_keys": expected_keys}):
            return (), "unknown"
        selected_rows = normalized_rows

    if read_context is None:
        from peer_read_context import PeerReadContext
        read_context = PeerReadContext(registry_append_only=registry_append_only)
    candidates = {}
    if selected_rows is None:
        for attempt_id in execution_attempt_ids:
            for record in view.completion_attempt_records(attempt_id):
                candidates[record.order] = (record.status, record)
    else:
        for order, row in enumerate(selected_rows):
            candidates[order] = (row["status"], {
                "metadata": row["metadata"],
                "completion_metadata": row["completion_metadata"],
                "fields": row["fields"],
            })
    binding_keys = ()
    if include_parent:
        try:
            from dispatch_seat_handover import _handover_binding_keys_for_session
            binding_keys = _handover_binding_keys_for_session(
                jobs_path, session_id, read_context=read_context)
        except (OSError, ValueError, TypeError):
            return (), "unknown"
        if authority_out is not None:
            authority_out["handover_keys"] = [list(key) for key in binding_keys]
        if expected_handover_keys is not None:
            try:
                expected_keys = tuple(sorted(tuple(key) for key in expected_handover_keys))
            except (TypeError, ValueError):
                return (), "unknown"
            if expected_keys != tuple(sorted(binding_keys)):
                return (), "unknown"
    if include_parent and selected_rows is None:
        for record in view.completion_parent_records(session_id):
            candidates[record.order] = (record.status, record)
        try:
            for _status, record in view.handover_candidates(session_id, binding_keys):
                candidates[record.order] = (record.status, record)
        except (OSError, ValueError, TypeError):
            # The current registered-parent rows remain visible; a failed
            # handover observation cannot grant inherited ownership.
            pass

    if view is not None and selected_rows is None and include_parent:
        for status, record in tuple(candidates.values()):
            metadata = (record.completion_metadata() if not isinstance(record, dict)
                        else record["metadata"])
            if (metadata.get("workflow_completion") != "runtime-v1"
                    or metadata.get("worker_type") != "owner"):
                continue
            attempt_id = metadata.get("attempt_id", "")
            route_id = metadata.get("owner_route_id") or metadata.get("route_id") or ""
            route_hash = metadata.get("owner_route_hash") or metadata.get("route_hash") or ""
            if attempt_id and route_id and route_hash:
                for related in view.completion_related(attempt_id, route_id, route_hash):
                    candidates[related.order] = (related.status, related)

    selected_registry_rows = []
    authority_rows = []
    authority_complete = authority_out is not None
    authority_size = None
    if authority_complete:
        import shared_readiness
        authority_size = shared_readiness.bounded_json_size(
            {"handover_keys": authority_out.get("handover_keys", []), "rows": []},
            shared_readiness.MAX_WATCH_AUTHORITY_BYTES)
        authority_complete = authority_size is not None
    for status, record in (candidates[key] for key in sorted(candidates)):
        if isinstance(record, dict):
            metadata = dict(record["metadata"])
            fields = list(record["fields"])
            completion_metadata = dict(record["completion_metadata"])
        else:
            fields = list(record.fields)
            metadata = parse_registry_metadata(record.fields[5])
            completion_metadata = record.completion_metadata()
        selected_registry_rows.append((fields, completion_metadata))
        if authority_complete:
            row = {"status": status, "metadata": metadata,
                   "completion_metadata": completion_metadata, "fields": fields}
            separator_size = 1 if authority_rows else 0
            row_size = shared_readiness.bounded_json_size(
                row, shared_readiness.MAX_WATCH_AUTHORITY_BYTES - authority_size - separator_size)
            if row_size is None:
                # Do not publish a valid-looking prefix of the selected rows.
                authority_complete = False
                authority_rows.clear()
            else:
                authority_rows.append(row)
                authority_size += separator_size + row_size
    if authority_out is not None:
        authority_out["rows"] = authority_rows if authority_complete else None

    result: list[RegisteredWorkObservation] = []
    for status, record in (candidates[key] for key in sorted(candidates)):
        if isinstance(record, dict):
            metadata = record["metadata"]
        else:
            fields = record.fields
            metadata = parse_registry_metadata(fields[5])
        executor = (metadata.get("attempt_id") in execution_attempt_ids
                    and metadata.get("harness") == harness)
        if include_parent:
            parent_owned = seat_owns(metadata, session_id, jobs_path,
                                     read_context=read_context)
            parent_harness = effective_parent_harness(
                metadata, jobs_path, read_context=read_context)
        else:
            parent_owned = False
            parent_harness = ""
        parent = (include_parent and parent_owned
                  and parent_harness in {None, "", harness})
        if not executor and not parent:
            continue
        attempt_id = metadata.get("attempt_id", "")
        if not attempt_id:
            return (), "unknown"
        live = observed_attempt_liveness(status, metadata)
        decision = decide_attempt(
            status, metadata, process_state=live.process_state,
            process_reason=live.process_reason,
            terminal_observed=live.state == "reconcile-needed",
        )
        if parent:
            # Parent watch/retire readiness is derived from the exact result
            # delivery below; owner closure is a separate completion controller.
            closure_state = "pending"
        else:
            try:
                closure = owner_completion_state(
                    jobs_path, status, metadata,
                    selected_registry_rows=selected_registry_rows).state
            except Exception:
                closure = "unknown"
            closure_state = ("settled" if closure in {"not-applicable", "complete"}
                             else "unknown" if closure == "unknown" else "pending")
        delivery_state = "unknown"
        if parent and status == "done":
            if metadata.get("delivery_intent") != "1":
                delivery_state = "not-required"
            else:
                delivery_id = metadata.get("delivery_id")
                expected_digest = metadata.get("delivery_receipt_digest")
                expected_revision = metadata.get("delivery_row_revision")
                if delivery_id and expected_digest and expected_revision:
                    try:
                        import dispatch_pending_delivery as pending
                        delivery = pending.read(state_root, metadata.get("parent_sid", ""),
                                                delivery_id)
                        if (delivery and delivery.get("delivery_id") == delivery_id
                                and attempt_id in delivery.get("attempt_ids", [])
                                and delivery.get("recipient_kind") == metadata.get(
                                    "parent_completion_delivery")
                                and delivery.get("receipt_digest") == expected_digest
                                and (delivery.get("row_revisions") or {}).get(attempt_id)
                                    == expected_revision):
                            delivery_state = (delivery.get("state") if delivery.get("state") in {
                                "pending", "claimed", "queued", "unverified", "sent-ambiguous",
                                "acked"} else "unknown")
                    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                        pass
        result.append(RegisteredWorkObservation(
            attempt_id=attempt_id, decision=decision,
            process_state=live.process_state,
            evidence_state=metadata.get("receipt_state", metadata.get("note", "unclassified")),
            owner_completion_state=closure_state,
            cleanup_state=("pending" if metadata.get("cleanup_pending") == "1" else "settled"),
            pane_relation="executor" if executor else "parent",
            delivery_state=delivery_state if parent else "unknown",
            provenance=(("jobs", str(jobs_path)), ("registry_status", status),
                        ("registry_state_root", str(state_root))),
        ))
    return tuple(result), "observed"
