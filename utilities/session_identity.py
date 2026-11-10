#!/usr/bin/env python3
"""Who is this session: the one reading of the harness identity variables.

Each harness exports its own native session id to the commands it runs:
Claude `CLAUDE_CODE_SESSION_ID` (older builds `CLAUDE_SESSION_ID`), Codex
`CODEX_THREAD_ID` (older builds `CODEX_SESSION_ID`), OpenCode
`OPENCODE_SESSION_ID`. A command may also be told which harness it runs under
(`AGENT_DISPATCH_CALLER_HARNESS`, then `AGENT_DISPATCH_CURRENT_HARNESS`).

The same question used to be answered by about a dozen local copies, each with
its own variable list, order, and answer when two harnesses' ids were both
inherited (refuse, take the first, or say nothing). This module answers it once
and reports how sure the answer is; callers keep only their own policy on top
(a launch refuses an ambiguous caller, a label falls back to "operator").

`identity()` never raises. Its `confidence` is:

- `named`: a harness was named and its id (possibly empty) is read from that
  harness's own variables.
- `sole`: no harness was named and exactly one harness's id is set.
- `ambiguous`: no harness was named and two or more harnesses' ids are set.
- `invalid`: the named harness is not one of the three.
- `none`: nothing is set.

A command run by a native sub-session (a subagent its session spawned) is
answered with the root session it belongs to, as a Claude subagent already is:
Claude exports the root's id to its subagents, Codex records the spawn in its
state DB (`thread_spawn_edges`), and the OpenCode plugin exports the root id
beside the native one (`harness-capabilities.json`
`session_identity.sub_session_root`). The sub-session's own id stays in
`native_session_id`. A dispatch it starts, a report it sends and a duty it
records then name the session the user talks to, which is the one woken (PG-49).
An unreadable spawn record leaves the native id.
"""
from __future__ import annotations

from dataclasses import dataclass
import functools
import os
import re
import sqlite3


@functools.lru_cache(maxsize=1)
def session_env() -> dict[str, tuple[str, ...]]:
    """Per harness, the variables that carry its native session id, current name first.

    Each adapter declares them (`harness-capabilities.json` `session_identity.env`);
    read on first use, so importing this module never depends on the declarations.
    """
    from harness_capabilities import session_env as declared
    return declared()


def __getattr__(name):
    if name == "SESSION_ENV":
        return session_env()
    raise AttributeError(name)
# Variables that name the harness a command runs under, strongest first.
HARNESS_ENV = ("AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS")
KNOWN = frozenset(("named", "sole"))


@dataclass(frozen=True)
class SessionIdentity:
    harness: str = ""
    session_id: str = ""
    source: str = ""  # the variable the answer came from; several, comma-joined, when ambiguous
    confidence: str = "none"
    native_session_id: str = ""  # the sub-session's own id when `session_id` is its root

    @property
    def known(self) -> bool:
        return self.confidence in KNOWN


def session_ids(environ=None) -> dict[str, tuple[str, str]]:
    """`{harness: (session_id, variable)}` for every harness whose id is set."""
    env = os.environ if environ is None else environ
    found = {}
    for harness, names in session_env().items():
        for name in names:
            value = env.get(name)
            if value:
                found[harness] = (value, name)
                break
    return found


# A sub-session spawns sub-sessions only a few levels deep; the bound keeps a
# corrupt record that loops from walking forever.
ROOT_WALK_LIMIT = 16
_ROOTS: dict[tuple[str, str], str] = {}


def _codex_state_db(home: str) -> str | None:
    """The newest versioned Codex state DB in ``home`` (as the Fleet collector reads it)."""
    try:
        names = os.listdir(home)
    except OSError:
        return None
    found = [(int(match.group(1)), name) for name in names
             if (match := re.fullmatch(r"state_(\d+)\.sqlite", name))]
    return os.path.join(home, max(found)[1]) if found else None


def _codex_root(session_id: str, env) -> str:
    """The root thread of a Codex `spawn_agent` thread, else ``session_id``."""
    home = env.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    key = (home, session_id)
    if key in _ROOTS:
        return _ROOTS[key]
    db = _codex_state_db(home)
    if not db:
        return session_id
    current = session_id
    try:
        connection = sqlite3.connect("file:" + db + "?mode=ro", uri=True, timeout=0.5)
        try:
            for _ in range(ROOT_WALK_LIMIT):
                parents = {row[0] for row in connection.execute(
                    "SELECT parent_thread_id FROM thread_spawn_edges WHERE child_thread_id = ?",
                    (current,)) if isinstance(row[0], str) and row[0]}
                if len(parents) != 1:
                    break  # a root, or an ambiguous record: stop at what is known
                parent = parents.pop()
                if parent in (current, session_id):
                    break
                current = parent
        finally:
            connection.close()
    except sqlite3.Error:
        return session_id
    if current != session_id:
        _ROOTS[key] = current  # a thread's root never changes; a root may still gain an edge
    return current


def sub_session_root(harness: str, session_id: str, environ=None) -> str:
    """The root session of ``session_id`` under ``harness``; ``session_id`` itself for a root."""
    env = os.environ if environ is None else environ
    if not session_id:
        return session_id
    try:
        from harness_capabilities import sub_session_roots
        how = sub_session_roots().get(harness, "shared-id")
    except Exception:
        return session_id
    if how == "spawn-edges":
        return _codex_root(session_id, env)
    if how.startswith("env:"):
        return env.get(how[4:]) or session_id
    return session_id


def _rooted(found: SessionIdentity, env) -> SessionIdentity:
    root = sub_session_root(found.harness, found.session_id, env)
    if not root or root == found.session_id:
        return found
    return SessionIdentity(found.harness, root, found.source, found.confidence,
                           native_session_id=found.session_id)


def identity(environ=None) -> SessionIdentity:
    env = os.environ if environ is None else environ
    return _rooted(_native_identity(env), env)


def _native_identity(env) -> SessionIdentity:
    sessions = session_ids(env)
    for name in HARNESS_ENV:
        named = env.get(name)
        if not named:
            continue
        if named not in session_env():
            return SessionIdentity(source=name, confidence="invalid")
        session_id, variable = sessions.get(named, ("", ""))
        return SessionIdentity(named, session_id, variable or name, "named")
    if len(sessions) > 1:
        return SessionIdentity(source=",".join(variable for _sid, variable in sessions.values()),
                               confidence="ambiguous")
    if sessions:
        harness, (session_id, variable) = next(iter(sessions.items()))
        return SessionIdentity(harness, session_id, variable, "sole")
    return SessionIdentity()


def session_label(environ=None, default: str = "operator") -> str:
    """A session id to record as who did something; never a check.

    The known identity's id when there is one, else the first id set in the
    fixed claude, codex, opencode order, else `default`.
    """
    found = identity(environ)
    if found.known and found.session_id:
        return found.session_id
    sessions = session_ids(environ)
    return next((session_id for session_id, _variable in sessions.values()), default)
