#!/usr/bin/env python3
"""Small, process-shared readiness projections owned by the existing runner."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time


SCHEMA_VERSION = 1
DUTY_KIND = "readiness-observation"
MAX_AGE_SECONDS = 15.0
MAX_TARGET_PROJECTIONS = 256
RUNNER_WAKE_NAME = "readiness-wake.fifo"
# Normal selected authority already exceeds 64 KiB with a dozen rows. Keep a
# bounded envelope large enough for extended selections while small projections
# continue to use their existing 64 KiB limit.
MAX_SMALL_JSON_BYTES = 65536
MAX_WATCH_TRANSITION_BYTES = 4 * 1024 * 1024
MAX_WATCH_AUTHORITY_BYTES = MAX_WATCH_TRANSITION_BYTES - MAX_SMALL_JSON_BYTES
_JSON_CHUNK_CHARS = 65536
_CHILD_FIELDS = ("attempt_id", "slug", "status", "readiness", "reason",
                 "required_action")
_WATCH_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DIR_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
              | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))


def attempt_identity(jobs, attempt_id, *, parent="", slug="", consumer_pid,
                     consumer_start, request_token=""):
    """Bind one read request to an exact selection and its live consumer."""
    path = Path(jobs).expanduser().resolve(strict=False)
    return {
        "jobs": str(path),
        "selection": {"attempt_id": str(attempt_id), "parent": str(parent), "slug": str(slug)},
        "consumer": {"pid": int(consumer_pid), "pid_start": str(consumer_start)},
        "request_token": str(request_token),
    }


def duty_id(identity):
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "readiness-" + hashlib.sha256(encoded).hexdigest()


def target_identity(*, server, endpoint, pane, harness, session_id,
                    shell_pid, shell_start, native_state, purpose="completion"):
    try:
        pid = int(shell_pid)
    except (TypeError, ValueError):
        return None
    values = (server, endpoint, pane, harness, shell_start, native_state, purpose)
    if (pid <= 0 or any(not isinstance(value, str) or not value for value in values)
            or not isinstance(session_id, str) or native_state not in {"idle", "done"}):
        return None
    if pane == "-" or harness not in {"claude", "codex", "opencode"}:
        return None
    return {"server": server, "endpoint": endpoint, "pane": pane,
            "harness": harness, "session_id": session_id,
            "shell_pid": pid, "shell_start": shell_start,
            "native_state": native_state, "purpose": purpose}


def legacy_target_identity(*, server, endpoint, pane, harness, session_id,
                           native_state, purpose="completion"):
    """Key an accepted pre-birth watch until the runner proves current lineage.

    This identity deliberately contains no process birth. The runner must attach
    a current ``lineage`` to every projection, and consumers validate that
    lineage against the live endpoint before accepting ready.
    """
    values = (server, endpoint, pane, harness, native_state, purpose)
    if (any(not isinstance(value, str) or not value for value in values)
            or not isinstance(session_id, str)
            or pane == "-" or harness not in {"claude", "codex", "opencode"}
            or native_state not in {"idle", "done"}):
        return None
    return {"server": server, "endpoint": endpoint, "pane": pane,
            "harness": harness, "session_id": session_id,
            "shell_pid": None, "shell_start": "",
            "native_state": native_state, "purpose": purpose}


def _target_path(root, identity):
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    name = hashlib.sha256(encoded).hexdigest() + ".json"
    return Path(root) / "readiness-v1" / name


def bounded_json_size(value, limit):
    """Return the encoded JSON size up to limit without building a large copy."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        return None
    size = 0
    try:
        encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=True)
        for fragment in encoder.iterencode(value):
            for offset in range(0, len(fragment), _JSON_CHUNK_CHARS):
                size += len(fragment[offset:offset + _JSON_CHUNK_CHARS].encode("ascii"))
                if size > limit:
                    return None
        return size
    except (TypeError, ValueError, UnicodeError):
        return None


