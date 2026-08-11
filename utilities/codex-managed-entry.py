#!/usr/bin/env python3
"""Launch an opt-in Codex App Server, single-ingress gateway, and remote client."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "utilities" / "codex-managed-gateway.py"
FEATURE = "default_mode_request_user_input"


class EntryError(RuntimeError):
    """The isolated managed-entry boundary is unsafe or unavailable."""


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def safe_directory(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise EntryError(f"{label}-path-unsafe")
    try:
        info = path.stat()
    except OSError as exc:
        raise EntryError(f"{label}-unavailable") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise EntryError(f"{label}-not-directory")
    return path.resolve()


def safe_private_directory(path: Path, label: str) -> Path:
    resolved = safe_directory(path, label)
    info = resolved.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise EntryError(f"{label}-permissions-unsafe")
    return resolved


def safe_registry(path: Path) -> Path:
    """Create or validate one owner-private exact registry file."""

    if not path.is_absolute() or path.is_symlink():
        raise EntryError("jobs-path-unsafe")
    parent = safe_directory(path.parent, "jobs-parent")
    candidate = parent / path.name
    existed = candidate.exists()
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags, 0o600)
    except OSError as exc:
        raise EntryError("jobs-unavailable") from exc
    try:
        if not existed:
            os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise EntryError("jobs-permissions-unsafe")
    finally:
        os.close(descriptor)
    return candidate.resolve(strict=True)


def _feature_enabled(output: str) -> bool:
    """Accept only an exact feature row whose effective value is true."""
    row = re.compile(
        rf"^\s*{re.escape(FEATURE)}\s+.+\s+(true|false)\s*$",
        re.IGNORECASE,
    )
    for line in output.splitlines():
        match = row.fullmatch(line)
        if match is not None:
            return match.group(1).lower() == "true"
    return False


def feature_capability(
    codex: str, workspace: Path, environment: dict[str, str]
) -> str:
    try:
        result = subprocess.run(
            [codex, "features", "list", "--enable", FEATURE],
            cwd=workspace,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unsupported"
    if result.returncode == 0 and _feature_enabled(result.stdout):
        return "enabled"
    return "unsupported"


def _config_disables_feature(value: str) -> bool:
    compact = "".join(value.split()).lower()
    return compact == f"features.{FEATURE}=false"


def explicit_feature_disable(args: list[str]) -> bool:
    for index, value in enumerate(args):
        if value == "--disable":
            if index + 1 < len(args) and args[index + 1] == FEATURE:
                return True
        elif value == f"--disable={FEATURE}":
            return True
        if value in {"-c", "--config"} and index + 1 < len(args):
            config_value = args[index + 1]
        elif value.startswith("-c") and value != "-c":
            config_value = value[2:]
        elif value.startswith("--config="):
            config_value = value.partition("=")[2]
        else:
            config_value = ""
        if config_value and _config_disables_feature(config_value):
            return True
    return False


def app_server_feature_args(feature_status: str) -> list[str]:
    if feature_status == "enabled":
        return ["--enable", FEATURE]
    if feature_status == "user-disabled":
        # The user's flag belongs to the remote TUI argv. Mirror that explicit
        # intent into the separate App Server process so persisted config
        # cannot turn the feature back on behind the user's back.
        return ["--disable", FEATURE]
    return []


def tui_feature_args(feature_status: str) -> list[str]:
    if feature_status == "enabled":
        return ["--enable", FEATURE]
    return []


def check_runtime(codex: str, workspace: Path, environment: dict[str, str]) -> str:
    """Verify the two runtime surfaces needed by managed entry without login I/O."""

    commands = (
        ([codex, "app-server", "--help"], "--listen", "app-server-unavailable"),
        ([codex, "--help"], "--remote", "remote-tui-unavailable"),
    )
    for command, marker, reason in commands:
        try:
            result = subprocess.run(
                command,
                cwd=workspace,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EntryError(reason) from exc
        if result.returncode != 0 or marker not in result.stdout:
            raise EntryError(reason)
    return feature_capability(codex, workspace, environment)


def wait_socket(path: Path, process: subprocess.Popen[Any], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise EntryError(f"process-exited-before-socket:{process.returncode}")
        try:
            if stat.S_ISSOCK(path.lstat().st_mode):
                return
        except FileNotFoundError:
            pass
        time.sleep(0.02)
    raise EntryError(f"socket-start-timeout:{path.name}")


def terminate(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=5)


def cleanup_socket(path: Path) -> None:
    """Remove only an exact leftover socket inside the explicit state dir."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISSOCK(info.st_mode):
        path.unlink()


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--codex", default="codex")
    value.add_argument("--codex-home", required=True, type=Path)
    value.add_argument("--state-dir", required=True, type=Path)
    value.add_argument("--workspace", required=True, type=Path)
    value.add_argument(
        "--jobs",
        type=Path,
        help="exact registry path; defaults to <state-dir>/jobs.log",
    )
    value.add_argument(
        "--check",
        action="store_true",
        help="validate the isolated entry contract without starting a TUI",
    )
    value.add_argument(
        "--client-command",
        help=(
            "proof-only command; token {remote} is replaced by the gateway URI. "
            "Default launches the real Codex remote TUI"
        ),
    )
    value.add_argument(
        "--gateway-fault",
        choices=("none", "before-send", "after-send"),
        default="none",
    )
    value.add_argument("client_args", nargs=argparse.REMAINDER)
    return value


