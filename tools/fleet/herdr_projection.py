#!/usr/bin/env python3
"""One pane-title projection shared by Claude, Codex, and OpenCode.

A pane header is the only identity surface all three harnesses share. Each runtime draws
its own status line differently and none of them can be made to agree, so the identity —
which session this is, and whether it is a steward — goes on the outside, in the herdr
pane header, where the shape is ours to fix (user 2026-09-09: "그 바깥에 장치를 두는게
맞겠다"). The header reads

    [3a] claude   통신 표시 일관성 수정
    [b0] claude ⚑ 하팅 감독 — 네 세션 관리

Both halves go into `title`, because that is the only field herdr paints above the pane:
`display_agent` is reported too and herdr stores it, but a pane whose `display_agent` said
``[6b] claude`` for hours still showed no identity until `title` was filled (measured
2026-09-10 — the user watched that A/B and said "제목은 뜨는데 id는 여전히 안뜨는데. 그게
중요한건데"). Nothing here generates a summary; it only projects one.

Display-only and fail-soft throughout: no herdr, no pane, no title, or a slow formatter
all mean "report less", never an error. Registered workers project nothing at all — the
pane belongs to the interactive session that owns it.

Every herdr report from a hook — this projection and `hooks/herdr-agent-state.sh` — asks
`may_report()` first. A process may report a session only when it IS that session's
runtime, proven from the process itself, never from the payload. Test suites run the real
hooks with fake session ids while inheriting the interactive pane's `HERDR_PANE_ID` and
often strip the worker markers, so the worker check alone let `directpromptsid` repaint a
live pane as `[0d] codex` and take over its `agent_session_id` (2026-09-24). When the
identity cannot be established the report is skipped: the header keeps its previous text
until the real session's next hook (user decision, 2026-09-24 stale-title).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_TOOLS = str(Path(__file__).resolve().parents[1])
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

from fleet.session_handle import (_cell_width, clip_cells,  # noqa: E402
                                  resolve_display_inputs, resolve_tag,
                                  sanitize_title)

HARNESSES = ("claude", "codex", "opencode")
_AGENT_W = 24            # herdr's display_agent budget
_TITLE_W = 48            # herdr's title budget
_STEWARD_MARK = "⚑"
_FORMATTER_TIMEOUT = 0.2
_HERDR_TIMEOUT = 0.5
_WORKER_ENV = ("AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH", "OPENCODE_DISPATCH_SLUG",
               "FLEET_TITLE_REFRESH", "MEM_DISTILL")


def is_worker() -> bool:
    """A registered/background worker never owns the interactive pane header (D-42)."""
    if os.environ.get("AGENT_SESSION_ROLE") == "worker":
        return True
    return any(os.environ.get(name) for name in _WORKER_ENV)


_MAX_ANCESTORS = 32


def _parent(pid: int):
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            return int(handle.read().rsplit(b")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _comm(pid: int) -> str:
    try:
        with open("/proc/%d/comm" % pid, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _claude_sessions_dir() -> Path:
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(config).expanduser() if config else Path.home() / ".claude"


def _claude_session_of(pid: int):
    """The session id Claude Code itself records for process ``pid``, or None.

    `sessions/<pid>.json` is rewritten on `/clear` (measured 2026-09-24: a probe session's
    file moved from d153cbe1… to 87f90316… on `/clear`), so it follows the session the
    runtime is on now, not the one it started with.
    """
    try:
        with open(_claude_sessions_dir() / "sessions" / ("%d.json" % pid),
                  encoding="utf-8") as handle:
            value = json.load(handle).get("sessionId")
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


def runtime_identity():
    """``(harness, session_id | None)`` of the nearest runtime process above this one.

    Walks this process and its ancestors: the first one Claude has a session file for is
    a Claude runtime with that session; a process named ``codex``/``codex-*`` or ``opencode`` is that
    runtime (its own session id is not readable from the process). ``(None, None)`` when
    no runtime is found — CI, a detached helper, a test with a fake config dir.
    """
    pid = os.getpid()
    for _ in range(_MAX_ANCESTORS):
        if not pid or pid <= 1:
            break
        session = _claude_session_of(pid)
        if session:
            return "claude", session
        comm = _comm(pid)
        if comm == "codex" or comm.startswith("codex-"):
            return "codex", None
        if comm == "opencode":
            return "opencode", None
        pid = _parent(pid)
    return None, None


def may_report(harness: str, session_id: str, *, worker=None) -> bool:
    """The ONE decision whether this process may report ``session_id`` to herdr.

    - never for a registered/background worker (D-42);
    - Claude: the nearest Claude runtime above this process must be on ``session_id``;
    - Codex: ``CODEX_THREAD_ID`` — set by Codex in the environment of what it runs — must
      equal ``session_id`` when present; otherwise the nearest runtime must be Codex;
    - OpenCode: the nearest runtime must be OpenCode.
    Unknown identity is a refusal, never a guess.
    """
    harness = str(harness or "").lower()
    if harness not in HARNESSES or not isinstance(session_id, str) or not session_id:
        return False
    if worker if worker is not None else is_worker():
        return False
    if harness == "codex":
        thread = os.environ.get("CODEX_THREAD_ID", "")
        if thread:
            return thread == session_id
    runtime, own = runtime_identity()
    if runtime != harness:
        return False
    return own == session_id if runtime == "claude" else True


def _runtime_name(harness: str, session_id: str) -> str:
    """The name the runtime itself exposes (Codex's `thread_name`, a user-set Claude
    name). Same resolver Fleet's collectors use, so the pane and the board agree."""
    try:
        if harness == "codex":
            from fleet.collectors.codex import _home, _thread_runtime_names
            name = _thread_runtime_names(_home()).get(session_id)
            if isinstance(name, str) and name.strip():
                return name.strip()
    except Exception:
        pass
    try:
        inputs = resolve_display_inputs(harness, session_id)
        return inputs.get("runtime_name") or inputs.get("registry_name") or ""
    except Exception:
        return ""


