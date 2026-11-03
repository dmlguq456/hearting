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
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import wait
from dataclasses import replace

from ..refresh import background_read

_TIMEOUT_S = 2.0
_OBSERVATION_WAIT_MAX_S = 0.75
_OBSERVATION_LOCK = threading.RLock()
_OBSERVATION_TRANSPORTS = {}
_OBSERVATION_FUTURES = {}
_OBSERVATION_VIEWS = {}
_OBSERVATION_ERRORS = {}
_OBSERVATION_LAST_STARTED = {}
_OBSERVATION_REFRESH_PENDING = {}
# Endpoint/socket keys whose one bounded first ``list_agents`` wait was used.
_OBSERVATION_FIRST_WAIT_SPENT = set()
_OBSERVATION_MIN_REFRESH_S = 0.25
_PANE_INFO_CACHE = OrderedDict()
_PANE_INFO_CACHE_LIMIT = 512


def _observation_module():
    from pathlib import Path
    utilities = str(Path(__file__).resolve().parents[3] / "utilities")
    if utilities not in sys.path:
        sys.path.insert(0, utilities)
    import herdr_observation
    return herdr_observation


def observation_view(session=None, *, refresh=False, wait_timeout=0.0):
    """Reuse the last complete view while one read runs for this endpoint/socket."""
    try:
        observation = _observation_module()
        endpoint = observation.endpoint_name(session)
        socket_path = observation.socket_path_for_endpoint(session)
        if not socket_path:
            return None
        key = (endpoint, os.path.realpath(socket_path))
        with _OBSERVATION_LOCK:
            transport = _OBSERVATION_TRANSPORTS.get(key)
            if transport is None:
                # A failed socket or snapshot must not delay Fleet's basic
                # process/NOW/resource publication. Transport work is bounded
                # off-thread and reused by the next collector lifetime.
                transport = observation.ObservationTransport(
                    endpoint, socket_path, timeout=0.15)
                _OBSERVATION_TRANSPORTS[key] = transport
            future = _OBSERVATION_FUTURES.get(key)
            completed_now = False
            if future is not None:
                if not future.done() and refresh:
                    _OBSERVATION_REFRESH_PENDING[key] = True
                    return None
                if future.done():
                    _OBSERVATION_FUTURES.pop(key, None)
                    _store_observation_result(key, future)
                    completed_now = True
            if _OBSERVATION_REFRESH_PENDING.pop(key, False):
                refresh = True
            if refresh:
                transport.close()
                _OBSERVATION_VIEWS.pop(key, None)
                _OBSERVATION_ERRORS.pop(key, None)
                completed_now = False
            elif completed_now:
                # All consumers in this tick share the just-completed result.
                # Do not immediately start another read from this same call.
                return _observation_view_for_key(key)
            elif future is None:
                last_started = _OBSERVATION_LAST_STARTED.get(key, 0.0)
                if time.monotonic() - last_started < _OBSERVATION_MIN_REFRESH_S:
                    return _observation_view_for_key(key)

            if future is None or refresh:
                future = background_read(transport.observe)
                _OBSERVATION_FUTURES[key] = future
                _OBSERVATION_LAST_STARTED[key] = time.monotonic()

            cached = _observation_view_for_key(key, pending=True)
            if wait_timeout <= 0:
                return cached

        try:
            completed = future.result(timeout=min(
                _OBSERVATION_WAIT_MAX_S, float(wait_timeout)))
        except TimeoutError:
            return cached
        except Exception as exc:
            with _OBSERVATION_LOCK:
                if _OBSERVATION_FUTURES.get(key) is future:
                    _OBSERVATION_FUTURES.pop(key, None)
                    _OBSERVATION_ERRORS[key] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
                return _observation_view_for_key(key)
        with _OBSERVATION_LOCK:
            if _OBSERVATION_FUTURES.get(key) is future:
                _OBSERVATION_FUTURES.pop(key, None)
                _store_observation_value(key, completed)
            return _observation_view_for_key(key)
    except Exception:
        return None


