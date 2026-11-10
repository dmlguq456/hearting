#!/usr/bin/env python3
"""Codex SessionStart bridge for portable lifecycle signals."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
PREFLIGHT = ROOT / "adapters" / "codex" / "bin" / "preflight.sh"
MEM_PY = ROOT / "tools" / "memory" / "mem.py"
UTILITIES = ROOT / "utilities"
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))


def first_string(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def load_payload() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


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


def cwd(payload: dict[str, Any]) -> str:
    return nested_string(payload, "cwd", "working_directory", "workingDirectory") or os.getcwd()


def session_id(payload: dict[str, Any]) -> str:
    sid = nested_string(payload, "session_id", "sessionID", "thread_id", "threadID")
    session = payload.get("session")
    if not sid and isinstance(session, dict):
        sid = first_string(session, "id")
    return sid


def run_preflight(*args: str) -> str:
    env = os.environ.copy()
    env.setdefault("AGENT_HOME", str(ROOT))
    result = subprocess.run(
        [str(PREFLIGHT), *args],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.stdout


def forget_shown_candidates(payload: dict[str, Any]) -> None:
    """After compact or clear the model no longer holds what memory showed it.

    The same ``mem.py`` helper the other harnesses call empties this session's
    candidate display history; every failure is silent.
    """
    source = nested_string(payload, "source").lower()
    sid = session_id(payload)
    if source not in {"compact", "clear"} or not sid or not MEM_PY.is_file():
        return
    try:
        subprocess.run(
            [sys.executable, str(MEM_PY), "_seen-reset", "--session-id", sid],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def env_truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}


def is_worker_session() -> bool:
    return (
        os.environ.get("AGENT_SESSION_ROLE", "").lower() == "worker"
        or os.environ.get("AGENT_DISPATCH_CHILD") == "1"
        or bool(os.environ.get("AGENT_DISPATCH_DEPTH"))
        or bool(os.environ.get("OPENCODE_DISPATCH_SLUG"))
        or os.environ.get("FLEET_TITLE_REFRESH") == "1"
    )


def emit_context(event_name: str, parts: list[str], system_message: str = "") -> None:
    context = "\n".join(part.strip() for part in parts if part.strip())
    if not context and not system_message:
        return
    output = {"hookSpecificOutput": {"hookEventName": event_name}}
    if context:
        output["hookSpecificOutput"]["additionalContext"] = context
    if system_message:
        output["systemMessage"] = system_message
    print(json.dumps(output, ensure_ascii=False), flush=True)


def request_lifecycle_recovery() -> None:
    """Ask the existing detached recovery owner once for this lifecycle event."""
    try:
        utilities = ROOT / "utilities"
        if str(utilities) not in sys.path:
            sys.path.insert(0, str(utilities))
        from peer_obligations import request_obligation_recovery
        request_obligation_recovery(lifecycle_root=ROOT)
    except Exception:
        pass


def main() -> int:
    payload = load_payload()
    current_cwd = cwd(payload)
    sid = session_id(payload)
    worker = is_worker_session()
    if worker:
        request_lifecycle_recovery()
    else:
        try:
            from herdr_session_projection import project
            project(payload, sid, worker=False, request_recovery=False)
        except Exception:
            pass

    parts = []
    try:
        if not worker:
            forget_shown_candidates(payload)
            if sid:
                try:
                    import session_tidy
                    native_model = nested_string(payload, "model")
                    display = {"env": {**os.environ, "HEARTING_SHELL_NATIVE_MODEL": native_model}} if native_model else {}
                    session_tidy.run_hook("codex", "start", sid,
                        source=nested_string(payload, "source"),
                        transcript=nested_string(payload, "transcript_path", "transcriptPath"),
                        cwd=current_cwd, render_context=False, **display)
                except Exception:
                    pass
            # This independent, opt-in profile injection has no prompt duplicate.
            if env_truthy("CODEX_SESSION_MEMORY_INJECT"):
                parts.append(run_preflight("memory", current_cwd))
        emit_context("SessionStart", parts)
    finally:
        if not worker:
            request_lifecycle_recovery()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
