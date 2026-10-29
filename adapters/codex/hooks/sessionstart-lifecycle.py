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
LOCAL_EVIDENCE_HOOK = ROOT / "hooks" / "local-evidence-inject.sh"
MEM_PY = ROOT / "tools" / "memory" / "mem.py"
SESSION_TIDY = ROOT / "utilities" / "session_tidy.py"


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


def local_evidence_context(current_cwd: str) -> str:
    """Run the bounded local-evidence presence probe; every failure is zero context.

    Session start rather than prompt submit: the block is byte-identical between
    prompts, and this event also fires on resume/clear/compact, which is the only
    thing a per-prompt repeat was buying. The probe holds its own wall-clock
    budget, so this timeout is a backstop, not the fence.
    """
    command = [str(LOCAL_EVIDENCE_HOOK), "--cwd", current_cwd, "--format", "text"]
    env = os.environ.copy()
    env["AGENT_HOME"] = str(ROOT)
    try:
        result = subprocess.run(
            command, cwd=str(ROOT), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


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


def card_context(payload: dict[str, Any], current_cwd: str) -> tuple[str, str, Any]:
    """Session card / tidy notice due at this start; independent of the memory opt-in.

    JSON mode hands back the consumption receipts instead of writing them, so
    they are committed only after this bridge's final output exists.
    """
    sid = session_id(payload)
    if not sid or not SESSION_TIDY.is_file():
        return "", "", None
    command = [sys.executable, str(SESSION_TIDY), "hook", "--harness", "codex",
               "--event", "start", "--session-id", sid, "--cwd", current_cwd, "--format", "json"]
    source = nested_string(payload, "source")
    if source:
        command += ["--source", source]
    transcript = nested_string(payload, "transcript_path", "transcriptPath")
    if transcript:
        command += ["--transcript", transcript]
    try:
        result = subprocess.run(
            command, cwd=str(ROOT), env=os.environ.copy(), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=4, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "", "", None
    if result.returncode:
        return "", "", None
    try:
        payload = json.loads(result.stdout or "{}")
    except ValueError:
        return "", "", None
    if not isinstance(payload, dict):
        return "", "", None
    return (str(payload.get("additionalContext") or ""),
            str(payload.get("humanNotice") or ""),
            payload.get("deferredConsumed"))


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
    human_notice = ""
    card_deferred = None
    try:
        if not worker:
            forget_shown_candidates(payload)
            card, human_notice, card_deferred = card_context(payload, current_cwd)
            parts.append(card)
            try:
                utilities = ROOT / "utilities"
                if str(utilities) not in sys.path:
                    sys.path.insert(0, str(utilities))
                from dispatch_contract import dispatch_state_roots, resolve_agent_home
                from dispatch_session_sweep import activate, delivery_context
                from dispatch_notice_state import NoticeReadContext
                notice_context = NoticeReadContext()
                if sid:
                    batches = [(root, activate(root, "codex-native-queue", sid,
                                                request_recovery=False, notice_context=notice_context))
                               for root in dict.fromkeys(dispatch_state_roots(resolve_agent_home()))
                               if root.is_dir()]
                    parts.append(delivery_context(batches))
            except Exception:
                pass
            if env_truthy("CODEX_SESSION_MEMORY_INJECT"):
                parts.append(run_preflight("memory", current_cwd))
            parts.append(local_evidence_context(current_cwd))
    finally:
        try:
            emit_context("SessionStart", parts, human_notice)
            if card_deferred:
                try:
                    import session_tidy
                    session_tidy.apply_deferred(card_deferred)
                except Exception:
                    pass
        finally:
            if not worker:
                request_lifecycle_recovery()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
