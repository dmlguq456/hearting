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


def read(harness, now=None, stale_max=None):
    """Last-good snapshot. `stale_max` widens the Fleet default (15 min) for a
    reader that prefers an older true reading over none, e.g. dispatch placement
    after a transient probe failure (2026-09-16: codex backend 503 at compose)."""
    now = time.time() if now is None else float(now)
    stale_max = STALE_MAX if stale_max is None else float(stale_max)
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
    if age < 0 or age > stale_max:
        return _unknown(attempted)
    freshness = "fresh" if age <= FRESH_WINDOWS.get(harness, 180.0) else "stale"
    return {"payload": item["payload"], "freshness": freshness,
            "observed_at": fetched, "attempted_at": attempted}


def _write(harness, payload, fetched_at, attempted_at):
    directory = state_dir()
    os.makedirs(directory, exist_ok=True)
    providers = {}
    try:
        with open(_path(harness), encoding="utf-8") as fh:
            existing = json.load(fh)
        if (isinstance(existing, dict) and existing.get("schema_version") == SCHEMA_VERSION
                and existing.get("harness") == harness and isinstance(existing.get("providers"), dict)):
            providers = existing["providers"]
    except (OSError, ValueError, TypeError):
        pass
    item = {"schema_version": SCHEMA_VERSION, "harness": harness,
            "fetched_at": fetched_at, "attempted_at": attempted_at,
            "payload": payload if isinstance(payload, dict) else {}, "providers": providers}
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


def record(harness, payload, now=None):
    """Write-through from any successful ad-hoc probe (dispatch placement, a
    manual `harness-capacity.py --refresh`), so one reading serves every reader.
    Only a dict payload counts; anything else is a no-op."""
    if not isinstance(payload, dict):
        return False
    now = time.time() if now is None else float(now)
    try:
        _write(harness, payload, now, now)
        return True
    except OSError:
        return False


def record_attempt(harness, now=None):
    """Note a failed probe without discarding the last-good payload; the file
    mtime moves so stale-driven refreshers back off instead of retrying every tick."""
    now = time.time() if now is None else float(now)
    previous = read(harness, now=now, stale_max=float("inf"))
    try:
        _write(harness, previous.get("payload") or {}, previous.get("observed_at") or 0.0, now)
        return True
    except OSError:
        return False


