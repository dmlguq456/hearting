#!/usr/bin/env python3
"""Keep a block that is already in a session's context from being injected again.

A hook that re-renders the same notice every turn (a start receipt, the "[답변 필요]"
status line, the fixed peer-message guidance) pays for it on every later request, yet the
model has already read it. This module remembers, per session and per kind, the hashes of
the text last delivered; a block whose text is identical to that is left out. A changed
text, and the first turn after a compaction or a clear (``reset``), is delivered again. The
first delivery is never withheld. A line that judges the current input (the open-question
line a person's prompt triggers) is new every turn and does not go through here.

State is one tiny JSON file per session next to the session-tidy records
(``<state>/hearting/session-tidy/repeat/``). No daemon or poller: files untouched for two
days are removed when a write happens. Every failure means "deliver it" (fail-open).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

PRUNE_AFTER_SECONDS = 2 * 24 * 3600
_PRUNE_LIMIT = 50


def _dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "hearting" / "session-tidy" / "repeat"


def _path(harness: str, sid: str) -> Path:
    key = hashlib.sha256(f"{harness}\0{sid}".encode("utf-8", "replace")).hexdigest()[:20]
    return _dir() / f"{harness}-{key}.json"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    kinds = value.get("kinds") if isinstance(value, dict) else None
    return kinds if isinstance(kinds, dict) else {}


def _write(path: Path, kinds: dict) -> None:
    # Atomic replace without a lock: a lost race only means one block is delivered again.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps({"kinds": kinds}), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    _prune(path.parent)


def _prune(folder: Path) -> None:
    cutoff = time.time() - PRUNE_AFTER_SECONDS
    removed = 0
    try:
        for entry in os.scandir(folder):
            if removed >= _PRUNE_LIMIT:
                break
            try:
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.unlink(entry.path)
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass


def filter_fresh(harness: str, sid: str, kind: str, blocks):
    """``(blocks to inject, commit entry or None)``.

    A block is left out when its text equals one delivered by the previous evaluation of
    this kind. The entry replaces the remembered set with what is evaluated now, so a block
    that disappeared and comes back is delivered again. Pass the entry to :func:`commit`
    once the output actually went out."""
    blocks = [b for b in blocks if isinstance(b, str) and b]
    if not harness or not sid or not isinstance(kind, str):
        return blocks, None
    try:
        path = _path(harness, sid)
        known = set(_read(path).get(kind) or [])
    except (OSError, TypeError, ValueError):
        return blocks, None
    hashes = [_hash(b) for b in blocks]
    fresh = [b for b, h in zip(blocks, hashes) if h not in known]
    entry = {"path": str(path), "kind": kind, "hashes": sorted(set(hashes))}
    if sorted(known) == entry["hashes"]:
        return fresh, None
    return fresh, entry


def commit(entry) -> bool:
    if not isinstance(entry, dict) or not entry.get("path"):
        return False
    try:
        path = Path(str(entry["path"]))
        kinds = _read(path)
        hashes = [str(h) for h in entry.get("hashes") or []]
        if hashes:
            kinds[str(entry["kind"])] = hashes
        else:
            kinds.pop(str(entry["kind"]), None)
        _write(path, kinds)
        return True
    except (OSError, TypeError, ValueError):
        return False


def fresh_once(harness: str, sid: str, kind: str, text: str) -> str:
    """``text`` when it must be injected now, else ``""``; remembers it at once."""
    blocks, entry = filter_fresh(harness, sid, kind, [text])
    if entry is not None:
        commit(entry)
    return blocks[0] if blocks else ""


def _mark_path(sid: str) -> Path:
    return _dir() / f"compact-{hashlib.sha256(sid.encode('utf-8', 'replace')).hexdigest()[:20]}.mark"


def compaction_mark(sid: str) -> str:
    """Token of the session's latest compaction ("" when none is recorded).

    It changes with every compaction (``reset(..., compaction=True)``), whatever the order
    of the harness's compaction hooks, so a repeat guard elsewhere can tell a new
    compaction from one event delivered twice."""
    try:
        return _mark_path(sid).read_text(encoding="utf-8").strip() if sid else ""
    except OSError:
        return ""


def reset(harness: str, sid: str, *, compaction: bool = False) -> None:
    """A compaction or clear may have dropped the earlier text: deliver once more.

    ``compaction=True`` also records that a compaction happened (``compaction_mark``)."""
    if not harness or not sid:
        return
    try:
        _path(harness, sid).unlink(missing_ok=True)
    except OSError:
        pass
    if compaction:
        try:
            path = _mark_path(sid)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            try:
                tmp.write_text(f"{time.time_ns()}.{os.getpid()}", encoding="utf-8")
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
        except OSError:
            pass
