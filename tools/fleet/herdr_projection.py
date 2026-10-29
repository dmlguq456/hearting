#!/usr/bin/env python3
"""One pane-title projection shared by Claude, Codex, and OpenCode.

A pane header is the only identity surface all three harnesses share. Each runtime draws
its own status line differently and none of them can be made to agree, so the identity —
which session this is, and whether it is a steward — goes on the outside, in the herdr
pane header, where the shape is ours to fix (user 2026-09-09: "그 바깥에 장치를 두는게
맞겠다"). The header reads

    [3a] claude   통신 표시 일관성 수정
    [b0] ⚑ claude 하팅 감독 — 네 세션 관리

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
runtime, proven from the process itself, never from the payload or the environment
(OpenCode proves the harness only — see `may_report`). For Codex, process_identity
proves the thread through the daemon loader's exact foreground PID. A visible footer
confirms the thread and refuses a disagreement; an absent footer does not erase the
process proof. Shared services use the caller's CODEX_THREAD_ID to locate that TUI.
Ambiguous process ownership proves nothing. Test suites run the real
hooks with fake session ids while inheriting the interactive pane's `HERDR_PANE_ID` and
often strip the worker markers, so the worker check alone let `directpromptsid` repaint a
live pane as `[0d] codex` and take over its `agent_session_id` (2026-09-24). When the
identity cannot be established the report is skipped: the header keeps its previous text
until the real session's next hook (user decision, 2026-09-24 stale-title).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

_TOOLS = str(Path(__file__).resolve().parents[1])
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

from fleet.session_handle import (_cell_width, clip_cells,  # noqa: E402
                                  current_session_subject, resolve_display_inputs, resolve_tag,
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


def runtime_identity():
    """``(harness, session_id | None)`` of the nearest runtime process above this one.

    Walks this process and its ancestors and asks Fleet's own collectors which session
    each one is — never a second copy of their resolution:

    - Claude: `collectors.claude.session_id_of_process` (its `sessions/<pid>.json`, else
      the statusline tap matched by pid + start time, for the hours a live process's
      registry file goes missing — F-25 tier 2);
    - Codex: a process named ``codex``/``codex-*`` is the runtime only once
      `collectors.codex.session_id_of_process` proves its thread through a held rollout
      or the daemon loader's unique TUI PID. A process without either proof is passed
      over for the ancestor that is, and no proof anywhere means ``("codex", None)``;
    - OpenCode: a process named ``opencode`` — its session id is the one its own TUI
      selection record proves, else ``("opencode", None)`` (a start-time ``--session``
      is not proof: the TUI may have switched since).

    Every harness is read through `fleet.process_identity`, the one record shape.

    ``(None, None)`` when no runtime is found — CI, a detached helper, a test with a fake
    config dir.
    """
    from fleet.process_identity import PROVEN, process_identity
    pid, codex_seen = os.getpid(), False

    for _ in range(_MAX_ANCESTORS):
        if not pid or pid <= 1:
            break
        found = process_identity(pid, "claude")
        if found.confidence == PROVEN:
            return "claude", found.session_id
        comm = _comm(pid)
        if comm == "codex" or comm.startswith("codex-"):
            codex_seen = True
            found = process_identity(pid, "codex")
            if found.confidence == PROVEN:
                return "codex", found.session_id
        elif comm == "opencode":
            found = process_identity(pid, "opencode")
            return "opencode", found.session_id if found.confidence == PROVEN else None
        pid = _parent(pid)
    return ("codex", None) if codex_seen else (None, None)


def verified_pane(pane, harness, session_id=None, **kwargs):
    utilities = str(Path(__file__).resolve().parents[2] / "utilities")
    if utilities not in sys.path:
        sys.path.insert(0, utilities)
    from pane_ownership import verified_pane as verify
    return verify(pane, harness, session_id, **kwargs)


def _nearest_codex_pid():
    """Return the nearest Codex process in this hook's ancestry, if any."""
    pid = os.getpid()
    for _ in range(_MAX_ANCESTORS):
        if not pid or pid <= 1:
            break
        comm = _comm(pid)
        if comm == "codex" or comm.startswith("codex-"):
            return pid
        pid = _parent(pid)
    return None


