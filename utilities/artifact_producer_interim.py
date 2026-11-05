"""Artifact producer: open-cycle interim manifests and checkpoint state.

Interim reservations, checkpoint limits and locks, the checkpoint scan, and the
removal of an empty open cycle.

Split out of `artifact_producer.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import contextlib
from dataclasses import asdict, dataclass, field, replace
import fcntl
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import artifact_identity
import artifact_locator
import artifact_manifest

from artifact_producer_store import (
    _campaign_path, _ensure_dir, _read_json, _write_campaign, campaign_dir, cycle_dir,
    cycle_record_path, producer_dir, ProducerError, read_campaign, read_cycle_record,
)


# ---------------------------------------------------------------------------
# open-cycle checkpoint: the interim manifest
# ---------------------------------------------------------------------------
#
# A consumer that mirrors artifacts (Cairn) can only read a sealed cycle's
# `manifest.json`.  A checkpoint publishes the same closed D-6 document for a
# cycle that is still open -- `cycle.state` is `open` -- under
# `.runtime/artifact-producer/v1/open-manifests/<cyc>.json`.
#
# IDs come from one cumulative reservation ledger,
# `checkpoints/<cyc>.ids.json`: every locator a checkpoint ever published keeps
# its artifact ID there, even while a later checkpoint leaves the file out
# (grown past a size limit, briefly missing), and a revision keeps its ID,
# provenance and recording event while its digest is unchanged.  Every later
# checkpoint and the final `finalize` assign IDs from that ledger, so a
# consumer row survives the seal.  Sealing removes the interim files.
# `checkpoints/<cyc>.json` is bookkeeping only (interval, last result, digest
# cache).

OPEN_MANIFEST_DIR = "open-manifests"
CHECKPOINT_DIR = "checkpoints"
CHECKPOINT_TRIGGERS = ("explicit", "stage-complete", "supervisor-poll", "turn-end", "begin")
CHECKPOINT_INTERVAL_ENV = "AGENT_ARTIFACT_CHECKPOINT_MIN_INTERVAL"
CHECKPOINT_MIN_INTERVAL_SECONDS = 900.0
# An automatic trigger never publishes a first interim document for a cycle
# whose files have not changed for this long: that cycle is not live work.
CHECKPOINT_STALE_SECONDS = 24 * 3600.0
CHECKPOINT_FINALIZE_LOCK_SECONDS = 60.0
RESERVATION_SCHEMA_VERSION = 1
# Earlier revisions a locator keeps in the ledger, so content that returns to
# an earlier digest returns to that revision's ID.
RESERVATION_HISTORY_LIMIT = 16
# A route is finalized by the release it was sealed to.  Only a release that
# carries this file keeps interim IDs at the seal, so a cycle whose route is
# sealed to an older release publishes no interim document.
INTERIM_SUPPORT_MARKER = "utilities/artifact_checkpoint_trigger.py"
# Weights, archives, and array dumps are not something a reader opens; they
# stay on disk and are declared only by the sealed manifest.
CHECKPOINT_EXCLUDED_SUFFIXES = frozenset({
    ".pt", ".pth", ".ckpt", ".safetensors", ".bin", ".onnx", ".h5", ".hdf5", ".pkl",
    ".pickle", ".joblib", ".npy", ".npz", ".tflite", ".pb", ".tar", ".zip", ".gz", ".tgz",
    ".xz", ".bz2", ".7z", ".zst", ".tfrecord", ".arrow", ".parquet", ".lmdb", ".mdb",
    ".nemo", ".gguf",
})
_STALE_TEMP_SECONDS = 3600.0


@dataclass(frozen=True)
class CheckpointLimits:
    max_walk_entries: int = 20000
    max_files: int = 2000
    max_total_bytes: int = 256 * 1024 * 1024
    max_file_bytes: int = 32 * 1024 * 1024

    def to_payload(self) -> Dict[str, int]:
        return {"max_walk_entries": self.max_walk_entries, "max_files": self.max_files,
                "max_total_bytes": self.max_total_bytes, "max_file_bytes": self.max_file_bytes}


@dataclass(frozen=True)
class ReservedRevision:
    artifact_id: str
    artifact_revision_id: str
    content_digest: str
    # The manifest revision that first recorded this artifact revision and its
    # `artifact.revision.recorded` event; reused while the digest is unchanged.
    recorded_in: Optional[str] = None
    event_id: Optional[str] = None
    stream_id: Optional[str] = None
    recorded_at: Optional[str] = None


@dataclass(frozen=True)
class InterimReservation:
    manifest_id: Optional[str]
    artifacts: Dict[str, ReservedRevision]
    dropped: int = 0
    # locator -> content_digest -> an earlier revision of the same artifact
    history: Dict[str, Dict[str, ReservedRevision]] = field(default_factory=dict)

    def revision_for(self, locator: str, digest: str) -> Optional[ReservedRevision]:
        current = self.artifacts.get(locator)
        if current is not None and current.content_digest == digest:
            return current
        return self.history.get(locator, {}).get(digest) if current is not None else None


def _without_event_reuse(reserved: InterimReservation) -> InterimReservation:
    """The same IDs, with every reused provenance/event field dropped."""
    def bare(row: ReservedRevision) -> ReservedRevision:
        return replace(row, recorded_in=None, event_id=None, stream_id=None, recorded_at=None)
    return InterimReservation(
        reserved.manifest_id, {loc: bare(row) for loc, row in reserved.artifacts.items()}, reserved.dropped,
        {loc: {d: bare(row) for d, row in rows.items()} for loc, rows in reserved.history.items()},
    )


def open_manifest_path(root: Path, cycle_id: str) -> Path:
    if not artifact_identity.is_well_formed(cycle_id, "cycle"):
        raise ProducerError("cycle-id-invalid", str(cycle_id))
    return producer_dir(root) / OPEN_MANIFEST_DIR / f"{cycle_id}.json"


def checkpoint_state_path(root: Path, cycle_id: str) -> Path:
    if not artifact_identity.is_well_formed(cycle_id, "cycle"):
        raise ProducerError("cycle-id-invalid", str(cycle_id))
    return producer_dir(root) / CHECKPOINT_DIR / f"{cycle_id}.json"


def reservation_path(root: Path, cycle_id: str) -> Path:
    return checkpoint_state_path(root, cycle_id).with_suffix(".ids.json")


def _checkpoint_lock_path(root: Path, cycle_id: str) -> Path:
    return checkpoint_state_path(root, cycle_id).with_suffix(".lock")


CLOSED_CYCLE_REFRESH_OFF_FILE = "cycle-refresh.off"
CHECKPOINT_LIMITS_FILE = "checkpoint-limits.json"
CHECKPOINT_LIMITS_MAX_BYTES = 64 * 1024


def closed_cycle_refresh_off() -> bool:
    """A machine-local switch that pauses the §45 closed-cycle refresh (and its sweep).

    `${XDG_CONFIG_HOME:-~/.config}/hearting/cycle-refresh.off` present = paused.  It lets a
    reader that does not yet follow updated closed-cycle manifests (Cairn sync) catch up
    before closed cycles start changing; open-cycle checkpoints are unaffected.  Absent
    file, the default, = on.  Read on every call so all three runtimes follow it at once."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    try:
        return os.path.exists(os.path.join(base, "hearting", CLOSED_CYCLE_REFRESH_OFF_FILE))
    except (OSError, ValueError):
        return False


