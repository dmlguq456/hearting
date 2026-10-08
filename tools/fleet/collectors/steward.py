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
"""
def _peer_message_module():
    from . import peer_messages
    return peer_messages._load_peer_message()


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


def _session_keys(sess):
    """The shared join-key rule — a marker written before a resume still names the id the
    session used to have, so an exact-only join silently drops the relation."""
    from .. import session_registry
    return session_registry.session_join_keys(sess)


def role_targets(harness, session_id, *, aliases=(), markers=None):
    """One role lookup for Fleet and herdr, including proven prior session ids."""
    if markers is None:
        markers = read_markers()
    mod = _peer_message_module()
    evidence = getattr(mod, "steward_evidence_targets", None) if mod is not None else None
    if evidence is None:
        return []
    for sid in dict.fromkeys([session_id, *aliases]):
        marker = markers.get((str(harness or "").lower(), sid)) if sid else None
        if not marker:
            continue
        try:
            targets = evidence(marker)
        except Exception:
            targets = []
        if targets:
            return list(targets)
    return []


def enrich(sessions, markers=None):
    if markers is None:
        markers = read_markers()
    if not markers:
        return
    # target key → the stewards that named it. Built from the SAME evidence entries the
    # forward projection uses, so "who watches me" can never claim a relation that the
    # steward's own row does not also show.
    parents_by_key = {}
    for s in sessions:
        keys = _session_keys(s)
        targets = role_targets(s.harness, s.session_id, markers=markers,
                               aliases=[sid for _harness, sid in keys[1:]]
                               + list(getattr(s, "_gpu_session_aliases", ()) or ()))
        if not targets:
            continue
        s.steward = True
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
