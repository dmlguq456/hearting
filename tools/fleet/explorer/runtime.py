"""Runtime seam between collected Fleet data and the pure explorer renderer.

The initial implementation deliberately preserves the existing view model and
input behavior. Collection, ordering, and persistence policy can be supplied by
the live adapter without moving I/O into the drawing code.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import stat
import sys
import threading
import time
import unicodedata
import uuid


_CACHE_SCHEMA = 1
_CACHE_LIMIT = 1024 * 1024
_COMPLETE_STATES = frozenset({"complete", "completed", "success", "ok", "observed", "continued"})
_OBSERVED_STATES = frozenset({"complete", "completed", "success", "ok", "observed", "absent"})


class ExplorerRuntime:
    """Behaviour-preserving runtime hooks for the explorer."""

    def __init__(self, state_home=None, *, strict_readiness=False):
        self._identity_generation = object()
        self._identity_detail = None
        self._identity_tags = None
        self._identity_by_tag = None
        self._order_path = None
        self._order_signature = object()
        self._order_value = None
        self._state_home = Path(state_home).expanduser() if state_home is not None else None
        self._strict_readiness = bool(strict_readiness)
        self._cache_context = None
        self._cache_path = None
        self._cached_continuation = None
        self._has_complete = False
        self._persisted_generations = set()
        self._pending_generations = set()
        self._persist_lock = threading.Lock()
        self._writer_threads = []

    @property
    def has_complete(self):
        return self._has_complete

    @property
    def cache_path(self):
        return self._cache_path

    @property
    def cached_continuation(self):
        return self._cached_continuation

    def clear_cached_continuation(self):
        payload, self._cached_continuation = self._cached_continuation, None
        return payload

    def configure_persistence(self, harness_filter=None, *, strict_readiness=None):
        """Bind this runtime to a host, effective user, and normalized harness filter."""
        if strict_readiness is not None:
            self._strict_readiness = bool(strict_readiness)
        if harness_filter is None:
            normalized_filter = None
        else:
            if isinstance(harness_filter, str):
                harness_filter = harness_filter.split(",")
            normalized_filter = sorted({str(item).strip().lower() for item in harness_filter
                                        if str(item).strip()})
        try:
            uid = os.geteuid()
        except AttributeError:
            uid = os.getuid()
        try:
            host = socket.gethostname()
        except OSError:
            host = "unknown-host"
        host = host[:255] if isinstance(host, str) and host else "unknown-host"
        self._cache_context = {"host": host, "uid": uid, "harness_filter": normalized_filter}
        key_material = json.dumps(self._cache_context, sort_keys=True, separators=(",", ":"))
        cache_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:32]
        state_home = self._state_home or Path(
            os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser()
        self._cache_path = state_home / "hearting" / f"fleet2-observation-{cache_key}.json"
        return self._cache_path

    @staticmethod
    def _owner_uid():
        try:
            return os.geteuid()
        except AttributeError:
            return os.getuid()

    def _read_cache(self):
        """Read and strictly validate one private cache file; invalid caches are ignored."""
        if self._cache_path is None or self._cache_context is None:
            return None
        path = self._cache_path
        try:
            before = path.lstat()
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != self._owner_uid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > _CACHE_LIMIT):
                return None
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != self._owner_uid()
                        or stat.S_IMODE(info.st_mode) != 0o600
                        or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
                        or info.st_size > _CACHE_LIMIT):
                    return None
                chunks, remaining = [], _CACHE_LIMIT + 1
                while remaining:
                    chunk = os.read(fd, min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
            finally:
                os.close(fd)
            if len(raw) > _CACHE_LIMIT:
                return None
            envelope = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError):
            return None
        if not isinstance(envelope, Mapping):
            return None
        allowed = {"schema_version", "host", "uid", "harness_filter", "observed_at",
                   "generation", "generation_identity", "source_freshness", "fresh_complete",
                   "all_new", "vm", "detail_continuation"}
        if set(envelope) - allowed:
            return None
        schema = envelope.get("schema_version")
        if isinstance(schema, bool) or not isinstance(schema, int) or schema != _CACHE_SCHEMA:
            return None
        if (not isinstance(envelope.get("host"), str)
                or isinstance(envelope.get("uid"), bool)
                or not isinstance(envelope.get("uid"), int)
                or (envelope.get("harness_filter") is not None
                    and (not isinstance(envelope.get("harness_filter"), list)
                         or not all(isinstance(item, str)
                                    for item in envelope["harness_filter"])))):
            return None
        for key in ("host", "uid", "harness_filter"):
            if envelope.get(key) != self._cache_context[key]:
                return None
        stamp = envelope.get("observed_at")
        generation = envelope.get("generation")
        generation_identity = envelope.get("generation_identity")
        if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                or not math.isfinite(stamp) or stamp < 0
                or isinstance(generation, bool) or not isinstance(generation, int) or generation < 0
                or not isinstance(generation_identity, str) or not generation_identity
                or len(generation_identity) > 256
                or any(unicodedata.category(char) == "Cc" for char in generation_identity)
                or envelope.get("fresh_complete") is not True
                or envelope.get("all_new") is not True):
            return None
        from .view_model import restore_persisted_view_model
        try:
            model = restore_persisted_view_model(envelope.get("vm"))
        except (TypeError, ValueError):
            return None
        freshness = envelope.get("source_freshness")
        if not self._valid_freshness(freshness):
            return None
        continuation = envelope.get("detail_continuation")
        if continuation is not None and not self._valid_continuation(continuation):
            return None
        self._cached_continuation = continuation
        return {"model": model, "observed_at": float(stamp), "generation": generation,
                "generation_identity": generation_identity,
                "source_freshness": freshness}

    @staticmethod
    def _valid_freshness(value):
        if not isinstance(value, Mapping) or len(value) > 64:
            return False
        for name, item in value.items():
            if not isinstance(name, str) or len(name) > 100 or not isinstance(item, Mapping):
                return False
            if set(item) - {"state", "observed_at", "reason"}:
                return False
            if not isinstance(item.get("state"), str) or len(item["state"]) > 32:
                return False
            stamp = item.get("observed_at")
            if stamp is not None and (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                                      or not math.isfinite(stamp)):
                return False
            reason = item.get("reason")
            if reason is not None:
                from .view_model import _safe_text
                if _safe_text(reason, limit=160) != reason:
                    return False
        return True

    @staticmethod
    def _valid_continuation(value):
        """Continuation payloads are absent until the detail contract supplies a bounded DTO."""
        if not isinstance(value, Mapping) or len(value) > 128:
            return False
        # Never accept source snapshots, objects, or arbitrary nested records from disk.
        allowed = {"schema_version", "identity", "binding", "activity", "route_neighbourhood",
                   "fields", "rows", "captured_at"}
        if set(value) - allowed:
            return False
        def bounded(item, depth=0):
            if depth > 8:
                return False
            if item is None or isinstance(item, (bool, int)):
                return not isinstance(item, int) or abs(item) <= 2**53
            if isinstance(item, float):
                return math.isfinite(item)
            if isinstance(item, str):
                return len(item) <= 512 and not any(unicodedata.category(c) == "Cc" for c in item)
            if isinstance(item, (list, tuple)):
                return len(item) <= 512 and all(bounded(part, depth + 1) for part in item)
            if isinstance(item, Mapping):
                return (len(item) <= 128 and all(isinstance(key, str) and len(key) <= 100
                                                and key.lower() not in {"source", "source_records",
                                                                        "raw", "snapshot", "dialogue",
                                                                        "config", "environment", "secret",
                                                                        "token", "password"}
                                                and bounded(part, depth + 1)
                                                for key, part in item.items()))
            return False
        return bounded(value)

    def load_last_complete(self, harness_filter=None):
        """Load a private complete VM before the first live collection is awaited."""
        self.configure_persistence(harness_filter)
        envelope = self._read_cache()
        if envelope is None:
            return None
        return self.prepare_replay(envelope["model"])

    def prepare_replay(self, vm):
        """Reconcile cached display order against the user order file without live identity."""
        from . import order
        from .view_model import ExplorerViewModel
        model = ExplorerViewModel.from_mapping(vm)
        ordering = model.ordering if isinstance(model.ordering, Mapping) else {}
        tag_keys = dict(ordering.get("tag_keys", {}))
        sessions = []
        for row in model.sessions:
            tag = str(row.get("tag", ""))
            session = dict(row)
            session["order_key"] = str(tag_keys.get(tag) or ("replay:" + str(row.get("harness", "")) + ":" + tag))
            sessions.append(session)
        saved = self._load_order(order)
        reconciled = order.reconcile_order(saved, model.repos, sessions)
        ordered_sessions = reconciled["sessions"]
        ordered_tags = [str(row.get("tag", "")) for row in ordered_sessions]
        replay_ordering = {
            "saved": {"repos": list(reconciled["repo_order"]),
                      "sessions": reconciled["session_order"]},
            "repo_keys": list(reconciled["repo_order"]),
            "session_keys": {key: list(value) for key, value in reconciled["session_order"].items()},
            "tag_keys": {tag: str(tag_keys.get(tag) or ("replay:" + tag)) for tag in ordered_tags},
            "repo_by_tag": {str(row.get("tag", "")): str(row.get("repo_id", row.get("repo", "")))
                            for row in ordered_sessions},
            "tags_by_key": {str(tag_keys.get(str(row.get("tag", ""))) or ("replay:" + str(row.get("tag", "")))):
                            str(row.get("tag", "")) for row in ordered_sessions},
            "tag_cycle": ["session:" + tag for tag in ordered_tags],
        }
        return replace(model, repos=tuple(reconciled["repos"]),
                       sessions=tuple(ordered_sessions), ordering=replay_ordering)

    @staticmethod
    def _source_freshness(readiness):
        result = {}
        sources = readiness.get("sources", {}) if isinstance(readiness, Mapping) else {}
        if not isinstance(sources, Mapping):
            return result
        for name, item in list(sources.items())[:64]:
            if not isinstance(name, str) or not isinstance(item, Mapping):
                continue
            state = item.get("state")
            if not isinstance(state, str):
                continue
            record = {"state": state[:32]}
            stamp = item.get("observed_at")
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and math.isfinite(stamp):
                record["observed_at"] = stamp
            reason = item.get("reason")
            if isinstance(reason, str):
                from .view_model import _safe_text
                safe_reason = _safe_text(reason, limit=160)
                if safe_reason is not None:
                    record["reason"] = safe_reason
            result[name[:100]] = record
        return result

    def _cache_token(self, vm, readiness):
        if self._cache_context is None:
            self.configure_persistence()
        if not isinstance(readiness, Mapping) or not self.publication_ready(readiness):
            return None
        if not self.all_new(readiness):
            return None
        stamp = readiness.get("observed_at", getattr(vm, "clock", None))
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            return None
        generation = readiness.get("generation", 0)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            generation = 0
        identity = readiness.get("generation_identity", generation)
        if not isinstance(identity, (str, int, float)) or isinstance(identity, bool):
            identity = generation
        identity = hashlib.sha256(str(identity).encode("utf-8", "replace")).hexdigest()
        return generation, identity, float(stamp)

    def _cache_envelope(self, vm, readiness, detail_continuation=None):
        from .view_model import persistable_view_model
        token = self._cache_token(vm, readiness)
        if token is None:
            return None
        generation, identity, stamp = token
        clean_model = persistable_view_model(vm)
        clean_continuation = None
        if detail_continuation is not None and self._valid_continuation(detail_continuation):
            try:
                clean_continuation = json.loads(json.dumps(
                    detail_continuation, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
            except (TypeError, ValueError):
                clean_continuation = None
        envelope = {
            "schema_version": _CACHE_SCHEMA,
            **self._cache_context,
            "observed_at": stamp,
            "generation": generation,
            "generation_identity": identity,
            "source_freshness": self._source_freshness(readiness),
            "fresh_complete": True,
            "all_new": True,
            "vm": clean_model,
            "detail_continuation": clean_continuation,
        }
        return envelope

    def persist_complete_async(self, vm, readiness, detail_continuation=None):
        """Freeze the safe payload now and write it away from the curses/UI thread."""
        token = self._cache_token(vm, readiness)
        if token is None:
            return None
        with self._persist_lock:
            if token in self._persisted_generations or token in self._pending_generations:
                return None
            self._pending_generations.add(token)
        envelope = self._cache_envelope(vm, readiness, detail_continuation)
        if envelope is None:
            with self._persist_lock:
                self._pending_generations.discard(token)
            return None
        thread = threading.Thread(target=self._write_generation, args=(envelope, token),
                                  name="fleet2-observation-save", daemon=True)
        self._writer_threads.append(thread)
        try:
            thread.start()
        except (RuntimeError, OSError):
            with self._persist_lock:
                self._pending_generations.discard(token)
            return None
        return thread

    def _write_generation(self, envelope, token):
        try:
            written = self._write_cache(envelope)
            if not written:
                existing = self._read_cache()
                written = existing is not None and existing.get("observed_at", 0) >= token[2]
            if written:
                with self._persist_lock:
                    self._persisted_generations.add(token)
        finally:
            with self._persist_lock:
                self._pending_generations.discard(token)

    def wait_for_writes(self, timeout=None):
        """Test seam: wait for the bounded cache writers without making UI shutdown wait."""
        for thread in tuple(self._writer_threads):
            thread.join(timeout)
        self._writer_threads = [thread for thread in self._writer_threads if thread.is_alive()]

    def _write_cache(self, envelope):
        path = self._cache_path
        if path is None:
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory_info = path.parent.lstat()
            if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != self._owner_uid():
                return False
            raw = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
            if len(raw) > _CACHE_LIMIT:
                return False
            lock_path = path.with_suffix(path.suffix + ".lock")
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            lock_fd = os.open(lock_path, flags, 0o600)
            try:
                lock_info = os.fstat(lock_fd)
                if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != self._owner_uid()
                        or stat.S_IMODE(lock_info.st_mode) != 0o600):
                    return False
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                old = self._read_cache()
                if old is not None and old["observed_at"] >= envelope["observed_at"]:
                    return False
                temp_path = path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".tmp")
                backup_path = path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".bak")
                has_backup = False
                if old is not None:
                    os.link(path, backup_path, follow_symlinks=False)
                    has_backup = True
                temp_fd = -1
                try:
                    temp_fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                      getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
                    stream = os.fdopen(temp_fd, "wb")
                    temp_fd = -1
                    with stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.replace(temp_path, path)
                        os.fsync(directory_fd)
                    except OSError:
                        try:
                            if has_backup:
                                os.replace(backup_path, path)
                                has_backup = False
                            else:
                                path.unlink()
                        except OSError:
                            pass
                        return False
                    finally:
                        os.close(directory_fd)
                    if has_backup:
                        try:
                            backup_path.unlink()
                            has_backup = False
                        except OSError:
                            pass
                finally:
                    if temp_fd >= 0:
                        os.close(temp_fd)
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
                    if has_backup:
                        try:
                            backup_path.unlink()
                        except OSError:
                            pass
                return True
            finally:
                os.close(lock_fd)
        except (OSError, ValueError, TypeError, ImportError):
            return False

    def publication_ready(self, readiness):
        if not isinstance(readiness, Mapping):
            return bool(getattr(readiness, "ready", False))
        return bool(readiness.get("ready") and readiness.get("fully_fresh", readiness.get("ready")))

    def all_new(self, readiness):
        if not isinstance(readiness, Mapping):
            return self.publication_ready(readiness)
        return bool(readiness.get("all_new", readiness.get("fully_fresh", readiness.get("ready"))))

    @staticmethod
    def _file_signature(path):
        try:
            info = path.stat()
        except OSError:
            return None
        return (info.st_dev, info.st_ino, info.st_size,
                getattr(info, "st_mtime_ns", int(info.st_mtime * 1e9)),
                getattr(info, "st_ctime_ns", int(info.st_ctime * 1e9)))

    def _load_order(self, order):
        path = order.order_path()
        signature = self._file_signature(path)
        if path != self._order_path or signature != self._order_signature:
            self._order_value = order.load_order(path)
            self._order_path = path
            self._order_signature = signature
        return self._order_value

    def prepare(self, snapshot, detail, *, vm, generation, now):
        """Return the already-built model and collection readiness for this generation."""
        model = vm
        try:
            from . import live, order
            tags = tuple(str(row.get("tag", "")) for row in model.sessions)
            cached = (generation == self._identity_generation and detail is self._identity_detail
                      and tags == self._identity_tags)
            if not cached:
                resolver = live._resolver(snapshot)
                _rows, by_tag = live._session_records(snapshot, now or time.time(), resolver)
                source_rows = tuple(live._records(live._get(snapshot, "sessions", default=())))
                for source in source_rows:
                    explicit = live._text(live._get(source, "session_tag", "tag"))
                    sid = live._text(live._get(source, "session_id", "sid", "id"))
                    if explicit:
                        by_tag.setdefault(explicit, {"session": source, "sid": sid})
                    if sid:
                        by_tag.setdefault(sid, {"session": source, "sid": sid})
                identity_by_tag = {}
                for row in model.sessions:
                    tag = str(row.get("tag", ""))
                    identity = by_tag.get(tag, {})
                    source = identity.get("session")
                    sid = identity.get("sid")
                    harness = str(row.get("harness") or "")
                    if sid:
                        stable = "sid:" + harness + ":" + str(sid)
                    elif source is not None:
                        stable = "process:" + ":".join(str(live._get(source, key, default=""))
                                                           for key in ("harness", "pid", "proc_start", "managed_dir"))
                    else:
                        # Unknown identity remains ephemeral for this generation and is never
                        # persisted as a display-tag key.
                        stable = "unidentified:" + harness + ":" + tag
                    identity_by_tag[tag] = stable
                self._identity_generation = generation
                self._identity_detail = detail
                self._identity_tags = tags
                self._identity_by_tag = identity_by_tag
            sessions = []
            tag_keys, repo_by_tag, tags_by_key = {}, {}, {}
            for row in model.sessions:
                tag = str(row.get("tag", ""))
                stable = self._identity_by_tag[tag]
                session = dict(row)
                session["order_key"] = stable
                sessions.append(session)
                tag_keys[tag] = stable
                repo_by_tag[tag] = str(row.get("repo_id", row.get("repo", "")))
                tags_by_key[stable] = tag
            saved = self._load_order(order)
            reconciled = order.reconcile_order(saved, model.repos, sessions)
            ordered_sessions = reconciled["sessions"]
            ordered_tags = [str(row.get("tag", "")) for row in ordered_sessions]
            ordering = {
                "saved": {"repos": list(reconciled["repo_order"]),
                          "sessions": reconciled["session_order"]},
                "repo_keys": list(reconciled["repo_order"]),
                "session_keys": {key: list(value) for key, value in reconciled["session_order"].items()},
                "tag_keys": tag_keys, "repo_by_tag": repo_by_tag,
                "tags_by_key": tags_by_key,
                "tag_cycle": ["session:" + tag for tag in ordered_tags],
            }
            from .view_model import freeze
            repos_value = tuple(reconciled["repos"])
            sessions_value = tuple(ordered_sessions)
            if (tuple(model.repos) != repos_value or tuple(model.sessions) != sessions_value
                    or model.ordering != freeze(ordering)):
                model = replace(model, repos=repos_value,
                                sessions=sessions_value, ordering=ordering)
        except (AttributeError, TypeError, ValueError):
            # Preserve a valid frame if a third-party snapshot violates an optional
            # ordering identity shape; malformed order files are handled by load_order.
            pass
        readiness = getattr(model, "readiness", None)
        if readiness is None:
            observations = getattr(snapshot, "observations", None)
            if observations is None and isinstance(snapshot, Mapping):
                observations = snapshot.get("observations")
            observations = observations if isinstance(observations, Mapping) else {}
            source_states = {}
            for name, item in observations.items():
                state = item.get("state") if isinstance(item, Mapping) else getattr(item, "state", None)
                reason = item.get("reason") if isinstance(item, Mapping) else getattr(item, "reason", None)
                error = item.get("error") if isinstance(item, Mapping) else getattr(item, "error", None)
                observed_at = item.get("observed_at") if isinstance(item, Mapping) else None
                source_states[str(name)] = {"state": state or "unknown", "reason": reason,
                                            "error": error, "observed_at": observed_at}
                if isinstance(item, Mapping):
                    for flag in ("attempted", "all_new", "continuation_validated"):
                        if isinstance(item.get(flag), bool):
                            source_states[str(name)][flag] = item[flag]
                    for identity_key in ("generation_identity", "identity"):
                        identity = item.get(identity_key)
                        if isinstance(identity, (str, int)) and not isinstance(identity, bool):
                            source_states[str(name)][identity_key] = identity
            pending = tuple(sorted(name for name, item in source_states.items()
                                   if item["state"] == "pending"))
            # The live production route opts into the explicit required-source contract.
            # Older pure fixtures retain their old pending-only seam until marked live.
            if self._strict_readiness:
                expected_usage = (self._cache_context or {}).get("harness_filter")
                if expected_usage is None:
                    expected_usage = ("claude", "codex", "opencode")
                required = ["basic", "required-details", "resource-runs"] + [
                    "usage:" + str(harness) for harness in expected_usage
                ] + ["remote-host"]
                incomplete = []
                for name in required:
                    item = source_states.get(name)
                    state = str((item or {}).get("state") or "unknown").lower()
                    if name == "required-details":
                        complete = (state in _COMPLETE_STATES
                                    and (state != "continued"
                                         or (item or {}).get("continuation_validated") is True))
                    elif name == "remote-host":
                        complete = (state in {"ok", "success", "failed", "absent", "unavailable"}
                                    and ((item or {}).get("attempted") is True
                                         or state in {"ok", "success", "failed"}))
                    elif name == "resource-runs":
                        complete = (state in {"ok", "success", "absent"}
                                    and (item or {}).get("attempted") is True)
                    elif name.startswith("usage:"):
                        # The cache-only lookup itself is complete when it returns ok
                        # or unavailable. Pending HTTP refresh is separately represented.
                        complete = state in (_OBSERVED_STATES | {"unavailable", "failed"})
                    elif name == "basic":
                        complete = state in (_COMPLETE_STATES - {"continued"})
                    else:
                        complete = state in _OBSERVED_STATES
                    if not complete:
                        incomplete.append(name)
                basic_identity = (source_states.get("basic", {}).get("generation_identity")
                                  or source_states.get("basic", {}).get("identity"))
                detail_identity = (source_states.get("required-details", {}).get("generation_identity")
                                   or source_states.get("required-details", {}).get("identity"))
                if basic_identity is None or detail_identity != basic_identity:
                    incomplete.append("required-details")
                pending = tuple(sorted(set(pending) | set(incomplete)))
                ready = not pending
                all_new = ready and all(
                    (source_states[name].get("all_new", True) is True
                     and str(source_states[name].get("state", "")).lower() != "continued")
                    for name in required if name in source_states)
                basic = source_states.get("basic", {})
                generation_identity = basic.get("generation_identity", basic.get("identity", generation))
                observed = [item.get("observed_at") for item in source_states.values()
                            if isinstance(item.get("observed_at"), (int, float))
                            and not isinstance(item.get("observed_at"), bool)
                            and math.isfinite(item["observed_at"])]
                readiness = {"ready": ready, "fully_fresh": ready,
                             "all_new": all_new, "generation": generation,
                             "generation_identity": generation_identity,
                             "pending": pending, "sources": source_states,
                             "observed_at": max(observed) if observed else
                             (now if now is not None else time.time())}
            else:
                # Compatibility for fixture and direct runtime callers. Production
                # configure_persistence(strict_readiness=True) never uses this shortcut.
                readiness = {"ready": not pending, "fully_fresh": not pending,
                             "all_new": not pending, "generation": generation,
                             "pending": pending, "sources": source_states,
                             "observed_at": now if now is not None else time.time()}
        return model, readiness

    def mark_published(self, readiness):
        if self.publication_ready(readiness):
            self._has_complete = True

    @staticmethod
    def _model_settings_module():
        """Load the install model utility in the same module layout as its CLI."""
        import importlib

        install_dir = Path(__file__).resolve().parents[3] / "tools" / "install"
        original_path = list(sys.path)
        try:
            if str(install_dir) not in sys.path:
                sys.path.insert(0, str(install_dir))
            return importlib.import_module("model_settings")
        finally:
            sys.path[:] = original_path

    def _apply_model_action(self, effect, vm):
        """Validate one MODELS hit and pass its structured values to the model utility."""
        from collections.abc import Mapping

        def failed(message):
            return {"model_action": True, "status": "failed",
                    "message": str(message)[:240], "error": str(message)[:240],
                    "models": None}

        if os.environ.get("FLEET_DEMO"):
            return {"model_action": True, "status": "read-only",
                    "message": "MODELS is read-only in demo", "models": None}
        try:
            models = getattr(vm, "models", None)
            if not isinstance(models, Mapping):
                raise ValueError("model configuration is unavailable")
            if not isinstance(effect, (tuple, list)) or not effect or effect[0] != "models.apply":
                raise ValueError("invalid model action")
            if len(effect) == 5:
                _kind, harness, target, amount, dry_run = effect
                if target != "context-window":
                    raise ValueError("unsupported model setting")
                units = models.get("token_units")
                allowed_amounts = set(units.values()) if isinstance(units, Mapping) else set()
                if (not isinstance(amount, int) or isinstance(amount, bool)
                        or amount not in allowed_amounts):
                    raise ValueError("context window is not a displayed choice")
                target_name, requested = "context-window", str(amount)
                variant_observation = None
            elif len(effect) == 7:
                _kind, harness, target, profile, model, effort, dry_run = effect
                profiles = models.get("profiles", ())
                if (target != "profile" or not isinstance(profile, str)
                        or not isinstance(profiles, (list, tuple, set, frozenset))
                        or profile not in profiles):
                    raise ValueError("profile is not a displayed choice")
                choices = models.get("model_choices", {})
                current_profiles = models.get("profile_map", {})
                current = (current_profiles.get(harness, {}).get(profile)
                           if isinstance(current_profiles, Mapping)
                           and isinstance(current_profiles.get(harness), Mapping) else None)
                current_model = (current[0] if isinstance(current, (list, tuple)) and current else None)
                harness_choices = choices.get(harness) if isinstance(choices, Mapping) else None
                if (not isinstance(harness_choices, (list, tuple, set, frozenset))
                        or not isinstance(model, str)
                        or (model not in harness_choices and model != current_model)):
                    raise ValueError("model is not a displayed or current choice")
                efforts = models.get("efforts", ())
                variant_observation = None
                catalog_sources = models.get("catalog_sources")
                source = (catalog_sources.get(harness)
                          if isinstance(catalog_sources, Mapping) else None)
                observed = source.get("variants") if isinstance(source, Mapping) else None
                if harness == "opencode" and isinstance(observed, Mapping):
                    variant_observation = {"state": source.get("state"),
                                           "variants": dict(observed)}
                    model_variants = observed.get(model)
                    if isinstance(model_variants, (list, tuple)) and model_variants:
                        efforts = model_variants
                if (not isinstance(effort, str)
                        or (not isinstance(efforts, (list, tuple, set, frozenset))
                            or effort not in efforts)
                        and not (harness == "opencode" and effort == "runtime-default")):
                    raise ValueError("effort is not a supported choice")
                target_name = "profile/" + profile
                requested = model + "@" + effort
            else:
                raise ValueError("invalid model action shape")
            if harness not in ("claude", "codex", "opencode") or not isinstance(dry_run, bool):
                raise ValueError("invalid model action target")
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            return failed(exc)

        from . import focus
        model_settings = None
        try:
            model_settings = self._model_settings_module()
            model_config = focus.utility("model_config")
            install_dir = str(Path(__file__).resolve().parents[3] / "tools" / "install")
            original_path = list(sys.path)
            try:
                if install_dir not in sys.path:
                    sys.path.insert(0, install_dir)
                result = model_settings.set_model(
                    harness, target_name, requested,
                    runtime=model_config.runtime_home(harness),
                    source_root=Path(__file__).resolve().parents[3],
                    dry_run=dry_run, variant_observation=variant_observation)
            finally:
                sys.path[:] = original_path
            if not isinstance(result, Mapping):
                raise RuntimeError("model utility returned no structured result")
        except Exception as exc:
            status = ("invalid-arguments" if model_settings is not None
                      and isinstance(exc, model_settings.ModelSettingsUsageError) else "failed")
            result = {"status": status, "error": str(exc),
                      "lines": [f"model set: {exc}"]}
        try:
            refreshed = focus.read_model_config()
        except Exception as exc:
            refreshed = None
            refresh_error = str(exc)
        else:
            refresh_error = None
        if not isinstance(result, Mapping):
            result = {"status": "failed", "error": "model utility returned no result",
                      "lines": ["model set: failed"]}
        lines = result.get("lines", ())
        message = next((str(line) for line in lines if line), None)
        if not message:
            message = "model set: " + str(result.get("status", "finished"))
        if refresh_error:
            message += " · refresh failed: " + refresh_error
        return {"model_action": True, "status": result.get("status", "failed"),
                "message": message[:240], "error": result.get("error"),
                "models": refreshed}

    def _apply_allocation_action(self, effect, vm):
        """Validate one DISPATCH ALLOCATION or SUPERVISION hit and pass its typed values to the shared setter."""
        from . import focus

        payload = effect[1] if isinstance(effect, (tuple, list)) and len(effect) > 1 else ()
        steward = bool(payload) and isinstance(payload, (tuple, list)) and all(
            isinstance(item, (tuple, list)) and str(item[0]).startswith("steward.") for item in payload)
        section = "steward" if steward else "allocation"

        def failed(message, status="failed"):
            text = f"{section}: {status}: {message}"[:240]
            return {"model_action": True, "status": status, "message": text, "error": str(message)[:240],
                    "models": None}

        if os.environ.get("FLEET_DEMO"):
            return {"model_action": True, "status": "read-only",
                    "message": f"{section}: read-only in demo", "models": None}
        kinds = {"strategy": str, "window": int, "usage_gate_used_percent": int,
                 "usage_headroom_exponent": int, "depth_affinity_weight": float}
        updates = {}
        try:
            _kind, payload, dry_run = effect
            if not isinstance(dry_run, bool) or not isinstance(payload, (tuple, list)) or not payload:
                raise ValueError("invalid allocation action")
            for name, text in payload:
                parent, _dot, child = str(name).partition(".")
                if steward:
                    if child not in ("check_interval_seconds", "max_supervisors_per_repo"):
                        raise ValueError(f"steward.{child} is not editable here")
                    updates[child] = int(text)
                elif name in kinds:
                    updates[name] = kinds[name](text)
                elif parent == "depth_affinity" and child in ("owner", "worker"):
                    updates[name] = None if text == "none" else str(text)
                elif parent == "harness_weights" and child in ("claude", "codex", "opencode"):
                    updates[name] = float(text)
                else:
                    raise ValueError(f"allocation.{name} is not editable here")
        except (TypeError, ValueError) as exc:
            return failed(exc, "invalid-arguments")
        install_dir = str(Path(__file__).resolve().parents[3] / "tools" / "install")
        original_path = list(sys.path)
        settings = None
        try:
            import importlib
            if install_dir not in sys.path:
                sys.path.insert(0, install_dir)
            settings = importlib.import_module("dispatch_settings")
            result = settings.set_allocation(updates, dry_run=dry_run, section=section)
        except Exception as exc:
            usage = settings is not None and isinstance(exc, settings.DispatchSettingsUsageError)
            return failed(exc, "invalid-arguments" if usage else "failed")
        finally:
            sys.path[:] = original_path
        try:
            refreshed = focus.read_model_config()
        except Exception as exc:
            refreshed, note = None, " · refresh failed: " + str(exc)
        else:
            note = ""
        lines = [str(line) for line in result.get("lines", ()) if line]
        message = (lines[0] if lines else f"{section}: " + str(result.get("status", "finished")))
        if result.get("warnings"):
            message += " · warning: " + "; ".join(result["warnings"])
        return {"model_action": True, "status": result.get("status", "failed"),
                "message": (message + note)[:240], "error": None, "models": refreshed}

    def apply_effects(self, effects, vm):
        """Apply accepted ordering and validated MODELS effects."""
        from . import order
        ordering = getattr(vm, "ordering", None)
        saved = ({"repos": list(ordering.get("repo_keys", ())),
                 "sessions": {str(key): list(value) for key, value in
                              ordering.get("session_keys", {}).items()}}
                 if isinstance(ordering, Mapping) else None)
        result = None
        for effect in effects or ():
            if effect and effect[0] == "models.apply":
                result = self._apply_model_action(effect, vm)
                continue
            if effect and effect[0] == "alloc.apply":
                result = self._apply_allocation_action(effect, vm)
                continue
            if not effect or effect[0] != "reorder" or len(effect) < 5:
                continue
            if saved is None:
                continue
            _name, kind, repo, source, target = effect[:5]
            if kind == "repo":
                saved = order.move_repo(saved, source, target)
            elif kind == "session":
                saved = order.move_session(saved, repo, source, target)
            order.save_order(saved)
            # A drag writes the authoritative order. Reload on the next compose so
            # the cached view follows the same disk-backed path as external changes.
            self._order_signature = object()
        return result

    @staticmethod
    def execute_question_action(effect):
        """Call the shared selected-request API for one typed UI action."""
        if not isinstance(effect, (tuple, list)) or not effect:
            return {"state": "unknown", "reason": "invalid selected request action"}
        kind = effect[0]
        names = {"questions.read": "read_selected_request",
                 "questions.draft": "write_selected_draft",
                 "questions.submit": "submit_selected_response"}
        method_name = names.get(kind)
        if method_name is None:
            return {"state": "unknown", "reason": "unsupported selected request action"}
        if len(effect) < 2 or not isinstance(effect[1], Mapping):
            return {"state": "unknown", "reason": "selected request binding is unavailable"}
        # Read starts from a body-free row locator. Draft and submit carry the
        # selected private envelope, including native transport incarnation/state.
        ref = dict(effect[1])
        if not all(ref.get(key) for key in ("id", "harness", "session_id")):
            return {"state": "unknown", "reason": "selected request has no exact session binding"}
        try:
            import importlib
            module = importlib.import_module("human_wait_state")
            method = getattr(module, method_name, None)
            if not callable(method):
                return {"state": "unknown",
                        "reason": "selected request backend is not available; use the native prompt"}
            if kind == "questions.read":
                result = method(ref)
            elif kind == "questions.draft":
                if len(effect) != 3 or not isinstance(effect[2], str):
                    return {"state": "unknown", "reason": "invalid answer draft"}
                result = method(ref, effect[2])
            else:
                if len(effect) != 3 or not isinstance(effect[2], Mapping):
                    return {"state": "unknown", "reason": "invalid selected response"}
                result = method(ref, dict(effect[2]), action_id=None)
        except Exception:
            return {"state": "unknown", "reason": "selected request backend failed; use the native prompt"}
        if not isinstance(result, Mapping):
            return {"state": "unknown", "reason": "selected request backend returned no status"}
        return dict(result)

    def loading_frame(self, cols, rows):
        """Return a small terminal-safe loading frame while a source is pending."""
        from .frame import CellFrame
        from .grid import CellGrid
        grid = CellGrid(cols, rows)
        if rows > 0:
            label = "불러오는 중"
            x, y = max(0, (cols - len(label)) // 2), max(0, rows // 2)
            grid.put(max(0, x - 2), y, "⠏", "g_spin")    # turned at draw time, like a working mark
            grid.put(x, y, label, "dim")
        return CellFrame(grid.snapshot(), tuple(), int(cols), int(rows), "OVERVIEW", "loading")

    def collection_options(self, first_complete):
        """Keep basic observations fast; required detail publication owns completeness."""
        return {"fast_first": True,
                "include_report_verification": False}


def tag_cycle(ordering):
    """Read a supplied ordering cycle without imposing an ordering policy."""
    if isinstance(ordering, Mapping):
        value = ordering.get("tag_cycle", ())
    else:
        value = getattr(ordering, "tag_cycle", ())
    if callable(value):
        value = value()
    return tuple(value or ())
