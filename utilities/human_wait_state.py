"""Private, observational state for explicitly marked final-reply questions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import fcntl
import math
import uuid
from contextlib import contextmanager
from datetime import datetime
import ipaddress
import socket
from decimal import Decimal, InvalidOperation
import urllib.error
import urllib.parse
import urllib.request


_MARKER = re.compile(r"^\s*\[(?:답변 필요|결정 필요)\]\s*(.*)$")
_MAX_QUESTION = 160
_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:@/+\-=]{1,256}$")
_SAFE_ROUTE_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_CACHE_MAX_BYTES = 2 * 1024 * 1024
_DISPLAY_TTL = 180.0
_REQUEST_TTL = 60.0
_SELECTED_DRAFT_MAX = 4096
_SELECTED_RESPONSE_MAX = 256
_SELECTED_STATES = frozenset(("open", "submitted", "received", "applied",
                              "cancelled", "expired", "unknown"))
_SELECTED_ENTRY_KEYS = frozenset(("identity", "selected_id", "draft", "state", "action_id",
                                  "response", "result", "updated_at"))


def state_root() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(xdg) / "hearting" / "human-waits"


def _path(seat_key: str) -> Path:
    digest = hashlib.sha256(str(seat_key).encode("utf-8")).hexdigest()
    return state_root() / (digest + ".json")


def _cache_path() -> Path:
    return state_root() / "projection.json"


def _fleet_cache_path() -> Path:
    return state_root() / "fleet.json"


@contextmanager
def _cache_write_lock():
    root = state_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    meta = os.lstat(root)
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid():
        raise OSError("answer-wait directory is not private owner state")
    os.chmod(root, 0o700)
    # The producer holds .projection.lock for its whole lifetime. Cache writes
    # and selected-action claims need a separate short transaction lock.
    fd = os.open(root / ".cache-write.lock", os.O_CREAT | os.O_RDWR |
                 getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_cache_file():
    path = _cache_path()
    try:
        parent = os.lstat(path.parent)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) & 0o077):
            return None
        meta = os.lstat(path)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or stat.S_IMODE(meta.st_mode) & 0o077 or meta.st_size > _CACHE_MAX_BYTES):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) and value.get("schema_version") == 3 else None


def _write_cache_value(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _CACHE_MAX_BYTES:
            return False
        _write(_cache_path(), value)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _clean(value: str) -> str:
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    value = " ".join("".join(ch for ch in value if ch >= " " and ch != "\x7f").split())
    return value[:_MAX_QUESTION]


def _safe_native_ref(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value)
    return value if _SAFE.fullmatch(value) else None


def _safe_request_id(value):
    """Validate a JSON-RPC request id without collapsing int and string ids."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _SAFE_REQUEST_ID.fullmatch(value):
        return value
    return None


def _same_request_id(left, right):
    return (type(left) is type(right) and _safe_request_id(left) == left
            and _safe_request_id(right) == right and left == right)


def marked_question(text: str) -> str | None:
    """Return a short marked line outside fenced code, never infer from tone."""
    if not isinstance(text, str):
        return None
    fence = None
    lines = text.splitlines()
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is not None:
            continue
        match = _MARKER.match(line)
        if match:
            return _clean(match.group(1)) or _clean(line.strip())
    return None


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    meta = os.lstat(path.parent)
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid():
        raise OSError("answer-wait directory is not private owner state")
    os.chmod(path.parent, 0o700)
    fd, name = tempfile.mkstemp(prefix=".wait-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def signal_value(harness: str, session_id: str, seat_key: str, text: str, *,
                 sequence: int = 0, now: float | None = None) -> dict | None:
    """Build a validated private signal without writing or notifying."""
    question = marked_question(text)
    if (not question or harness not in {"claude", "codex", "opencode"}
            or not _SAFE.fullmatch(str(session_id)) or not seat_key):
        return None
    moment = time.time() if now is None else float(now)
    generation = max(0, int(sequence))
    item_id = hashlib.sha256((harness + "\0" + session_id + "\0" + seat_key +
                              "\0" + question + "\0" + str(generation)).encode()).hexdigest()
    prior = _read(_path(seat_key))
    if prior and prior.get("id") == "S4:" + item_id and prior.get("question") == question:
        moment = float(prior.get("since", moment))
    value = {"schema_version": 1, "id": "S4:" + item_id, "source": "S4",
             "harness": harness, "session_id": session_id, "seat_key": str(seat_key),
             "question": question, "since": moment, "sequence": generation}
    return value


def write_signal(value: dict) -> bool:
    """Write a previously validated signal without running notification work."""
    if not isinstance(value, dict) or not value.get("seat_key") or not value.get("id"):
        return False
    try:
        _write(_path(str(value["seat_key"])), value)
        return True
    except (OSError, ValueError, TypeError):
        return False


def remove_signal_if_current(value: dict) -> bool:
    """Remove this exact observer's signal, preserving any replacement."""
    if not isinstance(value, dict) or not value.get("seat_key") or not value.get("id"):
        return False
    path = _path(str(value["seat_key"]))
    try:
        current = _read(path)
        if current is None or current.get("id") != value.get("id"):
            return False
        path.unlink()
        return True
    except OSError:
        return False


def signal_is_current(value: dict) -> bool:
    """Return whether this exact S4 item is still open for its seat."""
    if not isinstance(value, dict) or not value.get("seat_key") or not value.get("id"):
        return False
    current = _read(_path(str(value["seat_key"])))
    return current is not None and current.get("id") == value.get("id")


def notify_signal(value: dict) -> None:
    """Run registration side effects after the caller has released seat locks."""
    _notify_item(value, boundary="registered")
    _request_producer()


def open_signal(harness: str, session_id: str, seat_key: str, text: str, *,
                sequence: int = 0, now: float | None = None) -> bool:
    """Unconditional convenience writer for callers without a sequence fence."""
    value = signal_value(harness, session_id, seat_key, text, sequence=sequence, now=now)
    if value is None or not write_signal(value):
        return False
    notify_signal(value)
    return True


def close_signal(seat_key: str, *, sequence: int | None = None) -> bool:
    """Close only this exact seat, and never let an older input clear a newer wait."""
    path = _path(seat_key)
    try:
        current = _read(path)
        if current is None:
            return True
        if sequence is not None and int(current.get("sequence", 0)) > int(sequence):
            return False
        path.unlink()
        return True
    except (OSError, ValueError, TypeError):
        return False


def _read(path: Path):
    try:
        meta = os.lstat(path)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or stat.S_IMODE(meta.st_mode) & 0o077):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("source") != "S4" or not isinstance(value.get("question"), str)
                or not value.get("seat_key") or not value.get("id")):
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


def read_signals(*, now: float | None = None) -> list[dict]:
    """Read valid private signals. This function never creates or changes state."""
    root = state_root()
    try:
        entries = tuple(root.iterdir())
    except OSError:
        return []
    instant = time.time() if now is None else float(now)
    out = []
    for path in entries:
        if path.suffix != ".json" or path.is_symlink():
            continue
        value = _read(path)
        if value is None or not isinstance(value.get("since"), (int, float)):
            continue
        value["stale"] = instant - float(value["since"]) >= 7 * 86400
        out.append(value)
    return sorted(out, key=lambda item: (item.get("since", 0), item["id"]))


def publish_cache(result: dict) -> bool:
    """Publish private and question-free Fleet views atomically per projection."""
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        return False
    by_session: dict[str, list[str]] = {}
    items = []
    for item in result["items"]:
        if not isinstance(item, dict):
            continue
        owner = item.get("owner") or {}
        harness, sid = owner.get("harness"), owner.get("session_id")
        row = {key: item.get(key) for key in ("id", "source", "kind", "since", "route_id",
               "attempt_id", "gate_id", "attribution", "stale")}
        route_hash = item.get("route_hash")
        if isinstance(route_hash, str) and _SAFE_ROUTE_HASH.fullmatch(route_hash):
            row["route_hash"] = route_hash
        row["owner"] = {"harness": harness, "session_id": sid}
        if item.get("source") == "S1":
            for key in ("request_id", "turn_id", "item_id"):
                ref = (_safe_request_id(item.get(key)) if key == "request_id"
                       else _safe_native_ref(item.get(key)))
                if ref is not None:
                    row[key] = ref
        items.append(row)
        if harness and sid and not item.get("stale"):
            key = str(harness) + "\0" + str(sid)
            by_session.setdefault(key, []).append(str(item.get("id") or ""))
    stale_total = sum(bool(item.get("stale")) for item in items)
    current_total = sum(not bool(item.get("stale")) for item in items)
    observed_at = float(result.get("observed_at") or time.time())
    value = {"schema_version": 3, "observed_at": observed_at,
             "complete": bool(result.get("complete")), "total": current_total,
             "older": stale_total, "stale_total": stale_total, "items": items, "by_session": by_session,
             "unattributed": [item.get("id") for item in items
                              if item.get("attribution") == "unattributed" and not item.get("stale")],
             "health": [{"source": row.get("source"), "state": row.get("state")}
                        for row in result.get("health", ()) if isinstance(row, dict)]}
    try:
        fleet_items = []
        for item in items:
            source = item.get("source")
            kind = item.get("kind")
            if source == "S1":
                source = "S1-approval" if kind in {"approval", "permission"} else "S1-question"
                kind = "approval" if source == "S1-approval" else "decision"
            elif source == "S2":
                kind = "human gate"
            elif source == "S3":
                kind = "blocked owner"
            elif source == "S4":
                kind = "marker"
            owner = item.get("owner") or {}
            fleet_item = {
                "id": item.get("id"), "source": source, "kind": kind,
                "session_id": owner.get("session_id"), "harness": owner.get("harness"),
                "pane_id": None, "route_id": item.get("route_id"),
                "node": item.get("gate_id"), "attempt_id": item.get("attempt_id"),
                "since": item.get("since"),
                "stale": "older" if item.get("stale") else "current",
            }
            if item.get("source") == "S1":
                for key in ("request_id", "turn_id", "item_id"):
                    ref = (_safe_request_id(item.get(key)) if key == "request_id"
                           else _safe_native_ref(item.get(key)))
                    if ref is not None:
                        fleet_item[key] = ref
            fleet_items.append(fleet_item)
        fleet_value = {"schema_version": 1, "observed_at": observed_at,
                       "complete": bool(result.get("complete")), "total": current_total,
                       "older": stale_total, "items": fleet_items,
                       "unattributed": [item["id"] for item in fleet_items
                                        if not item["session_id"] or not item["harness"]],
                       "health": [{"source": row.get("source"), "state": row.get("state")}
                                  for row in result.get("health", ()) if isinstance(row, dict)]}
        with _cache_write_lock():
            previous = _read_cache_file()
            selected = previous.get("selected_responses", {}) if previous else {}
            if not _valid_selected_overlay(selected):
                return False
            if selected:
                value["selected_responses"] = selected
                # A producer may briefly see an old native sidecar after its
                # exact selected request was answered. Suppress only that
                # observation, including its original timestamp-derived id;
                # a replacement request/incarnation keeps its own current row.
                settled = {entry.get("selected_id"): entry["identity"]
                           for entry in selected.values()
                           if entry.get("selected_id")
                           and entry.get("state") in {"applied", "cancelled", "expired"}}
                def answered(row):
                    identity = settled.get(row["id"])
                    owner = row["owner"]
                    return (row["source"] == "S1" and identity is not None
                            and identity["harness"] == owner["harness"]
                            and identity["session_id"] == owner["session_id"]
                            and _same_request_id(identity["request_id"], row.get("request_id"))
                            and all(identity.get(key) == row.get(key)
                                    for key in ("turn_id", "item_id")))
                removed = {row["id"] for row in items if answered(row)}
                if removed:
                    value["items"] = [row for row in items if row["id"] not in removed]
                    value["by_session"] = {key: [ref for ref in refs if ref not in removed]
                                           for key, refs in by_session.items()
                                           if any(ref not in removed for ref in refs)}
                    fleet_value["items"] = [row for row in fleet_value["items"]
                                            if row["id"] not in removed]
                    current_total = sum(not row.get("stale") for row in value["items"])
                    older_total = len(value["items"]) - current_total
                    for projection in (value, fleet_value):
                        projection["total"] = current_total
                        projection["older"] = older_total
                        projection["unattributed"] = [ref for ref in projection["unattributed"]
                                                       if ref not in removed]
                    value["stale_total"] = value["older"]
            if not _write_cache_value(value):
                return False
            _write(_fleet_cache_path(), fleet_value)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _read_cache(*, now=None, max_age=_DISPLAY_TTL):
    value = _read_cache_file()
    current = time.time() if now is None else float(now)
    if (not isinstance(value, dict) or value.get("schema_version") != 3
            or not isinstance(value.get("observed_at"), (int, float))
            or isinstance(value.get("observed_at"), bool)
            or not math.isfinite(float(value["observed_at"]))
            or not isinstance(value.get("items"), list)
            or not isinstance(value.get("by_session"), dict)
            or not isinstance(value.get("total"), int) or isinstance(value.get("total"), bool)
            or not isinstance(value.get("older"), int) or isinstance(value.get("older"), bool)
            or current - float(value["observed_at"]) < 0
            or current - float(value["observed_at"]) > max_age):
        return None
    for item in value["items"]:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not isinstance(item.get("source"), str)
                or not isinstance(item.get("since"), (int, float))
                or isinstance(item.get("since"), bool) or not math.isfinite(float(item["since"]))
                or not isinstance(item.get("owner"), dict)):
            return None
        if ("route_hash" in item and
                (not isinstance(item["route_hash"], str)
                 or not _SAFE_ROUTE_HASH.fullmatch(item["route_hash"]))):
            return None
        if (("request_id" in item and _safe_request_id(item["request_id"]) != item["request_id"])
                or any(_safe_native_ref(item[key]) != item[key]
                       for key in ("turn_id", "item_id") if key in item)):
            return None
    for session, ids in value["by_session"].items():
        if not isinstance(session, str) or "\0" not in session or not isinstance(ids, list) \
                or any(not isinstance(item_id, str) for item_id in ids):
            return None
    return value


def cached_fleet_view(*, now=None, request=True):
    """Return the question-free Fleet projection and request its shared producer."""
    path = _fleet_cache_path()
    try:
        parent = os.lstat(path.parent)
        meta = os.lstat(path)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) & 0o077
                or not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or stat.S_IMODE(meta.st_mode) & 0o077 or meta.st_size > _CACHE_MAX_BYTES):
            raise OSError("invalid Fleet cache permissions or type")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        value = None
    current = time.time() if now is None else float(now)
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or not isinstance(value.get("observed_at"), (int, float))
            or isinstance(value.get("observed_at"), bool)
            or not math.isfinite(float(value["observed_at"]))
            or type(value.get("complete")) is not bool
            or type(value.get("total")) is not int or value["total"] < 0
            or type(value.get("older")) is not int or value["older"] < 0
            or not isinstance(value.get("items"), list)
            or any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
                   or row.get("stale") not in ("current", "older")
                   for row in value["items"])
            or current - float(value["observed_at"]) < 0
            or current - float(value["observed_at"]) > _DISPLAY_TTL):
        value = None
    if request and (value is None or current - float(value["observed_at"]) > _REQUEST_TTL):
        _request_producer()
    return value


def cached_view(*, now=None, request=True):
    value = _read_cache(now=now)
    if request:
        observed = value.get("observed_at") if value else None
        if observed is None or (time.time() if now is None else float(now)) - float(observed) > _REQUEST_TTL:
            _request_producer()
    if value is None:
        return None
    indexed = {tuple(key.split("\0", 1)): ids for key, ids in value.get("by_session", {}).items()
               if isinstance(key, str) and "\0" in key and isinstance(ids, list)}
    return {"items": value["items"], "by_session": indexed, "total": value.get("total", 0),
            "stale_total": value.get("stale_total", 0), "unattributed": value.get("unattributed", []),
            "health": value.get("health", []), "complete": bool(value.get("complete")),
            "observed_at": value["observed_at"]}


def _selected_identity(envelope):
    if not isinstance(envelope, dict):
        return None
    harness, session_id = envelope.get("harness"), envelope.get("session_id")
    request_id = _safe_request_id(envelope.get("request_id"))
    raw_turn_id = envelope.get("turn_id")
    turn_id = _safe_native_ref(raw_turn_id) if raw_turn_id is not None else None
    if (harness not in {"claude", "codex", "opencode"}
            or _safe_native_ref(session_id) != session_id or request_id is None
            or (raw_turn_id is not None and turn_id is None)):
        return None
    identity = {"harness": harness, "session_id": session_id,
                "request_id": request_id, "turn_id": turn_id}
    native = envelope.get("native_ref")
    native_request = native.get("request") if isinstance(native, dict) else None
    item_id = envelope.get("item_id")
    if item_id is None and isinstance(native_request, dict):
        item_id = native_request.get("item_id")
    if item_id is not None:
        item_id = _safe_native_ref(item_id)
        if item_id is None:
            return None
        identity["item_id"] = item_id
    for key in ("route_id", "question_id"):
        value = envelope.get(key)
        if value is not None:
            safe = _safe_native_ref(value)
            if safe is None:
                return None
            identity[key] = safe
    if (envelope.get("source") == "S2" or "gate_epoch" in envelope
            or isinstance(native, dict)
            and native.get("transport") == "hearting-human-gate"):
        gate_epoch = envelope.get("gate_epoch")
        if type(gate_epoch) is not int or gate_epoch < 1:
            return None
        identity["gate_epoch"] = gate_epoch
        artifact_sha256 = (native.get("artifact_sha256") if isinstance(native, dict)
                           else envelope.get("artifact_sha256"))
        if artifact_sha256 is not None:
            if not isinstance(artifact_sha256, str) or not _SAFE_ROUTE_HASH.fullmatch(artifact_sha256):
                return None
            identity["artifact_sha256"] = artifact_sha256
    return identity


