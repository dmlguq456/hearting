#!/usr/bin/env python3
"""Route interactive Codex CLI surfaces through the managed App Server entry."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


PASSTHROUGH_COMMANDS = {
    "app",
    "app-server",
    "apply",
    "a",
    "archive",
    "cloud",
    "completion",
    "debug",
    "delete",
    "doctor",
    "e",
    "exec",
    "exec-server",
    "features",
    "help",
    "login",
    "logout",
    "mcp",
    "mcp-server",
    "plugin",
    "remote-control",
    "review",
    "sandbox",
    "unarchive",
    "update",
}
INTERACTIVE_COMMANDS = {"resume", "fork"}
VALUE_OPTIONS = {
    "-a",
    "--add-dir",
    "--ask-for-approval",
    "-c",
    "--cd",
    "--config",
    "-C",
    "--disable",
    "--enable",
    "-i",
    "--image",
    "--local-provider",
    "-m",
    "--model",
    "-p",
    "--profile",
    "--remote",
    "--remote-auth-token-env",
    "-s",
    "--sandbox",
}
PASSTHROUGH_FLAGS = {"-h", "--help", "-V", "--version"}

# Approval/sandbox posture the caller may have selected for itself. Any of these means
# the invocation already carries an explicit stance and the default must not touch it.
# `-p/--profile` is in the set because a profile is a user-authored config layer that may
# pin `approval_policy`/`sandbox_mode`; deferring to it costs a default and never widens
# access, which is the direction to be wrong in.
POSTURE_FLAGS = {
    "-s",
    "--sandbox",
    "-a",
    "--ask-for-approval",
    "--approve-for-me",
    "--dangerously-bypass-approvals-and-sandbox",
    "--yolo",
    "-p",
    "--profile",
}
POSTURE_CONFIG_KEYS = ("approval_policy", "sandbox_mode", "sandbox_permissions")
BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"


class LauncherError(RuntimeError):
    """Installed launcher state is unsafe or incomplete."""


LOCK_NAME = "codex-launcher.lock"
# A nested sandbox may read the installed lock without modifying its directory.
# Lock acquisition failures remain refusals; per-file atomic reads do not replace
# the installer's multi-file transaction lock.
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_lock_descriptor(path: Path) -> int:
    try:
        return os.open(path, _READ_FLAGS)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise LauncherError(f"unsafe Codex launcher lock: {path}") from exc
    create = (
        os.O_RDWR | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(path, create, 0o600)
    except FileExistsError:
        # Join the winner's lock; a failed reopen must not bypass synchronization.
        try:
            return os.open(path, _READ_FLAGS)
        except OSError as exc:
            raise LauncherError(f"unsafe Codex launcher lock: {path}") from exc
    except OSError as exc:
        raise LauncherError(f"Codex launcher lock unavailable: {path}") from exc


def _launcher_lock(home: Path) -> int:
    """Join the installer's lock protocol as a reader, without writing to it.

    `tools/install/codex_launcher.py` holds this lock `LOCK_EX` for the whole
    install transaction, so a reader still has to take it -- otherwise it can
    read a `codex-launcher.json` whose recorded ingress and vendor binary are
    only half in place. But the launcher never mutates that state, so it needs
    the reader half: a shared flock on a descriptor opened read-only.

    Taking it exclusively (`open("a+b")` plus an unconditional `chmod`) is what
    made every managed `codex` invocation exit 69 with `[Errno 30] Read-only
    file system` under a nested sandboxed reviewer, including pass-through
    commands such as `codex exec` that read nothing but the recorded command.

    A missing lock may be created only if the directory permits it. Otherwise
    refuse, as before: an unlocked read cannot preserve the install transaction.
    """
    path = home / ".harness" / LOCK_NAME
    descriptor = _open_lock_descriptor(path)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise LauncherError(f"unsafe Codex launcher lock: {path}")
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _unlock(descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _read_private_file(path: Path, limit: int, unavailable: str, unsafe: str) -> str:
    """Read one owner-private launcher file through a single descriptor.

    Type, size, owner, and mode are all checked with `fstat` on the descriptor
    the bytes come from, so an installer's atomic replace can never pair one
    inode's permissions with another inode's content. `O_NOFOLLOW` rejects a
    symlinked pathname the same way the previous `is_symlink()` check did.
    """
    try:
        descriptor = os.open(path, _READ_FLAGS)
    except OSError as exc:
        raise LauncherError(unavailable) from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise LauncherError(unavailable)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise LauncherError(unsafe)
            payload = handle.read(limit + 1)
    except OSError as exc:
        raise LauncherError(unavailable) from exc
    if len(payload) > limit:
        raise LauncherError(unavailable)
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LauncherError(unavailable) from exc


def _codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    home = Path(raw).expanduser() if raw else Path.home() / ".codex"
    return home.absolute()


def launcher_state_home(runtime_home: Path) -> Path:
    """Resolve the global CLI binding without hijacking a private CODEX_HOME."""

    runtime_state = runtime_home / ".harness" / "codex-launcher.json"
    if runtime_state.is_file() and not runtime_state.is_symlink():
        return runtime_home
    default_home = (Path.home() / ".codex").absolute()
    default_state = default_home / ".harness" / "codex-launcher.json"
    if default_home != runtime_home and default_state.is_file() and not default_state.is_symlink():
        return default_home
    return runtime_home


def _state(home: Path) -> dict:
    if home.is_symlink() or not home.is_dir():
        raise LauncherError(f"managed CODEX_HOME is unsafe: {home}")
    harness_state = home / ".harness"
    if harness_state.is_symlink() or not harness_state.is_dir():
        raise LauncherError(f"managed launcher state directory is unsafe: {harness_state}")
    path = home / ".harness" / "codex-launcher.json"
    raw = _read_private_file(
        path,
        32_768,
        f"managed launcher state is unavailable: {path}",
        f"managed launcher state permissions are unsafe: {path}",
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LauncherError(f"managed launcher state is invalid: {path}") from exc
    if not isinstance(value, dict) or value.get("schema") not in {1, 2} or value.get("phase") != "installed":
        raise LauncherError(f"managed launcher state is incomplete: {path}")
    real = Path(str(value.get("real_command") or value.get("vendor_binding", {}).get("command_path", "")))
    if not real.is_absolute() or not real.exists() or not os.access(real, os.X_OK):
        raise LauncherError(f"real Codex command is unavailable: {real}")
    if _is_harness_wrapper(real):
        raise LauncherError(
            f"real Codex command resolves to an hearting launcher wrapper: {real}"
        )
    value["real_command"] = str(real)
    ingress = Path(str(value.get("ingress_path") or value.get("wrapper_path", "")))
    if not ingress.is_absolute() or ingress.name != "codex":
        raise LauncherError("managed launcher ingress path is invalid")
    try:
        if ingress.resolve(strict=False).parent == home.resolve(strict=False) / ".harness" / "bin":
            pass
    except OSError as exc:
        raise LauncherError("managed launcher ingress path is invalid") from exc
    return value


def pinned_runtime(home: Path) -> dict:
    """Resolve one activation root once for the lifetime of a new session."""

    path = home / ".harness" / "activation.json"
    raw = _read_private_file(
        path,
        2_000_000,
        f"runtime activation state is unavailable: {path}",
        f"runtime activation state permissions are unsafe: {path}",
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LauncherError(f"runtime activation state is invalid: {path}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != 2
        or value.get("runtime") != "codex"
        or value.get("mode") not in {"packaged", "linked"}
    ):
        raise LauncherError(f"runtime activation state is incomplete: {path}")
    declared = Path(str(value.get("active_root", "")))
    if not declared.is_absolute():
        raise LauncherError("runtime activation root is unsafe")
    try:
        active = declared.resolve(strict=True)
        projected = (home / "hearting").resolve(strict=True)
    except OSError as exc:
        raise LauncherError("runtime activation projection is unavailable") from exc
    if projected != active or not (active / "core" / "CORE.md").is_file():
        raise LauncherError("runtime activation projection is inconsistent")
    revision = value.get("active_revision")
    if not isinstance(revision, str) or not revision:
        raise LauncherError("runtime activation revision is missing")
    checksum = value.get("bundle_checksum")
    if value["mode"] == "packaged":
        # A packaged bundle addresses the release by symlink when the activation
        # source was an immutable managed release, so containment and metadata are
        # asserted on the DECLARED bundle path -- what activation wrote -- and the
        # resolved path is only used to prove the tree is really there. Resolving
        # first made a linked bundle read as "escapes bundle storage" and put its
        # `bundle.json` beside the release, which refused every managed codex
        # launch with exit 69.
        bundle_root = (home / ".harness" / "bundles").resolve(strict=False)
        declared_parent = declared.parent
        try:
            declared.parent.parent.resolve(strict=False).relative_to(bundle_root)
        except ValueError as exc:
            raise LauncherError("packaged runtime root escapes bundle storage") from exc
        metadata = declared_parent / "bundle.json"
        try:
            bundle = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LauncherError("packaged runtime metadata is unavailable") from exc
        if (
            not isinstance(checksum, str)
            or not checksum
            or not isinstance(bundle, dict)
            or bundle.get("checksum") != checksum
            or bundle.get("source_revision") != revision
        ):
            raise LauncherError("packaged runtime identity is inconsistent")
    return {
        "active_root": active,
        "mode": value["mode"],
        "revision": revision,
        "identity": f"{value['mode']}:{revision}:{checksum or '-'}",
    }


def export_runtime_binding(binding: dict) -> None:
    os.environ.update(
        {
            "AGENT_HOME": str(binding["active_root"]),
            "AGENT_RUNTIME_ROOT": str(binding["active_root"]),
            "AGENT_RUNTIME_IDENTITY": str(binding["identity"]),
            "AGENT_RUNTIME_ACTIVATION_MODE": str(binding["mode"]),
        }
    )


def _is_harness_wrapper(command: Path) -> bool:
    """A recorded binding must never point at any install's launcher ingress."""
    try:
        if command.is_symlink() or not command.is_file() or command.stat().st_size > 4096:
            return False
        payload = command.read_bytes()
    except OSError:
        return False
    return b"hearting" in payload and b"codex-launcher.py" in payload


