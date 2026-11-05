#!/usr/bin/env python3
"""Runtime-neutral hook bridge for worker compaction records."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "utilities" / "worker-state-ledger.py"


def _active() -> bool:
    return (
        os.environ.get("AGENT_DISPATCH_STAGE_AUTHORITY") == "0"
        or bool(os.environ.get("AGENT_DISPATCH_SUBSESSION_ID"))
    )


def _binding() -> tuple[str, str]:
    path = os.environ.get("AGENT_WORKER_STATE_LEDGER", "")
    attempt = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID", "")
    if not path or not attempt:
        raise ValueError("worker sub-session ledger binding missing")
    return path, attempt


def _run(action: str) -> subprocess.CompletedProcess[str]:
    ledger, attempt = _binding()
    command = [sys.executable, str(LEDGER), action, "--path", ledger, "--attempt-id", attempt]
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _native_worker(harness: str, payload: dict) -> bool:
    if payload.get("worker") is True:
        return True
    if harness == "claude":
        return bool(payload.get("agent_id") or payload.get("agentId"))
    if harness == "codex":
        return bool(payload.get("agent_id") or payload.get("agentId") or payload.get("subagent")
                    or payload.get("agent_type") == "subagent"
                    or payload.get("agentType") == "subagent")
    return False


def _main_compact(action: str, harness: str, payload: dict) -> int:
    env = os.environ.copy()
    if _native_worker(harness, payload):
        env["AGENT_SESSION_ROLE"] = "worker"
    if env.get("AGENT_SESSION_ROLE") == "worker" or env.get("AGENT_DISPATCH_CHILD") == "1" \
            or env.get("AGENT_DISPATCH_DEPTH") or env.get("FLEET_TITLE_REFRESH") == "1" \
            or env.get("MEM_DISTILL") == "1" or any(k.startswith("OPENCODE_DISPATCH") and v for k, v in env.items()):
        return 0
    root = Path(os.environ.get("AGENT_HOME") or Path(__file__).resolve().parents[1])
    runtime_status = action == "runtime-status"
    command = [sys.executable, str(root / "utilities/session_tidy.py"), "hook",
               "--harness", harness, "--event", "runtime-status" if runtime_status else "compact"]
    if not runtime_status:
        command.extend(("--phase", "before" if action == "compact-before" else "after"))
        if action == "compact-after":
            command.extend(("--format", "json"))
    sid = payload.get("session_id") or payload.get("sessionID") or payload.get("sessionId")
    cwd = payload.get("cwd")
    transcript = payload.get("transcript_path") or payload.get("transcriptPath")
    if isinstance(sid, str) and sid:
        command.extend(("--session-id", sid))
    if isinstance(cwd, str) and cwd:
        command.extend(("--cwd", cwd))
    if isinstance(transcript, str) and transcript:
        command.extend(("--transcript", transcript))
    compact_summary = payload.get("compact_summary") or payload.get("compactSummary") or payload.get("summary") or ""
    compact_context = {}
    for key in ("trigger", "summary_id", "parent_id", "tail_start_id", "compaction_part_id",
                "turn_id", "event_id", "status", "observed_at"):
        value = payload.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            compact_context[key] = value
    if isinstance(transcript, str) and transcript:
        compact_context["transcript"] = transcript
    try:
        body = compact_context if runtime_status else {"summary": compact_summary, **compact_context}
        result = subprocess.run(command, input=json.dumps(body), text=True,
                                capture_output=True, check=False, timeout=4, env=env)
    except subprocess.TimeoutExpired:
        if action == "compact-after":
            print("booking=pending reason=compact-hook-timeout")
        return 0
    except OSError:
        if action == "compact-after":
            print("booking=pending reason=compact-hook-unavailable")
        return 0
    if result.returncode == 0 and action == "compact-before" and harness in ("claude", "opencode"):
        sys.stdout.write(result.stdout or "")
        sys.stdout.flush()
    elif result.returncode == 0 and action == "compact-after":
        try:
            emitted = json.loads(result.stdout or "{}")
            notice = emitted.get("humanNotice", "")
        except (ValueError, AttributeError):
            notice = ""
        if notice:
            print(json.dumps({"systemMessage": notice,
                              "deferredConsumed": emitted.get("deferredConsumed", [])},
                             ensure_ascii=False), flush=True)
        status = next((line.strip() for line in (result.stdout or "").splitlines()
                       if line.strip().startswith("booking=")), "")
        if status:
            # Runtime adapters consume this internal status; hook wrappers keep it out of prompt input.
            sys.stdout.write(status + "\n")
    elif result.returncode != 0 and action == "compact-after":
        print(f"booking=pending reason=compact-hook-exit-{result.returncode}")
    elif result.returncode == 0 and action == "runtime-status" \
            and (result.stdout or "").strip() == "booking=pending":
        sys.stdout.write("booking=pending\n")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("compact-before", "compact-after", "runtime-status"))
    p.add_argument("--harness", choices=("claude", "codex", "opencode"))
    args = p.parse_args()
    # Keep the declared sub-session ledger branch first and fail-closed exactly as before.
    if _active():
        if args.action == "runtime-status":
            return 0
        try:
            result = _run(args.action)
            if result.returncode:
                raise ValueError((result.stderr or result.stdout).strip())
            if args.action == "compact-after" and result.stdout:
                print(result.stdout, end="")
            return 0
        except ValueError as exc:
            print(f"worker-state-hook: {exc}", file=sys.stderr)
            return 2
    if args.harness:
        try:
            payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
            if not isinstance(payload, dict):
                payload = {}
            return _main_compact(args.action, args.harness, payload)
        except BaseException as exc:
            if args.action == "compact-after":
                print(f"booking=pending reason=bridge-{type(exc).__name__}")
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
