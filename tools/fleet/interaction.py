"""Privacy-minimal, cross-harness interaction-wait sidecars.

Only a fixed allowlist of scalar metadata can reach disk.  Prompt text, answer
choices, commands, arguments, denial reasons, model output, and tool payloads
have no field in this schema.  Producers are observational and fail open.

State lives below ``FLEET_INTERACTION_STATE_DIR`` when set, otherwise below
``${XDG_STATE_HOME:-~/.local/state}/agent-fleet/interactions``.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import time


SCHEMA_VERSION = 1
WAIT_KINDS = ("decision", "approval", "permission", "elicitation")
WAIT_SOURCES = (
    "claude-asktool",
    "claude-permission",
    "codex-permissionrequest",
    "codex-rollout",
    "codex-appserver",
    "opencode-question",
    "opencode-form",
    "opencode-permission",
)
_BASE_KEYS = frozenset(
    ("schema_version", "harness", "session_id", "kind", "source", "waiting_since")
)
_OPTIONAL_ID_KEYS = ("request_id", "turn_id", "item_id")
_ALLOWED_KEYS = _BASE_KEYS | frozenset(_OPTIONAL_ID_KEYS)
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_STALE_SWEEP_SEC = 24 * 3600
_CLOCK_SKEW_SEC = 60


def _safe_key(value, label):
    value = str(value or "")
    if not value or not _SAFE_KEY_RE.fullmatch(value):
        raise ValueError("invalid %s" % label)
    return value


def _safe_native_ref(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value)
    if not value or len(value) > 200 or not _SAFE_KEY_RE.fullmatch(value):
        return None
    return value


def _safe_identity_ref(name, value):
    # JSON-RPC request 501 and request "501" are different requests. Other
    # optional native refs retain the historical string representation.
    if name == "request_id" and type(value) is int:
        return value
    return _safe_native_ref(value)


def state_root():
    explicit = os.environ.get("FLEET_INTERACTION_STATE_DIR")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(xdg, "agent-fleet", "interactions")


def interactions_dir(harness):
    return os.path.join(state_root(), _safe_key(harness, "harness"))


def sidecar_path(session_id, harness):
    return os.path.join(
        interactions_dir(harness), _safe_key(session_id, "session id") + ".json"
    )


def _number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _encode(harness, session_id, kind, source, waiting_since, *,
            request_id=None, turn_id=None, item_id=None):
    if kind not in WAIT_KINDS or source not in WAIT_SOURCES or not _number(waiting_since):
        return None
    value = {
        "schema_version": SCHEMA_VERSION,
        "harness": _safe_key(harness, "harness"),
        "session_id": _safe_key(session_id, "session id"),
        "kind": kind,
        "source": source,
        "waiting_since": float(waiting_since),
    }
    for name, ref in (("request_id", request_id), ("turn_id", turn_id), ("item_id", item_id)):
        if ref is None:
            continue
        safe_ref = _safe_identity_ref(name, ref)
        if safe_ref is None:
            return None
        value[name] = safe_ref
    return value


def _prepare_directory(directory):
    os.makedirs(directory, mode=0o700, exist_ok=True)
    metadata = os.lstat(directory)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise OSError("interaction directory must be an owner directory")
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass


def _atomic_write(path, payload):
    if not _BASE_KEYS.issubset(payload) or not set(payload).issubset(_ALLOWED_KEYS):
        raise ValueError("interaction payload key set violates the allowlist")
    if any(_safe_identity_ref(name, payload[name]) != payload[name]
           for name in _OPTIONAL_ID_KEYS if name in payload):
        raise ValueError("interaction native identity ref is invalid")
    directory = os.path.dirname(path)
    _prepare_directory(directory)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".interaction-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def set_wait(session_id, harness, kind, source, now=None, *,
             request_id=None, turn_id=None, item_id=None):
    """Atomically publish one exact wait marker; every failure is observational."""
    try:
        waiting_since = time.time() if now is None else now
        payload = _encode(harness, session_id, kind, source, waiting_since,
                          request_id=request_id, turn_id=turn_id, item_id=item_id)
        if payload is None:
            return False
        _atomic_write(sidecar_path(session_id, harness), payload)
        return True
    except Exception:
        return False


def clear_wait(session_id, harness, *, request_id=None):
    """Remove only the exact harness/session marker."""
    try:
        if request_id is not None:
            current = read_wait(session_id, harness)
            expected = _safe_identity_ref("request_id", request_id)
            if expected is None or current is None or current.get("request_id") != expected:
                return False
        path = sidecar_path(session_id, harness)
        os.unlink(path)
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False


def read_wait(session_id, harness):
    """Return one validated marker, or ``None`` for missing/foreign/malformed state."""
    try:
        expected_harness = _safe_key(harness, "harness")
        expected_session = _safe_key(session_id, "session id")
        path = sidecar_path(expected_session, expected_harness)
        metadata = os.lstat(path)
        if (
            metadata.st_uid != os.getuid()
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            return None
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        if (not isinstance(payload, dict) or not _BASE_KEYS.issubset(payload)
                or not set(payload).issubset(_ALLOWED_KEYS)):
            return None
        if any(_safe_identity_ref(name, payload[name]) != payload[name]
               for name in _OPTIONAL_ID_KEYS if name in payload):
            return None
        if payload.get("schema_version") != SCHEMA_VERSION:
            return None
        if payload.get("harness") != expected_harness:
            return None
        if payload.get("session_id") != expected_session:
            return None
        if payload.get("kind") not in WAIT_KINDS:
            return None
        if payload.get("source") not in WAIT_SOURCES:
            return None
        if not _number(payload.get("waiting_since")):
            return None
        return dict(payload)
    except Exception:
        return None


def pending_wait(session_id, harness, session_start=None, activity_since=None, now=None):
    """Return a marker only while exact freshness/identity rules still hold."""
    payload = read_wait(session_id, harness)
    if payload is None:
        return None
    current = time.time() if now is None else now
    if not _number(current):
        return None
    waiting_since = payload["waiting_since"]
    if waiting_since > float(current) + _CLOCK_SKEW_SEC:
        return None
    if _number(session_start) and waiting_since < float(session_start):
        return None
    if (
        payload.get("source") != "codex-appserver"
        and _number(activity_since)
        and float(activity_since) > waiting_since
    ):
        return None
    return payload


def sweep(now=None, max_age=_STALE_SWEEP_SEC):
    """Remove old temporary/malformed files; valid wait markers are not age-expired."""
    current = time.time() if now is None else now
    if not _number(current) or not _number(max_age):
        return 0
    removed = 0
    try:
        harnesses = os.listdir(state_root())
    except OSError:
        return 0
    for harness in harnesses:
        directory = os.path.join(state_root(), harness)
        try:
            directory_metadata = os.lstat(directory)
        except OSError:
            continue
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != os.getuid()
        ):
            continue
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not (name.endswith(".json") or name.endswith(".tmp")):
                continue
            path = os.path.join(directory, name)
            try:
                metadata = os.lstat(path)
                if (
                    metadata.st_uid == os.getuid()
                    and stat.S_ISREG(metadata.st_mode)
                    and float(current) - metadata.st_mtime > float(max_age)
                ):
                    if name.endswith(".json") and read_wait(name[:-5], harness) is not None:
                        continue
                    os.unlink(path)
                    removed += 1
            except OSError:
                pass
    return removed
