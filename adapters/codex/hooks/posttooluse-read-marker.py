#!/usr/bin/env python3
"""Codex PostToolUse bridge for portable spec read markers."""

from __future__ import annotations

import json
import importlib.util
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
PREFLIGHT = ROOT / "adapters" / "codex" / "bin" / "preflight.sh"
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import resolve_agent_home as _resolve_agent_home  # noqa: E402


def _agent_home() -> Path:
    return _resolve_agent_home(runtime_pointer=Path.home() / ".codex" / "hearting")


def first_string(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def nested_mapping(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def nested_string(payload: dict[str, Any], *keys: str) -> str:
    direct = first_string(payload, *keys)
    if direct:
        return direct
    for key in ("context", "workspace", "session", "payload", "event", "input", "data"):
        value = payload.get(key)
        if isinstance(value, dict):
            found = nested_string(value, *keys)
            if found:
                return found
    return ""


def tool_name(payload: dict[str, Any]) -> str:
    direct = first_string(payload, "tool_name", "toolName", "matcher")
    if direct:
        return direct
    raw_tool = payload.get("tool")
    if isinstance(raw_tool, str) and raw_tool:
        return raw_tool
    tool = nested_mapping(payload, "tool", "toolUse", "tool_use")
    return first_string(tool, "name", "tool_name", "toolName")


def tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    direct = nested_mapping(payload, "tool_input", "toolInput", "input", "arguments", "args", "params")
    if direct:
        return direct
    tool = nested_mapping(payload, "tool", "toolUse", "tool_use")
    return nested_mapping(tool, "tool_input", "toolInput", "input", "arguments", "args", "params")


def cwd(payload: dict[str, Any]) -> Path:
    raw = nested_string(payload, "cwd", "working_directory", "workingDirectory")
    return Path(raw) if raw else Path.cwd()


def normalize(base: Path, raw: str) -> str:
    if not raw or raw == "/dev/null":
        return ""
    path = Path(raw)
    if not path.is_absolute():
        path = base / path
    return str(path)


def is_shell_tool(name: str) -> bool:
    return name in {"Bash", "bash", "Shell", "shell", "exec_command", "functions.exec_command"} or name.endswith(
        ".exec_command"
    )


def shell_command(payload: dict[str, Any], args: dict[str, Any]) -> str:
    return first_string(args, "command", "cmd", "script", "input") or first_string(
        payload, "command", "cmd", "script"
    )


def shell_read_target(payload: dict[str, Any], args: dict[str, Any]) -> str:
    command = shell_command(payload, args)
    if not command:
        return ""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return ""

    read_commands = {"cat", "sed", "awk", "grep", "rg", "head", "tail", "nl", "less", "more"}
    if not any(Path(token).name in read_commands for token in tokens):
        return ""

    base = cwd(payload)
    for token in tokens:
        if token.startswith("-") or token in {"|", "&&", "||", ";"}:
            continue
        normalized = token.replace("\\", "/")
        if "spec/prd.md" in normalized or ("/core/" in normalized and normalized.endswith(".md")) or (
            normalized.startswith("core/") and normalized.endswith(".md")
        ):
            return normalize(base, token)
    return ""


def read_target(payload: dict[str, Any]) -> str:
    name = tool_name(payload)
    args = tool_input(payload)
    if name in {"Read", "read"}:
        return normalize(cwd(payload), first_string(args, "file_path", "filePath", "path", "file"))
    if is_shell_tool(name):
        return shell_read_target(payload, args)
    return ""


def bind_material_route(payload: dict[str, Any], session_id: str) -> None:
    name = tool_name(payload)
    if not is_shell_tool(name):
        return
    command = shell_command(payload, tool_input(payload))
    guard_path = ROOT / "hooks" / "material-route-guard.py"
    spec = importlib.util.spec_from_file_location("material_route_guard", guard_path)
    if not spec or not spec.loader:
        return
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    invocations = guard.route_compile_invocations(command, cwd(payload).resolve(strict=False))
    outputs = [path for invocation in invocations for path in invocation.outputs]
    if len(outputs) != 1 or len(invocations) != 1:
        return
    effective_cwd = invocations[0].effective_cwd
    env = os.environ.copy()
    env.setdefault("AGENT_HOME", str(_agent_home()))
    subprocess.run(
        [str(PREFLIGHT), "material-route", "bind", "--route", str(outputs[0]),
         "--cwd", str(effective_cwd), "--session", session_id],
        cwd=str(ROOT), env=env, text=True, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False,
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0

    session_id = nested_string(payload, "session_id", "sessionID", "thread_id", "threadID")
    if not session_id:
        session_id = first_string(nested_mapping(payload, "session"), "id")
    session_id = session_id or "codex-hook"
    bind_material_route(payload, session_id)
    file = read_target(payload)
    if not file:
        return 0
    env = os.environ.copy()
    env.setdefault("AGENT_HOME", str(_agent_home()))
    result = subprocess.run(
        [str(PREFLIGHT), "read", file, session_id],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
