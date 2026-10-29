"""Which harness session a process is, in the one identity record shape.

Each harness proves a process's session from its own native source -- the
translation stays in its collector:

- Claude: `~/.claude/sessions/<pid>.json`, else the statusline tap matched by
  pid and start time (`collectors.claude.session_id_of_process`);
- Codex: the rollout the process holds open, or the root thread whose
  `codex_tui` MCP origin is listening on that TUI PID
  (`collectors.codex.session_id_of_process`);
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

import os
import math
from pathlib import Path
import sys

PROVEN = "proven"
STARTED_ON = "started-on"
HARNESS_ONLY = "harness-only"
_UNPROVEN_SOURCES = {"opencode-argv": STARTED_ON}


def process_role(*, tty=None, registered=False, session_backend=False, native_session=False,
                 invocation="unknown"):
    """One role from native invocation and observed ownership, without name exceptions.

    Native command mode is an adapter translation, not an inherited environment hint.
    Unknown reads and not-yet-published headless conversations remain unknown.
    """
    if session_backend:
        return "session-backend"
    if invocation == "command" and not native_session:
        return "service"
    if registered:
        return "registered-worker"
    if native_session:
        return "conversation"
    if tty and tty != "?":
        return "conversation"
    return "unknown"


def resolve_session_claims(claims, successors=None, *, current_source=None):
    """Resolve exact claims through directed conversation continuity, never recency.

    Adapters validate PID/start before supplying claims. A native current record
    distinguishes its current selection from historical taps and pane labels.
    It also follows a resume back to an earlier conversation. Without it,
    claims must agree through directed continuity.
    """
    claims = {source: sid for source, sid in claims.items() if sid}
    ids = set(claims.values())
    successors = successors or {}

    def reaches(origin, target):
        pending, seen = [origin], set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current not in seen:
                seen.add(current)
                pending.extend(successors.get(current, ()))
        return False

    historical = {}
    origin = claims.get(current_source) if current_source else None
    if origin:
        historical = {source: sid for source, sid in claims.items() if sid != origin}
        ids = {origin}
    current = [sid for sid in ids if all(reaches(other, sid) for other in ids)]
    sid = current[0] if len(current) == 1 else None
    evidence = {"verdict": "resolved" if sid else "conflict" if ids else "unobserved",
                "claims": claims, "session_id": sid}
    if origin:
        evidence.update(current_source=current_source, historical_claims=historical)
    return sid, evidence


def pane_process_claims(sessions, panes, bindings, *, complete=True):
    """Consume the existing foreground pane observation; never issue another probe."""
    from .collectors import procscan
    out = {}
    for s in sessions:
        pane_ids = bindings.get(s.pid, set())
        if (not s.proc_start or s.is_child or s.app_server
                or procscan.read_proc_start(s.pid) != s.proc_start):
            continue
        if len(pane_ids) != 1:
            candidates = [p for p in panes or [] if p.get("agent") == s.harness
                          and p.get("cwd") and s.cwd
                          and os.path.realpath(p["cwd"]) == os.path.realpath(s.cwd)]
            if not complete and (candidates or panes is None and s.herdr_attached is True):
                out[s.pid] = {"verdict": "unobserved", "proc_start": s.proc_start}
            continue
        pane_id = next(iter(pane_ids))
        values = set()
        for p in panes or []:
            native = p.get("agent_session")
            if (p.get("pane_id") == pane_id and p.get("agent") == s.harness
                    and isinstance(native, dict) and native.get("agent") == s.harness
                    and isinstance(native.get("value"), str) and native["value"]):
                values.add(native["value"])
        if len(values) == 1:
            out[s.pid] = {"session_id": values.pop(), "pane": pane_id, "proc_start": s.proc_start}
    return out


def pane_session_successors(harness, pane, cwd, *, claims=None):
    """Directed native clear transitions from the existing seat ledger.

    Display aliases are bidirectional and cannot supply these edges. Only a
    recorded clear start follows the previous native start on that same seat.
    """
    _record()  # make the common utilities importable
    import session_tidy
    from .gitinfo import resolve_gitdir
    repository = resolve_gitdir(cwd)[1]
    if not repository:
        return {}
    seat = session_tidy._pane_seat_of(pane)
    summary = session_tidy.session_summary(seat)
    rows = [row for row in session_tidy._read_ledger_lines(seat)
            if row.get("harness") == harness and row.get("sid")]
    def timestamp(value):
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) \
            and math.isfinite(value) and value > 0 else None

    starts = [dict(row, source=row.get("source") if row.get("event") == "start"
                   else row["start_source"], start_at=row.get("ts") if row.get("event") == "start"
                   else row.get("start_at")) for row in rows
              if row.get("event") == "start" or row.get("event") == "summary" and row.get("start_source")]
    starts = sorted((row for row in starts if timestamp(row.get("start_at")) is not None),
                    key=lambda row: row["start_at"])
    edges = {}
    def same_repository(row):
        return resolve_gitdir((summary.get((harness, row["sid"])) or {}).get("cwd"))[1] == repository

    for older, newer in zip(starts, starts[1:]):
        same_repo = all(same_repository(row) for row in (older, newer))
        if (same_repo and newer.get("source") == "clear" and older["sid"] != newer["sid"]
                and older["start_at"] < newer["start_at"]):
            edges.setdefault(older["sid"], set()).add(newer["sid"])
    # Older folds kept the seat's ordered session history but dropped START
    # sources. A later explicit clear closes those historical claims on this
    # same seat/repository; neither a startup/fork nor a display alias does so.
    folded = [row for row in rows if row.get("event") == "summary" and not row.get("start_source")
              and claims is not None and row["sid"] in claims]
    for row in starts:
        if row.get("source") == "clear" and same_repository(row):
            for older in folded:
                last_seen = timestamp((summary.get((harness, older["sid"])) or {}).get("last_seen"))
                if (older["sid"] != row["sid"] and same_repository(older)
                        and last_seen is not None and last_seen < row["start_at"]):
                    edges.setdefault(older["sid"], set()).add(row["sid"])
    return edges


def finalize_process_roles(sessions, jobs):
    """Publish one role after native identities and registered process rows are observed."""
    from .collectors import procscan, herdr
    from . import session_registry
    for s in sessions:
        native = session_registry.read(s.harness, s.pid)
        proven = bool(native and native.get("sessionId") and s.proc_start
                      and str(native.get("procStart") or "") == s.proc_start)
        evidence = s.session_identity_evidence or {}
        if s.harness == "claude":
            observed = procscan.claude_process_identity.observe_process(s.pid)
            if (observed and observed["proc_start"] == s.proc_start
                    and observed["kind"] == "service"):
                s.process_role = "service"
                continue
            proven = evidence.get("verdict") == "resolved"
        elif s.harness == "codex":
            proven = proven or bool(s.session_id and getattr(s, "_fd_owner", False))
        elif s.harness == "opencode" and not proven and getattr(s, "_tty", None) == "?":
            from .collectors import opencode
            _sid, source = opencode.session_of_process(s.pid)
            proven = bool(_sid and source == "opencode-tui-selection")
        registered = False
        if s.attempt_id and getattr(s, "_invocation_mode", "unknown") == "session":
            for j in jobs:
                if (j.attempt_id == s.attempt_id and j.harness == s.harness and j.pid and j.proc_start
                        and procscan.read_proc_start(j.pid) == j.proc_start
                        and procscan.read_proc_start(s.pid) == s.proc_start
                        and herdr.pid_in_panes(s.pid, set(), {j.pid}, max_depth=16)):
                    registered = True
                    break
        s.process_role = process_role(
            tty=getattr(s, "_tty", None), registered=registered,
            session_backend=bool(s.app_server and s.managed_dir), native_session=proven,
            invocation=getattr(s, "_invocation_mode", "unknown"))
    sessions[:] = [s for s in sessions if s.process_role != "service"]


def _record():
    utilities = str(Path(__file__).resolve().parents[2] / "utilities")
    if utilities not in sys.path:
        sys.path.insert(0, utilities)
    from session_identity import SessionIdentity
    return SessionIdentity


def process_identity(pid, harness, *, live_codex=None, codex_observation=None):
    """The identity record `harness`'s own source proves for `pid`."""
    record = _record()
    session_id, source = None, ""
    try:
        if harness == "claude":
            from .collectors import claude, procscan
            observed = procscan.claude_process_identity.observe_process(pid)
            if observed and observed["kind"] == "service":
                return record(harness, "", "", HARNESS_ONLY)
            session_id, source = claude.session_id_of_process(pid), "claude-session-registry"
        elif harness == "codex":
            from .collectors import codex
            session_id, source = codex.session_id_of_process(
                pid, live_codex, observation=codex_observation), "codex-process"
        elif harness == "opencode":
            from .collectors import opencode
            session_id, source = opencode.session_of_process(pid)
    except Exception:
        session_id = None
    if session_id:
        return record(harness, session_id, source, _UNPROVEN_SOURCES.get(source, PROVEN))
    return record(harness, "", "", HARNESS_ONLY)