def _selected_entry_key(identity):
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _valid_selected_overlay(value):
    if not isinstance(value, dict) or len(value) > _SELECTED_RESPONSE_MAX:
        return False
    for key, entry in value.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key)
                or not isinstance(entry, dict) or not set(entry).issubset(_SELECTED_ENTRY_KEYS)
                or not {"identity", "state", "updated_at"}.issubset(entry)
                or entry.get("state") not in _SELECTED_STATES
                or not isinstance(entry.get("updated_at"), (int, float))
                or isinstance(entry.get("updated_at"), bool)
                or not math.isfinite(float(entry["updated_at"]))):
            return False
        identity = _selected_identity(entry.get("identity"))
        if identity != entry.get("identity") or _selected_entry_key(identity) != key:
            return False
        draft = entry.get("draft", "")
        if not isinstance(draft, str) or len(draft) > _SELECTED_DRAFT_MAX:
            return False
        if entry.get("action_id") is not None and not _SAFE.fullmatch(str(entry["action_id"])):
            return False
        if (entry.get("selected_id") is not None
                and (not isinstance(entry["selected_id"], str) or len(entry["selected_id"]) > 512)):
            return False
        response = entry.get("response")
        if response is not None:
            if not isinstance(response, dict):
                return False
            try:
                if len(json.dumps(response, ensure_ascii=False).encode("utf-8")) > 16_384:
                    return False
            except (TypeError, ValueError):
                return False
        if entry.get("result") is not None and not isinstance(entry["result"], dict):
            return False
    return True


def _selected_entry(envelope):
    identity = _selected_identity(envelope)
    if identity is None:
        return None
    projection = _read_cache_file()
    if projection is None:
        return None
    overlay = projection.get("selected_responses", {})
    if not _valid_selected_overlay(overlay):
        return None
    entry = overlay.get(_selected_entry_key(identity))
    if not entry or entry.get("identity") != identity:
        return None
    return dict(entry)


def _store_selected_entry(envelope, entry):
    identity = _selected_identity(envelope)
    if identity is None:
        return False
    key = _selected_entry_key(identity)
    entry = dict(entry)
    entry["identity"] = identity
    if isinstance(envelope.get("selected_id"), str):
        entry["selected_id"] = envelope["selected_id"]
    entry["updated_at"] = time.time()
    if not _valid_selected_overlay({key: entry}):
        return False
    try:
        with _cache_write_lock():
            projection = _read_cache_file()
            if projection is None:
                return False
            overlay = projection.get("selected_responses", {})
            if not _valid_selected_overlay(overlay):
                return False
            previous = overlay.get(key)
            if previous and previous.get("identity") != identity:
                return False
            if previous is None and len(overlay) >= _SELECTED_RESPONSE_MAX:
                return False
            if previous:
                previous_action = previous.get("action_id")
                next_action = entry.get("action_id")
                if previous_action and next_action != previous_action:
                    return False
                if previous_action:
                    if entry.get("response") not in (None, previous.get("response")):
                        return False
                    entry["action_id"] = previous_action
                    entry["response"] = previous.get("response")
                    entry["draft"] = previous.get("draft", "")
                    rank = {"open": 0, "submitted": 1, "unknown": 2,
                            "received": 3, "applied": 4, "cancelled": 4, "expired": 4}
                    old_state = previous.get("state", "unknown")
                    new_state = entry.get("state", "unknown")
                    if (rank.get(new_state, -1) < rank.get(old_state, -1)
                            or (rank.get(new_state) == rank.get(old_state) == 4
                                and new_state != old_state)):
                        entry["state"] = old_state
                        entry["result"] = previous.get("result")
                    if entry.get("state") == "applied":
                        entry["draft"] = ""
                elif previous.get("state") in {"received", "applied", "cancelled", "expired"}:
                    old_state = previous["state"]
                    new_state = entry.get("state", "unknown")
                    if (new_state not in {"received", "applied", "cancelled", "expired"}
                            or (new_state != old_state and old_state in {"applied", "cancelled", "expired"})):
                        entry["state"] = old_state
                        entry["result"] = previous.get("result")
                        entry["draft"] = previous.get("draft", "")
                    elif entry.get("state") == "applied":
                        entry["draft"] = ""
            overlay[key] = entry
            projection["selected_responses"] = overlay
            return _write_cache_value(projection)
    except (OSError, ValueError, TypeError):
        return False


def _claim_selected_action(envelope, action_id, response):
    """Atomically persist the one allowed action before its native transport call."""
    identity = _selected_identity(envelope)
    if identity is None:
        return False, None
    key = _selected_entry_key(identity)
    try:
        with _cache_write_lock():
            projection = _read_cache_file()
            if projection is None:
                return False, None
            overlay = projection.get("selected_responses", {})
            if not _valid_selected_overlay(overlay):
                return False, None
            previous = overlay.get(key)
            if previous and previous.get("identity") != identity:
                return False, None
            if previous and (previous.get("action_id")
                             or previous.get("state") in {
                                 "submitted", "received", "applied", "cancelled", "expired"}):
                return False, dict(previous)
            if previous is None and len(overlay) >= _SELECTED_RESPONSE_MAX:
                return False, None
            claimed = dict(previous or {})
            claimed.update({"identity": identity, "state": "submitted",
                            "action_id": action_id, "response": response,
                            "result": {"status": "submitted"},
                            "updated_at": time.time()})
            if isinstance(envelope.get("selected_id"), str):
                claimed["selected_id"] = envelope["selected_id"]
            claimed.setdefault("draft", envelope.get("draft", ""))
            if not _valid_selected_overlay({key: claimed}):
                return False, None
            overlay[key] = claimed
            projection["selected_responses"] = overlay
            if not _write_cache_value(projection):
                return False, None
            return True, dict(claimed)
    except (OSError, ValueError, TypeError):
        return False, None


def _proc_start_ticks(pid):
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii", errors="replace")
        fields = raw[raw.rindex(") ") + 2:].split()
        return fields[19] if fields[19].isdigit() else None
    except (OSError, ValueError, IndexError):
        return None


def _proc_uid(pid):
    try:
        for line in (Path("/proc") / str(pid) / "status").read_text().splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _proc_argv(pid):
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
        if len(raw) > 32768:
            return []
        return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    except OSError:
        return []


def _explicit_opencode_server(argv):
    found = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--server":
            if index + 1 >= len(argv) or found is not None:
                return None
            found = argv[index + 1]
            index += 2
            continue
        if arg.startswith("--server="):
            if found is not None:
                return None
            found = arg.split("=", 1)[1]
        index += 1
    if not found:
        return None
    try:
        parsed = urllib.parse.urlsplit(found)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            return None
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return None
        if parsed.scheme == "http":
            try:
                local = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                local = parsed.hostname.lower() == "localhost"
            if not local:
                return None
        host = parsed.hostname.lower()
        if ":" in host:
            host = "[" + host + "]"
        authority = host + ((":" + str(port)) if port else "")
        return parsed.scheme + "://" + authority
    except ValueError:
        return None


def _proc_listening_sockets(pid):
    """Return the exact loopback listener (port, socket inodes) owned by this PID."""
    fd_root = Path("/proc") / str(pid) / "fd"
    try:
        inodes = set()
        for fd in fd_root.iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            match = re.fullmatch(r"socket:\[(\d+)\]", target)
            if match:
                inodes.add(match.group(1))
    except OSError:
        return None
    listeners = {}
    for table, ipv6 in ((Path("/proc/net/tcp"), False), (Path("/proc/net/tcp6"), True)):
        try:
            lines = table.read_text(encoding="ascii", errors="replace").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A" or fields[9] not in inodes:
                continue
            try:
                address_hex, port_hex = fields[1].split(":", 1)
                raw = bytes.fromhex(address_hex)
                if ipv6:
                    raw = b"".join(raw[pos:pos + 4][::-1] for pos in range(0, 16, 4))
                else:
                    raw = raw[::-1]
                address = ipaddress.ip_address(raw)
                port = int(port_hex, 16)
            except (ValueError, OverflowError):
                continue
            if not address.is_loopback or not port:
                continue
            target = listeners.setdefault(port, {"hosts": set(), "inodes": set()})
            target["hosts"].add("127.0.0.1" if address.version == 4 else "::1")
            target["inodes"].add(fields[9])
    if len(listeners) != 1:
        return None
    port, listener = next(iter(listeners.items()))
    host = "127.0.0.1" if "127.0.0.1" in listener["hosts"] else "::1"
    return port, host, tuple(sorted(listener["inodes"]))