def may_report(harness: str, session_id: str, *, worker=None, pane_id=None) -> bool:
    """The ONE decision whether this process may report ``session_id`` to herdr.

    - never for a registered/background worker (D-42);
    - Claude: the nearest runtime above this process must be proven to be on
      ``session_id`` (see `runtime_identity`); the payload and environment are never proof;
    - Codex: process_identity must prove ``session_id`` for the foreground TUI in its
      pane; a visible footer confirms it and rejects a different selection;
    - OpenCode: the nearest runtime must be OpenCode, and when that process proves its
      session (its TUI selection record) the id must match it. A process
      that proves none — an older TUI without the selection record — keeps the
      harness-only check, so its id is reported as before.
    Unknown identity is a refusal, never a guess: the header keeps its previous text.
    """
    harness = str(harness or "").lower()
    if harness not in HARNESSES or not isinstance(session_id, str) or not session_id:
        return False
    if worker if worker is not None else is_worker():
        return False
    runtime, own = runtime_identity()
    if runtime != harness:
        return False
    pane = pane_id or os.environ.get("HERDR_PANE_ID")
    if harness == "codex":
        # The callback's exact thread id is checked against the TUI process identity;
        # if a footer is visible, pane_ownership also rejects a conflicting selection.
        pid = _nearest_codex_pid()
        return bool(pid and verified_pane(pane, harness, session_id, pid=pid))
    if not (runtime == "opencode" and own is None) and own != session_id:
        return False
    if not verified_pane(pane, harness, session_id):
        return False
    return True


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
    """Share Fleet's explicit-user-name / last-successful-title / blank ladder."""
    runtime_name = None
    try:
        runtime_name = resolve_display_inputs(harness, session_id).get("runtime_name")
    except Exception:
        pass
    title = None
    try:
        from fleet.titles import last_title
        title = last_title(session_id, harness=harness)
    except Exception:
        pass
    return current_session_subject(runtime_name, title)


def is_steward(harness: str, session_id: str) -> bool:
    """True when this session's marker holds steward ROLE evidence.

    Uses Fleet's role lookup and its confirmed native-clear aliases, so a new
    conversation keeps the same role on both displays without copying a marker.
    """
    if not session_id:
        return False
    try:
        from fleet.collectors.herdr import _clear_gpu_session_aliases
        from fleet.collectors import steward as steward_collector
        markers = steward_collector.read_markers()
        if not markers:
            return False
        cwd = os.getcwd()
        harness_key = str(harness or "").lower()
        aliases = _clear_gpu_session_aliases(harness, session_id,
                                             os.environ.get("HERDR_PANE_ID"), cwd)
        sessions = steward_collector._projection_sessions()
        live = {(str(getattr(row, "harness", "") or "").lower(),
                 getattr(row, "session_id", None)) for row in sessions}
        aliases = [sid for sid in aliases if (harness_key, sid) not in live]
        targets = steward_collector.role_targets(
            harness, session_id, aliases=aliases, markers=markers, cwd=cwd,
            sessions=sessions, include_native=True)
        # The normal hook owns its cwd even before herdr has published a SID.
        return steward_collector.role_flag(harness, session_id, aliases=aliases,
                                           markers=markers, cwd=cwd, sessions=sessions,
                                           _targets=targets)
    except Exception:
        pass
    return False


def _tag_metadata_identity(agent):
    harness = agent.get("agent")
    identity = agent.get("agent_session") or {}
    pane = agent.get("pane_id")
    if (harness not in HARNESSES or not pane
            or not isinstance(identity, dict) or identity.get("kind") != "id"
            or identity.get("agent", harness) != harness):
        return None
    sid = identity.get("value")
    return (pane, harness, sid) if isinstance(sid, str) and sid else None