def session_title(harness: str, session_id: str) -> str:
    """The summary Fleet already has — never a newly generated one.

    The board's ladder (`collectors/claude.enrich` steps 3a/3b) — fresh sidecar, then the
    transcript's own ai-title — with a stale sidecar and the runtime's session name below
    it so a header never goes blank where it used to say something. It used to stop at the
    sidecar, so a session whose title worker had failed showed a full title on the board
    and NOTHING in its pane header — measured 2026-09-10, four of six Claude panes were
    anonymous while the board named every one of them. Two ladders for one value is how a
    pane and a board start disagreeing about the same session.

    It never falls back to the folder name, which would just repeat what herdr already
    shows beside the pane. A registered dispatch session's attempt-sid sidecar (the
    board's step 3a') is deliberately not consulted: a worker projects nothing at all —
    the pane belongs to the interactive session.
    """
    stale = ""
    try:
        from fleet.titles import fresh_title, read
        title = sanitize_title(fresh_title(session_id, harness=harness))
        if title:
            return title
        stale = sanitize_title((read(session_id, harness=harness) or {}).get("title"))
    except Exception:
        stale = ""
    if harness == "claude":
        try:
            from fleet.collectors.claude import ai_title_for_session
            title = sanitize_title(ai_title_for_session(session_id))
        except Exception:
            title = ""
        if title:
            return title
    # A sidecar too old for the board is still this session's own considered summary, and
    # a header that goes blank as a title ages is worse than one that keeps it. It sits
    # BELOW the ai-title so the two surfaces agree wherever the board has anything at all.
    if stale:
        return stale
    # Only the runtime's OWN name is an acceptable stand-in. `display_name()` would fall
    # through to the folder — or to its literal "?" last resort — and a pane header saying
    # "?" is worse than one saying nothing.
    return sanitize_title(_runtime_name(harness, session_id))


def is_steward(harness: str, session_id: str) -> bool:
    """True when this session's marker holds steward ROLE evidence.

    Asks the ledger tool itself which entries count, exactly as Fleet's collector does —
    a second copy of that rule here is how the badge and the board start disagreeing.
    """
    if not session_id:
        return False
    try:
        import importlib.util
        for candidate in Path(__file__).resolve().parents:
            tool = candidate / "utilities" / "peer-message.py"
            if not tool.is_file():
                continue
            spec = importlib.util.spec_from_file_location("_peer_message_ro", str(tool))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            marker = (module.read_steward_markers() or {}).get((harness, session_id))
            return bool(marker and module.steward_evidence_targets(marker))
    except Exception:
        pass
    return False


def compose(harness: str, session_id: str, *, tag=None, steward=None, title=None,
            label=None) -> tuple:
    """→ ``(display_agent, title)`` in the fixed order number → harness → ⚑ → summary.

    ``label`` lets a user formatter rename the middle harness word; the badge and the
    steward mark stay ours, so a personal formatter cannot quietly delete the two things
    that identify the session.
    """
    harness = str(harness or "").lower()
    if tag is None:
        tag = resolve_tag(harness, session_id)
    if steward is None:
        steward = is_steward(harness, session_id)
    if title is None:
        title = session_title(harness, session_id)
    middle = sanitize_title(label) or harness or "agent"
    agent = ("[%s] %s" % (tag, middle)) if tag else middle
    if steward:
        agent += " " + _STEWARD_MARK
    return clip_cells(agent, _AGENT_W), clip_cells(sanitize_title(title), _TITLE_W)


