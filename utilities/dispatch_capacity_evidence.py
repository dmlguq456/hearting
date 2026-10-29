"""Exact native quota evidence, scoped independently of positive usage gauges.

This reader never closes attempts or authorizes retries. Execution/cleanup and
the sealed selection policy retain those decisions. No credential bytes leave
scope hashing; legacy evidence without a launch scope remains diagnostic only.
A launch decision may recheck a holding Claude limit with one minimal request
through the login a worker would use (`probe`, at most every PROBE_INTERVAL).
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import stat
import tempfile
import time
try:
    import fcntl
except ImportError:  # pragma: no cover - platforms without advisory flock use direct reads
    fcntl = None

HARNESSES = ("claude", "codex", "opencode")
WINDOWS = {"five_hour": 6 * 3600, "seven_day": 8 * 86400,
           "seven_day_opus": 8 * 86400, "seven_day_sonnet": 8 * 86400}
CACHE_SCHEMA = 2
# Exact historical quota reasons, not every diagnostic containing "limit".
# In particular, model context/config discovery failure is not quota evidence.
_LEGACY_QUOTA_NOTES = frozenset({
    "dead-limit", "dead-session-limit", "dead-usage-limit", "dead-rate-limit", "dead-quota-limit",
})
# Tests switch this off to compare the cached answer with a direct computation.
_DISK_CACHE = True


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _json(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _api_configured(env):
    return any(env.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                                    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                                    "CLAUDE_CODE_USE_FOUNDRY"))


def _scope_parts(harness, env=None):
    """Seal the selected authentication scope before model start, without a probe.

    Native weekly feedback is currently supplied by Claude subscription events.
    Other runtimes retain their existing typed capacity handling, not a guessed
    weekly window derived from HTTP 429 or a provider name.
    """
    env = os.environ if env is None else env
    if harness != "claude":
        return {}
    home = Path(env.get("HOME") or Path.home())
    runtime = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser().resolve()
    if _api_configured(env):
        return {}  # API/provider throttling is not subscription quota.
    token = env.get("CLAUDE_CODE_OAUTH_TOKEN")
    config = runtime / ".claude.json" if env.get("CLAUDE_CONFIG_DIR") else home / ".claude.json"
    account = _json(config).get("oauthAccount") or {}
    if not isinstance(account, dict):
        account = {}
    identity = {k: account.get(k) for k in ("accountUuid", "organizationUuid")}
    if token:
        identity = {"oauth_token_digest": hashlib.sha256(token.encode()).hexdigest()}
    elif not all(isinstance(v, str) and v for v in identity.values()):
        return {}
    return runtime, identity


def _scope_candidates(harness, env=None):
    parts = _scope_parts(harness, env)
    if not parts:
        return {}
    runtime, identity = parts
    return {
        "claude-subscription-v2": digest(["claude-subscription-v2", identity]),
        # v2.140.3 sealed storage location too. Honor its existing evidence
        # only where that exact scope can still be proved; new rows use v2.
        "claude-subscription-v1": digest(["claude-subscription-v1", str(runtime), identity]),
    }


def launch_scope(harness, env=None):
    scopes = _scope_candidates(harness, env)
    kind = "claude-subscription-v2"
    return {"quota_scope": scopes[kind], "quota_scope_kind": kind} if scopes else {}


def native_quota(rows, *, observed_at, now=None, requested_model=None):
    """Accept only an exact failed native session's structured rejected window."""
    now = time.time() if now is None else now
    terminal_index = next((i for i in range(len(rows) - 1, -1, -1) if rows[i].get("type") == "result"), None)
    terminal = rows[terminal_index] if terminal_index is not None else None
    if not terminal or terminal.get("runtime") == "opencode" or terminal.get("is_error") is not True:
        return None
    if str(terminal.get("api_error_status", "")) != "429":
        return None
    sid = terminal.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None
    previous_terminal = next((i for i in range(terminal_index - 1, -1, -1)
                              if rows[i].get("type") == "result"), -1)
    rows = rows[previous_terminal + 1:terminal_index]
    # A newer accepted event for this session supersedes a previous rejection.
    event = next((r for r in reversed(rows) if r.get("type") == "rate_limit_event"
                  and r.get("session_id") == sid), None)
    info = (event or {}).get("rate_limit_info") or {}
    if not isinstance(info, dict):
        return None
    window = info.get("rateLimitType")
    if info.get("status") != "rejected" or not isinstance(window, str) or info.get("isUsingOverage") is True:
        return None
    if window == "seven_day_overage_included":
        # This is a separate included-credit pool, not proof that every model
        # on the subscription is exhausted. Bind it to the model the exact
        # attempt launched; neither prose nor an alias guessed from prose is
        # authority. Without that binding it remains diagnostic only.
        if (info.get("overageStatus") != "rejected" or info.get("isUsingOverage") is not False
                or not isinstance(requested_model, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", requested_model)):
            return None
        maximum, model_scope = 8 * 86400, "model:" + requested_model
    elif window in WINDOWS:
        maximum = WINDOWS[window]
        # Native quota bucket suffixes identify families, not execution-model
        # defaults. Derive the family from the admitted bucket name itself.
        model_scope = window.removeprefix("seven_day_") if window.startswith("seven_day_") else "all"
    else:
        return None
    reset = info.get("resetsAt")
    if (isinstance(reset, bool) or not isinstance(reset, (int, float)) or not math.isfinite(reset)
            or not observed_at <= now + 60 or not observed_at < reset <= observed_at + maximum):
        return None
    return {"window": window, "reset_epoch": int(reset), "model_scope": model_scope,
            "native_session_id": sid, "observed_at": observed_at,
            "evidence_digest": digest([event, terminal]), "expired": now >= reset}


def _native_rows(path):
    try:
        with Path(path).open("rb") as f:
            start = max(0, f.seek(0, 2) - 1024 * 1024)
            f.seek(start)
            lines = f.read().splitlines()
        if start:
            lines = lines[1:]
    except OSError:
        return []
    result = []
    for line in lines:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                result.append(row)
        except (ValueError, UnicodeError):
            pass
    return result


def _stat_key(path):
    """Identity of one file version, or None when it cannot be stat'ed (missing, unreadable)."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_mode, st.st_ctime_ns]


def _settled(stat):
    """False for a file modified within the last 100ms.

    Coarse filesystem timestamps can give a second same-size write the same mtime, so a
    fresh file is not trusted as a cache key until a later read sees it settled.
    """
    return stat is None or time.time_ns() - stat[3] > 100_000_000


def _cache_path(jobs):
    return Path(f"{os.path.abspath(jobs)}.capacity-cache.json")


def _build_snapshot(lines, now, previous=None):
    """The time- and environment-independent candidates of a registry.

    Only what the readers cannot cheaply rederive is kept: which rows can carry native
    quota evidence or a legacy limit marker. Expiry, the 8-day cutoff, scope matching,
    model selection and reset conversion stay per-call decisions. Native rows older than
    eight days at `now` can never matter again, so they are not kept.
    """
    native, legacy = [], []
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        meta = dict(cell.split("=", 1) for cell in fields[5].split(",") if "=" in cell)
        note = meta.get("note", "")
        attempt, log = meta.get("attempt_id", ""), meta.get("log_file", "")
        if (fields[1] == "done" and meta.get("harness") == "claude" and meta.get("failure_class") != "pass"
                and note.startswith("dead-") and re.fullmatch(r"att-[a-zA-Z0-9-]+", attempt)
                and attempt in Path(log).name):
            try:
                observed = datetime.fromisoformat(fields[0].replace("Z", "+00:00")).timestamp()
            except ValueError:
                observed = None
            if observed is not None and now - observed <= 8 * 86400:
                record = {"o": observed, "a": attempt, "l": log, "m": meta.get("model"),
                          "s": meta.get("quota_scope"), "k": meta.get("quota_scope_kind"), "d": digest(line)}
                earlier = (previous or {}).get(record["d"])
                if earlier:  # same row: its extracted evidence stays valid while the log's stat does
                    record.update(e=earlier["e"], ls=earlier["ls"])
                native.append(record)
        harness = meta.get("harness") or meta.get("owner_harness")
        if harness in HARNESSES and (note in _LEGACY_QUOTA_NOTES
                                     or (note == "dead-capacity" and meta.get("failure_class") != "pass")):
            legacy.append({"t": fields[0], "h": harness, "a": meta.get("attempt_id"),
                           "s": meta.get("quota_scope"), "k": meta.get("quota_scope_kind"),
                           "n": note, "r": meta.get("reset", "-"), "l": meta.get("log_file")})
    return {"native": native, "legacy": legacy}


def _is_stat(value):
    return value is None or (isinstance(value, list) and len(value) == 6
                             and all(isinstance(v, int) and not isinstance(v, bool) for v in value))


def _is_str(value, *, nullable=False):
    return isinstance(value, str) or (nullable and value is None)


def _valid_cache(data):
    """Structural check only: a cache file that fails it is ignored, never repaired."""
    if not isinstance(data, dict) or data.get("schema") != CACHE_SCHEMA:
        return False
    horizon = data.get("horizon")
    if (not _is_str(data.get("jobs")) or isinstance(horizon, bool) or not isinstance(horizon, (int, float))
            or not _is_stat(data.get("key")) or data.get("key") is None):
        return False
    if not isinstance(data.get("native"), list) or not isinstance(data.get("legacy"), list):
        return False
    for rec in data["native"]:
        if not (isinstance(rec, dict) and isinstance(rec.get("o"), (int, float)) and not isinstance(rec["o"], bool)
                and _is_str(rec.get("a")) and _is_str(rec.get("l")) and _is_str(rec.get("d"))
                and all(_is_str(rec.get(k), nullable=True) for k in ("m", "s", "k"))):
            return False
        if ("e" in rec) != ("ls" in rec) or not _is_stat(rec.get("ls")):
            return False
        evidence = rec.get("e")
        if evidence is not None and not (isinstance(evidence, list) and len(evidence) == 2
                                         and all(isinstance(row, dict) for row in evidence)):
            return False
    for rec in data["legacy"]:
        if not (isinstance(rec, dict) and _is_str(rec.get("t")) and rec.get("h") in HARNESSES
                and _is_str(rec.get("n")) and _is_str(rec.get("r"))
                and all(_is_str(rec.get(k), nullable=True) for k in ("a", "s", "k", "l"))):
            return False
        cached = rec.get("rr")
        if cached is not None and not (isinstance(cached, list) and len(cached) == 3
                                       and _is_str(cached[0]) and _is_str(cached[1], nullable=True)
                                       and _is_stat(cached[2])):
            return False
    return True


def _load_cache(path, now):
    """`(snapshot | None, earlier native records by row digest)` from the cache file."""
    key = _stat_key(path)
    if key is None:
        return None, {}
    data = json.loads(_cache_path(path).read_text())
    if not _valid_cache(data):
        return None, {}
    earlier = {rec["d"]: rec for rec in data["native"] if "e" in rec}
    # A cache built at a later `now` dropped rows this reader may still need.
    if data["jobs"] != os.path.abspath(path) or data["key"] != key or data["horizon"] > now \
            or _stat_key(path) != key:
        return None, earlier
    return {"native": data["native"], "legacy": data["legacy"], "path": path, "key": key,
            "horizon": data["horizon"], "cacheable": True, "dirty": False}, earlier


def _fresh_snapshot(jobs, now, earlier):
    """Fresh jobs.log read reusing already-validated `earlier` evidence, without a cache parse.

    Only the shared-cache miss path uses this: the caller already ran the pre-lock
    and post-lock `_load_cache` checks, so re-parsing the same cache file here
    would only repeat JSON parse work. All freshness rules (stat before/after,
    ctime, horizon/schema inside the earlier loads, settled/write-race checks
    here) stay with the callers and `_build_snapshot`.
    """
    path = Path(jobs)
    key = _stat_key(path)
    try:
        text = path.read_text()
    except (OSError, UnicodeError):
        return None
    stable = key is not None and _stat_key(path) == key
    return {**_build_snapshot(text.splitlines(), now, earlier), "path": path, "key": key,
            "horizon": now, "cacheable": _DISK_CACHE and stable and _settled(key), "dirty": True}


def _snapshot(jobs, now, lines=None):
    """Candidates for one call: from `lines` when given, else the disk cache or a fresh read.

    None means the registry could not be read. Any cache trouble (missing, corrupt,
    other schema, a file that changed under the read) silently becomes a fresh read.
    """
    if lines is not None:
        return {**_build_snapshot(lines, now), "cacheable": False, "dirty": False}
    path = Path(jobs)
    earlier = {}
    if _DISK_CACHE:
        try:
            snapshot, earlier = _load_cache(path, now)
            if snapshot is not None:
                return snapshot
        except Exception:  # noqa: BLE001 - a broken cache must never change the answer
            earlier = {}
    return _fresh_snapshot(path, now, earlier)


@contextmanager
def _snapshot_guard(jobs, now, lines=None):
    """Coalesce a stable shared-cache miss; normal hits never take this lock."""
    if lines is not None or not _DISK_CACHE or fcntl is None:
        yield _snapshot(jobs, now, lines)
        return
    path = Path(jobs)
    key = _stat_key(path)
    if key is None or not _settled(key):
        yield _snapshot(jobs, now)
        return
    pre_earlier = {}
    try:
        cached, pre_earlier = _load_cache(path, now)
    except Exception:  # noqa: BLE001 - broken cache remains a direct-read fallback
        cached, pre_earlier = None, {}
    if cached is not None:
        yield cached
        return

    lock_fd = None
    try:
        lock_path = Path(str(_cache_path(path)) + ".lock")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(lock_path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise OSError("cache lock is not a regular file")
        os.fchmod(lock_fd, 0o600)
        deadline = time.monotonic() + 1.0
        locked = False
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    except Exception:  # noqa: BLE001 - unsupported lock/cache filesystem uses direct read
        if lock_fd is not None:
            os.close(lock_fd)
        yield _snapshot(path, now)
        return

    if not locked:
        os.close(lock_fd)
        yield _snapshot(path, now)
        return
    try:
        post_earlier = {}
        try:
            cached, post_earlier = _load_cache(path, now)
        except Exception:  # noqa: BLE001
            cached, post_earlier = None, {}
        # Another cold process may have published while this one waited.
        if cached is not None:
            yield cached
        else:
            yield _fresh_snapshot(path, now, {**pre_earlier, **post_earlier})
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


def _persist(snapshot):
    """Publish the snapshot beside jobs.log: 0600, same-directory temp file, atomic replace."""
    if not (snapshot and snapshot.get("cacheable") and snapshot.get("dirty")):
        return
    tmp = None
    try:
        path = snapshot["path"]
        if _stat_key(path) != snapshot["key"]:
            return
        target = _cache_path(path)
        payload = {"schema": CACHE_SCHEMA, "jobs": os.path.abspath(path), "key": snapshot["key"],
                   "horizon": snapshot["horizon"], "native": snapshot["native"], "legacy": snapshot["legacy"]}
        fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        tmp = None
        snapshot["dirty"] = False
    except Exception:  # noqa: BLE001 - an unwritable directory just means no cache
        pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _evidence_pair(rows, observed, model):
    """`[rate_limit_event, terminal result]` when `native_quota` can ever accept these rows.

    `native_quota` reads nothing else from a log tail, so the pair reproduces its answer
    at every `now`; None (no `now` accepts it) stores as "no evidence".
    """
    if not native_quota(rows, observed_at=observed, now=observed, requested_model=model):
        return None
    terminal_index = next(i for i in range(len(rows) - 1, -1, -1) if rows[i].get("type") == "result")
    terminal = rows[terminal_index]
    previous = next((i for i in range(terminal_index - 1, -1, -1) if rows[i].get("type") == "result"), -1)
    event = next(r for r in reversed(rows[previous + 1:terminal_index])
                 if r.get("type") == "rate_limit_event" and r.get("session_id") == terminal.get("session_id"))
    return [event, terminal]


def _native_evidence(snapshot, record):
    """The record's evidence pair, re-extracted only when its log's stat moved."""
    log = record["l"]
    current = _stat_key(log)
    if "e" in record and record["ls"] == current:
        return record["e"]
    evidence = _evidence_pair(_native_rows(log), record["o"], record["m"])
    if _stat_key(log) == current and _settled(current):  # a moving log is used once, never kept
        record["e"], record["ls"] = evidence, current
        snapshot["dirty"] = True
    return evidence


def _native_observations(snapshot, env, now):
    scopes = _scope_candidates("claude", env)
    found = []
    for record in snapshot["native"]:
        if now - record["o"] > 8 * 86400:
            continue
        evidence = _native_evidence(snapshot, record)
        if not evidence:
            continue
        quota = native_quota(evidence, observed_at=record["o"], now=now, requested_model=record["m"])
        if not quota:
            continue
        bound_scope = record["s"]
        matches = bool(bound_scope and bound_scope == scopes.get(record["k"]))
        found.append({**quota, "harness": "claude", "attempt_id": record["a"],
                      "quota_scope": bound_scope, "scope_authority": "launch-bound" if bound_scope else "unbound",
                      "scope_matches": matches,
                      "row_digest": record["d"]})
    return found


def observations(jobs, *, now=None, env=None, registry_lines=None):
    now = time.time() if now is None else now
    with _snapshot_guard(jobs, now, registry_lines) as snapshot:
        if snapshot is None:
            return []
        found = _native_observations(snapshot, env, now)
        _persist(snapshot)
        return found


def _limit_models(profile, models, env):
    models = dict(models or {})
    if profile and "claude" not in models:
        from model_profile import resolve_runtime_profile, ModelProfileError
        try:
            models["claude"] = resolve_runtime_profile("claude", profile, environ=env)[0]["model"]
        except (ModelProfileError, KeyError):
            pass
    return models


def _opencode_model_state(profile, env):
    """Automatic placement admits OpenCode only while one automatic candidate is eligible.

    Confirmed rejections and providers Hearting cannot verify both leave the
    candidate out (`opencode_runtime.automatic_eligible`). The hold is not a
    usage limit with a reset: another harness is selected, nothing waits.
    """
    if not profile:
        return None
    try:
        from model_profile import resolve_runtime_profile
        import opencode_runtime
        selected, _receipt = resolve_runtime_profile(
            "opencode", profile, environ=env,
            source_root=Path(__file__).resolve().parents[1])
        candidates = opencode_runtime.automatic_candidates(
            selected["model"], selected["budget"], environ=env)
    except Exception:
        return None
    if not candidates or any(opencode_runtime.automatic_eligible(row.get("model"), row)
                             for row in candidates):
        return None
    causes = sorted({f"{row.get('model', '').split('/', 1)[0]}-{row.get('reason') or 'unavailable'}"
                     for row in candidates})
    kind = ("provider-model-unavailable" if all(row.get("state") == "unavailable" for row in candidates)
            else "provider-model-unverified")
    return f"limited({kind}:" + "+".join(causes) + ")"


def model_hold(harness, profile, *, env=None):
    """The provider/model hold `usage_states(profile=...)` applies to `harness`, or None."""
    if harness != "opencode":
        return None
    return _opencode_model_state(profile, os.environ if env is None else env)


def _scope_covers(model_scope, model):
    """Does a limit on `model_scope` hold the requested `model` (None: no model asked)?"""
    if model_scope == "all":
        return True
    if not isinstance(model, str) or not model:
        return False
    if model_scope.startswith("model:"):
        return model == model_scope.removeprefix("model:")
    return model_scope in model.lower()


def _admitted(native, models):
    """Unexpired observations for the current account that hold the requested model."""
    return [o for o in native if not o["expired"] and o["scope_matches"]
            and _scope_covers(o["model_scope"], models.get("claude"))]


def _select_limits(native, models):
    result = {}
    for observation in _admitted(native, models):
        previous = result.get("claude")
        if not previous or previous["reset_epoch"] < observation["reset_epoch"]:
            result["claude"] = observation
    return result


def _native_items(admitted):
    """Admitted native observations as holding items: when, on which window family, until when."""
    return [{"at": o["observed_at"], "scope": o["model_scope"], "reset": o["reset_epoch"],
             "window": o.get("window"), "source": "native"} for o in admitted]


_CAPACITY = None


def _capacity_reader():
    """`harness-capacity.py` by path: its filename is not importable.

    Loaded once per process; the module reads its environment at call time, so
    a fixture `FLEET_USAGE_STATE_DIR` still applies. Same shape as
    `dispatch-owner.py` / `dispatch-batch.py` / `dispatch_replacement.py`.
    """
    global _CAPACITY
    if _CAPACITY is None:
        spec = importlib.util.spec_from_file_location(
            "dispatch_capacity_harness_capacity", Path(__file__).with_name("harness-capacity.py"))
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load harness-capacity.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CAPACITY = module
    return _CAPACITY


def gauge_limits(now):
    """Shared-gauge holds (`codex`/`opencode`) as `active_limits` items.

    A window at 100% used with a future reset is a real usage limit, not the
    soft ordering gate; `harness-capacity.gauge_limit_epoch` is the one reader
    (Codex usable credits lift only the gauge hold; manual score, staleness
    and unknown-reset keep their existing rules). A
    missing or broken capacity reader leaves today's answer in place.
    """
    limits = {}
    try:
        capacity = _capacity_reader()
        for harness in ("codex", "opencode"):
            reset = capacity.gauge_limit_epoch(harness, now=now)
            if reset:
                limits[harness] = {"harness": harness, "window": "gauge", "model_scope": "all",
                                   "observed_at": now, "scope_authority": "gauge",
                                   "expired": False, "reset_epoch": reset}
    except Exception:  # a supplementary gauge must never break the reader
        return {}
    return limits


def _hold_from_gauge(states, epochs, limits):
    """Merge gauge holds: the later known end wins and the reset labels the hold."""
    for harness, limit in limits.items():
        reset = limit["reset_epoch"]
        known = epochs.get(harness)
        if isinstance(known, (int, float)) and known >= reset:
            continue
        states[harness] = f"limited({_iso(reset)})"
        epochs[harness] = reset


def active_limits(jobs, *, profile=None, models=None, now=None, env=None, registry_lines=None, recheck=False):
    now = time.time() if now is None else now
    models = _limit_models(profile, models, env)
    native = observations(jobs, now=now, env=env, registry_lines=registry_lines)
    limits = _select_limits(native, models)
    limits.update(gauge_limits(now))
    verdict, holder = _recheck(jobs, _native_items(_admitted(native, models)), models, env, now, recheck)
    if verdict == "ok":
        limits.pop("claude", None)
    elif verdict is not None:
        held = limits.get("claude") or {
            "harness": "claude", "window": holder.get("window"), "model_scope": holder["scope"],
            "observed_at": holder["at"], "scope_authority": holder["source"], "expired": False}
        limits["claude"] = {**held, "reset_epoch": verdict}
    return limits


def apply_limits(states, limits):
    states = dict(states)
    for harness, limit in limits.items():
        states[harness] = f"limited({_iso(limit['reset_epoch'])})"
    return states


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


# A recorded Claude limit describes the moment it was observed. The login a worker
# would use can recover before the recorded reset: another account is signed in,
# extra usage was added, or the window reset early. While Claude is held, a launch
# decision (`recheck=True`) sends that login one minimal request, at most every
# PROBE_INTERVAL seconds per login (or "unscoped" when none resolves) and model family,
# whatever the outcome of the last attempt. The probe model follows the window that
# holds: the shipped minimal probe alias for an all-model window, the limited
# family alias or exact model for a model-scoped one.
# Each limit is judged on its own (`_verdict`): an answer that went through later
# on a model its window covers lifts it, and a rejection holds the models its window
# covers until its own reset. A later rejection never revives a limit an earlier
# answer lifted: the entry retains successes per model and rejections per window.
# Gauges and other readers honor stored answers and never send one.
# No credential bytes are read here: the CLI authenticates from the credential
# source worker homes link.
PROBE_INTERVAL = 600
PROBE_TIMEOUT = 30
PROBE_SCHEMA = 1
# The CLI the probe runs. HEARTING_CAPACITY_PROBE_CLI replaces it for one environment,
# as HARNESS_CAPACITY_SCORES replaces the live gauges: tests point it at a fake or at nothing.
PROBE_CLI = "claude"
PROBE_PROMPT = "Reply with the single word OK."
PROBE_ARGS = ("-p", PROBE_PROMPT, "--output-format", "stream-json", "--verbose", "--setting-sources", "",
              "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands",
              "--tools", "", "--no-session-persistence")
UNSCOPED = "unscoped"
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")
_LATEST_EPOCH = 4102444800  # 2100-01-01: anything later in a stored answer is damage


def _probe_path(jobs):
    return Path(f"{os.path.abspath(jobs)}.capacity-probe.json")


_FAMILIES = tuple(window.removeprefix("seven_day_") for window in WINDOWS if window.startswith("seven_day_"))


def _probe_aliases():
    # Provider request aliases belong to adapter configuration, not worker quality
    # tiers. Read shipped defaults so old user config files need no migration.
    from model_config import parse_config, shipped_path
    return dict(part.split(":", 1) for part in
                parse_config(shipped_path("claude"))["CFG_CAPACITY_PROBE_MODELS"].split())


def _family(model):
    """The cadence key of a model: its quota family when it names one, else the model itself."""
    lowered = model.lower()
    return next((name for name in (*_FAMILIES, _probe_aliases()["all"]) if name in lowered), model)


def _probe_model(held, reader_model, env):
    """Choose from the holding window, independently of the requested worker profile."""
    scopes = sorted({item["scope"] for item in held})
    scope = "all" if "all" in scopes else next(iter(scopes), "")
    model = scope.removeprefix("model:") if scope.startswith("model:") else _probe_aliases().get(scope)
    return model if isinstance(model, str) and _MODEL_NAME.fullmatch(model) else None


def _login_scope(env):
    """The quota scope of the login a worker would use: its account, or UNSCOPED while the
    account cannot be resolved (the CLI still authenticates it). None for API/provider
    configuration, which is not subscription quota."""
    if _api_configured(env):
        return None
    return _scope_candidates("claude", env).get("claude-subscription-v2") or UNSCOPED


def _epoch_value(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
            and 0 <= value <= _LATEST_EPOCH)


def _valid_probe(entry):
    """A stored answer: when it was given, whether the request went through, its reset, and
    for a rejection the latest earlier answer that went through (`ok_at`)."""
    return (isinstance(entry, dict) and isinstance(entry.get("ok"), bool) and _epoch_value(entry.get("at"))
            and (entry.get("reset_epoch") is None or _epoch_value(entry["reset_epoch"]))
            and (entry.get("ok_at") is None or _epoch_value(entry["ok_at"]))
            and isinstance(entry.get("model_scope", "all"), str))


def _read_probes(jobs):
    try:
        data = json.loads(_probe_path(jobs).read_text())
    except (OSError, ValueError, UnicodeError):
        return {}
    probes = data.get("probes") if isinstance(data, dict) and data.get("schema") == PROBE_SCHEMA else None
    return probes if isinstance(probes, dict) else {}


def _probe_due(entry, now):
    """No probe for this login and model family was tried within PROBE_INTERVAL, whatever
    its outcome (an answer, a timeout, a missing CLI, an auth error)."""
    tried = entry.get("tried", entry.get("at")) if isinstance(entry, dict) else None
    return not _epoch_value(tried) or now - tried >= PROBE_INTERVAL


def _write_probe(jobs, key, entry, now):
    """Keep bounded observations per key beside jobs.log: 0600, atomic replace.

    Successful observations may suppress legacy holds beyond the native eight-day
    horizon, so age alone cannot discard them without reviving an old hold.
    """
    path = _probe_path(jobs)
    probes = _read_probes(jobs)
    probes[key] = entry
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w") as handle:
            json.dump({"schema": PROBE_SCHEMA, "probes": probes}, handle, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, path)
        tmp = None
        return True
    except OSError:
        return False
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def probe(env=None, *, model, now=None):
    """Send one minimal request through the caller's Claude login.

    `{"at", "ok", "reset_epoch", "window", "model_scope"}`, or None when the answer says
    nothing about quota (the CLI is missing, timed out, or failed for another reason).
    """
    return _probe_once(env, model, time.time() if now is None else now)


def _probe_once(env, model, now):
    env = {k: v for k, v in (os.environ if env is None else env).items() if k != "CLAUDECODE"}
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    cli = env.get("HEARTING_CAPACITY_PROBE_CLI") or PROBE_CLI
    try:
        with tempfile.TemporaryDirectory(prefix="capacity-probe-") as cwd:
            done = subprocess.run([cli, *PROBE_ARGS, "--model", model], cwd=cwd, env=env,
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):  # includes a timeout
        return None
    rows = []
    for line in done.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    result = next((r for r in reversed(rows) if r.get("type") == "result"), None)
    info = next((r.get("rate_limit_info") for r in reversed(rows) if r.get("type") == "rate_limit_event"), None)
    info = info if isinstance(info, dict) else {}
    window = info.get("rateLimitType") if isinstance(info.get("rateLimitType"), str) else None
    # Same family rule as native_quota; a window it cannot name holds only the probed model.
    if window in WINDOWS:
        model_scope = window.removeprefix("seven_day_") if window.startswith("seven_day_") else "all"
    else:
        model_scope = "model:" + model
    if result is not None and str(result.get("api_error_status", "")) == "429":
        # Quota only when the event itself rejected (as native_quota reads it); an overload or
        # concurrency 429 says nothing about the window.
        if info.get("status") != "rejected" or info.get("isUsingOverage") is True:
            return None
        reset = info.get("resetsAt")
        known = _epoch_value(reset) and now < reset <= now + 8 * 86400
        return {"at": now, "ok": False, "reset_epoch": int(reset) if known else None,
                "window": window, "model_scope": model_scope}
    if result is not None and result.get("is_error") is not False:
        return None  # an auth/transport error is not evidence of restored capacity
    if result is None and (done.returncode != 0 or not (
            info.get("status") in {"allowed", "allowed_warning"} or info.get("isUsingOverage") is True)):
        return None
    return {"at": now, "ok": True, "reset_epoch": None, "window": window, "model_scope": model_scope}


@contextmanager
def _probe_lock(jobs):
    """True while this process alone may probe; False when another one is probing."""
    if fcntl is None:
        yield False
        return
    fd = None
    try:
        fd = os.open(f"{_probe_path(jobs)}.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if fd is not None:
            os.close(fd)
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _answers(probes, scope):
    """Valid observations for this login, including the initial sidecar format."""
    prefix = scope + ":"
    answers = []
    for key, entry in probes.items():
        if not isinstance(key, str) or not key.startswith(prefix) or not isinstance(entry, dict):
            continue
        if isinstance(entry.get("answers"), list):
            answers.extend(a for a in entry["answers"] if _valid_probe(a)
                           and isinstance(a.get("model"), str) and _MODEL_NAME.fullmatch(a["model"]))
            continue
        if not _valid_probe(entry):
            continue
        model = entry.get("model") or key[len(prefix):]
        answers.append({**entry, "model": model})
        if not entry["ok"] and entry.get("ok_at") is not None:
            answers.append({"at": entry["ok_at"], "ok": True, "reset_epoch": None, "window": None,
                            "model_scope": "all", "model": model})
    return answers


def _retain_answers(answers):
    """Latest success per actual model and rejection per window/scope.

    Cadence is family-wide, but a success on one exact model cannot be rewritten
    as a success on another when that family's next check runs.
    """
    latest = {}
    for answer in answers:
        key = ((True, answer["model"]) if answer["ok"] else
               (False, answer.get("window"), answer.get("model_scope", "all")))
        if key not in latest or answer["at"] > latest[key]["at"]:
            latest[key] = answer
    return list(latest.values())


def _held(recorded, answers, reader_model, now):
    """`(recorded items, probe items)` still holding Claude for this reader.

    Each holding item stops holding at its reset, once a later answer went through on a
    model its window covers (an all-model window: any model; a family or model window: that
    one only), or once a later rejection on the same window speaks for it (a legacy marker
    names no window, so any rejection of its scope does).
    """
    oks = [a for a in answers if a["ok"]]
    probed = [{"at": a["at"], "scope": a.get("model_scope", "all"), "reset": a["reset_epoch"],
               "window": a.get("window"), "source": "probe"}
              for a in answers if not a["ok"] and a.get("reset_epoch")
              and _scope_covers(a.get("model_scope", "all"), reader_model)]

    def holds(item):
        if item["reset"] is None or item["reset"] <= now:
            return False
        if any(o["at"] > item["at"] and _scope_covers(item["scope"], o["model"]) for o in oks):
            return False
        return not any(p is not item and p["at"] > item["at"] and p["scope"] == item["scope"]
                       and (not p.get("window") or not item.get("window") or p["window"] == item["window"])
                       for p in probed)

    return [item for item in recorded if holds(item)], [item for item in probed if holds(item)]


def _verdict(recorded, answers, reader_model, now):
    """`(verdict, holder)` for Claude as this reader sees it.

    `recorded` holds Claude now; probe rejections that cover the reader's model hold it too
    (see `_held`). verdict None keeps the recorded state, "ok" lifts it, an epoch holds
    Claude until then. holder: the item that holds longest, None when nothing holds.
    """
    held_recorded, held_probed = _held(recorded, answers, reader_model, now)
    if not held_recorded and not held_probed:
        return ("ok" if recorded else None), None
    holder = max(held_recorded + held_probed, key=lambda item: item["reset"])
    if not held_probed and len(held_recorded) == len(recorded):
        return None, holder
    return int(holder["reset"]), holder


def _recheck(jobs, recorded, models, env, now, allow):
    """`(verdict, holder)` for Claude; `allow` marks a launch decision, which may probe first."""
    if jobs is None:
        return None, None
    try:
        return _recheck_answer(jobs, recorded, models, os.environ if env is None else env, now, allow)
    except Exception:  # noqa: BLE001 - a recheck that fails leaves the recorded state as it was
        return None, None


def _recheck_answer(jobs, recorded, models, env, now, allow):
    if not recorded and not _probe_path(jobs).exists():
        return None, None  # nothing holds Claude and nothing was ever answered
    scope = _login_scope(env)
    if scope is None:
        return None, None
    reader_model = models.get("claude")
    held = _held(recorded, _answers(_read_probes(jobs), scope), reader_model, now)
    model = _probe_model([*held[0], *held[1]], reader_model, env) if allow and any(held) else None
    key = f"{scope}:{_family(model)}" if model else None
    if key and _probe_due(_read_probes(jobs).get(key), now):
        with _probe_lock(jobs) as mine:
            prior = _read_probes(jobs).get(key) if mine else None  # another process may have probed meanwhile
            if mine and _probe_due(prior, now):
                answers = _answers({key: prior}, scope)
                entry = {"tried": now, "answers": answers}
                # Reserve before launch: interruption and failed persistence cannot
                # create another request inside the same interval.
                if not _write_probe(jobs, key, entry, now):
                    return _verdict(recorded, _answers(_read_probes(jobs), scope), reader_model, now)
                result = _probe_once(env, model, now)
                if result is not None:
                    entry["answers"] = _retain_answers([*answers, {**result, "model": model}])
                    _write_probe(jobs, key, entry, now)
    return _verdict(recorded, _answers(_read_probes(jobs), scope), reader_model, now)


_RESET_SENTENCE = re.compile(r"resets\s+([^()\n.·]+?)\s*(?:\(([^()]+)\)|[.·]|$)", re.I)
_TZ_NAME = re.compile(r"[A-Za-z0-9_+/-]+")
TAIL_BYTES = 64 * 1024


def _text_reset_epoch(text, observed, env, tz=None):
    """Epoch for a reset written as a clock or dated text; None when it cannot be anchored.

    A timezone reaches `date` only through its environment, never through a shell.
    """
    if text in {"", "-", "unknown", "unknown-reset"}:
        return None
    if tz and _TZ_NAME.fullmatch(tz):
        env = {**env, "TZ": tz}
    normalized = re.sub("noon", "12pm", text, flags=re.I)
    normalized = re.sub("midnight", "12am", normalized, flags=re.I)
    try:
        clock = bool(re.fullmatch(r"[0-9]{1,2}(?::[0-9]{2})?\s*(?:am|pm)?", normalized, re.I))
        if clock:
            day = subprocess.run(["date", "-d", f"@{int(observed)}", "+%Y-%m-%d"], capture_output=True, text=True, timeout=2, env=env)
            normalized = day.stdout.strip() + " " + normalized
        elif not re.search(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", normalized):
            raise ValueError("unanchored-reset")
        parsed = subprocess.run(["date", "-d", normalized, "+%s"], capture_output=True, text=True, timeout=2, env=env)
        expires = int(parsed.stdout.strip()) if parsed.returncode == 0 else None
        if expires is not None and expires < observed and clock:
            expires += 86400
        return expires
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _result_reset(meta):
    """`(reset text, timezone)` from the last result line of the attempt's own log tail."""
    attempt, log = meta.get("attempt_id", ""), meta.get("log_file", "")
    if not re.fullmatch(r"att-[a-zA-Z0-9-]+", attempt) or attempt not in Path(log).name:
        return "-", None
    try:
        with Path(log).open("rb") as f:
            start = max(0, f.seek(0, 2) - TAIL_BYTES)
            f.seek(start)
            lines = f.read().splitlines()
        if start:
            lines = lines[1:]
    except OSError:
        return "-", None
    for line in reversed(lines):
        if b'"type"' not in line or b'"result"' not in line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(row, dict) and row.get("type") == "result":
            found = _RESET_SENTENCE.search(row.get("result") if isinstance(row.get("result"), str) else "")
            return (found.group(1).strip(), found.group(2)) if found else ("-", None)
    return "-", None


def _cached_result_reset(snapshot, record, meta):
    """`_result_reset(meta)`, reused while the attempt log's stat is unchanged."""
    current = _stat_key(meta.get("log_file", ""))
    cached = record.get("rr")
    if cached is not None and cached[2] == current:
        return cached[0], cached[1]
    value = _result_reset(meta)
    if _stat_key(meta.get("log_file", "")) == current and _settled(current):
        record["rr"] = [value[0], value[1], current]
        snapshot["dirty"] = True
    return value


def _usage(jobs, *, profile=None, models=None, unknown_window_min=60, now=None, env=None, recheck=False):
    """`(states, epochs)`: the usage state per harness and, when known, when it ends."""
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    models = _limit_models(profile, models, env)
    with _snapshot_guard(jobs, now) as snapshot:
        states, epochs, recorded = _usage_snapshot(
            snapshot, models=models, unknown_window_min=unknown_window_min, now=now, env=env)
    _hold_from_gauge(states, epochs, gauge_limits(now))
    if not states["opencode"].startswith("limited("):
        model_state = _opencode_model_state(profile, env)
        if model_state:
            states["opencode"] = model_state
    if states["claude"] == "unknown":
        return states, epochs
    # Outside the snapshot guard: a probe takes seconds and must not hold the cache lock.
    verdict, _holder = _recheck(jobs, recorded, models, env, now, recheck)
    if verdict == "ok":
        states["claude"] = "ok"
        epochs.pop("claude", None)
    elif verdict is not None:
        states["claude"] = f"limited({_iso(verdict)})"
        epochs["claude"] = verdict
    return states, epochs


def _usage_snapshot(snapshot, *, models, unknown_window_min, now, env):
    """`(states, epochs, recorded)`; `recorded` lists the evidence holding Claude now as
    `_verdict` items (legacy markers hold every model)."""
    if snapshot is None:
        return dict.fromkeys(HARNESSES, "unknown"), {}, []
    states = dict.fromkeys(HARNESSES, "ok")
    epochs = {}
    recorded = []
    scopes = _scope_candidates("claude", env)
    native = _native_observations(snapshot, env, now)
    native_ids = {r["attempt_id"] for r in native}
    legacy = {}
    for record in snapshot["legacy"]:
        harness = record["h"]
        # A native event owns its account/model/window even after reset or
        # account change. A lossy text marker cannot widen it back to global.
        if record["a"] in native_ids:
            continue
        if record["s"] and (harness != "claude" or record["s"] != scopes.get(record["k"])):
            continue
        if harness not in legacy or record["t"] > legacy[harness]["t"]:
            legacy[harness] = record
    for harness, record in legacy.items():
        try:
            observed = datetime.fromisoformat(record["t"].replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        reset, tz = record["r"], None
        if reset in {"", "-", "unknown", "unknown-reset"} and record["n"] == "dead-capacity":
            meta = {k: v for k, v in (("attempt_id", record["a"]), ("log_file", record["l"])) if v is not None}
            reset, tz = _cached_result_reset(snapshot, record, meta)
        expires = _text_reset_epoch(reset, observed, env, tz)
        if expires is not None:
            if now < expires:
                # A sentence-derived reset has no row label of its own; name the moment instead.
                label = reset if tz is None else _iso(expires)
                states[harness] = f"limited({label})"
                epochs[harness] = expires
                if harness == "claude":
                    recorded.append({"at": observed, "scope": "all", "reset": expires, "source": "legacy"})
        elif 0 <= now - observed < unknown_window_min * 60:
            states[harness] = "limited(unknown-reset)"
            epochs[harness] = int(observed + unknown_window_min * 60)
            if harness == "claude":
                recorded.append({"at": observed, "scope": "all", "reset": epochs[harness], "source": "legacy"})
    limits = _select_limits(native, models)
    epochs.update({harness: limit["reset_epoch"] for harness, limit in limits.items()})
    recorded += _native_items(_admitted(native, models))
    _persist(snapshot)
    return apply_limits(states, limits), epochs, recorded


def usage_states(jobs, *, profile=None, models=None, unknown_window_min=60, now=None, env=None, recheck=False):
    """One read contract for scoped native proof and legacy text compatibility.

    `recheck=True` marks a launch decision: while Claude is held, it may send one probe
    first (see PROBE_INTERVAL). Every reader honors stored probe answers.
    """
    return _usage(jobs, profile=profile, models=models, unknown_window_min=unknown_window_min,
                  now=now, env=env, recheck=recheck)[0]


def harness_hold(jobs, harness, *, model=None, now=None, env=None, unknown_window_min=60, recheck=False):
    """`{'until_epoch', 'label'}` while the usage gate reports this harness limited, else None."""
    # Only a Claude decision sends a probe; another harness's decision must not wait on one.
    states, epochs = _usage(jobs, models={harness: model} if model else None,
                            unknown_window_min=unknown_window_min, now=now, env=env,
                            recheck=recheck and harness == "claude")
    state = states.get(harness, "ok")
    if not state.startswith("limited("):
        return None
    return {"until_epoch": epochs.get(harness), "label": state[len("limited("):-1]}


def attempt_model(jobs, attempt_id, *, now=None):
    """The model a Claude attempt that ended at a limit was launched with, when the registry kept it."""
    now = time.time() if now is None else now
    with _snapshot_guard(jobs, now) as snapshot:
        records = snapshot["native"] if snapshot else []
    return next((r["m"] for r in records if r["a"] == attempt_id and r.get("m")), None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "states"))
    parser.add_argument("--jobs")
    parser.add_argument("--harness", choices=(*HARNESSES, "all"), default="all")
    parser.add_argument("--unknown-window-min", "--window-min", type=int, default=int(os.environ.get("UNKNOWN_WINDOW_MIN", "60")))
    parser.add_argument("--model-profile")
    parser.add_argument("--recheck", action="store_true",
                        help="launch decision: recheck a holding Claude limit with one probe (at most every 10 minutes)")
    args = parser.parse_args()
    if not args.jobs:
        args.jobs = os.environ.get("AGENT_DISPATCH_JOBS")
    if not args.jobs:
        root = subprocess.run([str(Path(__file__).with_name("dispatch-state-root.sh"))], capture_output=True, text=True, check=True).stdout.strip()
        args.jobs = str(Path(root) / "jobs.log")
    if args.action == "inspect":
        print(json.dumps(observations(args.jobs), sort_keys=True))
    else:
        states = usage_states(args.jobs, profile=args.model_profile, unknown_window_min=args.unknown_window_min,
                              recheck=args.recheck)
        for harness in HARNESSES if args.harness == "all" else [args.harness]:
            print(harness, states[harness])
        print("bias", os.environ.get("HARNESS_CAPACITY_BIAS", "auto"))


if __name__ == "__main__":
    main()
