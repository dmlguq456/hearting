#!/usr/bin/env python3
"""Shared, generation-checked views of the dispatch registry.

The immutable view may be reused while the registry generation is unchanged.
Consumers must still observe their other inputs on each pass and recheck
authority under the existing lock before a state transition.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import atexit
import codecs
import contextlib
import contextvars
import functools
import hashlib
import json
import os
from pathlib import Path
import threading
import time


_RETRIES = 3
_CHUNK_SIZE = 1024 * 1024
_APPEND_OBSERVATION_GAP_SECONDS = 30.0
# A read this long after the file's last change cannot share that change's
# timestamp tick. Whole-second stamps (FAT 2 s, ext3/HFS+ 1 s) get the coarse
# margin; stamps with sub-second digits come from a clock of one kernel tick
# (at most 10 ms) or finer.
_SETTLED_NS = 2_000_000_000
_SETTLED_FINE_NS = 100_000_000
_CACHE_MAX_PATHS = 128
_CACHE_MAX_BYTES = 64 * 1024 * 1024
_MAX_SEGMENTS = 256
_COMPACT_SEGMENTS = 128
# A first read is cut into segments of this many rows, so a registry replaced
# by a rewrite that kept its leading rows can keep the parse of those rows.
_PARSE_CHUNK_LINES = 512
_NON_CANONICAL_LINE_SEPARATORS = (b"\r", b"\v", b"\f", b"\x1c", b"\x1d",
                                 b"\x1e", b"\xc2\x85", b"\xe2\x80\xa8",
                                 b"\xe2\x80\xa9")
_LIVE_STATUSES = frozenset({"open", "running"})
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
    "append_reuses": 0,
    "full_metadata_parses": 0,
    "metadata_rows_parsed": 0,
    "invalidations": 0,
    "cache_evictions": 0,
    "segment_compactions": 0,
    "prefix_reuses": 0,
    "prefix_segments_reused": 0,
    "prefix_bytes_reused": 0,
    "line_parse_reuses": 0,
    "replacement_segment_builds": 0,
    "segment_resyncs": 0,
    "segment_rebuilds": 0,
}
# Set once a process asks for ``replacement_rows``: segments built afterwards keep
# the attempt index they already compute, so that request costs no second parse.
_REPLACEMENT_ROWS_DEMAND = False


def _stat_signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


# ---------------------------------------------------------------------------
# registry change lineage
#
# Every supported writer replaces the registry through one atomic rename or
# appends complete rows.  A small sidecar beside `jobs.log` records the byte
# prefix each write preserved, so a later reader can rescan only the suffix.
# The sidecar is a derived accelerator: a missing, stale or malformed chain
# silently falls back to the full read that existed before it.
# ---------------------------------------------------------------------------

LINEAGE_SCHEMA = 1
_LINEAGE_MAX_ENTRIES = 64
_LINEAGE_ANCHOR_BYTES = 4096


def lineage_path(jobs) -> Path:
    return Path(str(Path(os.path.abspath(jobs))) + ".lineage.json")


def registry_key(source) -> list[int]:
    """One file version identity: the same six fields the capacity cache keys on."""
    info = source if isinstance(source, os.stat_result) else os.fstat(source)
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_mode, info.st_ctime_ns]


def _key_tuple(value) -> tuple[int, ...] | None:
    if (isinstance(value, list) and len(value) == 6
            and all(isinstance(item, int) and not isinstance(item, bool) for item in value)):
        return tuple(value)
    return None


def _read_lineage(jobs) -> dict | None:
    try:
        data = json.loads(lineage_path(jobs).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if (not isinstance(data, dict) or data.get("schema") != LINEAGE_SCHEMA
            or data.get("jobs") != os.path.abspath(jobs)
            or not isinstance(data.get("entries"), list)):
        return None
    entries = []
    for entry in data["entries"]:
        if (not isinstance(entry, dict) or _key_tuple(entry.get("p")) is None
                or _key_tuple(entry.get("c")) is None
                or not isinstance(entry.get("n"), int) or isinstance(entry.get("n"), bool)
                or entry["n"] < 0 or not isinstance(entry.get("a"), str)):
            return None
        entries.append({"p": entry["p"], "c": entry["c"], "n": entry["n"], "a": entry["a"]})
    return {"schema": LINEAGE_SCHEMA, "jobs": os.path.abspath(jobs), "entries": entries}


def record_lineage(jobs, parent_key, child_key, prefix: int, anchor_sha256: str) -> None:
    """Append one change fact; every failure leaves the previous file untouched."""
    temp = None
    try:
        parent, child = _key_tuple(parent_key), _key_tuple(child_key)
        if parent is None or child is None or not isinstance(prefix, int) or prefix < 0:
            return
        if not isinstance(anchor_sha256, str):
            return
        entries = (_read_lineage(jobs) or {"entries": []})["entries"]
        entries.append({"p": list(parent), "c": list(child), "n": prefix,
                        "a": anchor_sha256})
        payload = {"schema": LINEAGE_SCHEMA, "jobs": os.path.abspath(jobs),
                   "entries": entries[-_LINEAGE_MAX_ENTRIES:]}
        target = lineage_path(jobs)
        temp = target.with_name(target.name + ".tmp")
        data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.replace(str(temp), str(target))
    except Exception:  # noqa: BLE001 - a derived sidecar never changes the write result
        try:
            if temp is not None:
                temp.unlink()
        except OSError:
            pass


def resolve_lineage(jobs, from_key, to_key) -> tuple[int, str] | None:
    """The smallest prefix on a recorded chain from ``from_key`` to ``to_key``.

    Returns ``(prefix, anchor_sha256)``; the anchor proves the parent bytes at
    that prefix still match the current file.  None means "read the whole file".
    """
    try:
        start, target = _key_tuple(from_key), _key_tuple(to_key)
        if start is None or target is None:
            return None
        data = _read_lineage(jobs)
        if data is None:
            return None
        successors = {}
        for entry in data["entries"]:
            successors.setdefault(_key_tuple(entry["p"]), []).append(entry)
        best = None
        stack = [(start, [])]
        seen = set()
        while stack:
            key, path = stack.pop()
            if key == target:
                if path:
                    lowest = min(path, key=lambda entry: entry["n"])
                    if best is None or lowest["n"] < best["n"]:
                        best = lowest
                continue
            if key in seen:
                continue
            seen.add(key)
            for entry in successors.get(key, ()):
                stack.append((_key_tuple(entry["c"]), path + [entry]))
        if best is None:
            return None
        return best["n"], best["a"]
    except Exception:  # noqa: BLE001
        return None


def common_prefix_line_aligned(old, new: bytes) -> int:
    """Length of the shared prefix of two registry images, cut at a line start.

    Compares 1MiB slices (an mmap or bytes slice copies only that window), so a
    replacement never duplicates the whole registry image in memory.
    """
    limit = min(len(old), len(new))
    chunk = 1024 * 1024
    position = 0
    while position < limit:
        width = min(chunk, limit - position)
        left = old[position:position + width]
        right = new[position:position + width]
        if left != right:
            low, high = 0, width
            while low < high:
                middle = (low + high) // 2
                if left[:middle + 1] == right[:middle + 1]:
                    low = middle + 1
                else:
                    high = middle
            position += low
            break
        position += width
    return new.rfind(b"\n", 0, position) + 1



def _utf8_status(data: bytes) -> tuple[bool, bool]:
    """Check UTF-8 availability without a Python loop over registry bytes."""
    if data.isascii():
        return False, False
    try:
        _text, consumed = codecs.utf_8_decode(data, "strict", False)
    except UnicodeDecodeError:
        return True, False
    return False, consumed < len(data)


def _parse_completion(raw: str) -> tuple[tuple[str, str], ...]:
    values = {}
    for part in raw.split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            values[key] = value
    return tuple(values.items())


def _parse_handover(raw: str) -> tuple[tuple[str, str], ...]:
    values = {}
    for part in raw.split(","):
        key, separator, value = part.partition("=")
        if separator and key.strip():
            values[key.strip()] = value
    return tuple(values.items())


# Terminal rows parse on demand; the small LRU only spares repeated lookups of
# the same few rows and is cleared whenever a view generation is dropped.
_LAZY_PARSE_CACHE_SIZE = 256
_completion_metadata = functools.lru_cache(maxsize=_LAZY_PARSE_CACHE_SIZE)(_parse_completion)
_handover_metadata = functools.lru_cache(maxsize=_LAZY_PARSE_CACHE_SIZE)(_parse_handover)


def _clear_parse_caches() -> None:
    _completion_metadata.cache_clear()
    _handover_metadata.cache_clear()


def _handover_keys(metadata: dict) -> tuple:
    """Index keys a handover row contributes: parent, binding, headless-owner flag."""
    route = metadata.get("owner_route_id") or metadata.get("route_id") or ""
    digest = metadata.get("owner_route_hash") or metadata.get("route_hash") or ""
    headless = bool(metadata.get("dispatch_depth") == "1" and metadata.get("worker_type") == "owner"
                    and metadata.get("harness") == "opencode"
                    and metadata.get("transport") == "headless"
                    and metadata.get("registered_worker") == "1"
                    and metadata.get("execution_surface") == "registered-headless")
    return (metadata.get("parent_sid"), (route, digest, metadata.get("route_node") or ""), headless)


@dataclass(frozen=True, slots=True)
class RegistryRecord:
    order: int
    fields: tuple[str, ...]
    _completion_pairs: tuple[tuple[str, str], ...] = ()
    _handover_pairs: tuple[tuple[str, str], ...] = ()

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
        return dict(self._completion_pairs or _completion_metadata(self.fields[5]))

    def handover_metadata(self) -> dict[str, str]:
        return dict(self._handover_pairs or _handover_metadata(self.fields[5]))


class _RegistrySegment:
    """One run of consecutive rows (a chunk of a full read, or one append batch) and its indexes."""

    __slots__ = ("records", "malformed_lines", "malformed_rows", "completion_latest",
                 "completion_errors", "completion_all", "completion_children",
                 "completion_by_parent_sid",
                 "completion_by_route_alias", "settlement_owners",
                 "observation_owners", "observation_owner_keys",
                 "handover_latest", "handover_by_parent", "handover_by_binding",
                 "headless_owner_rows", "selection_by_parent", "selection_by_slug",
                 "selection_by_attempt", "selection_by_parent_slug",
                 "byte_end", "prefix_sha256", "order_end", "replacement_rows",
                 "order_start", "content_sha256", "inner_proofs")

    def __init__(self, lines, order_base: int, *, hint=None, byte_end=None,
                 prefix_sha256=None, content_sha256=None):
        # ``byte_end``/``prefix_sha256``: where this segment's last row ends in the
        # file and the digest of every byte up to there (None: not reusable).
        # ``hint`` maps the fields of an earlier row to its record: an identical row
        # shares that record's fields and, for an open row, its parsed pairs instead
        # of parsing them again. Records themselves are always rebuilt (new order).
        # ``content_sha256``: the digest of this segment's own bytes alone (None: unknown, so a
        # rewrite cannot recognise this segment again and builds it anew). With ``order_start`` it
        # names the whole input of everything below, which is a pure function of the two.
        self.byte_end = byte_end
        self.prefix_sha256 = prefix_sha256
        self.content_sha256 = content_sha256
        self.order_start = order_base
        # ``(byte_end, prefix_sha256, order_end)`` of member boundaries a compaction
        # merged away (see ``compact``); a plain segment has none.
        self.inner_proofs = ()
        demand = _REPLACEMENT_ROWS_DEMAND
        replacement = {}
        replacement_orders = {}
        first_duplicate = None
        line_reuses = 0
        line_count = 0
        records = []
        handover_keys = {}
        malformed_lines = []
        completion_latest = {}
        completion_errors = {}
        completion_all = {}
        completion_children = {}
        completion_by_parent_sid = {}
        completion_by_route_alias = {}
        settlement_owners = []
        observation_owners = []
        observation_owner_keys = []
        handover_latest = {}
        selection_by_parent = {}
        selection_by_slug = {}
        selection_by_attempt = {}
        selection_by_parent_slug = {}
        self.malformed_rows = 0
        for relative_order, line in enumerate(lines):
            line_count = relative_order + 1
            fields = tuple(line.split("\t"))
            earlier = hint.get(fields) if hint else None
            if earlier is not None:
                fields = earlier.fields     # one shared tuple, and its parsed pairs below
                line_reuses += 1
            if len(fields) != 6:
                self.malformed_rows += 1
                malformed_lines.append((order_base + relative_order, line))
                continue
            completion_pairs = (earlier._completion_pairs if earlier is not None else ()) \
                or _parse_completion(fields[5])
            canonical_keys = all(key and key == key.strip() for key, _value in completion_pairs)
            handover_pairs = (completion_pairs if canonical_keys
                              else _parse_handover(fields[5]))
            if fields[1] in _LIVE_STATUSES:
                record = RegistryRecord(order_base + relative_order, fields,
                                        completion_pairs, handover_pairs)
            else:
                # Terminal rows keep only their text; metadata parses on demand.
                record = RegistryRecord(order_base + relative_order, fields)
            records.append(record)
            completion_meta = dict(completion_pairs)
            handover_meta = completion_meta if canonical_keys else dict(handover_pairs)
            parent = completion_meta.get("parent")
            attempt_id = completion_meta.get("attempt_id")
            if demand and attempt_id:
                if attempt_id in replacement:
                    if first_duplicate is None:
                        first_duplicate = (order_base + relative_order, attempt_id)
                else:
                    replacement[attempt_id] = (list(fields), completion_meta)
                    replacement_orders[attempt_id] = order_base + relative_order
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
            if completion_meta.get("worker_type") == "owner":
                observation_owners.append(record)
                # What a consumer that keeps one row per attempt keys on, known here so it never
                # parses a finished row's metadata again just to find it.
                observation_owner_keys.append(attempt_id or record.order)
            handover_attempt = handover_meta.get("attempt_id")
            if handover_attempt:
                handover_latest[handover_attempt] = (fields[1], record)
                handover_keys[handover_attempt] = _handover_keys(handover_meta)
        self.order_end = order_base + line_count
        self.replacement_rows = ((replacement, first_duplicate, replacement_orders)
                                 if demand else None)
        if line_reuses:
            _METRICS["line_parse_reuses"] += line_reuses
        self.records = tuple(records)
        self.malformed_lines = tuple(malformed_lines)
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
        self.observation_owners = tuple(observation_owners)
        self.observation_owner_keys = tuple(observation_owner_keys)
        self.handover_latest = handover_latest
        self.selection_by_parent = {key: tuple(value) for key, value in selection_by_parent.items()}
        self.selection_by_slug = {key: tuple(value) for key, value in selection_by_slug.items()}
        self.selection_by_attempt = {key: tuple(value) for key, value in selection_by_attempt.items()}
        self.selection_by_parent_slug = {
            key: tuple(value) for key, value in selection_by_parent_slug.items()
        }
        self._index_handover(handover_keys)

    @classmethod
    def resynced(cls, old: "_RegistrySegment", byte_end: int, prefix_sha256: bytes):
        """A new segment object standing for the same rows as ``old`` at a new byte position.

        Only the position (``byte_end``) and the digest of everything before it are new; the rows,
        their records and every index are ``old``'s own immutable objects, so what a consumer
        keyed on them (``records`` is the identity of the content) computed still holds.
        """
        result = cls.__new__(cls)
        for name in cls.__slots__:
            setattr(result, name, getattr(old, name))
        result.byte_end = byte_end
        result.prefix_sha256 = prefix_sha256
        # Only a plain segment (one with its own content digest) is recognised again, and it
        # has no merged-away member boundaries; none that described the old byte positions stay.
        result.inner_proofs = ()
        return result

    def _index_handover(self, handover_keys=None):
        self.handover_by_parent = {}
        self.handover_by_binding = {}
        headless = []
        for attempt, (status, record) in self.handover_latest.items():
            keys = handover_keys.get(attempt) if handover_keys else None
            if keys is None:     # merged segments: parse without touching the LRU
                keys = _handover_keys(dict(record._handover_pairs
                                           or _parse_handover(record.fields[5])))
            parent, binding, is_headless = keys
            if parent:
                self.handover_by_parent.setdefault(parent, {})[attempt] = (status, record)
            route, digest, _node = binding
            if route and digest:     # an owner row has no route node: it binds as (route, hash, "")
                self.handover_by_binding.setdefault(binding, {})[attempt] = (status, record)
            if is_headless:
                headless.append((status, record))
        self.headless_owner_rows = tuple(headless)

    @property
    def lines(self) -> tuple[str, ...]:
        """Rebuild compatibility lines on demand without retaining duplicate rows."""
        malformed = dict(self.malformed_lines)
        records = {record.order: record for record in self.records}
        if not records and not malformed:
            return ()
        first, last = min((*records, *malformed)), max((*records, *malformed))
        return tuple(records[order].raw if order in records else malformed[order]
                     for order in range(first, last + 1))

    @classmethod
    def compact(cls, segments: tuple["_RegistrySegment", ...]):
        """Merge index deltas without reparsing their registry rows."""
        result = cls.__new__(cls)
        # The merged run ends where its last member ends, and that member's digest
        # already covers every byte before it. The boundaries inside are gone, so
        # a later replacement can only reuse this run as a whole. Their digests
        # stay as proofs of the leading bytes (``RegistryView.prefix_proofs``): a
        # cursor taken at one of them keeps its floor. The latest run of them is
        # enough for a cursor a few generations behind and bounds the list.
        result.byte_end = segments[-1].byte_end
        result.prefix_sha256 = segments[-1].prefix_sha256
        result.content_sha256 = None      # the boundaries inside are gone: not recognisable again
        result.order_start = segments[0].order_start
        result.order_end = segments[-1].order_end
        inner = []
        for segment in segments:
            inner.extend(segment.inner_proofs)
            if segment is not segments[-1] and segment.byte_end is not None:
                inner.append((segment.byte_end, segment.prefix_sha256, segment.order_end))
        result.inner_proofs = tuple(inner[-_COMPACT_SEGMENTS:])
        result.replacement_rows = None
        result.records = tuple(record for segment in segments for record in segment.records)
        result.malformed_lines = tuple(line for segment in segments for line in segment.malformed_lines)
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
        result.observation_owners = tuple(record for segment in segments
                                          for record in segment.observation_owners)
        result.observation_owner_keys = tuple(key for segment in segments
                                              for key in segment.observation_owner_keys)
        result.selection_by_parent = {key: tuple(value) for key, value in selection_by_parent.items()}
        result.selection_by_slug = {key: tuple(value) for key, value in selection_by_slug.items()}
        result.selection_by_attempt = {key: tuple(value) for key, value in selection_by_attempt.items()}
        result.selection_by_parent_slug = {
            key: tuple(value) for key, value in selection_by_parent_slug.items()
        }
        result._index_handover()
        return result


def _iter_lf_chunks(raw: bytes, boundary: int, chunk_lines: int, start: int = 0):
    """Yield ``(rows, end offset)`` for the LF rows of ``raw[start:boundary]``, ``chunk_lines`` at a time.

    The rows equal ``splitlines`` of that text when LF is the only separator.
    """
    position = start
    while position < boundary:
        rows = []
        while position < boundary and len(rows) < chunk_lines:
            end = raw.find(b"\n", position)
            rows.append(raw[position:end].decode("utf-8", errors="replace"))
            position = end + 1
        yield rows, position


def _resync_segments(data: bytes, boundary: int, base: int, hasher, order: int, hint, olds):
    """Segments of ``data[:boundary]`` after the first digest mismatch, following the old cuts.

    ``olds`` are the cached segments past the kept prefix. Each is given the same number of
    rows from ``data``; when that piece's own bytes have the digest the old segment recorded
    **and** its first row has the order the old segment began at, the piece is the same rows at
    the same positions, so a new segment object takes over the old one's rows and indexes
    (``_RegistrySegment.resynced``). Any other piece is parsed anew, with ``hint`` for the rows
    it still shares with the old ones. The walk ends at the first old segment that cannot be
    recognised again (no own digest: merged or built without one) or when ``data`` runs out;
    the caller cuts the rest the ordinary way. ``hasher`` has consumed everything up to the
    piece being cut and leaves each segment's cumulative digest behind, as the ordinary cut does.

    Returns ``(segments, consumed, order, resynced_rows, rebuilds)``.
    """
    segments = []
    window = memoryview(data)
    position = resynced_rows = rebuilds = 0
    for old in olds:
        if position >= boundary or old.content_sha256 is None:
            break
        want = old.order_end - old.order_start
        rows_end, cut = position, 0
        while cut < want and rows_end < boundary:
            rows_end = data.find(b"\n", rows_end) + 1
            cut += 1
        piece = window[position:rows_end]
        content = hashlib.sha256(piece).digest()
        hasher.update(piece)
        end = rows_end
        if cut == want and order == old.order_start and content == old.content_sha256:
            segments.append(_RegistrySegment.resynced(old, base + end, hasher.copy().digest()))
            resynced_rows += cut
            _METRICS["segment_resyncs"] += 1
        else:
            rows = [data[start:stop].decode("utf-8", errors="replace")
                    for start, stop in _line_spans(data, position, end)]
            segments.append(_RegistrySegment(rows, order, hint=hint, byte_end=base + end,
                                             prefix_sha256=hasher.copy().digest(),
                                             content_sha256=content))
            rebuilds += 1
            _METRICS["segment_rebuilds"] += 1
        order += cut
        position = end
    return segments, position, order, resynced_rows, rebuilds


def _line_spans(data: bytes, start: int, end: int):
    """``(start, stop)`` of each LF row of ``data[start:end]`` (stop excludes the LF)."""
    while start < end:
        stop = data.find(b"\n", start)
        yield start, stop
        start = stop + 1


def _reusable_prefix(cached, raw: bytes, resolved: str):
    """What of the cached parse the new bytes ``raw`` still prove unchanged.

    Returns ``(segments, byte_end, hasher, hint, whole, discarded)`` or None.
    ``segments`` are the leading cached segments whose cumulative digest equals the
    digest of the same byte range of ``raw`` (the first mismatch stops the walk; the
    inode is not asked, the digest proves the bytes). ``hasher`` has consumed
    ``raw[:byte_end]`` for the new view to continue. ``hint`` maps the fields of
    every discarded cached row to its record (no row text is copied); ``whole``
    says every cached segment was kept. ``discarded`` are the cached segments past the kept
    prefix, which ``_resync_segments`` may recognise again.
    None means a plain full parse.
    """
    if (cached is None or not cached._prefix_reuse_eligible
            or cached.resolved_path != resolved):
        return None
    view = memoryview(raw)
    good = hashlib.sha256()
    kept, position = 0, 0
    for segment in cached._segments:
        end = segment.byte_end
        if end is None or end > len(raw) or end < position:
            break
        trial = good.copy()
        trial.update(view[position:end])
        if trial.digest() != segment.prefix_sha256:
            break
        good, position, kept = trial, end, kept + 1
    suffix = raw[position:]
    # The reused rows were separator-free valid UTF-8; only the rest is checked.
    if (any(separator in suffix for separator in _NON_CANONICAL_LINE_SEPARATORS)
            or _utf8_status(suffix)[0]):
        return None
    discarded = cached._segments[kept:]
    hint = {record.fields: record for segment in discarded for record in segment.records} or None
    return cached._segments[:kept], position, good, hint, kept == len(cached._segments), discarded


def demand_replacement_rows() -> None:
    """Keep each new segment's attempt index from now on (see ``RegistryView.replacement_rows``)."""
    global _REPLACEMENT_ROWS_DEMAND
    _REPLACEMENT_ROWS_DEMAND = True


