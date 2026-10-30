"""F-100c — steward (depth −1) flag projection, read-only.

The ledger tool (`utilities/peer-message.py`) keeps one marker per session under
`<dispatch-state-root>/peer-steward/<harness>/<sid>.json`. Since 2026-09-06 the flag
is a ROLE: a marker entry is evidence only when its `source` is `explicit`
(`peer-steward.py steward on`), `watch` (`peer-steward.py wait`/`watch` observed a
real target) or `start` (`peer-steward.py start` launched the target). No `record`
path raises it — not a steer/handoff/gate-relay send and not a SendMessage with
`notify_when_idle` (recorded as `kind=watch`) — under the old rule every worker that
handed off to its steward wore the steward tag. This collector joins markers onto live sessions by exact
(harness, session_id) and asks the ledger tool's `steward_evidence_targets` — the
one definition of the rule — which entries count; a marker with none (an old
handoff-only leftover) is treated as absent, so `steward_targets` holds evidence
entries only. Nothing here writes, and a missing or unreadable marker root (or an
unavailable ledger module) leaves every session's default (`steward=False`).
An explicit self-role entry keeps the flag without inventing a target. Target
relations still require metadata confirming the same repository; foreign and
unknown targets remain communication only.
"""
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace


def _peer_message_module():
    from . import peer_messages
    return peer_messages._load_peer_message()


def _utilities_dir():
    """The repository (or installed release) ``utilities`` directory."""
    return Path(__file__).resolve().parents[3] / "utilities"


def read_markers():
    """``{(harness, session_id): marker}`` over every ledger root the board reads (the
    F-98d resolver chain plus each installed runtime's own root); empty on any failure."""
    mod = _peer_message_module()
    if mod is None:
        return {}
    try:
        from . import peer_messages as _pm
        roots = _pm._state_roots()
    except Exception:
        roots = None
    try:
        return mod.read_steward_markers(roots or None) or {}
    except Exception:
        return {}


def read_relations():
    """Read confirmed current holders without projecting a marker onto a successor SID."""
    try:
        import sys
        utilities = _utilities_dir()
        if str(utilities) not in sys.path:
            sys.path.insert(0, str(utilities))
        import dispatch_seat_handover
        return dispatch_seat_handover.resolved_steward_relations(include_released=True)
    except Exception:
        return []


def _session_keys(sess):
    """The shared join-key rule — a marker written before a resume still names the id the
    session used to have, so an exact-only join silently drops the relation."""
    from .. import session_registry
    keys = session_registry.session_join_keys(sess)
    harness = str(getattr(sess, "harness", "") or "").lower()
    for sid in getattr(sess, "_gpu_session_aliases", ()) or ():
        if sid and (harness, sid) not in keys:
            keys.append((harness, sid))
    return keys


def _repository_key(cwd):
    from ..gitinfo import resolve_gitdir
    try:
        return resolve_gitdir(cwd)[1] if cwd else None
    except (OSError, ValueError, TypeError):
        return None


def _registry_sessions():
    """Reuse native session metadata; no process, herdr or role probe."""
    from .. import session_registry
    rows = []
    for harness in ("claude", "codex", "opencode"):
        try:
            entries = os.listdir(session_registry._dir_for(harness))
        except OSError:
            continue
        for entry in entries:
            if not entry.endswith(".json") or not entry[:-5].isdigit():
                continue
            record = session_registry.read(harness, int(entry[:-5]))
            if record and record.get("sessionId") and record.get("cwd"):
                rows.append(SimpleNamespace(harness=harness, session_id=record["sessionId"],
                                            cwd=record["cwd"], runtime_name=record.get("name")))
    return rows


