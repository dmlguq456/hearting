#!/usr/bin/env python3
"""Periodic steward check: one ``steward-check`` duty per steward holder.

The duty lives in the shared obligation store and is advanced by the existing
obligation runner; there is no second process, timer or ledger. Once the
configured interval (``steward.check_interval_seconds``) has passed with no
undelivered notice, the duty sends ONE ordinary ``session_notice`` to the holder.
While that notice is undelivered nothing more is created, a long gap is never
caught up, and a role with nothing left to supervise gets one short release
reminder and then no further model turn while its state is unchanged. Only
``peer-steward steward check`` records ``last_checked``. A released role, a
changed holder or a proven-ended session retires the duty and its undelivered
notice, so nothing keeps retrying toward the old holder.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_UTILITIES = Path(__file__).resolve().parent
if str(_UTILITIES) not in sys.path:
    sys.path.insert(0, str(_UTILITIES))

DUTY_KIND = "steward-check"
NOTICE_KEY_PREFIX = "steward-check:"
NOTIFIED_RECHECK_SECONDS = 60.0
DEFAULT_INTERVAL_SECONDS = 7200
_TERMINAL = frozenset({"complete", "cancelled"})
_OPEN_NOTICE_STATES = frozenset({"pending", "claimed", "sent-ambiguous"})
_MAX_NAMES = 5

_POLICY = {"signature": None, "interval": DEFAULT_INTERVAL_SECONDS, "warnings": []}
_THROTTLE: dict[tuple[str, str], float] = {}
_MODULES: dict[str, object] = {}

TEXT_RELEASE_IF_IDLE = (
    "Steward check: no targets remain for you. Leave one status line and start no new work. "
    "If nothing else remains, tidy finished sessions through the existing tidy path and "
    "release your flag with `peer-steward steward off`.")
TEXT_TIDY_AND_RELEASE = (
    "Steward check: all {count} of your targets look finished ({names}). Tidy those sessions "
    "through the existing tidy path, then release your own flag with "
    "`peer-steward steward off` if nothing else remains. Start no new work.")
TEXT_RUN_CHECK = (
    "Periodic steward check is due. Run `peer-steward steward check` and act only on the "
    "targets it lists for you. If it shows no change, leave one or two status lines and "
    "start no new work.")


# ---------------------------------------------------------------- seams / lazy modules

def _now() -> float:
    return time.time()


def _module(name: str, loader):
    if name not in _MODULES:
        _MODULES[name] = loader()
    return _MODULES[name]


def _defaults():
    def load():
        spec = importlib.util.spec_from_file_location(
            "_steward_check_dispatch_defaults", str(_UTILITIES / "dispatch-defaults.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return _module("defaults", load)


def _collector():
    """Fleet's steward collector (role targets, repository and liveness), or None."""
    def load():
        tools = _UTILITIES.parent / "tools"
        if str(tools) not in sys.path:
            sys.path.insert(0, str(tools))
        from fleet.collectors import steward
        return steward
    try:
        return _module("collector", load)
    except Exception:  # noqa: BLE001 - the collector is optional evidence
        return None


def _store(store=None):
    if store is not None:
        return store
    import peer_obligations
    return peer_obligations.ObligationStore()


def duty_id(harness: str, session_id: str) -> str:
    import peer_obligations
    return peer_obligations.stable_duty_id(
        DUTY_KIND, {"harness": harness, "session_id": session_id})


def _identity_of(duty: dict) -> tuple[str, str]:
    identity = (duty.get("intent") or {}).get("identity") or {}
    return str(identity.get("harness") or ""), str(identity.get("session_id") or "")


def _active(duty) -> bool:
    return isinstance(duty, dict) and duty.get("state") not in _TERMINAL


# ---------------------------------------------------------------- policy