def refresh_observed_tag_metadata(agents) -> None:
    """A delayed detail may update only the same currently observed pane.

    This reads target identity immediately before each metadata attempt; it
    never changes session identity/lifecycle or reclassifies Fleet rows.
    """
    from fleet.collectors.herdr import list_agents
    herdr = shutil.which("herdr")
    if not herdr:
        return
    for observed in _tag_metadata_targets(agents):
        pane, harness, sid = observed
        metadata = _metadata_command(herdr, harness, sid, pane)
        # Titles and personal formatters may be slow. Confirm the target after
        # all preparation, immediately before sending the metadata command.
        current = list_agents()
        peers = [row for row in current or [] if row.get("pane_id") == observed[0]]
        if len(peers) == 1 and _tag_metadata_identity(peers[0]) == observed:
            prepared = re.match(r"^\[([0-9a-f]{2})\]",
                                metadata[metadata.index("--display-agent") + 1])
            if prepared and prepared.group(1) == resolve_tag(harness, sid):
                _send_projection(metadata, None)


def _tag_metadata_targets(agents):
    for agent in agents:
        identity = _tag_metadata_identity(agent)
        if identity is None:
            continue
        _, harness, sid = identity
        tag = resolve_tag(harness, sid)
        shown = re.match(r"^\[([0-9a-f]{2})\]", str(agent.get("display_agent") or ""))
        if tag and (not shown or shown.group(1) != tag):
            yield identity


def refresh_tag_metadata(agents) -> None:
    """Refresh changed numbers on exactly identified panes, even while idle.

    This reuses the regular metadata projection. It reports no runtime session
    identity or lifecycle, and never prompts or wakes the pane's session.
    """
    for pane, harness, sid in _tag_metadata_targets(agents):
        _report(harness, sid, pane, False)


def compose(harness: str, session_id: str, *, tag=None, steward=None, title=None,
            label=None) -> tuple:
    """→ ``(display_agent, title)`` in the fixed order number → ⚑ → harness → summary.

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
    if steward:
        middle = _STEWARD_MARK + " " + middle
    agent = ("[%s] %s" % (tag, middle)) if tag else middle
    return clip_cells(agent, _AGENT_W), clip_cells(sanitize_title(title), _TITLE_W)


_HEADER_W = 72           # herdr paints one string above the pane; give the summary room


def header_title(agent: str, title: str) -> str:
    """`[6b] ⚑ claude 요약` — the ONE string herdr actually paints above the pane.

    `display_agent` is reported as well, but herdr does not paint it on the pane header.
    Measured 2026-09-10: four panes carried `[6b] claude` in `display_agent` for hours
    while their headers showed no identity at all, and a header only started saying
    something once `title` was filled — the user watched exactly that A/B happen and
    reported "제목은 뜨는데 id는 여전히 안뜨는데. 그게 중요한건데".

    So the requested format — number, steward mark, harness, then the summary
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


_PROOF_WAIT_S = 90
_THREAD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _codex_status_line_feasibility() -> str:
    """Read only the current direct Codex frontend's known effective config."""
    try:
        from fleet.collectors import procscan
        pid = _nearest_codex_pid()
        if not pid or procscan.is_shared_codex_daemon(pid):
            return "unknown"
        argv = procscan._read_argv(pid)
        if procscan.invocation_mode("codex", argv) != "session":
            return "unknown"
        env = procscan.read_environ(pid)
        home = env.get("CODEX_HOME")
        if not home and env.get("HOME"):
            home = os.path.join(env["HOME"], ".codex")
        if not home:
            return "unknown"
        utilities = str(Path(__file__).resolve().parents[2] / "utilities")
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        from codex_tui_config import status_line_capability_for_home
        return status_line_capability_for_home(home, argv=argv)
    except Exception:
        return "unknown"