def _first_positional(args: list[str]) -> str | None:
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--":
            return args[index + 1] if index + 1 < len(args) else None
        if value in VALUE_OPTIONS:
            index += 2
            continue
        if any(value.startswith(option + "=") for option in VALUE_OPTIONS if option.startswith("--")):
            index += 1
            continue
        if value.startswith("-"):
            index += 1
            continue
        return value
    return None


def should_manage(args: list[str]) -> bool:
    if os.environ.get("AGENT_CODEX_LAUNCHER_BYPASS") == "1":
        return False
    if any(value == "--remote" or value.startswith("--remote=") for value in args):
        return False
    if any(value in {"-h", "--help", "-V", "--version"} for value in args):
        return False
    command = _first_positional(args)
    if command in INTERACTIVE_COMMANDS:
        return True
    if command in PASSTHROUGH_COMMANDS:
        return False
    if command is None and any(value in PASSTHROUGH_FLAGS for value in args):
        return False
    return True


def interactive_permission_mode() -> str:
    """`bypass` (default) or `inherit`, from AGENT_CODEX_INTERACTIVE_PERMISSION_MODE.

    User decision 2026-09-03: a Codex session this harness launches should come up ready
    to work, the same way `peer-steward.py start` already starts a child Codex root with
    the bypass flag and a registered Claude worker starts in `bypassPermissions`. An
    unrecognized value is not a silent third mode — it falls back to the default.
    """
    raw = os.environ.get("AGENT_CODEX_INTERACTIVE_PERMISSION_MODE", "").strip().lower()
    return "inherit" if raw == "inherit" else "bypass"