def execute(args: argparse.Namespace) -> int:
    codex_home = safe_private_directory(args.codex_home, "codex-home")
    state_dir = safe_private_directory(args.state_dir, "state-dir")
    workspace = safe_directory(args.workspace, "workspace")
    auth = codex_home / "auth.json"
    if not auth.is_file() or auth.is_symlink():
        raise EntryError("codex-home-auth-missing")
    auth_info = auth.stat()
    if auth_info.st_uid != os.geteuid() or auth_info.st_mode & 0o077:
        raise EntryError("codex-home-auth-permissions-unsafe")
    jobs = safe_registry(args.jobs or (state_dir / "jobs.log"))
    upstream = state_dir / "app-server.sock"
    front = state_dir / "managed-tui.sock"
    control = state_dir / "managed-control.sock"
    ledger = state_dir / "managed-deliveries.json"
    trace = state_dir / "managed-gateway.trace.jsonl"
    for path in (upstream, front, control):
        if path.exists() or path.is_symlink():
            raise EntryError(f"managed-socket-already-exists:{path.name}")
    environment = dict(os.environ)
    environment.update(
        {
            "CODEX_HOME": str(codex_home),
            "CODEX_SQLITE_HOME": str(codex_home),
            "AGENT_HOME": str(ROOT),
            "AGENT_RUNTIME_ROOT": str(ROOT),
            "AGENT_RUNTIME_IDENTITY": os.environ.get(
                "AGENT_RUNTIME_IDENTITY", f"direct:{ROOT.name}"
            ),
            "AGENT_RUNTIME_ACTIVATION_MODE": os.environ.get(
                "AGENT_RUNTIME_ACTIVATION_MODE", "direct"
            ),
            "AGENT_CODEX_MANAGED_GATEWAY": "1",
            "AGENT_CODEX_MANAGED_PARENT_RUNTIME": "codex",
            "AGENT_CODEX_MANAGED_CONTROL_SOCKET": str(control),
            "AGENT_DISPATCH_JOBS": str(jobs),
        }
    )
    capability = check_runtime(args.codex, workspace, environment)
    user_disabled = explicit_feature_disable(list(args.client_args))
    feature_status = "user-disabled" if user_disabled else capability
    if args.check:
        print(
            canonical(
                {
                    "schema_version": 1,
                    "status": "ready",
                    "runtime": "codex-managed-entry",
                    "jobs": str(jobs),
                    "feature_default_mode_request_user_input": feature_status,
                }
            )
        )
        return 0
    app_server: subprocess.Popen[Any] | None = None
    gateway: subprocess.Popen[Any] | None = None
    try:
        app_server = subprocess.Popen(
            [
                args.codex,
                "app-server",
                "--listen",
                f"unix://{upstream}",
                *app_server_feature_args(feature_status),
            ],
            cwd=workspace,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=None,
            start_new_session=True,
        )
        wait_socket(upstream, app_server, 20)
        gateway_command = [
            sys.executable,
            str(GATEWAY),
            "--listen",
            str(front),
            "--upstream",
            str(upstream),
            "--control",
            str(control),
            "--ledger",
            str(ledger),
            "--trace",
            str(trace),
        ]
        if args.gateway_fault != "none":
            gateway_command += ["--fault", args.gateway_fault]
        gateway = subprocess.Popen(
            gateway_command,
            cwd=workspace,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=None,
            start_new_session=True,
        )
        wait_socket(front, gateway, 20)
        wait_socket(control, gateway, 20)
        remote = f"unix://{front}"
        if args.client_command:
            client = [
                token.replace("{remote}", remote)
                for token in shlex.split(args.client_command)
            ]
        else:
            trailing = list(args.client_args)
            if trailing[:1] == ["--"]:
                trailing = trailing[1:]
            client = [
                args.codex,
                "--remote",
                remote,
                *tui_feature_args(feature_status),
                *trailing,
            ]
        if feature_status == "unsupported":
            print(
                canonical(
                    {
                        "status": "warning",
                        "feature": FEATURE,
                        "reason": "unsupported",
                    }
                ),
                file=sys.stderr,
            )
        result = subprocess.run(
            client,
            cwd=workspace,
            env=environment,
            check=False,
        )
        return result.returncode
    finally:
        terminate(gateway)
        terminate(app_server)
        for path in (front, control, upstream):
            cleanup_socket(path)


def main() -> int:
    args = parser().parse_args()
    try:
        return execute(args)
    except (EntryError, OSError) as exc:
        print(
            canonical({"status": "error", "reason": str(exc)}),
            file=sys.stderr,
        )
        return 65


if __name__ == "__main__":
    raise SystemExit(main())
