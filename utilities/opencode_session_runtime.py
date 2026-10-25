"""OpenCode process transport for the shared CLI session supervisor.

This driver owns native submission and session identity only. The supervisor
retains completion, retry, cleanup, and notification decisions.
"""
from __future__ import annotations

import json
import errno
import os
from pathlib import Path
import re
import selectors
import shlex
import stat
import subprocess
import tempfile
import time
from dispatch_contract import supervisor_lease_path
import opencode_runtime


MAX_EVENT_BYTES = 2 * 1024 * 1024
SESSION_ID = re.compile(r"ses_[A-Za-z0-9]+")


class OpenCodeTransportError(RuntimeError):
    pass


def binding_path(args) -> Path:
    return supervisor_lease_path(args.jobs, args.parent_attempt_id).with_suffix(".opencode-session.json")


def _read_native_record(path, limit=4096):
    """Bounded descriptor read of a normal native file; no private-mode gate."""
    def generation(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_uid,
                info.st_mtime_ns, info.st_ctime_ns)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if os.path.lexists(path):  # includes dangling symlinks
            raise OpenCodeTransportError("opencode-session-binding-unsafe")
        return None
    except OSError as exc:
        reason = "unsafe" if exc.errno == errno.ELOOP else "invalid"
        raise OpenCodeTransportError("opencode-session-binding-" + reason) from exc
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_size > limit):
            raise OpenCodeTransportError("opencode-session-binding-unsafe")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(limit + 1)
        if (len(raw) > limit or len(raw) != before.st_size
                or generation(before) != generation(os.fstat(fd))
                or generation(before) != generation(os.lstat(path))):
            raise OpenCodeTransportError("opencode-session-binding-unsafe")
        return {"raw": raw, "source": {"generation": generation(before),
                "mtime": before.st_mtime, "ctime": before.st_ctime}}
    except OSError as exc:
        raise OpenCodeTransportError("opencode-session-binding-invalid") from exc
    finally:
        os.close(fd)


