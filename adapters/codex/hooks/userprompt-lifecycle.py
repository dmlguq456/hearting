#!/usr/bin/env python3
"""Codex UserPromptSubmit bridge for portable prompt lifecycle signals."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
PREFLIGHT = ROOT / "adapters" / "codex" / "bin" / "preflight.sh"
RECALL_HOOK = ROOT / "hooks" / "mem-recall-inject.sh"
LOCAL_EVIDENCE_HOOK = ROOT / "hooks" / "local-evidence-inject.sh"
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
UTILITIES = ROOT / "utilities"
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))

from fleet.token_accounting import record_accounting  # noqa: E402
from fleet.token_budget import DIRECTIVE_TEXTS  # noqa: E402
from session_summary_trigger import launch_trigger  # noqa: E402

SD111_SWEEP_RECIPIENT_KIND = "codex-stop-hook"
SD111_SWEEP_SESSION_GENERATION = "unsupported"


@dataclass
class PreflightResult:
    stdout: str = ""
    returncode: int = 0
    timed_out: bool = False


def first_string(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


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


def load_payload() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def cwd(payload: dict[str, Any]) -> str:
    return nested_string(payload, "cwd", "working_directory", "workingDirectory") or os.getcwd()


def session_id(payload: dict[str, Any]) -> str:
    sid = nested_string(payload, "session_id", "sessionID", "thread_id", "threadID")
    session = payload.get("session")
    if not sid and isinstance(session, dict):
        sid = first_string(session, "id")
    return sid or "codex-hook"


def interaction_session_id(payload: dict[str, Any]) -> str:
    sid = nested_string(payload, "session_id", "sessionID", "thread_id", "threadID")
    session = payload.get("session")
    if not sid and isinstance(session, dict):
        sid = first_string(session, "id")
    return sid


def turn_id(payload: dict[str, Any]) -> str:
    return nested_string(payload, "turn_id", "turnID", "message_id", "messageID")


def candidate_context(payload: dict[str, Any], current_cwd: str, sid: str) -> str:
    """Run the bounded D-55 capsule probe; every failure is zero context."""
    prompt = nested_string(payload, "prompt")
    if not prompt:
        return ""
    command = [
        str(RECALL_HOOK), "--prompt", prompt, "--cwd", current_cwd,
        "--session-id", sid, "--runtime", "codex", "--format", "text",
    ]
    current_turn = turn_id(payload)
    if current_turn:
        command += ["--turn-id", current_turn]
    env = os.environ.copy()
    # The installed hook owns this bridge. Do not let a stale/invalid inherited
    # AGENT_HOME redirect the prompt probe to another tree's memory launcher.
    env["AGENT_HOME"] = str(ROOT)
    try:
        result = subprocess.run(
            command, cwd=str(ROOT), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def local_evidence_context(current_cwd: str) -> str:
    """Run the bounded local-evidence presence probe; every failure is zero context."""
    command = [str(LOCAL_EVIDENCE_HOOK), "--cwd", current_cwd, "--format", "text"]
    env = os.environ.copy()
    env["AGENT_HOME"] = str(ROOT)
    try:
        result = subprocess.run(
            command, cwd=str(ROOT), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def run_preflight_result(*args: str, timeout_seconds: float | None = None,
                         env_extra: dict[str, str] | None = None) -> PreflightResult:
    env = os.environ.copy()
    env.setdefault("AGENT_HOME", str(ROOT))
    if env_extra:
        env.update(env_extra)
    if timeout_seconds is not None:
        process = subprocess.Popen(
            [str(PREFLIGHT), *args], cwd=str(ROOT), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except (OSError, ProcessLookupError):
                pass
            process.communicate()
            return PreflightResult(returncode=process.returncode or -9, timed_out=True)
        if stderr:
            sys.stderr.write(stderr)
        return PreflightResult(stdout=stdout, returncode=process.returncode)
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
    return PreflightResult(stdout=result.stdout, returncode=result.returncode)


def run_preflight(*args: str, timeout_seconds: float | None = None) -> str:
    return run_preflight_result(*args, timeout_seconds=timeout_seconds).stdout


def is_worker_session() -> bool:
    return (
        os.environ.get("AGENT_SESSION_ROLE", "").lower() == "worker"
        or os.environ.get("AGENT_DISPATCH_CHILD") == "1"
        or bool(os.environ.get("AGENT_DISPATCH_DEPTH"))
        or bool(os.environ.get("OPENCODE_DISPATCH_SLUG"))
        or os.environ.get("FLEET_TITLE_REFRESH") == "1"
        or os.environ.get("MEM_DISTILL") == "1"
    )


def token_budget_timeout() -> float:
    try:
        value = float(os.environ.get("CODEX_TOKEN_BUDGET_HOOK_TIMEOUT_SECONDS", "1.0"))
    except ValueError:
        value = 1.0
    return min(5.0, max(0.05, value))


def emit_context(event_name: str, parts: list[str]) -> None:
    context = "\n".join(part.strip() for part in parts if part.strip())
    if not context:
        return
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": context}}, ensure_ascii=False))


def load_token_receipt(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > 2048:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("receipt_version") != 1:
        return None
    if value.get("outcome") not in {"zero", "emission"}:
        return None
    sample = value.get("session_total_tokens")
    if sample is not None and (not isinstance(sample, int) or isinstance(sample, bool) or sample < 0):
        return None
    return value


def token_budget_context(current_cwd: str, sid: str) -> str:
    """Run Phase 1 output and record one Phase 2 outcome without changing bytes."""

    temporary: tempfile.TemporaryDirectory[str] | None = None
    receipt_path: Path | None = None
    contribution = ""
    event: dict[str, Any] = {
        "outcome": "zero", "zero_reason": "timeout_or_error",
        "directive_id": None, "directive_utf8_bytes": 0,
        "session_total_tokens": None,
    }
    try:
        try:
            temporary = tempfile.TemporaryDirectory(prefix="codex-token-budget-")
            receipt_path = Path(temporary.name) / "result.json"
        except OSError:
            # Receipt infrastructure is optional and must not change Phase 1 output.
            temporary = None
            receipt_path = None
        env_extra = (
            {"AGENT_TOKEN_BUDGET_RESULT_PATH": str(receipt_path)}
            if receipt_path is not None else None
        )
        try:
            result = run_preflight_result(
                "token-budget", current_cwd, sid, "hook",
                timeout_seconds=token_budget_timeout(), env_extra=env_extra,
            )
        except (OSError, ValueError, TypeError):
            result = PreflightResult(returncode=-1)
        receipt = (
            load_token_receipt(receipt_path)
            if receipt_path is not None and result.returncode == 0 and not result.timed_out
            else None
        )
        delivered = next(
            ((directive_id, text) for directive_id, text in DIRECTIVE_TEXTS.items()
             if result.stdout == text + "\n"),
            None,
        ) if result.returncode == 0 and not result.timed_out else None
        if delivered is not None:
            directive_id, contribution = delivered
            expected_bytes = len(contribution.encode("utf-8"))
            if (receipt is not None and receipt.get("outcome") == "emission"
                    and receipt.get("directive_id") == directive_id
                    and receipt.get("directive_utf8_bytes") == expected_bytes):
                event = receipt
            else:
                event = {
                    "outcome": "emission", "zero_reason": None,
                    "directive_id": directive_id,
                    "directive_utf8_bytes": expected_bytes,
                    "session_total_tokens": None,
                }
        elif (result.returncode == 0 and not result.timed_out and result.stdout == ""
              and receipt is not None and receipt.get("outcome") == "zero"
              and receipt.get("zero_reason") in {
                  "normal", "unknown", "native", "same_band", "degraded"}
              and receipt.get("directive_id") is None
              and receipt.get("directive_utf8_bytes") == 0):
            event = receipt
    finally:
        try:
            record_accounting(sid, event, adapter="codex")
        except Exception:
            # Accounting is an observational side effect and never owns output.
            pass
        if temporary is not None:
            try:
                temporary.cleanup()
            except OSError:
                pass
    return contribution


def _sd111_sweep_marker(root: Path, sid: str) -> Path:
    digest = hashlib.sha256(sid.encode("utf-8")).hexdigest()
    return root / "session-sweep-markers" / f"{digest}.marker"


def sd111_first_prompt_sweep(sid: str) -> None:
    """SD-111 P4 -- Codex carrier 2. There is no confirmed Codex
    SessionStart-equivalent surface (plan §P4), so this runs at the first
    UserPromptSubmit of a session only -- gated by a durable per-session
    marker file, never on every turn. Codex is measured `unproven` for
    session-generation proof (§3.5), so the sweep is always refused with
    `pending-delivery-generation-unproven`; that refusal is the point.
    Fail-open end to end: any failure here is swallowed and never surfaces to
    the prompt.
    """

    if not sid:
        return
    try:
        from dispatch_contract import dispatch_state_roots, resolve_agent_home
        from dispatch_session_sweep import sweep
    except Exception:
        return
    try:
        roots = dispatch_state_roots(resolve_agent_home())
    except Exception:
        return
    if not roots:
        return
    marker = _sd111_sweep_marker(roots[0], sid)
    if marker.exists():
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
    except OSError:
        return
    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        try:
            sweep(root, SD111_SWEEP_RECIPIENT_KIND, sid, SD111_SWEEP_SESSION_GENERATION)
        except Exception:
            continue


def main() -> int:
    payload = load_payload()
    # Dispatch prompts carry their own explicit status/prompt-signal/mode
    # bootstrap. Main-only briefing, token context, and turn-nudge would be
    # duplicate context and can recursively create model work (D-42).
    if is_worker_session():
        return 0
    current_cwd = cwd(payload)
    interaction_sid = interaction_session_id(payload)
    try:
        from herdr_session_projection import project
        project(payload, interaction_sid or "", worker=False)
    except Exception:
        pass
    if interaction_sid:
        try:
            from fleet import interaction

            interaction.clear_wait(interaction_sid, "codex")
        except Exception:
            pass
    sid = session_id(payload)
    current_prompt = nested_string(payload, "prompt")
    if interaction_sid:
        launch_trigger(
            "codex", interaction_sid, "initial", anchor_text=current_prompt
        )
    sd111_first_prompt_sweep(sid)

    parts = []
    parts.append(candidate_context(payload, current_cwd, sid))
    parts.append(local_evidence_context(current_cwd))
    parts.append(run_preflight("briefing", current_cwd))
    # Phase 1 token self-regulation is transition-only. Normal, unknown,
    # native-owned, and repeated bands return an empty string (zero injection).
    parts.append(token_budget_context(current_cwd, sid))
    run_preflight("turn-nudge", current_cwd, sid)
    emit_context("UserPromptSubmit", parts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