def _projection_sessions():
    """Read the same native/herdr metadata as Fleet, without a process/role probe."""
    from . import herdr
    rows = []
    for agent in herdr.list_agents() or ():
        identity = agent.get("agent_session") or {}
        sid = identity.get("value") if isinstance(identity, dict) else None
        harness = str(agent.get("agent") or "").lower()
        cwd = agent.get("cwd")
        pane = agent.get("pane_id")
        if not sid:
            native = herdr.pane_session_metadata(harness, pane, cwd)
            sid = native.get("sid") if native else None
        if harness and cwd:
            rows.append(SimpleNamespace(harness=harness, session_id=sid, cwd=cwd,
                                        _herdr_name=agent.get("name"),
                                        session_aliases=herdr.pane_session_aliases(harness, sid, pane, cwd),
                                        _gpu_session_aliases=herdr._clear_gpu_session_aliases(
                                            harness, sid, pane, cwd)))
    registry_rows = _registry_sessions()
    live_keys = {(str(getattr(row, "harness", "") or "").lower(),
                  getattr(row, "session_id", None))
                 for row in [*rows, *registry_rows] if getattr(row, "session_id", None)}
    for row in rows:
        harness = str(getattr(row, "harness", "") or "").lower()
        for field in ("session_aliases", "_gpu_session_aliases"):
            aliases = getattr(row, field, None) or ()
            setattr(row, field, [sid for sid in aliases if (harness, sid) not in live_keys])
    projected_keys = {(str(getattr(row, "harness", "") or "").lower(),
                       getattr(row, "session_id", None))
                      for row in rows if getattr(row, "session_id", None)}
    return rows + [row for row in registry_rows
                   if (str(getattr(row, "harness", "") or "").lower(),
                       getattr(row, "session_id", None)) not in projected_keys]


def _native_role_sessions(markers, rows, relations=()):
    """Exact native directory metadata for evidenced actors/targets absent from the snapshot.

    An OpenCode pane can omit its SID, and old process registry files can be
    absent. Read only named actors and evidenced targets from the native metadata tables
    (OpenCode ``session_v2`` first, then the legacy ``session`` table), never
    conversation bodies, process state, or an inferred same-cwd identity.
    """
    from . import codex, opencode
    present = {key for row in rows for key in _session_keys(row)}
    wanted = set(markers)
    mod = _peer_message_module()
    evidence = getattr(mod, "steward_evidence_targets", None) if mod is not None else None
    for marker in markers.values():
        try:
            targets = evidence(marker) if evidence is not None else ()
        except Exception:
            continue
        for target in targets:
            harness = str(target.get("harness") or "").lower()
            sid = target.get("session_id")
            if sid:
                wanted.add((harness, sid))
    for relation in relations:
        if relation.get("released") is True:
            continue
        for target in relation.get("targets") or ():
            harness = str(target.get("harness") or "").lower()
            sid = target.get("session_id")
            if sid:
                wanted.add((harness, sid))
    out = []
    for harness, db, queries in (
            ("codex", codex._state_db(codex._home()),
             ("SELECT cwd FROM threads WHERE id=?",)),
            ("opencode", opencode._db(),
             ("SELECT directory FROM session_v2 WHERE id=?",
              "SELECT directory FROM session WHERE id=?"))):
        ids = sorted({sid for h, sid in wanted if h == harness and sid
                      and (h, sid) not in present})
        if not ids or not db:
            continue
        try:
            with closing(sqlite3.connect(Path(db).absolute().as_uri() + "?mode=ro",
                                         uri=True, timeout=0.2)) as con:
                for sid in ids:
                    for query in queries:
                        try:
                            result = con.execute(query, (sid,)).fetchone()
                        except sqlite3.OperationalError:
                            continue
                        if result and isinstance(result[0], str) and result[0]:
                            out.append(SimpleNamespace(harness=harness, session_id=sid,
                                                       cwd=result[0]))
                            break
        except (OSError, sqlite3.Error):
            continue
    return out