def _proc_stat(pid):
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as handle:
            raw = handle.read(8192)
        fields = raw[raw.rindex(b") ") + 2:].split()
        return int(fields[1]), fields[19].decode("ascii")
    except (OSError, ValueError, IndexError, UnicodeDecodeError):
        return None, None


def _proc_argv(pid, *, max_bytes=32768):
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as handle:
            raw = handle.read(max_bytes + 1)
    except (OSError, ValueError):
        return None
    if not raw or len(raw) > max_bytes:
        return None
    argv = [os.fsdecode(value) for value in raw.rstrip(b"\0").split(b"\0")]
    if not argv or not argv[0] or any(not value or "\0" in value for value in argv):
        return None
    return argv


def _valid_native_launcher_argv(argv, harness):
    if (harness not in {"claude", "codex", "opencode"} or not isinstance(argv, list)
            or not argv or len(argv) > 64):
        return False
    if any(not isinstance(arg, str) or not arg or "\0" in arg for arg in argv):
        return False
    if sum(len(arg.encode("utf-8", "surrogatepass")) for arg in argv) > 8192:
        return False
    executable = argv[0]
    return executable == harness or os.path.isabs(executable) and os.path.basename(executable) == harness


def _canonical_resume_argv(harness, session_id):
    return ([harness, "--resume", session_id] if harness == "claude" else
            [harness, "resume", session_id] if harness == "codex" else
            [harness, "--session", session_id])


