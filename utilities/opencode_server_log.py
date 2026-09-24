#!/usr/bin/env python3
"""Read the OpenCode server log for the one error line a missing attempt
result cannot itself carry.

A detached OpenCode attempt that leaves no result envelope (crash, kill,
truncated pipe) has nothing left in its own exec log to explain why. The
OpenCode CLI keeps a separate, durable, per-session server log outside that
attempt's own output; this module is the one place that binds an attempt to
its exact session and reads that log for a session-scoped ERROR line.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any

from opencode_session_runtime import SESSION_ID as _SESSION_ID_RE


_TAIL_BYTES = 8 * 1024 * 1024


def _tail_lines(path: Path) -> list[str]:
    try:
        size = path.stat().st_size
    except OSError:
        return []
    start = max(0, size - _TAIL_BYTES)
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if start and lines:
        # A byte-range read starting mid-file almost certainly begins inside
        # a truncated line -- drop it rather than feed a broken prefix to
        # json.loads (it would just fail to parse anyway, but this is exact
        # about why, matching dispatch_supervisor_terminal._tail_rows).
        lines = lines[1:]
    return [line.decode("utf-8", "replace") for line in lines if line.strip()]


def bind_session(log_file: str | Path | None) -> str | None:
    """Return the attempt's own sessionID from its exec log.

    The attempt's exec log is the raw `opencode run --format json` stream
    (or the nested claude-session-supervisor-driven equivalent), where every
    row carries a top-level `sessionID`. Returns that value only when the
    whole log names exactly one -- two or more is exactly the ambiguity a
    caller must not guess through, so it returns None.
    """
    if not log_file:
        return None
    found: set[str] = set()
    for line in _tail_lines(Path(log_file)):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        session = value.get("sessionID")
        if isinstance(session, str) and _SESSION_ID_RE.fullmatch(session):
            found.add(session)
            if len(found) > 1:
                return None
    return next(iter(found)) if len(found) == 1 else None


def _candidate_server_logs(metadata: dict[str, Any]) -> list[Path]:
    """Nested runtime path first, then the inherited XDG path (plan order).

    A nested dispatch gets its own `XDG_DATA_HOME` under the worktree
    (`adapters/opencode/bin/dispatch-headless.py:prepare_nested_runtime`); a
    plain foreground/legacy attempt shares the caller's inherited one.
    """
    candidates: list[Path] = []
    worktree = metadata.get("worktree")
    attempt_id = metadata.get("attempt_id")
    if worktree and attempt_id:
        candidates.append(
            Path(worktree) / ".dispatch" / "opencode-runtime" / str(attempt_id)
            / "data" / "opencode" / "log" / "opencode.log"
        )
    xdg_data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    candidates.append(Path(xdg_data) / "opencode" / "log" / "opencode.log")
    return candidates


def _parse_timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # OpenCode's own logger uses millisecond epoch; no plausible
        # second-epoch value is this large.
        return value / 1000.0 if value > 10_000_000_000 else float(value)
    if isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None


def _matching_error(path: Path, session_id: str, started_at: float | None) -> str | None:
    # Most-recent-first: the last matching ERROR line is the one closest to
    # the attempt's actual end, and is what a human would check first.
    for line in reversed(_tail_lines(path)):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if str(row.get("level", "")).upper() != "ERROR":
            continue
        session = row.get("session")
        if not isinstance(session, dict) or session.get("id") != session_id:
            continue
        if started_at is not None:
            observed = _parse_timestamp(
                row.get("time") or row.get("timestamp") or row.get("ts")
            )
            if observed is not None and observed < started_at:
                continue
        error = row.get("error")
        message = error.get("error") if isinstance(error, dict) else None
        if isinstance(message, str) and message:
            return message
    return None


def session_error(metadata: dict[str, Any]) -> tuple[str, Path] | None:
    """Find the session-scoped ERROR line for this attempt, or None.

    Exact session binding only (`bind_session`): an attempt log naming zero
    or more than one sessionID cannot be matched to one causal error line, so
    this returns None rather than guess (LOOP §4). `metadata` must carry
    `log_file` (the attempt's own exec log), `worktree` and `attempt_id`
    (nested-path candidate), and should carry `started_at` (an ISO-8601 or
    epoch timestamp) to exclude a stale error from a prior session reusing
    the same log file.
    """
    session_id = bind_session(metadata.get("log_file"))
    if not session_id:
        return None
    started_at = _parse_timestamp(metadata.get("started_at"))
    for candidate in _candidate_server_logs(metadata):
        if not candidate.is_file():
            continue
        message = _matching_error(candidate, session_id, started_at)
        if message is not None:
            return message, candidate
    return None
