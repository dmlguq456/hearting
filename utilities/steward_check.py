#!/usr/bin/env python3
"""Periodic steward check: one ``steward-check`` duty per steward holder.

The duty lives in the shared obligation store and is advanced by the existing
obligation runner; there is no second process, timer or ledger. Once the
configured interval (``steward.check_interval_seconds``) has passed with no
undelivered notice, the duty sends ONE ordinary ``session_notice`` to the holder.
While that notice is undelivered nothing more is created, a long gap is never
caught up. While open work (the repository's ``fleet.session_work`` records that the
shared ``memory.open_stage`` classification keeps open) or a queued peer request
remains, the notice names concrete next actions; it repeats every interval only while
a target is still live and unfinished. Otherwise a role gets one notice per changed
state and then no further model turn while its state is unchanged, and a role with
nothing left gets one short release reminder. Only
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
    "Steward check: no targets, peer requests or open work remain for you. Tidy finished "
    "sessions through the existing tidy path and release your flag with "
    "`peer-steward steward off`.")
TEXT_TIDY_AND_RELEASE = (
    "Steward check: all {count} of your targets look finished ({names}) and no open work "
    "or peer request remains. Tidy those sessions through the existing tidy path, then "
    "release your own flag with `peer-steward steward off`.")
TEXT_RUN_CHECK = (
    "Periodic steward check is due. Run `peer-steward steward check` now and act on the "
    "next actions it lists: assign open work to waiting targets; queued peer requests "
    "deliver on their own. An unchanged state does not mean nothing remains.")
TEXT_ASSIGN_NEXT = (
    "Steward check: work remains. Do these now: {actions}. Then run "
    "`peer-steward steward check`. A status line alone does not move the work.")

# Herdr states in which a live target is waiting for its next step.
_WAITING_STATES = frozenset({"idle", "done", "unused", "stale"})
_MAX_ACTIONS = 4
_MAX_HEADLINE = 70
# Headline or tag words that mark an open record as the user's decision, not a worker's.
_USER_DECISION_WORDS = ("사용자 결정", "user decision")
_USER_DECISION_TAGS = frozenset({"user-decision", "needs-user", "awaiting-user"})


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
        # Native registry rows, as in `_observe_role`: the flag never asks Herdr.
        rows = collector._registry_sessions()
        targets = collector.role_targets(harness, session_id, sessions=rows, include_native=True)
        return bool(function(harness, session_id, sessions=rows, _targets=targets))
    except Exception:  # noqa: BLE001
        return True


def _agents():
    """The shared steward pass intentionally has no Herdr inventory input."""
    return ()


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
    # No Herdr inventory: a missing ``agents`` means none, never a Herdr agent list.
    options = {"agents": () if agents is None else agents}
    if observation is not None:
        options["observation"] = observation
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


def _window_closed(harness: str, session_id: str) -> bool:
    """Release a holder only on positive native proof of exact session end."""
    return _session_liveness(harness, session_id, (), _end_observation()) == "ended"


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
    """Release a role only on exact native end or confirmed clear evidence.

    The release itself is the one ``release_closed_session``; if role resolution is
    uncertain, keep the duty for the next pass.
    """
    try:
        if _window_closed(harness, session_id):
            reason = "session-ended"
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


def _holder_cwd(harness: str, session_id: str):
    """The holder's own single cwd from the collector's session rows, or None."""
    collector = _collector()
    # Native registry rows: the holder's cwd never needs a Herdr agent list.
    rows_of = getattr(collector, "_registry_sessions", None)
    keys_of = getattr(collector, "_session_keys", None)
    if rows_of is None or keys_of is None:
        return None
    try:
        cwds = {row.cwd for row in rows_of() or ()
                if getattr(row, "cwd", None) and (harness, session_id) in keys_of(row)}
    except Exception:  # noqa: BLE001
        return None
    return cwds.pop() if len(cwds) == 1 else None


def _is_open_work(memory, row: dict) -> bool:
    """The shared repo-page classification (``memory.open_stage``); until it ships, only the
    collector's own open types count, so finished, pointer and feedback records never do."""
    open_stage = getattr(memory, "open_stage", None)
    if callable(open_stage):
        return open_stage(row) is not None
    return str(row.get("type") or "").casefold() in getattr(memory, "_OPEN_TYPES", ())


