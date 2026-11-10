#!/usr/bin/env python3
"""Codex start-summary switch and one-copy boundary.

Two native facts shape this helper (Codex 0.163):

* ``run_pending_session_start_hooks`` drains every queued start source in one
  loop before ``UserPromptSubmit`` and the next model request. A resumed thread
  whose first turn auto-compacts runs ``resume`` then ``compact`` back to back,
  and both additional contexts stay in that request. The compact start skips the
  summary only while the earlier start's summary is still unseen by a prompt;
  any later compact (after a prompt) needs it again and emits it.
* A shared app-server daemon builds hook processes from the daemon's own
  environment snapshot; a later ``CODEX_SESSION_MEMORY_INJECT=0 codex`` client
  environment never reaches the hook. The user-owned memory policy file is read
  at every start, so it carries the switch there. The process environment still
  wins field-for-field, as it does for the sync keys in the same file.

Every failure is silent and keeps the existing default (emit).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

FLAG = "CODEX_SESSION_MEMORY_INJECT"
POLICY_KEY = "codex_session_memory_inject"
TRUTHY = {"1", "true", "yes", "on"}
# Seconds a non-compact start's summary may wait for its prompt. The native
# drain runs the compact start immediately after it; a stale mark (a turn that
# ended before its prompt hook) must not suppress a later compact's summary.
PAIR_WINDOW_SECONDS = 60


def _policy_path(env) -> Path:
    base = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME") or Path.home()) / ".config")
    return Path(base) / "hearting" / "memory-sync.json"


def _policy_value(env) -> str | None:
    path = _policy_path(env)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get(POLICY_KEY) if isinstance(data, dict) else None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, str):
        return value.strip()
    return None


def inject_enabled(env=None) -> bool:
    """Environment first, then the shared policy file; absent in both means on."""
    env = os.environ if env is None else env
    if FLAG in env:
        return env[FLAG].lower() in TRUTHY
    value = _policy_value(env)
    if value is None:
        return True
    return value.lower() in TRUTHY


def _mark_path(sid: str, env) -> Path:
    base = env.get("XDG_STATE_HOME") or str(Path(env.get("HOME") or Path.home()) / ".local" / "state")
    digest = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:32]
    return Path(base) / "hearting" / "codex-start-memory" / f"{digest}.json"


def summary_due(sid: str, source: str, *, env=None, now=None) -> bool:
    """False only for a compact start that follows an unprompted start summary."""
    if not sid or source != "compact":
        return True
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    try:
        mark = json.loads(_mark_path(sid, env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(mark, dict) or mark.get("source") in (None, "compact"):
        return True
    at = mark.get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return True
    return not (0 <= now - at <= PAIR_WINDOW_SECONDS)


def note_start(sid: str, source: str, emitted: bool, *, env=None, now=None) -> None:
    """Record the start whose summary the next request will carry, if any."""
    if not sid:
        return
    env = os.environ if env is None else env
    path = _mark_path(sid, env)
    try:
        if not emitted:
            # A skipped compact keeps the earlier mark: that summary is the one
            # still waiting for this request.
            if source != "compact":
                path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(f".{path.name}.{os.getpid()}")
        tmp.write_text(json.dumps({"source": source or "", "at": time.time() if now is None else now}),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def note_prompt(sid: str, *, env=None) -> None:
    """A prompt reached the model with the summary; later compacts need it again."""
    if not sid:
        return
    try:
        _mark_path(sid, os.environ if env is None else env).unlink(missing_ok=True)
    except OSError:
        pass
