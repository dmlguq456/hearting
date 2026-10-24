#!/usr/bin/env python3
"""Shared, generation-checked views of the dispatch registry.

The immutable view may be reused while the registry generation is unchanged.
Consumers must still observe their other inputs on each pass and recheck
authority under the existing lock before a state transition.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import codecs
import contextlib
import contextvars
import functools
import hashlib
import os
from pathlib import Path
import threading
import time


_RETRIES = 3
_CHUNK_SIZE = 1024 * 1024
_APPEND_OBSERVATION_GAP_SECONDS = 30.0
_CACHE_MAX_PATHS = 128
_CACHE_MAX_BYTES = 64 * 1024 * 1024
_MAX_SEGMENTS = 256
_COMPACT_SEGMENTS = 128
_NON_CANONICAL_LINE_SEPARATORS = (b"\r", b"\v", b"\f", b"\x1c", b"\x1d",
                                 b"\x1e", b"\xc2\x85", b"\xe2\x80\xa8",
                                 b"\xe2\x80\xa9")
_LOCK = threading.RLock()
_CACHE: OrderedDict[str, "RegistryView"] = OrderedDict()
_READ_SCOPE: contextvars.ContextVar = contextvars.ContextVar("dispatch_registry_read_scope", default=None)
_CACHE_BYTES = 0
_METRICS = {
    "cache_hits": 0,
    "content_reads": 0,
    "bytes_read": 0,
    "delta_reads": 0,
    "delta_bytes_read": 0,
    "full_metadata_parses": 0,
    "metadata_rows_parsed": 0,
    "invalidations": 0,
    "cache_evictions": 0,
    "segment_compactions": 0,
}


def _stat_signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _utf8_status(data: bytes) -> tuple[bool, bool]:
    """Check UTF-8 availability without a Python loop over registry bytes."""
    if data.isascii():
        return False, False
    try:
        _text, consumed = codecs.utf_8_decode(data, "strict", False)
    except UnicodeDecodeError:
        return True, False
    return False, consumed < len(data)


@functools.lru_cache(maxsize=8192)
def _completion_metadata(raw: str) -> tuple[tuple[str, str], ...]:
    values = {}
    for part in raw.split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            values[key] = value
    return tuple(values.items())


@functools.lru_cache(maxsize=8192)
def _handover_metadata(raw: str) -> tuple[tuple[str, str], ...]:
    values = {}
    for part in raw.split(","):
        key, separator, value = part.partition("=")
        if separator and key.strip():
            values[key.strip()] = value
    return tuple(values.items())


@dataclass(frozen=True, slots=True)
class RegistryRecord:
    order: int
    fields: tuple[str, ...]

    @property
    def raw(self) -> str:
        """Compatibility view; avoid retaining a second copy of every row."""
        return "\t".join(self.fields)

    @property
    def status(self) -> str:
        return self.fields[1]

    @property
    def slug(self) -> str:
        return self.fields[4]

    def completion_metadata(self) -> dict[str, str]:
        return dict(_completion_metadata(self.fields[5]))

    def handover_metadata(self) -> dict[str, str]:
        return dict(_handover_metadata(self.fields[5]))


class _RegistrySegment:
    """One append batch and the indexes for rows introduced by that batch."""

    __slots__ = ("lines", "records", "malformed_rows", "completion_latest",
                 "completion_errors", "completion_all", "completion_children",
                 "completion_by_parent_sid",
                 "completion_by_route_alias", "settlement_owners",
                 "handover_latest", "handover_by_parent", "handover_by_binding",
                 "headless_owner_rows", "selection_by_parent", "selection_by_slug",
                 "selection_by_attempt", "selection_by_parent_slug")

    def __init__(self, lines: tuple[str, ...], order_base: int):
        self.lines = lines
        records = []
        completion_latest = {}
        completion_errors = {}
        completion_all = {}
        completion_children = {}
        completion_by_parent_sid = {}
        completion_by_route_alias = {}
        settlement_owners = []
        handover_latest = {}
        selection_by_parent = {}
        selection_by_slug = {}
        selection_by_attempt = {}
        selection_by_parent_slug = {}
        self.malformed_rows = 0
        for relative_order, line in enumerate(lines):
            fields = tuple(line.split("\t"))
            if len(fields) != 6:
                self.malformed_rows += 1
                continue
            record = RegistryRecord(order_base + relative_order, fields)
            records.append(record)
            completion_meta = dict(_completion_metadata(fields[5]))
            handover_meta = dict(_handover_metadata(fields[5]))
            parent = completion_meta.get("parent")
            attempt_id = completion_meta.get("attempt_id")
            if parent:
                selection_by_parent.setdefault(parent, []).append(record)
            selection_by_slug.setdefault(fields[4], []).append(record)
            if attempt_id:
                selection_by_attempt.setdefault(attempt_id, []).append(record)
            if parent:
                selection_by_parent_slug.setdefault((parent, fields[4]), []).append(record)
            attempt = completion_meta.get("attempt_id")
            if attempt:
                completion_all.setdefault(attempt, []).append(record)
                if completion_meta.get("attempt_schema_version") != "2":
                    completion_errors.setdefault(attempt, "attempt-row-schema-invalid")
                else:
                    completion_latest[attempt] = record
            parent_attempt = completion_meta.get("parent_attempt_id")
            if parent_attempt:
                completion_children.setdefault(parent_attempt, []).append(record)
            parent_sid = completion_meta.get("parent_sid")
            if parent_sid:
                completion_by_parent_sid.setdefault(parent_sid, []).append(record)
            for alias in {completion_meta.get(key) for key in
                          ("route_id", "route_hash", "owner_route_id", "owner_route_hash")} - {None, ""}:
                completion_by_route_alias.setdefault(alias, []).append(record)
            if (completion_meta.get("worker_type") == "owner"
                    and completion_meta.get("launch_started") == "1"
                    and completion_meta.get("supervisor_lease") == "flock-v1"):
                settlement_owners.append(record)
            handover_attempt = handover_meta.get("attempt_id")
            if handover_attempt:
                handover_latest[handover_attempt] = (fields[1], record)
        self.records = tuple(records)
        self.completion_latest = completion_latest
        self.completion_errors = completion_errors
        self.completion_all = {key: tuple(value) for key, value in completion_all.items()}
        self.completion_children = {key: tuple(value) for key, value in completion_children.items()}
        self.completion_by_parent_sid = {
            key: tuple(value) for key, value in completion_by_parent_sid.items()
        }
        self.completion_by_route_alias = {
            key: tuple(value) for key, value in completion_by_route_alias.items()
        }
        self.settlement_owners = tuple(settlement_owners)
        self.handover_latest = handover_latest
        self.selection_by_parent = {key: tuple(value) for key, value in selection_by_parent.items()}
        self.selection_by_slug = {key: tuple(value) for key, value in selection_by_slug.items()}
        self.selection_by_attempt = {key: tuple(value) for key, value in selection_by_attempt.items()}
        self.selection_by_parent_slug = {
            key: tuple(value) for key, value in selection_by_parent_slug.items()
        }
        self._index_handover()

    def _index_handover(self):
        self.handover_by_parent = {}
        self.handover_by_binding = {}
        headless = []
        for attempt, (status, record) in self.handover_latest.items():
            metadata = record.handover_metadata()
            parent = metadata.get("parent_sid")
            if parent:
                self.handover_by_parent.setdefault(parent, {})[attempt] = (status, record)
            route = metadata.get("owner_route_id") or metadata.get("route_id") or ""
            digest = metadata.get("owner_route_hash") or metadata.get("route_hash") or ""
            node = metadata.get("route_node") or ""
            if route and digest:     # an owner row has no route node: it binds as (route, hash, "")
                self.handover_by_binding.setdefault((route, digest, node), {})[attempt] = (status, record)
            if (metadata.get("dispatch_depth") == "1" and metadata.get("worker_type") == "owner"
                    and metadata.get("harness") == "opencode"
                    and metadata.get("transport") == "headless"
                    and metadata.get("registered_worker") == "1"
                    and metadata.get("execution_surface") == "registered-headless"):
                headless.append((status, record))
        self.headless_owner_rows = tuple(headless)

    @classmethod
    def compact(cls, segments: tuple["_RegistrySegment", ...]):
        """Merge index deltas without reparsing their registry rows."""
        result = cls.__new__(cls)
        result.lines = tuple(line for segment in segments for line in segment.lines)
        result.records = tuple(record for segment in segments for record in segment.records)
        result.malformed_rows = sum(segment.malformed_rows for segment in segments)
        result.completion_latest = {}
        result.completion_errors = {}
        completion_all = {}
        completion_children = {}
        completion_by_parent_sid = {}
        completion_by_route_alias = {}
        result.handover_latest = {}
        selection_by_parent = {}
        selection_by_slug = {}
        selection_by_attempt = {}
        selection_by_parent_slug = {}
        for segment in segments:
            result.completion_latest.update(segment.completion_latest)
            for attempt, error in segment.completion_errors.items():
                result.completion_errors.setdefault(attempt, error)
            for target, source in ((completion_all, segment.completion_all),
                                   (completion_children, segment.completion_children),
                                   (completion_by_route_alias, segment.completion_by_route_alias),
                                   (completion_by_parent_sid, segment.completion_by_parent_sid)):
                for key, rows in source.items():
                    target.setdefault(key, []).extend(rows)
            result.handover_latest.update(segment.handover_latest)
            for target, source in (
                (selection_by_parent, segment.selection_by_parent),
                (selection_by_slug, segment.selection_by_slug),
                (selection_by_attempt, segment.selection_by_attempt),
                (selection_by_parent_slug, segment.selection_by_parent_slug),
            ):
                for key, rows in source.items():
                    target.setdefault(key, []).extend(rows)
        result.completion_all = {key: tuple(value) for key, value in completion_all.items()}
        result.completion_children = {key: tuple(value) for key, value in completion_children.items()}
        result.completion_by_parent_sid = {
            key: tuple(value) for key, value in completion_by_parent_sid.items()
        }
        result.completion_by_route_alias = {
            key: tuple(value) for key, value in completion_by_route_alias.items()
        }
        result.settlement_owners = tuple(record for segment in segments
                                        for record in segment.settlement_owners)
        result.selection_by_parent = {key: tuple(value) for key, value in selection_by_parent.items()}
        result.selection_by_slug = {key: tuple(value) for key, value in selection_by_slug.items()}
        result.selection_by_attempt = {key: tuple(value) for key, value in selection_by_attempt.items()}
        result.selection_by_parent_slug = {
            key: tuple(value) for key, value in selection_by_parent_slug.items()
        }
        result._index_handover()
        return result


class RegistryView:
    """Immutable segmented registry view with incremental append indexes."""

    __slots__ = ("path_identity", "resolved_path", "signature", "_segments", "_tail",
                 "line_count", "record_count", "malformed_rows", "has_partial_tail",
                 "last_observed_at", "_has_duplicate_attempts", "_invalid_utf8_prefix",
                 "has_invalid_utf8", "_raw_digest", "_prefix_reuse_eligible",
                 "_append_only_generation")

    def __init__(self, path_identity: str, resolved_path: str, signature: tuple,
                 raw: bytes, *, previous: "RegistryView | None" = None):
        self.path_identity = path_identity
        self.resolved_path = resolved_path
        self.signature = signature
        self._append_only_generation = previous is not None
        self.last_observed_at = time.monotonic()
        self._has_duplicate_attempts = None
        self._raw_digest = hashlib.sha256(raw)
        if previous is None:
            invalid_utf8, incomplete_utf8 = _utf8_status(raw)
            self._invalid_utf8_prefix = invalid_utf8
            self.has_invalid_utf8 = invalid_utf8 or incomplete_utf8
            self._prefix_reuse_eligible = (
                not invalid_utf8
                and not any(separator in raw for separator in _NON_CANONICAL_LINE_SEPARATORS)
            )
            boundary = raw.rfind(b"\n") + 1
            complete = raw[:boundary].decode("utf-8", errors="replace").splitlines()
            tail = raw[boundary:]
            segment = _RegistrySegment(tuple(complete), 0)
            self._segments = (segment,) if complete else ()
            self._tail = tail
            self.line_count = len(complete) + (1 if tail else 0)
        else:
            # The caller supplied only bytes appended to a continuously observed,
            # trusted append-only registry. Rejoin its pending UTF-8 byte tail.
            combined = previous._tail + raw
            invalid_utf8, incomplete_utf8 = _utf8_status(combined)
            self._invalid_utf8_prefix = previous._invalid_utf8_prefix or invalid_utf8
            self.has_invalid_utf8 = self._invalid_utf8_prefix or incomplete_utf8
            self._raw_digest = previous._raw_digest.copy()
            self._raw_digest.update(raw)
            self._prefix_reuse_eligible = (
                previous._prefix_reuse_eligible and not invalid_utf8
                and not any(separator in combined for separator in _NON_CANONICAL_LINE_SEPARATORS)
            )
            pieces = combined.split(b"\n")
            complete_bytes, tail = pieces[:-1], pieces[-1]
            complete = tuple(piece.decode("utf-8", errors="replace") for piece in complete_bytes)
            old_partial = bool(previous._tail)
            order_base = previous.line_count - 1 if old_partial and complete else previous.line_count
            new_segments = list(previous._segments)
            appended_segment = None
            if complete:
                appended_segment = _RegistrySegment(complete, order_base)
                new_segments.append(appended_segment)
            if len(new_segments) > _MAX_SEGMENTS:
                compacted = _RegistrySegment.compact(tuple(new_segments[-_COMPACT_SEGMENTS:]))
                new_segments = new_segments[:-_COMPACT_SEGMENTS] + [compacted]
                _METRICS["segment_compactions"] += 1
            self._segments = tuple(new_segments)
            self._tail = tail
            self.line_count = (previous.line_count + len(complete)
                               - (1 if old_partial and complete else 0))
        if previous is None:
            self.record_count = sum(len(segment.records) for segment in self._segments)
            self.malformed_rows = sum(segment.malformed_rows for segment in self._segments)
            self.has_partial_tail = bool(self._tail)
        else:
            added_records = len(appended_segment.records) if appended_segment is not None else 0
            added_malformed = appended_segment.malformed_rows if appended_segment is not None else 0
            self.record_count = previous.record_count + added_records
            self.malformed_rows = previous.malformed_rows + added_malformed
            self.has_partial_tail = bool(self._tail)

    @property
    def lines(self) -> tuple[str, ...]:
        result = tuple(line for segment in self._segments for line in segment.lines)
        if self._tail:
            result += (self._tail.decode("utf-8", errors="replace"),)
        return result

    @property
    def records(self) -> tuple[RegistryRecord, ...]:
        return tuple(record for segment in self._segments for record in segment.records)

    def select_rows(self, *, parent: str = "", slug: str = "", attempt_id: str = ""):
        """Select latest CLI rows from indexes built with this registry generation."""
        candidates = {}
        for segment in self._segments:
            if attempt_id:
                rows = segment.selection_by_attempt.get(attempt_id, ())
            elif parent and slug:
                rows = segment.selection_by_parent_slug.get((parent, slug), ())
            elif parent:
                rows = segment.selection_by_parent.get(parent, ())
            elif slug:
                rows = segment.selection_by_slug.get(slug, ())
            else:
                rows = segment.records
            for record in rows:
                candidates[record.order] = record

        ordered = [(record.order, list(record.fields), record.completion_metadata())
                   for record in candidates.values()]
        if self._tail:
            tail_lines = self._tail.decode("utf-8", errors="replace").splitlines()
            for offset, line in enumerate(tail_lines):
                fields = line.split("\t")
                if len(fields) == 6:
                    metadata = dict(_completion_metadata(fields[5]))
                    ordered.append((self.line_count - 1 + offset,
                                    fields, metadata))

        latest = {}
        for order, fields, metadata in sorted(ordered, key=lambda item: item[0]):
            if parent and metadata.get("parent") != parent:
                continue
            if slug and fields[4] != slug:
                continue
            if attempt_id and metadata.get("attempt_id") != attempt_id:
                continue
            key = (("attempt", metadata["attempt_id"]) if metadata.get("attempt_id")
                   else ("legacy", fields[2], fields[3], fields[4]))
            latest[key] = (fields, metadata)
        return list(latest.values())

    def require_complete_text(self) -> None:
        """Authority cannot prove absence from damaged or uncommitted text."""
        if self.has_invalid_utf8 or self.has_partial_tail:
            raise UnicodeError("dispatch-registry-incomplete-text")

    @property
    def has_duplicate_attempts(self) -> bool:
        """Whether any exact attempt key occurs more than once in this generation."""
        if self._has_duplicate_attempts is None:
            seen = set()
            duplicate = False
            for segment in self._segments:
                for attempt, records in segment.completion_all.items():
                    if len(records) > 1 or attempt in seen:
                        duplicate = True
                        break
                    seen.add(attempt)
                if duplicate:
                    break
            self._has_duplicate_attempts = duplicate
        return self._has_duplicate_attempts

    def completion_attempt(self, attempt_id: str):
        latest, error = None, None
        for segment in self._segments:
            if attempt_id in segment.completion_latest:
                latest = segment.completion_latest[attempt_id]
            error = error or segment.completion_errors.get(attempt_id)
        return latest, error

    def completion_attempt_records(self, attempt_id: str) -> tuple[RegistryRecord, ...]:
        return tuple(record for segment in self._segments
                     for record in segment.completion_all.get(attempt_id, ()))

    def completion_children(self, parent_attempt_id: str) -> tuple[RegistryRecord, ...]:
        return tuple(record for segment in self._segments
                     for record in segment.completion_children.get(parent_attempt_id, ()))

    def completion_related(self, parent_attempt_id: str, route_id: str, route_hash: str):
        """Candidates only: preserve malformed/conflicting aliases for validation."""
        selected = {}
        for segment in self._segments:
            for record in segment.completion_children.get(parent_attempt_id, ()):
                selected[record.order] = record
            for alias in (route_id, route_hash):
                if alias:
                    for record in segment.completion_by_route_alias.get(alias, ()):
                        selected[record.order] = record
        return tuple(selected[key] for key in sorted(selected))

    def settlement_owner_candidates(self):
        return tuple(record for segment in self._segments for record in segment.settlement_owners)

    def records_since(self, order: int):
        """Return records in appended segments after an observed row cursor."""
        result = []
        for segment in self._segments:
            if not segment.records or segment.records[-1].order <= order:
                continue
            result.extend(record for record in segment.records if record.order > order)
        return tuple(result)

    def settlement_owner_candidates_since(self, order: int):
        """Avoid revisiting historical owner candidates on append-only growth."""
        result = []
        for segment in self._segments:
            if not segment.records or segment.records[-1].order <= order:
                continue
            result.extend(record for record in segment.settlement_owners
                          if record.order > order)
        return tuple(result)

    def settlement_owner_candidate(self, attempt_id: str):
        """Return the latest current row only when it still names a settlement owner."""
        record, _error = self.completion_attempt(attempt_id)
        if record is not None:
            metadata = record.completion_metadata()
            if (metadata.get("worker_type") == "owner"
                    and metadata.get("launch_started") == "1"
                    and metadata.get("supervisor_lease") == "flock-v1"):
                return record
        # Compatibility rows without attempt_schema_version are still
        # settlement candidates; their small active set is resolved here.
        candidates = [candidate for segment in self._segments
                      for candidate in segment.settlement_owners
                      if candidate.completion_metadata().get("attempt_id") == attempt_id]
        return max(candidates, key=lambda candidate: candidate.order, default=None)

    @property
    def append_only_generation(self):
        """Whether this view was derived from a continuously observed prefix."""
        return self._append_only_generation

    @property
    def last_complete_order(self):
        """Highest committed row order, excluding an incomplete UTF-8/LF tail."""
        ends = [segment.records[-1].order for segment in self._segments if segment.records]
        return max(ends, default=-1)

    def completion_parent_records(self, parent_sid: str) -> tuple[RegistryRecord, ...]:
        return tuple(record for segment in self._segments
                     for record in segment.completion_by_parent_sid.get(parent_sid, ()))

    def handover_latest(self) -> dict[str, tuple[str, dict[str, str]]]:
        latest = {}
        for segment in self._segments:
            latest.update(segment.handover_latest)
        return {attempt: (status, record.handover_metadata())
                for attempt, (status, record) in latest.items()}

    def handover_attempt(self, attempt_id: str):
        latest = None
        for segment in self._segments:
            latest = segment.handover_latest.get(attempt_id, latest)
        if latest is None:
            return None
        status, record = latest
        return status, record.handover_metadata()

    def handover_candidates(self, parent_sid: str, binding_keys=()):
        selected = {}
        for segment in self._segments:
            matches = dict(segment.handover_by_parent.get(parent_sid, {}))
            for key in binding_keys:
                if isinstance(key, tuple) and len(key) == 3:
                    matches.update(segment.handover_by_binding.get(key, {}))
            for attempt, candidate in matches.items():
                prior = selected.get(attempt)
                if prior is None or candidate[1].order > prior[1].order:
                    selected[attempt] = candidate
        current = []
        keys = {key for key in binding_keys if isinstance(key, tuple) and len(key) == 3}
        for attempt in selected:
            latest = self.handover_attempt(attempt)
            if latest is None:
                continue
            status, metadata = latest
            parent = metadata.get("parent_sid") == parent_sid
            route = metadata.get("owner_route_id") or metadata.get("route_id") or ""
            digest = metadata.get("owner_route_hash") or metadata.get("route_hash") or ""
            node = metadata.get("route_node") or ""
            if parent or (route, digest, node) in keys:
                record = next((segment.handover_latest[attempt][1]
                               for segment in reversed(self._segments)
                               if attempt in segment.handover_latest), None)
                if record is not None:
                    current.append((status, record))
        return tuple(current)

    def headless_owner_candidates(self):
        candidate_attempts = set()
        for segment in self._segments:
            candidate_attempts.update(attempt for _status, record in segment.headless_owner_rows
                                      for attempt in (record.handover_metadata().get("attempt_id"),)
                                      if attempt)
        result = []
        for attempt in candidate_attempts:
            latest = self.handover_attempt(attempt)
            if latest is None:
                continue
            status, metadata = latest
            if (metadata.get("dispatch_depth") == "1" and metadata.get("worker_type") == "owner"
                    and metadata.get("harness") == "opencode"
                    and metadata.get("transport") == "headless"
                    and metadata.get("registered_worker") == "1"
                    and metadata.get("execution_surface") == "registered-headless"):
                record = next((segment.handover_latest[attempt][1]
                               for segment in reversed(self._segments)
                               if attempt in segment.handover_latest), None)
                if record is None:
                    continue
                result.append((status, record))
        return tuple(result)


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    """One complete registry generation with the exact lines decoded from it.

    Selected authority envelopes intentionally remain separate: a bounded row
    selection can identify candidates, but it cannot establish registry-wide
    absence. Consumers that need absence proofs must use a complete snapshot.
    """

    path_identity: str
    resolved_path: str
    signature: tuple[int, int, int, int, int]
    view: RegistryView
    lines: tuple[str, ...]
    complete: bool
    reason: str = ""

    def matches_jobs(self, jobs) -> bool:
        path = Path(jobs)
        try:
            return (str(path.absolute()) == self.path_identity
                    and str(path.resolve(strict=True)) == self.resolved_path
                    and tuple(self.view.signature) == tuple(self.signature))
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    def contains_current_attempt(self, rows, *, attempt_id: str, status: str) -> bool:
        """Match the selected owner row to this generation's current attempt.

        This checks only the selected target. Registry-wide absence and
        prerequisite proofs still use ``rows`` from the complete snapshot.
        """
        if (not self.complete or not attempt_id
                or not isinstance(rows, (list, tuple))):
            return False
        latest, error = self.view.completion_attempt(attempt_id)
        if error or latest is None or latest.status != status:
            return False
        latest_metadata = latest.completion_metadata()
        try:
            for fields, metadata in rows:
                if (isinstance(fields, (list, tuple)) and isinstance(metadata, dict)
                        and tuple(fields) == latest.fields
                        and metadata == latest_metadata):
                    return True
        except (TypeError, ValueError):
            return False
        return False


def _stable_read(path: Path, path_identity: str, *, start: int = 0):
    last_error = None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    for _attempt in range(_RETRIES):
        fd = None
        try:
            resolved_before = str(path.resolve(strict=True))
            path_before = path.stat()
            fd = os.open(path, flags)
            fd_before = os.fstat(fd)
            before_signature = _stat_signature(path_before)
            if _stat_signature(fd_before) != before_signature:
                raise OSError("registry changed before read")
            if start < 0 or start > before_signature[2]:
                raise OSError("registry append boundary changed")
            if start:
                os.lseek(fd, start, os.SEEK_SET)

            chunks = []
            size = 0
            while True:
                chunk = os.read(fd, _CHUNK_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)

            fd_after = os.fstat(fd)
            path_after = path.stat()
            resolved_after = str(path.resolve(strict=True))
            after_signature = _stat_signature(path_after)
            if (resolved_before != resolved_after
                    or before_signature != _stat_signature(fd_after)
                    or before_signature != after_signature
                    or size != before_signature[2] - start):
                raise OSError("registry changed during read")
            return resolved_after, after_signature, b"".join(chunks)
        except OSError as exc:
            last_error = exc
        finally:
            if fd is not None:
                os.close(fd)
    raise OSError(getattr(last_error, "errno", None),
                  f"could not read a stable registry generation: {path_identity}") from last_error


@contextlib.contextmanager
def registry_read_scope():
    """Pin one stable generation of each registry for one passive Fleet pass."""
    current = _READ_SCOPE.get()
    if current is not None:
        yield
        return
    token = _READ_SCOPE.set({})
    try:
        yield
    finally:
        _READ_SCOPE.reset(token)


def registry_scope_active() -> bool:
    return _READ_SCOPE.get() is not None


def _pinned_generation(scope: dict, jobs: Path) -> tuple[RegistryView, tuple[str, ...]]:
    path = Path(jobs)
    key = (str(path.absolute()), str(path.resolve(strict=False)))
    held = scope.get(key)
    if held is None:
        try:
            held = _observe_generation(path)
        except Exception as exc:
            held = exc
        scope[key] = held
    if isinstance(held, BaseException):
        raise held
    return held


def _observe_generation(path: Path) -> tuple[RegistryView, tuple[str, ...]]:
    """Read one full stable generation into its view and its plain text lines.

    The lines equal ``read_text(errors="replace").splitlines()`` of the same bytes;
    the view's LF-segmented lines do not promise that for other line separators.
    The view is the cached one when that generation is already cached.
    """
    identity = str(path.absolute())
    with _LOCK:
        try:
            path.resolve(strict=True)  # a missing path fails at once, as in registry_view
            resolved, signature, raw = _stable_read(path, identity)
        except OSError:
            if _drop(identity) is not None:
                _METRICS["invalidations"] += 1
            raise
        _METRICS["content_reads"] += 1
        _METRICS["bytes_read"] += len(raw)
        cached = _CACHE.get(identity)
        if (cached is not None and cached.resolved_path == resolved
                and cached.signature == signature):
            _METRICS["cache_hits"] += 1
            _CACHE.move_to_end(identity)
            cached.last_observed_at = time.monotonic()
            view = cached
        else:
            _drop(identity)
            _METRICS["invalidations"] += 1
            previous = cached
            reusable = bool(
                previous is not None and previous._prefix_reuse_eligible
                and previous.resolved_path == resolved
                and previous.signature[:2] == signature[:2]
                and signature[2] > previous.signature[2]
                and len(raw) == signature[2]
                and hashlib.sha256(memoryview(raw)[:previous.signature[2]]).digest()
                    == previous._raw_digest.digest()
                and not any(separator in raw for separator in _NON_CANONICAL_LINE_SEPARATORS)
                and not _utf8_status(raw)[0]
            )
            if reusable:
                suffix = raw[previous.signature[2]:]
                view = RegistryView(identity, resolved, signature, suffix, previous=previous)
                _METRICS["delta_reads"] += 1
                _METRICS["delta_bytes_read"] += len(suffix)
                _METRICS["metadata_rows_parsed"] += view.record_count - previous.record_count
            else:
                # Drop both the cache entry and our local reference before parsing.
                cached = previous = None
                view = RegistryView(identity, resolved, signature, raw)
                _METRICS["full_metadata_parses"] += 1
                _METRICS["metadata_rows_parsed"] += view.record_count
            _remember(identity, view)
    return view, tuple(raw.decode("utf-8", errors="replace").splitlines())


def current_registry_view(jobs: Path, *, append_only: bool = False) -> RegistryView:
    """Return the Fleet-pinned view when a registry scope is active."""
    scope = _READ_SCOPE.get()
    if scope is None:
        return registry_view(jobs, append_only=append_only)
    return _pinned_generation(scope, jobs)[0]


def registry_text_lines(jobs: Path) -> tuple[str, ...] | None:
    """Plain text lines of the pinned generation, or None outside a Fleet pass."""
    scope = _READ_SCOPE.get()
    if scope is None:
        return None
    return _pinned_generation(scope, jobs)[1]


def registry_snapshot(jobs: Path) -> RegistrySnapshot:
    """Return the complete view and exact source lines from one stable generation.

    Outside an existing pass, the temporary scope ensures the view and text
    lines come from the same stable read. Inside a pass, every consumer of this
    path receives the already pinned generation and a failed read stays failed
    until that pass ends.
    """
    path = Path(jobs)
    scope = _READ_SCOPE.get()
    if scope is None:
        with registry_read_scope():
            return registry_snapshot(path)
    view, lines = _pinned_generation(scope, path)
    reason = ""
    if view.has_invalid_utf8:
        reason = "registry-invalid-utf8"
    elif view.has_partial_tail:
        reason = "registry-partial-tail"
    elif view.malformed_rows:
        reason = "registry-malformed"
    elif tuple(lines) != view.lines:
        # The segmented view indexes LF records. Non-canonical splitlines
        # separators remain readable for diagnostics but cannot prove absence.
        reason = "registry-line-boundary-invalid"
    return RegistrySnapshot(
        path_identity=str(path.absolute()), resolved_path=view.resolved_path,
        signature=tuple(view.signature), view=view, lines=tuple(lines),
        complete=not reason, reason=reason,
    )


def registry_view(jobs: Path, *, append_only: bool = False) -> RegistryView:
    scope = _READ_SCOPE.get()
    if scope is not None:
        return current_registry_view(jobs, append_only=append_only)
    return _registry_view_fresh(jobs, append_only=append_only)


def _registry_view_fresh(jobs: Path, *, append_only: bool = False) -> RegistryView:
    """Return the current stable view, rebuilding once when the source generation changes.

    A failed or unstable observation invalidates the cached generation; an older
    successful view is never returned as authority for a missing/unreadable path.
    ``append_only`` is an assertion by a caller executing the supported dispatch
    writer protocol: same-inode growth is used as a suffix only on that path.
    Arbitrary files and direct rewrites must leave it false; stat size alone is
    not evidence that an arbitrary writer preserved the old prefix.
    """
    path = Path(jobs)
    identity = str(path.absolute())
    with _LOCK:
        try:
            resolved = str(path.resolve(strict=True))
            signature = _stat_signature(path.stat())
        except OSError:
            if _drop(identity) is not None:
                _METRICS["invalidations"] += 1
            raise

        cached = _CACHE.get(identity)
        if (cached is not None and cached.resolved_path == resolved
                and cached.signature == signature):
            _METRICS["cache_hits"] += 1
            _CACHE.move_to_end(identity)
            cached.last_observed_at = time.monotonic()
            return cached

        can_append = bool(
            append_only and cached is not None and cached.resolved_path == resolved
            and cached.signature[:2] == signature[:2]
            and signature[2] > cached.signature[2]
            and time.monotonic() - cached.last_observed_at <= _APPEND_OBSERVATION_GAP_SECONDS
        )
        _drop(identity)
        _METRICS["invalidations"] += 1
        previous = None
        full_read = False
        if can_append:
            try:
                observed_resolved, observed_signature, raw = _stable_read(
                    path, identity, start=cached.signature[2]
                )
                if (observed_resolved == cached.resolved_path
                        and observed_signature[:2] == cached.signature[:2]
                        and observed_signature[2] > cached.signature[2]):
                    resolved, signature = observed_resolved, observed_signature
                    previous = cached
                    _METRICS["delta_reads"] += 1
                    _METRICS["delta_bytes_read"] += len(raw)
                else:
                    resolved, signature, raw = _stable_read(path, identity)
                    full_read = True
            except OSError:
                raise
        else:
            resolved, signature, raw = _stable_read(path, identity)
            full_read = True
        if full_read:
            # Ordinary fresh reads retain their established full-rebuild semantics.
            # Scoped passive observations use the checked-prefix path above; the
            # append-only API continues to use only its bounded trusted suffix path.
            cached = None
        _METRICS["content_reads"] += 1
        _METRICS["bytes_read"] += len(raw)
        view = RegistryView(identity, resolved, signature, raw, previous=previous)
        if previous is None:
            _METRICS["full_metadata_parses"] += 1
            _METRICS["metadata_rows_parsed"] += view.record_count
        else:
            _METRICS["metadata_rows_parsed"] += view.record_count - previous.record_count
        _remember(identity, view)
        return view


def _drop(identity: str) -> "RegistryView | None":
    """Forget one cached generation; the caller holds ``_LOCK``."""
    global _CACHE_BYTES
    removed = _CACHE.pop(identity, None)
    if removed is not None:
        _CACHE_BYTES -= removed.signature[2]
    return removed


def _remember(identity: str, view: RegistryView) -> None:
    """Cache one generation within the existing bounds; the caller holds ``_LOCK``."""
    global _CACHE_BYTES
    if view.signature[2] <= _CACHE_MAX_BYTES:
        _CACHE[identity] = view
        _CACHE_BYTES += view.signature[2]
    while (len(_CACHE) > _CACHE_MAX_PATHS or _CACHE_BYTES > _CACHE_MAX_BYTES):
        _evicted_identity, evicted = _CACHE.popitem(last=False)
        _CACHE_BYTES -= evicted.signature[2]
        _METRICS["cache_evictions"] += 1


def registry_lines(jobs: Path) -> tuple[str, ...]:
    """Compatibility facade retaining the existing tuple[str, ...]/OSError contract."""
    return registry_view(jobs).lines


def metrics_snapshot() -> dict[str, int]:
    """Return counters used by deterministic regression and workload fixtures."""
    with _LOCK:
        return {**_METRICS, "cache_entries": len(_CACHE), "cache_bytes": _CACHE_BYTES}


def reset_metrics() -> None:
    """Reset diagnostic counters without changing any cached generation."""
    with _LOCK:
        for key in _METRICS:
            _METRICS[key] = 0


def clear_cache() -> None:
    """Test helper: discard process-local views."""
    global _CACHE_BYTES
    with _LOCK:
        _CACHE.clear()
        _CACHE_BYTES = 0