def _is_user_decision(row: dict, headline: str) -> bool:
    tags = {str(tag).strip("[]\"' ").casefold() for tag in row.get("tags") or ()}
    text = headline.casefold()
    return bool(tags & _USER_DECISION_TAGS) or any(word in text for word in _USER_DECISION_WORDS)


def _open_work(harness: str, session_id: str) -> tuple[list[str], list[str]]:
    """``(open work, awaiting the user)`` headlines for the holder's repository from the
    existing Fleet work source (``fleet.session_work.observe``); ([], []) when unknown."""
    cwd = _holder_cwd(harness, session_id)
    if not cwd:
        return [], []
    try:
        tools = _UTILITIES.parent / "tools"
        if str(tools) not in sys.path:
            sys.path.insert(0, str(tools))
        from fleet.collectors import memory
        from fleet.session_work import observe
        rows, _watched = observe(cwd)
    except Exception:  # noqa: BLE001 - backlog is optional evidence
        return [], []
    work, decisions = [], []
    for row in rows or ():
        row = row if isinstance(row, dict) else {}
        headline = " ".join(str(row.get("headline") or "").split())
        if not headline:
            continue
        try:
            if not _is_open_work(memory, row):
                continue
        except Exception:  # noqa: BLE001
            continue
        (decisions if _is_user_decision(row, headline) else work).append(headline)
    return work, decisions


def _pending_count(module, harness: str, session_id) -> int:
    """Peer requests another session sent to ``session_id`` that it has not received yet.

    A runtime notice (``from.harness == "runtime"``: route completions, experiment
    finals and this check's own notice) is not a peer request: its non-delivery is the
    duty's own ``undelivered_notices``, and counting it would let the check's notice
    become the next check's work."""
    if module is None or not session_id:
        return 0
    try:
        rows = module.pending_messages({"harness": harness, "session_id": session_id})
    except Exception:  # noqa: BLE001
        return 0
    return len([row for row in rows or ()
                if row.get("state") == "pending"
                and (row.get("from") or {}).get("harness") != "runtime"])


def _observe_role(harness: str, session_id: str) -> dict:
    """``{"targets": [{name, harness, session_id, state, liveness, owed}],
    "pending_messages": n, "open_work": [headline, ...], "user_decisions": [headline, ...]}``
    for this holder's own winner targets only; ``owed`` and ``pending_messages`` count peer
    requests queued but not yet delivered to that target or to the holder."""
    collector = _collector()
    try:
        module = collector._peer_message_module() if collector is not None else None
    except Exception:  # noqa: BLE001
        module = None
    targets = []
    agents = ()
    observation = _end_observation()
    if collector is not None:
        # Native registry rows only (as `_holder_cwd` and `grant_steward`): no Herdr agent list.
        rows = collector._registry_sessions()
        resolved = collector.role_targets(harness, session_id, sessions=rows, include_native=True) or []
        unresolved_reader = getattr(collector, "unresolved_role_targets", None)
        unresolved = unresolved_reader(harness, session_id, sessions=rows) if callable(unresolved_reader) else []
        for target in [*resolved, *unresolved]:
            t_harness = str(target.get("harness") or "").lower()
            t_sid = target.get("session_id")
            try:
                from peer_steward_native import native_agent
                payload = native_agent(t_harness, t_sid)
                agent = (payload or {}).get("result", {}).get("agent")
            except Exception:
                agent = None
            if target.get("unresolved"):
                agent = None
            state = (agent or {}).get("agent_status") or "unknown"
            targets.append({
                "name": str(target.get("name") or (t_sid or "-")[:8]),
                "harness": t_harness or "-", "session_id": t_sid,
                "state": state,
                "liveness": (_session_liveness(t_harness, t_sid, agents, observation)
                             if t_sid else "unknown"),
                **({"unresolved": True} if target.get("unresolved") else {}),
                "owed": _pending_count(module, t_harness, t_sid)})
    pending = _pending_count(module, harness, session_id)
    work, decisions = _open_work(harness, session_id)
    return {"targets": targets, "pending_messages": pending,
            "open_work": work, "user_decisions": decisions}