def _role_winners(markers, rows, evidence, relations=()):
    """Newest evidenced same-repo supervisor per canonical target; no marker mutation."""
    from .peer_messages import _parse_ts
    by_key, by_name, repos = {}, {}, {}
    for row in rows:
        repos[id(row)] = _repository_key(getattr(row, "cwd", None))
        for key in _session_keys(row):
            by_key.setdefault(key, []).append(row)
        for name in {getattr(row, "runtime_name", None), getattr(row, "_herdr_name", None)} - {None, ""}:
            by_name.setdefault((row.harness, name), []).append(row)

    def one(candidates):
        if any(not row.session_id for row in candidates):
            return None
        identities = {(row.harness, row.session_id) for row in candidates}
        repositories = {repos[id(row)] for row in candidates}
        if len(identities) != 1 or len(repositories) != 1 or None in repositories:
            return None
        return next(iter(identities)), next(iter(repositories))

    resolved = []
    try:
        import sys
        utilities = _utilities_dir()
        if str(utilities) not in sys.path:
            sys.path.insert(0, str(utilities))
        import dispatch_seat_handover as handover
        known_generations = {str(relation.get("generation")) for relation in relations
                             if relation.get("generation")}
    except Exception:
        handover = None
        known_generations = set()
    for parent_key, marker in markers.items():
        if handover is not None:
            try:
                if handover.steward_marker_generation(marker) in known_generations:
                    continue
            except Exception:
                pass
        parent = one(by_key.get(parent_key, ()))
        if parent is None:
            continue
        try:
            targets = evidence(marker)
        except Exception:
            continue
        for target in targets:
            harness = str(target.get("harness") or "").lower()
            sid = target.get("session_id")
            candidates = by_key.get((harness, sid), ()) if sid else by_name.get((harness, target.get("name")), ())
            child = one(candidates)
            if child is None or child[1] != parent[1]:
                continue
            resolved.append((parent[0], child[0], target))
    for relation in relations:
        if relation.get("released") is True:
            continue
        holder = relation.get("holder") or {}
        parent_key = (str(holder.get("harness") or "").lower(), holder.get("sid"))
        parent = one(by_key.get(parent_key, ()))
        if parent is None:
            continue
        for target in relation.get("targets") or ():
            harness = str(target.get("harness") or "").lower()
            sid = target.get("session_id")
            candidates = by_key.get((harness, sid), ()) if sid else by_name.get((harness, target.get("name")), ())
            child = one(candidates)
            if child is None or child[1] != parent[1]:
                continue
            resolved.append((parent[0], child[0], target))
    starts = {(parent, child) for parent, child, target in resolved
              if target.get("source") == "start" or "start_ts" in target}
    claims = {}
    for parent, child, target in resolved:
        # Waiting for the launcher does not turn its worker into a supervisor.
        # Use canonical, same-repo endpoints so names and resumed IDs agree.
        if ((target.get("source") or target.get("kind")) == "watch"
                and (child, parent) in starts):
            continue
        raw_ts = str(target.get("ts") or "")
        instant = _parse_ts(raw_ts)
        instant = instant if instant is not None else float("-inf")
        rank = (instant, *parent)
        owners = claims.setdefault(child, {})
        previous = owners.get(parent)
        first = (instant, raw_ts)
        order = min(first, previous[3]) if previous else first
        if previous is None or rank > previous[0]:
            owners[parent] = (rank, parent, dict(target, session_id=child[1]), order)
        else:
            owners[parent] = (*previous[:3], order)
    winners = {child: max(owners.values(), key=lambda claim: claim[0])
               for child, owners in claims.items()}
    # Decide peers after the existing handover: only an actor with a winning
    # same-repository target is a supervisor. Relations between those actors
    # are communication, not another layer of supervision. Keep marker bytes.
    supervisors = {claim[1] for claim in winners.values()}
    return {child: claim for child, claim in winners.items() if child not in supervisors}


def _owner_targets(winners, harness, session_id):
    targets = [claim for claim in winners.values() if claim[1] == (harness, session_id)]
    return [entry for _rank, _parent, entry, _order in sorted(targets, key=lambda claim: claim[3])]


