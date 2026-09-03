"""F-100c — steward (depth −1) flag projection, read-only.

The ledger tool (`utilities/peer-message.py`) writes one marker per SENDING session
under `<dispatch-state-root>/peer-steward/<harness>/<sid>.json` whenever a
steer/handoff/gate-relay/watch record is recorded — by the steward wrapper, by the
Claude SendMessage hook, or by hand — so every send path raises the same flag and
`peer-message release` clears it. This collector joins those markers onto live
sessions by exact (harness, session_id); nothing here writes, and a missing or
unreadable marker root leaves every session's default (`steward=False`).
"""
import importlib.util
import sys
from pathlib import Path


def _peer_message_module():
    here = Path(__file__).resolve()
    for candidate in here.parents:
        tool = candidate / "utilities" / "peer-message.py"
        if tool.is_file():
            try:
                spec = importlib.util.spec_from_file_location("_peer_message_ro", str(tool))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
            except Exception:
                return None
    return None


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


def enrich(sessions, markers=None):
    if markers is None:
        markers = read_markers()
    if not markers:
        return
    for s in sessions:
        sid = getattr(s, "session_id", None)
        harness = str(getattr(s, "harness", "") or "").lower()
        marker = markers.get((harness, sid)) if sid else None
        if not marker:
            continue
        targets = marker.get("targets")
        s.steward = True
        s.steward_targets = (sorted(targets.values(), key=lambda t: t.get("ts") or "")
                             if isinstance(targets, dict) else [])
