"""Fail-soft, read-only collector for the SD-122 peer-message ledger.

Never writes. Bounded per file (tail 64KB) and overall (24h window, 200 records).
A missing or unreadable ledger returns an empty result — every Session field this
feeds stays at its snapshot default so the rendered board is byte-identical to a
pre-SD-122 board.
"""
import glob
import json
import os
import sys
import time
from pathlib import Path

_TAIL_BYTES = 64 * 1024
_WINDOW_SECONDS = 24 * 3600
_MAX_RECORDS = 200


def _agent_home():
    from . import dispatch
    return dispatch._registry_home()


def _state_roots():
    """F-98d — read through the SAME resolver chain the writer (`peer-message.py`) uses,
    not `dispatch._row_state_roots()`'s no-row default (which pins to the release tree's
    `.dispatch` and ignores an inherited `AGENT_DISPATCH_JOBS`). Modelled on
    `tools/fleet/route.py`'s `_dispatch_state_roots` — lazy import, tolerant of every
    failure (never raises out of a read-only collector)."""
    try:
        home = _agent_home()
    except Exception:
        return ()
    here = Path(__file__).resolve()
    for candidate in here.parents:
        utilities_dir = candidate / "utilities"
        if (utilities_dir / "dispatch_contract.py").is_file():
            if str(utilities_dir) not in sys.path:
                sys.path.insert(0, str(utilities_dir))
            try:
                from dispatch_contract import dispatch_state_roots

                return tuple(
                    str(root) for root in dispatch_state_roots(
                        Path(home), jobs=os.environ.get("AGENT_DISPATCH_JOBS"))
                )
            except Exception:
                return ()
    return ()


def _tail_lines(path):
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()  # discard the first, possibly-incomplete line
            return fh.read().splitlines()
    except OSError:
        return []


def _parse_ts(ts):
    try:
        raw = ts.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        from datetime import datetime
        return datetime.fromisoformat(raw).timestamp()
    except Exception:
        return None


def collect(state_roots=None):
    records = []
    malformed = 0
    now = time.time()
    roots = state_roots if state_roots is not None else _state_roots()
    seen_files = set()
    try:
        for root in roots:
            pattern = os.path.join(str(root), "peer-messages", "*", "*.jsonl")
            for path in glob.glob(pattern):
                real = os.path.realpath(path)
                if real in seen_files:
                    continue
                seen_files.add(real)
                for line in _tail_lines(path):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        malformed += 1
                        continue
                    ts = _parse_ts(rec.get("ts", ""))
                    if ts is None or (now - ts) > _WINDOW_SECONDS:
                        continue
                    records.append((ts, rec))
    except Exception:
        collect.last_diagnostics = []
        collect.last_malformed = malformed
        return {"records": [], "by_session": {}}

    records.sort(key=lambda pair: pair[0], reverse=True)
    records = records[:_MAX_RECORDS]

    by_session = {}
    for ts, rec in records:
        frm = rec.get("from") or {}
        to = rec.get("to") or {}
        from_sid = frm.get("session_id")
        to_sid = to.get("session_id")
        age_min = max(0, int((now - ts) // 60))
        if from_sid:
            row = by_session.setdefault(from_sid, {"sent_1h": 0, "recv_1h": 0, "last_recv": None})
            if age_min <= 60:
                row["sent_1h"] += 1
        if to_sid:
            row = by_session.setdefault(to_sid, {"sent_1h": 0, "recv_1h": 0, "last_recv": None})
            if age_min <= 60:
                row["recv_1h"] += 1
            if row["last_recv"] is None:
                # `to.name` is the recipient's own name on an ordinary sent record —
                # using it as the sender would let the recipient's subtitle claim
                # the message came from itself. The one exception is a `notice`
                # record: it is self-authored by the receiving session on receipt,
                # and there `to.name` is repurposed to carry the extracted sender
                # name (F-98b §10-5), the only name-shaped hint available for it.
                if rec.get("kind") == "notice":
                    from_name = frm.get("name") or to.get("name") or from_sid or ""
                else:
                    from_name = frm.get("name") or from_sid or ""
                row["last_recv"] = {
                    "from_name": from_name,
                    "from_session_id": from_sid,
                    "kind": rec.get("kind"),
                    "age_min": age_min,
                }

    collect.last_diagnostics = []
    collect.last_malformed = malformed
    return {"records": [rec for _ts, rec in records], "by_session": by_session}


collect.last_diagnostics = []
collect.last_malformed = 0