def checkpoint_interval_seconds() -> float:
    raw = os.environ.get(CHECKPOINT_INTERVAL_ENV, "")
    try:
        value = float(raw) if raw else CHECKPOINT_MIN_INTERVAL_SECONDS
    except ValueError:
        return CHECKPOINT_MIN_INTERVAL_SECONDS
    return value if value >= 0 else CHECKPOINT_MIN_INTERVAL_SECONDS


def _checkpoint_limits_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "hearting" / CHECKPOINT_LIMITS_FILE


def checkpoint_limits_from_config() -> Tuple[CheckpointLimits, str]:
    """Read optional interim limits on every call; malformed files use defaults."""
    defaults = CheckpointLimits()
    path = _checkpoint_limits_path()
    try:
        before = path.lstat()
    except FileNotFoundError:
        return defaults, "missing"
    except OSError:
        return defaults, "unreadable"
    if not stat.S_ISREG(before.st_mode) or not getattr(os, "O_NOFOLLOW", 0):
        return defaults, "not-regular-no-follow"
    fd = None
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            return defaults, "changed-during-open"
        if opened.st_size > CHECKPOINT_LIMITS_MAX_BYTES:
            return defaults, "too-large"
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(CHECKPOINT_LIMITS_MAX_BYTES + 1)
        if len(raw) > CHECKPOINT_LIMITS_MAX_BYTES:
            return defaults, "too-large"
        after = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            return defaults, "changed-during-read"
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate-key")
                result[key] = value
            return result
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                              parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        if not isinstance(document, dict):
            raise ValueError("not-object")
        allowed = {"max_files", "max_total_bytes", "max_scanned_files", "max_walk_entries", "max_file_bytes"}
        if set(document) - allowed:
            raise ValueError("unknown-key")
        if "max_scanned_files" in document and "max_walk_entries" in document:
            if document["max_scanned_files"] != document["max_walk_entries"]:
                raise ValueError("alias-conflict")
        values = defaults.to_payload()
        for key, value in document.items():
            target = "max_walk_entries" if key in {"max_scanned_files", "max_walk_entries"} else key
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**63 - 1:
                raise ValueError("invalid-value")
            values[target] = value
        return CheckpointLimits(**values), "loaded"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return defaults, "invalid"
    finally:
        if fd is not None:
            os.close(fd)