def _queued_for(target: dict) -> int:
    """Peer requests still queued for a target that can receive them; an ended session's
    undeliverable rows are not remaining work."""
    return 0 if target.get("liveness") == "ended" else int(target.get("owed") or 0)


def _remaining(observation: dict) -> bool:
    """Whether open work, a user decision or a queued peer request remains for this role."""
    return bool(observation.get("open_work")) or bool(observation.get("user_decisions")) \
        or observation.get("pending_messages", 0) > 0 \
        or any(_queued_for(t) > 0 for t in observation["targets"])


def _waiting(target: dict) -> bool:
    return target.get("liveness") != "ended" and target.get("state") in _WAITING_STATES


def _track_waiting(previous, targets: list[dict], now: float) -> dict:
    """``{session_id: first-seen-waiting ts}`` for targets waiting now, from this duty's own
    earlier observations; a target that resumed work drops out."""
    previous = previous if isinstance(previous, dict) else {}
    result = {}
    for target in targets:
        sid = str(target.get("session_id") or "")
        if sid and _waiting(target):
            try:
                result[sid] = min(float(previous.get(sid, now)), now)
            except (TypeError, ValueError):
                result[sid] = now
    return result


def _label(name: str) -> str:
    name = str(name or "-")
    return name if name.startswith("[") else f"[{name}]"


