"""F-100b — herdr attachment probe (stdlib only).

One ``herdr agent list`` per snapshot answers "is this depth-0 session attached to a
herdr pane RIGHT NOW" by exact session-id match: the JSON carries
``agent_session.value`` (the harness's own session id) per agent. That is a stronger
signal than process lineage (``procscan.provenance``), which answers only "who
launched it" and, with a shell between the harness and herdr, could not even see
herdr until F-100 fixed the walk.

Verdict per session (``Session.herdr_attached``):
  · True  — listed by herdr for this harness + session id.
  · False — herdr answered, the harness's id vocabulary is verified against herdr's
            (Claude today), and the session is not listed.
  · None  — no evidence either way: herdr absent/unreachable/malformed, no session id,
            an unverified-id harness, or a worker/companion row. When herdr is absent
            the lineage walk is the fallback and can only ever promote to True.
Misattribution is worse than absence (PRD F-26): every failure path yields None.
"""
import json
import shutil
import subprocess

_TIMEOUT_S = 2.0
# Harnesses whose Fleet session_id is known to equal herdr's ``agent_session.value``
# (measured 2026-09-03, herdr 0.8: Claude UUID ↔ ``sessionId``). A harness outside
# this set can be promoted to True by a match but never demoted to False.
VERIFIED_ID_HARNESSES = frozenset({"claude"})


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


def enrich(sessions, agents=None, lineage=None):
    """Set ``herdr_attached`` on every eligible depth-0 session. ``agents`` = a
    pre-fetched ``list_agents()`` result (``None`` → probe once here); ``lineage`` =
    ``pid -> provenance`` callable used only when herdr is absent."""
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
    for s in sessions:
        if not _eligible(s):
            continue
        sid = getattr(s, "session_id", None)
        harness = str(getattr(s, "harness", "") or "").lower()
        if not sid:
            s.herdr_attached = None
            continue
        if (harness, sid) in index:
            s.herdr_attached = True
        elif harness in VERIFIED_ID_HARNESSES:
            s.herdr_attached = False
        else:
            s.herdr_attached = None