def _resume_selector(argv, harness, session_id):
    """Return the selector token index, or canonical/None for ambiguous/sidless."""
    args = argv[1:]
    selectors = {"claude": {"--resume", "-r"}, "codex": set(),
                 "opencode": {"--session", "-s"}}[harness]
    value_options = {
        "claude": {"--model", "-m", "--permission-mode", "--settings", "--setting-sources"},
        "codex": {"-c", "--config", "--model", "--sandbox", "--ask-for-approval", "--color"},
        "opencode": {"--model", "-m", "--agent", "--variant", "--format", "--dir", "-D"},
    }[harness]
    flags = {"claude": {"--verbose", "--debug", "--dangerously-skip-permissions", "--print"},
             "codex": {"--full-auto", "--yolo", "--oss", "--search", "--no-alt-screen"},
             "opencode": {"--help", "--version", "--print-logs"}}[harness]
    found = []
    ambiguous = False
    continue_mode = False
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--":
            if i + 1 < len(args):
                ambiguous = True
            break
        if harness == "codex" and token in {"resume", "fork"}:
            if token == "fork":
                return _canonical_resume_argv(harness, session_id)
            if i + 1 >= len(args) or args[i + 1].startswith("-"):
                continue_mode = True
                i += 1
            else:
                found.append((i + 1, None))
                i += 2
            continue
        if token in {"--continue", "--last", "-c"} and harness != "codex":
            continue_mode = True
        if token in {"--fork-session", "--fork", "--fork=true"} or token.startswith("--fork=") and token != "--fork=false":
            return _canonical_resume_argv(harness, session_id)
        if token in selectors:
            if i + 1 >= len(args) or args[i + 1].startswith("-"):
                i += 1
            else:
                found.append((i + 1, None))
                i += 2
            continue
        selector = next((option for option in selectors if token.startswith(option + "=")), None)
        if selector:
            found.append((i, selector + "="))
            i += 1
            continue
        if token.startswith("-"):
            key = token.split("=", 1)[0]
            if key in value_options and "=" not in token:
                if i + 1 >= len(args):
                    ambiguous = True
                    i += 1
                else:
                    i += 2
                continue
            if key in flags or "=" in token:
                i += 1
                continue
            ambiguous = True
            i += 1
            continue
        # Positional project paths, subcommands, and prompts are unsafe to replay.
        ambiguous = True
        i += 1
    if not found:
        return None
    if len(found) != 1 or continue_mode or ambiguous:
        return _canonical_resume_argv(harness, session_id)
    index, prefix = found[0]
    result = [harness, *args]
    result[index + 1] = f"{prefix}{session_id}" if prefix else session_id
    if len(result) > 64 or sum(len(arg.encode("utf-8", "surrogatepass")) for arg in result) > 8192:
        return None
    return result


