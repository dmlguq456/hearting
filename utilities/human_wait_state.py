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


_MARKER = re.compile(r"^\s*\[(?:답변 필요|결정 필요)\]\s*(.*)$")
_MAX_QUESTION = 160
_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_CACHE_MAX_BYTES = 2 * 1024 * 1024
_DISPLAY_TTL = 180.0
_REQUEST_TTL = 60.0


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


def _clean(value: str) -> str:
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    value = " ".join("".join(ch for ch in value if ch >= " " and ch != "\x7f").split())
    return value[:_MAX_QUESTION]


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
        row["question"] = _clean(item.get("question") or "") or None
        row["seat_key"] = owner.get("seat_key")
        row["owner"] = {"harness": harness, "session_id": sid}
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
        _write(_cache_path(), value)
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
            fleet_items.append({
                "id": item.get("id"), "source": source, "kind": kind,
                "session_id": owner.get("session_id"), "harness": owner.get("harness"),
                "pane_id": None, "route_id": item.get("route_id"),
                "node": item.get("gate_id"), "attempt_id": item.get("attempt_id"),
                "since": item.get("since"),
                "stale": "older" if item.get("stale") else "current",
            })
        fleet_value = {"schema_version": 1, "observed_at": observed_at,
                       "complete": bool(result.get("complete")), "total": current_total,
                       "older": stale_total, "items": fleet_items,
                       "unattributed": [item["id"] for item in fleet_items
                                        if not item["session_id"] or not item["harness"]],
                       "health": [{"source": row.get("source"), "state": row.get("state")}
                                  for row in result.get("health", ()) if isinstance(row, dict)]}
        _write(_fleet_cache_path(), fleet_value)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _read_cache(*, now=None, max_age=_DISPLAY_TTL):
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