_HEADER_W = 72           # herdr paints one string above the pane; give the summary room


def header_title(agent: str, title: str) -> str:
    """`[6b] claude ⚑ 요약` — the ONE string herdr actually paints above the pane.

    `display_agent` is reported as well, but herdr does not paint it on the pane header.
    Measured 2026-09-10: four panes carried `[6b] claude` in `display_agent` for hours
    while their headers showed no identity at all, and a header only started saying
    something once `title` was filled — the user watched exactly that A/B happen and
    reported "제목은 뜨는데 id는 여전히 안뜨는데. 그게 중요한건데".

    So the requested format — number, harness, steward mark, then the summary
    (user 2026-09-09: "세션 번호를 herdr의 pane 상단 제목에 뜨게끔 하자 요약과 더불어서",
    "맨 앞에 harness 이름은 뜨게끔") — is joined here, into the field that reaches the
    header. The badge comes first and is never the part that gets clipped: a summary
    truncated by a few characters still reads, an identity truncated does not.
    """
    agent = sanitize_title(agent)
    title = sanitize_title(title)
    if not agent:
        return clip_cells(title, _HEADER_W)
    if not title:
        return clip_cells(agent, _HEADER_W)
    room = _HEADER_W - _cell_width(agent) - 1
    if room <= 0:
        return clip_cells(agent, _HEADER_W)
    return agent + " " + clip_cells(title, room)


def _formatter_path() -> Path:
    override = os.environ.get("HERDR_SESSION_METADATA_FORMATTER")
    if override:
        return Path(override).expanduser()
    config = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config / "hearting" / "herdr-session-metadata"


def _formatter_overrides(harness: str, session_id: str, title: str) -> tuple:
    """F-95's optional personal formatter → ``(label, title)`` overrides, else ``(None, None)``."""
    formatter = _formatter_path()
    try:
        if not formatter.is_file() or not os.access(formatter, os.X_OK):
            return None, None
        result = subprocess.run(
            [str(formatter), "--harness", harness, "--session-id", session_id,
             "--summary", title],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=_FORMATTER_TIMEOUT, check=False)
        if result.returncode or len(result.stdout.encode("utf-8")) > 4096:
            return None, None
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            return None, None
        return value.get("display_agent"), value.get("title")
    except Exception:
        return None, None


def project(harness: str, session_id: str, *, pane_id=None, worker=None,
            report_session=True) -> bool:
    """Report this session's pane metadata to herdr. Always returns True (fail-soft)."""
    harness = str(harness or "").lower()
    pane = pane_id or os.environ.get("HERDR_PANE_ID", "")
    herdr = shutil.which("herdr")
    if not pane or not herdr:
        return True
    if not may_report(harness, session_id, worker=worker):
        return True
    title = session_title(harness, session_id)
    label, custom_title = _formatter_overrides(harness, session_id, title)
    agent, shown_title = compose(harness, session_id, title=custom_title or title,
                                 label=label)
    source = "herdr:%s" % harness
    commands = []
    if report_session:
        commands.append([herdr, "pane", "report-agent-session", pane, "--source", source,
                         "--agent", harness, "--agent-session-id", session_id])
    # Both fields go in the SAME report: herdr's metadata record is per-source and a
    # report replaces it whole, so sending one alone clears the other (measured).
    metadata = [herdr, "pane", "report-metadata", pane, "--source", source,
                "--display-agent", agent]
    header = header_title(agent, shown_title)
    if header:
        metadata += ["--title", header]
    commands.append(metadata)
    for command in commands:
        try:
            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=_HERDR_TIMEOUT, check=False)
        except Exception:
            pass
    return True


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True, choices=list(HARNESSES))
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--pane")
    parser.add_argument("--no-report-session", action="store_true",
                        help="skip report-agent-session (the runtime's own hook owns it)")
    parser.add_argument("--print", action="store_true",
                        help="print the composed metadata instead of reporting it")
    parser.add_argument("--may-report", action="store_true",
                        help="exit 0 when this process may report the session, else 1")
    args = parser.parse_args(argv)
    if args.may_report:
        return 0 if may_report(args.harness, args.session_id) else 1
    if args.print:
        agent, title = compose(args.harness, args.session_id)
        print(json.dumps({"display_agent": agent, "title": title}, ensure_ascii=False))
        return 0
    project(args.harness, args.session_id, pane_id=args.pane,
            report_session=not args.no_report_session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