def _in_shared_codex_service() -> bool:
    """True when this hook runs inside the shared background Codex service.

    That service inherits the environment of whichever pane or shell first started
    it, so its HERDR_PANE_ID names that pane, not the session whose hook is running.
    """
    pid = os.getpid()
    for _ in range(_MAX_ANCESTORS):
        if not pid or pid <= 1:
            return False
        if _comm(pid) == "codex":
            try:
                with open("/proc/%d/cmdline" % pid, "rb") as handle:
                    args = handle.read().split(b"\0")
            except OSError:
                return False
            return b"app-server" in args and not any(b"managed-sessions" in arg for arg in args)
        pid = _parent(pid)
    return False


def _defer_until_proven(session_id: str, report_session: bool, *,
                        session_seq=None, session_start_source=None) -> None:
    """Report a Codex session its own hook could not prove, once its TUI is proven.

    A Codex TUI started without the managed app-server attaches to the user's shared
    background Codex service, which runs the hooks in that service's environment: no
    HERDR_PANE_ID, and the hook's ancestor is the service rather than the pane's TUI. The
    TUI also has no provable thread until its first turn starts. Either way the pane
    stayed anonymous, so a detached helper waits for the board's resolver to prove a live
    TUI on this session and reports to the pane herdr gave that TUI.
    """
    if not _THREAD_ID.fullmatch(session_id or ""):
        return
    if _codex_status_line_feasibility() == "incapable":
        return
    command = [sys.executable, str(Path(__file__).resolve()), "--harness", "codex",
               "--session-id", session_id, "--await-codex-tui"]
    if not report_session:
        command.append("--no-report-session")
    if session_seq is not None:
        command += ["--seq", str(session_seq)]
    if session_start_source is not None:
        command += ["--session-start-source", session_start_source]
    try:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


@contextmanager
def _herdr_endpoint(socket_path):
    """Use the proven frontend endpoint in this dedicated deferred reporter."""
    inherited = os.environ.get("HERDR_SOCKET_PATH")
    if socket_path:
        os.environ["HERDR_SOCKET_PATH"] = socket_path
    else:
        os.environ.pop("HERDR_SOCKET_PATH", None)
    try:
        yield
    finally:
        if inherited is None:
            os.environ.pop("HERDR_SOCKET_PATH", None)
        else:
            os.environ["HERDR_SOCKET_PATH"] = inherited


def _codex_tui_endpoint(session_id: str):
    """The unique pane/socket pair whose live foreground Codex TUI proves ``session_id``."""
    try:
        # The deferred reporter runs this module as a script, without the
        # hook's utilities path.
        utilities = str(Path(__file__).resolve().parents[2] / "utilities")
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        from pane_ownership import codex_tui_pane_endpoint
        return codex_tui_pane_endpoint(session_id)
    except Exception:
        return None


def _codex_tui_pane(session_id: str, *, transport=None):
    """The unique pane whose live foreground Codex TUI proves ``session_id``, or None.

    Two panes resolving to one session stay unknown: a badge on the wrong pane
    also misdirects peer messages.
    """
    pair = _codex_tui_endpoint(session_id)
    if pair is None:
        return None
    pane, socket_path = pair
    if transport is not None:
        transport.update(socket_path=socket_path)
    return pane