def _store_observation_value(key, value):
    if value is not None and getattr(value, "complete", False) is True:
        _OBSERVATION_VIEWS[key] = value
        _OBSERVATION_ERRORS.pop(key, None)
    else:
        _OBSERVATION_ERRORS[key] = "herdr returned no complete snapshot"


def _store_observation_result(key, future):
    try:
        value = future.result()
    except Exception as exc:
        _OBSERVATION_ERRORS[key] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
    else:
        _store_observation_value(key, value)


def _observation_view_for_key(key, *, pending=False):
    view = _OBSERVATION_VIEWS.get(key)
    if view is None:
        return None
    active = _OBSERVATION_FUTURES.get(key)
    pending = pending or (active is not None and not active.done())
    if pending or key in _OBSERVATION_ERRORS:
        try:
            return replace(view, complete=False)
        except (TypeError, ValueError):
            return view
    return view


def _observation_error(session=None):
    try:
        observation = _observation_module()
        endpoint = observation.endpoint_name(session)
        socket_path = observation.socket_path_for_endpoint(session)
        if not socket_path:
            return None
        key = (endpoint, os.path.realpath(socket_path))
        with _OBSERVATION_LOCK:
            error = _OBSERVATION_ERRORS.get(key)
            future = _OBSERVATION_FUTURES.get(key)
            if error:
                return error
            if future is not None and not future.done() and key in _OBSERVATION_VIEWS:
                return "herdr refresh pending"
    except Exception:
        return None
    return None


def _spend_first_observation_wait(session=None):
    """True once per endpoint/socket that has not yet produced a complete view.

    A fresh process's first read only starts the subscription; one-shot
    consumers would otherwise always see "unknown". The wait is spent whatever
    its outcome, so a long-running consumer never blocks after its first call.
    """
    try:
        observation = _observation_module()
        endpoint = observation.endpoint_name(session)
        socket_path = observation.socket_path_for_endpoint(session)
        if not socket_path:
            return False
        key = (endpoint, os.path.realpath(socket_path))
        with _OBSERVATION_LOCK:
            if key in _OBSERVATION_VIEWS or key in _OBSERVATION_FIRST_WAIT_SPENT:
                return False
            _OBSERVATION_FIRST_WAIT_SPENT.add(key)
            return True
    except Exception:
        return False


def close_observations():
    """Close endpoint subscriptions when their Fleet consumer exits."""
    with _OBSERVATION_LOCK:
        transports = list(_OBSERVATION_TRANSPORTS.values())
        futures = list(_OBSERVATION_FUTURES.values())
        _OBSERVATION_TRANSPORTS.clear()
        _OBSERVATION_FUTURES.clear()
        _OBSERVATION_VIEWS.clear()
        _OBSERVATION_ERRORS.clear()
        _OBSERVATION_LAST_STARTED.clear()
        _OBSERVATION_REFRESH_PENDING.clear()
        _OBSERVATION_FIRST_WAIT_SPENT.clear()
        _PANE_INFO_CACHE.clear()
    if futures:
        wait(futures, timeout=0.8)
    for transport in transports:
        try:
            transport.close()
        except Exception:
            pass


def verified_id_harnesses():
    """Harnesses whose Fleet session_id is known to equal herdr's ``agent_session.value``.

    Each adapter declares it (`harness-capabilities.json` ``session_identity.herdr_session_id``;
    measured 2026-09-03, herdr 0.8: Claude UUID ↔ ``sessionId``, Codex thread id ↔ the
    rollout session_id). A harness outside this set can be promoted to True by a match but
    never demoted to False.
    """
    import sys
    from pathlib import Path
    utilities = str(Path(__file__).resolve().parents[3] / "utilities")
    if utilities not in sys.path:
        sys.path.insert(0, utilities)
    from harness_capabilities import herdr_verified_harnesses
    return herdr_verified_harnesses()