def record_provider(harness, provider, model, account_scope, state, reason=None,
                    now=None, source=None, *, credential_generation=None):
    """Persist exact provider eligibility beside usage without refreshing quota age.

    ``credential_generation`` is an opaque digest of the credential the outcome
    was observed with; readers holding a different one treat the row as unknown.
    """
    if (harness != "opencode" or not isinstance(provider, str) or not provider
            or not isinstance(model, str) or not model
            or state not in ("usable", "unavailable")):
        return False
    now = time.time() if now is None else float(now)
    try:
        directory = state_dir()
        os.makedirs(directory, exist_ok=True)
        path = _path(harness)
        try:
            with open(path, encoding="utf-8") as fh:
                item = json.load(fh)
        except (OSError, ValueError, TypeError):
            item = {"schema_version": SCHEMA_VERSION, "harness": harness,
                    "fetched_at": 0.0, "attempted_at": now, "payload": {}}
        if (not isinstance(item, dict) or item.get("schema_version") != SCHEMA_VERSION
                or item.get("harness") != harness):
            item = {"schema_version": SCHEMA_VERSION, "harness": harness,
                    "fetched_at": 0.0, "attempted_at": now, "payload": {}}
        providers = item.get("providers") if isinstance(item.get("providers"), dict) else {}
        key = json.dumps([provider, model, account_scope or "unscoped"], separators=(",", ":"))
        providers[key] = {"provider": provider, "model": model,
                          "account_scope": account_scope or "unscoped", "state": state,
                          "reason": reason, "observed_at": now, "source": source}
        if isinstance(credential_generation, str) and credential_generation:
            providers[key]["credential_generation"] = credential_generation
        item["providers"] = providers
        item["attempted_at"] = now
        fd, temp = tempfile.mkstemp(prefix=".%s." % harness, dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(item, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                fh.flush(); os.fsync(fh.fileno())
            os.replace(temp, path)
        finally:
            try:
                os.unlink(temp)
            except OSError:
                pass
        return True
    except OSError:
        return False


def read_provider(harness, provider, model, account_scope, *, now=None,
                  usable_max_age=STALE_MAX, credential_generation=None):
    """Read provider state independently of the quota window's freshness.

    A row recorded with another credential generation no longer describes the
    current credential (re-login or refresh), so it reads as unknown. Rows
    without a generation keep their existing meaning.
    """
    now = time.time() if now is None else float(now)
    try:
        with open(_path(harness), encoding="utf-8") as fh:
            item = json.load(fh)
    except (OSError, ValueError, TypeError):
        return {"state": "unknown", "reason": "no-observation", "observed_at": None}
    if (not isinstance(item, dict) or item.get("schema_version") != SCHEMA_VERSION
            or item.get("harness") != harness):
        return {"state": "unknown", "reason": "cache-invalid", "observed_at": None}
    providers = item.get("providers")
    key = json.dumps([provider, model, account_scope or "unscoped"], separators=(",", ":"))
    row = providers.get(key) if isinstance(providers, dict) else None
    if not isinstance(row, dict) or row.get("state") not in ("usable", "unavailable"):
        return {"state": "unknown", "reason": "no-observation", "observed_at": None}
    observed = row.get("observed_at")
    if isinstance(observed, bool) or not isinstance(observed, (int, float)) or observed > now:
        return {"state": "unknown", "reason": "observation-invalid", "observed_at": None}
    recorded = row.get("credential_generation")
    if recorded and credential_generation and recorded != credential_generation:
        return {"state": "unknown", "reason": "credential-generation-changed",
                "observed_at": observed}
    if row["state"] == "usable" and now - observed > usable_max_age:
        return {"state": "unknown", "reason": "observation-stale", "observed_at": observed}
    result = dict(row)
    result.pop("credential_generation", None)
    return result


def provider_records(harness, account_scopes=None):
    """Bounded, redacted provider observations for Fleet's usage/status view."""
    try:
        with open(_path(harness), encoding="utf-8") as fh:
            item = json.load(fh)
    except (OSError, ValueError, TypeError):
        return []
    if (not isinstance(item, dict) or item.get("schema_version") != SCHEMA_VERSION
            or item.get("harness") != harness or not isinstance(item.get("providers"), dict)):
        return []
    rows = []
    for row in item["providers"].values():
        if (not isinstance(row, dict) or row.get("state") not in ("usable", "unavailable")
                or not isinstance(row.get("provider"), str)
                or not isinstance(row.get("model"), str)):
            continue
        if account_scopes is not None and row.get("account_scope") not in account_scopes:
            continue
        rows.append({key: row.get(key) for key in
                     ("provider", "model", "state", "reason", "observed_at", "source")})
    return sorted(rows, key=lambda row: (row["provider"], row["model"], row["observed_at"] or 0))[-32:]


def _worker(harness):
    try:
        fetcher = _fetcher(harness)
        payload = fetcher() if fetcher else None
        now = time.time()
        previous = read(harness, now=now)
        if isinstance(payload, dict) and payload.get("error") in ("auth", "subscription-missing"):
            previous = read(harness, now=now, stale_max=float("inf"))
            old_payload = previous.get("payload") or {}
            quota = {key: value for key, value in old_payload.items()
                     if key in {"rl_5h", "rl_7d", "rl_ms", "rs_5h", "rs_7d", "rl_windows"}}
            _write(harness, {**quota, **payload}, previous.get("observed_at") or 0.0, now)
        elif isinstance(payload, dict) and "error" not in payload:
            _write(harness, payload, now, now)
        elif isinstance(payload, dict):
            previous = read(harness, now=now)
            if previous.get("payload") is not None:
                _write(harness, previous["payload"], previous.get("observed_at"), now)
            else:
                _write(harness, payload, 0.0, now)
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


def refresh_in_flight(harness, now=None, max_age=LEASE_MAX):
    """Whether a live usage refresh still owns a non-expired harness lease."""
    now = time.time() if now is None else float(now)
    try:
        lease_age = now - os.stat(_lease_path(harness)).st_mtime
        return 0 <= lease_age < min(LEASE_MAX, float(max_age))
    except OSError:
        return False


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