def is_native_resume_argv(argv, harness, session_id=None):
    """Validate native launcher structure, or a usable wire command when SID is given."""
    if not _valid_native_launcher_argv(argv, harness):
        return False
    return session_id is None or (isinstance(session_id, str) and bool(session_id)
                                  and "\0" not in session_id
                                  and native_resume_wire_argv(argv, harness, session_id) is not None)


def native_resume_wire_argv(argv, harness, session_id=None):
    """Build a bounded Herdr resume command using the current proven native SID."""
    if (not _valid_native_launcher_argv(argv, harness) or not isinstance(session_id, str)
            or not session_id or "\0" in session_id):
        return None
    canonical = _canonical_resume_argv(harness, session_id)
    if (len(canonical) > 64 or
            sum(len(arg.encode("utf-8", "surrogatepass")) for arg in canonical) > 8192):
        return None
    return _resume_selector(argv, harness, session_id)


def launch_context(pid, harness, session_id):
    """Read argv and cwd only while the exact native session and PID birth agree."""
    if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1
            or harness not in {"claude", "codex", "opencode"}
            or not isinstance(session_id, str) or not session_id):
        return None
    _parent, before = _proc_stat(pid)
    identity = process_identity(pid, harness)
    if not before or identity.confidence != PROVEN or identity.session_id != session_id:
        return None
    argv = _proc_argv(pid)
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        cwd = None
    _parent, after = _proc_stat(pid)
    final = process_identity(pid, harness)
    if (after != before or final.confidence != PROVEN or final.session_id != session_id
            or argv is None):
        return None
    return {"pid": pid, "proc_start": before, "harness": harness,
            "session_id": session_id, "argv": argv, "cwd": cwd}


def current_session_identity(harness, *, max_ancestors=32):
    """Return a proven session from the current runtime ancestry, else unknown."""
    if harness not in {"claude", "codex", "opencode"}:
        return None
    pid = os.getpid()
    for _ in range(max(1, min(int(max_ancestors), 64))):
        if not pid or pid <= 1:
            break
        parent, before = _proc_stat(pid)
        if not before:
            break
        found = process_identity(pid, harness)
        _parent, after = _proc_stat(pid)
        if after != before:
            return None
        if found.confidence == PROVEN and found.session_id:
            return found.session_id
        pid = parent
    return None


def current_launch_context(harness, session_id, *, max_ancestors=32):
    """Find the nearest ancestor that proves this exact session and retain its birth."""
    pid = os.getpid()
    for _ in range(max(1, min(int(max_ancestors), 64))):
        if not pid or pid <= 1:
            break
        context = launch_context(pid, harness, session_id)
        if context is not None:
            return context
        parent, _start = _proc_stat(pid)
        pid = parent
    return None
