#!/usr/bin/env python3
"""Route interactive Codex CLI surfaces through the managed App Server entry."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile


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


class LauncherError(RuntimeError):
    """Installed launcher state is unsafe or incomplete."""


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
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32_768:
        raise LauncherError(f"managed launcher state is unavailable: {path}")
    info = path.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise LauncherError(f"managed launcher state permissions are unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LauncherError(f"managed launcher state is invalid: {path}") from exc
    if not isinstance(value, dict) or value.get("schema") != 1 or value.get("phase") != "installed":
        raise LauncherError(f"managed launcher state is incomplete: {path}")
    real = Path(str(value.get("real_command", "")))
    if not real.is_absolute() or not real.exists() or not os.access(real, os.X_OK):
        raise LauncherError(f"real Codex command is unavailable: {real}")
    if _is_harness_wrapper(real):
        raise LauncherError(
            f"real Codex command resolves to an hearting launcher wrapper: {real}"
        )
    value["real_command"] = str(real)
    return value


def pinned_runtime(home: Path) -> dict:
    """Resolve one activation root once for the lifetime of a new session."""

    path = home / ".harness" / "activation.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
        raise LauncherError(f"runtime activation state is unavailable: {path}")
    info = path.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise LauncherError(f"runtime activation state permissions are unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LauncherError(f"runtime activation state is invalid: {path}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != 2
        or value.get("runtime") != "codex"
        or value.get("mode") not in {"packaged", "linked"}
    ):
        raise LauncherError(f"runtime activation state is incomplete: {path}")
    active = Path(str(value.get("active_root", "")))
    if not active.is_absolute() or active.is_symlink():
        raise LauncherError("runtime activation root is unsafe")
    try:
        active = active.resolve(strict=True)
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
        bundle_root = (home / ".harness" / "bundles").resolve(strict=False)
        try:
            active.relative_to(bundle_root)
        except ValueError as exc:
            raise LauncherError("packaged runtime root escapes bundle storage") from exc
        metadata = active.parent / "bundle.json"
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
        value = _state(state_home)
        real = Path(value["real_command"])
        # A global launcher may be used with a one-off CODEX_HOME for tests,
        # repair, or an administrative command. Its global binding remains
        # usable, but only a home with its own launcher state may become a
        # managed interactive parent.
        if state_home == runtime_home and should_manage(args) and managed_auth_ready(runtime_home):
            binding = pinned_runtime(runtime_home)
            export_runtime_binding(binding)
            command = managed_command(args, runtime_home, real, binding)
        else:
            command = [str(real), *args]
        os.execv(command[0], command)
    except (LauncherError, OSError) as exc:
        print(f"hearting: Codex launcher failed: {exc}", file=sys.stderr)
        return 69


if __name__ == "__main__":
    raise SystemExit(main())
