"""F-100b — herdr attachment probe (stdlib only).

One ``herdr agent list`` per snapshot answers "is this depth-0 session attached to a
herdr pane RIGHT NOW" by exact session-id match: the JSON carries
``agent_session.value`` (the harness's own session id) per agent. That is a stronger
signal than process lineage (``procscan.provenance``), which answers only "who
launched it" and, with a shell between the harness and herdr, could not even see
herdr until F-100 fixed the walk.

Verdict per session (``Session.herdr_attached``):
  · True  — listed by herdr for this harness + session id, OR (F-100c) the session's
            pid is a foreground process of a herdr pane / descends from a herdr pane's
            shell (``herdr pane process-info``), which is exact for every harness —
            herdr reports no session id at all for OpenCode (measured 2026-09-03).
  · False — herdr answered (agent list + pane probe) and neither signal matched.
  · None  — no evidence either way: herdr absent/unreachable/malformed, or a
            worker/companion row. When herdr is absent the lineage walk is the
            fallback and can only ever promote to True.
Misattribution is worse than absence (PRD F-26): every failure path yields None.
"""
import json
import os
import shutil
import subprocess

_TIMEOUT_S = 2.0
# Harnesses whose Fleet session_id is known to equal herdr's ``agent_session.value``
# (measured 2026-09-03, herdr 0.8: Claude UUID ↔ ``sessionId``; Codex thread id
# ``01a064d8-…`` ↔ the rollout session_id, F-100 comms test). A harness outside this
# set can be promoted to True by a match but never demoted to False.
VERIFIED_ID_HARNESSES = frozenset({"claude", "codex"})


def list_agents(runner=subprocess.run, which=shutil.which):
    """``[agent dict, ...]`` from ``herdr agent list``; ``None`` when herdr is absent,
    the call fails/times out, or the payload is not the documented shape."""
    if which("herdr") is None:
        return None
    try:
        proc = runner(["herdr", "agent", "list"], capture_output=True, text=True,
                      timeout=_TIMEOUT_S)
    except Exception:
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    try:
        payload = json.loads(proc.stdout or "")
    except ValueError:
        return None
    result = payload.get("result") if isinstance(payload, dict) else None
    agents = result.get("agents") if isinstance(result, dict) else None
    if not isinstance(agents, list):
        return None
    return [a for a in agents if isinstance(a, dict)]


def list_panes(runner=subprocess.run, which=shutil.which):
    """``[pane dict, ...]`` from ``herdr pane list``; ``None`` on any failure."""
    if which("herdr") is None:
        return None
    try:
        proc = runner(["herdr", "pane", "list"], capture_output=True, text=True,
                      timeout=_TIMEOUT_S)
        payload = json.loads(proc.stdout or "")
    except Exception:
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    result = payload.get("result") if isinstance(payload, dict) else None
    panes = result.get("panes") if isinstance(result, dict) else None
    if not isinstance(panes, list):
        return None
    return [p for p in panes if isinstance(p, dict)]


def pane_pids(panes, runner=subprocess.run):
    """F-100c — ``(shell_pids, foreground_pids)`` over the panes herdr tags with an
    ``agent`` (one ``pane process-info --pane`` each; measured instant). A pane without
    an agent is skipped: the probe exists to place harness sessions, not shells."""
    shells, fg = set(), set()
    for pane in panes or []:
        if not pane.get("agent"):
            continue
        pane_id = pane.get("pane_id")
        if not pane_id:
            continue
        try:
            proc = runner(["herdr", "pane", "process-info", "--pane", str(pane_id)],
                          capture_output=True, text=True, timeout=_TIMEOUT_S)
            info = (json.loads(proc.stdout or "").get("result") or {}).get("process_info") or {}
        except Exception:
            continue
        try:
            if info.get("shell_pid"):
                shells.add(int(info["shell_pid"]))
            for proc_rec in info.get("foreground_processes") or []:
                if isinstance(proc_rec, dict) and proc_rec.get("pid"):
                    fg.add(int(proc_rec["pid"]))
        except (TypeError, ValueError):
            continue
    return shells, fg


def _ppid_of(pid):
    try:
        with open("/proc/%d/stat" % int(pid)) as f:
            data = f.read()
        return int(data[data.rindex(")") + 1:].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def pid_in_panes(pid, shells, fg, max_depth=6, ppid_of=None):
    """True when ``pid`` is a pane's foreground process or descends from a pane shell."""
    if ppid_of is None:
        ppid_of = _ppid_of          # resolved at call time so tests can patch the module
    try:
        cur = int(pid)
    except (TypeError, ValueError):
        return False
    if cur in fg:
        return True
    for _ in range(max_depth):
        cur = ppid_of(cur)
        if not cur or cur <= 1:
            return False
        if cur in shells or cur in fg:
            return True
    return False


def attached_index(agents):
    """``{(harness, session_id): agent}`` over the listed agents."""
    out = {}
    for agent in agents or []:
        if not isinstance(agent, dict):
            continue
        sess = agent.get("agent_session")
        if not isinstance(sess, dict):
            continue
        harness = str(sess.get("agent") or agent.get("agent") or "").lower()
        value = sess.get("value")
        if harness and isinstance(value, str) and value:
            out[(harness, value)] = agent
    return out


def _eligible(session):
    if getattr(session, "is_child", False) or getattr(session, "app_server", False):
        return False
    if getattr(session, "mem_worker", False):
        return False
    return True


def enrich(sessions, agents=None, lineage=None, panes=None, pids=None):
    """Set ``herdr_attached`` on every eligible depth-0 session. ``agents`` = a
    pre-fetched ``list_agents()`` result (``None`` → probe once here); ``panes``/``pids``
    = pre-fetched pane list / ``pane_pids()`` result (``None`` → probe once here);
    ``lineage`` = ``pid -> provenance`` callable used only when herdr is absent."""
    if agents is None:
        agents = list_agents()
    if agents is None:
        if lineage is None:
            try:
                from . import procscan
                lineage = procscan.provenance
            except Exception:
                lineage = None
        for s in sessions:
            if not _eligible(s) or lineage is None:
                continue
            try:
                s.herdr_attached = True if lineage(s.pid) == "herdr" else None
            except Exception:
                s.herdr_attached = None
        return
    index = attached_index(agents)
    if pids is None:
        if panes is None:
            panes = list_panes()
        pids = pane_pids(panes) if panes is not None else None
    shells, fg = pids if pids else (set(), set())
    probe_ok = pids is not None
    for s in sessions:
        if not _eligible(s):
            continue
        sid = getattr(s, "session_id", None)
        harness = str(getattr(s, "harness", "") or "").lower()
        if sid and (harness, sid) in index:
            s.herdr_attached = True
        elif probe_ok and pid_in_panes(getattr(s, "pid", None), shells, fg):
            s.herdr_attached = True
        elif probe_ok or (sid and harness in VERIFIED_ID_HARNESSES):
            # herdr answered on both surfaces (or on the id surface for a verified-id
            # harness) and nothing matched → a plain terminal, not a guess.
            s.herdr_attached = False
        else:
            s.herdr_attached = None
