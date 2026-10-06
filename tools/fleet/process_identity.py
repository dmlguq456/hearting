"""Which harness session a process is, in the one identity record shape.

Each harness proves a process's session from its own native source -- the
translation stays in its collector:

- Claude: `~/.claude/sessions/<pid>.json`, else the statusline tap matched by
  pid and start time (`collectors.claude.session_id_of_process`);
- Codex: the rollout the process holds open, the managed registry, or the
  start-time match (`collectors.codex.session_id_of_process`);
- OpenCode: the pane's own TUI selection record
  (`collectors.opencode.session_of_process`). The `--session` a pane was
  started with is reported as `started-on`, not proof: the TUI can switch
  sessions after start, and only the selection record follows that.

The answer is a `session_identity.SessionIdentity` -- the same record the
environment reading gives -- with `confidence="proven"` when the process
proved its session and `"harness-only"` when only the harness is known. The
caller decides what an unproven session id may do; nothing here guesses.

Import contract: like `route_chain`, importable with only `tools` on sys.path;
`utilities/` is added lazily.
"""
from __future__ import annotations

from pathlib import Path
import sys

PROVEN = "proven"
STARTED_ON = "started-on"
HARNESS_ONLY = "harness-only"
_UNPROVEN_SOURCES = {"opencode-argv": STARTED_ON}


def _record():
    utilities = str(Path(__file__).resolve().parents[2] / "utilities")
    if utilities not in sys.path:
        sys.path.insert(0, utilities)
    from session_identity import SessionIdentity
    return SessionIdentity


def process_identity(pid, harness, *, live_codex=None):
    """The identity record `harness`'s own source proves for `pid`."""
    record = _record()
    session_id, source = None, ""
    try:
        if harness == "claude":
            from fleet.collectors import claude
            session_id, source = claude.session_id_of_process(pid), "claude-session-registry"
        elif harness == "codex":
            from fleet.collectors import codex
            session_id, source = codex.session_id_of_process(pid, live_codex), "codex-process"
        elif harness == "opencode":
            from fleet.collectors import opencode
            session_id, source = opencode.session_of_process(pid)
    except Exception:
        session_id = None
    if session_id:
        return record(harness, session_id, source, _UNPROVEN_SOURCES.get(source, PROVEN))
    return record(harness, "", "", HARNESS_ONLY)