def interval_seconds() -> int:
    """The configured check interval; one ``os.stat`` per call while the file is unchanged."""
    try:
        defaults = _defaults()
        path = str(defaults.default_config_path())
        try:
            info = os.stat(path)
            signature = (path, info.st_mtime_ns, info.st_size)
        except OSError:
            signature = (path, None, None)  # no user file: the shipped default applies
        if _POLICY["signature"] != signature:
            if signature[1] is None:
                _POLICY.update(signature=signature, warnings=[],
                               interval=DEFAULT_INTERVAL_SECONDS)
            else:
                policy, warnings = defaults.load_steward_policy()
                _POLICY.update(signature=signature, warnings=list(warnings),
                               interval=int(policy["check_interval_seconds"]))
        return int(_POLICY["interval"])
    except Exception:  # noqa: BLE001 - the shipped default keeps the schedule alive
        return DEFAULT_INTERVAL_SECONDS


def policy_warnings() -> list[str]:
    interval_seconds()
    return list(_POLICY["warnings"])


# ---------------------------------------------------------------- observation seams

def _relations() -> list[dict]:
    import dispatch_seat_handover
    return dispatch_seat_handover.resolved_steward_relations(include_released=False)


def _role_held(harness: str, session_id: str) -> dict:
    """``{"held", "reason", "successor"}`` from the confirmed role relations (no herdr call)."""
    try:
        relations = _relations()
    except Exception:  # noqa: BLE001 - unknown authority never retires a duty
        return {"held": True, "reason": None, "successor": None}
    for relation in relations:
        holder = relation.get("holder") or {}
        if relation.get("released") is not True and (
                holder.get("harness"), holder.get("sid")) == (harness, session_id):
            return {"held": True, "reason": None, "successor": None}
    for relation in relations:
        origin = relation.get("origin") or {}
        holder = relation.get("holder") or {}
        if (relation.get("released") is not True
                and (origin.get("harness"), origin.get("sid")) == (harness, session_id)
                and holder.get("harness") and holder.get("sid")):
            return {"held": False, "reason": "holder-changed",
                    "successor": (holder["harness"], holder["sid"])}
    return {"held": False, "reason": "role-released", "successor": None}


def _role_flagged(harness: str, session_id: str) -> bool:
    """The Fleet steward flag (`role_flag`): explicit self role or a same-repository
    target. Marker evidence alone (a foreign-repository watch) is communication."""
    collector = _collector()
    function = getattr(collector, "role_flag", None)
    if function is None:
        return True  # no collector: keep the existing check rather than go silent
    try:
        return bool(function(harness, session_id))
    except Exception:  # noqa: BLE001
        return True


def _agents():
    try:
        tools = _UTILITIES.parent / "tools"
        if str(tools) not in sys.path:
            sys.path.insert(0, str(tools))
        from fleet.collectors import herdr
        return herdr.list_agents()
    except Exception:  # noqa: BLE001
        return None


def _end_observation():
    """One pass's shared process observation from the collector, or None."""
    function = getattr(_collector(), "end_observation", None)
    try:
        return function() if function is not None else None
    except Exception:  # noqa: BLE001
        return None


def _session_liveness(harness: str, session_id: str, agents=None, observation=None) -> str:
    """``live|ended|unknown``; unknown when the collector lacks the proof."""
    collector = _collector()
    function = getattr(collector, "session_liveness", None)
    if function is None or not session_id:
        return "unknown"
    options = {key: value for key, value in (("agents", agents), ("observation", observation))
               if value is not None}
    try:
        result = function(harness, session_id, **options)
    except Exception:  # noqa: BLE001
        return "unknown"
    return result if result in {"live", "ended", "unknown"} else "unknown"


def _holder_seat_keys(harness: str, session_id: str):
    """Seat keys of the unreleased roles this session holds, or None when unknown."""
    try:
        relations = _relations()
    except Exception:  # noqa: BLE001
        return None
    keys = set()
    for relation in relations:
        holder = relation.get("holder") or {}
        if (relation.get("released") is not True
                and (holder.get("harness"), holder.get("sid")) == (harness, session_id)):
            if not relation.get("seat_key"):
                return None
            keys.add(str(relation["seat_key"]))
    return keys