def role_targets(harness, session_id, *, aliases=(), markers=None, cwd=None, sessions=None,
                 _winners=None, include_native=False, _relations=None):
    """One same-repository role lookup for Fleet and herdr, with proven prior ids.

    Current snapshot rows override registry history for their exact join keys.
    A name-only marker can join an unambiguous existing native/herdr name; it
    never guesses a repository from a project basename or a display tag.
    """
    if markers is None:
        markers = read_markers()
    # ``enrich`` supplies one relation snapshot for the whole refresh; direct
    # helper callers still get a fresh read by default.
    relations = read_relations() if _relations is None else _relations
    mod = _peer_message_module()
    evidence = getattr(mod, "steward_evidence_targets", None) if mod is not None else None
    if evidence is None:
        return []
    harness = str(harness or "").lower()
    has_marker = any(markers.get((harness, sid)) for sid in [session_id, *aliases] if sid)
    has_relation = any(relation.get("released") is not True
                       and (relation.get("holder") or {}).get("harness") == harness
                       and (relation.get("holder") or {}).get("sid") in {session_id, *aliases}
                       for relation in relations)
    if not has_marker and not has_relation:
        return []
    if _winners is not None:
        return _owner_targets(_winners, harness, session_id)
    rows = _projection_sessions() if sessions is None else list(sessions)
    own_keys = {(harness, sid) for sid in [session_id, *aliases] if sid}
    if cwd is None:
        own_cwds = {row.cwd for row in rows if own_keys.intersection(_session_keys(row))}
        cwd = own_cwds.pop() if len(own_cwds) == 1 else None
    repository = _repository_key(cwd)
    if not repository:
        return []
    own_rows = [row for row in rows if own_keys.intersection(_session_keys(row))]
    own_names = {getattr(row, "_herdr_name", None) for row in own_rows} - {None, ""}
    runtime_names = {getattr(row, "runtime_name", None) for row in own_rows} - {None, ""}
    own = SimpleNamespace(harness=harness, session_id=session_id, cwd=cwd,
                          session_aliases=list(aliases),
                          _herdr_name=next(iter(own_names)) if len(own_names) == 1 else None,
                          runtime_name=next(iter(runtime_names)) if len(runtime_names) == 1 else None)
    rows = [own] + [row for row in rows if not own_keys.intersection(_session_keys(row))]
    if sessions is None or include_native:
        rows += _native_role_sessions(markers, rows, relations)
    winners = _role_winners(markers, rows, evidence, relations)
    return _owner_targets(winners, harness, session_id)


def role_flag(harness, session_id, *, aliases=(), markers=None, cwd=None, sessions=None,
              _targets=None, _relations=None):
    """One display decision: exact self-role evidence or same-repository targets.

    The CLI's unknown/- sentinel is a flag on the current session, never a
    relationship target. Aliases must come from the caller's confirmed native
    clear history, just as in ``role_targets``; marker history creates no rows.
    """
    if not session_id:
        return False
    if markers is None:
        markers = read_markers()
    relations = read_relations() if _relations is None else _relations
    mod = _peer_message_module()
    evidence = getattr(mod, "steward_evidence_targets", None) if mod is not None else None
    if evidence is None:
        return False
    harness = str(harness or "").lower()
    for sid in (session_id, *aliases):
        marker = markers.get((harness, sid))
        if (not isinstance(marker, dict) or marker.get("session_id") != sid
                or marker.get("harness", harness) != harness):
            continue
        if any(target.get("source") == "explicit" and target.get("kind") == "explicit"
               and target.get("harness") == "unknown" and target.get("name") == "-"
               and target.get("session_id") is None and not target.get("pane")
               for target in evidence(marker)):
            return True
    for relation in relations:
        if relation.get("released") is True:
            continue
        holder = relation.get("holder") or {}
        if (holder.get("harness") == harness and holder.get("sid") in {session_id, *aliases}
                and any(target.get("source") == "explicit" and target.get("kind") == "explicit"
                        and target.get("harness") == "unknown" and target.get("name") == "-"
                        and target.get("session_id") is None and not target.get("pane")
                        for target in relation.get("targets") or ())):
            return True
    targets = _targets if _targets is not None else role_targets(
        harness, session_id, aliases=aliases, markers=markers, cwd=cwd, sessions=sessions,
        _relations=relations)
    return bool(targets)


# --- per-repository supervision count (grant limit + Fleet display) -----------------

_DEFAULT_MAX_SUPERVISORS = 2
_dispatch_defaults_module = None


def _steward_limit():
    """``steward.max_supervisors_per_repo`` from the one policy reader; the shipped
    default when the settings cannot be read (no bypass of the limit)."""
    global _dispatch_defaults_module
    try:
        if _dispatch_defaults_module is None:
            import importlib.util
            path = Path(__file__).resolve().parents[3] / "utilities" / "dispatch-defaults.py"
            spec = importlib.util.spec_from_file_location("dispatch_defaults_policy", str(path))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _dispatch_defaults_module = module
        policy, _warnings = _dispatch_defaults_module.load_steward_policy()
        return int(policy["max_supervisors_per_repo"])
    except Exception:
        return _DEFAULT_MAX_SUPERVISORS


def _registry_record(harness, session_id):
    from .. import session_registry
    try:
        entries = os.listdir(session_registry._dir_for(harness))
    except (OSError, ValueError):
        return None
    for entry in entries:
        if not entry.endswith(".json") or not entry[:-5].isdigit():
            continue
        record = session_registry.read(harness, int(entry[:-5]))
        if record and record.get("sessionId") == session_id:
            return record
    return None