def _bounded_json_bytes(value, limit):
    """Serialize JSON into a capped buffer; never retain more than the transport bound."""
    size = 0
    raw = bytearray()
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True)
    for fragment in encoder.iterencode(value):
        for offset in range(0, len(fragment), _JSON_CHUNK_CHARS):
            chunk = fragment[offset:offset + _JSON_CHUNK_CHARS].encode("ascii")
            size += len(chunk)
            if size > limit:
                raise ValueError("readiness-projection-too-large")
            raw.extend(chunk)
    return bytes(raw)


def _read_json(path, *, max_bytes=MAX_SMALL_JSON_BYTES):
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return None
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _atomic_json(path, value, *, max_bytes=MAX_SMALL_JSON_BYTES):
    if path.parent.name == "readiness-v1":
        state_root = path.parents[1].resolve(strict=False)
        children = ("readiness-v1",)
    elif (path.parent.name == "watch-transitions"
          and path.parent.parent.name == "readiness-v1"):
        state_root = path.parents[2].resolve(strict=False)
        children = ("readiness-v1", "watch-transitions")
    else:
        raise OSError("readiness-projection-path-invalid")
    root_fd = _open_state_children(state_root, children)
    temporary = None
    fd = None
    try:
        raw = _bounded_json_bytes(value, max_bytes)
        try:
            existing = os.stat(path.name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise OSError("readiness-projection-path-symlink")
        temporary = ".readiness-%s.tmp" % os.urandom(8).hex()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=root_fd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        fd = None
        os.replace(temporary, path.name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)
    except BaseException:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise
    finally:
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=root_fd)
        os.close(root_fd)


