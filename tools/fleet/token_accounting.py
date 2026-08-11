"""Content-free, bounded token-budget accounting primitives.

The aggregate records observations only. It never stores session ids, prompts,
responses, transcripts, or directive bodies, and every public write path is
fail-open so accounting cannot affect production hook output.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


ACCOUNTING_VERSION = 1
MAX_FILE_BYTES = 8 * 1024
MAX_FILES = 256
MAX_DIRECTORY_BYTES = 2 * 1024 * 1024
LOCK_TIMEOUT_SECONDS = 0.5
LOCK_STALE_SECONDS = 30
ZERO_REASONS = (
    "normal",
    "unknown",
    "native",
    "same_band",
    "degraded",
    "timeout_or_error",
)
DIRECTIVE_IDS = ("tight-v1", "critical-v1")
AGGREGATE_FIELDS = (
    "accounting_version",
    "adapter",
    "session_digest",
    "first_observed_at",
    "last_observed_at",
    "hook_invocations",
    "zero_injections",
    "emissions",
    "zero_reason_counts",
    "directive_utf8_bytes_total",
    "directive_utf8_bytes_max",
    "observed_session_token_samples",
    "observed_session_total_tokens_first",
    "observed_session_total_tokens_last",
    "observed_session_token_delta_monotonic",
    "counter_decrease_events",
    "unavailable_token_samples",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def session_digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest()[:32]


def token_budget_root(state_dir: str | Path | None = None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "hearting" / "token-budget"


def accounting_dir(state_dir: str | Path | None = None) -> Path:
    return token_budget_root(state_dir) / "accounting"


def accounting_path(session_id: str, state_dir: str | Path | None = None) -> Path:
    return accounting_dir(state_dir) / f"{session_digest(session_id)}.json"


def canonical_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def empty_aggregate(*, adapter: str, digest: str, observed_at: str) -> dict[str, Any]:
    return {
        "accounting_version": ACCOUNTING_VERSION,
        "adapter": adapter,
        "session_digest": digest,
        "first_observed_at": observed_at,
        "last_observed_at": observed_at,
        "hook_invocations": 0,
        "zero_injections": 0,
        "emissions": 0,
        "zero_reason_counts": {reason: 0 for reason in ZERO_REASONS},
        "directive_utf8_bytes_total": 0,
        "directive_utf8_bytes_max": 0,
        "observed_session_token_samples": 0,
        "observed_session_total_tokens_first": None,
        "observed_session_total_tokens_last": None,
        "observed_session_token_delta_monotonic": 0,
        "counter_decrease_events": 0,
        "unavailable_token_samples": 0,
    }


def _non_negative_int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _timestamp_epoch(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _valid_aggregate(value: Any, *, adapter: str, digest: str) -> bool:
    if not isinstance(value, dict):
        return False
    if set(value) != set(AGGREGATE_FIELDS):
        return False
    if value.get("accounting_version") != ACCOUNTING_VERSION:
        return False
    if value.get("adapter") != adapter or value.get("session_digest") != digest:
        return False
    first_observed = _timestamp_epoch(value.get("first_observed_at"))
    last_observed = _timestamp_epoch(value.get("last_observed_at"))
    if first_observed is None or last_observed is None or last_observed < first_observed:
        return False
    counters = (
        "hook_invocations", "zero_injections", "emissions",
        "directive_utf8_bytes_total", "directive_utf8_bytes_max",
        "observed_session_token_samples", "observed_session_token_delta_monotonic",
        "counter_decrease_events", "unavailable_token_samples",
    )
    if any(_non_negative_int(value.get(key)) is None for key in counters):
        return False
    if value["hook_invocations"] != value["zero_injections"] + value["emissions"]:
        return False
    if (value["observed_session_token_samples"] + value["unavailable_token_samples"]
            != value["hook_invocations"]):
        return False
    reasons = value.get("zero_reason_counts")
    valid = (
        isinstance(reasons, dict)
        and set(reasons) == set(ZERO_REASONS)
        and all(_non_negative_int(reasons[key]) is not None for key in ZERO_REASONS)
    )
    if not valid:
        return False
    if sum(reasons.values()) != value["zero_injections"]:
        return False
    if (value["directive_utf8_bytes_max"] > 240
            or value["directive_utf8_bytes_max"] > value["directive_utf8_bytes_total"]
            or value["directive_utf8_bytes_total"] > value["emissions"] * 240):
        return False
    if value["emissions"] == 0:
        if value["directive_utf8_bytes_total"] != 0 or value["directive_utf8_bytes_max"] != 0:
            return False
    elif value["directive_utf8_bytes_total"] == 0 or value["directive_utf8_bytes_max"] == 0:
        return False
    first = value.get("observed_session_total_tokens_first")
    last = value.get("observed_session_total_tokens_last")
    if value["observed_session_token_samples"] == 0:
        return (first is None and last is None
                and value["observed_session_token_delta_monotonic"] == 0
                and value["counter_decrease_events"] == 0)
    if (_non_negative_int(first) is None or _non_negative_int(last) is None
            or last < first
            or value["observed_session_token_delta_monotonic"] != last - first
            or value["counter_decrease_events"] >= value["observed_session_token_samples"]):
        return False
    return True


def reduce_accounting(aggregate: dict[str, Any], event: dict[str, Any], *, observed_at: str) -> dict[str, Any]:
    """Apply one validated lifecycle event and preserve the accounting identity."""

    outcome = event.get("outcome")
    reason = event.get("zero_reason")
    directive_id = event.get("directive_id")
    directive_bytes = _non_negative_int(event.get("directive_utf8_bytes"))
    if outcome not in {"zero", "emission"} or directive_bytes is None:
        raise ValueError("invalid accounting outcome")
    if outcome == "zero":
        if reason not in ZERO_REASONS or directive_bytes != 0 or directive_id is not None:
            raise ValueError("invalid zero accounting event")
    elif reason is not None or directive_id not in DIRECTIVE_IDS or not 0 < directive_bytes <= 240:
        raise ValueError("invalid emission accounting event")

    result = json.loads(json.dumps(aggregate))
    result["last_observed_at"] = observed_at
    result["hook_invocations"] += 1
    if outcome == "zero":
        result["zero_injections"] += 1
        result["zero_reason_counts"][reason] += 1
    else:
        result["emissions"] += 1
        result["directive_utf8_bytes_total"] += directive_bytes
        result["directive_utf8_bytes_max"] = max(result["directive_utf8_bytes_max"], directive_bytes)

    sample = _non_negative_int(event.get("session_total_tokens"))
    if sample is None:
        result["unavailable_token_samples"] += 1
    else:
        result["observed_session_token_samples"] += 1
        last = result.get("observed_session_total_tokens_last")
        if not isinstance(last, int):
            result["observed_session_total_tokens_first"] = sample
            result["observed_session_total_tokens_last"] = sample
        elif sample < last:
            result["counter_decrease_events"] += 1
        else:
            result["observed_session_token_delta_monotonic"] += sample - last
            result["observed_session_total_tokens_last"] = sample

    if result["hook_invocations"] != result["zero_injections"] + result["emissions"]:
        raise ValueError("accounting identity violated")
    return result


class DirectoryLock:
    """Portable bounded lock based on atomic directory creation."""

    def __init__(self, directory: Path, timeout_seconds: float = LOCK_TIMEOUT_SECONDS):
        self.path = directory / ".lock"
        self.timeout_seconds = timeout_seconds
        self.acquired = False

    def __enter__(self):
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self.path.mkdir()
                self.acquired = True
                return self
            except FileExistsError:
                try:
                    stale = time.time() - self.path.stat().st_mtime > LOCK_STALE_SECONDS
                except OSError:
                    stale = False
                if stale:
                    try:
                        self.path.rmdir()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("token accounting lock timeout")
                time.sleep(0.01)

    def __exit__(self, exc_type, exc, traceback):
        if self.acquired:
            try:
                self.path.rmdir()
            except OSError:
                pass


def _load(path: Path, *, adapter: str, digest: str, observed_at: str) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("oversize aggregate")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return empty_aggregate(adapter=adapter, digest=digest, observed_at=observed_at)
    if not _valid_aggregate(value, adapter=adapter, digest=digest):
        return empty_aggregate(adapter=adapter, digest=digest, observed_at=observed_at)
    return value


def read_accounting(session_id: str, *, adapter: str, state_dir: str | Path | None = None) -> dict[str, Any] | None:
    path = accounting_path(session_id, state_dir)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if _valid_aggregate(value, adapter=adapter, digest=session_digest(session_id)) else None


def _atomic_replace(path: Path, data: bytes) -> None:
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("accounting aggregate exceeds file bound")
    fd, name = tempfile.mkstemp(prefix=".accounting-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _oldest_key(path: Path) -> tuple[float, str]:
    try:
        if path.stat().st_size <= MAX_FILE_BYTES:
            value = json.loads(path.read_text(encoding="utf-8"))
            observed = _timestamp_epoch(value.get("last_observed_at"))
            if observed is not None:
                return observed, path.name
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return mtime, path.name


def _prune(directory: Path) -> None:
    files = [path for path in directory.glob("*.json") if path.is_file()]
    sizes = {}
    for path in files:
        try:
            sizes[path] = path.stat().st_size
        except OSError:
            sizes[path] = 0
    total = sum(sizes.values())
    for path in sorted(files, key=_oldest_key):
        if len(files) <= MAX_FILES and total <= MAX_DIRECTORY_BYTES:
            break
        try:
            path.unlink()
        except OSError:
            continue
        files.remove(path)
        total -= sizes[path]


def record_accounting(session_id: str, event: dict[str, Any], *, adapter: str = "codex",
                      state_dir: str | Path | None = None, observed_at: str | None = None) -> bool:
    """Record exactly one lifecycle event; return false on any fail-open error."""

    try:
        if not isinstance(session_id, str) or not session_id:
            return False
        observed_at = observed_at or utc_now()
        directory = accounting_dir(state_dir)
        directory.mkdir(parents=True, exist_ok=True)
        digest = session_digest(session_id)
        path = directory / f"{digest}.json"
        with DirectoryLock(directory):
            current = _load(path, adapter=adapter, digest=digest, observed_at=observed_at)
            updated = reduce_accounting(current, event, observed_at=observed_at)
            _atomic_replace(path, canonical_bytes(updated))
            _prune(directory)
        return True
    except (OSError, TimeoutError, ValueError, TypeError):
        return False