class _EndObservation:
    """One pass's reading of the live Claude/Codex/OpenCode session holders, taken lazily
    and at most once per harness, so a pass whose holders all have herdr rows reads no
    /proc."""

    def __init__(self):
        self._seen = {}

    def _once(self, harness, read):
        if harness not in self._seen:
            try:
                self._seen[harness] = read()
            except Exception:
                self._seen[harness] = None
        return self._seen[harness]

    def claude(self):
        def read():
            import sys
            utilities = str(_utilities_dir())
            if utilities not in sys.path:
                sys.path.insert(0, utilities)
            import recipient_lifetime
            return recipient_lifetime.RecipientCensus()
        return self._once("claude", read)

    def codex(self):
        from . import codex
        return self._once("codex", codex.open_rollout_sessions)

    def opencode(self):
        from . import opencode
        return self._once("opencode", opencode.session_visibility)


def end_observation():
    """A fresh per-pass observation for ``session_liveness(..., observation=...)``."""
    return _EndObservation()


def _process_proof(harness, session_id, observation):
    """``"live"`` | ``"ended"`` | ``"unknown"`` from the runtime's own session-ownership
    proof, read backwards: a Claude session no live Claude process claims (registry, exact
    tap, launch argument) under a complete census while it exists in native
    history/transcripts, a Codex rollout nobody holds open, or an OpenCode session no
    client shows and no running runtime can still be running. Only a complete observation
    can say ``ended``; an unreadable table or a PID namespace that hides processes is
    ``unknown``."""
    try:
        if harness == "claude":
            census = observation.claude()
            if census is None:
                return "unknown"
            return {"alive": "live", "ended": "ended"}.get(
                census.state("claude", session_id), "unknown")
        if harness == "codex":
            seen = observation.codex()
            if not seen:
                return "unknown"
            if session_id in seen["open"]:
                return "live"
            if not seen["complete"]:
                return "unknown"
            from . import codex
            return "ended" if codex.exact_rollout_for_session_id(session_id) else "unknown"
        seen = observation.opencode()
        if not seen:
            return "unknown"
        if session_id in seen["visible"]:
            return "live"
        if not seen["complete"]:
            return "unknown"
        from . import opencode
        activity = opencode.session_activity(session_id)
        if activity is None:
            return "unknown"
        updated, idle = activity
        started = seen["earliest_start_ms"]
        if started is None or updated < started or (idle is not None and idle >= updated):
            return "ended"
    except Exception:
        pass
    return "unknown"


def session_liveness(harness, session_id, *, agents=None, observation=None):
    """``"live"`` | ``"ended"`` | ``"unknown"``. Only proof of exit says ``ended``:
    a herdr row means live; Claude, Codex and OpenCode then answer from the processes that
    hold the session (``_process_proof``); otherwise the registry record's process is checked
    (dead pid, recycled pid, or an ``exited`` status). Anything unobserved is
    ``unknown`` and keeps counting."""
    harness = str(harness or "").lower()
    if not session_id:
        return "unknown"
    if agents is None:
        try:
            from . import herdr
            agents = herdr.list_agents()
        except Exception:
            agents = None
    for agent in agents or ():
        identity = agent.get("agent_session") or {}
        if (str(agent.get("agent") or "").lower() == harness and isinstance(identity, dict)
                and identity.get("value") == session_id):
            return "live"
    if harness in ("claude", "codex", "opencode"):
        observation = end_observation() if observation is None else observation
        proof = _process_proof(harness, session_id, observation)
        if proof == "live":
            return "live"
        if proof == "ended" and _registry_record(harness, session_id) is None:
            return "ended"
    try:
        record = _registry_record(harness, session_id)
        if record is None:
            return "unknown"
        pid = record.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int):
            return "unknown"
        if record.get("status") == "exited":
            return "ended"
        from . import liveness
        alive, match = liveness._proc_evidence(pid, record.get("procStart"))
    except Exception:
        return "unknown"
    return "ended" if (not alive or match is False) else "live"


