"""Fleet-owned, non-blocking last-good usage cache and per-harness lease."""
import fcntl
import json
import math
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager

SCHEMA_VERSION = 1
FRESH_WINDOWS = {"claude": 180.0, "codex": 60.0, "opencode": 180.0}
STALE_MAX = 900.0
RETRY_AFTER = 60.0
LEASE_MAX = 60.0
_ERRORS = {"auth", "subscription-missing", "missing-credential", "unknown",
           "rate-limit", "timeout", "network", "response-invalid"}
_REASONS = {"credential-not-found", "credential-expired", "authentication-rejected",
            "subscription-missing", "credential-store-unavailable",
            "credential-store-read-failed", "credential-invalid", "credential-changed",
            "usage-rate-limited", "usage-timeout", "usage-unreachable",
            "usage-response-invalid", "usage-http-error", "usage-unreadable"}

FETCHERS = {}
_LOCK = threading.RLock()
_THREADS = {}
_CLAUDE_LOCK = threading.RLock()


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
            return _fetch_claude
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
            "attempted_at": attempted_at, "error_observation": None}


def _valid_time(value, now):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value <= now)


def _valid_retry(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _error_observation(item, now, stale_max):
    observation = item.get("error_observation") if isinstance(item, dict) else None
    if isinstance(observation, dict):
        observed_at = observation.get("observed_at")
        error = observation.get("error")
        reason = observation.get("reason")
        account_scope = observation.get("account_scope")
    else:
        # Schema-1 caches written before error_observation used attempted_at for
        # error-only payloads. Reuse that original attempt time; never renew it.
        payload = item.get("payload") if isinstance(item, dict) else None
        if not isinstance(payload, dict) or not payload.get("error"):
            return None
        observed_at = item.get("attempted_at")
        error = payload.get("error")
        reason = payload.get("reason")
        account_scope = payload.get("account_scope")
    retry_at = observation.get("retry_at") if isinstance(observation, dict) else None
    waiting = _valid_retry(retry_at) and now < retry_at
    if (not _valid_time(observed_at, now)
            or (now - observed_at > stale_max and not waiting)):
        return None
    if not isinstance(error, str) or error not in _ERRORS:
        return None
    result = {"error": error, "reason": reason if isinstance(reason, str)
            and reason in _REASONS else None,
            "observed_at": observed_at,
            "account_scope": account_scope}
    if _valid_retry(retry_at):
        result["retry_at"] = retry_at
    return result


def read(harness, now=None, stale_max=None):
    if harness == "claude":
        from . import usage_api
        credential = usage_api._credential()
        snapshot = _read(harness, now, stale_max, credential=credential)
        current = usage_api._credential()
        if current.generation != credential.generation:
            return _credential_unknown(current, time.time() if now is None else float(now))
        return snapshot
    return _read(harness, now, stale_max)


def _credential_unknown(credential, now):
    snapshot = _unknown()
    snapshot["error_observation"] = {
        "error": "missing-credential" if credential.reason == "credential-not-found" else "unknown",
        "reason": "credential-changed" if credential.generation else credential.reason,
        "observed_at": now, "account_scope": "current-credential"}
    return snapshot


def _read(harness, now=None, stale_max=None, *, credential=None):
    """Last-good snapshot. `stale_max` widens the Fleet default (15 min) for a
    reader that prefers an older true reading over none, e.g. dispatch placement
    after a transient probe failure (2026-09-16: codex backend 503 at compose)."""
    now = time.time() if now is None else float(now)
    stale_max = STALE_MAX if stale_max is None else float(stale_max)
    if harness == "claude" and not credential.generation:
        return _credential_unknown(credential, now)
    try:
        with open(_path(harness), encoding="utf-8") as fh:
            item = json.load(fh)
    except (OSError, ValueError, TypeError):
        return _unknown()
    if not isinstance(item, dict) or item.get("schema_version") != SCHEMA_VERSION \
            or item.get("harness") != harness or not isinstance(item.get("payload"), dict):
        return _unknown()
    if harness == "claude" and item.get("_credential_generation") != credential.generation:
        return _credential_unknown(credential, now)
    error_observation = _error_observation(item, now, stale_max)
    fetched = item.get("fetched_at")
    attempted = item.get("attempted_at")
    if not _valid_time(fetched, now) or not _valid_time(attempted, now):
        unknown = _unknown(attempted)
        unknown["error_observation"] = error_observation
        return unknown
    age = now - float(fetched)
    if age < 0 or age > stale_max:
        unknown = _unknown(attempted)
        unknown["error_observation"] = error_observation
        return unknown
    freshness = "fresh" if age <= FRESH_WINDOWS.get(harness, 180.0) else "stale"
    return {"payload": item["payload"], "freshness": freshness,
            "observed_at": fetched, "attempted_at": attempted,
            "error_observation": error_observation}


def _write(harness, payload, fetched_at, attempted_at, error_observation=None,
           *, credential_generation=None):
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
    if error_observation is None and isinstance(payload, dict) and payload.get("error"):
        error_observation = {key: payload.get(key) for key in
                             ("error", "reason", "account_scope")}
        error_observation["observed_at"] = attempted_at
    if isinstance(error_observation, dict):
        retry_at = error_observation.get("retry_at")
        error_observation = {
            "error": error_observation.get("error")
                     if isinstance(error_observation.get("error"), str)
                     and error_observation.get("error") in _ERRORS
                     else "unknown",
            "reason": error_observation.get("reason")
                      if isinstance(error_observation.get("reason"), str)
                      and error_observation.get("reason") in _REASONS else None,
            "account_scope": error_observation.get("account_scope"),
            "observed_at": error_observation.get("observed_at"),
        }
        if _valid_retry(retry_at):
            error_observation["retry_at"] = retry_at
    item = {"schema_version": SCHEMA_VERSION, "harness": harness,
            "fetched_at": fetched_at, "attempted_at": attempted_at,
            "payload": payload if isinstance(payload, dict) else {}, "providers": providers}
    if harness == "claude" and credential_generation:
        item["_credential_generation"] = credential_generation
    if isinstance(error_observation, dict):
        item["error_observation"] = error_observation
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
    # An ad-hoc dict cannot prove which Claude credential made its HTTP request.
    # Claude publication belongs to the owned fetch/lease path below.
    if harness == "claude" or not isinstance(payload, dict):
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
    if harness == "claude":
        return False
    now = time.time() if now is None else float(now)
    previous = read(harness, now=now, stale_max=float("inf"))
    try:
        _write(harness, previous.get("payload") or {}, previous.get("observed_at") or 0.0,
               now, error_observation=previous.get("error_observation"))
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


@contextmanager
def _claude_mutex():
    """Serialize only Claude lease/publication changes across threads/processes.

    Keep the lock inode stable. HTTP and worker joins never run under this lock.
    """
    with _CLAUDE_LOCK:
        os.makedirs(state_dir(), exist_ok=True)
        fd = os.open(os.path.join(state_dir(), ".claude.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def _claude_lease():
    try:
        with open(_lease_path("claude"), encoding="utf-8") as fh:
            lease = json.load(fh)
        return lease if isinstance(lease, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _owns_claude_lease(owner, credential):
    lease = _claude_lease()
    return lease.get("owner") == owner and lease.get("generation") == credential.generation


def _request_claude_refresh(now=None):
    from . import usage_api
    now = time.time() if now is None else float(now)
    try:
        with _claude_mutex():
            credential = usage_api._credential()
            if not credential.generation:
                return False
            snapshot = _read("claude", now, credential=credential)
            error = snapshot.get("error_observation") or {}
            if _valid_retry(error.get("retry_at")) and now < error["retry_at"]:
                return False
            if snapshot.get("freshness") == "fresh" and not error:
                return False
            attempted = snapshot.get("attempted_at")
            if (not _valid_retry(error.get("retry_at")) and _valid_time(attempted, now)
                    and now - attempted < RETRY_AFTER):
                return False
            lease = _claude_lease()
            if (lease.get("generation") == credential.generation
                    and _valid_time(lease.get("started_at"), now)
                    and now - lease["started_at"] < LEASE_MAX):
                return False
            owner = uuid.uuid4().hex
            # All lease writers/read-modify-writers share the mutex. Replacement
            # does not wait for an older account's HTTP request or thread.
            with open(_lease_path("claude"), "w", encoding="utf-8",
                      opener=lambda path, flags: os.open(path, flags, 0o600)) as fh:
                json.dump({"owner": owner, "generation": credential.generation,
                           "started_at": now}, fh)
            thread = threading.Thread(target=_claude_worker, args=(credential, owner),
                                      name="fleet-usage-claude", daemon=True)
            with _LOCK:
                _THREADS["claude"] = thread
            try:
                thread.start()
            except Exception:
                if _owns_claude_lease(owner, credential):
                    os.unlink(_lease_path("claude"))
                with _LOCK:
                    if _THREADS.get("claude") is thread:
                        _THREADS.pop("claude", None)
                return False
        return True
    except OSError:
        return False


def _claude_worker(credential, owner):
    from . import usage_api
    try:
        try:
            fetcher = _fetcher("claude")
            payload = fetcher(credential) if fetcher else None
        except Exception:
            payload = {"error": "unknown", "reason": "usage-unreadable"}
        with _claude_mutex():
            if (not _owns_claude_lease(owner, credential)
                    or usage_api._credential().generation != credential.generation):
                return
            now = time.time()
            if isinstance(payload, dict) and "error" not in payload:
                _write("claude", payload, now, now,
                       credential_generation=credential.generation)
            else:
                previous = _read("claude", now, stale_max=float("inf"), credential=credential)
                error = payload if isinstance(payload, dict) else {}
                retry_at = error.get("retry_at")
                # Preserve the existing minimum retry interval as well as the
                # server's deadline. This is the effective next allowed attempt.
                retry_at = max(now + RETRY_AFTER, retry_at) if _valid_retry(retry_at) else now + RETRY_AFTER
                diagnostic = {"error": error.get("error", "unknown"),
                              "reason": error.get("reason", "usage-unreadable"),
                              "account_scope": "current-credential", "observed_at": now,
                              "retry_at": retry_at}
                _write("claude", previous.get("payload") or {},
                       previous.get("observed_at") or 0.0, now,
                       error_observation=diagnostic, credential_generation=credential.generation)
    except OSError:
        pass
    finally:
        try:
            with _claude_mutex():
                if _owns_claude_lease(owner, credential):
                    os.unlink(_lease_path("claude"))
        except OSError:
            pass
        with _LOCK:
            if _THREADS.get("claude") is threading.current_thread():
                _THREADS.pop("claude", None)


def _worker(harness):
    # Claude's owned worker requires the captured HTTP credential and lease id.
    # It must never fall through this legacy, unbound publication path.
    if harness == "claude":
        return
    try:
        fetcher = _fetcher(harness)
        payload = fetcher() if fetcher else None
        now = time.time()
        previous = read(harness, now=now)
        if isinstance(payload, dict) and payload.get("error") in ("auth", "subscription-missing"):
            previous = read(harness, now=now, stale_max=float("inf"))
            old_payload = previous.get("payload") or {}
            same_scope = (isinstance(payload.get("account_scope"), str)
                          and payload.get("account_scope")
                          and payload.get("account_scope") == old_payload.get("account_scope"))
            quota = ({key: value for key, value in old_payload.items()
                      if key in {"rl_5h", "rl_7d", "rl_ms", "rs_5h", "rs_7d", "rl_windows"}}
                     if same_scope else {})
            safe_error = {key: payload.get(key) for key in
                          ("error", "reason", "account_scope") if payload.get(key) is not None}
            _write(harness, {**quota, **safe_error},
                   previous.get("observed_at") if quota else 0.0, now,
                   error_observation={**safe_error, "observed_at": now})
        elif isinstance(payload, dict) and "error" not in payload:
            _write(harness, payload, now, now)
        elif isinstance(payload, dict):
            previous = read(harness, now=now)
            if previous.get("payload") is not None:
                diagnostic = {key: payload.get(key) for key in
                              ("error", "reason", "account_scope")}
                diagnostic["observed_at"] = now
                _write(harness, previous["payload"], previous.get("observed_at"), now,
                       error_observation=diagnostic)
            else:
                safe_error = {key: payload.get(key) for key in
                              ("error", "reason", "account_scope") if payload.get(key) is not None}
                _write(harness, safe_error, 0.0, now,
                       error_observation={**safe_error, "observed_at": now})
        elif previous.get("payload") is not None:
            _write(harness, previous["payload"], previous.get("observed_at"), now,
                   error_observation=previous.get("error_observation"))
        elif previous.get("error_observation") is not None:
            _write(harness, {}, 0.0, now,
                   error_observation=previous.get("error_observation"))
        else:
            _write(harness, {}, 0.0, now)
    except Exception:
        try:
            now = time.time(); previous = read(harness, now=now)
            _write(harness, previous.get("payload") or {}, previous.get("observed_at") or 0.0,
                   now, error_observation={"error": "unknown", "reason": "usage-unreadable",
                                           "observed_at": now})
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
    if harness == "claude":
        return _request_claude_refresh(now)
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
    if harness == "claude":
        from . import usage_api
        credential = usage_api._credential()
        lease = _claude_lease()
        return bool(credential.generation and lease.get("generation") == credential.generation
                    and _valid_time(lease.get("started_at"), now)
                    and now - lease["started_at"] < min(LEASE_MAX, float(max_age)))
    try:
        lease_age = now - os.stat(_lease_path(harness)).st_mtime
        return 0 <= lease_age < min(LEASE_MAX, float(max_age))
    except OSError:
        return False


def account_usage(harness, usage="cache-only", now=None):
    snapshot = read(harness, now=now)
    if usage == "refresh":
        request_refresh(harness, now=now)
        if harness == "claude":
            return read(harness, now=now)
    return snapshot


def _fetch_claude(credential):
    from . import usage_api
    return usage_api._fetch(credential)


def _fetch_codex():
    from . import codex
    return codex.account_usage()


def _fetch_opencode():
    from . import zen_go_usage
    return zen_go_usage.account_usage()


FETCHERS.update({"claude": _fetch_claude, "codex": _fetch_codex,
                 "opencode": _fetch_opencode})