def selects_own_posture(args: list[str]) -> bool:
    """True when the invocation already states an approval/sandbox stance of its own."""
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--":
            return False
        if value in POSTURE_FLAGS:
            return True
        if any(value.startswith(flag + "=") for flag in POSTURE_FLAGS if flag.startswith("--")):
            return True
        if value in {"-c", "--config"}:
            if index + 1 < len(args) and _is_posture_override(args[index + 1]):
                return True
            index += 2
            continue
        if value.startswith("--config=") and _is_posture_override(value.partition("=")[2]):
            return True
        if value.startswith("-c") and len(value) > 2 and _is_posture_override(value[2:]):
            return True
        if value in VALUE_OPTIONS:
            index += 2
            continue
        index += 1
    return False


def _is_posture_override(override: str) -> bool:
    key = str(override).partition("=")[0].strip()
    return key in POSTURE_CONFIG_KEYS


def apply_interactive_permission_mode(args: list[str]) -> list[str]:
    """Prepend the bypass flag to a managed interactive invocation that wants the default.

    The flag goes in front so it stays a root-level option even when the invocation is
    `resume`/`fork`. `codex-managed-entry.py` forwards it verbatim to the remote TUI for
    a new session; for `resume`/`fork` it relocates this exact flag onto the App Server
    as `approval_policy`/`sandbox_mode` config, because Codex >= 0.154 refuses permission
    overrides on a remote resume ("Permission overrides are not supported when resuming
    a remote task").
    Only this managed interactive path is affected — `codex exec` and every other
    passthrough subcommand never reach here, so the registered dispatch wrapper's
    `approval_policy=never` plus a real sandbox (stage-dispatch SD-125 (5)) is unchanged.
    """
    if interactive_permission_mode() != "bypass" or selects_own_posture(args):
        return list(args)
    return [BYPASS_FLAG, *args]