def repository_for_session(harness, session_id, *, sessions=None):
    """Canonical repository key of one session, or ``None``. The cwd comes from the
    session's own row (herdr/registry/native metadata) and only when it is a single
    value; the process cwd is never a substitute."""
    harness = str(harness or "").lower()
    if not session_id:
        return None
    rows = _projection_sessions() if sessions is None else list(sessions)
    own = {(harness, session_id)}
    cwds = {row.cwd for row in rows if getattr(row, "cwd", None) and own.intersection(_session_keys(row))}
    if not cwds:
        cwds = {row.cwd for row in _native_role_sessions({(harness, session_id): {}}, rows)
                if row.harness == harness and row.session_id == session_id}
    return _repository_key(cwds.pop()) if len(cwds) == 1 else None


def _context_rows(markers, relations, sessions):
    rows = _projection_sessions() if sessions is None else list(sessions)
    return rows + _native_role_sessions(markers, rows, relations)


def _holder_name(harness, session_id, rows):
    for row in rows:
        if (harness, session_id) in _session_keys(row):
            name = getattr(row, "runtime_name", None) or getattr(row, "_herdr_name", None)
            if name:
                return name
    try:
        from .. import session_registry
        return session_registry.name_for_session_id(session_id, harness)
    except Exception:
        return None


def _repo_holders(repository_key, *, markers=None, relations=None, sessions=None, agents=None,
                  observation=None, _rows=None):
    """Distinct current holders attributed to ``repository_key``.

    A holder belongs to the repository when its role shows there (``basis="role"``,
    the Fleet flag rule) or its marker recorded the repository at grant time
    (``basis="grant"``) while no row says otherwise. ``counted`` is False only for
    a session whose exit is proven."""
    if not repository_key:
        return []
    markers = read_markers() if markers is None else markers
    relations = read_relations() if relations is None else relations
    mod = _peer_message_module()
    evidence = getattr(mod, "steward_evidence_targets", None) if mod is not None else None
    if evidence is None:
        return []
    rows = _context_rows(markers, relations, sessions) if _rows is None else _rows
    winners = _role_winners(markers, rows, evidence, relations)
    if agents is None:
        try:
            from . import herdr
            agents = herdr.list_agents()
        except Exception:
            agents = None
    out, seen = [], set()
    for relation in relations:
        if relation.get("released") is True:
            continue
        holder = relation.get("holder") or {}
        harness = str(holder.get("harness") or "").lower()
        sid = holder.get("sid")
        if not sid or (harness, sid) in seen:
            continue
        row_repository = repository_for_session(harness, sid, sessions=rows)
        basis = None
        if row_repository == repository_key and role_flag(
                harness, sid, markers=markers, sessions=rows, _relations=relations,
                _targets=_owner_targets(winners, harness, sid)):
            basis = "role"
        elif row_repository is None or row_repository == repository_key:
            origin = relation.get("origin") or {}
            for owner in ((str(origin.get("harness") or "").lower(), origin.get("sid")), (harness, sid)):
                marker = markers.get(owner)
                if isinstance(marker, dict) and marker.get("repository") == repository_key:
                    basis = "grant"
                    break
        if basis is None:
            continue
        seen.add((harness, sid))
        liveness = session_liveness(harness, sid, agents=agents, observation=observation)
        out.append({"harness": harness, "session_id": sid,
                    "name": _holder_name(harness, sid, rows), "liveness": liveness,
                    "basis": basis, "counted": liveness != "ended"})
    return out


def repo_supervision(cwd=None, *, repository_key=None, limit=None, markers=None, relations=None,
                     sessions=None, agents=None, observation=None):
    """The one count of current supervisors in a repository (linked worktrees fold
    into their primary). The grant limit and the Fleet display both call this."""
    key = repository_key or _repository_key(cwd)
    limit = _steward_limit() if limit is None else limit
    holders = _repo_holders(key, markers=markers, relations=relations, sessions=sessions,
                            agents=agents,
                            observation=end_observation() if observation is None else observation)
    counted = sum(1 for holder in holders if holder["counted"])
    return {"repository": Path(key).parent.name if key else None, "repository_key": key,
            "holders": holders, "counted": counted, "limit": limit,
            "over_limit": counted > limit}