_CODEX_PANE_CACHE = {"key": None, "until": 0.0, "identities": {}}
_CODEX_PANE_TTL_S = 10.0


def list_agents(runner=subprocess.run, which=shutil.which):
    """``[agent dict, ...]`` from ``herdr agent list``; ``None`` when herdr is absent,
    the call fails/times out, or the payload is not the documented shape."""
    if which("herdr") is None:
        return None
    observation = None
    if runner is subprocess.run and which is shutil.which:
        observation = _observation_module()
        view = observation_view()
        if view is None and _spend_first_observation_wait():
            view = observation_view(wait_timeout=_OBSERVATION_WAIT_MAX_S)
        if view is not None:
            return [dict(agent) for agent in view.agents]
        # An explicitly selected socket that did not produce a complete
        # subscription/snapshot is unknown. Do not fall through to a second,
        # potentially different endpoint or stall basic Fleet discovery.
        if observation.socket_path_for_endpoint():
            return None
    try:
        endpoint = observation.endpoint_name() if observation is not None else "default"
        argv = ["herdr"]
        if endpoint != "default":
            argv.extend(("--session", endpoint))
        argv.extend(("agent", "list"))
        proc = runner(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
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


def list_panes(runner=subprocess.run, which=shutil.which, *, wait_timeout=0.0,
               with_observation=False):
    """Return pane dicts, or their paired observation when ``with_observation`` is set."""
    try:
        observation = _observation_module()
        if runner is subprocess.run and which is shutil.which:
            view = (observation_view(wait_timeout=wait_timeout)
                    if wait_timeout > 0 else observation_view())
            if view is not None:
                panes = [dict(pane) for pane in view.panes]
                return (panes, view) if with_observation else panes
            if observation.socket_path_for_endpoint():
                return (None, None) if with_observation else None
        census = observation.pane_census(runner=runner, which=which)
        panes = list(census.panes) if census is not None else None
        return (panes, None) if with_observation else panes
    except Exception:
        return (None, None) if with_observation else None


class PaneEvidence(tuple):
    """Unpackable PID evidence with negative completeness and fresh-pane provenance."""
    def __new__(cls, values, complete=True, errors=(), fresh_shells=None,
                fresh_foreground=None, pane_infos=None):
        result = super().__new__(cls, values)
        result.complete, result.errors = complete, tuple(errors)
        shells = result[0] if len(result) > 0 else set()
        foreground = result[1] if len(result) > 1 else set()
        result.fresh_shells = set(
            (shells if complete else ()) if fresh_shells is None else fresh_shells)
        result.fresh_foreground = set(
            (foreground if complete else ())
            if fresh_foreground is None else fresh_foreground)
        result.pane_infos = {str(pane): dict(info) for pane, info in (pane_infos or {}).items()}
        return result


def pane_evidence(panes, runner=subprocess.run, bindings=None, observation=None):
    """Return shell PIDs, foreground PIDs, and exact Codex PID → thread IDs.

    A pane's session metadata alone can outlive its foreground process. Only a live
    foreground ``codex`` process reported by that same pane may supply an identity;
    conflicting pane claims for one PID are discarded.
    """
    shells, fg = set(), set()
    fresh_shells, fresh_fg = set(), set()
    identities = {}
    pane_infos = {}
    errors = []
    selected = [pane for pane in panes or [] if pane.get("agent")]

    def read(pane):
        pane_id = pane.get("pane_id")
        if not pane_id:
            raise ValueError("pane id missing")
        cache_key = None
        if observation is not None and runner is subprocess.run:
            cache_key = (observation.endpoint, observation.socket_path,
                         observation.generation,
                         observation.pane_revisions.get(str(pane_id), 0), str(pane_id))
            with _OBSERVATION_LOCK:
                cached = _PANE_INFO_CACHE.get(cache_key)
                if cached is not None:
                    _PANE_INFO_CACHE.move_to_end(cache_key)
                    return dict(cached), False
        if observation is not None:
            endpoint = observation.endpoint
        elif runner is subprocess.run:
            try:
                endpoint = _observation_module().endpoint_name()
            except Exception:
                endpoint = "default"
        else:
            endpoint = "default"
        argv = ["herdr"]
        if endpoint != "default":
            argv.extend(("--session", endpoint))
        argv.extend(("pane", "process-info", "--pane", str(pane_id)))
        proc = runner(argv,
                      stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=_TIMEOUT_S)
        if getattr(proc, "returncode", 1) != 0:
            raise RuntimeError("process-info exit %s" % proc.returncode)
        info = (json.loads(proc.stdout or "").get("result") or {}).get("process_info")
        if not isinstance(info, dict):
            raise ValueError("process-info missing")
        if cache_key is not None:
            with _OBSERVATION_LOCK:
                _PANE_INFO_CACHE[cache_key] = dict(info)
                _PANE_INFO_CACHE.move_to_end(cache_key)
                while len(_PANE_INFO_CACHE) > _PANE_INFO_CACHE_LIMIT:
                    _PANE_INFO_CACHE.popitem(last=False)
        return info, True

    # One shared deadline replaces N serial pane deadlines. Daemon reads never
    # hold the TUI open; subprocess.run still owns each child's timeout/cleanup.
    reads = [(pane, background_read(lambda pane=pane: read(pane))) for pane in selected]
    if reads:
        wait([future for _, future in reads], timeout=_TIMEOUT_S)
    for pane, future in reads:
        pane_id = pane.get("pane_id")
        try:
            if not future.done():
                raise TimeoutError("process-info deadline")
            info, fresh = future.result()
            pane_infos[str(pane_id)] = dict(info)
            if info.get("shell_pid"):
                shell_pid = int(info["shell_pid"])
                shells.add(shell_pid)
                if fresh:
                    fresh_shells.add(shell_pid)
            for proc_rec in info.get("foreground_processes") or []:
                if isinstance(proc_rec, dict) and proc_rec.get("pid"):
                    pid = int(proc_rec["pid"])
                    fg.add(pid)
                    if fresh:
                        fresh_fg.add(pid)
                    if bindings is not None and proc_rec.get("name") == pane.get("agent"):
                        bindings.setdefault(pid, set()).add(str(pane_id))
                    agent_session = pane.get("agent_session") or {}
                    if (pane.get("agent") == "codex"
                            and isinstance(agent_session, dict)
                            and agent_session.get("agent") == "codex"
                            and isinstance(agent_session.get("value"), str)
                            and proc_rec.get("name") == "codex"):
                        identities.setdefault(pid, set()).add(agent_session["value"])
        except Exception as exc:
            errors.append("%s: %s: %s" % (pane_id, type(exc).__name__, str(exc)[:120]))
    observation_complete = observation is None or getattr(observation, "complete", True) is True
    return PaneEvidence((shells, fg, {pid: next(iter(sids)) for pid, sids in identities.items()
                                    if len(sids) == 1}),
                        complete=panes is not None and not errors and observation_complete,
                        errors=errors, fresh_shells=fresh_shells,
                        fresh_foreground=fresh_fg, pane_infos=pane_infos)


def pane_pids(panes, runner=subprocess.run, bindings=None, observation=None):
    """F-100c — ``(shell_pids, foreground_pids)`` for agent panes."""
    evidence = pane_evidence(panes, runner=runner, bindings=bindings,
                             observation=observation)
    shells, fg, _identities = evidence
    return PaneEvidence((shells, fg), complete=getattr(evidence, "complete", True),
                        errors=getattr(evidence, "errors", ()),
                        fresh_shells=getattr(evidence, "fresh_shells", None),
                        fresh_foreground=getattr(evidence, "fresh_foreground", None),
                        pane_infos=getattr(evidence, "pane_infos", None))


def codex_pane_sessions(sessions, panes=None, runner=subprocess.run, *, force_current=False,
                        wait_timeout=0.0, observation=None):
    """Process identities for Codex TUIs currently foreground in herdr panes.

    Pane metadata is not thread proof. The live foreground PID's
    ``process_identity`` is authoritative; pane process info confirms ownership.
    """
    from . import procscan

    targets = {
        int(s.pid): s for s in sessions
        if getattr(s, "pid", None) is not None and getattr(s, "proc_start", None)
    }
    if not targets:
        return {}
    key = tuple(sorted((pid, str(s.proc_start), os.path.realpath(s.cwd or ""))
                       for pid, s in targets.items()))
    now = time.monotonic()
    if not force_current and panes is None and runner is subprocess.run \
            and key == _CODEX_PANE_CACHE["key"] \
            and now < _CODEX_PANE_CACHE["until"]:
        found = _CODEX_PANE_CACHE["identities"]
    else:
        if panes is None:
            observed_panes = getattr(observation, "panes", None)
            if observed_panes is not None:
                panes = [dict(pane) for pane in observed_panes]
            elif runner is subprocess.run:
                listed = list_panes(wait_timeout=wait_timeout, with_observation=True)
                if isinstance(listed, tuple) and len(listed) == 2:
                    panes, pane_observation = listed
                    if observation is None:
                        observation = pane_observation
                else:  # a direct test or adapter wrapper may still provide the legacy list
                    panes = listed
            else:
                panes = list_panes(runner=runner, wait_timeout=wait_timeout)
        cwds = {os.path.realpath(s.cwd or "") for s in targets.values()}
        selected = [p for p in panes or []
                    if p.get("agent") == "codex"
                    and os.path.realpath(p.get("cwd") or "") in cwds]
        pane_bindings = {}
        evidence = pane_evidence(selected, runner=runner, bindings=pane_bindings,
                                 observation=observation)
        _shells, foreground, _metadata = evidence
        utilities = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "utilities"))
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        try:
            from pane_ownership import current_codex_thread
            found = {}
            for pid, session in targets.items():
                if pid not in foreground or procscan.read_proc_start(pid) != session.proc_start:
                    continue
                bound_panes = pane_bindings.get(pid, set())
                if len(bound_panes) != 1:
                    continue
                pane_id = next(iter(bound_panes))
                pane_info = getattr(evidence, "pane_infos", {}).get(str(pane_id))
                sid = current_codex_thread(str(pane_id), pid, process_info=pane_info)
                if sid:
                    found[pid] = sid
        except Exception:
            found = {}
        if runner is subprocess.run:
            _CODEX_PANE_CACHE.update(key=key, until=now + _CODEX_PANE_TTL_S,
                                     identities=found)
    return {pid: sid for pid, sid in found.items()
            if pid in targets and procscan.read_proc_start(pid) == targets[pid].proc_start}


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