@contextlib.contextmanager
def _checkpoint_lock(root: Path, cycle_id: str, *, timeout: float):
    """Per-cycle lock shared by checkpoint (ID assignment + writes) and finalize
    (ID reuse through interim removal).  Never taken before the admission lock."""
    path = _checkpoint_lock_path(root, cycle_id)
    _ensure_dir(path.parent)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ProducerError("checkpoint-lock-busy", cycle_id)
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _well_formed_or_none(value: Any, kind: str) -> Optional[str]:
    return value if isinstance(value, str) and artifact_identity.is_well_formed(value, kind) else None


def _reserved_revision(row: Any, artifact_id: str) -> Optional[ReservedRevision]:
    """One revision entry, checked by ID format only.  Reused provenance and
    event fields must also pass the manifest's own value rules; a field that
    does not is dropped (a fresh one is issued), never carried into a seal."""
    if not isinstance(row, Mapping):
        return None
    arev_id = _well_formed_or_none(row.get("artifact_revision_id"), "artifact_revision")
    digest = row.get("content_digest")
    if arev_id is None or not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        return None
    recorded_at = row.get("recorded_at")
    return ReservedRevision(
        artifact_id, arev_id, digest,
        recorded_in=_well_formed_or_none(row.get("recorded_in"), "manifest_revision"),
        event_id=_well_formed_or_none(row.get("event_id"), "event"),
        stream_id=_well_formed_or_none(row.get("stream_id"), "stream"),
        recorded_at=(recorded_at if isinstance(recorded_at, str)
                     and artifact_manifest._RFC3339_RE.match(recorded_at) else None),
    )


def _reserved_row(locator: Any, row: Any) -> Optional[Tuple[ReservedRevision, List[ReservedRevision]]]:
    """One ledger row (current revision + earlier ones), checked by ID format and
    locator grammar only -- a later manifest schema or contract version never
    invalidates a reservation."""
    if not isinstance(locator, str) or not isinstance(row, Mapping):
        return None
    if not artifact_manifest.validate_locator_path(locator).ok:
        return None
    art_id = _well_formed_or_none(row.get("artifact_id"), "artifact")
    current = _reserved_revision(row, art_id) if art_id else None
    if current is None:
        return None
    raw = row.get("history")
    earlier = [_reserved_revision(item, art_id) for item in (raw if isinstance(raw, list) else [])]
    return current, [rev for rev in earlier if rev is not None]


def _reservation_from_rows(manifest_id: Any, rows: Iterable[Tuple[Any, Any]]) -> InterimReservation:
    kept: Dict[str, ReservedRevision] = {}
    history: Dict[str, Dict[str, ReservedRevision]] = {}
    seen_artifacts: Set[str] = set()
    seen_revisions: Set[str] = set()
    seen_events: Set[str] = set()
    seen_streams: Set[str] = set()
    dropped = 0

    def unique_events(rev: ReservedRevision) -> ReservedRevision:
        # A duplicated event or stream ID would make the sealed manifest invalid;
        # keep the revision and let it record a fresh event instead.
        if (rev.event_id in seen_events or rev.stream_id in seen_streams
                or not (rev.event_id and rev.stream_id and rev.recorded_at)):
            return replace(rev, event_id=None, stream_id=None, recorded_at=None)
        seen_events.add(rev.event_id)
        seen_streams.add(rev.stream_id)
        return rev

    for locator, row in rows:
        parsed = _reserved_row(locator, row)
        # A duplicated artifact or revision ID would make the next manifest
        # invalid; drop the row.
        if (parsed is None or locator in kept or parsed[0].artifact_id in seen_artifacts
                or parsed[0].artifact_revision_id in seen_revisions):
            dropped += 1
            continue
        current, earlier = parsed
        kept[locator] = unique_events(current)
        seen_artifacts.add(current.artifact_id)
        seen_revisions.add(current.artifact_revision_id)
        for rev in earlier:
            if (rev.artifact_revision_id in seen_revisions or rev.content_digest == current.content_digest
                    or rev.content_digest in history.get(locator, {})):
                continue
            history.setdefault(locator, {})[rev.content_digest] = unique_events(rev)
            seen_revisions.add(rev.artifact_revision_id)
    return InterimReservation(_well_formed_or_none(manifest_id, "manifest"), kept, dropped, history)