def _seat_panes(harness: str, session_id: str, seat_keys):
    """``{seat_key: {(pane, server)}}`` from this holder's watch arms, or None.

    A watch arm names the pane its supervisor sat in, and the arm file outlives its
    receipt. A seat with no such arm has no pane evidence, so nothing is concluded."""
    import json
    module = getattr(_collector(), "_peer_message_module", None)
    root = Path(module().peer_state_root()) / "peer-watches" if module else None
    if root is None or not root.is_dir():
        return None
    found = {key: set() for key in seat_keys}
    for path in sorted(root.glob("*.json")):
        if path.name.startswith(".") or path.name.endswith(
                (".receipt.json", ".ack.json", ".observation.json")):
            continue
        try:
            arm = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        steward = arm.get("steward") if isinstance(arm, dict) else None
        seat = arm.get("steward_seat") if isinstance(arm, dict) else None
        if (not isinstance(steward, dict) or not isinstance(seat, dict)
                or (steward.get("harness"), steward.get("session_id")) != (harness, session_id)
                or seat.get("key") not in found or seat.get("kind") != "pane"
                or not seat.get("pane") or not arm.get("steward_server")):
            continue
        found[seat["key"]].add((str(seat["pane"]), str(arm["steward_server"])))
    return found if all(found.values()) else None


def _pane_census(server: str):
    try:
        import herdr_observation
        return herdr_observation.pane_census(session=server)
    except Exception:  # noqa: BLE001
        return None


def _window_closed(harness: str, session_id: str) -> bool:
    """True only when every seat this holder's role sits in was seen empty in a complete
    pane census and the session's liveness is not ``live``.

    The ground for release is therefore "unknown (or ended) liveness plus the absence of
    the same seat's pane", not liveness alone: a holder that is merely unobserved while
    its pane still exists, or whose pane now shows another session, is left as it is.
    A holder that moved to a different pane without a herdr row or process evidence is
    the residual case; it is handled like any session whose proof of life is missing."""
    keys = _holder_seat_keys(harness, session_id)
    if not keys:
        return False
    panes = _seat_panes(harness, session_id, keys)
    if panes is None:
        return False
    try:
        import herdr_observation
    except Exception:  # noqa: BLE001
        return False
    censuses = {}
    for pane, server in sorted({item for values in panes.values() for item in values}):
        if server not in censuses:
            censuses[server] = _pane_census(server)
        if not herdr_observation.confirms_absence(censuses[server], pane, endpoint=server):
            return False
    return _session_liveness(harness, session_id, _agents(), _end_observation()) != "live"


_ACTIVITY_EVENTS = frozenset({"start", "prompt", "real-prompt", "summary"})