def supervision_by_repository(sessions=None, *, limit=None, markers=None, relations=None,
                              agents=None, observation=None):
    """``{repository_key: repo_supervision(...)}`` for every repository that has a
    holder; a thin loop with no counting rule of its own."""
    markers = read_markers() if markers is None else markers
    relations = read_relations() if relations is None else relations
    rows = _context_rows(markers, relations, sessions)
    observation = end_observation() if observation is None else observation
    keys = set()
    for relation in relations:
        if relation.get("released") is True:
            continue
        holder = relation.get("holder") or {}
        harness = str(holder.get("harness") or "").lower()
        sid = holder.get("sid")
        if not sid:
            continue
        key = repository_for_session(harness, sid, sessions=rows)
        if key is None:
            origin = relation.get("origin") or {}
            for owner in ((str(origin.get("harness") or "").lower(), origin.get("sid")), (harness, sid)):
                marker = markers.get(owner)
                if isinstance(marker, dict) and marker.get("repository"):
                    key = marker["repository"]
                    break
        if key:
            keys.add(key)
    return {key: repo_supervision(repository_key=key, limit=limit, markers=markers,
                                  relations=relations, sessions=rows, agents=agents,
                                  observation=observation)
            for key in sorted(keys)}


# The board-wide census from the last collection pass. The MODELS view reads this
# instead of rereading markers, relations and the registry on every draw.
_last_supervision = None


def last_supervision():
    """``supervision_by_repository`` from the last ``enrich`` pass; ``None`` before one ran."""
    return _last_supervision


def _record_supervision(context, markers, relations):
    global _last_supervision
    if not relations:
        _last_supervision = {}
        return
    try:
        from . import herdr
        agents = herdr.list_agents()
    except Exception:
        agents = None
    try:
        _last_supervision = supervision_by_repository(
            context, limit=_steward_limit(), markers=markers, relations=relations, agents=agents,
            observation=end_observation())
    except Exception:
        _last_supervision = None


def enrich(sessions, markers=None):
    for s in sessions:
        s.steward = False
        s.steward_targets = s.steward_parents = None
    if markers is None:
        markers = read_markers()
    relations = read_relations()
    if not markers and not relations:
        _record_supervision((), markers, relations)
        return
    # Reuse registry history for ended targets, but never overrule current rows.
    current_keys = {key for s in sessions for key in _session_keys(s)}
    context = list(sessions) + [s for s in _registry_sessions()
                                if not current_keys.intersection(_session_keys(s))]
    context += _native_role_sessions(markers, context, relations)
    _record_supervision(context, markers, relations)
    mod = _peer_message_module()
    evidence = getattr(mod, "steward_evidence_targets", None) if mod is not None else None
    if evidence is None:
        return
    winners = _role_winners(markers, context, evidence, relations)
    # target key → the stewards that named it. Built from the SAME evidence entries the
    # forward projection uses, so "who watches me" can never claim a relation that the
    # steward's own row does not also show.
    parents_by_key = {}
    for s in sessions:
        keys = _session_keys(s)
        targets = role_targets(s.harness, s.session_id, markers=markers,
                               aliases=[sid for _harness, sid in keys[1:]],
                               cwd=getattr(s, "cwd", None), sessions=context, _winners=winners,
                               _relations=relations)
        s.steward = role_flag(s.harness, s.session_id, markers=markers,
                              aliases=[sid for _harness, sid in keys[1:]], _targets=targets,
                              _relations=relations)
        if not targets:
            continue
        # `steward_evidence_targets` returns oldest-first — the stable order the
        # renderer's front-preserving +N fold relies on.
        s.steward_targets = list(targets)
        for target in targets:
            target_sid = target.get("session_id")
            if not target_sid:
                continue
            parent = {"harness": str(getattr(s, "harness", "") or "").lower(),
                      "session_id": getattr(s, "session_id", None),
                      "name": getattr(s, "runtime_name", None) or getattr(s, "slug", None),
                      "source": target.get("source"), "ts": target.get("ts")}
            key = (str(target.get("harness") or "").lower(), target_sid)
            parents_by_key.setdefault(key, []).append(parent)
    if not parents_by_key:
        return
    for s in sessions:
        parents = []
        seen = set()
        for key in _session_keys(s):
            for parent in parents_by_key.get(key, ()):
                identity = (parent["harness"], parent["session_id"])
                if identity in seen or identity == (str(getattr(s, "harness", "") or "").lower(),
                                                    getattr(s, "session_id", None)):
                    continue      # a session is never its own supervisor
                seen.add(identity)
                parents.append(parent)
        if parents:
            s.steward_parents = parents