def _build_replacement_rows(segment: "_RegistrySegment"):
    """The attempt index of one segment built from its records, outside the LRU."""
    rows, orders, first_duplicate = {}, {}, None
    for record in segment.records:
        meta = dict(record._completion_pairs or _parse_completion(record.fields[5]))
        attempt = meta.get("attempt_id")
        if not attempt:
            continue
        if attempt in rows:
            if first_duplicate is None:
                first_duplicate = (record.order, attempt)
        else:
            rows[attempt] = (list(record.fields), meta)
            orders[attempt] = record.order
    return rows, first_duplicate, orders


class RegistryView:
    """Immutable segmented registry view with incremental append indexes."""

    __slots__ = ("path_identity", "resolved_path", "signature", "_fd", "_segments", "_tail",
                 "line_count", "record_count", "malformed_rows", "has_partial_tail",
                 "last_observed_at", "_has_duplicate_attempts", "_invalid_utf8_prefix",
                 "has_invalid_utf8", "_raw_digest", "_prefix_reuse_eligible",
                 "_asserted_suffix", "_settled",
                 "_parent_sessions", "_complete_digest", "_resynced_rows")

    def __init__(self, path_identity: str, resolved_path: str, signature: tuple,
                 raw: bytes, *, previous: "RegistryView | None" = None, fd: int = -1,
                 asserted_suffix: bool = False, settled: bool = False, reuse=None):
        self.path_identity = path_identity
        self.resolved_path = resolved_path
        self.signature = signature
        self._fd = fd
        # True while any part of this generation rests on a caller's append-only
        # assertion instead of bytes whose prefix digest was compared here.
        self._asserted_suffix = bool(
            previous is not None and (asserted_suffix or previous._asserted_suffix))
        # True once these bytes were read when the file's last change was already
        # older than the timestamp granularity: any later write changes the stat.
        self._settled = bool(settled and not self._asserted_suffix)
        self._parent_sessions = None
        self._resynced_rows = 0
        self.last_observed_at = time.monotonic()
        self._has_duplicate_attempts = None
        self._complete_digest = None
        if previous is None:
            kept, base, hasher, hint = reuse[:4] if reuse is not None else ((), 0, None, None)
            # ``data`` is what this view parses; the first ``base`` bytes are the
            # already proven prefix whose segments are ``kept``.
            data = raw[base:] if base else raw
            invalid_utf8, incomplete_utf8 = _utf8_status(data)
            self._invalid_utf8_prefix = invalid_utf8
            self.has_invalid_utf8 = invalid_utf8 or incomplete_utf8
            self._prefix_reuse_eligible = (
                not invalid_utf8
                and not any(separator in data for separator in _NON_CANONICAL_LINE_SEPARATORS)
            )
            boundary = data.rfind(b"\n") + 1
            tail = data[boundary:]
            if self._prefix_reuse_eligible:
                # LF is the only separator: decode a chunk of rows at a time so the
                # whole text and its line list never coexist. One hasher walks the
                # bytes once and leaves each chunk's cumulative digest behind.
                hasher = hasher if hasher is not None else hashlib.sha256()
                segments = list(kept)
                order = kept[-1].order_end if kept else 0
                lf_rows = data.count(b"\n", 0, boundary)
                chunk_lines = max(_PARSE_CHUNK_LINES, -(-lf_rows // _COMPACT_SEGMENTS))
                window, consumed = memoryview(data), 0
                olds = reuse[5] if reuse is not None and len(reuse) > 5 else ()
                if olds:
                    # Past the first mismatch the old segments' own cuts are followed, so a
                    # rewrite leaves the segments whose rows did not change as they were.
                    synced, consumed, order, self._resynced_rows, _built = _resync_segments(
                        data, boundary, base, hasher, order, hint, olds)
                    segments.extend(synced)
                for rows, end in _iter_lf_chunks(data, boundary, chunk_lines, consumed):
                    piece = window[consumed:end]
                    hasher.update(piece)
                    consumed = end
                    segments.append(_RegistrySegment(
                        rows, order, hint=hint, byte_end=base + end,
                        prefix_sha256=hasher.copy().digest(),
                        content_sha256=hashlib.sha256(piece).digest()))
                    _METRICS["segment_rebuilds"] += 1
                    order += len(rows)
                # A finished view holds at most _MAX_SEGMENTS segments: the one that makes it
                # _MAX_SEGMENTS + 1 (257) exists only here, and is merged at once with the
                # _COMPACT_SEGMENTS before it. A resynced rewrite keeps the old cuts, so it does
                # not change that count.
                if len(segments) > _MAX_SEGMENTS:
                    compacted = _RegistrySegment.compact(tuple(segments[-_COMPACT_SEGMENTS:]))
                    segments = segments[:-_COMPACT_SEGMENTS] + [compacted]
                    _METRICS["segment_compactions"] += 1
                self._complete_digest = hasher
                self._raw_digest = hasher.copy()
                self._raw_digest.update(tail)
                complete_count = order
            else:
                complete = data[:boundary].decode("utf-8", errors="replace").splitlines()
                segment = _RegistrySegment(tuple(complete), 0)
                segments = [segment] if complete else []
                complete_count = len(complete)
                self._raw_digest = hashlib.sha256(raw)
            self._segments = tuple(segments)
            self._tail = tail
            self.line_count = complete_count + (1 if tail else 0)
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
            # Rows already complete before this append; a pending tail row was counted
            # in ``previous.line_count`` and is completed by (or rejoined into) this one.
            before = previous.line_count - (1 if old_partial else 0)
            order_base = before
            new_segments = list(previous._segments)
            appended_segment = None
            byte_end = prefix_digest = None
            hasher = previous._complete_digest
            if hasher is not None and self._prefix_reuse_eligible:
                hasher = hasher.copy()
                if complete:
                    hasher.update(memoryview(combined)[:len(combined) - len(tail)])
                    byte_end = (previous.signature[2] - len(previous._tail)
                                + len(combined) - len(tail))
                    prefix_digest = hasher.digest()
                self._complete_digest = hasher
            if complete:
                own = (hashlib.sha256(memoryview(combined)[:len(combined) - len(tail)]).digest()
                       if prefix_digest is not None else None)
                appended_segment = _RegistrySegment(complete, order_base, byte_end=byte_end,
                                                    prefix_sha256=prefix_digest, content_sha256=own)
                new_segments.append(appended_segment)
            # Same bound as in the full read above: 257 segments exist only until this merge.
            if len(new_segments) > _MAX_SEGMENTS:
                compacted = _RegistrySegment.compact(tuple(new_segments[-_COMPACT_SEGMENTS:]))
                new_segments = new_segments[:-_COMPACT_SEGMENTS] + [compacted]
                _METRICS["segment_compactions"] += 1
            self._segments = tuple(new_segments)
            self._tail = tail
            self.line_count = before + len(complete) + (1 if tail else 0)
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
    def completeness_reason(self) -> str:
        """The same absence judgment for a snapshot or a generation-checked view."""
        if self.has_invalid_utf8:
            return "registry-invalid-utf8"
        if self.has_partial_tail:
            return "registry-partial-tail"
        if self.malformed_rows:
            return "registry-malformed"
        if not self._prefix_reuse_eligible:
            return "registry-line-boundary-invalid"
        return ""

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

    def replacement_rows(self):
        """``(rows, duplicate)``: rows by attempt id, ``(fields list, metadata dict)``.

        The same answer as parsing ``read_text().splitlines()`` row by row, for a
        view whose lines equal that text (LF only, valid UTF-8); None otherwise.
        ``duplicate`` is the attempt id whose second row comes first in the file.
        Each segment keeps its own attempt index, so a segment carried over a
        replacement is not parsed again.
        """
        if not self._prefix_reuse_eligible or self.has_invalid_utf8:
            return None
        merged = {}
        for segment in self._segments:
            built = segment.replacement_rows
            if built is None:
                built = segment.replacement_rows = _build_replacement_rows(segment)
                _METRICS["replacement_segment_builds"] += 1
            rows, first_duplicate, orders = built
            candidates = [(orders[attempt], attempt) for attempt in rows if attempt in merged]
            if first_duplicate is not None:
                candidates.append(first_duplicate)
            if candidates:
                return merged, min(candidates)[1]
            merged.update(rows)
        if self._tail:
            fields = self._tail.decode("utf-8").split("\t")
            if len(fields) == 6:
                meta = dict(_parse_completion(fields[5]))
                attempt = meta.get("attempt_id")
                if attempt:
                    if attempt in merged:
                        return merged, attempt
                    merged[attempt] = (fields, meta)
        return merged, None

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

    def answer_wait_owner_candidates(self):
        """All owner rows for observational wait projection (no lease filter)."""
        return tuple(record for segment in self._segments for record in segment.observation_owners)

    def answer_wait_owner_keyed(self):
        """``(attempt id or row order, record)`` of every owner row, in registry order.

        The key is each row's ``attempt_id`` (its order when it has none); the segments computed
        it once, so a pass that keeps the last row per attempt does not parse a finished row.
        """
        return tuple(pair for segment in self._segments
                     for pair in zip(segment.observation_owner_keys, segment.observation_owners))

    def route_alias_candidates(self, *aliases):
        """Return every row matching aliases; callers must validate full route identity."""
        selected = {}
        for segment in self._segments:
            selected_aliases = aliases or tuple(segment.completion_by_route_alias)
            for alias in selected_aliases:
                if alias:
                    for record in segment.completion_by_route_alias.get(alias, ()):
                        selected[record.order] = record
        return tuple(selected[key] for key in sorted(selected))

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

    def prefix_proofs(self):
        """``(byte length, SHA-256 of those leading bytes, rows inside)`` per row boundary.

        Every digest covers the file from offset 0, so an equal digest at an equal
        length proves the same leading rows, whichever inode, read path, or
        rename produced either view. Ascending; empty for a registry the LF
        segmentation does not describe.
        """
        if not self._prefix_reuse_eligible:
            return ()
        proofs = []
        for segment in self._segments:
            proofs.extend(segment.inner_proofs)
            if segment.byte_end is not None:
                proofs.append((segment.byte_end, segment.prefix_sha256, segment.order_end))
        return tuple(proofs)

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

    PARENT_SESSION_KEYS = ("parent_sid", "parent_harness", "main_runtime_harness",
                           "parent_completion_delivery")

    def handover_parent_sessions(self) -> tuple[tuple[str | None, ...], ...]:
        """Distinct ``PARENT_SESSION_KEYS`` values of each attempt's latest row.

        Projected once per immutable generation, so a caller that scans every
        parent session on each pass does not re-parse every historical row.
        """
        if self._parent_sessions is None:
            latest = {}
            for segment in self._segments:
                latest.update(segment.handover_latest)
            distinct = {}
            for _status, record in latest.values():
                metadata = dict(record._handover_pairs or _parse_handover(record.fields[5]))
                distinct[tuple(metadata.get(key) for key in self.PARENT_SESSION_KEYS)] = None
            self._parent_sessions = tuple(distinct)
        return self._parent_sessions

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
    # The scope's pinned ``[view, lines]``; most consumers read only the view.
    _pinned: list = field(repr=False, compare=False)
    complete: bool
    reason: str = ""

    @property
    def lines(self) -> tuple[str, ...]:
        """The exact text lines of this generation, decoded on first use."""
        return _pinned_lines(self._pinned)

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


def _stable_read(path: Path, path_identity: str):
    """Read one stable generation and return its still-open descriptor to cache."""
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
            if (fd_before.st_nlink <= 0
                    or (path_before.st_dev, path_before.st_ino) != (fd_before.st_dev, fd_before.st_ino)
                    or _stat_signature(fd_before) != before_signature):
                raise OSError("registry changed before read")
            chunks = []
            size = 0
            _METRICS["content_reads"] += 1
            # One read sized to the stat result lets join() return that buffer
            # itself instead of copying a second full-size registry image.
            request = max(_CHUNK_SIZE, before_signature[2] + 1)
            while True:
                chunk = os.read(fd, request)
                request = _CHUNK_SIZE
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                _METRICS["bytes_read"] += len(chunk)

            fd_after = os.fstat(fd)
            path_after = path.stat()
            resolved_after = str(path.resolve(strict=True))
            after_signature = _stat_signature(path_after)
            if (resolved_before != resolved_after or fd_after.st_nlink <= 0
                    or (path_after.st_dev, path_after.st_ino) != (fd_after.st_dev, fd_after.st_ino)
                    or before_signature != _stat_signature(fd_after)
                    or before_signature != after_signature
                    or size != before_signature[2]):
                raise OSError("registry changed during read")
            owned_fd, fd = fd, None
            return resolved_after, after_signature, b"".join(chunks), owned_fd
        except (OSError, RuntimeError) as exc:
            last_error = exc
        finally:
            if fd is not None:
                os.close(fd)
    raise OSError(getattr(last_error, "errno", None),
                  f"could not read a stable registry generation: {path_identity}") from last_error


def _close_view_fd(view: RegistryView | None) -> None:
    if view is None:
        return
    fd, view._fd = view._fd, -1
    if fd >= 0:
        with contextlib.suppress(OSError):
            os.close(fd)


def _path_matches_fd(path: Path, resolved: str, signature: tuple, fd: int) -> bool:
    try:
        path_info = path.stat()
        fd_info = os.fstat(fd)
        return (str(path.resolve(strict=True)) == resolved
                and path_info.st_dev == fd_info.st_dev
                and path_info.st_ino == fd_info.st_ino
                and fd_info.st_nlink > 0
                and _stat_signature(path_info) == signature
                and _stat_signature(fd_info) == signature)
    except (OSError, RuntimeError, ValueError):
        return False


def _settled_read(fd: int, started_ns: int) -> bool:
    """Whether bytes read after ``started_ns`` predate every possible later write.

    A write within one timestamp tick of the previous write can keep size, mtime
    and ctime equal. Once a read starts at least the granularity margin after the
    file's last change, a later write must carry a newer timestamp, so an equal
    stat proves the bytes unchanged (the "racily clean" rule).
    """
    try:
        info = os.fstat(fd)
    except OSError:
        return False
    fine = info.st_mtime_ns % 1_000_000_000 and info.st_ctime_ns % 1_000_000_000
    margin = min(_SETTLED_NS, _SETTLED_FINE_NS) if fine else _SETTLED_NS
    return started_ns - max(info.st_mtime_ns, info.st_ctime_ns) >= margin


def _current_cached(path: Path, identity: str, *, scoped: bool = False) -> "RegistryView | None":
    """The cached generation when the path and its retained fd still name it.

    One decision for every reader; the caller holds ``_LOCK``. Equal path and
    descriptor metadata identify the bytes that were read, so nothing is re-read.
    A scoped reader also needs bytes it can vouch for without a stat collision:
    a generation read too soon after its last write, or extended by an asserted
    append suffix, keeps its full read and digest comparison; so does a registry
    whose text lines differ from the view's rows.
    """
    cached = _CACHE.get(identity)
    if cached is None or cached._fd < 0:
        return None
    if scoped and (not cached._settled or cached._asserted_suffix
                   or not cached._prefix_reuse_eligible or cached.has_invalid_utf8):
        return None
    try:
        resolved = str(path.resolve(strict=True))
    except (OSError, RuntimeError):
        return None
    if (cached.resolved_path != resolved
            or not _path_matches_fd(path, resolved, cached.signature, cached._fd)):
        return None
    _METRICS["cache_hits"] += 1
    _CACHE.move_to_end(identity)
    cached.last_observed_at = time.monotonic()
    return cached


def _read_retained_suffix(path: Path, path_identity: str, cached: RegistryView):
    """Read appended bytes from the cache owner's continuously retained fd."""
    last_error = None
    for _attempt in range(_RETRIES):
        try:
            resolved_before = str(path.resolve(strict=True))
            path_before = path.stat()
            fd_before = os.fstat(cached._fd)
            before_signature = _stat_signature(path_before)
            fd_signature = _stat_signature(fd_before)
            if (resolved_before != cached.resolved_path or fd_before.st_nlink <= 0
                    or fd_signature[:2] != cached.signature[:2]
                    or (path_before.st_dev, path_before.st_ino) != (fd_before.st_dev, fd_before.st_ino)
                    or before_signature[:2] != cached.signature[:2]
                    or before_signature[2] <= cached.signature[2]):
                raise OSError("registry append generation changed")
            os.lseek(cached._fd, cached.signature[2], os.SEEK_SET)
            chunks, size = [], 0
            _METRICS["content_reads"] += 1
            while True:
                chunk = os.read(cached._fd, _CHUNK_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                _METRICS["bytes_read"] += len(chunk)
            fd_after = os.fstat(cached._fd)
            path_after = path.stat()
            resolved_after = str(path.resolve(strict=True))
            after_signature = _stat_signature(path_after)
            if (resolved_before != resolved_after
                    or before_signature != after_signature
                    or fd_signature[:2] != _stat_signature(fd_after)[:2]
                    or _stat_signature(fd_after) != after_signature
                    or fd_after.st_nlink <= 0
                    or (path_after.st_dev, path_after.st_ino) != (fd_after.st_dev, fd_after.st_ino)
                    or size != after_signature[2] - cached.signature[2]):
                raise OSError("registry changed during append read")
            _METRICS["delta_reads"] += 1
            _METRICS["delta_bytes_read"] += size
            return resolved_after, after_signature, b"".join(chunks)
        except (OSError, RuntimeError, ValueError) as exc:
            last_error = exc
    raise OSError(getattr(last_error, "errno", None),
                  f"could not read a stable registry append: {path_identity}") from last_error


@contextlib.contextmanager
def registry_read_scope(*, pinned: "RegistryView | None" = None):
    """Pin one stable generation of each registry for one passive Fleet pass.

    Pass lifetime of the callers in the delivery path: a sweep opens exactly one
    scope for discovery (``dispatch_session_sweep.addressed_records``), one
    fresh scope per post-claim effect group, a claim opens its own scope under
    the record lock (``dispatch_pending_delivery.claim`` ->
    ``_replacement_disposition``), and rendering opens one after the claims
    (``_render_context``). Each effect scope is paired with one
    ``PeerReadContext``. A claim is an effect boundary, so the group that hands
    records over must observe the registry outside the discovery scope: a
    registry append between discovery and effect is seen by the effect check,
    not hidden behind the earlier pin. Read-only history readers keep the same
    per-pass scope and never reuse a delivery authority across a write.

    ``pinned`` seeds a read-only observation scope with a generation the caller
    already read, so nested readers share it instead of reading the file again.
    It is for a manual observation section only (the owner-settlement
    classification, whose settlement writes run after the scope closes): the
    seeded generation is held until that scope ends and is never carried into a
    discovery or effect scope, so a claim or an effect group still reads the
    registry fresh. An outer scope keeps its own generation and ``pinned`` is
    ignored.
    """
    current = _READ_SCOPE.get()
    if current is not None:
        yield
        return
    scope = {}
    if (pinned is not None and pinned._prefix_reuse_eligible
            and not pinned.has_invalid_utf8):
        scope[pinned.path_identity] = [pinned, None]
    token = _READ_SCOPE.set(scope)
    try:
        yield
    finally:
        _READ_SCOPE.reset(token)


def registry_scope_active() -> bool:
    return _READ_SCOPE.get() is not None


def _pinned_generation(scope: dict, jobs: Path) -> list:
    """The pinned ``[view, lines]`` of one path; ``lines`` stays None until asked."""
    path = Path(jobs)
    key = str(path.absolute())
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


def _observe_generation(path: Path) -> list:
    """Observe one stable generation as ``[view, lines]``.

    ``lines`` is None when the view's own lines already equal
    ``read_text(errors="replace").splitlines()`` of the same bytes (LF-only
    registry, valid UTF-8 prefix); otherwise it is those plain text lines, which
    the LF-segmented view does not promise for other line separators.
    A cached generation that the path and its retained descriptor still name is
    returned without reading; any other generation is read in full and its
    prefix digest decides how much of the cached parse it may reuse.
    """
    identity = str(path.absolute())
    with _LOCK:
        current = _current_cached(path, identity, scoped=True)
        if current is not None:
            return [current, None]
        fd = None
        try:
            path.resolve(strict=True)  # a missing path fails at once, as in registry_view
            started_ns = time.time_ns()
            resolved, signature, raw, fd = _stable_read(path, identity)
        except OSError:
            if _drop(identity) is not None:
                _METRICS["invalidations"] += 1
            raise
        except RuntimeError as exc:
            if _drop(identity) is not None:
                _METRICS["invalidations"] += 1
            raise OSError(f"could not resolve registry path: {identity}") from exc
        settled = _settled_read(fd, started_ns)
        cached = _CACHE.get(identity)
        if (cached is not None and cached.resolved_path == resolved
                and cached.signature == signature
                and hashlib.sha256(raw).digest() == cached._raw_digest.digest()):
            os.close(fd)
            fd = None
            _METRICS["cache_hits"] += 1
            _CACHE.move_to_end(identity)
            cached.last_observed_at = time.monotonic()
            cached._asserted_suffix = False  # the full digest now vouches for it
            cached._settled = cached._settled or settled
            view = cached
        else:
            reuse = _reusable_prefix(cached, raw, resolved)
            _drop(identity, keep_parse=bool(reuse and reuse[0]))
            _METRICS["invalidations"] += 1
            cached = None  # the old generation is not held while the new one parses
            view = RegistryView(identity, resolved, signature, raw, fd=fd,
                                settled=settled, reuse=reuse)
            fd = None
            _count_generation(view, reuse)
            _remember(identity, view)
        if fd is not None:
            os.close(fd)
    if view._prefix_reuse_eligible and not view.has_invalid_utf8:
        # Valid LF-only text: the view's lines are exactly the splitlines() of
        # these bytes, so the scope builds them from the view only on demand
        # instead of holding a second full-size decode of the whole registry.
        return [view, None]
    return [view, tuple(raw.decode("utf-8", errors="replace").splitlines())]


def _pinned_lines(entry: list) -> tuple[str, ...]:
    """Materialize the scope's text lines once, from the view when it is exact."""
    if entry[1] is None:
        entry[1] = entry[0].lines
    return entry[1]


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
    return _pinned_lines(_pinned_generation(scope, jobs))


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
    entry = _pinned_generation(scope, path)
    view = entry[0]
    reason = view.completeness_reason
    return RegistrySnapshot(
        path_identity=str(path.absolute()), resolved_path=view.resolved_path,
        signature=tuple(view.signature), view=view, _pinned=entry,
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
        except RuntimeError as exc:
            if _drop(identity) is not None:
                _METRICS["invalidations"] += 1
            raise OSError(f"could not resolve registry path: {identity}") from exc

        current = _current_cached(path, identity)
        if current is not None:
            return current

        cached = _CACHE.get(identity)
        can_append = bool(
            append_only and cached is not None and cached.resolved_path == resolved
            and cached._fd >= 0
            and cached.signature[:2] == signature[:2]
            and signature[2] > cached.signature[2]
            and time.monotonic() - cached.last_observed_at <= _APPEND_OBSERVATION_GAP_SECONDS
        )
        previous = None
        fd = -1
        if can_append:
            try:
                observed_resolved, observed_signature, raw = _read_retained_suffix(path, identity, cached)
                if (observed_resolved == cached.resolved_path
                        and observed_signature[:2] == cached.signature[:2]
                        and observed_signature[2] > cached.signature[2]):
                    resolved, signature = observed_resolved, observed_signature
                    previous = cached
                    fd, cached._fd = cached._fd, -1
                else:
                    raise OSError("registry append generation changed")
            except OSError:
                _drop(identity)
                cached = None
                _METRICS["invalidations"] += 1
                previous = None
        started_ns = time.time_ns()
        reuse = None
        if previous is None:
            # The cached generation stays until the new bytes are read: its
            # segment digests decide how much of its parse they may keep.
            stale, cached = cached, None
            try:
                resolved, signature, raw, fd = _stable_read(path, identity)
            except OSError:
                if _drop(identity) is not None:
                    _METRICS["invalidations"] += 1
                raise
            reuse = _reusable_prefix(stale, raw, resolved)
            if stale is not None:
                _drop(identity, keep_parse=bool(reuse and reuse[0]))
                _METRICS["invalidations"] += 1
            stale = None
        else:
            _drop(identity, keep_parse=True)
            cached = None
            _METRICS["invalidations"] += 1
        view = RegistryView(identity, resolved, signature, raw, previous=previous, fd=fd,
                            asserted_suffix=previous is not None,
                            settled=previous is None and _settled_read(fd, started_ns),
                            reuse=reuse)
        if previous is None:
            _count_generation(view, reuse)
        else:
            _METRICS["append_reuses"] += 1
            _METRICS["metadata_rows_parsed"] += view.record_count - previous.record_count
        _remember(identity, view)
        return view


def _count_generation(view: RegistryView, reuse) -> None:
    """Account one generation built from a full read, with or without a reused prefix."""
    kept = len(reuse[0]) if reuse is not None else 0
    reused_rows = sum(len(segment.records) for segment in reuse[0]) if kept else 0
    # Rows of segments recognised again past the first mismatch were not parsed either.
    _METRICS["metadata_rows_parsed"] += view.record_count - reused_rows - view._resynced_rows
    if not kept:
        _METRICS["full_metadata_parses"] += 1
    elif reuse[4]:
        _METRICS["append_reuses"] += 1
    else:
        _METRICS["prefix_reuses"] += 1
        _METRICS["prefix_segments_reused"] += kept
        _METRICS["prefix_bytes_reused"] += reuse[1]


def _drop(identity: str, *, keep_parse: bool = False) -> "RegistryView | None":
    """Forget one cached generation; the caller holds ``_LOCK``.

    The lazy-parse LRU belongs to the view's lifetime, so it is cleared with the
    view unless an append successor is about to continue the same generation.
    """
    global _CACHE_BYTES
    removed = _CACHE.pop(identity, None)
    if removed is not None:
        _CACHE_BYTES -= removed.signature[2]
        _close_view_fd(removed)
        if not keep_parse:
            _clear_parse_caches()
    return removed


def _remember(identity: str, view: RegistryView) -> None:
    """Cache one generation within the existing bounds; the caller holds ``_LOCK``.

    The byte bound limits the paths beside the largest cached registry, so one
    large registry (the live jobs.log) is neither evicted for its own size nor
    pushed out by a second, smaller path. The oldest other path leaves first;
    the generation just remembered is never evicted for its own size.
    """
    global _CACHE_BYTES
    _CACHE[identity] = view
    _CACHE_BYTES += view.signature[2]
    while len(_CACHE) > 1:
        largest = max(_CACHE, key=lambda key: _CACHE[key].signature[2])
        if (len(_CACHE) <= _CACHE_MAX_PATHS
                and _CACHE_BYTES - _CACHE[largest].signature[2] <= _CACHE_MAX_BYTES):
            break
        victim = next((key for key in _CACHE if key not in (largest, identity)),
                      next(key for key in _CACHE if key != identity))
        evicted = _CACHE.pop(victim)
        _CACHE_BYTES -= evicted.signature[2]
        _close_view_fd(evicted)
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
        for view in _CACHE.values():
            _close_view_fd(view)
        _CACHE.clear()
        _CACHE_BYTES = 0
        _clear_parse_caches()


atexit.register(clear_cache)