def _await_tui_and_project(session_id: str, report_session: bool, *,
                           session_seq=None, session_start_source=None) -> None:
    import fcntl
    import tempfile
    # One waiter per session: every later prompt's helper exits while one is waiting.
    lock = open(os.path.join(tempfile.gettempdir(),
                             "hearting-herdr-await-%s.lock" % session_id), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return
    try:
        deadline = time.monotonic() + _PROOF_WAIT_S
        while time.monotonic() < deadline:
            endpoint = _codex_tui_endpoint(session_id)
            if endpoint:
                pane, socket_path = endpoint
                with _herdr_endpoint(socket_path):
                    _report("codex", session_id, pane, report_session,
                            session_seq=session_seq, session_start_source=session_start_source,
                            _proof=lambda: _codex_tui_endpoint(session_id) == endpoint)
                return
            time.sleep(2)
    finally:
        lock.close()


def _session_lifecycle(harness, session_seq, session_start_source, resume_argv=None, *, session_id=None):
    """Optional native bootstrap fields; never infer a source or widen a guard."""
    from fleet.process_identity import native_resume_wire_argv

    if session_seq is None and session_start_source is None and resume_argv is None:
        return {}
    max_seq = 9007199254740991 if harness == "opencode" else 18446744073709551615
    if (harness not in HARNESSES or type(session_seq) is not int
            or not 0 < session_seq <= max_seq
            or session_start_source not in (None, "startup", "resume", "select", "clear", "compact", "fork")):
        return None
    result = dict(session_seq=session_seq, session_start_source=session_start_source)
    if resume_argv is not None:
        wire_argv = native_resume_wire_argv(resume_argv, harness, session_id)
        if wire_argv is None:
            result.pop("resume_argv", None)
        else:
            result["resume_argv"] = wire_argv
    return result


def _native_opencode_lifecycle(session_id):
    """Read lifecycle metadata only from this proven TUI's PID/birth selection record."""
    try:
        from fleet.process_identity import current_launch_context, native_resume_wire_argv
        from fleet.collectors import opencode
        launch = current_launch_context("opencode", session_id)
        if not launch:
            return {}
        record = opencode._tui_selection_record(launch["pid"], launch["proc_start"])
        value = (record or {}).get("value") if isinstance(record, dict) else None
        if not isinstance(value, dict) or value.get("sessionID") != session_id:
            return {}
        result = {}
        sequence = value.get("sessionSeq")
        source = value.get("sessionStartSource")
        if type(sequence) is int and 0 < sequence <= 9007199254740991:
            result["session_seq"] = sequence
        if source in ("startup", "resume", "select", "clear", "compact", "fork"):
            result["session_start_source"] = source
        argv = launch.get("argv")
        wire_argv = native_resume_wire_argv(argv, "opencode", session_id)
        if wire_argv is not None:
            result["resume_argv"] = wire_argv
        return result
    except Exception:
        return {}


def project(harness: str, session_id: str, *, pane_id=None, worker=None,
            report_session=True, observation=None, session_seq=None,
            session_start_source=None, resume_argv=None) -> bool:
    """Report this session's pane metadata to herdr. Always returns True (fail-soft)."""
    harness = str(harness or "").lower()
    pane = pane_id or os.environ.get("HERDR_PANE_ID", "")
    if observation is not None:
        observation.update(schema="hearting-pane-observation-v1", reason="not-attempted",
                           session_report="not-attempted", metadata_report="not-attempted")
    if not shutil.which("herdr"):
        if observation is not None:
            observation["reason"] = "herdr-unavailable"
        return True
    codex_main = harness == "codex" and not (
        worker if worker is not None else is_worker())
    if codex_main and not pane_id and _in_shared_codex_service():
        pane = ""
    if not pane:
        if observation is not None:
            observation["reason"] = "pane-unavailable"
        if codex_main:
            _defer_until_proven(session_id, report_session, session_seq=session_seq,
                                session_start_source=session_start_source)
        return True
    if not may_report(harness, session_id, worker=worker, pane_id=pane):
        if observation is not None:
            observation["reason"] = "guard-refused"
        if codex_main and runtime_identity()[0] == "codex":
            pid = _nearest_codex_pid()
            if pid and verified_pane(pane, "codex", pid=pid):
                _defer_until_proven(session_id, report_session, session_seq=session_seq,
                                    session_start_source=session_start_source)
        return True
    if report_session and session_seq is None and session_start_source is None:
        # A start may precede native process identity. The next existing callback
        # reuses that recorded source once this exact runtime is identifiable.
        try:
            utilities = str(Path(__file__).resolve().parents[2] / "utilities")
            if utilities not in sys.path:
                sys.path.insert(0, utilities)
            import session_tidy
            context = session_tidy.session_start_context(harness, session_id, pane)
            session_seq = context.get("session_seq")
            session_start_source = context.get("session_start_source")
            resume_argv = context.get("resume_argv")
            if harness == "opencode":
                native = _native_opencode_lifecycle(session_id)
                session_seq = native.get("session_seq", session_seq)
                session_start_source = native.get("session_start_source", session_start_source)
                resume_argv = native.get("resume_argv", resume_argv)
            if session_start_source and session_seq is None:
                session_seq = time.time_ns() if harness != "opencode" else time.time_ns() // 1000
            if resume_argv is None:
                from fleet.process_identity import current_launch_context
                from fleet.process_identity import native_resume_wire_argv
                launch = current_launch_context(harness, session_id)
                argv = (launch or {}).get("argv")
                resume_argv = native_resume_wire_argv(argv, harness, session_id)
        except Exception:
            session_start_source = None
    if (report_session and resume_argv is None
            and session_start_source in {"startup", "resume", "select", "clear", "compact", "fork"}):
        try:
            from fleet.process_identity import current_launch_context, native_resume_wire_argv
            launch = current_launch_context(harness, session_id)
            resume_argv = native_resume_wire_argv((launch or {}).get("argv"), harness, session_id)
        except Exception:
            resume_argv = None
    lifecycle = _session_lifecycle(harness, session_seq, session_start_source, resume_argv,
                                   session_id=session_id)
    if lifecycle is None:
        report_session, lifecycle = False, {}
    if harness == "codex" and not may_report(harness, session_id, worker=worker, pane_id=pane):
        # The start-source lookup above takes real time. A /new, resume or fork that landed
        # meanwhile makes this callback historical: neither report may name the old session.
        if observation is not None:
            observation["reason"] = "guard-refused-before-report"
        return True
    if observation is not None:
        lifecycle["observation"] = observation
    if harness == "codex":
        lifecycle["_proof"] = lambda: may_report(
            harness, session_id, worker=worker, pane_id=pane)
    if not report_session:
        _assign_observed_tag(harness, session_id, pane)
    _report(harness, session_id, pane, report_session, **lifecycle)
    return True


def _assign_observed_tag(harness, session_id, pane):
    try:
        from .session_tags import assign_observed
        assign_observed(harness, session_id, pane_id=pane)
    except Exception:
        pass  # Optional numbering never rejects a successful native report.


def _send_projection(command, observation):
    field = "session_report" if command[2] == "report-agent-session" else "metadata_report"
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=_HERDR_TIMEOUT, check=False)
        if observation is not None:
            observation[field] = "exit0" if result.returncode == 0 else "nonzero"
            observation[field + "_rc"] = result.returncode
    except subprocess.TimeoutExpired:
        if observation is not None:
            observation[field] = "timeout"
    except Exception:
        if observation is not None:
            observation[field] = "spawn-error"