def _clear_gpu_session_aliases(harness, sid, pane, cwd=None):
    """Confirmed display predecessors from the shared same-repository reader."""
    if harness not in {"claude", "codex", "opencode"} or not sid or not pane or not cwd:
        return []
    try:
        import sys
        from pathlib import Path
        utilities = str(Path(__file__).resolve().parents[3] / "utilities")
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        import session_tidy
        return session_tidy.confirmed_pane_predecessors(harness, sid, pane, cwd)
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
        return []


def pane_session_metadata(harness, pane, cwd):
    """Existing native seat metadata when the pane omits its session ID.

    Read-only display context, not a process identity or a new probe. A foreign
    repository record cannot identify the pane's current displayed project.
    """
    if not pane or harness not in {"claude", "codex", "opencode"}:
        return None
    try:
        import sys
        from pathlib import Path
        utilities = str(Path(__file__).resolve().parents[3] / "utilities")
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        import session_tidy
        from ..gitinfo import resolve_gitdir
        seat = session_tidy.Seat("pane", session_tidy._digest("pane", pane), pane, harness)
        row = session_tidy.latest_session(seat, harness)
        repository = resolve_gitdir(cwd)[1]
        if row and repository and resolve_gitdir(row.get("cwd"))[1] == repository:
            return row
    except Exception:
        pass
    return None