def _opencode_loopback_url(value):
    """Parse an HTTP loopback URL without resolving names or following proxies."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme != "http" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            return None
        hostname = parsed.hostname.lower()
        if hostname == "localhost":
            hosts = {"127.0.0.1", "::1"}
        else:
            if "%" in hostname:
                return None
            address = ipaddress.ip_address(hostname)
            if not address.is_loopback:
                return None
            hosts = {str(address)}
        port = parsed.port or 80
        if not 1 <= port <= 65535:
            return None
        return hosts, port
    except ValueError:
        return None


def _opencode_info_matches_listener(url, advertised_urls, listener_port, listener_host):
    requested = _opencode_loopback_url(url)
    if requested is None:
        return []
    requested_hosts, requested_port = requested
    if requested_port != listener_port or listener_host not in requested_hosts:
        return []
    matching = []
    for advertised in advertised_urls:
        parsed = _opencode_loopback_url(advertised)
        if parsed is None:
            continue
        hosts, port = parsed
        if (port == listener_port and listener_host in hosts
                and requested_hosts.intersection(hosts)):
            matching.append(advertised)
    return sorted(set(matching))


def _opencode_server_endpoint(base_url, session_id, frontend_pid, frontend_start,
                              opencode_collector):
    """Bind a selected TUI to the V2 server PID and its physical loopback socket."""
    if _opencode_loopback_url(base_url) is None:
        return None
    info_result = _opencode_http({"url": base_url}, "GET", "/api/info")
    info = info_result.get("body") if isinstance(info_result, dict) else None
    if (not isinstance(info_result, dict) or info_result.get("status") != 200
            or not isinstance(info, dict)
            or not isinstance(info.get("version"), str) or not info["version"]
            or not isinstance(info.get("pid"), int) or isinstance(info.get("pid"), bool)
            or info["pid"] <= 0
            or not isinstance(info.get("urls"), list) or not info["urls"]
            or any(not isinstance(value, str) for value in info["urls"])
            or not isinstance(info.get("paths"), dict)
            or not isinstance(info["paths"].get("tmp"), str)):
        return None

    server_pid = info["pid"]
    if _proc_uid(server_pid) != os.getuid():
        return None
    server_start = _proc_start_ticks(server_pid)
    if not server_start or not str(server_start).isdigit():
        return None
    listener = _proc_listening_sockets(server_pid)
    if (not isinstance(listener, tuple) or len(listener) != 3
            or not isinstance(listener[0], int) or isinstance(listener[0], bool)
            or listener[1] not in {"127.0.0.1", "::1"}
            or not isinstance(listener[2], (tuple, list)) or not listener[2]
            or any(not isinstance(inode, str) or not inode.isdigit()
                   for inode in listener[2])):
        return None
    server_urls = _opencode_info_matches_listener(
        base_url, info["urls"], listener[0], listener[1])
    if (not server_urls or _proc_start_ticks(server_pid) != server_start
            or _proc_uid(server_pid) != os.getuid()
            or _proc_uid(frontend_pid) != os.getuid()
            or str(_proc_start_ticks(frontend_pid)) != str(frontend_start)):
        return None
    try:
        selected, source = opencode_collector.session_of_process(frontend_pid)
    except Exception:
        return None
    if (selected != session_id or source != "opencode-tui-selection"
            or str(_proc_start_ticks(frontend_pid)) != str(frontend_start)
            or _proc_start_ticks(server_pid) != server_start):
        return None

    socket_ids = sorted(set(listener[2]))
    identity_parts = [str(frontend_pid), str(frontend_start), str(server_pid),
                      str(server_start), base_url, *server_urls, *socket_ids]
    generation = hashlib.sha256("\0".join(identity_parts).encode("utf-8")).hexdigest()
    return {"url": base_url, "generation": generation,
            "pid": server_pid, "proc_start": str(server_start),
            "socket_inodes": socket_ids, "server_pid": server_pid,
            "server_proc_start": str(server_start), "server_urls": server_urls,
            "frontend_pid": frontend_pid,
            "frontend_proc_start": str(frontend_start)}


def _opencode_endpoint(session_id):
    """Resolve one current OpenCode TUI selection to its V2 server instance."""
    if not sys.platform.startswith("linux") or not _safe_native_ref(session_id):
        return None
    try:
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet.collectors import opencode as opencode_collector
        pids = sorted(int(path.name) for path in Path("/proc").iterdir()
                      if path.name.isdigit())
    except (OSError, ImportError, ValueError):
        return None
    found = []
    for pid in pids:
        if _proc_uid(pid) != os.getuid():
            continue
        start = _proc_start_ticks(pid)
        argv = _proc_argv(pid)
        if not start or not argv or os.path.basename(argv[0]) != "opencode":
            continue
        try:
            observed, source = opencode_collector.session_of_process(pid)
        except Exception:
            continue
        if observed != session_id or source != "opencode-tui-selection":
            continue
        if _proc_start_ticks(pid) != start:
            continue
        explicit = _explicit_opencode_server(argv)
        if explicit:
            base_url = explicit
        else:
            listener = _proc_listening_sockets(pid)
            if listener is None:
                continue
            port, host, _socket_ids = listener
            formatted_host = "[" + host + "]" if ":" in host else host
            base_url = "http://" + formatted_host + ":" + str(port)
        endpoint = _opencode_server_endpoint(
            base_url, session_id, pid, start, opencode_collector)
        if endpoint is not None:
            found.append(endpoint)
    return found[0] if len(found) == 1 else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _opencode_http(endpoint, method, path, payload=None):
    """Call a checked OpenCode endpoint directly; never follow proxies or redirects."""
    if (not isinstance(endpoint, dict) or not isinstance(endpoint.get("url"), str)
            or not path.startswith("/api/")):
        return {"status": 0, "reason": "endpoint-invalid"}
    url = endpoint["url"].rstrip("/") + path
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=3.0) as response:
            status = int(response.status)
            raw = response.read(_CACHE_MAX_BYTES + 1)
            if len(raw) > _CACHE_MAX_BYTES:
                return {"status": status, "reason": "response-oversized"}
            if status == 204 or not raw:
                return {"status": status, "body": None}
            try:
                return {"status": status, "body": json.loads(raw.decode("utf-8"))}
            except (ValueError, UnicodeDecodeError):
                return {"status": status, "reason": "response-invalid"}
    except urllib.error.HTTPError as exc:
        return {"status": int(exc.code), "reason": "http-error"}
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return {"status": 0, "reason": "transport-unavailable"}


def _opencode_data(result):
    body = result.get("body") if isinstance(result, dict) else None
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return None


def _opencode_identity_matches(endpoint, session_id):
    current = _opencode_endpoint(session_id)
    return (current if current and endpoint
            and current.get("generation") == endpoint.get("generation") else None)


def _opencode_form_decimal(value, *, allow_infinity=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if allow_infinity and value in ("Infinity", "-Infinity"):
        return Decimal(value)
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if result.is_nan() or (result.is_infinite() and not allow_infinity):
        return None
    return result


def _opencode_form_option_rows(value):
    if not isinstance(value, list):
        return None
    rows, seen = [], set()
    for option in value:
        if (not isinstance(option, dict) or not isinstance(option.get("value"), str)
                or not option["value"] or not isinstance(option.get("label"), str)
                or not option["label"] or option["value"] in seen):
            return None
        seen.add(option["value"])
        rows.append({"value": option["value"], "label": option["label"],
                     "description": option.get("description")
                     if isinstance(option.get("description"), str) else ""})
    return rows


def _opencode_form_question(field, form_title):
    """Normalize one documented V2 field without losing its native key or value."""
    if not isinstance(field, dict):
        return None, "opencode-form-field-invalid"
    field_type = field.get("type")
    key = _safe_native_ref(field.get("key"))
    if (not key or not isinstance(field_type, str)
            or field_type not in {"string", "number", "integer", "boolean",
                                  "multiselect", "external"}):
        return None, "opencode-form-field-unsupported"
    base_keys = {"key", "type", "title", "description", "required", "hidden", "when"}
    type_keys = {
        "string": {"format", "minLength", "maxLength", "pattern", "placeholder",
                   "default", "options", "custom"},
        "number": {"minimum", "maximum", "default"},
        "integer": {"minimum", "maximum", "default"},
        "boolean": {"default"},
        "multiselect": {"options", "minItems", "maxItems", "custom", "default"},
        "external": {"url"},
    }
    if set(field).difference(base_keys | type_keys[field_type]):
        return None, "opencode-form-field-property-unsupported"
    common = {"id": key,
              "header": field.get("title") if isinstance(field.get("title"), str) else "",
              "question": field.get("title") if isinstance(field.get("title"), str)
              and field["title"] else form_title,
              "description": field.get("description")
              if isinstance(field.get("description"), str) else "",
              "field_type": field_type,
              "required": field.get("required", False),
              "hidden": field.get("hidden", False),
              "when": field.get("when", [])}
    if type(common["required"]) is not bool or type(common["hidden"]) is not bool:
        return None, "opencode-form-field-flags-invalid"
    if not isinstance(common["when"], list):
        return None, "opencode-form-condition-invalid"
    if field_type == "external":
        if not isinstance(field.get("url"), str):
            return None, "opencode-form-external-field-invalid"
        common["url"] = field["url"]
        return common, "opencode-form-external-field-unsupported"

    custom = field.get("custom", False)
    if type(custom) is not bool:
        return None, "opencode-form-custom-flag-invalid"

    if field_type in {"number", "integer"}:
        for name in ("minimum", "maximum"):
            if name in field and _opencode_form_decimal(field[name], allow_infinity=True) is None:
                return None, "opencode-form-numeric-bound-invalid"
            if name in field:
                common[name] = field[name]
        minimum = _opencode_form_decimal(field.get("minimum"), allow_infinity=True)
        maximum = _opencode_form_decimal(field.get("maximum"), allow_infinity=True)
        if minimum is not None and maximum is not None and minimum > maximum:
            return None, "opencode-form-numeric-bounds-invalid"

    if field_type == "string":
        for name in ("minLength", "maxLength"):
            value = field.get(name)
            if value is not None and (type(value) is not int or value < 0):
                return None, "opencode-form-string-bounds-invalid"
            if value is not None:
                common["min_length" if name == "minLength" else "max_length"] = value
        minimum, maximum = common.get("min_length"), common.get("max_length")
        if minimum is not None and maximum is not None and minimum > maximum:
            return None, "opencode-form-string-bounds-invalid"
        # Python and JavaScript do not share regex, URL, and date parsing semantics.
        if any(name in field for name in ("format", "pattern")):
            return common, "opencode-form-string-constraint-unsupported"

    options = field.get("options")
    common["options_present"] = options is not None
    if field_type == "multiselect" or options is not None:
        option_rows = _opencode_form_option_rows(options)
        if option_rows is None or (field_type == "multiselect" and not option_rows):
            return None, "opencode-form-options-invalid"
        if field_type == "string" and not option_rows and not custom:
            return common, "opencode-form-options-empty"
        common["choices"] = option_rows
        common["option_values"] = [row["value"] for row in option_rows]
    else:
        common["choices"] = None
        common["option_values"] = []

    common["custom"] = custom

    if field_type == "multiselect":
        for name in ("minItems", "maxItems"):
            value = field.get(name)
            if value is not None and (type(value) is not int or value < 0):
                return None, "opencode-form-multiselect-bounds-invalid"
            common["min_items" if name == "minItems" else "max_items"] = value
        minimum, maximum = common.get("min_items"), common.get("max_items")
        if minimum is not None and maximum is not None and minimum > maximum:
            return None, "opencode-form-multiselect-bounds-invalid"

    # A hidden required value has no user-visible input and no documented server defaulting.
    if common["hidden"] and common["required"]:
        return common, "opencode-form-hidden-required-field-unsupported"
    return common, None


def _opencode_form_conditions_supported(questions):
    earlier = {}
    for question in questions:
        for condition in question.get("when", []):
            if (not isinstance(condition, dict) or condition.get("op") not in {"eq", "neq"}
                    or not isinstance(condition.get("key"), str)
                    or condition["key"] not in earlier or "value" not in condition):
                return False
            target = earlier[condition["key"]]
            value = condition["value"]
            target_type = target["field_type"]
            if target_type == "boolean":
                valid = type(value) is bool
            elif target_type in {"number", "integer"}:
                valid = _opencode_form_decimal(value) is not None
            else:
                valid = isinstance(value, str)
                values = target.get("option_values", [])
                if valid and target.get("options_present") and not target.get("custom"):
                    valid = value in values
            if not valid:
                return False
        earlier[question["id"]] = question
    return True


def _opencode_form_schema_binding(title, fields):
    try:
        raw = json.dumps({"title": title, "fields": fields}, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _opencode_current_selected(base, *, requested_id=None):
    session_id = base["session_id"]
    endpoint = _opencode_endpoint(session_id)
    if endpoint is None:
        return {"state": "unknown", "reason": "opencode-server-identity-unavailable"}
    kind = "approval" if base["kind"] in {"approval", "permission"} else "question"
    if kind == "approval":
        collection = "permission"
    else:
        collection = "form"
    sid_path = urllib.parse.quote(session_id, safe="")
    list_result = _opencode_http(endpoint, "GET", f"/api/session/{sid_path}/{collection}")
    requests = _opencode_data(list_result)
    if list_result.get("status") != 200 or not isinstance(requests, list):
        return {"state": "unknown", "reason": "opencode-request-list-unavailable"}
    candidates = [row for row in requests if isinstance(row, dict)
                  and row.get("sessionID") == session_id
                  and _safe_native_ref(row.get("id")) is not None]
    wanted = requested_id or base.get("request_id") or base.get("item_id")
    if wanted is not None:
        matches = [row for row in candidates if row.get("id") == wanted]
    else:
        matches = candidates
    if len(matches) != 1:
        return {"state": "unknown", "reason": "opencode-request-missing-or-ambiguous"}
    request = matches[0]
    request_id = request["id"]
    id_path = urllib.parse.quote(request_id, safe="")
    detail_result = _opencode_http(
        endpoint, "GET", f"/api/session/{sid_path}/{collection}/{id_path}")
    detail = _opencode_data(detail_result)
    if (detail_result.get("status") != 200 or not isinstance(detail, dict)
            or detail.get("sessionID") != session_id or detail.get("id") != request_id):
        return {"state": "unknown", "reason": "opencode-request-detail-unavailable"}
    if not _opencode_identity_matches(endpoint, session_id):
        return {"state": "unknown", "reason": "opencode-server-replaced"}
    native_ref = {"transport": "opencode-http-v2", "generation": endpoint["generation"],
                  "request_id": request_id, "url": endpoint["url"],
                  "server_pid": endpoint["server_pid"],
                  "server_proc_start": endpoint["server_proc_start"],
                  "proc_start": endpoint["server_proc_start"],
                  "frontend_pid": endpoint["frontend_pid"],
                  "frontend_proc_start": endpoint["frontend_proc_start"],
                  "server_urls": endpoint["server_urls"],
                  "socket_inodes": endpoint["socket_inodes"]}
    if kind == "approval":
        turn_id = _safe_native_ref(detail.get("messageID"))
        if detail.get("state") not in (None, "pending"):
            return {"state": "unknown", "reason": "opencode-permission-not-pending"}
        request_detail = {
            "title": detail.get("title") if isinstance(detail.get("title"), str) else "",
            "operation": detail.get("type") if isinstance(detail.get("type"), str) else "",
            "metadata": detail.get("metadata") if isinstance(detail.get("metadata"), dict) else {},
            "pattern": detail.get("pattern"),
            "message_id": turn_id,
        }
        supported = bool(turn_id and (request_detail["title"] or request_detail["operation"]
                                      or request_detail["metadata"] or request_detail["pattern"]))
        return {"state": "open" if supported else "unknown",
                "reason": None if supported else "opencode-permission-binding-or-detail-incomplete",
                "kind": kind, "request_id": request_id, "turn_id": turn_id,
                "question_id": None, "detail": request_detail, "choices": None,
                "support": supported, "native_ref": native_ref}

    form_state = detail.get("state")
    form_status = form_state.get("status") if isinstance(form_state, dict) else None
    if form_status not in {"pending", "answered", "cancelled"}:
        return {"state": "unknown",
                "reason": "opencode-form-not-pending", "kind": kind,
                "request_id": request_id, "native_ref": native_ref}
    fields = detail.get("fields")
    if not isinstance(fields, list) or not fields:
        return {"state": "unknown", "reason": "opencode-form-needs-native-ui",
                "kind": kind, "request_id": request_id, "native_ref": native_ref}
    questions, field_keys, schema_error = [], [], None
    for field in fields:
        question, error = _opencode_form_question(
            field, detail.get("title") if isinstance(detail.get("title"), str) else "")
        if question is None:
            return {"state": "unknown", "reason": error,
                    "kind": kind, "request_id": request_id, "native_ref": native_ref}
        if question["id"] in field_keys:
            return {"state": "unknown", "reason": "opencode-form-field-keys-ambiguous",
                    "kind": kind, "request_id": request_id, "native_ref": native_ref}
        field_keys.append(question["id"])
        questions.append(question)
        schema_error = schema_error or error
    if not _opencode_form_conditions_supported(questions):
        schema_error = schema_error or "opencode-form-condition-unsupported"
    question_id = field_keys[0] if len(field_keys) == 1 else None
    metadata = detail.get("metadata") if isinstance(detail.get("metadata"), dict) else {}
    supplied_turns = [metadata[key] for key in ("turnID", "messageID")
                      if key in metadata and metadata[key] is not None]
    turn_refs = [_safe_native_ref(value) for value in supplied_turns]
    if any(value is None for value in turn_refs) or len(set(turn_refs)) > 1:
        schema_error = schema_error or "opencode-form-native-turn-invalid"
    turn_id = turn_refs[0] if turn_refs and all(turn_refs) else None
    title = detail.get("title") if isinstance(detail.get("title"), str) else ""
    schema_binding = _opencode_form_schema_binding(title, fields)
    if schema_binding is None:
        schema_error = schema_error or "opencode-form-schema-unbindable"
    native_ref.update({"session_id": session_id, "field_keys": field_keys,
                       "schema_binding": schema_binding})
    if len(questions) == 1:
        question = questions[0]
        native_ref.update({"field_type": question["field_type"],
                           "option_values": question.get("option_values", []),
                           "custom": question.get("custom", False),
                           "min_items": question.get("min_items"),
                           "max_items": question.get("max_items")})
    detail_value = {"title": title, "questions": questions}
    if len(questions) == 1:
        question = questions[0]
        detail_value.update({"question": question["question"],
                             "description": question["description"],
                             "field_type": question["field_type"]})
    choices = questions[0].get("choices") if len(questions) == 1 else None
    supported = schema_error is None and schema_binding is not None
    terminal = {"answered": "applied", "cancelled": "cancelled"}.get(form_status)
    return {"state": terminal if supported and terminal else "open" if supported else "unknown",
            "reason": "opencode-form-not-pending" if terminal else None if supported else
            (schema_error or "opencode-form-schema-unsupported"),
            "kind": kind, "request_id": request_id, "turn_id": turn_id,
            "question_id": question_id, "detail": detail_value,
            "choices": choices, "support": supported and terminal is None, "native_ref": native_ref,
            **({"result": {"source": "opencode-http-v2", "runtime": form_status,
                           "progress": "unknown"}} if supported and terminal else {})}


def _codex_control_path(argv):
    result = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--control":
            if result is not None or index + 1 >= len(argv):
                return None
            result = argv[index + 1]
            index += 2
            continue
        if arg.startswith("--control="):
            if result is not None:
                return None
            result = arg.split("=", 1)[1]
        index += 1
    return result


def _codex_control_request(path, payload):
    try:
        path = Path(path)
        meta = os.lstat(path)
        if (not stat.S_ISSOCK(meta.st_mode) or meta.st_uid != os.getuid()
                or stat.S_IMODE(meta.st_mode) & 0o077):
            return None
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) > 256 * 1024:
            return None
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(2.5)
        try:
            connection.connect(str(path))
            connection.sendall(encoded)
            response = bytearray()
            while b"\n" not in response:
                chunk = connection.recv(8192)
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > _CACHE_MAX_BYTES:
                    return None
        finally:
            connection.close()
        line, separator, remainder = bytes(response).partition(b"\n")
        if not separator or remainder.strip():
            return None
        value = json.loads(line.decode("utf-8"))
        return value if isinstance(value, dict) and value.get("schema_version") == 1 else None
    except (OSError, ValueError, TypeError, UnicodeDecodeError):
        return None


def _codex_control(session_id):
    """Discover the one same-user managed gateway bound to this exact thread."""
    if not sys.platform.startswith("linux") or not _safe_native_ref(session_id):
        return None
    matches = []
    try:
        pids = sorted(int(path.name) for path in Path("/proc").iterdir()
                      if path.name.isdigit())
    except (OSError, ValueError):
        return None
    for pid in pids:
        if _proc_uid(pid) != os.getuid():
            continue
        start = _proc_start_ticks(pid)
        argv = _proc_argv(pid)
        if not start or not any(os.path.basename(arg) == "codex-managed-gateway.py" for arg in argv):
            continue
        control_path = _codex_control_path(argv)
        if not control_path or _proc_start_ticks(pid) != start:
            continue
        status = _codex_control_request(control_path, {"schema_version": 1, "op": "status"})
        if (not status or status.get("status") != "ready"
                or status.get("binding_blocked") is not False
                or status.get("thread_id") != session_id
                or status.get("upstream_clients") != 1
                or not isinstance(status.get("epoch"), int)
                or isinstance(status.get("epoch"), bool)):
            continue
        if _proc_start_ticks(pid) != start:
            continue
        matches.append({"path": control_path, "pid": pid, "proc_start": str(start),
                        "epoch": status["epoch"], "status": status})
    return matches[0] if len(matches) == 1 else None


def _codex_safe_ref(ref):
    if not isinstance(ref, dict):
        return False
    forbidden = {"detail", "question", "choices", "prompt", "draft", "response",
                 "command", "arguments", "output", "text", "payload"}
    if forbidden.intersection(ref):
        return False
    no_turn_elicitation = (ref.get("kind") == "elicitation"
                           and ref.get("method") == "mcpServer/elicitation/request"
                           and ref.get("turn_id") is None)
    return (ref.get("harness") == "codex"
            and isinstance(ref.get("session_id"), str)
            and _safe_request_id(ref.get("request_id")) is not None
            and (_safe_native_ref(ref.get("turn_id")) is not None or no_turn_elicitation))


def _codex_exact_binding(value):
    no_turn_elicitation = (isinstance(value, dict) and value.get("kind") == "elicitation"
                           and value.get("turn_id") is None)
    return (isinstance(value, dict) and value.get("harness") == "codex"
            and isinstance(value.get("session_id"), str)
            and _safe_request_id(value.get("request_id")) is not None
            and (_safe_native_ref(value.get("turn_id")) is not None or no_turn_elicitation))


def _codex_operation_available(value):
    status = value.get("status") if isinstance(value, dict) else None
    return (isinstance(status, str) and bool(status)
            and status.lower() not in {"unsupported", "unknown", "error", "rejected",
                                       "disconnected", "stale", "unavailable"})


def _codex_questions(detail):
    questions = detail.get("questions") if isinstance(detail, dict) else None
    if not isinstance(questions, list) or not questions:
        return None, "codex-question-list-unavailable"
    normalized = []
    seen = set()
    for question in questions:
        if not isinstance(question, dict):
            return None, "codex-question-schema-invalid"
        question_id = _safe_native_ref(question.get("id"))
        prompt = question.get("question")
        options = question.get("options")
        if (not question_id or question_id in seen or not isinstance(prompt, str) or not prompt
                or (options is not None and not isinstance(options, list))):
            return None, "codex-question-schema-invalid"
        seen.add(question_id)
        choices = None
        if isinstance(options, list):
            choices = []
            for option in options:
                if not isinstance(option, dict) or not isinstance(option.get("label"), str) \
                        or not option["label"]:
                    return None, "codex-question-options-invalid"
                choices.append({"value": option["label"], "label": option["label"],
                                "description": option.get("description")
                                if isinstance(option.get("description"), str) else ""})
        normalized.append({"id": question_id,
                           "header": question.get("header")
                           if isinstance(question.get("header"), str) else "",
                           "question": prompt, "choices": choices,
                           "is_other": bool(question.get("isOther")),
                           "is_secret": bool(question.get("isSecret"))})
    result = {"questions": normalized}
    if len(normalized) == 1:
        result["question"] = normalized[0]["question"]
        result["header"] = normalized[0]["header"]
    return {"detail": result,
            "question_id": normalized[0]["id"] if len(normalized) == 1 else None,
            "choices": normalized[0]["choices"] if len(normalized) == 1 else None}, None


def _codex_request_ref_matches(base, ref):
    if (not _codex_safe_ref(ref) or ref.get("session_id") != base.get("session_id")):
        return False
    wanted_id = base.get("request_id")
    if wanted_id is not None and not _same_request_id(wanted_id, ref.get("request_id")):
        return False
    for key in ("turn_id", "item_id"):
        wanted = base.get(key)
        if wanted is not None and ref.get(key) != wanted:
            return False
    return True


def _codex_retired_selected(base, control, saved_ref):
    saved_gateway = base.get("native_ref")
    if (not isinstance(saved_gateway, dict)
            or saved_gateway.get("transport") != "codex-managed-control"
            or saved_gateway.get("pid") != control.get("pid")
            or saved_gateway.get("proc_start") != control.get("proc_start")
            or saved_gateway.get("control_path") != control.get("path")
            or not _codex_request_ref_matches(base, saved_ref)):
        return None
    result = _codex_control_request(control["path"], {
        "schema_version": 1, "op": "interaction-detail", "ref": saved_ref})
    request = result.get("request") if isinstance(result, dict) else None
    if (not isinstance(request, dict) or not _codex_exact_binding(request)
            or request.get("session_id") != base.get("session_id")
            or not _same_request_id(request.get("request_id"), saved_ref.get("request_id"))
            or request.get("turn_id") != saved_ref.get("turn_id")):
        return None
    state = request.get("state")
    status = result.get("status")
    if state not in _SELECTED_STATES or status != state:
        return None
    return {"state": state, "kind": request.get("kind"),
            "request_id": request["request_id"], "turn_id": request.get("turn_id"),
            "detail": request.get("detail"), "support": False,
            "reason": "codex-native-request-" + state,
            "native_ref": saved_gateway, "gateway": control}


def _codex_current_selected(base, *, requested_id=None):
    session_id = base["session_id"]
    control = _codex_control(session_id)
    if control is None:
        return {"state": "unknown", "reason": "codex-managed-control-unavailable"}
    listing = _codex_control_request(control["path"], {
        "schema_version": 1, "op": "interaction-list", "session_id": session_id})
    requests = listing.get("requests") if isinstance(listing, dict) else None
    if not _codex_operation_available(listing) or not isinstance(requests, list):
        return {"state": "unknown", "reason": "codex-native-request-list-unavailable"}
    candidates = [ref for ref in requests if _codex_safe_ref(ref)
                  and ref.get("session_id") == session_id]
    wanted_id = requested_id if requested_id is not None else base.get("request_id")
    wanted_item = base.get("item_id")
    if wanted_id is not None:
        candidates = [ref for ref in candidates
                      if _same_request_id(ref.get("request_id"), wanted_id)]
    if wanted_item is not None:
        candidates = [ref for ref in candidates if ref.get("item_id") == wanted_item]
    saved = base.get("native_ref")
    saved_ref = saved.get("request") if isinstance(saved, dict) else None
    if isinstance(saved_ref, dict):
        exact = [ref for ref in candidates if ref == saved_ref]
        if exact:
            candidates = exact
        else:
            retired = _codex_retired_selected(base, control, saved_ref)
            if retired is not None:
                return retired
            if candidates:
                return {"state": "unknown", "reason": "codex-native-request-binding-changed"}
    if not candidates:
        return {"state": "unknown", "reason": "codex-native-request-missing-or-ambiguous"}
    if len(candidates) != 1:
        return {"state": "unknown", "reason": "codex-native-request-missing-or-ambiguous"}
    safe_ref = candidates[0]
    ref_epoch = safe_ref.get("connection_epoch", safe_ref.get("gateway_epoch", safe_ref.get("epoch")))
    if ref_epoch != control["epoch"]:
        retired = _codex_retired_selected(base, control, safe_ref)
        if retired is not None:
            return retired
        return {"state": "unknown", "reason": "codex-native-connection-replaced"}
    detail_result = _codex_control_request(control["path"], {
        "schema_version": 1, "op": "interaction-detail", "ref": safe_ref})
    request = detail_result.get("request") if isinstance(detail_result, dict) else None
    if (not _codex_operation_available(detail_result)
            or not _codex_exact_binding(request)
            or request.get("session_id") != session_id
            or not _same_request_id(request.get("request_id"), safe_ref.get("request_id"))
            or request.get("turn_id") != safe_ref.get("turn_id")):
        return {"state": "unknown", "reason": "codex-native-request-detail-unavailable"}
    request_epoch = request.get("connection_epoch", request.get("gateway_epoch", request.get("epoch")))
    if request_epoch is not None and request_epoch != control["epoch"]:
        return {"state": "unknown", "reason": "codex-native-request-not-current"}
    kind = request.get("kind")
    if request.get("state") != "open":
        return _codex_retired_selected(base, control, safe_ref) or {
            "state": "unknown", "reason": "codex-native-request-not-current"}
    if kind not in {"question", "approval", "elicitation"}:
        return {"state": "unknown", "kind": kind,
                "request_id": request.get("request_id"), "turn_id": request.get("turn_id"),
                "detail": request.get("detail"), "support": False,
                "reason": "codex-native-request-kind-unsupported",
                "native_ref": {"transport": "codex-managed-control",
                               "pid": control["pid"], "proc_start": control["proc_start"],
                               "control_path": control["path"], "epoch": control["epoch"],
                               "request": safe_ref}, "gateway": control}
    if not isinstance(request.get("detail"), (str, dict)):
        return {"state": "unknown", "reason": "codex-native-request-detail-invalid"}
    detail = request["detail"]
    question_id = request.get("question_id")
    choices = request.get("choices")
    reason = None
    if kind == "question":
        normalized, reason = _codex_questions(detail)
        if normalized is None:
            return {"state": "open", "kind": kind, "request_id": request["request_id"],
                    "turn_id": request["turn_id"], "detail": detail, "support": False,
                    "reason": reason, "native_ref": {"transport": "codex-managed-control",
                    "pid": control["pid"], "proc_start": control["proc_start"],
                    "control_path": control["path"], "epoch": control["epoch"],
                    "request": safe_ref}, "gateway": control}
        detail, question_id, choices = (normalized["detail"], normalized["question_id"],
                                        normalized["choices"])
    native_ref = {"transport": "codex-managed-control",
                  "pid": control["pid"], "proc_start": control["proc_start"],
                  "control_path": control["path"], "epoch": control["epoch"],
                  "request": safe_ref}
    support = kind in {"question", "approval"}
    reason = None
    native_support = request.get("support")
    if isinstance(native_support, dict) and native_support.get("approve_supported") is False:
        reason = native_support.get("reason")
    if kind == "elicitation":
        support = False
        reason = ("codex-native-turn-binding-unavailable" if request.get("turn_id") is None
                  else "codex-native-elicitation-requires-native-ui")
    return {"state": "open", "kind": kind, "request_id": request["request_id"],
            "turn_id": request["turn_id"], "route_id": request.get("route_id"),
            "question_id": question_id, "item_id": request.get("item_id"),
            "detail": detail, "choices": choices, "support": support, "reason": reason,
            "native_ref": native_ref,
            "gateway": control}


_OWNER_INPUT_BINDING_KEYS = ("attempt_id", "harness", "owner_route_id", "owner_route_hash",
                             "route_id", "route_hash", "parent_sid", "supervisor_lease_nonce",
                             "parent_harness", "owner_route_file", "route_file")


def _workflow_registry_paths():
    try:
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet.collectors import dispatch
        return tuple(Path(path).expanduser().resolve(strict=True)
                     for path in dispatch.human_gate_registry_paths())
    except (OSError, ValueError, TypeError, ImportError):
        return ()


def _workflow_current_item(item_id, source):
    """Re-read the exact S2/S3 wait from its registry source before showing detail."""
    try:
        import human_gate_observation
    except ImportError:
        return None, "workflow-wait-observer-unavailable"
    paths = _workflow_registry_paths()
    if not paths:
        return None, "workflow-registry-unavailable"
    matches = []
    for path in paths:
        try:
            result = human_gate_observation.read_answer_waits(jobs_paths=(path,))
        except Exception:
            return None, "workflow-wait-source-unknown"
        if result.get("complete") is not True:
            return None, "workflow-wait-source-incomplete"
        matches.extend((path, item) for item in result.get("items", ())
                       if isinstance(item, dict) and item.get("id") == item_id
                       and item.get("source") == source)
    if len(matches) != 1:
        return None, ("workflow-wait-source-ambiguous" if matches else
                      "workflow-wait-no-longer-current")
    return matches[0], None


def _workflow_item_matches(base, item):
    for key in ("id", "source", "route_id", "attempt_id", "gate_id"):
        if base.get(key) != item.get(key):
            return False
    try:
        if abs(float(base.get("since")) - float(item.get("since"))) >= 0.001:
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    old_hash = base.get("route_hash")
    if old_hash is not None and old_hash != item.get("route_hash"):
        return False
    old_owner, owner = base.get("owner") or {}, item.get("owner") or {}
    return (old_owner.get("harness") == owner.get("harness")
            and old_owner.get("session_id") == owner.get("session_id"))


def _owner_binding(metadata):
    return {key: str(metadata.get(key) or "") for key in _OWNER_INPUT_BINDING_KEYS}


def _workflow_owner_person_context(base, metadata, jobs):
    """Require the selected route's current effective native parent to answer."""
    env = os.environ
    if (env.get("AGENT_DISPATCH_REGISTERED_WORKER") == "1"
            or env.get("AGENT_DISPATCH_CHILD") == "1"
            or env.get("AGENT_DISPATCH_ATTEMPT_ID")):
        return False
    depth = env.get("AGENT_DISPATCH_DEPTH")
    if depth not in (None, "", "0"):
        return False
    try:
        import route_authority
        harness, session_id = route_authority.caller_identity()
    except Exception:
        return False
    owner = base.get("owner") if isinstance(base.get("owner"), dict) else {}
    parent_harness = metadata.get("parent_harness")
    parent_session = metadata.get("parent_sid")
    if not (harness and session_id and parent_harness and parent_session):
        return False
    try:
        import dispatch_seat_handover
        from peer_read_context import PeerReadContext

        read_context = PeerReadContext()
        effective_session = dispatch_seat_handover.effective_parent(
            metadata, jobs, read_context=read_context)
        effective_harness = dispatch_seat_handover.effective_parent_harness(
            metadata, jobs, read_context=read_context)
        owns = route_authority.owns(metadata, session_id, jobs, read_context=read_context)
    except Exception:
        return False
    return bool(owns and harness == effective_harness == owner.get("harness")
                and session_id == effective_session == owner.get("session_id"))