def _metadata_command(herdr, harness, session_id, pane):
    title = session_title(harness, session_id)
    label, custom_title = _formatter_overrides(harness, session_id, title)
    agent, shown_title = compose(harness, session_id, title=custom_title or title, label=label)
    try:
        utilities = str(Path(__file__).resolve().parents[2] / "utilities")
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        import human_wait_state
        waits = human_wait_state.cached_view()
        if waits is None:
            waits = {"items": [], "by_session": {}}
        waiting_ids = set(waits.get("by_session", {}).get((harness, session_id), ()))
        if any(item.get("id") in waiting_ids and not item.get("stale")
               for item in waits.get("items", ())):
            shown_title = "◑답변 필요 " + shown_title
    except Exception:
        pass
    # Herdr replaces the whole metadata record, so send both fields together.
    metadata = [herdr, "pane", "report-metadata", pane, "--source", "herdr:%s" % harness,
                "--display-agent", agent]
    header = header_title(agent, shown_title)
    if header:
        metadata += ["--title", header]
    return metadata


def _report(harness: str, session_id: str, pane: str, report_session: bool,
            observation=None, session_seq=None, session_start_source=None, resume_argv=None,
            _proof=None) -> None:
    herdr = shutil.which("herdr")
    if not herdr:
        if observation is not None:
            observation["reason"] = "herdr-unavailable"
        return
    source = "herdr:%s" % harness
    lifecycle = _session_lifecycle(harness, session_seq, session_start_source, resume_argv,
                                   session_id=session_id)
    if lifecycle is None:
        report_session = False
    def current():
        try:
            return _proof is None or bool(_proof())
        except Exception:
            return False
    if not current():
        if observation is not None:
            observation["reason"] = "guard-refused-before-report"
        return
    if report_session:
        session_command = [herdr, "pane", "report-agent-session", pane, "--source", source,
                           "--agent", harness, "--agent-session-id", session_id]
        if session_seq is not None:
            session_command += ["--seq", str(session_seq)]
        if session_start_source is not None:
            session_command += ["--session-start-source", session_start_source]
        if lifecycle.get("resume_argv"):
            session_command += ["--", *lifecycle["resume_argv"]]
        # Publish the exact native identity before assigning its display number.
        _send_projection(session_command, observation)
        if not current():
            if observation is not None:
                observation["reason"] = "guard-refused-before-metadata"
            return
        _assign_observed_tag(harness, session_id, pane)
    metadata = _metadata_command(herdr, harness, session_id, pane)
    if not current():
        if observation is not None:
            observation["reason"] = "guard-refused-before-metadata"
        return
    if observation is not None and not report_session:
        observation["session_report"] = "skipped"
    _send_projection(metadata, observation)
    if observation is not None:
        observation["reason"] = "report-attempts-finished"


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True, choices=list(HARNESSES))
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--pane")
    parser.add_argument("--no-report-session", action="store_true",
                        help="skip report-agent-session (the runtime's own hook owns it)")
    parser.add_argument("--seq", type=int,
                        help="optional OpenCode native publisher sequence")
    parser.add_argument("--session-start-source", choices=["startup", "resume", "clear", "compact", "fork"],
                        help="the actual native session-start source")
    parser.add_argument("--print", action="store_true",
                        help="print the composed metadata instead of reporting it")
    parser.add_argument("--await-codex-tui", action="store_true",
                        help="wait until a live Codex TUI is proven on --session-id, then report")
    parser.add_argument("--may-report", action="store_true",
                        help="exit 0 when this process may report the session, else 1")
    args = parser.parse_args(argv)
    if args.await_codex_tui:
        _await_tui_and_project(args.session_id, not args.no_report_session,
                               session_seq=args.seq,
                               session_start_source=args.session_start_source)
        return 0
    if args.may_report:
        return 0 if may_report(args.harness, args.session_id) else 1
    if args.print:
        agent, title = compose(args.harness, args.session_id)
        print(json.dumps({"display_agent": agent, "title": title}, ensure_ascii=False))
        return 0
    # Only the existing OpenCode callback consumes this bounded observation.
    # Keep project()'s fail-soft bool and the other harnesses' quiet CLI contract.
    observation = {} if args.harness == "opencode" else None
    lifecycle = {}
    if args.seq is not None or args.session_start_source is not None:
        lifecycle = dict(session_seq=args.seq, session_start_source=args.session_start_source)
    project(args.harness, args.session_id, pane_id=args.pane,
            report_session=not args.no_report_session, observation=observation, **lifecycle)
    if observation is not None:
        print(json.dumps(observation, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
