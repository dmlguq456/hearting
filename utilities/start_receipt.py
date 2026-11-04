#!/usr/bin/env python3
"""Best-effort storage and post-tool observation of actual start results."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import uuid

from dispatch_contract import resolve_agent_home, resolve_dispatch_state_root
from session_identity import identity


def runtime_commands(agent_home):
    """Reuse the known launcher/root; paths are guidance, never capability checks."""
    from parent_next_directive import command_home, entrypoint

    result = {"agent_home": command_home(agent_home),
              "run_command": shlex.join(["sh", entrypoint(agent_home, "tools/install/harness.sh"), "run"])}
    who = identity()
    if who.known and who.harness in {"claude", "codex", "opencode"}:
        relative = f"adapters/{who.harness}/bin/preflight.sh"
        if (Path(agent_home) / relative).is_file():
            result["preflight_command"] = shlex.join(["sh", entrypoint(agent_home, relative)])
    return result


def _directory(jobs=None):
    if jobs is not None:
        return Path(jobs).resolve().parent / "start-receipts"
    return resolve_dispatch_state_root(resolve_agent_home(), environ=os.environ) / "start-receipts"


def _key(harness, sid):
    return hashlib.sha256(f"{harness}\0{sid}".encode()).hexdigest()


def _write(path, value):
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
            name = file.name
            os.chmod(name, 0o600)
            json.dump(value, file, ensure_ascii=False)
        os.replace(name, path)
        name = None
    finally:
        if name is not None:
            os.unlink(name)


def save(result, jobs):
    """Keep the result byte values; storage failure never changes its directive."""
    try:
        who = identity()
        folder = _directory(jobs)
        folder.mkdir(parents=True, exist_ok=True)
        key = _key(who.harness, who.session_id) if who.known and who.session_id else _key("route", result["route_id"])
        path = folder / (key + ".json")
        receipt = {**result, "receipt_file": str(path)}
        record = {"schema": "start_receipt_v1", "id": uuid.uuid4().hex, "saved_at": time.time(),
                  "harness": who.harness if who.known else "", "session_id": who.session_id if who.known else "",
                  "receipt": receipt}
        with (folder / (key + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _write(path, record)
        return receipt
    except (OSError, ValueError, TypeError, KeyError):
        return result


_ANSWER_ACTIONS = {"answer-blocked-owner", "answer-failed-owner"}
_STEP_COMMANDS = ("recovery_command", "correction_command", "resume_command", "parent_next_command")


def _finish_command(receipt):
    from parent_next_directive import entrypoint, route_reference
    import shlex
    route = receipt.get("route_file") or receipt.get("route_id") or "<route>"
    entry = entrypoint(Path(__file__).resolve().parents[1], "utilities/capability-route.py")
    return (shlex.join([sys.executable, entry, "finish", "--route", route_reference(route)])
            + " --evidence <FILE|URL> --summary-file <FILE>")


def next_line(receipt):
    """One plain line naming what the caller does next, or "" for no receipt.

    A command prints it after its JSON so neither a person nor an agent has to parse
    the JSON to act. It restates the receipt's own directive (`parent_next`,
    `required_action`, `next_step` and the commands they name) and never adds one.
    """
    if not isinstance(receipt, dict) or not receipt:
        return ""
    state = receipt.get("state")
    action = receipt.get("required_action")
    command = receipt.get("parent_next_command")
    if receipt.get("parent_next") == "bounded-wait" and command and command != "-":
        return f"next: run this bounded wait, then read its result: {command}"
    if receipt.get("parent_next") == "end-turn":
        resume_at = (receipt.get("auto_resume") or {}).get("resume_at")
        if resume_at:
            return (f"next: end this turn; nothing to run - the runtime starts this route again at "
                    f"{resume_at} and leaves one notice")
        return "next: end this turn; the runtime brings the result back to this session"
    if state == "inline" or action == "execute-inline":
        return f"next: do the work in this session, then run: {_finish_command(receipt)}"
    if action in _ANSWER_ACTIONS and receipt.get("correction_command"):
        return ("next: get the person's answer to owner_report, then run: "
                f"{receipt['correction_command']} --message-file <answer file>")
    if state == "completed" and action in {None, "advance-completed"}:
        return "next: the route is complete; report its result"
    step = " ".join(str(receipt.get("next_step") or "").split())
    if step:
        named = [f"{name}={receipt[name]}" for name in _STEP_COMMANDS
                 if receipt.get(name) and receipt[name] != "-" and name in step]
        head = action or (state if state not in {None, "needs-attention"} else None)
        reason = receipt.get("reason")
        if reason:
            head = f"{head} ({reason})" if head else reason
        return "next: " + (f"{head}: " if head else "") + step + "".join(f" {item}" for item in named)
    if receipt.get("resume_command"):
        return f"next: state {state or 'unknown'}; to continue, run: {receipt['resume_command']}"
    return f"next: state {state}" if state else ""


def print_next_line(receipt, stream=None):
    """Write `next_line` last on stderr: stdout keeps exactly the JSON its readers parse."""
    line = next_line(receipt)
    if line:
        sys.stdout.flush()
        print(line, file=stream or sys.stderr, flush=True)


def context(harness, sid, jobs=None):
    """Observe once; a receipt is a hint, never completion or launch authority."""
    if harness not in {"claude", "codex", "opencode"} or not sid:
        return ""
    try:
        folder = _directory(jobs)
        key = _key(harness, sid)
        path = folder / (key + ".json")
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4_000_000:
            return ""
        with (folder / (key + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            row = json.loads(path.read_text())
            if row.get("schema") != "start_receipt_v1" or (row.get("harness"), row.get("session_id")) != (harness, sid):
                return ""
            if not 0 <= time.time() - row["saved_at"] <= 3600:
                return ""
            receipt = row["receipt"]
            if not isinstance(receipt, dict) or not receipt.get("parent_next"):
                return ""
            seen = folder / (key + ".seen")
            if seen.exists() and json.loads(seen.read_text()) == row["id"]:
                return ""
            text = "[start-receipt] " + " ".join(f"{k}={receipt[k]}" for k in ("route_id", "state") if k in receipt)
            text += f"\nreceipt_file={path}"
            for name in ("parent_next", "parent_next_reason", "parent_next_command", "required_action", "resume_command"):
                if name in receipt:
                    text += f"\n{name}={receipt[name] or '-'}"
            line = next_line(receipt)
            if line:
                text += "\n" + line
            if len(text.encode()) > 8000:
                text = f"[start-receipt] parent_next={receipt['parent_next']}\nRead the full receipt: {path}"
            _write(seen, row["id"])
            return text
    except (OSError, ValueError, TypeError, KeyError):
        return ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True, choices=("claude", "codex", "opencode"))
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args()
    text = context(args.harness, args.session_id)
    if text:
        print(text)


if __name__ == "__main__":
    main()
