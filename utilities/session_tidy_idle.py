#!/usr/bin/env python3
"""Normalize real harness idle signals into one bounded session-tidy booking."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any

try:
    import tomllib
except ImportError:  # Python 3.10 deployments without the optional tomli wheel stay unknown.
    tomllib = None

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))
if str(ROOT / "utilities") not in sys.path:
    sys.path.insert(0, str(ROOT / "utilities"))


def _walk(payload: dict, *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    for name in ("context", "workspace", "session", "payload", "event", "input", "data", "properties"):
        child = payload.get(name)
        if isinstance(child, dict):
            value = _walk(child, *keys)
            if value:
                return value
    return ""


def _safe_tokens(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return int(value)


def _transcript_identity(path: str, sid: str) -> str:
    if not path:
        return ""
    target = Path(path)
    try:
        info = target.lstat()
        if not target.is_file() or target.is_symlink():
            return ""
        with target.open("rb") as handle:
            handle.seek(max(0, info.st_size - 8192))
            tail = handle.read(8192)
        stamp = f"{sid}\0{info.st_dev}:{info.st_ino}:{info.st_size}\0".encode()
        return hashlib.sha256(stamp + tail).hexdigest()
    except OSError:
        return ""


def _opencode_latest(sid: str) -> tuple[int | None, str, str, str]:
    """Read only the latest completed assistant request for this exact OpenCode session."""
    try:
        import tidy_transcripts
        database = Path(tidy_transcripts.opencode_db_path())
        if database.is_symlink() or not database.is_file():
            return None, "", "", ""
        uri = database.as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=0.2)
        try:
            rows = con.execute(
                "SELECT id,data FROM message WHERE session_id=? ORDER BY time_updated DESC LIMIT 50", (sid,))
            for message_id, raw in rows:
                try:
                    payload = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(payload, dict) or payload.get("role") != "assistant":
                    continue
                moment = payload.get("time") if isinstance(payload.get("time"), dict) else {}
                if not payload.get("finish") and not moment.get("completed"):
                    continue
                tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else {}
                # OpenCode exposes one total context count on some providers and split
                # input/cache counts on others. Never add the same cache field twice.
                total = _safe_tokens(tokens.get("total"))
                if total is None:
                    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
                    if not any(key in tokens for key in ("input",)) and not any(
                            key in cache for key in ("read", "write")):
                        return None, "", "", str(message_id or payload.get("id") or "")
                    parts = [_safe_tokens(tokens.get("input", 0)), _safe_tokens(cache.get("read", 0)),
                             _safe_tokens(cache.get("write", 0))]
                    if any(part is None for part in parts):
                        return None, "", "", str(message_id or payload.get("id") or "")
                    total = sum(parts)
                return (total, str(payload.get("providerID") or ""),
                        str(payload.get("modelID") or ""), str(message_id or payload.get("id") or ""))
        finally:
            con.close()
    except (OSError, sqlite3.Error, ValueError, TypeError):
        pass
    return None, "", "", ""


def _finished_text(payload: dict, parts: list) -> str:
    moment = payload.get("time") if isinstance(payload.get("time"), dict) else {}
    if payload.get("finish") != "stop" or not moment.get("completed") or payload.get("error"):
        return ""
    return "\n".join(part["text"] for part in parts
                     if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
                     and not part.get("synthetic") and not part.get("ignored"))


def _opencode_last_reply(sid: str) -> str:
    """Text of this session's newest message when it is an assistant reply that finished normally.

    OpenCode 2 writes ``session_message`` rows (content inline); older stores write
    ``message`` + ``part``. The newer table is preferred when it exists."""
    try:
        import tidy_transcripts
        database = Path(tidy_transcripts.opencode_db_path())
        if database.is_symlink() or not database.is_file():
            return ""
        con = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.2)
        try:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "session_message" in tables:
                row = con.execute("SELECT type,data FROM session_message WHERE session_id=? AND type IN "
                                  "('user','assistant') ORDER BY time_created DESC, id DESC LIMIT 1",
                                  (sid,)).fetchone()
                if row and row[0] == "assistant":
                    payload = json.loads(row[1])
                    if isinstance(payload, dict) and isinstance(payload.get("content"), list):
                        return _finished_text(payload, payload["content"])
                if row:
                    return ""
            if "message" not in tables:
                return ""
            row = con.execute("SELECT id,data FROM message WHERE session_id=? ORDER BY time_created DESC, id DESC "
                              "LIMIT 1", (sid,)).fetchone()
            if not row:
                return ""
            payload = json.loads(row[1])
            if not isinstance(payload, dict) or payload.get("role") != "assistant":
                return ""
            parts = [json.loads(raw) for (raw,) in con.execute(
                "SELECT data FROM part WHERE message_id=? ORDER BY id", (row[0],))]
            return _finished_text(payload, parts)
        finally:
            con.close()
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
        return ""


def last_reply(harness: str, payload: dict, sid: str) -> str:
    """The turn's final assistant text: Claude/Codex Stop input, OpenCode's finished message."""
    if harness in ("claude", "codex"):
        value = payload.get("last_assistant_message")
        return value if isinstance(value, str) else ""
    return _opencode_last_reply(sid)


def _codex_provider() -> str:
    if tomllib is None:
        return "unknown"
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        value = config.get("model_provider")
        return value if isinstance(value, str) and value else "unknown"
    except (OSError, ValueError):
        return "unknown"


def _tokens_and_provider(harness: str, payload: dict, sid: str, transcript: str):
    if harness == "claude":
        from context_alert import claude_usage
        tokens = claude_usage(transcript, sid)
        provider = _walk(payload, "provider", "provider_id", "providerID")
        if not provider:
            provider = "bedrock" if os.environ.get("CLAUDE_CODE_USE_BEDROCK") else (
                "vertex" if os.environ.get("CLAUDE_CODE_USE_VERTEX") else "unknown")
        return tokens, provider, _walk(payload, "model", "model_id", "modelID")
    if harness == "codex":
        from context_alert import codex_usage
        if not transcript:
            try:
                from fleet.token_budget import find_codex_rollout
                home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
                transcript, reason = find_codex_rollout(home, sid)
                if reason != "ok":
                    transcript = ""
            except Exception:
                transcript = ""
        return codex_usage(transcript, sid), _codex_provider(), _walk(payload, "model", "model_id", "modelID")
    tokens, provider, model, _message_id = _opencode_latest(sid)
    return tokens, provider or _walk(payload, "provider", "provider_id", "providerID"), \
        model or _walk(payload, "model", "model_id", "modelID")


def observe_payload(harness: str, payload: dict, *, now: float | None = None,
                    lock_wait: float | None = None) -> str:
    """Accept one actual Stop/session.idle payload; unknown usage never books a clear."""
    if harness not in ("claude", "codex", "opencode") or not isinstance(payload, dict):
        return "booking=skipped reason=invalid-payload"
    if harness == "claude" and payload.get("hook_event_name") not in (None, "Stop"):
        return "booking=skipped reason=wrong-event"
    if harness == "opencode":
        event = payload.get("event") or payload.get("type")
        if event and event not in ("session.idle", "idle"):
            return "booking=skipped reason=wrong-event"
    sid = _walk(payload, "session_id", "sessionID", "thread_id", "threadID")
    cwd = _walk(payload, "cwd", "working_directory", "workingDirectory") or os.getcwd()
    transcript = _walk(payload, "transcript_path", "transcriptPath", "transcript")
    if not sid:
        return "booking=skipped reason=session-unknown"
    from session_tidy import is_worker
    if is_worker() or os.environ.get("AGENT_SESSION_ROLE", "").lower() in {
            "owner", "worker", "auxiliary", "reviewer"}:
        return "booking=skipped reason=worker"
    # This observation is independent of idle usage/compact booking. A lock
    # contention or unknown usage never prevents the marked wait from appearing.
    try:
        import human_wait_state
        import session_tidy as tidy
        seat = tidy.resolve_seat(harness, cwd, os.environ, sid)
        current_sequence = tidy.read_prompt_seq(seat)
        expected_sequence = payload.get("_answer_wait_prompt_seq",
                                       os.environ.get("HEARTING_ANSWER_WAIT_PROMPT_SEQ", current_sequence))
        expected_sequence = int(expected_sequence)
        signal = human_wait_state.signal_value(
            harness, sid, seat.key, last_reply(harness, payload, sid),
            sequence=expected_sequence, now=now)
        if signal is not None:
            registered = False
            fallback = False
            try:
                # Prompt closure uses this same lock. Keep the sequence fence and
                # private atomic write together, and leave subprocess work outside.
                with tidy.seat_lock(seat.key, wait=0.1):
                    if tidy.read_prompt_seq(seat) == expected_sequence:
                        registered = human_wait_state.write_signal(signal)
            except BlockingIOError:
                # A busy seat must not discard a real wait. Write optimistically,
                # then retract only our own signal if prompt closure won the race.
                fallback = True
                if human_wait_state.write_signal(signal):
                    if tidy.read_prompt_seq(seat) == expected_sequence:
                        registered = True
                    else:
                        try:
                            with tidy.seat_lock(seat.key, wait=0.05):
                                if tidy.read_prompt_seq(seat) != expected_sequence:
                                    human_wait_state.remove_signal_if_current(signal)
                        except BlockingIOError:
                            human_wait_state.remove_signal_if_current(signal)
            if registered:
                current = human_wait_state.signal_is_current(signal)
                sequence_matches = (not fallback or
                                    tidy.read_prompt_seq(seat) == expected_sequence)
                if current and sequence_matches:
                    # A prompt can still close after this check and before herdr starts; at most one silent registration notice is lost.
                    human_wait_state.notify_signal(signal)
                elif current:
                    human_wait_state.remove_signal_if_current(signal)
    except Exception:
        pass
    from session_tidy import accept_reply_card, reconcile_compact_frontier
    try:
        # A card written as the reply to an automatic tidy request is taken before the
        # compact/idle bookings below look at this turn; an ordinary reply changes nothing.
        accept_reply_card(harness, sid, cwd, last_reply(harness, payload, sid))
    except Exception:  # noqa: BLE001 - optional; the request simply stays unanswered
        pass
    compact_result = reconcile_compact_frontier(harness, sid, cwd, now=now, lock_wait=lock_wait)
    if compact_result and compact_result not in ("booking=skipped reason=new-input",
                                                  "booking=skipped reason=card-changed"):
        return compact_result
    tokens, provider, model = _tokens_and_provider(harness, payload, sid, transcript)
    tokens = _safe_tokens(tokens) if tokens is not None else None
    if tokens is None:
        return "booking=skipped reason=usage-unknown"
    event_id = _walk(payload, "turn_id", "turnID", "message_id", "messageID", "assistant_id")
    if not event_id:
        event_id = _transcript_identity(transcript, sid)
    if not event_id and harness == "opencode":
        _tokens, _provider, _model, event_id = _opencode_latest(sid)
    if not event_id:
        return "booking=skipped reason=turn-identity-unknown"
    from session_tidy_clear import observe_idle_event
    try:
        return observe_idle_event(harness, sid, cwd, event_id=event_id, provider=provider,
                                  model=model, tokens=tokens, now=now, lock_wait=lock_wait)
    except (BlockingIOError, TimeoutError):
        return "booking=skipped reason=seat-busy"


def launch_observer(harness: str, payload: dict, *, environ=None, popen=None) -> bool:
    """Start the optional observer as a detached, worker-scrubbed child and return immediately."""
    environ = os.environ if environ is None else environ
    if harness not in ("claude", "codex", "opencode") or not isinstance(payload, dict):
        return False
    from session_tidy import is_worker
    if is_worker(environ) or str(environ.get("AGENT_SESSION_ROLE", "")).lower() in {
            "owner", "worker", "auxiliary", "reviewer"}:
        return False
    payload_fd = None
    try:
        import session_tidy as tidy
        sid = _walk(payload, "session_id", "sessionID", "thread_id", "threadID")
        cwd = _walk(payload, "cwd", "working_directory", "workingDirectory") or os.getcwd()
        seat = tidy.resolve_seat(harness, cwd, environ, sid)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > 48 * 1024:
            return False
        memfd = getattr(os, "memfd_create", None)
        if memfd is None:
            return False
        payload_fd = memfd("hearting-idle-observer", getattr(os, "MFD_CLOEXEC", 0))
        view = memoryview(body)
        while view:
            written = os.write(payload_fd, view)
            view = view[written:]
        os.lseek(payload_fd, 0, os.SEEK_SET)
        from session_tidy_runner import clean_env
        child_env = clean_env(environ)
        child_env["HEARTING_ANSWER_WAIT_PROMPT_SEQ"] = str(tidy.read_prompt_seq(seat))
        # A pane id is only a lookup hint and is revalidated by the normal seat resolver.
        for key in ("HERDR_PANE_ID", "HERDR_SOCKET_PATH"):
            if environ.get(key):
                child_env[key] = str(environ[key])
        command = [sys.executable, str(Path(__file__).resolve()), "--harness", harness]
        spawn = subprocess.Popen if popen is None else popen
        spawn(command, stdin=payload_fd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True, pass_fds=(payload_fd,), start_new_session=True,
              env=child_env, cwd=str(ROOT))
        return True
    except (OSError, subprocess.SubprocessError, BrokenPipeError, ValueError, TypeError):
        return False
    finally:
        if payload_fd is not None:
            try:
                os.close(payload_fd)
            except OSError:
                pass


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="session_tidy_idle.py")
    parser.add_argument("--harness", choices=("claude", "codex", "opencode"), required=True)
    parser.add_argument("--detach", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if args.detach:
        launch_observer(args.harness, payload)
        return 0
    result = observe_payload(args.harness, payload, lock_wait=0.05)
    if result:
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