def _workflow_owner_lineage(jobs, native_ref):
    """Return exact same-route owner attempts after validating the full replacement chain."""
    if not isinstance(native_ref, dict):
        return None
    selected = native_ref.get("attempt_id")
    expected = native_ref.get("owner_binding")
    if not _safe_native_ref(selected) or not isinstance(expected, dict):
        return None
    try:
        import dispatch_completion_join
        import dispatch_replacement

        effective, _mapping = dispatch_replacement.effective_attempts(jobs, {selected})
        if len(effective) != 1:
            return None
        effective_attempt = next(iter(effective))
        chain = []
        seen = set()
        current = selected
        for _ in range(64):
            if current in seen:
                return None
            seen.add(current)
            row = dispatch_completion_join.exact_attempt_row(jobs, current)
            metadata = row.metadata
            if (metadata.get("attempt_id") != current
                    or metadata.get("worker_type") != "owner"
                    or str(metadata.get("dispatch_depth")) != "1"):
                return None
            actual = _owner_binding(metadata)
            if current == selected:
                if actual != expected:
                    return None
            elif any(actual.get(key) != expected.get(key)
                     for key in _OWNER_INPUT_BINDING_KEYS
                     if key not in {"attempt_id", "supervisor_lease_nonce"}):
                return None
            chain.append(current)
            source = dispatch_replacement.source_binding(jobs, metadata)
            replacement = source[1] if source else None
            if not replacement:
                break
            try:
                dispatch_completion_join.exact_attempt_row(jobs, replacement)
            except Exception:
                # A durable reservation without a launched registry row cannot take delivery.
                break
            current = replacement
        else:
            return None
        if effective_attempt not in chain:
            return None
        return {"attempts": chain, "effective_attempt": effective_attempt}
    except Exception:
        return None


def _workflow_owner_snapshot(base, *, expected_native_ref=None):
    """Resolve a current S3 owner row and its existing public input surface."""
    match, error = _workflow_current_item(base.get("id"), "S3")
    if match is None:
        return None, error
    jobs, item = match
    if not _workflow_item_matches(base, item):
        return None, "workflow-owner-request-binding-changed"
    try:
        import dispatch_completion_join
        import dispatch_owner_input
        import route_authority
        row = dispatch_completion_join.exact_attempt_row(jobs, item["attempt_id"])
        metadata = row.metadata
        route_id = metadata.get("owner_route_id") or metadata.get("route_id")
        route_hash = metadata.get("owner_route_hash") or metadata.get("route_hash")
        if (metadata.get("worker_type") != "owner"
                or str(metadata.get("dispatch_depth")) != "1"
                or row.status != "done"
                or route_authority.answerable_owner_end(row.status, metadata) not in {"BLOCKED", "FAIL"}
                or route_id != item.get("route_id") or route_hash != item.get("route_hash")):
            return None, "workflow-owner-target-not-answerable"
        binding = _owner_binding(metadata)
        native_ref = {"transport": "hearting-owner-input", "jobs_path": str(jobs),
                      "source_id": item["id"], "attempt_id": item["attempt_id"],
                      "route_id": route_id, "route_hash": route_hash,
                      "owner_binding": binding}
        if expected_native_ref is not None and expected_native_ref != native_ref:
            return None, "workflow-owner-binding-changed"
        if not _workflow_owner_person_context(base, metadata, jobs):
            return {"jobs": jobs, "item": item, "native_ref": native_ref,
                    "observed": None, "support": False,
                    "reason": "owner-correction-native-parent-context-required"}, None
        lineage = _workflow_owner_lineage(jobs, native_ref)
        if not lineage:
            return {"jobs": jobs, "item": item, "native_ref": native_ref,
                    "observed": None, "support": False,
                    "reason": "owner-correction-lineage-unverified"}, None
        current_attempt = lineage["effective_attempt"]
        try:
            observed = dispatch_owner_input.inspect(jobs, current_attempt)
        except Exception as exc:
            return {"jobs": jobs, "item": item, "native_ref": native_ref,
                    "current_attempt_id": current_attempt,
                    "observed": None, "support": False,
                    "reason": "owner-correction-surface-unavailable:" + type(exc).__name__}, None
        unsettled = {"retained", "queued", "undelivered", "sending", "delivery-unknown"}
        if any((request.get("state") in unsettled
                or request.get("delivery_observation") in unsettled)
               for request in observed.get("requests", ()) if isinstance(request, dict)):
            return {"jobs": jobs, "item": item, "native_ref": native_ref,
                    "current_attempt_id": current_attempt,
                    "observed": observed, "support": False,
                    "reason": "owner-correction-already-pending"}, None
        return {"jobs": jobs, "item": item, "native_ref": native_ref,
                "current_attempt_id": current_attempt,
                "observed": observed, "support": True}, None
    except Exception as exc:
        return None, "owner-correction-target-unavailable:" + type(exc).__name__


def _workflow_owner_receipt(base, entry):
    """Read an already-claimed S3 correction receipt; never retry its delivery."""
    native_ref = base.get("native_ref")
    if (not isinstance(native_ref, dict)
            or native_ref.get("transport") != "hearting-owner-input"
            or not isinstance(entry, dict) or not entry.get("action_id")):
        return {"state": "unknown", "reason": "owner-correction-receipt-binding-missing"}
    try:
        paths = _workflow_registry_paths()
        jobs = Path(native_ref["jobs_path"]).resolve(strict=True)
        if jobs not in paths:
            return {"state": "unknown", "reason": "owner-correction-registry-changed"}
        import dispatch_owner_input
        lineage = _workflow_owner_lineage(jobs, native_ref)
        if not lineage:
            return {"state": "unknown", "reason": "owner-correction-target-replaced"}
        prior_result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
        target = prior_result.get("target_attempt_id")
        lineage_attempts = lineage["attempts"]
        targets = [target] if target is not None else lineage_attempts
        if any(attempt not in lineage_attempts for attempt in targets):
            return {"state": "unknown", "reason": "owner-correction-target-outside-lineage"}
        matches = []
        for attempt in targets:
            try:
                observed = dispatch_owner_input.inspect(jobs, attempt)
            except Exception:
                continue
            matches.extend((attempt, request) for request in observed.get("requests", ())
                           if isinstance(request, dict)
                           and request.get("id") == entry.get("action_id"))
        if len(matches) != 1:
            return {"state": "unknown", "reason": "owner-correction-receipt-unavailable"}
        target, request = matches[0]
        request_state = request.get("state")
        if request_state == "turn-completed":
            state = "applied"
        elif request_state in {"retained", "queued", "accepted"}:
            state = "received"
        else:
            state = "unknown"
        result = {"status": request_state, "request_id": entry.get("action_id"),
                  "target_attempt_id": target}
        if request.get("delivery_observation"):
            result["delivery_observation"] = request["delivery_observation"]
        continuation = prior_result.get("continuation")
        if isinstance(continuation, dict):
            result["continuation"] = continuation
        return {"state": state, "result": result}
    except Exception as exc:
        return {"state": "unknown", "reason": "owner-correction-receipt-unavailable:" +
                type(exc).__name__}


def _stored_workflow_action(ref):
    """Recover a selected workflow receipt after its source wait leaves the projection."""
    if not isinstance(ref, dict):
        return None
    native_ref = ref.get("native_ref")
    if (not isinstance(native_ref, dict)
            or native_ref.get("transport") not in {"hearting-owner-input", "hearting-human-gate"}):
        return None
    entry = _selected_entry(ref)
    if not entry or not entry.get("action_id"):
        return None
    identity = _selected_identity(ref)
    if identity is None:
        return None
    source = "S2" if native_ref.get("transport") == "hearting-human-gate" else "S3"
    return {**identity, "id": ref.get("selected_id") or ref.get("id"),
            "selected_id": ref.get("selected_id") or ref.get("id"),
            "item_id": identity.get("item_id"), "source": source,
            "attempt_id": native_ref.get("attempt_id"), "since": ref.get("since"),
            "detail": ref.get("detail"),
            "native_ref": native_ref}


def _selected_hints_match(values, base):
    for key in ("request_id", "turn_id", "item_id", "question_id", "gate_epoch"):
        supplied = values.get(key)
        actual = base.get(key)
        if supplied is None:
            continue
        if key == "gate_epoch":
            if type(supplied) is not int or supplied != actual:
                return False
            continue
        safe = _safe_request_id(supplied) if key == "request_id" else _safe_native_ref(supplied)
        if safe is None or (not _same_request_id(safe, actual) if key == "request_id"
                            else safe != actual):
            return False
    for key in ("source", "attempt_id", "gate_id"):
        if values.get(key) is not None and values.get(key) != base.get(key):
            return False
    return True