def _open_state_children(state_root, children):
    """Create only known child directories under an existing state-root inode."""
    state_root = Path(state_root).resolve(strict=False)
    expected = os.stat(state_root, follow_symlinks=False)
    if not stat.S_ISDIR(expected.st_mode):
        raise OSError("readiness-state-root-unavailable")
    expected_identity = (expected.st_dev, expected.st_ino)
    fd = os.open(state_root, _DIR_FLAGS)
    try:
        if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != expected_identity:
            raise OSError("readiness-state-root-replaced")
        for child in children:
            try:
                next_fd = os.open(child, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(child, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                next_fd = os.open(child, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        current = os.stat(state_root, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != expected_identity:
            raise OSError("readiness-state-root-replaced")
        result = fd
        fd = -1
        return result
    finally:
        if fd >= 0:
            os.close(fd)


def publish_runner_incarnation(root, pid, pid_start, incarnation):
    path = Path(root) / "readiness-v1" / "runner.json"
    _atomic_json(path, {"pid": int(pid), "pid_start": str(pid_start),
                        "incarnation": str(incarnation)})


def _runner_incarnation_current(root, producer):
    marker = _read_json(Path(root) / "readiness-v1" / "runner.json")
    return bool(marker and marker.get("pid") == producer.get("pid")
                and marker.get("pid_start") == producer.get("pid_start")
                and marker.get("incarnation") == producer.get("incarnation"))


def _runner_wake_path(root):
    directory = Path(root)
    try:
        info = os.stat(directory, follow_symlinks=False)
    except OSError as exc:
        raise OSError("readiness-runner-root-unavailable") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("readiness-runner-root-invalid")
    root_fd = os.open(directory, _DIR_FLAGS)
    path = directory / RUNNER_WAKE_NAME
    try:
        try:
            os.stat(RUNNER_WAKE_NAME, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                os.mkfifo(RUNNER_WAKE_NAME, 0o600, dir_fd=root_fd)
            except FileExistsError:
                pass
        info = os.stat(RUNNER_WAKE_NAME, dir_fd=root_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISFIFO(info.st_mode):
            raise OSError("readiness-runner-wake-invalid")
        return path
    finally:
        os.close(root_fd)


def open_runner_wake(root):
    """Open the existing runner's nonblocking event input under its state root."""
    path = _runner_wake_path(root)
    return os.open(path, os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
                   | getattr(os, "O_NOFOLLOW", 0))


def wake_runner(root):
    """Wake a live existing runner after publishing a read demand or watch result."""
    try:
        path = _runner_wake_path(root)
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
                     | getattr(os, "O_NOFOLLOW", 0))
    except (OSError, TypeError, ValueError):
        return False
    try:
        return os.write(fd, b"\0") == 1
    except OSError:
        return False
    finally:
        os.close(fd)


def publish_target_projection(root, identity, result, *, producer_pid,
                              producer_start, incarnation, generation=None,
                              lineage=None, now=None):
    """Atomically publish readiness once for one exact pane/session/birth."""
    state = result.get("state", "unknown") if isinstance(result, dict) else "unknown"
    reason = result.get("reason", "") if isinstance(result, dict) else "invalid-readiness"
    if state not in {"ready", "pending", "unknown"}:
        state = "unknown"
    value = {"schema_version": SCHEMA_VERSION, "identity": identity,
             "producer": {"pid": int(producer_pid), "pid_start": str(producer_start),
                          "incarnation": str(incarnation)},
             "observed_at": time.time() if now is None else float(now),
             "generation": generation,
             "result": {"state": state, "reason": reason if isinstance(reason, str) else ""}}
    if isinstance(lineage, dict):
        try:
            pid = int(lineage.get("shell_pid"))
            start = str(lineage.get("shell_start") or "")
            if pid > 0 and start:
                value["lineage"] = {"shell_pid": pid, "shell_start": start}
        except (TypeError, ValueError):
            pass
    path = _target_path(root, identity)
    previous = _read_json(path)
    changed = not (previous and previous.get("identity") == identity
                   and previous.get("producer") == value["producer"]
                   and previous.get("generation") == generation
                   and previous.get("lineage") == value.get("lineage")
                   and previous.get("result") == value["result"])
    _atomic_json(path, value)
    projections = [candidate for candidate in path.parent.glob("[0-9a-f]*.json")
                   if len(candidate.name) == 69 and not candidate.is_symlink()]
    if len(projections) > MAX_TARGET_PROJECTIONS:
        projections.sort(key=lambda candidate: candidate.stat().st_mtime_ns)
        for stale in projections[:-MAX_TARGET_PROJECTIONS]:
            try:
                stale.unlink()
            except OSError:
                pass
    return changed


def registry_generation(path, view, *, selected=False):
    """Capture a present registry or an explicit, recheckable absence.

    ``None`` is deliberately not a generation: it can mean an unreadable,
    malformed, or otherwise unobserved registry and must never act as a
    wildcard for a ready projection.
    """
    candidate = Path(path)
    try:
        if view is not None:
            signature = list(view.signature)
            resolved = str(view.resolved_path)
            if len(signature) != 5 or not resolved:
                return None
            current = candidate.stat()
            current_signature = [current.st_dev, current.st_ino, current.st_size,
                                 current.st_mtime_ns, current.st_ctime_ns]
            if (str(candidate.resolve(strict=True)) != resolved
                    or current_signature != signature):
                return None
            generation = {"path": str(candidate), "resolved": resolved,
                          "signature": signature}
        else:
            resolved = str(candidate.resolve(strict=False))
            try:
                candidate.lstat()
            except FileNotFoundError:
                generation = {"path": str(candidate), "resolved": resolved,
                              "missing": True}
            else:
                return None
        if selected:
            generation["selection"] = "dispatch-registry"
        return generation
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _generation_current(generation):
    if not isinstance(generation, dict):
        return False
    path = generation.get("path")
    resolved = generation.get("resolved")
    if not isinstance(path, str) or not isinstance(resolved, str):
        return False
    try:
        candidate = Path(path)
        if generation.get("selection") is not None:
            if generation.get("selection") != "dispatch-registry":
                return False
            from dispatch_contract import resolve_agent_home, resolve_dispatch_state_root
            selected = resolve_dispatch_state_root(resolve_agent_home()) / "jobs.log"
            if selected.resolve(strict=False) != candidate.resolve(strict=False):
                return False
        if generation.get("missing") is True:
            if "signature" in generation or str(candidate.resolve(strict=False)) != resolved:
                return False
            try:
                candidate.lstat()
            except FileNotFoundError:
                return True
            return False
        signature = generation.get("signature")
        if not isinstance(signature, list) or len(signature) != 5:
            return False
        current_resolved = str(candidate.resolve(strict=True))
        info = candidate.stat()
        current = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        return current_resolved == resolved and current == signature
    except (OSError, RuntimeError, ValueError):
        return False


def target_result(root, identity, *, now=None, max_age=MAX_AGE_SECONDS):
    """Read a current target projection; missing or stale data stays unknown."""
    if not isinstance(identity, dict):
        return None
    value = _read_json(_target_path(root, identity))
    if (not value or value.get("schema_version") != SCHEMA_VERSION
            or value.get("identity") != identity):
        return None
    producer = value.get("producer")
    if not isinstance(producer, dict) or not _runner_incarnation_current(root, producer):
        return None
    try:
        from dispatch_contract import process_start_ticks
        if str(process_start_ticks(int(producer.get("pid"))) or "") != str(producer.get("pid_start")):
            return None
        age = (time.time() if now is None else float(now)) - float(value.get("observed_at"))
    except (ImportError, OSError, TypeError, ValueError):
        return None
    if age < -60 or age > max_age or not _generation_current(value.get("generation")):
        return None
    result = value.get("result")
    if not isinstance(result, dict) or result.get("state") not in {"ready", "pending", "unknown"}:
        return None
    lineage = value.get("lineage")
    pid = identity.get("shell_pid")
    start = identity.get("shell_start")
    if pid is None:
        if not isinstance(lineage, dict):
            return None
        pid, start = lineage.get("shell_pid"), lineage.get("shell_start")
    try:
        from dispatch_contract import process_start_ticks
        if (int(pid) <= 0 or not start
                or str(process_start_ticks(int(pid)) or "") != str(start)):
            return None
    except (ImportError, OSError, TypeError, ValueError):
        return None
    return {**result, **({"lineage": lineage} if isinstance(lineage, dict) else {})}


def _watch_transition_path(root, watch_id):
    if not isinstance(watch_id, str) or not _WATCH_ID.fullmatch(watch_id):
        raise ValueError("readiness-watch-id-invalid")
    return Path(root) / "readiness-v1" / "watch-transitions" / f"{watch_id}.json"


def valid_watch_authority(authority):
    """Accept registry-shaped rows within the bounded watch transport envelope."""
    if not isinstance(authority, dict):
        return False
    rows = authority.get("rows")
    keys = authority.get("handover_keys")
    if not isinstance(rows, list) or not isinstance(keys, list):
        return False
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("status"), str)
                or not isinstance(row.get("metadata"), dict)
                or not isinstance(row.get("completion_metadata"), dict)
                or not isinstance(row.get("fields"), list) or len(row["fields"]) != 6
                or any(not isinstance(field, str)
                       for field in row["fields"])):
            return False
        for mapping in (row["metadata"], row["completion_metadata"]):
            if any(not isinstance(key, str) or not isinstance(value, str)
                   for key, value in mapping.items()):
                return False
    for key in keys:
        if (not isinstance(key, list) or len(key) != 3
                or any(not isinstance(value, str) for value in key)):
            return False
    return bounded_json_size(authority, MAX_WATCH_AUTHORITY_BYTES) is not None


def publish_watch_transition(root, watch_id, identity, request_token, result, *,
                             producer_pid, producer_start, incarnation,
                             generation=None, lineage=None, authority=None, now=None):
    """Publish the runner's fresh, request-bound authority check for one watch."""
    if (not isinstance(identity, dict) or not isinstance(request_token, str)
            or not request_token or len(request_token) > 128):
        raise ValueError("readiness-watch-transition-invalid")
    state = result.get("state", "unknown") if isinstance(result, dict) else "unknown"
    reason = result.get("reason", "") if isinstance(result, dict) else "invalid-readiness"
    if state not in {"ready", "pending", "unknown"}:
        state = "unknown"
    if authority is not None and not valid_watch_authority(authority):
        state, reason, authority = "unknown", "shared-readiness-authority-unbounded", None
    observed_at = time.time() if now is None else float(now)
    value = {
        "schema_version": SCHEMA_VERSION,
        "watch_id": watch_id,
        "request_token": request_token,
        "identity": identity,
        "producer": {"pid": int(producer_pid), "pid_start": str(producer_start),
                     "incarnation": str(incarnation)},
        "observed_at": observed_at,
        "generation": generation,
        "result": {"state": state, "reason": reason if isinstance(reason, str) else ""},
    }
    if isinstance(lineage, dict):
        try:
            pid = int(lineage.get("shell_pid"))
            start = str(lineage.get("shell_start") or "")
            if pid > 0 and start:
                value["lineage"] = {"shell_pid": pid, "shell_start": start}
        except (TypeError, ValueError):
            pass
    if authority is not None:
        value["authority"] = authority
    if bounded_json_size(value, MAX_WATCH_TRANSITION_BYTES) is None:
        value["result"] = {"state": "unknown",
                           "reason": "shared-readiness-authority-unbounded"}
        value.pop("authority", None)
    path = _watch_transition_path(root, watch_id)
    previous = _read_json(path, max_bytes=MAX_WATCH_TRANSITION_BYTES)
    previous_age = None
    if previous:
        try:
            previous_age = observed_at - float(previous.get("observed_at"))
        except (TypeError, ValueError):
            pass
    changed = not (previous and previous_age is not None
                   and -60 <= previous_age <= MAX_AGE_SECONDS
                   and previous.get("watch_id") == watch_id
                   and previous.get("request_token") == request_token
                   and previous.get("identity") == identity
                   and previous.get("producer") == value["producer"]
                   and previous.get("generation") == generation
                   and previous.get("lineage") == value.get("lineage")
                   and previous.get("result") == value["result"]
                   and previous.get("authority") == value.get("authority"))
    _atomic_json(path, value, max_bytes=MAX_WATCH_TRANSITION_BYTES)
    return changed


def watch_transition_result(root, watch_id, identity, request_token, *,
                            now=None, max_age=MAX_AGE_SECONDS):
    """Read a current per-watch refresh response; mismatches remain unknown."""
    try:
        value = _read_json(_watch_transition_path(root, watch_id),
                           max_bytes=MAX_WATCH_TRANSITION_BYTES)
    except ValueError:
        return None
    if (not value or value.get("schema_version") != SCHEMA_VERSION
            or value.get("watch_id") != watch_id
            or value.get("request_token") != request_token
            or value.get("identity") != identity):
        return None
    producer = value.get("producer")
    if not isinstance(producer, dict) or not _runner_incarnation_current(root, producer):
        return None
    try:
        from dispatch_contract import process_start_ticks
        if str(process_start_ticks(int(producer.get("pid"))) or "") != str(producer.get("pid_start")):
            return None
        age = (time.time() if now is None else float(now)) - float(value.get("observed_at"))
    except (ImportError, OSError, TypeError, ValueError):
        return None
    if age < -60 or age > max_age or not _generation_current(value.get("generation")):
        return None
    result = value.get("result")
    lineage = value.get("lineage")
    authority = value.get("authority")
    if authority is not None and not valid_watch_authority(authority):
        return None
    if not isinstance(result, dict) or result.get("state") not in {"ready", "pending", "unknown"}:
        return None
    pid = identity.get("shell_pid")
    start = identity.get("shell_start")
    if pid is None:
        if not isinstance(lineage, dict):
            return None
        pid, start = lineage.get("shell_pid"), lineage.get("shell_start")
    try:
        from dispatch_contract import process_start_ticks
        if (int(pid) <= 0 or not start
                or str(process_start_ticks(int(pid)) or "") != str(start)):
            return None
    except (ImportError, OSError, TypeError, ValueError):
        return None
    return {**result,
            **({"lineage": lineage} if isinstance(lineage, dict) else {}),
            **({"authority": authority} if authority is not None else {})}


def clear_watch_transition(root, watch_id):
    """Drop one watch's bounded response; its observation keeps the request token for refresh."""
    try:
        _watch_transition_path(root, watch_id).unlink()
    except FileNotFoundError:
        pass


def consumer_is_current(identity):
    consumer = identity.get("consumer") if isinstance(identity, dict) else None
    if not isinstance(consumer, dict):
        return False
    try:
        pid = int(consumer.get("pid"))
        expected = str(consumer.get("pid_start") or "")
        if pid <= 0 or not expected:
            return False
        from dispatch_contract import process_start_ticks
        return str(process_start_ticks(pid) or "") == expected
    except (ImportError, OSError, TypeError, ValueError):
        return False


def make_projection(identity, result, *, producer_pid, producer_start, incarnation,
                    generation=None, now=None):
    """Keep only the small result needed by a caller; never persist full rows."""
    state = str(result.get("state", "unknown")) if isinstance(result, dict) else "unknown"
    reason = result.get("reason", "") if isinstance(result, dict) else "invalid-classification"
    children = result.get("children", ()) if isinstance(result, dict) else ()
    projected_children = []
    if isinstance(children, (list, tuple)):
        for child in children[:16]:
            if isinstance(child, dict):
                projected_children.append({key: child[key] for key in _CHILD_FIELDS
                                           if key in child and isinstance(child[key], (str, int, bool))})
    value = {
        "schema_version": SCHEMA_VERSION,
        "identity": identity,
        "producer": {"pid": int(producer_pid), "pid_start": str(producer_start),
                     "incarnation": str(incarnation)},
        "observed_at": time.time() if now is None else float(now),
        "generation": generation,
        "result": {"state": state, "reason": reason if isinstance(reason, str) else "",
                   "children": projected_children},
    }
    if len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")) > 65536:
        value["result"] = {"state": "unknown", "reason": "readiness-projection-too-large",
                           "children": []}
    return value


def projection_is_current(value, identity, *, now=None, max_age=MAX_AGE_SECONDS):
    """Reject stale or unverifiable producer results as unknown, never ready."""
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return False
    if value.get("identity") != identity or not consumer_is_current(identity):
        return False
    producer = value.get("producer")
    if not isinstance(producer, dict):
        return False
    jobs_root = Path(identity.get("jobs", "")).parent
    obligations = jobs_root / "peer-steward" / "obligations"
    if not _runner_incarnation_current(obligations, producer):
        return False
    try:
        pid = int(producer.get("pid"))
        expected = str(producer.get("pid_start") or "")
        if pid <= 0 or not expected or not producer.get("incarnation"):
            return False
        from dispatch_contract import process_start_ticks
        if str(process_start_ticks(pid) or "") != expected:
            return False
        stamp = float(value.get("observed_at"))
    except (ImportError, OSError, TypeError, ValueError):
        return False
    current = time.time() if now is None else float(now)
    age = current - stamp
    if age < -60 or age > max_age:
        return False
    result = value.get("result")
    return isinstance(result, dict) and result.get("state") in {
        "ready", "pending", "terminal", "contract-error", "unknown"
    }


def current_result(duty, identity, *, now=None, max_age=MAX_AGE_SECONDS):
    """Return a validated tiny result or ``None`` when the runner must reobserve."""
    if not isinstance(duty, dict):
        return None
    intent = duty.get("intent")
    if not isinstance(intent, dict) or intent.get("kind") != DUTY_KIND:
        return None
    if duty.get("id") != duty_id(identity) or intent.get("identity") != identity:
        return None
    observation = duty.get("observation")
    value = observation.get("shared_readiness") if isinstance(observation, dict) else None
    if not projection_is_current(value, identity, now=now, max_age=max_age):
        return None
    return value["result"]
