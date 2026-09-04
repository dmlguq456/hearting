"""Fleet-owned, non-blocking last-good usage cache and per-harness lease."""
import json
import os
import tempfile
import threading
import time

SCHEMA_VERSION = 1
FRESH_WINDOWS = {"claude": 180.0, "codex": 60.0, "opencode": 180.0}
STALE_MAX = 900.0
RETRY_AFTER = 60.0
LEASE_MAX = 60.0

FETCHERS = {}
_LOCK = threading.RLock()
_THREADS = {}


def state_dir():
    return os.environ.get(
        "FLEET_USAGE_STATE_DIR",
        os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
                     "agent-fleet", "usage"),
    )


def _path(harness):
    return os.path.join(state_dir(), "%s.json" % harness)


def _lease_path(harness):
    return os.path.join(state_dir(), ".%s.lease" % harness)


def _fetcher(harness):
    if harness in FETCHERS:
        return FETCHERS[harness]
    try:
        if harness == "claude":
            from . import usage_api
            return usage_api.account_usage
        if harness == "codex":
            from . import codex
            return codex.account_usage
        if harness == "opencode":
            from . import zen_go_usage
            return zen_go_usage.account_usage
    except Exception:
        return None
    return None


def _unknown(attempted_at=None):
    return {"payload": None, "freshness": "unknown", "observed_at": None,
            "attempted_at": attempted_at}


def read(harness, now=None):
    now = time.time() if now is None else float(now)
    try:
        with open(_path(harness), encoding="utf-8") as fh:
            item = json.load(fh)
    except (OSError, ValueError, TypeError):
        return _unknown()
    if not isinstance(item, dict) or item.get("schema_version") != SCHEMA_VERSION \
            or item.get("harness") != harness or not isinstance(item.get("payload"), dict):
        return _unknown(item.get("attempted_at") if isinstance(item, dict) else None)
    fetched = item.get("fetched_at")
    attempted = item.get("attempted_at")
    if not isinstance(fetched, (int, float)) or not isinstance(attempted, (int, float)) \
            or fetched > now or attempted > now:
        return _unknown(attempted)
    age = now - float(fetched)
    if age < 0 or age > STALE_MAX:
        return _unknown(attempted)
    freshness = "fresh" if age <= FRESH_WINDOWS.get(harness, 180.0) else "stale"
    return {"payload": item["payload"], "freshness": freshness,
            "observed_at": fetched, "attempted_at": attempted}


def _write(harness, payload, fetched_at, attempted_at):
    directory = state_dir()
    os.makedirs(directory, exist_ok=True)
    item = {"schema_version": SCHEMA_VERSION, "harness": harness,
            "fetched_at": fetched_at, "attempted_at": attempted_at,
            "payload": payload if isinstance(payload, dict) else {}}
    fd, temp = tempfile.mkstemp(prefix=".%s." % harness, dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(item, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fh.flush(); os.fsync(fh.fileno())
        os.replace(temp, _path(harness))
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass


def _worker(harness):
    try:
        fetcher = _fetcher(harness)
        payload = fetcher() if fetcher else None
        now = time.time()
        previous = read(harness, now=now)
        if isinstance(payload, dict):
            _write(harness, payload, now, now)
        elif previous.get("payload") is not None:
            _write(harness, previous["payload"], previous.get("observed_at"), now)
        else:
            _write(harness, {}, 0.0, now)
    except Exception:
        try:
            now = time.time(); previous = read(harness, now=now)
            _write(harness, previous.get("payload") or {}, previous.get("observed_at") or 0.0, now)
        except Exception:
            pass
    finally:
        try:
            os.unlink(_lease_path(harness))
        except OSError:
            pass
        with _LOCK:
            _THREADS.pop(harness, None)


def request_refresh(harness, now=None):
    now = time.time() if now is None else float(now)
    snapshot = read(harness, now=now)
    if snapshot.get("freshness") == "fresh":
        return False
    attempted = snapshot.get("attempted_at")
    if isinstance(attempted, (int, float)) and now - attempted < RETRY_AFTER:
        return False
    directory = state_dir()
    os.makedirs(directory, exist_ok=True)
    lease = _lease_path(harness)
    try:
        age = now - os.stat(lease).st_mtime
        if age < LEASE_MAX:
            return False
        os.unlink(lease)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    try:
        fd = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        return False
    with _LOCK:
        if harness in _THREADS:
            return False
        thread = threading.Thread(target=_worker, args=(harness,),
                                  name="fleet-usage-%s" % harness, daemon=True)
        _THREADS[harness] = thread
        thread.start()
    return True


def account_usage(harness, usage="cache-only", now=None):
    snapshot = read(harness, now=now)
    if usage == "refresh":
        request_refresh(harness, now=now)
    return snapshot


def _fetch_claude():
    from . import usage_api
    return usage_api._fetch()


def _fetch_codex():
    from . import codex
    return codex.account_usage()


def _fetch_opencode():
    from . import zen_go_usage
    return zen_go_usage.account_usage()


FETCHERS.update({"claude": _fetch_claude, "codex": _fetch_codex,
                 "opencode": _fetch_opencode})