def _workflow_route_source(jobs, item):
    """Resolve the unique sealed route named by this current registry source."""
    try:
        from dispatch_registry_cache import registry_snapshot
        from tools.fleet import route as route_module

        snapshot = registry_snapshot(jobs)
        if not snapshot.complete:
            return None, None, "workflow-route-registry-incomplete"
        matches = set()
        for row in snapshot.view.route_alias_candidates():
            meta = row.completion_metadata()
            route_id = meta.get("owner_route_id") or meta.get("route_id")
            route_hash = meta.get("owner_route_hash") or meta.get("route_hash")
            route_file = meta.get("owner_route_file") or meta.get("route_file")
            if route_id == item.get("route_id") and route_hash == item.get("route_hash") and route_file:
                matches.add(str(route_file))
        if len(matches) != 1:
            return None, None, ("workflow-route-source-ambiguous" if matches else
                                "workflow-route-source-unavailable")
        path = Path(next(iter(matches))).expanduser().resolve(strict=True)
        route = route_module.load(str(path), item.get("route_hash"), item.get("route_id"))
        if (not isinstance(route, dict) or route.get("route_id") != item.get("route_id")
                or route.get("route_hash") != item.get("route_hash")):
            return None, None, "workflow-route-binding-changed"
        return path, route, None
    except Exception as exc:
        return None, None, "workflow-route-source-unavailable:" + type(exc).__name__


def _workflow_gate_source(jobs, item, *, allow_missing_artifact=False):
    """Bind a selected S2 row to the journal's current raise and sealed route."""
    route_path, route, error = _workflow_route_source(jobs, item)
    if route is None:
        return None, error
    try:
        import workflow_state as WS

        ledger_root, _ = WS.ledger_root_for(jobs)
        ledger = WS.WorkflowLedger(item["route_id"], item["route_hash"], root=ledger_root)
        journal = [row for row in ledger.journal()
                   if row.get("route_id") == item["route_id"]
                   and row.get("route_hash") == item["route_hash"]]
        resolution = WS.human_gate_resolution(journal, item.get("gate_id"))
        epoch = resolution.get("epoch")
        if (resolution.get("status") != "blocked" or type(epoch) is not int or epoch < 1
                or not resolution.get("raised_at")):
            return None, "workflow-gate-no-longer-current"
        try:
            raised_at = float(datetime.fromisoformat(
                str(resolution["raised_at"]).replace("Z", "+00:00")).timestamp())
            item_since = float(item.get("since"))
        except (TypeError, ValueError, OverflowError):
            return None, "workflow-gate-raise-time-invalid"
        if not math.isfinite(raised_at) or abs(raised_at - item_since) >= 0.001:
            return None, "workflow-gate-raise-changed"
        artifact = resolution.get("artifact")
        artifact_root = route.get("artifact_root")
        if not isinstance(artifact, str) or not isinstance(artifact_root, str):
            if allow_missing_artifact:
                return {"route_path": str(route_path), "route": route,
                        "resolution": resolution, "gate_epoch": epoch,
                        "artifact_sha256": None,
                        "response_error": "workflow-gate-artifact-unavailable"}, None
            return None, "workflow-gate-artifact-unavailable"
        artifact_path = Path(artifact).expanduser().resolve(strict=True)
        root_path = Path(artifact_root).expanduser().resolve(strict=True)
        if (not artifact_path.is_file()
                or root_path not in artifact_path.parents):
            return None, "workflow-gate-artifact-outside-root"
        artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        recorded_digest = resolution.get("artifact_sha256")
        if recorded_digest is not None and recorded_digest != artifact_sha256:
            return None, "workflow-gate-artifact-changed"
        binding = {"route_path": str(route_path), "route": route,
                   "resolution": resolution, "gate_epoch": epoch,
                   "artifact_sha256": "sha256:" + artifact_sha256}
        return binding, None
    except Exception as exc:
        return None, "workflow-gate-journal-unavailable:" + type(exc).__name__


def _workflow_gate_interview(binding, item):
    """Load only the exact interview sealed on this gate raise, using its validator."""
    resolution = binding["resolution"]
    artifact = resolution.get("artifact")
    digest = binding.get("artifact_sha256")
    route = binding["route"]
    if not artifact or not digest or not isinstance(route.get("artifact_root"), str):
        return None, None
    try:
        import frame_interview as FI
        import human_gate_observation as HGO

        path = Path(artifact).expanduser().resolve(strict=True)
        if path.suffix.lower() != ".json":
            return None, None
        if not HGO._inside(path, route["artifact_root"]):
            return None, "workflow-interview-outside-artifact-root"
        if "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            return None, "workflow-interview-artifact-changed"
        interview = FI.load_json_file(path, kind="interview")
        if not FI.is_interview(interview):
            return None, None
        if (interview.get("route_id") != item.get("route_id") or FI.validate(interview)):
            return None, "workflow-interview-invalid"
        return interview, None
    except Exception as exc:
        return None, "workflow-interview-unavailable:" + type(exc).__name__