def _reservation_from_document(document: Mapping[str, Any]) -> InterimReservation:
    """Fallback when the ledger is gone: the published interim document's rows."""
    events = {row.get("target_id"): row for row in document.get("events") or []
              if isinstance(row, Mapping) and row.get("event_type") == "artifact.revision.recorded"}
    rows = []
    for revision in document.get("artifact_revisions") or []:
        if not isinstance(revision, Mapping):
            continue
        locator = (revision.get("locator") or {}).get("path") if isinstance(revision.get("locator"), Mapping) else None
        event = events.get(revision.get("artifact_id")) or {}
        provenance = revision.get("provenance") if isinstance(revision.get("provenance"), Mapping) else {}
        rows.append((locator, {
            "artifact_id": revision.get("artifact_id"),
            "artifact_revision_id": revision.get("artifact_revision_id"),
            "content_digest": revision.get("content_digest"),
            "recorded_in": provenance.get("source_revision_id"),
            "event_id": event.get("event_id"), "stream_id": event.get("stream_id"),
            "recorded_at": event.get("recorded_at"),
        }))
    return _reservation_from_rows(document.get("manifest_id"), rows)


def read_interim_reservation(root: Path, record: Mapping[str, Any]) -> Tuple[Optional[InterimReservation], str]:
    """The cycle's ID reservation and a status word: `absent`, `present`,
    `document-fallback` (ledger missing, rebuilt from the interim document),
    `unreadable`, or `identity-mismatch`.  For the last two the returned
    reservation is the document fallback, when one can be read."""
    cid = record["cycle_id"]
    fallback: Optional[InterimReservation] = None
    document = _read_json(open_manifest_path(root, cid))
    if document is not None and isinstance(document.get("cycle"), Mapping) \
            and document["cycle"].get("cycle_id") == cid:
        fallback = _reservation_from_document(document)
    ledger_path = reservation_path(root, cid)
    try:
        ledger_path.lstat()
    except FileNotFoundError:
        return (fallback, "document-fallback") if fallback is not None else (None, "absent")
    except OSError:
        return fallback, "unreadable"
    payload = _read_json(ledger_path)
    if payload is None or not isinstance(payload.get("artifacts"), Mapping):
        return fallback, "unreadable"
    if (payload.get("cycle_id") != cid or payload.get("campaign_id") != record.get("campaign_id")
            or payload.get("route_id") != record.get("route_id")):
        return fallback, "identity-mismatch"
    return _reservation_from_rows(payload.get("manifest_id"), sorted(payload["artifacts"].items())), "present"


def _ledger_row(current: ReservedRevision, earlier: Mapping[str, ReservedRevision]) -> Dict[str, Any]:
    row = {key: value for key, value in asdict(current).items() if value is not None}
    kept = [rev for digest, rev in earlier.items() if digest != current.content_digest]
    if kept:
        row["history"] = [
            {key: value for key, value in asdict(rev).items() if value is not None and key != "artifact_id"}
            for rev in kept[-RESERVATION_HISTORY_LIMIT:]
        ]
    return row


def _reservation_payload(record: Mapping[str, Any], document: Mapping[str, Any],
                         previous: Optional[InterimReservation]) -> Dict[str, Any]:
    """Union of the previous reservation and this document -- never shrinks.  A
    locator whose content changed keeps its earlier revision in `history`."""
    rows: Dict[str, Dict[str, Any]] = {}
    if previous is not None:
        for locator, row in previous.artifacts.items():
            rows[locator] = _ledger_row(row, previous.history.get(locator, {}))
    events = {row["target_id"]: row for row in document["events"]
              if row["event_type"] == "artifact.revision.recorded"}
    for revision in document["artifact_revisions"]:
        locator = revision["locator"]["path"]
        event = events.get(revision["artifact_id"], {})
        current = ReservedRevision(
            revision["artifact_id"], revision["artifact_revision_id"], revision["content_digest"],
            recorded_in=revision["provenance"]["source_revision_id"],
            event_id=event.get("event_id"), stream_id=event.get("stream_id"),
            recorded_at=event.get("recorded_at"),
        )
        earlier: Dict[str, ReservedRevision] = {}
        if previous is not None:
            earlier = dict(previous.history.get(locator, {}))
            before = previous.artifacts.get(locator)
            if before is not None and before.content_digest != current.content_digest:
                earlier.pop(before.content_digest, None)
                earlier[before.content_digest] = before
        rows[locator] = _ledger_row(current, earlier)
    return {
        "schema_version": RESERVATION_SCHEMA_VERSION, "cycle_id": record["cycle_id"],
        "campaign_id": record["campaign_id"], "route_id": record.get("route_id"),
        "manifest_id": document["manifest_id"], "artifacts": dict(sorted(rows.items())),
    }


