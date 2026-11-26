"""Fixture shapes the registry alone does not give: what Fleet reads *besides* ``jobs.log``.

The base fixture has the operating registry size but not the operating work shape. Two of
the missing pieces live outside the registry and are added here, both synthetic, both under
the fixture root (``/tmp/hearting-e2e-*``), nothing copied from a real profile:

(b) a Codex session: a rollout lifecycle log (``<CODEX_HOME>/sessions/Y/M/D/rollout-<ts>-<sid>.jsonl``,
    <= 200 MB) held open by a marked fixture process, so Fleet's Codex collector finds it
    through ``/proc/<pid>/fd`` and a new Fleet process reads it from byte 0 (the cold
    lifecycle scan; ``_initialize_lifecycle_cursor``). Rows are mostly small ``response_item``
    tool-call rows (decoded, skipped), with ``task_started`` / ``task_complete`` / ``token_count`` /
    ``turn_context`` the readers act on.
(c) a Claude transcript per fixture session (``<CLAUDE_CONFIG_DIR>/projects/<cwd>/<sid>.jsonl``)
    with ``Agent`` tool_use / tool_result pairs, finished ones and async launches still active
    (``claude._tail_subagents`` always scans the whole file).

(a), the recently finished attempts inside Fleet's 15-minute afterglow and 361-minute stall
windows, are registry rows and are placed by ``hook_perf_fixture._history_age_s``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import shutil
import time
import uuid

from . import fixture as fx

MAX_CODEX_LOG_BYTES = 200 * 1024 * 1024
DEFAULT_CODEX_LOG_MB = 190
DEFAULT_TRANSCRIPT_MB = 4


def _iso(moment: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(moment))


def _small_row(index: int) -> str:
    """One ~130 byte ``response_item`` row (a short tool-call step), decoded and ignored by the scan.

    A real rollout is mostly such rows, not a few huge ones: the cold lifecycle scan's cost
    is per row (one decode, two observers), so this shape, not the byte count alone, makes it
    take seconds within the 200 MB cap.
    """
    stamp = _iso(1_794_000_000 + index // 50)
    if index % 2:
        payload = {"type": "function_call", "name": "shell", "call_id": f"c{index}", "arguments": "{}"}
    else:
        payload = {"type": "function_call_output", "call_id": f"c{index - 1}", "output": "ok"}
    return json.dumps({"timestamp": stamp, "type": "response_item", "payload": payload}, separators=(",", ":")) + "\n"


def write_codex_rollout(path: Path, *, cwd: str, session_id: str, megabytes: float,
                        now: float | None = None) -> dict:
    """A synthetic rollout of ~``megabytes`` MB ending in a finished task; returns its facts."""
    now = time.time() if now is None else now
    size = int(min(megabytes * 1024 * 1024, MAX_CODEX_LOG_BYTES))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    written = 0
    index = 0
    with path.open("w", encoding="utf-8") as handle:
        head = json.dumps({"timestamp": _iso(now - 7200), "type": "session_meta",
                           "payload": {"id": session_id, "cwd": cwd, "originator": "codex_cli_rs",
                                       "cli_version": "0.0.0-e2e", "source": "cli"}}) + "\n"
        handle.write(head)
        written += len(head)
        turn = 0
        while written < size:
            turn += 1
            turn_id = f"turn-e2e-{turn:05d}"
            rows = [json.dumps({"timestamp": _iso(now - 7000 + index), "type": "event_msg",
                                "payload": {"type": "task_started", "turn_id": turn_id}}) + "\n",
                    json.dumps({"timestamp": _iso(now - 7000 + index), "type": "turn_context",
                                "payload": {"turn_id": turn_id, "model": "gpt-e2e", "effort": "medium"}}) + "\n"]
            for _ in range(2000):
                index += 1
                rows.append(_small_row(index))
            rows.append(json.dumps({"timestamp": _iso(now - 7000 + index), "type": "event_msg",
                                    "payload": {"type": "token_count", "info": {
                                        "last_token_usage": {"total_tokens": 50_000 + turn},
                                        "model_context_window": 258_400}}}) + "\n")
            rows.append(json.dumps({"timestamp": _iso(now - 7000 + index), "type": "event_msg",
                                    "payload": {"type": "task_complete", "turn_id": turn_id}}) + "\n")
            chunk = "".join(rows)
            handle.write(chunk)
            written += len(chunk.encode("utf-8"))
    os.chmod(path, 0o600)
    return {"path": str(path), "bytes": path.stat().st_size, "turns": turn}


def add_codex_session(fixture: dict, *, megabytes: float = DEFAULT_CODEX_LOG_MB) -> dict:
    """Write the rollout, start the marked process that holds it open, return the session record.

    The record goes to ``fixture["codex_sessions"]`` (not ``fixture["sessions"]``: the events
    address Claude sessions); ``Children`` hands it to the shim's session discovery.
    """
    root = Path(fixture["root"])
    home = root / "codex-home"
    sid = str(uuid.UUID(int=0xC0DE_0000 + 1))
    cwd = str(Path(fixture["sessions"][0]["cwd"]))
    rollout = home / "sessions" / "2026" / "11" / "13" / f"rollout-2026-11-13T00-00-00-{sid}.jsonl"
    facts = write_codex_rollout(rollout, cwd=cwd, session_id=sid, megabytes=megabytes)
    env = {**fixture["env"], fx.MARKER_ENV: fixture["marker"]}
    handle = open(rollout, "rb")           # the process holds this file as its stdin
    proc = subprocess.Popen([shutil.which("sleep") or "/bin/sleep", "86400"], env=env, stdin=handle,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    handle.close()
    fx._SLEEPERS.append(proc)
    record = {"harness": "codex", "pid": proc.pid, "pid_start": fx._process_start(proc.pid) or "",
              "cwd": cwd, "session_id": sid, "rollout": facts["path"], "bytes": facts["bytes"]}
    fixture.setdefault("codex_sessions", []).append(record)
    fixture["env"]["CODEX_HOME"] = str(home)
    fixture["codex_home"] = str(home)
    return record


def write_claude_transcript(path: Path, *, session_id: str, megabytes: float, now: float | None = None) -> dict:
    """A Claude transcript with finished and still-active sub-agent calls."""
    now = time.time() if now is None else now
    size = int(megabytes * 1024 * 1024)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    written, index, calls = 0, 0, 0
    text = ("y" * 79 + " ") * 25
    with path.open("w", encoding="utf-8") as handle:
        while written < size:
            index += 1
            rows = []
            if index % 120 == 0:                       # an Agent call, finished or async-active
                calls += 1
                tid = f"toolu_e2e_{index:06d}"
                rows.append(json.dumps({"type": "assistant", "sessionId": session_id, "timestamp": _iso(now - 3600 + index),
                                        "message": {"role": "assistant", "content": [
                                            {"type": "tool_use", "id": tid, "name": "Agent",
                                             "input": {"subagent_type": "e2e", "prompt": "work"}}]}}) + "\n")
                body = (f"Async agent launched successfully. agentId: agent{index:06d}" if calls % 3 == 0
                        else "done")
                rows.append(json.dumps({"type": "user", "sessionId": session_id, "timestamp": _iso(now - 3590 + index),
                                        "message": {"role": "user", "content": [
                                            {"type": "tool_result", "tool_use_id": tid, "content": body}]}}) + "\n")
            role = "user" if index % 2 else "assistant"
            rows.append(json.dumps({"type": role, "sessionId": session_id, "timestamp": _iso(now - 3600 + index),
                                    "message": {"role": role, "content": text}}) + "\n")
            chunk = "".join(rows)
            handle.write(chunk)
            written += len(chunk.encode("utf-8"))
    os.chmod(path, 0o600)
    return {"path": str(path), "bytes": path.stat().st_size, "agent_calls": calls}


def add_claude_transcripts(fixture: dict, *, megabytes: float = DEFAULT_TRANSCRIPT_MB) -> dict:
    """One transcript of ``megabytes`` MB per fixture session, where ``claude.py`` looks for it."""
    from fleet.collectors.claude import _enc_cwd
    home = Path(fixture["env"]["CLAUDE_CONFIG_DIR"])
    total, calls = 0, 0
    for session in fixture["sessions"]:
        path = home / "projects" / _enc_cwd(session["cwd"]) / f"{session['session_id']}.jsonl"
        facts = write_claude_transcript(path, session_id=session["session_id"], megabytes=megabytes)
        total += facts["bytes"]
        calls += facts["agent_calls"]
    return {"transcripts": len(fixture["sessions"]), "bytes": total, "agent_calls": calls}


def describe(fixture: dict) -> dict:
    """What shape this fixture has, for the report's table (counts and sizes only)."""
    codex = fixture.get("codex_sessions") or []
    return {"codex_sessions": len(codex), "codex_log_bytes": sum(c["bytes"] for c in codex),
            "claude_transcripts": fixture.get("claude_transcripts"),
            "recent_rows_6h": fixture.get("recent_rows_6h"), "recent_rows_24h": fixture.get("recent_rows_24h")}