def _workflow_gate_detail(binding, item):
    """Build selected-only questions from the current gate or its sealed interview."""
    route = binding["route"]
    gate = item.get("gate_id")
    if binding.get("response_error"):
        # A current legacy raise can still keep its selected private draft.
        # Missing release input never becomes permission to release the gate.
        return {"detail": {"gate": gate, "question": item.get("question"),
                           "choices": item.get("choices") or []},
                "choices": [], "interview": None}, binding["response_error"]
    interview, error = _workflow_gate_interview(binding, item)
    if error:
        return None, error
    bindings = [row for row in route.get("human_gate_bindings", ())
                if isinstance(row, dict) and row.get("gate") == gate]
    if len(bindings) != 1:
        return None, "workflow-gate-route-binding-ambiguous"
    target_node = bindings[0].get("node")
    node = next((row for row in route.get("nodes", ())
                 if isinstance(row, dict) and row.get("id") == target_node), None)
    target = {"route_id": route.get("route_id"), "gate": gate, "node": target_node}
    if node:
        for key in ("title", "task", "description"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                target[key] = value.strip()[:240]
                break
    if interview is None:
        if binding["resolution"].get("interview"):
            return None, "workflow-interview-unavailable"
        detail = {"gate": gate, "target": target, "response_mode": "gate-decision",
                  "decision": "explicit", "questions": [{
                      "id": "gate-decision", "question": "Choose what to do with this human gate.",
                      "choices": [{"value": decision, "label": decision}
                                  for decision in ("proceed", "revise", "stop")],
                      "is_other": False,
                  }]}
        return {"detail": detail, "choices": ["proceed", "revise", "stop"],
                "interview": None}, None
    import frame_interview as FI
    questions = [{"id": FI.UNDERSTANDING_SLOT,
                  "question": interview["understanding"],
                  "choices": [{"value": "I understand the restatement",
                               "label": "I understand the restatement"}],
                  "is_other": True}]
    for question in interview.get("questions", ()):
        if not isinstance(question, dict):
            continue
        options = question.get("options") if isinstance(question.get("options"), list) else []
        questions.append({"id": question["id"], "question": question["question"],
                          "choices": [{"value": option["label"], "label": option["label"],
                                       **({"means": option["means"]} if isinstance(option.get("means"), str)
                                          else {})}
                                      for option in options if isinstance(option, dict)
                                      and isinstance(option.get("label"), str)],
                          # The existing frame answer validator accepts off-menu notes.
                          "is_other": True})
    detail = {"gate": gate, "target": target, "response_mode": "frame-interview",
              "decision": "proceed", "questions": questions}
    return {"detail": detail, "choices": [], "interview": interview}, None


def _workflow_gate_receipt(base, entry):
    """Confirm one selected gate action from the release receipt and journal transition."""
    native = base.get("native_ref") if isinstance(base, dict) else None
    if (not isinstance(native, dict) or native.get("transport") != "hearting-human-gate"
            or not isinstance(entry, dict) or not entry.get("action_id")):
        return {"state": "unknown", "reason": "workflow-release-receipt-binding-missing"}
    jobs = native.get("jobs_path")
    route_path = native.get("route_path")
    route_id, route_hash, gate = native.get("route_id"), native.get("route_hash"), native.get("gate_id")
    epoch, request_id = native.get("gate_epoch"), entry.get("action_id")
    if (not isinstance(jobs, str) or not isinstance(route_path, str)
            or type(epoch) is not int or epoch < 1 or not isinstance(request_id, str)):
        return {"state": "unknown", "reason": "workflow-release-receipt-binding-invalid"}
    public_receipt = entry.get("result")
    if (not isinstance(public_receipt, dict) or public_receipt.get("route_id") != route_id
            or public_receipt.get("route_hash") != route_hash
            or public_receipt.get("gate") != gate or public_receipt.get("gate_epoch") != epoch
            or public_receipt.get("request_id") != request_id
            or public_receipt.get("decision") not in {"proceed", "revise", "stop"}
            or public_receipt.get("actor_kind") != "user"):
        return {"state": "unknown", "reason": "workflow-release-receipt-unconfirmed"}
    try:
        import workflow_state as WS

        ledger_root, _ = WS.ledger_root_for(jobs)
        ledger = WS.WorkflowLedger(route_id, route_hash, root=ledger_root)
        journal = [row for row in ledger.journal()
                   if row.get("route_id") == route_id and row.get("route_hash") == route_hash]
        releases = [(row, row.get("evidence")) for row in journal
                    if isinstance(row.get("evidence"), dict)
                    and row["evidence"].get("selected_request_id") == request_id]
        if any(evidence.get("released_gate") != gate
               or evidence.get("gate_epoch") != epoch
               or evidence.get("route_hash") != route_hash
               or evidence.get("decision") != public_receipt["decision"]
               for _row, evidence in releases):
            return {"state": "unknown", "reason": "workflow-release-request-binding-changed"}
        release = next((row for row, evidence in reversed(releases)
                        if evidence.get("released_gate") == gate
                        and evidence.get("gate_epoch") == epoch), None)
        person_release = any(evidence.get("actor_kind") == "user"
                             and evidence.get("released_gate") == gate
                             and evidence.get("decision") == public_receipt["decision"]
                             for _row, evidence in releases)
        sidecar = Path(route_path).with_name(Path(route_path).stem + ".gate-release.json")
        try:
            value = json.loads(sidecar.read_text(encoding="utf-8"))
        except FileNotFoundError:
            value = None
        rows = value.get("gate_releases") if isinstance(value, dict) else None
        sidecar_row = (next((row for row in reversed(rows or ())
                            if isinstance(row, dict) and row.get("request_id") == request_id
                            and row.get("gate") == gate and row.get("route_hash") == route_hash
                            and row.get("gate_epoch") == epoch), None)
                       if isinstance(value, dict) and value.get("route_id") == route_id else None)
        if (release is None
                or release.get("workflow_state") not in {
                    "RUNNING", "NEXT_REGISTERED", "FAILED_RETRYABLE", "CANCELLED"}
                or not person_release):
            return {"state": "unknown", "reason": "workflow-release-receipt-unconfirmed"}
        if sidecar_row is not None:
            recorded = sidecar_row.get("receipt")
            if (not isinstance(recorded, dict)
                    or any(recorded.get(key) != public_receipt.get(key) for key in
                           ("route_id", "route_hash", "gate", "gate_epoch", "request_id",
                            "decision", "actor_kind"))):
                return {"state": "unknown", "reason": "workflow-release-sidecar-mismatch"}
        return {"state": "applied", "result": {
            "action_id": request_id, "route_id": route_id, "route_hash": route_hash,
            "gate": gate, "gate_epoch": epoch, "decision": public_receipt["decision"],
            "workflow_state": public_receipt.get("workflow_state") or release.get("workflow_state"),
            "actor_kind": "user"}}
    except Exception as exc:
        return {"state": "unknown", "reason": "workflow-release-receipt-unavailable:" +
                type(exc).__name__}


def _selected_base(ref):
    cache = _read_cache()
    if isinstance(ref, str):
        values = {"id": ref}
    elif isinstance(ref, dict):
        values = ref.get("selected_ref") if isinstance(ref.get("selected_ref"), dict) else ref
        if isinstance(ref.get("ref"), dict):
            values = ref["ref"]
    else:
        return None, "selected-wait-reference-invalid"
    item_id = values.get("id") or values.get("selected_id")
    if not isinstance(item_id, str):
        return None, "selected-wait-reference-invalid"
    if cache is None:
        stored = _stored_workflow_action(ref)
        if stored is not None:
            return stored, None
        _request_producer()
        return None, "selected-wait-cache-unknown"
    item = next((row for row in cache["items"] if row.get("id") == item_id), None)
    if not isinstance(item, dict):
        stored = _stored_workflow_action(ref)
        if stored is not None:
            return stored, None
        return None, "selected-item-not-current"
    owner = item.get("owner") or {}
    harness, session_id = owner.get("harness"), owner.get("session_id")
    if harness not in {"claude", "codex", "opencode"} or not _safe_native_ref(session_id):
        return None, "selected-item-identity-unknown"
    for key, actual in (("harness", harness), ("session_id", session_id)):
        supplied = values.get(key)
        if supplied is not None and supplied != actual:
            return None, "selected-item-identity-changed"
    route_id = item.get("route_id")
    supplied_route = values.get("route_id")
    if route_id is not None and supplied_route not in (None, route_id):
        return None, "selected-route-changed"
    source = item.get("source")
    if source == "S1":
        hints = {}
        for key in ("request_id", "turn_id", "item_id", "question_id"):
            safe_ref = _safe_request_id if key == "request_id" else _safe_native_ref
            actual = safe_ref(item.get(key))
            supplied = safe_ref(values.get(key))
            if item.get(key) is not None and actual is None:
                return None, "selected-native-reference-invalid"
            if (actual is not None and supplied is not None
                    and (not _same_request_id(actual, supplied) if key == "request_id"
                         else actual != supplied)):
                return None, "selected-native-reference-changed"
            hints[key] = actual if actual is not None else supplied
        raw_kind = item.get("kind")
        if raw_kind not in {"decision", "approval", "permission", "elicitation"}:
            return None, "selected-kind-unsupported"
        return {"id": item_id, "source": source, "harness": harness,
                "session_id": session_id, "route_id": route_id, "kind": raw_kind,
                "since": item.get("since"), **hints,
                "native_ref": ref.get("native_ref") if isinstance(ref, dict) else None}, None

    if source == "S4":
        signal = next((value for value in read_signals()
                       if value.get("id") == item_id), None)
        if (signal is None or signal.get("harness") != harness
                or signal.get("session_id") != session_id):
            return None, "selected-marker-no-longer-current"
        native_ref = {"transport": "hearting-answer-marker", "signal_id": item_id,
                      "seat_key": signal["seat_key"], "sequence": signal.get("sequence")}
        supplied = ref.get("native_ref") if isinstance(ref, dict) else None
        if supplied is not None and supplied != native_ref:
            return None, "selected-marker-binding-changed"
        base = {"id": item_id, "source": source, "harness": harness,
                "session_id": session_id, "route_id": None, "kind": "decision",
                "request_id": item_id, "turn_id": None, "question_id": "marker-response",
                "since": item.get("since"), "question": signal.get("question"),
                "native_ref": native_ref}
        if not _selected_hints_match(values, base):
            return None, "selected-marker-reference-changed"
        return base, None

    if source not in {"S2", "S3"}:
        return None, "selected-item-source-unsupported"
    base = {"id": item_id, "source": source, "harness": harness,
            "session_id": session_id, "route_id": route_id, "route_hash": item.get("route_hash"),
            "attempt_id": item.get("attempt_id"), "gate_id": item.get("gate_id"),
            "kind": "decision", "since": item.get("since"), "owner": owner,
            "question": item.get("question"), "choices": item.get("choices") or []}
    if source == "S2":
        base.update({"request_id": item_id,
                     "turn_id": None,
                     "question_id": item.get("gate_id"),
                     "item_id": item.get("attempt_id")
                     if _safe_native_ref(item.get("attempt_id")) else None})
        match, error = _workflow_current_item(item_id, source)
        if match is None:
            stored = _stored_workflow_action(ref)
            if stored is not None:
                return stored, None
            return None, error
        jobs, current = match
        if not _workflow_item_matches(base, current):
            return None, "workflow-gate-binding-changed"
        binding, error = _workflow_gate_source(jobs, current, allow_missing_artifact=True)
        if binding is None:
            return None, error
        base["gate_epoch"] = binding["gate_epoch"]
        base["route_path"] = binding["route_path"]
        native_ref = {"transport": "hearting-human-gate", "jobs_path": str(jobs),
                      "source_id": item_id, "route_id": current.get("route_id"),
                      "route_hash": current.get("route_hash"), "gate_id": current.get("gate_id"),
                      "attempt_id": current.get("attempt_id"),
                      "route_path": binding["route_path"],
                      "artifact_sha256": binding["artifact_sha256"],
                      "gate_epoch": binding["gate_epoch"],
                      "raised_at": float(current.get("since"))}
    else:
        attempt_id = item.get("attempt_id")
        if not _safe_native_ref(attempt_id):
            return None, "workflow-owner-attempt-unknown"
        base.update({"request_id": item_id, "turn_id": None,
                     "question_id": "owner-correction", "item_id": attempt_id})
        snapshot, error = _workflow_owner_snapshot(base)
        if snapshot is None:
            stored = _stored_workflow_action(ref)
            if stored is not None:
                return stored, None
            return None, error
        native_ref = snapshot["native_ref"]
    if not _selected_hints_match(values, base):
        return None, "selected-workflow-reference-changed"
    supplied = ref.get("native_ref") if isinstance(ref, dict) else None
    if supplied is not None and supplied != native_ref:
        return None, "workflow-selected-binding-changed"
    base["native_ref"] = native_ref
    return base, None


def _selected_fallback(base, reason, *, state="unknown"):
    if base is None:
        base = {"harness": "unknown", "session_id": "", "kind": "question"}
    kind = "approval" if base.get("kind") in {"approval", "permission"} else "question"
    value = {"v": 1, "harness": base.get("harness"),
             "session_id": base.get("session_id"),
             "request_id": base.get("request_id"), "turn_id": base.get("turn_id"),
             "kind": kind, "detail": None, "state": state,
             "support": {"supported": False, "reason": reason},
             "actions": [], "result": {"reason": reason}}
    for key in ("route_id", "question_id", "item_id", "gate_epoch"):
        if base.get(key) is not None:
            value[key] = base[key]
    identity = _selected_identity(base)
    if identity is not None:
        value.update(identity)
    if base.get("id") or base.get("selected_id"):
        value["selected_id"] = base.get("id") or base["selected_id"]
    entry = _selected_entry(value)
    if entry:
        value["draft"] = entry.get("draft", "")
        if entry.get("action_id"):
            value["result"] = entry.get("result") or value["result"]
        if (value["state"] == "unknown"
                and entry.get("state") in {"submitted", "received", "applied", "cancelled", "expired"}):
                value["state"] = entry["state"]
    return value


def _selected_actions(kind, detail, native_ref):
    if kind != "approval":
        return ["reply"]
    request = native_ref.get("request") if isinstance(native_ref, dict) else None
    if (isinstance(request, dict)
            and request.get("method") == "item/fileChange/requestApproval"):
        changes = detail.get("changes") if isinstance(detail, dict) else None
        if (not isinstance(detail, dict) or detail.get("grantRoot") is not None
                or not isinstance(changes, list) or not changes
                or any(not isinstance(change, dict) or not isinstance(change.get("path"), str)
                       or not change["path"] for change in changes)):
            return ["reject", "defer"]
    if (isinstance(request, dict)
            and request.get("method") == "item/commandExecution/requestApproval"):
        available = detail.get("availableDecisions") if isinstance(detail, dict) else None
        if available is None:
            return ["approve", "reject", "defer"]  # legacy native one-request decisions
        if not isinstance(available, list):
            return ["defer"]
        actions = []
        if "accept" in available:
            actions.append("approve")
        if "decline" in available:
            actions.append("reject")
        actions.append("defer")
        return actions
    return ["approve", "reject", "defer"]


def _selected_envelope(base, resolved):
    if not isinstance(resolved, dict):
        return _selected_fallback(base, "native-request-unavailable")
    request_id = _safe_request_id(resolved.get("request_id"))
    if request_id is None:
        request_id = base.get("request_id")
    turn_id = _safe_native_ref(resolved.get("turn_id")) or base.get("turn_id")
    route_id = base.get("route_id")
    native_route = _safe_native_ref(resolved.get("route_id"))
    if route_id is not None and native_route is not None and route_id != native_route:
        return _selected_fallback(base, "native-route-binding-changed")
    kind = resolved.get("kind") if resolved.get("kind") in {"question", "approval"} else (
        "approval" if base.get("kind") in {"approval", "permission"} else "question")
    envelope = {"v": 1, "harness": base["harness"], "session_id": base["session_id"],
                "request_id": request_id, "turn_id": turn_id, "kind": kind,
                "detail": resolved.get("detail"),
                "state": resolved.get("state") if resolved.get("state") in _SELECTED_STATES else "unknown"}
    if route_id is not None:
        envelope["route_id"] = route_id
    if base.get("source") in {"S2", "S3", "S4"}:
        envelope["source"] = base["source"]
    if type(base.get("gate_epoch")) is int and base["gate_epoch"] > 0:
        envelope["gate_epoch"] = base["gate_epoch"]
    question_id = _safe_native_ref(resolved.get("question_id")) or base.get("question_id")
    if question_id is not None:
        envelope["question_id"] = question_id
    item_id = _safe_native_ref(resolved.get("item_id")) or base.get("item_id")
    if item_id is not None:
        envelope["item_id"] = item_id
    choices = resolved.get("choices")
    if isinstance(choices, list):
        envelope["choices"] = choices
    if base.get("id"):
        envelope["selected_id"] = base["id"]
    native_ref = resolved.get("native_ref")
    if isinstance(native_ref, dict):
        envelope["native_ref"] = native_ref
    if isinstance(resolved.get("result"), dict):
        envelope["result"] = resolved["result"]
    support = bool(resolved.get("support"))
    reason = resolved.get("reason")
    envelope["support"] = {"supported": support}
    if isinstance(reason, str):
        envelope["support"]["reason"] = reason
        envelope["reason"] = reason
    entry = _selected_entry(envelope)
    if entry:
        envelope["draft"] = entry.get("draft", "")
        if entry.get("result") is not None and "result" not in envelope:
            envelope["result"] = entry["result"]
        if entry.get("state") in {"submitted", "received", "applied", "cancelled", "expired", "unknown"}:
            if envelope["state"] == "open":
                envelope["state"] = entry["state"]
            if entry.get("action_id"):
                envelope["action_id"] = entry["action_id"]
    if envelope["state"] == "open" and support:
        envelope["actions"] = _selected_actions(kind, envelope.get("detail"), native_ref)
    else:
        envelope["actions"] = []
    return envelope


def _selected_transport(base, requested_id=None):
    try:
        if base.get("source") in {"S2", "S3", "S4"}:
            return _workflow_selected_transport(base)
        if base["harness"] == "codex":
            return _codex_current_selected(base, requested_id=requested_id)
        if base["harness"] == "opencode":
            return _opencode_current_selected(base, requested_id=requested_id)
        return {"state": "unknown", "reason": "claude-native-response-surface-unavailable"}
    except Exception:
        return {"state": "unknown", "reason": "native-request-observation-failed"}


def _workflow_selected_transport(base):
    source = base.get("source")
    if source == "S4":
        signal = next((value for value in read_signals()
                       if value.get("id") == base.get("id")), None)
        if (signal is None or signal.get("harness") != base.get("harness")
                or signal.get("session_id") != base.get("session_id")):
            return {"state": "unknown", "reason": "selected-marker-no-longer-current"}
        native_ref = {"transport": "hearting-answer-marker", "signal_id": signal["id"],
                      "seat_key": signal["seat_key"], "sequence": signal.get("sequence")}
        if base.get("native_ref") != native_ref:
            return {"state": "unknown", "reason": "selected-marker-binding-changed"}
        return {"state": "open", "kind": "question", "request_id": base["request_id"],
                "turn_id": None, "question_id": base["question_id"],
                "detail": {"question": signal.get("question"), "choices": []},
                "choices": [], "support": False,
                "reason": "marker-has-no-bound-native-response-surface",
                "native_ref": native_ref}

    entry = _selected_entry(base)
    if source == "S3" and entry and entry.get("action_id"):
        receipt = _workflow_owner_receipt(base, entry)
        return {"state": receipt["state"], "kind": "question",
                "request_id": base["request_id"], "turn_id": base["turn_id"],
                "route_id": base.get("route_id"), "question_id": base.get("question_id"),
                "item_id": base.get("item_id"),
                "detail": base.get("detail") or {"question": base.get("question"), "choices": []},
                "choices": [], "support": True, "reason": receipt.get("reason"),
                "result": receipt.get("result"), "native_ref": base.get("native_ref")}

    if source == "S2" and entry and entry.get("action_id"):
        receipt = _workflow_gate_receipt(base, entry)
        return {"state": receipt["state"], "kind": "question",
                "request_id": base.get("request_id"), "turn_id": None,
                "route_id": base.get("route_id"), "question_id": base.get("question_id"),
                "gate_epoch": base.get("gate_epoch"), "detail": base.get("detail"),
                "choices": [], "support": True, "reason": receipt.get("reason"),
                "result": receipt.get("result"), "native_ref": base.get("native_ref")}

    match, error = _workflow_current_item(base.get("id"), source)
    if match is None:
        return {"state": "unknown", "reason": error}
    jobs, item = match
    if not _workflow_item_matches(base, item):
        return {"state": "unknown", "reason": "workflow-selected-binding-changed"}
    if source == "S2":
        binding, error = _workflow_gate_source(jobs, item, allow_missing_artifact=True)
        if binding is None:
            return {"state": "unknown", "reason": error}
        native_ref = {"transport": "hearting-human-gate", "jobs_path": str(jobs),
                      "source_id": item["id"], "route_id": item.get("route_id"),
                      "route_hash": item.get("route_hash"), "gate_id": item.get("gate_id"),
                      "attempt_id": item.get("attempt_id"), "route_path": binding["route_path"],
                      "artifact_sha256": binding["artifact_sha256"],
                      "gate_epoch": binding["gate_epoch"],
                      "raised_at": float(item.get("since"))}
        if base.get("native_ref") != native_ref:
            return {"state": "unknown", "reason": "workflow-gate-binding-changed"}
        detail, detail_error = _workflow_gate_detail(binding, item)
        return {"state": "open", "kind": "question", "request_id": base["request_id"],
                "turn_id": None, "route_id": item.get("route_id"),
                "question_id": item.get("gate_id"), "gate_epoch": binding["gate_epoch"],
                "detail": detail.get("detail") if detail else None,
                "choices": detail.get("choices", []) if detail else [],
                "support": bool(detail) and not detail_error, "reason": detail_error,
                "native_ref": native_ref}

    snapshot, error = _workflow_owner_snapshot(base, expected_native_ref=base.get("native_ref"))
    if snapshot is None:
        return {"state": "unknown", "reason": error}
    support = bool(snapshot.get("support"))
    return {"state": "open", "kind": "question", "request_id": base["request_id"],
            "turn_id": base["turn_id"], "route_id": item.get("route_id"),
            "question_id": "owner-correction", "item_id": item.get("attempt_id"),
            "detail": {"question": item.get("question"), "choices": []}, "choices": [],
            "support": support,
            "reason": snapshot.get("reason") or (None if support else "owner-correction-surface-unavailable"),
            "native_ref": snapshot["native_ref"]}


def read_selected_request(ref):
    """Read one selected current native request; details never enter shared projections."""
    base, reason = _selected_base(ref)
    if base is None:
        # An expired/temporarily missing projection cannot authorize a reply,
        # but the caller's exact selected identity can still recover its draft.
        return _selected_fallback(ref if _selected_identity(ref) is not None else None, reason)
    requested_id = base.get("request_id")
    resolved = _selected_transport(base, requested_id=requested_id)
    if not isinstance(resolved, dict):
        return _selected_fallback(base, "native-request-unavailable")
    resolved_id = _safe_request_id(resolved.get("request_id"))
    resolved_turn = _safe_native_ref(resolved.get("turn_id"))
    resolved_base = dict(base)
    if resolved_id is not None:
        resolved_base["request_id"] = resolved_id
    if resolved_turn:
        resolved_base["turn_id"] = resolved_turn
    if (base.get("request_id") is not None and resolved_id is not None
            and base["request_id"] != resolved_id):
        return _selected_fallback(resolved_base, "native-request-binding-changed")
    if (base.get("turn_id") is not None and resolved_turn is not None
            and base["turn_id"] != resolved_turn):
        return _selected_fallback(resolved_base, "native-turn-binding-changed")
    old_native_ref = base.get("native_ref")
    new_native_ref = resolved.get("native_ref")
    if old_native_ref is not None and old_native_ref != new_native_ref:
        return _selected_fallback(resolved_base, "native-transport-incarnation-changed")
    if (base.get("route_id") is not None and resolved.get("route_id") is not None
            and base["route_id"] != resolved.get("route_id")):
        return _selected_fallback(resolved_base, "native-route-binding-changed")
    envelope = _selected_envelope(resolved_base, resolved)
    if envelope["state"] in {"submitted", "received", "applied", "cancelled", "expired"}:
        identity = _selected_identity(envelope)
        if identity is not None:
            previous = _selected_entry(envelope) or {"draft": "", "state": "open"}
            if previous.get("state") != envelope["state"]:
                previous["state"] = envelope["state"]
                if envelope["state"] == "applied":
                    previous["draft"] = ""
                receipt = resolved.get("result")
                source = ({"S3": "hearting-owner-input", "S2": "hearting-human-gate"}
                          .get(base.get("source")) or
                          ("opencode-http-v2" if base.get("harness") == "opencode"
                           else "codex-managed-gateway"))
                previous["result"] = {"source": source, **receipt} if isinstance(receipt, dict) else {
                    "source": source, "status": envelope["state"]}
                if _store_selected_entry(envelope, previous):
                    stored = _selected_entry(envelope)
                    if stored:
                        envelope["state"] = stored.get("state", envelope["state"])
                        envelope["result"] = stored.get("result", envelope.get("result"))
                        envelope["draft"] = stored.get("draft", envelope.get("draft", ""))
    return envelope


def write_selected_draft(ref, draft):
    """Persist only a selected request's private draft in the owner-only cache overlay."""
    base, reason = _selected_base(ref)
    if base is None:
        return _selected_fallback(ref if _selected_identity(ref) is not None else None, reason)
    if isinstance(ref, dict) and ref.get("state") in {"applied", "cancelled", "expired"}:
        return dict(ref)
    identity = _selected_identity(ref) if isinstance(ref, dict) else None
    if (identity is None or identity.get("harness") != base["harness"]
            or identity.get("session_id") != base["session_id"]
            or identity.get("route_id") != base.get("route_id")):
        return _selected_fallback(base, "exact-selected-request-binding-required")
    if not isinstance(draft, str) or len(draft.encode("utf-8")) > _SELECTED_DRAFT_MAX:
        return _selected_fallback(base, "selected-draft-invalid")
    envelope = dict(ref)
    envelope["selected_id"] = base["id"]
    previous = _selected_entry(envelope)
    if previous and (previous.get("state") in {"submitted", "received", "applied",
                                                "cancelled", "expired"}
                     or (previous.get("state") == "unknown" and previous.get("action_id"))):
        return _selected_fallback(base, "selected-response-already-settled-or-ambiguous",
                                  state=previous["state"])
    entry = previous or {"draft": "", "state": "open"}
    draft_state = "unknown" if ref.get("state") == "unknown" else "open"
    entry["draft"] = draft
    entry["state"] = draft_state
    if not _store_selected_entry(envelope, entry):
        return _selected_fallback(base, "selected-draft-storage-unavailable")
    updated = dict(ref)
    updated.update({"selected_id": base["id"], "draft": draft, "state": draft_state})
    return updated


def _opencode_form_active(question, native_answers):
    for condition in question.get("when", []):
        value = native_answers.get(condition["key"])
        if value is None:
            return False
        hit = (any(item == condition["value"] for item in value)
               if isinstance(value, list) else value == condition["value"])
        if (condition["op"] == "eq" and not hit) or (condition["op"] == "neq" and hit):
            return False
    return True


def _opencode_form_answers(envelope, answers):
    """Validate portable field answers and return the exact native typed answer map."""
    detail = envelope.get("detail") if isinstance(envelope, dict) else None
    native_ref = envelope.get("native_ref") if isinstance(envelope, dict) else None
    questions = detail.get("questions") if isinstance(detail, dict) else None
    if (not isinstance(native_ref, dict) or native_ref.get("transport") != "opencode-http-v2"
            or not isinstance(questions, list) or not questions
            or not isinstance(answers, dict)):
        return None, "opencode-form-schema-unavailable"
    field_keys = [question.get("id") for question in questions
                  if isinstance(question, dict)]
    if (len(field_keys) != len(questions) or not field_keys
            or len(set(field_keys)) != len(field_keys)
            or native_ref.get("field_keys") != field_keys
            or not set(answers).issubset(set(field_keys))):
        return None, "opencode-form-answer-keys-invalid"

    result, native_answers = {}, {}
    for question in questions:
        key = question["id"]
        active = _opencode_form_active(question, native_answers)
        supplied = key in answers
        if not active:
            if supplied:
                return None, "opencode-form-inactive-field-answer"
            continue
        if question.get("hidden") is True:
            if supplied:
                return None, "opencode-form-hidden-field-answer"
            if question.get("required") is True:
                return None, "opencode-form-hidden-required-field-unsupported"
            continue
        if not supplied:
            if question.get("required") is True:
                return None, "opencode-form-required-answer-missing"
            continue
        values = answers[key]
        if not isinstance(values, list):
            return None, "opencode-form-answer-shape-invalid"
        field_type = question.get("field_type")
        required = question.get("required") is True
        option_values = question.get("option_values", [])
        custom = question.get("custom") is True

        if field_type == "string":
            if len(values) != 1:
                return None, "opencode-string-answer-count-invalid"
            if not isinstance(values[0], str):
                return None, "opencode-form-string-answer-invalid"
            value = values[0]
            if not value and not required:
                continue
            if not value:
                return None, "opencode-form-required-answer-empty"
            try:
                length = len(value.encode("utf-16-le")) // 2
            except UnicodeEncodeError:
                return None, "opencode-form-string-answer-invalid"
            minimum, maximum = question.get("min_length"), question.get("max_length")
            if ((minimum is not None and length < minimum)
                    or (maximum is not None and length > maximum)):
                return None, "opencode-form-string-answer-length-invalid"
            if question.get("options_present") and not custom and value not in option_values:
                return None, "opencode-answer-not-in-current-options"
            native_value = value

        elif field_type in {"number", "integer"}:
            if len(values) != 1:
                return None, "opencode-form-numeric-answer-invalid"
            raw = values[0]
            if isinstance(raw, str) and not raw.strip() and not required:
                continue
            numeric = _opencode_form_decimal(raw)
            if numeric is None:
                return None, "opencode-form-numeric-answer-invalid"
            minimum = _opencode_form_decimal(question.get("minimum"), allow_infinity=True)
            maximum = _opencode_form_decimal(question.get("maximum"), allow_infinity=True)
            if ((question.get("minimum") is not None and minimum is None)
                    or (question.get("maximum") is not None and maximum is None)):
                return None, "opencode-form-numeric-bound-invalid"
            if ((minimum is not None and numeric < minimum)
                    or (maximum is not None and numeric > maximum)):
                return None, "opencode-form-numeric-answer-out-of-bounds"
            if field_type == "integer":
                if numeric != numeric.to_integral_value():
                    return None, "opencode-form-integer-answer-invalid"
                if not -9007199254740991 <= numeric <= 9007199254740991:
                    return None, "opencode-form-integer-answer-unsafe"
                native_value = int(numeric)
            else:
                native_value = float(numeric)
                if (not math.isfinite(native_value)
                        or Decimal(str(native_value)) != numeric):
                    return None, "opencode-form-number-answer-unsafe"

        elif field_type == "boolean":
            if len(values) != 1 or type(values[0]) is not bool:
                return None, "opencode-form-boolean-answer-explicit-required"
            native_value = values[0]

        elif field_type == "multiselect":
            if any(not isinstance(value, str) for value in values):
                return None, "opencode-form-multiselect-answer-invalid"
            if not values and not required and question.get("min_items") in (None, 0):
                continue
            minimum, maximum = question.get("min_items"), question.get("max_items")
            if ((minimum is not None and len(values) < minimum)
                    or (maximum is not None and len(values) > maximum)):
                return None, "opencode-answer-count-invalid"
            if len(set(values)) != len(values):
                return None, "opencode-answer-duplicate-option"
            if not custom and any(value not in option_values for value in values):
                return None, "opencode-answer-not-in-current-options"
            native_value = list(values)

        else:
            return None, "opencode-form-type-unsupported"
        result[key] = native_value
        native_answers[key] = native_value

    return result, None


def _opencode_turnless_form_binding(envelope):
    if (not isinstance(envelope, dict) or envelope.get("harness") != "opencode"
            or envelope.get("kind") != "question" or envelope.get("turn_id") is not None):
        return False
    native_ref = envelope.get("native_ref")
    detail = envelope.get("detail")
    questions = detail.get("questions") if isinstance(detail, dict) else None
    if (not isinstance(native_ref, dict) or native_ref.get("transport") != "opencode-http-v2"
            or native_ref.get("session_id") != envelope.get("session_id")
            or native_ref.get("request_id") != envelope.get("request_id")
            or not isinstance(native_ref.get("generation"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", native_ref["generation"])
            or not isinstance(native_ref.get("schema_binding"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", native_ref["schema_binding"])
            or type(native_ref.get("server_pid")) is not int or native_ref["server_pid"] < 1
            or not _safe_native_ref(native_ref.get("proc_start"))
            or not isinstance(native_ref.get("field_keys"), list)
            or not isinstance(questions, list) or not questions):
        return False
    keys = [question.get("id") for question in questions if isinstance(question, dict)]
    return (len(keys) == len(questions) and len(set(keys)) == len(keys)
            and keys == native_ref["field_keys"]
            and _safe_request_id(envelope.get("request_id")) is not None
            and _safe_native_ref(envelope.get("session_id")) is not None)


def _portable_response(envelope, response):
    if not isinstance(response, dict):
        return None, "response-shape-invalid"
    if envelope.get("kind") == "approval":
        if set(response) != {"decision"} or response.get("decision") not in {"approve", "reject", "defer"}:
            return None, "approval-decision-required"
        return {"decision": response["decision"]}, None
    if envelope.get("kind") != "question" or set(response) != {"answers"}:
        return None, "question-answer-required"
    answers = response.get("answers")
    detail = envelope.get("detail")
    questions = detail.get("questions") if isinstance(detail, dict) else None
    native_ref = envelope.get("native_ref")
    if (envelope.get("harness") == "opencode" and isinstance(native_ref, dict)
            and native_ref.get("transport") == "opencode-http-v2"):
        if not isinstance(questions, list) or not questions:
            return None, "opencode-form-schema-unavailable"
        question_by_id = {question.get("id"): question for question in questions
                          if isinstance(question, dict)}
        if (len(question_by_id) != len(questions) or not isinstance(answers, dict)
                or not set(answers).issubset(set(question_by_id))):
            return None, "opencode-form-answer-keys-invalid"
        normalized = {}
        for question_id, values in answers.items():
            question = question_by_id[question_id]
            if not isinstance(values, list):
                return None, "opencode-form-answer-shape-invalid"
            field_type = question.get("field_type")
            if field_type in {"string", "multiselect"}:
                valid = all(isinstance(value, str)
                            and len(value.encode("utf-8")) <= _SELECTED_DRAFT_MAX
                            for value in values)
            elif field_type in {"number", "integer"}:
                valid = all(not isinstance(value, bool)
                            and isinstance(value, (int, float, str))
                            and (not isinstance(value, float) or math.isfinite(value))
                            and (not isinstance(value, str)
                                 or len(value.encode("utf-8")) <= _SELECTED_DRAFT_MAX)
                            for value in values)
            elif field_type == "boolean":
                if not all(type(value) is bool for value in values):
                    return None, "opencode-form-boolean-answer-explicit-required"
                valid = True
            else:
                valid = False
            if not valid:
                return None, "opencode-form-answer-value-invalid"
            normalized[question_id] = list(values)
        try:
            if len(json.dumps(normalized, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 16_384:
                return None, "opencode-form-answer-too-large"
        except (TypeError, ValueError):
            return None, "opencode-form-answer-value-invalid"
        return {"answers": normalized}, None
    if isinstance(questions, list) and questions:
        question_ids = [_safe_native_ref(question.get("id"))
                        if isinstance(question, dict) else None for question in questions]
        if any(question_id is None for question_id in question_ids) \
                or len(set(question_ids)) != len(question_ids):
            return None, "native-question-identities-invalid"
    else:
        question_id = _safe_native_ref(envelope.get("question_id"))
        question_ids = [question_id] if question_id is not None else []
    if not isinstance(answers, dict) or not question_ids or set(answers) != set(question_ids):
        return None, "nonempty-exact-question-answer-required"
    normalized = {}
    for question_id in question_ids:
        values = answers.get(question_id)
        if (not isinstance(values, list) or not values
                or any(not isinstance(value, str) or not value.strip()
                       or len(value.encode("utf-8")) > _SELECTED_DRAFT_MAX for value in values)):
            return None, "nonempty-exact-question-answer-required"
        normalized[question_id] = list(values)
    return {"answers": normalized}, None


def _native_response_error(envelope, portable):
    """Check a portable response against the exact selected native request shape."""
    native_ref = envelope.get("native_ref")
    detail = envelope.get("detail")
    if (envelope.get("harness") == "opencode" and envelope.get("kind") == "question"
            and isinstance(native_ref, dict)
            and native_ref.get("transport") == "opencode-http-v2"):
        return _opencode_form_answers(envelope, portable.get("answers"))[1]
    if isinstance(native_ref, dict) and native_ref.get("transport") == "hearting-owner-input":
        return None
    if isinstance(native_ref, dict) and native_ref.get("transport") == "hearting-human-gate":
        if (envelope.get("source") != "S2" or type(envelope.get("gate_epoch")) is not int
                or envelope.get("gate_epoch") != native_ref.get("gate_epoch")
                or not isinstance(detail, dict)):
            return "workflow-gate-binding-invalid"
        questions = detail.get("questions")
        if not isinstance(questions, list) or not questions:
            return "workflow-gate-questions-unavailable"
        answers = portable.get("answers") if isinstance(portable, dict) else None
        if not isinstance(answers, dict) or set(answers) != {row.get("id") for row in questions}:
            return "workflow-gate-answer-set-incomplete"
        for question in questions:
            if not isinstance(question, dict):
                return "workflow-gate-question-invalid"
            values = answers.get(question.get("id"))
            if not isinstance(values, list) or len(values) != 1:
                return "workflow-gate-answer-count-invalid"
            choices = question.get("choices")
            allowed = {choice.get("value", choice.get("label")) for choice in choices
                       if isinstance(choice, dict)} if isinstance(choices, list) else set()
            if values[0] not in allowed and question.get("is_other") is not True:
                return "workflow-gate-answer-not-current-option"
        return None
    if isinstance(native_ref, dict) and native_ref.get("transport") == "hearting-answer-marker":
        return "workflow-response-surface-unavailable"
    if envelope.get("kind") == "approval":
        decision = portable.get("decision")
        if decision == "defer":
            return None
        request_ref = native_ref.get("request") if isinstance(native_ref, dict) else None
        if (isinstance(request_ref, dict)
                and request_ref.get("method") == "item/commandExecution/requestApproval"):
            available = detail.get("availableDecisions") if isinstance(detail, dict) else None
            native_decision = "accept" if decision == "approve" else "decline"
            if available is not None and (
                    not isinstance(available, list) or native_decision not in available):
                return "codex-command-decision-not-available"
        if decision not in _selected_actions("approval", detail, native_ref):
            return "native-approval-decision-not-available"
        return None

    answers = portable["answers"]
    harness = envelope.get("harness")
    if harness == "codex" and isinstance(detail, dict):
        questions = detail.get("questions")
        if not isinstance(questions, list):
            question_id = envelope.get("question_id")
            questions = [{"id": question_id, "choices": envelope.get("choices")
                          if isinstance(envelope.get("choices"), list) else None,
                          "is_other": False}]
        for question in questions:
            if not isinstance(question, dict):
                return "codex-question-schema-invalid"
            question_id = question.get("id")
            values = answers.get(question_id, [])
            choices = question.get("choices", question.get("options"))
            if (isinstance(choices, list) and not question.get("is_other")
                    and any(value not in {
                        option.get("value", option.get("label"))
                        for option in choices if isinstance(option, dict)
                    } for value in values)):
                return "codex-answer-not-in-current-options"
    return None


def _workflow_gate_submit(envelope, portable, action_id):
    """Submit a current person's explicit S2 decision through the normal release CLI."""
    base, error = _selected_base(envelope)
    if base is None:
        return "unknown", {"reason": error}
    match, error = _workflow_current_item(base.get("id"), "S2")
    if match is None:
        return "unknown", {"reason": error}
    jobs, item = match
    if not _workflow_item_matches(base, item):
        return "unknown", {"reason": "workflow-gate-binding-changed"}
    binding, error = _workflow_gate_source(jobs, item)
    if binding is None:
        return "unknown", {"reason": error}
    current, error = _workflow_gate_detail(binding, item)
    native = envelope.get("native_ref")
    expected_native = {"transport": "hearting-human-gate", "jobs_path": str(jobs),
                       "source_id": item["id"], "route_id": item.get("route_id"),
                       "route_hash": item.get("route_hash"), "gate_id": item.get("gate_id"),
                       "attempt_id": item.get("attempt_id"), "route_path": binding["route_path"],
                       "artifact_sha256": binding["artifact_sha256"],
                       "gate_epoch": binding["gate_epoch"],
                       "raised_at": float(item.get("since"))}
    if (current is None or error or native != expected_native
            or envelope.get("gate_epoch") != binding["gate_epoch"]
            or current.get("detail") != envelope.get("detail")):
        return "unknown", {"reason": error or "workflow-gate-detail-changed"}
    answers = portable.get("answers") if isinstance(portable, dict) else None
    mode = current["detail"].get("response_mode")
    answer_file = None
    temp_dir = None
    if mode == "gate-decision":
        decision = answers.get("gate-decision", [None])[0]
        if decision not in {"proceed", "revise", "stop"}:
            return "unknown", {"reason": "workflow-gate-decision-invalid"}
    elif mode == "frame-interview":
        import frame_interview as FI

        asked = []
        for question in current["detail"].get("questions", ()):
            values = answers.get(question.get("id"))
            if not isinstance(values, list) or len(values) != 1:
                return "unknown", {"reason": "workflow-interview-answer-incomplete"}
            options = [choice.get("label", choice.get("value"))
                       for choice in question.get("choices", ()) if isinstance(choice, dict)]
            asked.append({"question": question.get("question"), "options": options,
                          "answer": values[0]})
        answers_file_value = FI.answers_from_native(current["interview"], asked)
        if (not isinstance(answers_file_value, dict)
                or FI.validate_answers(current["interview"], answers_file_value)):
            return "unknown", {"reason": "workflow-interview-answer-invalid"}
        decision = "proceed"
        try:
            temp_dir = tempfile.TemporaryDirectory(prefix="hearting-s2-", dir="/tmp")
            answer_file = Path(temp_dir.name) / "answers.json"
            fd = os.open(answer_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(answers_file_value, handle, ensure_ascii=False,
                              separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
        except Exception as exc:
            if temp_dir is not None:
                temp_dir.cleanup()
            return "unknown", {"reason": "workflow-answer-file-unavailable:" + type(exc).__name__}
    else:
        return "unknown", {"reason": "workflow-gate-response-mode-unsupported"}

    command = [sys.executable, str(Path(__file__).resolve().parent / "workflow-supervisor.py"),
               "release", "--route", binding["route_path"], "--gate", str(item["gate_id"]),
               "--decision", decision, "--jobs", str(jobs),
               "--expected-route-hash", str(item["route_hash"]),
               "--expected-gate-epoch", str(binding["gate_epoch"]),
               "--request-id", action_id]
    if answer_file is not None:
        command.extend(("--answers", str(answer_file)))
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=120, check=False)
        if completed.returncode != 0:
            return "unknown", {"reason": "workflow-release-command-refused",
                                "returncode": completed.returncode}
        try:
            receipt = json.loads(completed.stdout)
        except (TypeError, ValueError):
            return "unknown", {"reason": "workflow-release-receipt-invalid"}
        required = {"route_id": item["route_id"], "route_hash": item["route_hash"],
                    "gate": item["gate_id"], "gate_epoch": binding["gate_epoch"],
                    "request_id": action_id, "decision": decision, "actor_kind": "user"}
        if not isinstance(receipt, dict) or any(receipt.get(key) != value
                                                for key, value in required.items()):
            return "unknown", {"reason": "workflow-release-receipt-mismatch"}
        verified = _workflow_gate_receipt(
            {**base, "native_ref": expected_native},
            {"action_id": action_id, "result": receipt})
        if verified["state"] != "applied":
            return "unknown", {"reason": verified.get("reason") or
                                "workflow-release-source-transition-unconfirmed"}
        return "applied", receipt
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "unknown", {"reason": "workflow-release-command-ambiguous:" + type(exc).__name__}
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def _workflow_owner_submit(envelope, portable, action_id):
    """Use the public exact-owner correction command once and read its durable receipt."""
    base, error = _selected_base(envelope)
    if base is None:
        return "unknown", {"reason": error}
    snapshot, error = _workflow_owner_snapshot(
        base, expected_native_ref=envelope.get("native_ref"))
    if snapshot is None:
        return "unknown", {"reason": error}
    if not snapshot.get("support"):
        return "unknown", {"reason": snapshot.get("reason") or "owner-correction-unavailable"}
    answers = portable.get("answers") if isinstance(portable, dict) else None
    values = answers.get("owner-correction") if isinstance(answers, dict) else None
    if not isinstance(values, list) or len(values) != 1:
        return "unknown", {"reason": "owner-correction-answer-shape-invalid"}
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", action_id):
        return "unknown", {"reason": "owner-correction-action-id-invalid"}
    attempt_id = envelope["native_ref"]["attempt_id"]
    lineage = _workflow_owner_lineage(snapshot["jobs"], envelope.get("native_ref"))
    if (not lineage or snapshot.get("current_attempt_id") != lineage["effective_attempt"]):
        return "unknown", {"reason": "owner-correction-lineage-changed"}
    temp_dir = None
    try:
        temp_dir = tempfile.TemporaryDirectory(prefix="hearting-owner-correction-")
        fd, message_path = tempfile.mkstemp(prefix="answer-", suffix=".txt", dir=temp_dir.name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(values[0])
            handle.flush()
            os.fsync(handle.fileno())
        command = [sys.executable, str(Path(__file__).resolve().with_name("capability-route.py")),
                   "correct", "--attempt-id", attempt_id, "--jobs", str(snapshot["jobs"]),
                   "--message-file", message_path, "--request-id", action_id]
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=30, check=False)
        if completed.returncode != 0:
            return "unknown", {"reason": "owner-correction-command-unconfirmed",
                                "returncode": completed.returncode}
        receipts = []
        for line in (completed.stdout or "").splitlines():
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                receipts.append(value)
        if len(receipts) != 1:
            return "unknown", {"reason": "owner-correction-command-receipt-ambiguous"}
        public_receipt = receipts[0]
        correction = public_receipt.get("correction", public_receipt)
        if not isinstance(correction, dict):
            return "unknown", {"reason": "owner-correction-request-receipt-missing"}
        redirected_from = correction.get("redirected_from", public_receipt.get("redirected_from"))
        if redirected_from is not None and redirected_from != attempt_id:
            return "unknown", {"reason": "owner-correction-selection-redirected"}
        if (correction.get("request_id") != action_id
                or not isinstance(correction.get("attempt_id"), str)):
            return "unknown", {"reason": "owner-correction-request-binding-missing"}
        request = next((row for row in correction.get("requests", ())
                        if isinstance(row, dict) and row.get("id") == action_id), None)
        if request is None:
            return "unknown", {"reason": "owner-correction-request-receipt-missing"}
        target_attempt = correction["attempt_id"]
        lineage = _workflow_owner_lineage(snapshot["jobs"], envelope.get("native_ref"))
        if not lineage or target_attempt not in lineage["attempts"]:
            return "unknown", {"reason": "owner-correction-target-outside-lineage"}
        import dispatch_owner_input
        try:
            observed = dispatch_owner_input.inspect(snapshot["jobs"], target_attempt)
        except Exception as exc:
            return "unknown", {"reason": "owner-correction-receipt-unavailable:" +
                                type(exc).__name__}
        request = next((row for row in observed.get("requests", ())
                        if isinstance(row, dict) and row.get("id") == action_id), None)
        if request is None:
            return "unknown", {"reason": "owner-correction-receipt-missing"}
        request_state = request.get("state")
        if request_state == "turn-completed":
            state = "applied"
        elif request_state in {"retained", "queued", "accepted"}:
            state = "received"
        else:
            state = "unknown"
        result = {"status": request_state, "request_id": action_id,
                  "target_attempt_id": target_attempt,
                  "duplicate": bool(correction.get("duplicate"))}
        continuation = {key: public_receipt[key] for key in
                        ("state", "reason", "required_action", "owner_attempt_id", "parent_next")
                        if isinstance(public_receipt.get(key), (str, int, float, bool))}
        if continuation:
            result["continuation"] = continuation
        if request.get("delivery_observation"):
            result["delivery_observation"] = request["delivery_observation"]
        return state, result
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        return "unknown", {"reason": "owner-correction-command-ambiguous:" + type(exc).__name__}
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def _selected_invalid_response(envelope, reason):
    """Keep a known local validation failure editable without claiming the action."""
    value = dict(envelope)
    value.update({"state": "open", "reason": reason,
                  "result": {"status": "invalid-local-response", "reason": reason}})
    value.setdefault("draft", "")
    value["actions"] = list(envelope.get("actions", ()))
    return value


def _opencode_submit(envelope, portable):
    base, error = _selected_base(envelope)
    if base is None:
        return "unknown", {"reason": error}
    native_ref = envelope.get("native_ref")
    if not isinstance(native_ref, dict) or native_ref.get("transport") != "opencode-http-v2":
        return "unknown", {"reason": "opencode-native-reference-required"}
    current = _opencode_current_selected(base, requested_id=envelope.get("request_id"))
    if (current.get("state") != "open" or not current.get("support")
            or current.get("native_ref") != native_ref
            or current.get("request_id") != envelope.get("request_id")
            or current.get("turn_id") != envelope.get("turn_id")):
        return "unknown", {"reason": current.get("reason") or "opencode-request-binding-changed"}
    endpoint = _opencode_endpoint(base["session_id"])
    if endpoint is None or endpoint.get("generation") != native_ref.get("generation"):
        return "unknown", {"reason": "opencode-server-replaced"}
    session_path = urllib.parse.quote(base["session_id"], safe="")
    request_path = urllib.parse.quote(envelope["request_id"], safe="")
    if envelope["kind"] == "approval":
        decision = "once" if portable["decision"] == "approve" else "reject"
        reply_path = f"/api/session/{session_path}/permission/{request_path}/reply"
        result = _opencode_http(endpoint, "POST", reply_path, {"decision": decision})
        if result.get("status") != 204:
            return "unknown", {"reason": "opencode-permission-response-ambiguous"}
        if not _opencode_identity_matches(endpoint, base["session_id"]):
            return "unknown", {"reason": "opencode-server-replaced-after-response"}
        # HTTP success acknowledges receipt, not permission consumption or
        # subsequent execution. Preserve the draft until application is known.
        return "received", {"transport": "received", "runtime": "unknown", "http_status": 204}
    answer_value, validation_error = _opencode_form_answers(
        envelope, portable.get("answers") if isinstance(portable, dict) else None)
    if validation_error:
        return "unknown", {"reason": validation_error}
    reply_path = f"/api/session/{session_path}/form/{request_path}/reply"
    result = _opencode_http(endpoint, "POST", reply_path,
                            {"answer": answer_value})
    if result.get("status") != 204:
        return "unknown", {"reason": "opencode-form-response-ambiguous"}
    confirmation = _opencode_http(endpoint, "GET",
                                  f"/api/session/{session_path}/form/{request_path}")
    detail = _opencode_data(confirmation)
    if not _opencode_identity_matches(endpoint, base["session_id"]):
        return "unknown", {"reason": "opencode-server-replaced-after-response"}
    if (confirmation.get("status") == 200 and isinstance(detail, dict)
            and detail.get("id") == envelope["request_id"]
            and detail.get("sessionID") == base["session_id"]
            and isinstance(detail.get("state"), dict)
            and detail["state"].get("status") == "answered"):
        return "applied", {"transport": "received", "runtime": "answered", "progress": "unknown",
                           "http_status": 204}
    if (confirmation.get("status") == 200 and isinstance(detail, dict)
            and detail.get("id") == envelope["request_id"]
            and detail.get("sessionID") == base["session_id"]
            and isinstance(detail.get("state"), dict)
            and detail["state"].get("status") == "cancelled"):
        return "cancelled", {"reason": "opencode-form-cancelled"}
    return "received", {"transport": "received", "http_status": 204}


def _codex_submit(envelope, portable, action_id):
    base, error = _selected_base(envelope)
    if base is None:
        return "unknown", {"reason": error}
    native_ref = envelope.get("native_ref")
    if (not isinstance(native_ref, dict) or native_ref.get("transport") != "codex-managed-control"
            or not isinstance(native_ref.get("request"), dict)):
        return "unknown", {"reason": "codex-native-reference-required"}
    current = _codex_current_selected(base, requested_id=envelope.get("request_id"))
    if (current.get("state") != "open" or not current.get("support")
            or current.get("native_ref") != native_ref
            or current.get("request_id") != envelope.get("request_id")
            or current.get("turn_id") != envelope.get("turn_id")):
        return "unknown", {"reason": current.get("reason") or "codex-request-binding-changed"}
    gateway = current.get("gateway")
    if (not isinstance(gateway, dict) or gateway.get("pid") != native_ref.get("pid")
            or gateway.get("proc_start") != native_ref.get("proc_start")
            or gateway.get("path") != native_ref.get("control_path")
            or gateway.get("epoch") != native_ref.get("epoch")):
        return "unknown", {"reason": "codex-gateway-incarnation-changed"}
    result = _codex_control_request(gateway["path"], {
        "schema_version": 1, "op": "interaction-reply",
        "ref": native_ref["request"], "response": portable, "action_id": action_id})
    if (not result or result.get("action_id") != action_id
            or result.get("request") != native_ref["request"]):
        return "unknown", {"reason": "codex-response-result-ambiguous"}
    state = result.get("status")
    if state not in _SELECTED_STATES:
        return "unknown", {"reason": "codex-response-status-unknown"}
    response_result = {"action_id": action_id, "transport": state}
    if isinstance(result.get("reason"), str):
        response_result["reason"] = result["reason"]
    return state, response_result


def submit_selected_response(ref, response, *, action_id=None):
    """Submit one explicit response to the exact live request; ambiguous delivery never retries."""
    base, reason = _selected_base(ref)
    if base is None:
        return _selected_fallback(ref if _selected_identity(ref) is not None else None, reason)
    if isinstance(ref, dict) and ref.get("state") in {"applied", "cancelled", "expired"}:
        return dict(ref)
    identity = _selected_identity(ref) if isinstance(ref, dict) else None
    if (identity is None or identity.get("harness") != base["harness"]
            or identity.get("session_id") != base["session_id"]
            or identity.get("route_id") != base.get("route_id")):
        return _selected_fallback(base, "exact-request-turn-binding-required")
    current = read_selected_request(ref)
    current_identity = _selected_identity(current)
    if (current.get("state") != "open" or current.get("support", {}).get("supported") is not True
            or current_identity != identity
            or current.get("native_ref") != ref.get("native_ref")):
        return current
    native_ref = current.get("native_ref")
    workflow_owner_request = (
        isinstance(native_ref, dict)
        and native_ref.get("transport") == "hearting-owner-input"
        and identity.get("turn_id") is None
        and identity.get("item_id") == native_ref.get("attempt_id")
        and identity.get("route_id") == native_ref.get("route_id"))
    workflow_gate_request = (
        isinstance(native_ref, dict)
        and native_ref.get("transport") == "hearting-human-gate"
        and identity.get("turn_id") is None
        and identity.get("gate_epoch") == native_ref.get("gate_epoch")
        and identity.get("route_id") == native_ref.get("route_id"))
    opencode_turnless_form = (identity.get("turn_id") is None
                              and _opencode_turnless_form_binding(current))
    if (identity.get("turn_id") is None and not workflow_owner_request
            and not workflow_gate_request and not opencode_turnless_form):
        return _selected_fallback(base, "exact-request-turn-binding-required")
    portable, response_error = _portable_response(current, response)
    if portable is None:
        return _selected_invalid_response(current, response_error)
    if current["kind"] == "approval" and portable["decision"] == "defer":
        prior = _selected_entry(current)
        entry = prior or {"draft": current.get("draft", ""), "state": "open"}
        entry["response"] = {"decision": "defer"}
        entry["result"] = {"decision": "defer", "status": "no-response-sent"}
        entry["state"] = "open"
        if not _store_selected_entry(current, entry):
            return _selected_fallback(base, "selected-response-storage-unavailable")
        returned = dict(current)
        returned["state"] = "open"
        returned["result"] = entry["result"]
        return returned
    if action_id is None:
        action_id = uuid.uuid4().hex
    if not isinstance(action_id, str) or not _SAFE.fullmatch(action_id):
        return _selected_invalid_response(current, "action-id-invalid")
    native_ref = current.get("native_ref")
    if (isinstance(native_ref, dict)
            and native_ref.get("transport") == "hearting-owner-input"
            and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", action_id)):
        return _selected_invalid_response(current, "owner-correction-action-id-invalid")
    response_error = _native_response_error(current, portable)
    if response_error:
        return _selected_invalid_response(current, response_error)
    claimed, entry = _claim_selected_action(current, action_id, portable)
    if not claimed:
        return read_selected_request(current)
    if (isinstance(native_ref, dict)
            and native_ref.get("transport") == "hearting-owner-input"):
        state, result = _workflow_owner_submit(current, portable, action_id)
    elif (isinstance(native_ref, dict)
          and native_ref.get("transport") == "hearting-human-gate"):
        state, result = _workflow_gate_submit(current, portable, action_id)
    elif current["harness"] == "opencode":
        state, result = _opencode_submit(current, portable)
    elif current["harness"] == "codex":
        state, result = _codex_submit(current, portable, action_id)
    else:
        state, result = "unknown", {"reason": "claude-native-response-surface-unavailable"}
    entry["state"] = state
    entry["result"] = result
    if state == "applied":
        entry["draft"] = ""
    if not _store_selected_entry(current, entry):
        state, result = "unknown", {"reason": "response-result-storage-unavailable"}
    returned = dict(current)
    returned.update({"state": state, "action_id": action_id, "result": result,
                     "actions": [] if state != "open" else current.get("actions", [])})
    if state == "applied":
        returned["draft"] = ""
    return returned


def cached_count(harness: str, session_id: str, *, now: float | None = None,
                 max_age: float = _DISPLAY_TTL) -> tuple[int, int] | None:
    """Return (global current total, exact session count) from a display-valid cache."""
    value = _read_cache(now=now, max_age=max_age)
    if value is None:
        if max_age == _DISPLAY_TTL:
            _request_producer()
        return None
    current = time.time() if now is None else float(now)
    if max_age == _DISPLAY_TTL and current - float(value["observed_at"]) > _REQUEST_TTL:
        _request_producer()
    key = str(harness) + "\0" + str(session_id)
    counts = value.get("by_session") if isinstance(value.get("by_session"), dict) else {}
    key = str(harness) + "\0" + str(session_id)
    total = int(value.get("total", 0)) if value.get("complete") else -1
    return total, len(counts.get(key, ()))


def _request_producer():
    root = state_root()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_meta = os.lstat(root)
        if not stat.S_ISDIR(root_meta.st_mode) or root_meta.st_uid != os.getuid():
            return
        os.chmod(root, 0o700)
        marker = root / ".demand"
        demand_lock = os.open(root / ".demand.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(demand_lock, 0o600)
            try:
                fcntl.flock(demand_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                age = time.time() - marker.stat().st_mtime
            except OSError:
                age = _REQUEST_TTL
            if age >= _REQUEST_TTL:
                marker.touch(exist_ok=True)
        finally:
            os.close(demand_lock)
        now = time.time()
        cache = _read_cache(now=now)
        fresh = cache is not None and now - float(cache["observed_at"]) <= _REQUEST_TTL
        if not fresh:
            _ensure_producer()
    except OSError:
        return


def _ensure_producer():
    root = state_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_meta = os.lstat(root)
    if not stat.S_ISDIR(root_meta.st_mode) or root_meta.st_uid != os.getuid():
        return False
    os.chmod(root, 0o700)
    fd = os.open(root / ".projection.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(fd, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--refresh-cache", "--lock-fd", str(fd)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, pass_fds=(fd,),
                         start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        os.close(fd)
        return False
    os.close(fd)
    return True


def _refresh_cache() -> bool:
    """Run one observational refresh. Caller owns the single-flight lock."""
    try:
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet.collectors import dispatch
        import human_gate_observation
        result = human_gate_observation.read_answer_waits(
            jobs_paths=dispatch.human_gate_registry_paths())
        if not publish_cache(result):
            failure = {"items": [], "complete": False, "observed_at": time.time(),
                       "health": [{"source": "producer", "state": "unknown"}]}
            publish_cache(failure)
            return False
        _notify_result(result)
        return True
    except Exception:
        publish_cache({"items": [], "complete": False, "observed_at": time.time(),
                       "health": [{"source": "producer", "state": "unknown"}]})
        return False


def _claim_notification(item_id: str, boundary: str):
    digest = hashlib.sha256((item_id + "\0" + boundary).encode()).hexdigest()
    root = state_root() / "notifications"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    path = root / (digest + ".sent")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        return None
    return path, fd


def _notify_item(item: dict, *, boundary: str, sound: bool = False) -> bool:
    executable = shutil.which("herdr")
    if not executable:
        return False
    claim = None
    try:
        claim = _claim_notification(str(item.get("id") or ""), boundary)
        if claim is None:
            return True
        marker, fd = claim
        os.write(fd, (boundary + "\n").encode())
        os.fchmod(fd, 0o600)
        os.close(fd)
        claim = (marker, None)
        question = _clean(item.get("question") or "사용자 답 대기")
        age = max(0, int((time.time() - float(item.get("since") or time.time())) // 3600))
        title = "◑답변 필요" + (" · %dh" % age if boundary in {"1h", "6h"} else "")
        command = [executable, "notification", "show", title, "--body", question or "사용자 답 대기"]
        if sound:
            command.extend(("--sound", "request"))
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5, check=False)
        if result.returncode == 0:
            return True
        marker.unlink(missing_ok=True)
        return False
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        if claim is not None:
            try:
                if claim[1] is not None:
                    os.close(claim[1])
            except OSError:
                pass
            claim[0].unlink(missing_ok=True)
        return False


def _notify_result(result: dict) -> None:
    now = float(result.get("observed_at") or time.time())
    for item in result.get("items", ()):
        if (not isinstance(item, dict) or item.get("stale")
                or now - float(item.get("since") or now) >= 72 * 3600
                   and item.get("source") in {"S2", "S3"}
                or now - float(item.get("since") or now) >= 7 * 86400):
            continue
        _notify_item(item, boundary="registered")
        age = now - float(item.get("since") or now)
        if age >= 6 * 3600:
            _notify_item(item, boundary="1h")  # skipped boundary prevents a delayed replay
            _notify_item(item, boundary="6h", sound=True)
        elif age >= 3600:
            _notify_item(item, boundary="1h", sound=True)


def _notification_delay(items, now, *, enabled):
    """Seconds to the next 1h/6h boundary, or None when no producer is needed."""
    if not enabled:
        return None
    deadlines = []
    for item in items:
        if not isinstance(item, dict) or item.get("stale"):
            continue
        try:
            since = float(item["since"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        maximum = 72 * 3600 if item.get("source") in {"S2", "S3"} else 7 * 86400
        age = now - since
        if age < 0 or age >= maximum:
            continue
        for boundary in (3600, 6 * 3600):
            remaining = since + boundary - now
            if remaining > 0:
                deadlines.append(remaining)
    return min(deadlines) if deadlines else None


def _producer_loop(fd: int) -> None:
    root = state_root()
    try:
        _refresh_cache()
        while True:
            now = time.time()
            try:
                demand = (root / ".demand").stat().st_mtime
            except OSError:
                demand = 0
            cached = _read_cache(now=now, max_age=float("inf")) or {}
            boundary = _notification_delay(cached.get("items", ()), now,
                                           enabled=bool(shutil.which("herdr")))
            idle = now - demand
            if idle >= 600 and boundary is None:
                try:
                    latest_demand = (root / ".demand").stat().st_mtime
                except OSError:
                    latest_demand = 0
                if latest_demand <= demand and time.time() - latest_demand >= 600:
                    return
                continue
            if idle >= 600 and boundary is not None:
                # With no live display demand, a notification is the only
                # reason to retain the producer. Sleep until that boundary;
                # the 30 second refresh cadence is for live demand only.
                delay = max(1.0, boundary)
            else:
                delay = min(30.0, max(1.0, 600.0 - idle))
                if boundary is not None:
                    delay = min(delay, max(1.0, boundary))
            time.sleep(delay)
            _refresh_cache()
    except Exception:
        return
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except OSError:
            pass


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--lock-fd", type=int)
    parser.add_argument("--count", nargs=2, metavar=("HARNESS", "SESSION"))
    args = parser.parse_args(argv)
    if args.refresh_cache:
        if args.lock_fd is not None:
            _producer_loop(args.lock_fd)
        else:
            # Direct internal invocation participates in the same single flight.
            try:
                root = state_root()
                root.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(root / ".projection.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _producer_loop(fd)
            except (OSError, BlockingIOError):
                pass
        return 0
    if args.count:
        value = cached_count(*args.count)
        if value is not None:
            print(("?" if value[0] < 0 else str(value[0])) + " " + str(value[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