def read_binding_record(args):
    record = _read_native_record(binding_path(args))
    if record is None:
        return None
    try:
        value = json.loads(record["raw"])
        session = value["session_id"]
        if (value["attempt_id"] != args.parent_attempt_id
                or value["worktree"] != os.path.realpath(args.worktree)
                or not isinstance(session, str) or not SESSION_ID.fullmatch(session)):
            raise ValueError("binding mismatch")
        return {"value": value, "source": record["source"]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OpenCodeTransportError("opencode-session-binding-invalid") from exc


def read_binding(args) -> str:
    record = read_binding_record(args)
    return record["value"]["session_id"] if record is not None else ""


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
    prefix = shlex.split(override) if override else ["opencode"]
    if not prefix:
        raise OpenCodeTransportError("opencode-command-empty")
    binary = prefix[-1]
    argv = opencode_runtime.run_argv(
        binary=binary, cwd=args.worktree,
        agent=getattr(args, "opencode_agent", "build"), model=args.model,
        variant=getattr(args, "variant", None), session=session or None,
        standalone=bool(getattr(args, "opencode_standalone", False)),
        title=("hearting-worker-" + str(args.parent_attempt_id)
               if not session and opencode_runtime.is_v2(binary) else None),
    )
    # Preserve a configured launcher prefix (for wrappers such as `env VAR=...`).
    return prefix[:-1] + argv


def run_turn(args, prompt: str, *, emit, idle_reset: bool = False,
             ordinary_owner_turn: bool = False) -> tuple[dict, int]:
    """Stream one exact native turn, then return the portable result envelope.

    `idle_reset` turns `args.turn_timeout` into a no-progress window: each read
    from the live process restarts it, so a long owner wait that keeps emitting
    events is not cut off while silence for the whole window still times out.
    The default keeps the fixed deadline a bounded caller relies on."""
    session = read_binding(args)
    permission_projection_sent = False

    def publish_permission_projection() -> None:
        nonlocal permission_projection_sent
        if permission_projection_sent or not session:
            return
        try:
            import commit_policy
            projection = commit_policy.permission_projection_event(
                getattr(args, "owner_runtime_permission_state", None),
                adapter="opencode", attempt_id=getattr(args, "parent_attempt_id", ""),
                thread_id=session,
                turn_id=str(getattr(args, "owner_runtime_turn_ordinal", "direct")),
                sandbox="adapter-default", network_access=False,
                file_enforcement="tool-permission", read_enforcement="tool-permission",
                network_enforcement="none",
            )
        except ImportError:
            projection = None
        if projection is not None:
            emit(projection)
            permission_projection_sent = True

    if session:
        emit({"type": "dispatch.supervisor.session", "runtime": "opencode",
              "parent_attempt_id": args.parent_attempt_id,
              "session_id": session, "cwd": args.worktree})
        publish_permission_projection()
    final_text = ""
    terminal_stop = False
    runtime_error = None
    binary_parts = shlex.split(getattr(args, "opencode_command", None) or "opencode")
    runtime_binary = binary_parts[-1] if binary_parts else "opencode"
    native_opencode = Path(runtime_binary).name == "opencode"
    modern = opencode_runtime.is_v2(runtime_binary)
    buffer = b""
    oversized = False
    deadline = time.monotonic() + args.turn_timeout
    activity = None

    def event(line: bytes) -> bool:
        nonlocal session, final_text, terminal_stop, runtime_error
        try:
            value = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return False  # Native CLI diagnostics are not result evidence.
        if not isinstance(value, dict):
            return False
        # Keep the supervisor's established V1 event envelope exactly intact;
        # the reusable normalizer is used by passive consumers, while this
        # identity-bearing stream validates the raw event before forwarding it.
        native = value.get("sessionID")
        kind = value.get("type")
        if not native:
            if kind == "error":
                runtime_error = opencode_runtime.safe_provider_error(value.get("error"))
                return False
            raise OpenCodeTransportError("opencode-event-session-missing")
        if not isinstance(native, str) or not SESSION_ID.fullmatch(native):
            raise OpenCodeTransportError("opencode-event-session-missing")
        if session and session != native:
            raise OpenCodeTransportError("opencode-session-identity-mismatch")
        if not session:
            bind_session(args, native)
            session = native
            if activity is not None:
                activity.bind_session(session)
            emit({"type": "dispatch.supervisor.session", "runtime": "opencode",
                  "parent_attempt_id": args.parent_attempt_id,
                  "session_id": session, "cwd": args.worktree})
            publish_permission_projection()
        part = value.get("part") or {}
        if kind == "text":
            final_text = part.get("text", "")
            terminal_stop = False
        elif kind == "step_start":
            final_text, terminal_stop = "", False
        elif kind == "step_finish":
            terminal_stop = part.get("reason") == "stop"
        elif kind == "error":
            runtime_error = opencode_runtime.safe_provider_error(value.get("error"))
        elif kind == "tool_use":
            # Progress has no authority to finish the owner. Keep model prose,
            # outputs and intermediate stop events out of the terminal log.
            state = part.get("state") or {}
            emit({"type": "tool_use", "timestamp": value.get("timestamp"),
                  "sessionID": session, "part": {"type": "tool",
                      "tool": part.get("tool"), "callID": part.get("callID"),
                      "id": part.get("id"), "state": {"status": state.get("status")}}})
        return kind in {"step_start", "step_finish", "text", "tool_use"}

    # Credit the outcome to the account this request starts with.
    account = (opencode_runtime.model_account_observation(getattr(args, "model", None))
               if native_opencode else None)
    with tempfile.TemporaryFile() as input_file:
        input_file.write(prompt.encode("utf-8"))
        input_file.seek(0)
        process = subprocess.Popen(command(args, session), cwd=args.worktree,
                                   stdin=input_file, stdout=subprocess.PIPE, stderr=None)
        if ordinary_owner_turn:
            from foreground_activity import owner_turn_activity
            activity = owner_turn_activity(
                args, process, harness="opencode", window_seconds=args.turn_timeout,
            )
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                eof = False
                while not eof:
                    if activity is None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise OpenCodeTransportError("opencode-turn-timeout")
                        wait = min(1.0, remaining)
                    else:
                        wait = activity.sample_seconds
                    events = selector.select(wait)
                    payload_progress = False
                    for key, _ in events:
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            eof = True
                            break
                        if idle_reset and activity is None:
                            deadline = time.monotonic() + args.turn_timeout
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            if oversized or len(line) > MAX_EVENT_BYTES:
                                oversized = False
                                continue
                            payload_progress = event(line) or payload_progress
                        if len(buffer) > MAX_EVENT_BYTES:
                            buffer, oversized = b"", True
                    if activity is not None and activity.expired(payload_progress=payload_progress):
                        raise OpenCodeTransportError("opencode-turn-timeout")
                if buffer and not oversized:
                    payload_progress = event(buffer)
                    if activity is not None and activity.expired(payload_progress=payload_progress):
                        raise OpenCodeTransportError("opencode-turn-timeout")
            if activity is not None:
                while process.poll() is None:
                    try:
                        process.wait(timeout=activity.sample_seconds)
                    except subprocess.TimeoutExpired:
                        if activity.expired():
                            raise OpenCodeTransportError("opencode-turn-timeout")
                code = process.returncode
            else:
                try:
                    code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as exc:
                    raise OpenCodeTransportError("opencode-turn-timeout") from exc
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
    if not session and not runtime_error:
        raise OpenCodeTransportError("opencode-session-not-observed")
    # V2's JSON stream contains step/text/error/tool events, not a terminal
    # stop event. The v2.0.32 CLI waits for session.wait() before returning;
    # require the exact session, an error-free final text, and a successful
    # return from that native wait. V1 still requires its explicit final stop.
    if modern and code == 0 and isinstance(final_text, str) and final_text.strip() and not runtime_error:
        terminal_stop = True
    if session and not runtime_error and (not terminal_stop or not isinstance(final_text, str)):
        raise OpenCodeTransportError("opencode-final-stop-missing")
    result = {"type": "result", "runtime": "opencode", "session_id": session or "",
              "subtype": "error_during_execution" if runtime_error or code else "success",
              "is_error": bool(runtime_error or code), "result": final_text}
    if runtime_error:
        result["error"] = runtime_error
        if native_opencode:
            opencode_runtime.record_model_outcome(args.model, error=runtime_error,
                                                  account_observation=account)
    elif native_opencode and session and code == 0:
        opencode_runtime.record_model_outcome(args.model, success=True,
                                              account_observation=account)
    return result, code