def pane_session_aliases(harness, sid, pane, cwd):
    """Reuse the common seat-history display join, without another pane probe."""
    try:
        import sys
        from pathlib import Path
        utilities = str(Path(__file__).resolve().parents[3] / "utilities")
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        import session_tidy
        return session_tidy.pane_session_aliases(harness, sid, pane, cwd)
    except Exception:
        return []


def _lineage_attachment(sessions, lineage):
    """Reuse launcher evidence when the pane surface cannot be observed."""
    if lineage is None:
        from . import procscan
        lineage = procscan.provenance
    for s in sessions:
        if not _eligible(s):
            continue
        try:
            s.herdr_attached = True if lineage(s.pid) == "herdr" else None
        except Exception:
            s.herdr_attached = None


def enrich(sessions, agents=None, lineage=None, panes=None, pids=None, pane_bindings=None,
           wait_timeout=0.0):
    """Set ``herdr_attached`` on every eligible depth-0 session. ``agents`` = a
    pre-fetched ``list_agents()`` result (``None`` → probe once here); ``panes``/``pids``
    = pre-fetched pane list / ``pane_pids()`` result (``None`` → probe once here);
    ``lineage`` = ``pid -> provenance`` callable used only when herdr is absent."""
    for session in sessions:
        session._gpu_session_aliases = []
        session._herdr_name = None
        session._pane_session_claim = None
    observation = None
    if agents is None and panes is None:
        observation = observation_view(wait_timeout=wait_timeout)
        if observation is not None:
            agents = [dict(agent) for agent in observation.agents]
            panes = [dict(pane) for pane in observation.panes]
        elif _observation_module().socket_path_for_endpoint():
            agents = None
            panes = None
        else:
            agents = list_agents()
    elif agents is None:
        agents = list_agents()
    if agents is None:
        _lineage_attachment(sessions, lineage)
        # Agent-list failure does not erase a supplied foreground observation.
        # For a known herdr process, an absent pane observation cannot confirm
        # its older native registry identity either. Do not issue another probe.
        from ..process_identity import pane_process_claims
        claims = pane_process_claims(sessions, panes, pane_bindings or {}, complete=False)
        for s in sessions:
            s._pane_session_claim = claims.get(s.pid)
        return {"complete": False, "last_error": _observation_error() or "herdr 조회 불가"}
    index = attached_index(agents)
    bindings = {} if pane_bindings is None else pane_bindings
    if pids is None:
        if panes is None:
            listed = list_panes(with_observation=True)
            if isinstance(listed, tuple) and len(listed) == 2:
                panes, pane_observation = listed
                if observation is None:
                    observation = pane_observation
            else:
                panes = listed
        pids = pane_pids(panes, bindings=bindings, observation=observation) if panes is not None else None
    if panes is None:
        _lineage_attachment(sessions, lineage)
    shells, fg = pids if pids else (set(), set())
    probe_ok = pids is not None and getattr(pids, "complete", True)
    fresh_shells = getattr(pids, "fresh_shells", shells if probe_ok else set())
    fresh_fg = getattr(pids, "fresh_foreground", fg if probe_ok else set())
    positive_shells, positive_fg = (shells, fg) if probe_ok else (fresh_shells, fresh_fg)
    from ..process_identity import pane_process_claims
    claims = pane_process_claims(sessions, panes, bindings,
                                 complete=pids is not None and getattr(pids, "complete", True))
    for session in sessions:
        session._pane_session_claim = claims.get(session.pid)
    # A native resume can run inside the still-live foreground runtime. Keep
    # one pane row instead of letting that detached companion make the same
    # display alias ambiguous. Independent live processes remain separate.
    from . import procscan
    companions = set()
    for owner in sessions:
        owner_panes = bindings.get(owner.pid, set())
        if not _eligible(owner) or len(owner_panes) != 1 or not owner.proc_start:
            continue
        pane = next(iter(owner_panes))
        aliases = pane_session_aliases(owner.harness, owner.session_id, pane, owner.cwd)
        for other in sessions:
            agent = index.get((other.harness, other.session_id), {})
            if (other is owner or not _eligible(other) or other.harness != owner.harness
                    or not other.detached or other.session_id not in aliases
                    or agent.get("pane_id") != pane or not other.proc_start
                    or procscan.read_proc_start(owner.pid) != owner.proc_start
                    or procscan.read_proc_start(other.pid) != other.proc_start
                    or not pid_in_panes(other.pid, set(), {owner.pid})):
                continue
            companions.add(id(other))
            owner.session_aliases = list(dict.fromkeys(
                list(getattr(owner, "session_aliases", None) or []) + [other.session_id]))
            for field in ("liveness", "mtime", "title", "summary", "summary_ts", "model", "effort",
                          "ctx_pct", "context", "context_window_tokens", "exec_tool", "work_projection"):
                value = getattr(other, field, None)
                if value is not None:
                    setattr(owner, field, value)
            if (owner.session_id == other.session_id
                    and getattr(owner, "attempt_id", None) == getattr(other, "attempt_id", None)):
                from . import shell_jobs
                owner.shell_jobs = shell_jobs.merge_jobs(
                    getattr(owner, "shell_jobs", None), getattr(other, "shell_jobs", None))
                owner.shell_job_observation = (getattr(owner, "shell_job_observation", None)
                                               or getattr(other, "shell_job_observation", None))
    if companions:
        sessions[:] = [s for s in sessions if id(s) not in companions]
    for s in sessions:
        if not _eligible(s):
            continue
        sid = getattr(s, "session_id", None)
        harness = str(getattr(s, "harness", "") or "").lower()
        observation_complete = observation is None or getattr(observation, "complete", True) is True
        if observation_complete and sid and (harness, sid) in index:
            s.herdr_attached = True
        elif pids is not None and pid_in_panes(
                getattr(s, "pid", None), positive_shells, positive_fg):
            s.herdr_attached = True
        elif probe_ok:
            # herdr answered on both surfaces (or on the id surface for a verified-id
            # harness) and nothing matched → a plain terminal, not a guess.
            s.herdr_attached = False
        else:
            s.herdr_attached = None
        # Herdr can lag a native resume/clear. Its exact foreground PID (or an
        # existing session match) identifies the pane; seat history joins its IDs.
        matching_panes = bindings.get(s.pid, set())
        if not matching_panes and sid and (harness, sid) in index:
            pane = index[(harness, sid)].get("pane_id")
            matching_panes = {pane} if pane else set()
        if len(matching_panes) == 1:
            pane = next(iter(matching_panes))
            names = {agent.get("name") for agent in agents
                     if isinstance(agent, dict) and agent.get("pane_id") == pane
                     and agent.get("agent") == harness
                     and agent.get("name")}
            if len(names) == 1:
                s._herdr_name = names.pop()
            session_cwd = getattr(s, "cwd", None)
            aliases = _clear_gpu_session_aliases(harness, sid, pane, session_cwd)
            pane_aliases = pane_session_aliases(harness, sid, pane, session_cwd)
            if aliases or pane_aliases:
                from . import procscan
                if getattr(s, "proc_start", None) and procscan.read_proc_start(s.pid) == s.proc_start:
                    live = {(other.harness, other.session_id) for other in sessions}
                    s._gpu_session_aliases = [old for old in aliases if (harness, old) not in live]
                    s.session_aliases = list(dict.fromkeys(
                        list(getattr(s, "session_aliases", None) or [])
                        + [other for other in pane_aliases if (harness, other) not in live]))
    errors = list(getattr(pids, "errors", ()))
    last_error = "; ".join(errors) if errors else None
    if not probe_ok and not last_error:
        last_error = _observation_error() or "pane 조회 불가"
    return {"complete": probe_ok, "last_error": last_error}
