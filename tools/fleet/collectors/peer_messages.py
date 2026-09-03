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


def _runtime_ledger_roots():
    """F-100c — each installed runtime resolves ITS OWN dispatch state root
    (`~/.codex/.harness/dispatch`, `~/.config/opencode/.harness/dispatch`, …; measured
    2026-09-03), so a Codex/OpenCode receiver's `notice` lands in that runtime's ledger,
    not in the stable per-user root the Claude side writes to. The board reads all of
    them; existence-gated, so a runtime that was never activated adds nothing."""
    from . import dispatch as _dispatch
    out = []
    for home in (_dispatch._codex_home(), _dispatch._proj_home(), _dispatch._opencode_config_home()):
        if not home:
            continue
        root = os.path.join(home, ".harness", "dispatch")
        if os.path.isdir(os.path.join(root, "peer-messages")) or os.path.isdir(os.path.join(root, "peer-steward")):
            out.append(root)
    return out


def _state_roots():
    """F-98d — read through the SAME resolver chain the writer (`peer-message.py`) uses,
    not `dispatch._row_state_roots()`'s no-row default (which pins to the release tree's
    `.dispatch` and ignores an inherited `AGENT_DISPATCH_JOBS`). Modelled on
    `tools/fleet/route.py`'s `_dispatch_state_roots` — lazy import, tolerant of every
    failure (never raises out of a read-only collector). F-100c appends every installed
    runtime's own dispatch root (`_runtime_ledger_roots`)."""
    roots = []
    try:
        home = _agent_home()
    except Exception:
        home = None
    if home is not None:
        here = Path(__file__).resolve()
        for candidate in here.parents:
            utilities_dir = candidate / "utilities"
            if (utilities_dir / "dispatch_contract.py").is_file():
                if str(utilities_dir) not in sys.path:
                    sys.path.insert(0, str(utilities_dir))
                try:
                    from dispatch_contract import dispatch_state_roots
                    roots = [str(root) for root in dispatch_state_roots(
                        Path(home), jobs=os.environ.get("AGENT_DISPATCH_JOBS"))]
                except Exception:
                    roots = []
                break
    try:
        for extra in _runtime_ledger_roots():
            if extra not in roots:
                roots.append(extra)
    except Exception:
        pass
    return tuple(roots)


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

    ordered = sorted(records, key=lambda pair: pair[0])
    by_session = {}
    pending = {}

    def _key(block):
        sid = block.get("session_id")
        return (str(block.get("harness") or "").lower(), sid) if sid else None

    def _row(key):
        return by_session.setdefault(key, {"sent_1h": 0, "recv_1h": 0, "last_recv": None})

    def _record_recv(to_key, kind, frm, to, from_key, age_min):
        row = _row(to_key)
        if age_min <= 60:
            row["recv_1h"] += 1
        from_sid = from_key[1] if from_key else None
        from_name = (frm.get("name") or from_sid or "")
        row["last_recv"] = {"from_name": from_name, "from_session_id": from_sid,
                             "from_harness": from_key[0] if from_key else "",
                             "kind": kind, "age_min": age_min}

    def _upgrade_recv(to_key, inherited, frm, to, from_key, age_min):
        # A correlated notice is, by construction, the newest successful receipt for
        # `to_key` (it is processed in ts order and only reached once its matching
        # sent record popped off `pending`) — so it replaces `last_recv` unconditionally,
        # even when a later record from a different sender had already overwritten it.
        # `recv_1h` is untouched here: the notice and its correlated sent record are one
        # logical message and must count once (F-101i).
        row = _row(to_key)
        from_sid = from_key[1] if from_key else None
        from_name = (frm.get("name") or from_sid or "")
        row["last_recv"] = {"from_name": from_name, "from_session_id": from_sid,
                             "from_harness": from_key[0] if from_key else "",
                             "kind": inherited, "age_min": age_min}

    # Correlation is bounded by the existing tail/window/max limits. A clipped sender
    # leaves a notice as `notice` (honest miss); LIFO reduces but cannot eliminate an
    # ambiguity when the same exact pair sends several kinds in quick succession.
    for ts, rec in ordered:
        frm, to = rec.get("from") or {}, rec.get("to") or {}
        from_key, to_key = _key(frm), _key(to)
        age_min = max(0, int((now - ts) // 60))
        kind = rec.get("kind")
        status = ((rec.get("delivery") or {}).get("status") or "").lower()
        deliverable = status != "failed"
        if from_key:
            row = _row(from_key)
            if age_min <= 60:
                row["sent_1h"] += 1
        if kind != "notice":
            if from_key and to_key and deliverable:
                pending.setdefault((from_key, to_key), []).append(kind)
            if to_key and deliverable:
                _record_recv(to_key, kind, frm, to, from_key, age_min)
        else:
            stack = pending.get((from_key, to_key)) if from_key and to_key else None
            if stack:
                inherited = stack.pop()
                if to_key and deliverable:
                    _upgrade_recv(to_key, inherited, frm, to, from_key, age_min)
            elif to_key and deliverable:
                _record_recv(to_key, "notice", frm, to, from_key, age_min)

    collect.last_diagnostics = []
    collect.last_malformed = malformed
    return {"records": [rec for _ts, rec in records], "by_session": by_session}


collect.last_diagnostics = []
collect.last_malformed = 0