def _row_ts(row) -> float:
    try:
        return float(row.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _evidence_ts(relation: dict) -> float:
    """Latest role-evidence time of a relation (``YYYY-mm-ddTHH:MM:SSZ``), 0 if none."""
    latest = 0.0
    for target in relation.get("targets") or ():
        try:
            value = datetime.strptime(str(target.get("ts")), "%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            continue
        latest = max(latest, value.replace(tzinfo=timezone.utc).timestamp())
    return latest


def _seat_cleared(harness: str, session_id: str) -> bool:
    """True only when every seat this holder's role sits in recorded the holder's own
    confirmed same-harness ``/clear``, nothing shows the holder after that clear, and the
    session is not ``live``.

    This is the confirmed same-seat ``/clear`` the session-tidy start hook releases on,
    read again by a later pass from the same shared lineage: the seat's own ledger holds
    the edge out of this session and the lineage resolves the session to a different
    chain end. Without this pass a clear that the hook missed (one taken before that
    release existed, or while the lineage was momentarily unresolved) keeps the
    predecessor's role, duty and watches for good, because the successor keeps the pane
    occupied.

    A clear edge is history, not proof that the session stayed gone, and a resume makes
    no edge. So the pass concludes nothing when, after the last clear edge by row time
    (never ledger order, which compaction regroups), the session has a ``start``,
    ``prompt``, ``real-prompt`` or ``summary`` row in any of its ledgers or newer role
    evidence; nor while this seat's tidy snapshot from the holder still waits for its
    successor's first prompt (the hook's handover can still carry the role). An
    ambiguous lineage, a clear on another seat or a live session is left as it is."""
    try:
        held = [relation for relation in _relations()
                if relation.get("released") is not True
                and ((relation.get("holder") or {}).get("harness"),
                     (relation.get("holder") or {}).get("sid")) == (harness, session_id)]
    except Exception:  # noqa: BLE001
        return False
    if not held or not all(relation.get("seat_key") for relation in held):
        return False
    keys = {str(relation["seat_key"]) for relation in held}
    vertex = (harness, session_id)
    try:
        import dispatch_seat_handover as handover
        import session_tidy as tidy
        graph = handover.lineage_graph()
        end, state = handover._chain_end(graph, harness, session_id)
        if state != "successor" or end == session_id:
            return False
        edges = [(key, row) for key, rows in graph.ledger_rows.items() for row in rows
                 if (tidy.tidy_lineage_edge(row) or ())[:2] == vertex]
        if not keys <= {key for key, _row in edges}:
            return False
        cleared_at = max(_row_ts(row) for _key, row in edges)

        def active_after(who, events):
            return any((row.get("harness"), row.get("sid")) == who
                       and row.get("event") in events and _row_ts(row) > cleared_at
                       for key in graph.ledger_keys.get(who, ())
                       for row in graph.ledger_rows.get(key, ()))

        if active_after(vertex, _ACTIVITY_EVENTS):
            return False
        if any(_evidence_ts(relation) > cleared_at for relation in held):
            return False
        successors = {tidy.tidy_lineage_edge(row)[2] for _key, row in edges}
        for key in keys:
            origin = (handover.read_snapshot(key) or {}).get("from") or {}
            if ((origin.get("harness"), origin.get("sid")) == vertex
                    and not any(active_after((harness, successor), {"prompt", "real-prompt"})
                                for successor in successors)):
                return False
    except Exception:  # noqa: BLE001 - unreadable lineage concludes nothing
        return False
    return _session_liveness(harness, session_id, _agents(), _end_observation()) != "live"


def _release_if_window_closed(duty, store, harness: str, session_id: str, now: float) -> bool:
    """Release a role whose session closed through an ordinary path the hook did not see:
    its seat pane vanished (``window-closed``) or its seat moved on by a confirmed
    ``/clear`` (``cleared``). The release itself is the one ``release_closed_session``;
    while it cannot resolve the role, the duty stays for the next pass."""
    try:
        if _window_closed(harness, session_id):
            reason = "window-closed"
        elif _seat_cleared(harness, session_id):
            reason = "cleared"
        else:
            return False
        module = getattr(_collector(), "_peer_message_module", None)
        peer_message = module() if module else None
        if peer_message is None:
            return False
        summary = peer_message.release_closed_session(harness, session_id, reason=reason)
        if isinstance(summary, dict) and summary.get("roles") == "unresolved":
            return False
        latest = store.get(duty["id"])
        if _active(latest):
            _retire(latest, store, reason, now=now)
        return True
    except Exception:  # noqa: BLE001 - the next pass decides again
        return False


def _human_wait_open(harness: str, session_id: str, now: float) -> bool:
    try:
        import human_wait_state
        return any(signal.get("harness") == harness and signal.get("session_id") == session_id
                   and not signal.get("stale")
                   for signal in human_wait_state.read_signals(now=now))
    except Exception:  # noqa: BLE001 - unreadable signals never block a check
        return False


def _observe_role(harness: str, session_id: str) -> dict:
    """``{"targets": [{name, harness, session_id, state, liveness}], "pending_messages": n}``
    for this holder's own winner targets only."""
    collector = _collector()
    targets = []
    agents = _agents()
    observation = _end_observation()
    by_session = {}
    for agent in agents or ():
        identity = agent.get("agent_session") or {}
        sid = identity.get("value") if isinstance(identity, dict) else None
        if sid:
            by_session[(str(agent.get("agent") or "").lower(), sid)] = agent
    if collector is not None:
        for target in collector.role_targets(harness, session_id) or ():
            t_harness = str(target.get("harness") or "").lower()
            t_sid = target.get("session_id")
            agent = by_session.get((t_harness, t_sid))
            state = (agent or {}).get("agent_status") or "unknown"
            targets.append({
                "name": str(target.get("name") or (t_sid or "-")[:8]),
                "harness": t_harness or "-", "session_id": t_sid,
                "state": state,
                "liveness": (_session_liveness(t_harness, t_sid, agents, observation)
                             if t_sid else "unknown")})
    pending = 0
    try:
        module = collector._peer_message_module() if collector is not None else None
        rows = module.pending_messages({"harness": harness, "session_id": session_id}) if module else []
        pending = len([row for row in rows if row.get("state") == "pending"])
    except Exception:  # noqa: BLE001
        pending = 0
    return {"targets": targets, "pending_messages": pending}


def _signature(action: str, observation: dict) -> str:
    rows = sorted((t["name"], t["harness"], str(t["session_id"]), t["state"], t["liveness"])
                  for t in observation["targets"])
    raw = repr((action, rows, observation["pending_messages"]))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _classify(observation: dict) -> str:
    targets = observation["targets"]
    if not targets and observation["pending_messages"] == 0:
        return "release-if-idle"
    if targets and all(t["state"] == "done" or t["liveness"] == "ended" for t in targets):
        return "tidy-and-release"
    return "run-steward-check"


# ---------------------------------------------------------------- notices

def _notice_key(harness: str, session_id: str, epoch: str, cycle: int) -> str:
    return f"{NOTICE_KEY_PREFIX}{harness}:{session_id}:{epoch}:{cycle}"


def _delivery_id(key: str) -> str:
    return "delivery-" + hashlib.sha256(f"notice\0{key}".encode()).hexdigest()


def _notice_root() -> Path:
    import session_notice
    return session_notice._registry(None).resolve().parent


def _notice_record(session_id: str, delivery_id: str):
    import dispatch_pending_delivery as pending
    try:
        return pending.read(_notice_root(), session_id, delivery_id)
    except Exception:  # noqa: BLE001
        return None


def _retire_notice(session_id: str, delivery_id: str, owner: str) -> None:
    """Close an undelivered notice; a claimed one is closed by the carrier through
    ``session_notice.notice_is_current`` once the duty no longer vouches for it."""
    import dispatch_pending_delivery as pending
    try:
        root = _notice_root()
        record = pending.read(root, session_id, delivery_id)
        if record is None or record.get("state") != "pending":
            return
        pending.claim(root, session_id, delivery_id,
                      claim_owner="steward-check-retire:" + owner, lease_seconds=30,
                      require_generation_proof=False)
        pending.reject_claimed(root, session_id, delivery_id,
                               claim_owner="steward-check-retire:" + owner,
                               reason="notice-obligation-resolved")
    except Exception:  # noqa: BLE001 - the carrier-side currency check covers the rest
        pass


def _parse_key(key: str):
    if not key.startswith(NOTICE_KEY_PREFIX):
        return None
    harness, _, rest = key[len(NOTICE_KEY_PREFIX):].partition(":")
    parts = rest.rsplit(":", 2)
    if not harness or len(parts) != 3 or not parts[0]:
        return None
    return harness, parts[0], parts[1], parts[2]


def notice_is_current(record: dict) -> bool:
    """True only while the duty still vouches for this exact notice."""
    try:
        parsed = _parse_key(str((record.get("receipt") or {}).get("key") or ""))
        if parsed is None:
            return True
        harness, session_id, epoch, _cycle = parsed
        duty = _store().get(duty_id(harness, session_id))
        if not _active(duty):
            return False
        observation = duty.get("observation") or {}
        if (observation.get("epoch") != epoch
                or observation.get("notice_delivery_id") != record.get("delivery_id")):
            return False
        return bool(_role_held(harness, session_id)["held"])
    except Exception:  # noqa: BLE001 - unknown evidence stays recoverable
        return True


# ---------------------------------------------------------------- duty lifecycle

def _new_observation(now: float, *, anchor=None, last_checked=None, reason="registered") -> dict:
    return {"phase": "scheduled", "epoch": os.urandom(8).hex(), "cycle": 0,
            "anchor": float(now if anchor is None else anchor), "notified_at": None,
            "notice_delivery_id": None, "last_checked": last_checked, "skipped": 0,
            "reason": reason, "quiet_signature": None, "check_signature": None}


def _ensure_runner(store) -> None:
    try:
        import peer_obligations
        if not peer_obligations.ensure_normal_runner(store.state_root):
            peer_obligations.request_dispatch_reconciliation(store.state_root)
    except Exception:  # noqa: BLE001 - the next lifecycle bridge retries
        pass


def register(harness: str, session_id: str, *, now: float | None = None,
             anchor: float | None = None, store=None, ensure_runner: bool = True):
    """Idempotent: a pending duty is untouched; a complete one is reactivated."""
    if not harness or not session_id:
        return None
    try:
        import peer_obligations
        from harness_capabilities import parent_completion
        carrier = parent_completion(harness).get("carrier")
        if not carrier:
            return None
        st = _store(store)
        now = _now() if now is None else now
        record = st.create(duty_id(harness, session_id), DUTY_KIND,
                           {"harness": harness, "session_id": session_id},
                           {"carrier": carrier, "schema": 1})
        changed = False
        if record.get("state") in _TERMINAL:
            prior = record.get("observation") or {}
            record = st.update(
                record["id"], state="pending", cleanup="pending",
                observation=_new_observation(now, anchor=anchor,
                                             last_checked=prior.get("last_checked"),
                                             reason="reactivated"))
            changed = True
        elif not isinstance(record.get("observation"), dict):
            record = st.update(record["id"], state="pending",
                               observation=_new_observation(now, anchor=anchor))
            changed = True
        if changed and ensure_runner:
            _ensure_runner(st)
        return record
    except Exception:  # noqa: BLE001 - schedule upkeep never fails a grant or release
        return None


def _retire(duty: dict, store, reason: str, *, successor=None, now: float | None = None) -> None:
    harness, session_id = _identity_of(duty)
    observation = dict(duty.get("observation") or {})
    delivery_id = observation.get("notice_delivery_id")
    if successor:
        register(successor[0], successor[1], now=now, anchor=observation.get("anchor"),
                 store=store, ensure_runner=False)
    store.update(duty["id"], state="complete", cleanup="complete",
                 observation={**observation, "phase": "retired", "reason": reason})
    _THROTTLE.pop((duty["id"], "notified"), None)
    _THROTTLE.pop((duty["id"], "deferred"), None)
    if delivery_id:
        _retire_notice(session_id, delivery_id, duty["id"])


def settle_session(harness: str, session_id: str, *, reason: str, store=None):
    """Retire this holder's duty (role released, holder changed, session ended)."""
    try:
        st = _store(store)
        duty = st.get(duty_id(harness, session_id))
        if not _active(duty):
            return None
        _retire(duty, st, reason)
        return st.get(duty["id"])
    except Exception:  # noqa: BLE001
        return None


def _retire_for_role(duty: dict, store, role: dict, now=None) -> None:
    _retire(duty, store, role.get("reason") or "role-released",
            successor=role.get("successor"), now=now)


def _names(targets: list[dict]) -> str:
    names = [t["name"] for t in targets]
    shown = ", ".join(names[:_MAX_NAMES])
    return shown + (f" +{len(names) - _MAX_NAMES} more" if len(names) > _MAX_NAMES else "")


def _throttled(duty_id_: str, kind: str, now: float) -> bool:
    last = _THROTTLE.get((duty_id_, kind))
    if last is not None and 0 <= now - last < NOTIFIED_RECHECK_SECONDS:
        return True
    _THROTTLE[(duty_id_, kind)] = now
    return False


def advance(duty: dict, store, *, now: float | None = None) -> None:
    """One runner pass for one duty. Before the due time this is a memory comparison."""
    if not _active(duty):
        return
    now = _now() if now is None else now
    harness, session_id = _identity_of(duty)
    observation = duty.get("observation")
    if not isinstance(observation, dict) or not observation.get("phase"):
        store.update(duty["id"], state="pending", observation=_new_observation(now))
        return
    phase = observation["phase"]
    if phase == "retired":
        store.update(duty["id"], state="complete", cleanup="complete")
        return
    if not _throttled(duty["id"], "window", now):
        # A polling cadence for `herdr pane list`, not a time condition on the verdict.
        if _release_if_window_closed(duty, store, harness, session_id, now):
            return
    if phase == "notified":
        _advance_notified(duty, store, observation, harness, session_id, now)
        return
    if now < float(observation.get("anchor") or 0) + interval_seconds():
        return
    _due(duty, store, observation, harness, session_id, now)


def _advance_notified(duty, store, observation, harness, session_id, now) -> None:
    if _throttled(duty["id"], "notified", now):
        return
    delivery_id = observation.get("notice_delivery_id")
    record = _notice_record(session_id, delivery_id) if delivery_id else None
    state = (record or {}).get("state")
    if state == "acked":
        anchor = max(float(observation.get("anchor") or 0), now,
                     float(observation.get("last_checked") or 0))
        store.update(duty["id"], observation={**observation, "phase": "scheduled",
                                              "anchor": anchor, "reason": "notice-acked"})
    elif state in _OPEN_NOTICE_STATES:
        role = _role_held(harness, session_id)
        if not role["held"]:
            _retire_for_role(duty, store, role, now)
        elif _session_liveness(harness, session_id) == "ended":
            _retire(duty, store, "session-ended", now=now)
    else:  # rejected, expired or gone: schedule from now
        store.update(duty["id"], observation={**observation, "phase": "scheduled",
                                              "anchor": now, "reason": f"notice-{state or 'missing'}"})


def _due(duty, store, observation, harness, session_id, now) -> None:
    role = _role_held(harness, session_id)
    if not role["held"]:
        _retire_for_role(duty, store, role, now)
        return
    if _session_liveness(harness, session_id) == "ended":
        _retire(duty, store, "session-ended", now=now)
        return
    deferred_at = _THROTTLE.get((duty["id"], "deferred"))
    if deferred_at is not None and 0 <= now - deferred_at < NOTIFIED_RECHECK_SECONDS:
        return
    if _human_wait_open(harness, session_id, now):
        _THROTTLE[(duty["id"], "deferred")] = now
        if observation.get("reason") != "human-wait-deferred":
            store.update(duty["id"], observation={**observation, "reason": "human-wait-deferred"})
        return
    _THROTTLE.pop((duty["id"], "deferred"), None)
    if not _role_flagged(harness, session_id):
        # Evidence without the steward flag (only a foreign-repository target):
        # no notice and no model turn; the duty waits for a role or a release.
        store.update(duty["id"], observation={
            **observation, "anchor": now, "skipped": int(observation.get("skipped") or 0) + 1,
            "reason": "no-steward-flag"})
        return
    seen = _observe_role(harness, session_id)
    action = _classify(seen)
    signature = _signature(action, seen)
    quiet = action != "run-steward-check"
    if quiet and signature == observation.get("quiet_signature"):
        store.update(duty["id"], observation={
            **observation, "anchor": now, "skipped": int(observation.get("skipped") or 0) + 1,
            "reason": "quiet-no-change"})
        return
    if action == "release-if-idle":
        text = TEXT_RELEASE_IF_IDLE
    elif action == "tidy-and-release":
        text = TEXT_TIDY_AND_RELEASE.format(count=len(seen["targets"]), names=_names(seen["targets"]))
    else:
        text = TEXT_RUN_CHECK
    cycle = int(observation.get("cycle") or 0)
    key = _notice_key(harness, session_id, observation["epoch"], cycle)
    delivery_id = _delivery_id(key)
    # Record the intent first so a carrier claiming right away already finds the
    # duty vouching for this exact notice.
    sent = {**observation, "phase": "notified", "cycle": cycle + 1, "notified_at": now,
            "notice_delivery_id": delivery_id, "reason": action,
            "quiet_signature": signature if quiet else None}
    store.update(duty["id"], observation=sent)
    _THROTTLE[(duty["id"], "notified")] = now
    try:
        import session_notice
        record = session_notice.notify(harness, session_id, key=key, subject="steward-check",
                                       text=text, required_action=action)
    except Exception:  # noqa: BLE001
        record = None
    if record is None:
        store.update(duty["id"], observation={**observation, "anchor": now,
                                              "reason": "notice-unavailable"})


# ---------------------------------------------------------------- reconcile

def _marker_exists(store, harness: str, session_id: str) -> bool:
    safe = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in str(session_id or ""))
    path = Path(store.state_root) / "peer-steward" / str(harness or "unknown") / f"{safe or 'unknown'}.json"
    return path.exists()


def reconcile_self(store=None, *, identity=None) -> None:
    """Register (or retire) the calling session's duty from its own role state."""
    try:
        if identity is None:
            import session_identity
            identity = session_identity.identity()
        harness, session_id = getattr(identity, "harness", ""), getattr(identity, "session_id", "")
        if not harness or not session_id:
            return
        st = _store(store)
        duty = st.get(duty_id(harness, session_id))
        marker = _marker_exists(st, harness, session_id)
        if not marker and not _active(duty):
            return
        if marker and _active(duty):
            return  # consistent; the runner re-verifies at each due time
        role = _role_held(harness, session_id)
        if role["held"]:
            register(harness, session_id, store=st, ensure_runner=False)
        elif _active(duty):
            _retire_for_role(duty, st, role)
    except Exception:  # noqa: BLE001
        return


def reconcile_all(store) -> None:
    """Once per runner start: register every held, not-ended holder.

    It reads only the role relations and one record per holder, never a store
    listing, so the runner's cold pass keeps its I/O shape; a duty whose holder
    lost the role retires at its own next due point (`_due`)."""
    try:
        relations = _relations()
    except Exception:  # noqa: BLE001
        return
    holders = {}
    for relation in relations:
        holder = relation.get("holder") or {}
        if relation.get("released") is not True and holder.get("harness") and holder.get("sid"):
            holders[(holder["harness"], holder["sid"])] = relation
    if not holders:
        return
    agents = _agents()
    observation = _end_observation()
    for (harness, session_id) in holders:
        try:
            if _session_liveness(harness, session_id, agents, observation) != "ended":
                register(harness, session_id, store=store, ensure_runner=False)
        except Exception:  # noqa: BLE001
            continue


# ---------------------------------------------------------------- steward check command

def _check_digest(seen: dict) -> str:
    parts = [f"{t['name']}@{t['harness']}={hashlib.sha256(repr((t['state'], t['liveness'])).encode()).hexdigest()[:8]}"
             for t in seen["targets"]]
    parts.append("pending=" + str(seen["pending_messages"]))
    return "|".join(sorted(parts))


def _changes(previous, current: str):
    before = dict(item.rsplit("=", 1) for item in (previous or "pending=0").split("|") if "=" in item)
    after = dict(item.rsplit("=", 1) for item in current.split("|") if "=" in item)
    count = sum(1 for key in set(before) | set(after) if before.get(key) != after.get(key))
    return "none" if count == 0 else count


def cmd_check(harness: str, session_id: str, *, now: float | None = None, out=None) -> int:
    out = sys.stdout if out is None else out
    now = _now() if now is None else now
    role = _role_held(harness, session_id)
    if not role["held"]:
        print("steward-check role=no", file=out)
        return 0
    st = _store()
    duty = st.get(duty_id(harness, session_id))
    if not _active(duty):
        duty = register(harness, session_id, now=now, store=st)
    seen = _observe_role(harness, session_id)
    digest = _check_digest(seen)
    observation = dict((duty or {}).get("observation") or {})
    changes = _changes(observation.get("check_signature"), digest)
    undelivered = 0
    delivery_id = observation.get("notice_delivery_id")
    if observation.get("phase") == "notified" and delivery_id:
        record = _notice_record(session_id, delivery_id)
        undelivered = 1 if (record or {}).get("state") in _OPEN_NOTICE_STATES else 0
    interval = interval_seconds()
    if _active(duty):
        st.update(duty["id"], observation={
            **(observation or _new_observation(now)), "phase": "scheduled", "last_checked": now,
            "anchor": now, "check_signature": digest, "notice_delivery_id": None,
            "reason": "checked"})
        if delivery_id:
            _retire_notice(session_id, delivery_id, duty["id"])
        _THROTTLE.pop((duty["id"], "notified"), None)
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    targets = seen["targets"]
    count = len(targets) or "none"
    print(f"steward-check role=yes harness={harness} session_id={session_id} "
          f"targets={count} changes={changes} checked_at={stamp}", file=out)
    for warning in policy_warnings():
        print(f"warning={warning}", file=out)
    if not targets:
        print("hint=nothing to supervise; tidy finished sessions and release with "
              "peer-steward steward off", file=out)
        return 0
    for target in targets:
        print(f"target name={target['name']} harness={target['harness']} "
              f"state={target['state']} liveness={target['liveness']}", file=out)
    print(f"pending_messages={seen['pending_messages']} undelivered_notices={undelivered}", file=out)
    print(f"next_due_in_minutes={-(-interval // 60)}", file=out)
    return 0