def next_actions(observation: dict, waiting_since=None, now: float | None = None) -> list[str]:
    """Concrete next steps: queued peer requests, waiting targets paired with open work,
    unassigned open work, then decisions awaiting the user. Empty when nothing remains."""
    now = _now() if now is None else now
    waiting_since = waiting_since or {}
    targets = observation["targets"]
    work = [h if len(h) <= _MAX_HEADLINE else h[:_MAX_HEADLINE - 1] + "…"
            for h in observation.get("open_work") or ()]
    waiting = sorted((t for t in targets if _waiting(t) and not _queued_for(t)),
                     key=lambda t: waiting_since.get(str(t.get("session_id")), now))
    actions = []
    for target in targets:
        owed = _queued_for(target)
        if owed:
            actions.append(f"{_label(target['name'])} {owed} peer request(s) queued, not yet "
                           "delivered → do not resend")
    for target in waiting:
        minutes = int(max(0.0, now - float(waiting_since.get(str(target.get("session_id")), now))) // 60)
        since = f" {minutes} min" if minutes else ""
        if not work:
            break
        actions.append(f"{_label(target['name'])} waiting{since} · {work.pop(0)} → assign now")
    for headline in work:
        actions.append(f"open: {headline} → assign or do now")
    for headline in observation.get("user_decisions") or ():
        short = headline if len(headline) <= _MAX_HEADLINE else headline[:_MAX_HEADLINE - 1] + "…"
        actions.append(f"awaiting user decision: {short} → raise it to the user")
    pending = int(observation.get("pending_messages") or 0)
    if pending:
        actions.insert(0, f"{pending} peer request(s) queued for you → handle them as they arrive")
    return actions


def _signature(action: str, observation: dict) -> str:
    rows = sorted((t["name"], t["harness"], str(t["session_id"]), t["state"], t["liveness"],
                   int(t.get("owed") or 0))
                  for t in observation["targets"])
    raw = repr((action, rows, observation["pending_messages"],
                tuple(observation.get("open_work") or ()),
                tuple(observation.get("user_decisions") or ())))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _quiet(observation: dict) -> bool:
    """No live unfinished target: an unchanged state sends no further model turn. Open
    records alone do not repeat; a live unfinished target is checked every interval.
    Unresolved targets remain visible, but only repeat when positively live."""
    return not any(t["state"] != "done" and t["liveness"] != "ended"
                   for t in observation["targets"]
                   if not t.get("unresolved") or t["liveness"] == "live")


def _classify(observation: dict) -> str:
    """Release or tidy advice only when nothing remains; remaining work names next steps."""
    targets = observation["targets"]
    if _remaining(observation):
        return "assign-next" if next_actions(observation) else "run-steward-check"
    if not targets:
        return "release-if-idle"
    if all(t["state"] == "done" or t["liveness"] == "ended" for t in targets):
        return "tidy-and-release"
    return "run-steward-check"


def _actions_text(actions: list[str]) -> str:
    """The assign-next notice within the carrier's text limit."""
    import session_notice
    limit = getattr(session_notice, "MAX_TEXT", 600)
    shown = list(actions[:_MAX_ACTIONS])
    while True:
        more = len(actions) - len(shown)
        joined = "; ".join(shown) + (f"; +{more} more" if more else "")
        text = TEXT_ASSIGN_NEXT.format(actions=joined)
        if len(text) <= limit or len(shown) <= 1:
            return text[:limit]
        shown.pop()


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
        # A cadence for the native window-closed read, not a time condition on the verdict.
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
    waiting_since = _track_waiting(observation.get("waiting_since"), seen["targets"], now)
    observation = {**observation, "waiting_since": waiting_since}
    action = _classify(seen)
    signature = _signature(action, seen)
    quiet = _quiet(seen)
    if quiet and signature == observation.get("quiet_signature"):
        store.update(duty["id"], observation={
            **observation, "anchor": now, "skipped": int(observation.get("skipped") or 0) + 1,
            "reason": "quiet-no-change"})
        return
    if action == "release-if-idle":
        text = TEXT_RELEASE_IF_IDLE
    elif action == "tidy-and-release":
        text = TEXT_TIDY_AND_RELEASE.format(count=len(seen["targets"]), names=_names(seen["targets"]))
    elif action == "assign-next":
        text = _actions_text(next_actions(seen, waiting_since, now))
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
        module = getattr(_collector(), "_peer_message_module", None)
        peer_message = module() if module else None
        resume = getattr(peer_message, "observe_steward_resume", None)
        st = _store(store)
        duty = st.get(duty_id(harness, session_id))
        marker = _marker_exists(st, harness, session_id)
        if not marker and not _active(duty):
            return
        if marker and _active(duty):
            if resume:
                resume(harness, session_id)
            return  # consistent; the runner re-verifies at each due time
        role = _role_held(harness, session_id)
        if role["held"]:
            if resume:
                resume(harness, session_id)
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
            else:
                module = getattr(_collector(), "_peer_message_module", None)
                peer_message = module() if module else None
                if peer_message:
                    peer_message.release_closed_session(harness, session_id, reason="session-ended")
        except Exception:  # noqa: BLE001
            continue


# ---------------------------------------------------------------- steward check command

def _check_digest(seen: dict) -> str:
    parts = [f"{t['name']}@{t['harness']}={hashlib.sha256(repr((t['state'], t['liveness']) + ((int(t['owed']),) if t.get('owed') else ())).encode()).hexdigest()[:8]}"
             for t in seen["targets"]]
    parts.append("pending=" + str(seen["pending_messages"]))
    if seen.get("open_work"):
        parts.append("open=" + hashlib.sha256(repr(tuple(seen["open_work"])).encode()).hexdigest()[:8])
    if seen.get("user_decisions"):
        parts.append("user=" + hashlib.sha256(repr(tuple(seen["user_decisions"])).encode()).hexdigest()[:8])
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
    waiting_since = _track_waiting(observation.get("waiting_since"), seen["targets"], now)
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
            "waiting_since": waiting_since, "reason": "checked"})
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
    actions = next_actions(seen, waiting_since, now)
    if not targets and not actions:
        print("hint=nothing to supervise; tidy finished sessions and release with "
              "peer-steward steward off", file=out)
        return 0
    for target in targets:
        owed = int(target.get("owed") or 0)
        print(f"target name={target['name']} harness={target['harness']} "
              f"state={target['state']} liveness={target['liveness']}"
              + (f" owed={owed}" if owed else ""), file=out)
    print(f"pending_messages={seen['pending_messages']} undelivered_notices={undelivered}"
          f" open_work={len(seen.get('open_work') or ())}"
          + (f" user_decisions={len(seen['user_decisions'])}" if seen.get("user_decisions") else ""),
          file=out)
    for action in actions:
        print(f"next={action}", file=out)
    if actions:
        print("hint=do the next actions now; release only when none remain", file=out)
    print(f"next_due_in_minutes={-(-interval // 60)}", file=out)
    return 0