def managed_auth_ready(home: Path) -> bool:
    """Let the real CLI own first-login and unsafe-auth remediation."""

    auth = home / "auth.json"
    if auth.is_symlink() or not auth.is_file():
        return False
    info = auth.stat()
    return info.st_uid == os.geteuid() and not info.st_mode & 0o077


def private_directory(path: Path) -> Path:
    if path.is_symlink():
        raise LauncherError(f"managed state directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise LauncherError(f"managed state directory is not owner-controlled: {path}")
    os.chmod(path, 0o700)
    return path


def workspace(args: list[str]) -> Path:
    current = Path.cwd()
    index = 0
    selected: str | None = None
    while index < len(args):
        value = args[index]
        if value in {"-C", "--cd"} and index + 1 < len(args):
            selected = args[index + 1]
            index += 2
            continue
        if value.startswith("--cd="):
            selected = value.partition("=")[2]
        index += 1
    if selected is None:
        return current
    candidate = Path(selected).expanduser()
    return (candidate if candidate.is_absolute() else current / candidate).resolve(strict=False)


def managed_command(
    args: list[str], home: Path, real: Path, binding: dict | None = None
) -> list[str]:
    binding = binding or pinned_runtime(home)
    agent_home = Path(binding["active_root"])
    entry = agent_home / "utilities" / "codex-managed-entry.py"
    if not entry.is_file():
        raise LauncherError(f"managed-entry projection is unavailable: {entry}")
    harness_state = private_directory(home / ".harness")
    state_root = private_directory(harness_state / "managed-sessions")
    session = Path(tempfile.mkdtemp(prefix="session-", dir=str(state_root)))
    os.chmod(session, 0o700)
    dispatch_root = private_directory(harness_state / "dispatch")
    jobs = dispatch_root / "jobs.log"
    return [
        sys.executable,
        str(entry),
        "--codex",
        str(real),
        "--codex-home",
        str(home),
        "--state-dir",
        str(session),
        "--workspace",
        str(workspace(args)),
        "--jobs",
        str(jobs),
        "--",
        *args,
    ]


def main() -> int:
    args = sys.argv[1:]
    runtime_home = _codex_home()
    try:
        # execv keeps the PID, so a circular binding (wrapper -> launcher ->
        # wrapper ...) re-enters this process. Spawned children get new PIDs
        # and are unaffected. Fail fast instead of looping forever.
        guard_pid = os.environ.get("AGENT_CODEX_LAUNCHER_GUARD_PID")
        if guard_pid == str(os.getpid()):
            raise LauncherError(
                "launcher re-entered itself; the recorded real Codex command is circular"
            )
        os.environ["AGENT_CODEX_LAUNCHER_GUARD_PID"] = str(os.getpid())
        state_home = launcher_state_home(runtime_home)
        lock = _launcher_lock(state_home)
        try:
            value = _state(state_home)
        finally:
            _unlock(lock)
        real = Path(value["real_command"])
        # A global launcher may be used with a one-off CODEX_HOME for tests,
        # repair, or an administrative command. Its global binding remains
        # usable, but only a home with its own launcher state may become a
        # managed interactive parent.
        if state_home == runtime_home and should_manage(args) and managed_auth_ready(runtime_home):
            binding = pinned_runtime(runtime_home)
            export_runtime_binding(binding)
            command = managed_command(
                apply_interactive_permission_mode(args), runtime_home, real, binding
            )
        else:
            command = [str(real), *args]
        os.execv(command[0], command)
    except (LauncherError, OSError) as exc:
        print(f"hearting: Codex launcher failed: {exc}", file=sys.stderr)
        return 69


if __name__ == "__main__":
    raise SystemExit(main())