def remove_interim(root: Path, cycle_id: str) -> None:
    """Drop a cycle's interim document, reservation and bookkeeping (idempotent)."""
    if not artifact_identity.is_well_formed(cycle_id, "cycle"):
        return
    for path in (open_manifest_path(root, cycle_id), reservation_path(root, cycle_id),
                 checkpoint_state_path(root, cycle_id), _checkpoint_lock_path(root, cycle_id)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _sweep_orphan_interims(root: Path, *, now: Optional[float] = None) -> List[str]:
    """Remove interim files of cycles that are no longer open.  A cycle record
    that exists but cannot be read keeps its files (a transient read error must
    not cost a live cycle its IDs); abandoned temporary files are dropped."""
    clock = time.time() if now is None else now
    removed: List[str] = []
    for name in (OPEN_MANIFEST_DIR, CHECKPOINT_DIR):
        directory = producer_dir(root) / name
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.name.startswith(".") and ".tmp-" in entry.name:
                try:
                    if clock - entry.lstat().st_mtime > _STALE_TEMP_SECONDS:
                        entry.unlink()
                except OSError:
                    pass
                continue
            cycle_id = entry.name.split(".", 1)[0]
            if not artifact_identity.is_well_formed(cycle_id, "cycle") or cycle_id in removed:
                continue
            try:
                cycle_record_path(root, cycle_id).lstat()
            except FileNotFoundError:
                record: Optional[Dict[str, Any]] = {"state": "absent"}
            except OSError:
                continue
            else:
                record = read_cycle_record(root, cycle_id)
                if record is None:
                    continue
            if record.get("state") != "open":
                remove_interim(root, cycle_id)
                removed.append(cycle_id)
    return removed


def _route_release_supports_interim(route: Mapping[str, Any]) -> Tuple[bool, str]:
    tuple_ = route.get("launch_compatibility_tuple")
    runtime = tuple_.get("runtime_root") if isinstance(tuple_, Mapping) else None
    path = runtime.get("path") if isinstance(runtime, Mapping) else None
    if not isinstance(path, str) or not path:
        return False, "runtime-root-unsealed"
    return (Path(path) / INTERIM_SUPPORT_MARKER).is_file(), path


def _remove_empty_cycle(root: Path, record: Mapping[str, Any]) -> None:
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    artifacts = directory / "artifacts"
    binding = directory / artifact_locator.CYCLE_BINDING
    # A payload-only cycle has no manifest lineage, but its excluded files are
    # user-owned. Keep the locator, binding, campaign membership and tree so a
    # later normal operation can still find them; never remove contents here.
    try:
        with os.scandir(artifacts) as entries:
            if next(entries, None) is not None:
                return
    except FileNotFoundError:
        pass
    except OSError:
        return
    try:
        with os.scandir(directory) as entries:
            if any(entry.name not in {"artifacts", artifact_locator.CYCLE_BINDING}
                   for entry in entries):
                return
    except OSError:
        return
    artifact_locator.prepare_index_update(root, [record["campaign_id"]])
    if binding.is_file() and not binding.is_symlink():
        binding.unlink()
    for path in (artifacts, directory):
        try:
            path.rmdir()
        except OSError as exc:
            raise ProducerError("cycle-dir-not-empty", str(path)) from exc
    campaign = read_campaign(root, record["campaign_id"])
    if campaign is not None:
        campaign["cycles"] = [c for c in campaign.get("cycles", []) if c != record["cycle_id"]]
        if not campaign["cycles"] and campaign.get("key", "").endswith(record["route_id"]):
            # Campaign created by this begin and never populated: drop it.
            try:
                campaign_path = campaign_dir(root, campaign["campaign_id"], campaign)
                _campaign_path(root, campaign["campaign_id"], campaign).unlink()
                campaign_path.rmdir()
            except OSError:
                _write_campaign(root, campaign, exclusive=False)
        else:
            _write_campaign(root, campaign, exclusive=False)
    artifact_locator.update_indexes(root, [record["campaign_id"]])
