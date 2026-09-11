"""OpenCode process transport for the shared CLI session supervisor.

This driver owns native submission and session identity only. The supervisor
retains completion, retry, cleanup, and notification decisions.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import shlex
import subprocess
import tempfile
import time
from dispatch_contract import supervisor_lease_path


MAX_EVENT_BYTES = 2 * 1024 * 1024
SESSION_ID = re.compile(r"ses_[A-Za-z0-9]+")


class OpenCodeTransportError(RuntimeError):
    pass


def binding_path(args) -> Path:
    return supervisor_lease_path(args.jobs, args.parent_attempt_id).with_suffix(".opencode-session.json")


def read_binding(args) -> str:
    path = binding_path(args)
    if path.is_symlink():
        raise OpenCodeTransportError("opencode-session-binding-unsafe")
    if not path.exists():
        return ""
    if path.stat().st_size > 4096:
        raise OpenCodeTransportError("opencode-session-binding-unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        session = value["session_id"]
        if (value["attempt_id"] != args.parent_attempt_id
                or value["worktree"] != os.path.realpath(args.worktree)
                or not isinstance(session, str) or not SESSION_ID.fullmatch(session)):
            raise ValueError("binding mismatch")
        return session
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OpenCodeTransportError("opencode-session-binding-invalid") from exc


def bind_session(args, session: str) -> None:
    existing = read_binding(args)
    if existing:
        if existing != session:
            raise OpenCodeTransportError("opencode-session-identity-mismatch")
        return
    path = binding_path(args)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    value = {"attempt_id": args.parent_attempt_id,
             "worktree": os.path.realpath(args.worktree), "session_id": session}
    # The common exact-attempt supervisor lease serializes this native binding.
    fd, temporary = tempfile.mkstemp(prefix=".opencode-session-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def command(args, session: str) -> list[str]:
    override = getattr(args, "opencode_command", None)
    result = shlex.split(override) if override else ["opencode", "run"]
    result += ["--dir", args.worktree, "--format", "json",
               "--agent", getattr(args, "opencode_agent", "build")]
    if session:
        result += ["--session", session]
    if args.model:
        result += ["--model", args.model]
    variant = getattr(args, "variant", None)
    if variant and variant != "runtime-default":
        result += ["--variant", variant]
    return result


def run_turn(args, prompt: str, *, emit) -> tuple[dict, int]:
    """Stream one exact native turn, then return the portable result envelope."""
    session = read_binding(args)
    if session:
        emit({"type": "dispatch.supervisor.session", "runtime": "opencode",
              "parent_attempt_id": args.parent_attempt_id,
              "session_id": session, "cwd": args.worktree})
    final_text = ""
    terminal_stop = False
    runtime_error = None
    buffer = b""
    oversized = False
    deadline = time.monotonic() + args.turn_timeout

    def event(line: bytes) -> None:
        nonlocal session, final_text, terminal_stop, runtime_error
        try:
            value = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return  # Native CLI diagnostics are not result evidence.
        if not isinstance(value, dict):
            return
        native = value.get("sessionID")
        if not isinstance(native, str) or not SESSION_ID.fullmatch(native):
            raise OpenCodeTransportError("opencode-event-session-missing")
        if session and session != native:
            raise OpenCodeTransportError("opencode-session-identity-mismatch")
        if not session:
            bind_session(args, native)
            session = native
            emit({"type": "dispatch.supervisor.session", "runtime": "opencode",
                  "parent_attempt_id": args.parent_attempt_id,
                  "session_id": session, "cwd": args.worktree})
        kind = value.get("type")
        part = value.get("part") or {}
        if kind == "text":
            final_text = part.get("text", "")
            terminal_stop = False
        elif kind == "step_start":
            final_text, terminal_stop = "", False
        elif kind == "step_finish":
            terminal_stop = part.get("reason") == "stop"
        elif kind == "error":
            runtime_error = value.get("error") or {"message": "opencode-runtime-error"}
        elif kind == "tool_use":
            # Progress has no authority to finish the owner. Keep model prose,
            # outputs and intermediate stop events out of the terminal log.
            state = part.get("state") or {}
            emit({"type": "tool_use", "timestamp": value.get("timestamp"),
                  "sessionID": session, "part": {"type": "tool",
                      "tool": part.get("tool"), "callID": part.get("callID"),
                      "id": part.get("id"), "state": {"status": state.get("status")}}})

    with tempfile.TemporaryFile() as input_file:
        input_file.write(prompt.encode("utf-8"))
        input_file.seek(0)
        process = subprocess.Popen(command(args, session), cwd=args.worktree,
                                   stdin=input_file, stdout=subprocess.PIPE, stderr=None)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                eof = False
                while not eof:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise OpenCodeTransportError("opencode-turn-timeout")
                    for key, _ in selector.select(min(1.0, remaining)):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            eof = True
                            break
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            if oversized or len(line) > MAX_EVENT_BYTES:
                                oversized = False
                                continue
                            event(line)
                        if len(buffer) > MAX_EVENT_BYTES:
                            buffer, oversized = b"", True
                if buffer and not oversized:
                    event(buffer)
            try:
                code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise OpenCodeTransportError("opencode-turn-timeout") from exc
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
    if not session:
        raise OpenCodeTransportError("opencode-session-not-observed")
    if not runtime_error and (not terminal_stop or not isinstance(final_text, str)):
        raise OpenCodeTransportError("opencode-final-stop-missing")
    result = {"type": "result", "runtime": "opencode", "session_id": session,
              "subtype": "error_during_execution" if runtime_error or code else "success",
              "is_error": bool(runtime_error or code), "result": final_text}
    if runtime_error:
        result["error"] = runtime_error
    return result, code
