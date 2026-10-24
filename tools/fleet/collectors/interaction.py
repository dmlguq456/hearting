"""Neutral Fleet interaction-wait enrichment (read-only, fail open)."""

from pathlib import Path
import time

from .. import interaction


def enrich(sess, now=None):
    session_id = getattr(sess, "session_id", None)
    harness = getattr(sess, "harness", None)
    if not session_id or not harness:
        return
    record = interaction.pending_wait(
        session_id,
        harness,
        session_start=getattr(sess, "started_at", None),
        activity_since=getattr(sess, "_interaction_activity", None),
        now=now,
    )
    if not record:
        return
    current = time.time() if now is None else float(now)
    if current - float(record["waiting_since"]) >= 7 * 86400:
        return
    candidate = {
        "kind": record["kind"],
        "source": record["source"],
        "waiting_since": record["waiting_since"],
    }
    existing = getattr(sess, "interaction_state", None)
    priorities = {"codex-appserver": 2, "codex-rollout": 1}
    if isinstance(existing, dict):
        existing_priority = priorities.get(existing.get("source"), 0)
        candidate_priority = priorities.get(candidate.get("source"), 0)
        if existing_priority > candidate_priority:
            return
        if (
            existing_priority == candidate_priority
            and isinstance(existing.get("waiting_since"), (int, float))
            and existing["waiting_since"] > candidate["waiting_since"]
        ):
            return
    sess.interaction_state = candidate


def enrich_human_gates(sessions, *, jobs_path=None, now=None):
    """Attach exact session-ledger observations without replacing native evidence.

    A pending gate belongs to one row: among the visible sessions' route-chain
    lines for that route (compose/compile/continuation/start), the row holding
    the latest line at or before the raise. When no line precedes the raise, the
    composing row keeps it.
    """
    import sys
    from pathlib import Path
    from .. import route, route_chain, session_registry
    utilities = str(Path(__file__).resolve().parents[3] / "utilities")
    if utilities not in sys.path:
        sys.path.insert(0, utilities)
    import human_gate_observation
    from . import dispatch

    current = time.time() if now is None else float(now)
    registries = dispatch.human_gate_registry_paths(jobs_path)
    health = []
    by_route = {}
    for index, sess in enumerate(sessions):
        sess.interaction_observations = []
        if getattr(sess, "is_child", False) or getattr(sess, "app_server", False):
            continue
        try:
            join_keys = set(session_registry.session_join_keys(sess))
            lines = route_chain.session_lines(sess, full_history=True)
        except Exception as exc:
            health.append((sess, "route-chain:%s" % type(exc).__name__))
            continue
        for line in lines:
            if (line.get("event") not in route_chain.OWNING_EVENTS
                    or (line.get("harness"), line.get("session_id")) not in join_keys):
                continue
            key = (line.get("route_id"), line.get("route_hash"))
            if not all(key):
                continue
            ts = line.get("ts")
            ts = float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0.0
            by_route.setdefault(key, []).append((ts, index, line))

    found = {}
    for key, entries in by_route.items():
        _ts, _index, line = entries[0]
        owners_for_health = {index for _ts, index, _line in entries}
        try:
            record = route.load(line.get("route_file"), line.get("route_hash"), line.get("route_id"))
        except Exception as exc:
            health.extend((sessions[i], "route-record:%s" % type(exc).__name__) for i in owners_for_health)
            continue
        if record is None:
            continue
        # The loader tags its copy with a Fleet-private schema status that the
        # sealed route hash never covered; the reader must see the sealed bytes.
        record = {name: value for name, value in record.items() if name != "_fleet_schema_status"}
        outcome = route.load_outcome(line.get("route_file"), line.get("route_id"), line.get("route_hash"))
        matches = []
        for registry in registries:
            waits = human_gate_observation.pending_human_waits(record, jobs=registry, outcome=outcome)
            if waits:
                matches.append(waits)
        # Split registries are not inferred from a cwd or duplicate route. Multiple
        # exact journals for this route are ambiguous and stay out of the card.
        if len(matches) > 1:
            health.extend((sessions[i], "ambiguous-registry") for i in owners_for_health)
            continue
        if len(matches) != 1:
            continue
        for wait in matches[0]:
            if current - float(wait["waiting_since"]) >= 7 * 86400:
                continue
            owner = _gate_owner(entries, wait["waiting_since"])
            if owner is None:
                continue
            gate = (owner, wait["route_id"], wait["gate_id"])
            observation = {name: wait[name] for name in ("kind", "source", "waiting_since")}
            previous = found.get(gate)
            if previous is None or observation["waiting_since"] < previous["waiting_since"]:
                found[gate] = observation
    for (index, _route_id, _gate_id), observation in found.items():
        sessions[index].interaction_observations.append(observation)
    for sess in sessions:
        sess.interaction_observations.sort(key=lambda row: row["waiting_since"])
    return health


def _gate_owner(entries, raised_at):
    """Index of the visible row that held the route when the gate was raised."""
    from .. import route_chain
    before = [item for item in entries if item[0] <= raised_at]
    if before:
        # Latest line wins; at an equal time a composing line outranks a bare start.
        return max(before, key=lambda item: (item[0], item[2].get("event") in route_chain.COMPOSING_EVENTS))[1]
    composing = [item for item in entries if item[2].get("event") in route_chain.COMPOSING_EVENTS]
    return min(composing, key=lambda item: item[0])[1] if composing else None
