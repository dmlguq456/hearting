#!/usr/bin/env python3
"""Build the deterministic W4 Hearting artifact-knowledge feed.

The command surface is intentionally closed and read-only with respect to the
artifact root and Cairn. Mapping and feed files are written only to explicit,
external caller-selected paths; scan and export never invoke a model.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterable, NamedTuple, Sequence


SCHEMA = "hearting-artifact-knowledge-feed/v1"
SCHEMA_VERSION = 1
BOUNDARY_VERSION = "w1-v1"
PATTERN_VERSION = "w1-v1"
PATTERN_TEXT = (
    r"^(final_report|report|summary|pipeline_summary|overview|index|readme|"
    r".*_summary|.*_report)\.(md|ya?ml|json)$"
)
ENTRY_SUMMARY_RE = re.compile(PATTERN_TEXT, re.IGNORECASE)
MAPPING_NAMESPACE = "cairn-plans"
MAPPING_VERSION = "artifact-legacy-mapping/v1"
PINNED_CAIRN_COMMIT = "1fa0d99e4b714b5ce305f78c8f7c7773255e8f87"
PINNED_CAIRN_MODULE = "lib/artifact-projection/degraded.ts"

EXIT_USAGE = 64
EXIT_IDENTITY = 65
EXIT_MISSING = 66
EXIT_CAIRN = 69
EXIT_WRITE = 73
EXIT_DRIFT = 75

ID_RE = re.compile(r"^lk_[0-9a-f]{32}$")
REQUIRED_MAPPING = {
    "mapping_namespace",
    "mapping_version",
    "legacy_key_id",
    "migration_id",
    "canonical_source_key",
    "identity_class",
    "tombstone",
    "collision_group_id",
    "seed_provenance",
    "bucket_state",
}
CONSUMER_REQUIRED = {
    "last_successful_projection_at",
    "namespace_evidence",
    "namespace_id",
    "namespace_state",
    "projection_envelope_id",
}
REQUIRED_BUCKETS = ("plans", "research", "spec", "documents")
DECLARED_OPTIONAL_BUCKETS = ("experiments", "designs")
SOURCE_PREFERENCE = ("plan.md", "prd.md", "source.md", "source", "document.md")


class FeedError(Exception):
    """A public, typed producer failure."""

    def __init__(self, code: str, message: str, exit_code: int = EXIT_IDENTITY):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


class Cycle(NamedTuple):
    locator: str
    bucket: str
    physical_dir: Path
    fallback: bool
    members: tuple[Path, ...]


class ClosedParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise FeedError("usage", "invalid command arguments", EXIT_USAGE)


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError) as exc:
        raise FeedError("canonical-encoding", "value is not canonically encodable") from exc


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _utf8_key(value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FeedError("invalid-locator", "locator is not valid UTF-8") from exc


def migration_id(
    legacy_key_id: str,
    namespace: str = MAPPING_NAMESPACE,
    version: str = MAPPING_VERSION,
) -> str:
    if not ID_RE.fullmatch(legacy_key_id):
        raise FeedError("malformed-legacy-id", "malformed legacy identity")
    material = f"{namespace}|{version}|{legacy_key_id}".encode("utf-8")
    return "migration:" + hashlib.sha256(material).hexdigest()


def _absolute(value: str | Path, error: str = "absolute-path-required") -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise FeedError(error, "an absolute path is required", EXIT_USAGE)
    return path


def _root(value: str | Path) -> Path:
    path = _absolute(value, "absolute-root-required")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise FeedError("artifact-root-missing", "artifact root is unavailable", EXIT_MISSING) from exc
    if not resolved.is_dir():
        raise FeedError("artifact-root-missing", "artifact root is unavailable", EXIT_MISSING)
    return resolved


def _input_file(value: str | Path, code: str) -> Path:
    path = _absolute(value)
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        raise FeedError(code, "input is unavailable", EXIT_MISSING) from exc
    if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        raise FeedError(code, "input must be a regular non-symlink file", EXIT_MISSING)
    return path


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _resolved_output(value: str | Path, forbidden: Sequence[Path]) -> Path:
    path = _absolute(value)
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise FeedError("output-symlink", "output must not be a symlink", EXIT_WRITE)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise FeedError("output-write", "output path is unavailable", EXIT_WRITE) from exc
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise FeedError("output-write", "output path is unavailable", EXIT_WRITE) from exc
    for root in forbidden:
        if resolved == root or _contains(root, resolved):
            raise FeedError("output-containment", "output location is not permitted", EXIT_WRITE)
    return resolved


def _assert_external_input(path: Path, root: Path) -> None:
    resolved = path.resolve(strict=True)
    if resolved == root or _contains(root, resolved):
        raise FeedError("input-containment", "producer input must be external to the artifact root")


def _safe_locator(locator: str, *, directory: bool = False) -> str:
    if (
        not locator
        or locator.startswith("/")
        or "\\" in locator
        or "\x00" in locator
    ):
        raise FeedError("invalid-locator", "invalid relative locator")
    raw = locator[:-1] if locator.endswith("/") else locator
    parts = raw.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise FeedError("invalid-locator", "invalid relative locator")
    _utf8_key(locator)
    return raw + ("/" if directory else "")


def _locator(root: Path, path: Path, *, directory: bool = False) -> str:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise FeedError("invalid-locator", "path escapes artifact root") from exc
    return _safe_locator(relative, directory=directory)


def _scan_directory(path: Path) -> list[os.DirEntry[str]]:
    try:
        entries = list(os.scandir(path))
    except OSError as exc:
        raise FeedError(
            "inventory-incomplete",
            "cycle population cannot be observed",
            EXIT_DRIFT,
        ) from exc
    return sorted(entries, key=lambda entry: _utf8_key(entry.name))


def _real_directories(base: Path, *, excluded: set[str] | None = None) -> list[Path]:
    excluded = excluded or set()
    directories: list[Path] = []
    for entry in _scan_directory(base):
        if entry.name in excluded:
            continue
        try:
            mode = os.lstat(entry.path).st_mode
        except OSError as exc:
            raise FeedError("source-drift", "source changed during observation", EXIT_DRIFT) from exc
        if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
            directories.append(Path(entry.path))
    return directories


def _bucket(root: Path, name: str, *, optional: bool = False) -> Path | None:
    path = root / name
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        if optional:
            return None
        raise FeedError("inventory-incomplete", "required artifact bucket is missing", EXIT_DRIFT)
    except OSError as exc:
        raise FeedError("inventory-incomplete", "artifact bucket is unavailable", EXIT_DRIFT) from exc
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        raise FeedError("inventory-incomplete", "artifact bucket is unavailable", EXIT_DRIFT)
    return path


def _is_regular_nofollow(path: Path) -> bool:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise FeedError("source-drift", "source changed during observation", EXIT_DRIFT) from exc
    return stat.S_ISREG(mode) and not stat.S_ISLNK(mode)


def d23_cycles(root: Path, selection: str) -> list[Cycle]:
    if selection not in {"plans", "all-d23"}:
        raise FeedError("invalid-bucket", "unsupported bucket", EXIT_USAGE)

    cycles: list[Cycle] = []
    names = ("plans",) if selection == "plans" else REQUIRED_BUCKETS + DECLARED_OPTIONAL_BUCKETS
    for name in names:
        base = _bucket(root, name, optional=name in DECLARED_OPTIONAL_BUCKETS)
        if base is None:
            continue
        excluded = {"_scratch"} if name == "plans" else {"_internal"} if name == "spec" else set()
        for directory in _real_directories(base, excluded=excluded):
            cycles.append(
                Cycle(
                    locator=_locator(root, directory, directory=True),
                    bucket=name,
                    physical_dir=directory,
                    fallback=False,
                    members=(),
                )
            )

        if name == "spec":
            loose_names = {"prd.md", "pipeline_state.yaml", "pipeline_summary.md"}
            members = tuple(
                Path(entry.path)
                for entry in _scan_directory(base)
                if entry.name in loose_names and _is_regular_nofollow(Path(entry.path))
            )
            if members:
                cycles.append(
                    Cycle(
                        "spec/_unscoped-legacy-component/",
                        "spec",
                        base,
                        True,
                        tuple(sorted(members, key=lambda path: _utf8_key(path.name))),
                    )
                )
        elif name == "documents":
            members = tuple(
                Path(entry.path)
                for entry in _scan_directory(base)
                if entry.name.lower().endswith(".md") and _is_regular_nofollow(Path(entry.path))
            )
            if members:
                cycles.append(
                    Cycle(
                        "documents/_loose-documents/",
                        "documents",
                        base,
                        True,
                        tuple(sorted(members, key=lambda path: _utf8_key(path.name))),
                    )
                )

    cycles.sort(key=lambda cycle: _utf8_key(cycle.locator))
    locators = [cycle.locator for cycle in cycles]
    if len(locators) != len(set(locators)):
        raise FeedError("identity-collision", "cycle population contains duplicate locators")
    return cycles


def d23_population(root: Path, selection: str) -> list[str]:
    return [cycle.locator for cycle in d23_cycles(root, selection)]


def _inventory(root: Path, selection: str) -> tuple[list[dict[str, Any]], str]:
    rows: list[dict[str, Any]] = []
    names = ("plans",) if selection == "plans" else REQUIRED_BUCKETS + DECLARED_OPTIONAL_BUCKETS

    def visit(path: Path, locator: str) -> None:
        try:
            observed = os.lstat(path)
        except OSError as exc:
            raise FeedError("source-drift", "source changed during observation", EXIT_DRIFT) from exc
        mode = observed.st_mode
        if stat.S_ISLNK(mode):
            kind = "symlink"
        elif stat.S_ISDIR(mode):
            kind = "directory"
        elif stat.S_ISREG(mode):
            kind = "file"
        else:
            kind = "other"
        row: dict[str, Any] = {
            "locator": locator,
            "kind": kind,
            "mode": stat.S_IMODE(mode),
            "mtime_ns": int(observed.st_mtime_ns),
            "size": int(observed.st_size),
        }
        if kind == "file":
            try:
                row["digest"] = digest(path.read_bytes())
            except PermissionError:
                row["read_error"] = "EACCES"
            except OSError as exc:
                if exc.errno in {errno.ENOENT, errno.ESTALE}:
                    raise FeedError("source-drift", "source changed during observation", EXIT_DRIFT) from exc
                row["read_error"] = errno.errorcode.get(exc.errno or 0, "EIO")
        elif kind == "symlink":
            try:
                row["target"] = os.readlink(path)
            except OSError as exc:
                raise FeedError("source-drift", "source changed during observation", EXIT_DRIFT) from exc
        rows.append(row)
        if kind != "directory":
            return
        for entry in _scan_directory(path):
            if locator == "plans/" and entry.name == "_scratch":
                continue
            child = Path(entry.path)
            try:
                child_mode = os.lstat(child).st_mode
            except OSError as exc:
                raise FeedError("source-drift", "source changed during observation", EXIT_DRIFT) from exc
            is_directory = stat.S_ISDIR(child_mode) and not stat.S_ISLNK(child_mode)
            child_locator = locator + entry.name + ("/" if is_directory else "")
            visit(child, _safe_locator(child_locator, directory=is_directory))

    for name in names:
        base = _bucket(root, name, optional=name in DECLARED_OPTIONAL_BUCKETS)
        if base is None:
            rows.append({"locator": name + "/", "kind": "declared-absent"})
            continue
        visit(base, name + "/")

    rows.sort(key=lambda row: _utf8_key(str(row["locator"])))
    encoded = canonical(rows)
    return rows, digest(encoded)


def _record(locator: str, legacy_key_id: str) -> dict[str, Any]:
    return {
        "bucket_state": "present",
        "canonical_source_key": _safe_locator(locator, directory=True),
        "collision_group_id": None,
        "identity_class": "legacy",
        "legacy_key_id": legacy_key_id,
        "mapping_namespace": MAPPING_NAMESPACE,
        "mapping_version": MAPPING_VERSION,
        "migration_id": migration_id(legacy_key_id),
        "seed_provenance": "w4-mapping-init",
        "tombstone": False,
    }


def mapping_bytes(records: Iterable[dict[str, Any]]) -> bytes:
    ordered = sorted(
        records,
        key=lambda row: (
            _utf8_key(str(row.get("canonical_source_key", ""))),
            bool(row.get("tombstone")),
            _utf8_key(str(row.get("legacy_key_id", ""))),
        ),
    )
    return b"".join(canonical(row) + b"\n" for row in ordered)


def validate_mapping(
    data: bytes,
    population: Iterable[str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    if data.startswith(b"\xef\xbb\xbf") or (data and not data.endswith(b"\n")):
        raise FeedError("mapping-schema", "mapping must be UTF-8 JSONL ending in LF")
    try:
        records = [json.loads(line) for line in data.decode("utf-8").splitlines() if line]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeedError("mapping-schema", "invalid mapping JSONL") from exc

    seen_locator: set[str] = set()
    seen_key: set[str] = set()
    seen_migration: set[str] = set()
    for row in records:
        if not isinstance(row, dict) or set(row) != REQUIRED_MAPPING:
            raise FeedError("mapping-schema", "mapping keys mismatch")
        if (
            row["mapping_namespace"] != MAPPING_NAMESPACE
            or row["mapping_version"] != MAPPING_VERSION
            or row["identity_class"] != "legacy"
            or row["tombstone"] is not False
            or row["collision_group_id"] is not None
            or row["seed_provenance"] != "w4-mapping-init"
            or row["bucket_state"] != "present"
        ):
            raise FeedError("mapping-schema", "mapping contract mismatch")
        locator = _safe_locator(str(row["canonical_source_key"]), directory=True)
        key = row["legacy_key_id"]
        if not isinstance(key, str) or not ID_RE.fullmatch(key):
            raise FeedError("malformed-legacy-id", "malformed legacy identity")
        expected_migration = migration_id(key)
        if row["migration_id"] != expected_migration:
            raise FeedError("migration-id-mismatch", "migration identity mismatch")
        if locator in seen_locator or key in seen_key or expected_migration in seen_migration:
            raise FeedError("identity-collision", "mapping identity collision")
        seen_locator.add(locator)
        seen_key.add(key)
        seen_migration.add(expected_migration)

    if data != mapping_bytes(records):
        raise FeedError("mapping-order", "mapping is not canonical")
    if population is not None and seen_locator != set(population):
        raise FeedError("population-drift", "mapping population differs", EXIT_DRIFT)
    return records, digest(data)


def _atomic_write(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FeedError("output-write", "output directory is unavailable", EXIT_WRITE) from exc

    if exclusive:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            return
        except FileExistsError as exc:
            raise FeedError("output-exists", "output already exists", EXIT_WRITE) from exc
        except OSError as exc:
            raise FeedError("output-write", "output cannot be written", EXIT_WRITE) from exc

    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".artifact-knowledge-", dir=str(path.parent))
    except OSError as exc:
        raise FeedError("output-write", "output cannot be written", EXIT_WRITE) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise FeedError("output-write", "output cannot be written", EXIT_WRITE) from exc
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _candidate_files(cycle: Cycle) -> list[Path]:
    if cycle.fallback:
        return list(cycle.members)
    return [Path(entry.path) for entry in _scan_directory(cycle.physical_dir)]


def _file_state(path: Path) -> tuple[str, str | None, int | None]:
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return "missing", None, None
    except OSError as exc:
        raise FeedError("source-drift", "source changed during observation", EXIT_DRIFT) from exc
    if not stat.S_ISREG(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        return "unreadable", None, int(observed.st_mtime_ns)
    try:
        data = path.read_bytes()
    except PermissionError:
        return "unreadable", None, int(observed.st_mtime_ns)
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ESTALE}:
            raise FeedError("source-drift", "source changed during observation", EXIT_DRIFT) from exc
        return "unreadable", None, int(observed.st_mtime_ns)
    return "readable", digest(data), int(observed.st_mtime_ns)


def _summary(cycle: Cycle) -> tuple[Path | None, str, str | None, int | None]:
    matches: list[Path] = []
    for path in _candidate_files(cycle):
        if ENTRY_SUMMARY_RE.fullmatch(path.name) and _is_regular_nofollow(path):
            matches.append(path)
    if not matches:
        return None, "missing", None, None
    path = sorted(matches, key=lambda item: _utf8_key(item.name))[0]
    state, source_digest, mtime_ns = _file_state(path)
    if state != "readable":
        return path, "unreadable", source_digest, mtime_ns
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return path, "unreadable", source_digest, mtime_ns
    return path, "empty" if not text.strip() else "usable", source_digest, mtime_ns


def _source_candidate(cycle: Cycle) -> Path:
    candidates = _candidate_files(cycle)
    by_name = {path.name: path for path in candidates}
    for name in SOURCE_PREFERENCE:
        if name in by_name:
            return by_name[name]
    regular = [
        path
        for path in candidates
        if _is_regular_nofollow(path) and not ENTRY_SUMMARY_RE.fullmatch(path.name)
    ]
    if regular:
        return sorted(regular, key=lambda item: _utf8_key(item.name))[0]
    return cycle.physical_dir / "source"


def _cycle_inventory_rows(
    root: Path,
    rows: list[dict[str, Any]],
    cycle: Cycle,
) -> list[dict[str, Any]]:
    if cycle.fallback:
        wanted = {_locator(root, member) for member in cycle.members}
        selected = [row for row in rows if row["locator"] in wanted]
    else:
        prefix = cycle.locator
        selected = [
            row
            for row in rows
            if row["locator"] == prefix or str(row["locator"]).startswith(prefix)
        ]
    return sorted(selected, key=lambda row: _utf8_key(str(row["locator"])))


def _row(
    root: Path,
    cycle: Cycle,
    mapping: dict[str, Any],
    inventory: list[dict[str, Any]],
) -> dict[str, Any]:
    summary_path, summary_state, summary_digest, summary_mtime = _summary(cycle)
    manifest_path = cycle.physical_dir / "manifest.json"
    source_path = _source_candidate(cycle)
    manifest_state, manifest_digest, _ = _file_state(manifest_path)
    source_state, source_digest, source_mtime = _file_state(source_path)

    reason: str | None = None
    missing_fields: list[str] = []
    if summary_state == "missing":
        reason, missing_fields = "entry-summary-missing", ["entry_summary"]
    elif summary_state == "unreadable":
        reason, missing_fields = "source-unreadable", ["entry_summary"]
    elif summary_state == "empty":
        reason, missing_fields = "summary-generation-failed", ["entry_summary"]
    elif manifest_state == "missing":
        reason, missing_fields = "manifest-missing", ["manifest"]
    elif manifest_state != "readable":
        reason, missing_fields = "source-unreadable", ["manifest"]
    elif source_state != "readable":
        reason, missing_fields = "source-unreadable", ["canonical_raw_locator"]

    degraded = reason is not None
    cycle_inventory = _cycle_inventory_rows(root, inventory, cycle)
    inventory_digest = digest(canonical(cycle_inventory))
    record_digest = digest(canonical(mapping))
    locator = cycle.locator
    evidence_mtime = source_mtime
    if evidence_mtime is None:
        try:
            evidence_mtime = int(os.lstat(cycle.physical_dir).st_mtime_ns)
        except OSError as exc:
            raise FeedError("source-drift", "source changed during observation", EXIT_DRIFT) from exc

    return {
        "bucket": cycle.bucket,
        "canonical_raw_locator": locator,
        "canonical_source_key": locator,
        "conflict": False,
        "cycle_binding": {
            "boundary_version": BOUNDARY_VERSION,
            "bucket": cycle.bucket,
            "cycle_locator": locator,
            "fallback_container": cycle.fallback,
        },
        "degraded": degraded,
        "disposition": "path-only-degraded" if degraded else "complete",
        "entry_kind": "path_only_degraded" if degraded else "navigation",
        "freshness": {"source_mtime": evidence_mtime},
        "integrity": {
            "algorithm": "sha256",
            "mapping_record_digest": record_digest,
            "source_digest": inventory_digest,
        },
        "legacy_key_id": mapping["legacy_key_id"],
        "manifest_path": _locator(root, manifest_path) if manifest_state != "missing" else None,
        "mapping_version": MAPPING_VERSION,
        "missing_fields": missing_fields,
        "model_calls": 0,
        "partial": degraded,
        "producer_evidence": {
            "cycle_inventory_digest": inventory_digest,
            "manifest_digest": manifest_digest,
            "source_file_digest": source_digest,
            "summary_digest": summary_digest,
            "summary_state": summary_state,
            "summary_source_mtime": summary_mtime,
        },
        "reason": reason,
        "source_path": _locator(root, source_path) if source_state != "missing" else None,
        "summary_path": _locator(root, summary_path) if summary_path is not None else None,
        "target_id": mapping["migration_id"],
        "tombstone_state": None,
    }


def _seal_epoch(value: str) -> str:
    if not value or any(ord(character) < 0x20 for character in value):
        raise FeedError("invalid-seal-epoch", "seal epoch is invalid")
    return value


def build_feed(
    root: str | Path,
    selection: str,
    map_data: bytes,
    seal_epoch: str,
) -> dict[str, Any]:
    artifact_root = _root(root)
    seal = _seal_epoch(seal_epoch)
    before, before_digest = _inventory(artifact_root, selection)
    cycles = d23_cycles(artifact_root, selection)
    population = [cycle.locator for cycle in cycles]
    records, map_digest = validate_mapping(map_data, population)
    by_locator = {row["canonical_source_key"]: row for row in records}
    rows = [_row(artifact_root, cycle, by_locator[cycle.locator], before) for cycle in cycles]
    after, after_digest = _inventory(artifact_root, selection)
    if before_digest != after_digest or before != after:
        raise FeedError("source-drift", "source changed during observation", EXIT_DRIFT)

    missing_locators = {
        row["canonical_source_key"]
        for row in rows
        if row["producer_evidence"]["summary_state"] == "missing"
    }
    degraded_missing_locators = {
        row["canonical_source_key"]
        for row in rows
        if row["reason"] == "entry-summary-missing"
    }
    degraded_count = sum(bool(row["degraded"]) for row in rows)
    declared_absent = [
        name
        for name in DECLARED_OPTIONAL_BUCKETS
        if not (artifact_root / name).exists()
    ] if selection == "all-d23" else []

    feed = {
        "E": len(degraded_missing_locators),
        "G": len(missing_locators),
        "conflict_count": 0,
        "consumer_required": sorted(CONSUMER_REQUIRED),
        "cycle_boundary_version": BOUNDARY_VERSION,
        "declared_absent_buckets": declared_absent,
        "degraded_count": degraded_count,
        "entry_summary_pattern": PATTERN_TEXT,
        "entry_summary_pattern_set_version": PATTERN_VERSION,
        "integrity_algorithm": "sha256",
        "inventory_after_digest": after_digest,
        "inventory_before_digest": before_digest,
        "mapping_count": len(records),
        "mapping_digest": map_digest,
        "mapping_namespace": MAPPING_NAMESPACE,
        "mapping_version": MAPPING_VERSION,
        "measurement_definition": {
            "case_rule": "case-insensitive",
            "cycle_boundary_version": BOUNDARY_VERSION,
            "denominator_population": len(population),
            "direct_child_scope": "cycle-container-direct-regular-files",
            "entry_summary_pattern_set_version": PATTERN_VERSION,
            "seal_epoch": seal,
        },
        "model_calls": 0,
        "outcome": "partial" if degraded_count else "completed",
        "population_count": len(population),
        "projection_eligible": True,
        "quiescence_outcome": "stable",
        "row_count": len(rows),
        "rows": rows,
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "seal_epoch": seal,
        "selection": {
            "bucket": selection,
            "mode": "production-pilot" if selection == "plans" else "fixture-diagnostic",
        },
        "summary_present_count": len(population) - len(missing_locators),
        "unresolved_mapping_count": 0,
    }
    validate_feed(feed, require_production=False)
    return feed


def validate_feed(feed: dict[str, Any], *, require_production: bool = True) -> None:
    if feed.get("schema") != SCHEMA or feed.get("schema_version") != SCHEMA_VERSION:
        raise FeedError("feed-schema", "unsupported feed schema", EXIT_CAIRN)
    if require_production and feed.get("selection") != {"bucket": "plans", "mode": "production-pilot"}:
        raise FeedError("export-selection", "feed is not production projectable", EXIT_CAIRN)
    rows = feed.get("rows")
    if not isinstance(rows, list):
        raise FeedError("feed-schema", "feed rows are invalid", EXIT_CAIRN)
    population = feed.get("population_count")
    if (
        not isinstance(population, int)
        or feed.get("row_count") != population
        or feed.get("mapping_count") != population
        or feed.get("unresolved_mapping_count") != 0
        or feed.get("conflict_count") != 0
        or feed.get("E") != feed.get("G")
        or feed.get("model_calls") != 0
        or len(rows) != population
        or feed.get("projection_eligible") is not True
    ):
        raise FeedError("feed-incomplete", "feed is not projectable", EXIT_CAIRN)
    if feed.get("consumer_required") != sorted(CONSUMER_REQUIRED):
        raise FeedError("consumer-field", "consumer field contract mismatch", EXIT_CAIRN)
    if any(name in feed for name in CONSUMER_REQUIRED):
        raise FeedError("consumer-field", "consumer-owned field was populated", EXIT_CAIRN)

    locators: set[str] = set()
    identities: set[str] = set()
    missing_count = 0
    observed_missing_count = 0
    degraded_count = 0
    for row in rows:
        if not isinstance(row, dict):
            raise FeedError("row-schema", "feed row is invalid", EXIT_CAIRN)
        locator = _safe_locator(str(row.get("canonical_source_key", "")), directory=True)
        legacy_key_id = row.get("legacy_key_id")
        if row.get("target_id") != migration_id(str(legacy_key_id)):
            raise FeedError("row-identity", "row identity mismatch", EXIT_CAIRN)
        if locator in locators or row["target_id"] in identities:
            raise FeedError("row-identity", "row identity collision", EXIT_CAIRN)
        locators.add(locator)
        identities.add(row["target_id"])
        if row.get("canonical_raw_locator") != locator:
            raise FeedError("row-locator", "canonical raw locator mismatch", EXIT_CAIRN)
        if row.get("model_calls") != 0 or row.get("mapping_version") != MAPPING_VERSION:
            raise FeedError("row-schema", "row producer contract mismatch", EXIT_CAIRN)
        integrity = row.get("integrity")
        freshness = row.get("freshness")
        if (
            not isinstance(integrity, dict)
            or not isinstance(integrity.get("source_digest"), str)
            or not isinstance(freshness, dict)
            or "source_mtime" not in freshness
        ):
            raise FeedError("row-evidence", "row evidence is incomplete", EXIT_CAIRN)
        degraded = row.get("degraded") is True
        if degraded:
            degraded_count += 1
            if (
                row.get("partial") is not True
                or row.get("entry_kind") != "path_only_degraded"
                or not row.get("reason")
                or not isinstance(row.get("missing_fields"), list)
            ):
                raise FeedError("row-degraded", "degraded row is malformed", EXIT_CAIRN)
        elif row.get("partial") or row.get("reason") is not None or row.get("missing_fields") != []:
            raise FeedError("row-degraded", "complete row carries degraded state", EXIT_CAIRN)
        elif row.get("entry_kind") != "navigation" or row.get("disposition") != "complete":
            raise FeedError("row-schema", "complete row shape is invalid", EXIT_CAIRN)
        if row.get("conflict") and degraded:
            raise FeedError("row-conflict", "conflict and degraded sets overlap", EXIT_CAIRN)
        if row.get("reason") == "entry-summary-missing":
            missing_count += 1
        producer_evidence = row.get("producer_evidence")
        if not isinstance(producer_evidence, dict):
            raise FeedError("row-evidence", "producer evidence is incomplete", EXIT_CAIRN)
        if producer_evidence.get("summary_state") == "missing":
            observed_missing_count += 1

    if (
        missing_count != feed.get("E")
        or observed_missing_count != feed.get("G")
        or missing_count != observed_missing_count
        or degraded_count != feed.get("degraded_count")
    ):
        raise FeedError("feed-metrics", "feed metrics do not match rows", EXIT_CAIRN)
    definition = feed.get("measurement_definition")
    if (
        feed.get("inventory_before_digest") != feed.get("inventory_after_digest")
        or not isinstance(definition, dict)
        or definition.get("cycle_boundary_version") != BOUNDARY_VERSION
        or definition.get("entry_summary_pattern_set_version") != PATTERN_VERSION
        or definition.get("denominator_population") != population
        or definition.get("direct_child_scope") != "cycle-container-direct-regular-files"
        or definition.get("case_rule") != "case-insensitive"
        or definition.get("seal_epoch") != feed.get("seal_epoch")
    ):
        raise FeedError("feed-definition", "feed measurement definition is invalid", EXIT_CAIRN)
    expected_outcome = "partial" if degraded_count else "completed"
    if feed.get("outcome") != expected_outcome:
        raise FeedError("feed-outcome", "feed outcome does not match rows", EXIT_CAIRN)


def _export_locator(row: dict[str, Any], field: str, missing_name: str) -> str:
    value = row.get(field)
    if value is None:
        value = str(row["canonical_source_key"]) + missing_name
    return _safe_locator(str(value), directory=False)


def export_cairn(
    feed: dict[str, Any],
    output: Path,
    cairn_root: Path,
    commit: str,
) -> dict[str, Any]:
    validate_feed(feed)
    if commit != PINNED_CAIRN_COMMIT:
        raise FeedError("cairn-commit", "unsupported Cairn commit", EXIT_CAIRN)
    try:
        blob = subprocess.check_output(
            ["git", "-C", str(cairn_root), "show", f"{commit}:{PINNED_CAIRN_MODULE}"],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise FeedError("cairn-mismatch", "pinned Cairn module is unavailable", EXIT_CAIRN) from exc

    sources = []
    for row in feed["rows"]:
        sources.append(
            {
                "expected_reason": row["reason"],
                "manifest_path": _export_locator(row, "manifest_path", "manifest.json"),
                "source_path": _export_locator(row, "source_path", "source"),
                "stable_id": row["target_id"],
                "summary_path": _export_locator(row, "summary_path", "entry-summary-missing.md"),
            }
        )
    result = {
        "cairn_commit": commit,
        "module": PINNED_CAIRN_MODULE,
        "module_digest": digest(blob),
        "path_contract": "artifact-root-relative-posix/v1",
        "read_only": True,
        "reason_translation": {"entry-summary-missing": "summary-generation-failed"},
        "schema": "hearting-cairn-degraded-compat/v1",
        "sources": sources,
    }
    _atomic_write(output, canonical(result) + b"\n")
    return result


def _parser() -> ClosedParser:
    parser = ClosedParser(prog="artifact-knowledge-feed.py")
    commands = parser.add_subparsers(dest="command", required=True)

    mapping = commands.add_parser("mapping-init")
    mapping.add_argument("--artifact-root", required=True)
    mapping.add_argument("--bucket", choices=("plans", "all-d23"), required=True)
    mapping.add_argument("--output", required=True)
    mapping.add_argument("--seal-epoch", required=True)

    scan = commands.add_parser("scan")
    scan.add_argument("--artifact-root", required=True)
    scan.add_argument("--bucket", choices=("plans", "all-d23"), required=True)
    scan.add_argument("--identity-map", required=True)
    scan.add_argument("--output", required=True)
    scan.add_argument("--seal-epoch", required=True)

    export = commands.add_parser("export-cairn-degraded")
    export.add_argument("--artifact-root", required=True)
    export.add_argument("--feed", required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--cairn-root", required=True)
    export.add_argument("--cairn-commit", required=True)
    return parser


def _stable_population(root: Path, selection: str) -> list[str]:
    before, before_digest = _inventory(root, selection)
    population = d23_population(root, selection)
    after, after_digest = _inventory(root, selection)
    if before_digest != after_digest or before != after:
        raise FeedError("source-drift", "source changed during observation", EXIT_DRIFT)
    return population


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "mapping-init":
            root = _root(args.artifact_root)
            output = _resolved_output(args.output, [root])
            _seal_epoch(args.seal_epoch)
            population = _stable_population(root, args.bucket)
            records = [
                _record(locator, "lk_" + secrets.token_bytes(16).hex())
                for locator in population
            ]
            encoded = mapping_bytes(records)
            validate_mapping(encoded, population)
            _atomic_write(output, encoded, exclusive=True)
            result = {
                "command": "mapping-init",
                "mapping_digest": digest(encoded),
                "population_count": len(records),
            }
        elif args.command == "scan":
            root = _root(args.artifact_root)
            identity_map = _input_file(args.identity_map, "mapping-missing")
            _assert_external_input(identity_map, root)
            output = _resolved_output(args.output, [root])
            if output == identity_map.resolve(strict=True):
                raise FeedError("output-containment", "mapping cannot be overwritten", EXIT_WRITE)
            produced = build_feed(root, args.bucket, identity_map.read_bytes(), args.seal_epoch)
            encoded = canonical(produced) + b"\n"
            _atomic_write(output, encoded)
            result = {
                "E": produced["E"],
                "G": produced["G"],
                "command": "scan",
                "degraded_count": produced["degraded_count"],
                "feed_digest": digest(encoded),
                "model_calls": produced["model_calls"],
                "outcome": produced["outcome"],
                "population_count": produced["population_count"],
            }
        else:
            artifact_root = _root(args.artifact_root)
            feed_path = _input_file(args.feed, "feed-missing")
            _assert_external_input(feed_path, artifact_root)
            cairn_root = _root(args.cairn_root)
            output = _resolved_output(args.output, [artifact_root, cairn_root])
            if output == feed_path.resolve(strict=True):
                raise FeedError("output-containment", "feed cannot be overwritten", EXIT_WRITE)
            try:
                feed = json.loads(feed_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FeedError("feed-schema", "feed input is invalid", EXIT_MISSING) from exc
            produced = export_cairn(feed, output, cairn_root, args.cairn_commit)
            result = {
                "command": "export-cairn-degraded",
                "compat_digest": digest(canonical(produced) + b"\n"),
                "module_digest": produced["module_digest"],
                "source_count": len(produced["sources"]),
            }
        print(canonical(result).decode("utf-8"))
        return 0
    except FeedError as exc:
        print(
            canonical({"error": exc.code, "message": str(exc)}).decode("utf-8"),
            file=sys.stderr,
        )
        return exc.exit_code
    except (OSError, ValueError, TypeError):
        print(
            canonical({"error": "input-error", "message": "input could not be processed"}).decode("utf-8"),
            file=sys.stderr,
        )
        return EXIT_MISSING


if __name__ == "__main__":
    raise SystemExit(main())
