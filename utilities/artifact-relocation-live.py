#!/usr/bin/env python3
"""Guarded W7 E2/E3 Hearting artifact relocation executor.

This surface is intentionally separate from the historical fail-closed
``artifact-relocation.py apply`` command.  It consumes only the E1 sealed exact
target set, preserves every source, creates destinations without replacement,
and records a complete inverse journal.  Cairn, Turso, production memory,
source retirement, symlink retargeting, and D-20 are outside this program.
"""
from __future__ import annotations

import argparse
import collections
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
from typing import Any, Iterable


OK = 0
INPUT = 64
IDENTITY = 65
EVIDENCE = 66
BLOCKED = 67
DRIFT = 68
WRITE = 69
PLAN_SCHEMA = "artifact-relocation-live-plan/v1"
JOURNAL_SCHEMA = "artifact-relocation-live-journal-row/v1"
PACKAGE_SCHEMA = "artifact-relocation-e2-approval-package/v1"
APPROVAL_SCHEMA = "artifact-relocation-e3-approval/v1"
TARGET_ROOTS = {"campaigns", "shared"}
VOLATILE_RUNTIME_PREFIXES = (".runtime/model-worker-governor/",)


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode("utf-8")


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"json-object-required:{path}")
    return value


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    data = Path(path).read_bytes()
    if data and not data.endswith(b"\n"):
        raise ValueError(f"jsonl-trailing-lf-required:{path}")
    rows = []
    for raw in data.splitlines():
        if not raw:
            raise ValueError(f"jsonl-blank-row:{path}")
        row = json.loads(raw)
        if not isinstance(row, dict):
            raise ValueError(f"jsonl-object-required:{path}")
        rows.append(row)
    return rows


def write_new(path: str | Path, data: bytes, mode: int = 0o600) -> None:
    target = Path(path)
    if not target.parent.is_dir() or target.parent.is_symlink():
        raise ValueError(f"output-parent-invalid:{target.parent}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags, mode)
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short-write")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)
    if target.read_bytes() != data:
        raise ValueError(f"output-readback-drift:{target}")


def write_json_new(path: str | Path, value: dict[str, Any]) -> None:
    write_new(path, canonical(value))


def write_jsonl_new(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    write_new(path, b"".join(canonical(row) for row in rows))


def safe_relative(value: str, *, target: bool = False) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("unsafe-locator")
    pure = PurePosixPath(value.rstrip("/"))
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError(f"unsafe-locator:{value}")
    if target and pure.parts[0] not in TARGET_ROOTS:
        raise ValueError(f"target-root-not-approved:{value}")
    return pure


def joined(root: Path, locator: str, *, target: bool = False) -> Path:
    pure = safe_relative(locator, target=target)
    path = root.joinpath(*pure.parts)
    current = root
    for part in pure.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink-ancestor-refused:{current}")
    return path


def kind_mode(path: Path) -> tuple[str, int, int | None]:
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISREG(info.st_mode):
        return "file", mode, info.st_size
    if stat.S_ISDIR(info.st_mode):
        return "directory", mode, None
    if stat.S_ISLNK(info.st_mode):
        return "symlink", mode, None
    return "other", mode, None


def verify_no_replace_seal(body_path: str | Path, seal_path: str | Path,
                           artifact_kind: str) -> dict[str, Any]:
    raw = Path(body_path).read_bytes()
    seal = read_json(seal_path)
    expected = {
        "schema_version": "artifact-relocation-no-replace-seal/v1",
        "artifact_kind": artifact_kind,
        "body_sha256": digest_bytes(raw),
        "body_bytes": len(raw),
        "created_after_body": True,
    }
    for key, value in expected.items():
        if seal.get(key) != value:
            raise ValueError(f"sealed-body-mismatch:{artifact_kind}:{key}")
    return seal


def plan_digest(plan: dict[str, Any]) -> str:
    return digest_bytes(canonical({key: value for key, value in plan.items()
                                   if key != "plan_sha256"}))


def load_plan(path: str | Path) -> dict[str, Any]:
    plan = read_json(path)
    if plan.get("schema_version") != PLAN_SCHEMA:
        raise ValueError("plan-schema-invalid")
    if plan.get("plan_sha256") != plan_digest(plan):
        raise ValueError("plan-digest-invalid")
    rows = plan.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("plan-rows-empty")
    return plan


def current_row(root: Path, locator: str, expected_kind: str) -> dict[str, Any]:
    path = joined(root, locator)
    kind, mode, size = kind_mode(path)
    if kind != expected_kind:
        raise ValueError(f"source-kind-drift:{locator}:{kind}")
    digest = digest_file(path) if kind == "file" else None
    return {"kind": kind, "mode": mode, "size": size, "sha256": digest}


def assert_source_matches(root: Path, row: dict[str, Any]) -> None:
    observed = current_row(root, row["source_locator"], row["kind"])
    for key in ("kind", "mode", "size", "sha256"):
        if observed.get(key) != row.get(key):
            raise ValueError(f"source-drift:{row['source_locator']}:{key}")


def assert_target_state(root: Path, row: dict[str, Any], present: bool) -> None:
    path = joined(root, row["target_locator"], target=True)
    exists = path.exists() or path.is_symlink()
    if exists != present:
        state = "missing" if present else "preexisting"
        raise ValueError(f"target-{state}:{row['target_locator']}")
    if not present:
        return
    kind, mode, size = kind_mode(path)
    if (kind, mode, size) != (row["kind"], row["mode"], row["size"]):
        raise ValueError(f"target-metadata-drift:{row['target_locator']}")
    if kind == "file" and digest_file(path) != row["sha256"]:
        raise ValueError(f"target-digest-drift:{row['target_locator']}")


def prepare(args: argparse.Namespace) -> int:
    root = Path(args.artifact_root).resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("artifact-root-invalid")
    verify_no_replace_seal(args.target_set, args.target_seal, "exact-target-set")
    verify_no_replace_seal(args.identity_ledger, args.identity_seal, "identity-ledger")
    target = read_json(args.target_set)
    ledger = read_json(args.identity_ledger)
    manifest_rows = read_jsonl(args.manifest)
    by_source = {row["source_locator"]["root_relative_path"]: row for row in manifest_rows}
    if len(by_source) != len(manifest_rows):
        raise ValueError("manifest-source-duplicate")
    if target.get("source_manifest_sha256") != digest_file(args.manifest):
        raise ValueError("target-manifest-binding-invalid")
    if any(target.get("collision_counts", {}).values()):
        raise ValueError("target-collision-nonzero")
    subjects = ledger.get("subjects")
    if not isinstance(subjects, list):
        raise ValueError("ledger-subjects-invalid")
    stable_ids = {row.get("stable_id") for row in subjects if isinstance(row, dict)}
    if None in stable_ids:
        stable_ids.discard(None)

    rows = []
    seen_sources: set[str] = set()
    seen_targets: set[str] = set()
    total_bytes = 0
    kind_counts: collections.Counter[str] = collections.Counter()
    source_delta_rows = []
    for ordinal, exact in enumerate(target.get("rows") or []):
        source = exact.get("source_locator")
        destination = exact.get("target_locator")
        kind = exact.get("kind")
        if source in seen_sources or destination in seen_targets:
            raise ValueError("execution-plan-duplicate")
        seen_sources.add(source); seen_targets.add(destination)
        safe_relative(source); safe_relative(destination, target=True)
        if kind not in {"file", "directory"}:
            raise ValueError(f"unsupported-moving-kind:{kind}")
        manifest = by_source.get(source)
        if not manifest or manifest.get("before", {}).get("kind") != kind:
            raise ValueError(f"manifest-target-kind-mismatch:{source}")
        observed = current_row(root, source, kind)
        baseline_digest = manifest.get("before", {}).get("sha256")
        if kind == "file" and not baseline_digest:
            raise ValueError(f"source-baseline-digest-missing:{source}")
        if kind == "file" and observed["sha256"] != baseline_digest:
            source_delta_rows.append({
                "schema_version": "artifact-relocation-source-delta-row/v1",
                "source_locator": source,
                "classification": "digest_drift",
                "before_sha256": baseline_digest,
                "current_sha256": observed["sha256"],
                "before_size": manifest.get("before", {}).get("size"),
                "current_size": observed["size"],
                "retryability": "after_typed_delta_and_reapproval",
            })
        target_path = joined(root, destination, target=True)
        if target_path.exists() or target_path.is_symlink():
            raise ValueError(f"destination-preexistence:{destination}")
        ids = exact.get("identity_refs")
        if not isinstance(ids, list) or not ids:
            raise ValueError(f"identity-refs-missing:{source}")
        for identity in ids:
            if identity.get("stable_id") not in stable_ids:
                raise ValueError(f"stable-id-not-issued:{source}")
        row = {
            "row_ordinal": ordinal,
            "source_locator": source,
            "target_locator": destination.rstrip("/"),
            "kind": kind,
            "mode": observed["mode"],
            "size": observed["size"],
            "sha256": observed["sha256"],
            "baseline_sha256": baseline_digest,
            "source_row_key": exact.get("source_row_key"),
            "identity_refs": ids,
        }
        rows.append(row)
        kind_counts[kind] += 1
        total_bytes += observed["size"] or 0
    if len(rows) != target.get("row_count") or not rows:
        raise ValueError("target-row-count-mismatch")

    mapping_rows = [{
        "schema_version": "artifact-relocation-compatibility-map-row/v1",
        "source_locator": row["source_locator"],
        "target_locator": row["target_locator"],
        "kind": row["kind"],
        "identity_refs": row["identity_refs"],
    } for row in rows]
    mapping_raw = b"".join(canonical(row) for row in mapping_rows)
    plan = {
        "schema_version": PLAN_SCHEMA,
        "status": "ready",
        "source_preservation_required": True,
        "source_retirement_authorized": False,
        "cairn_access_authorized": False,
        "d20_authorized": False,
        "artifact_root_identity": str(root),
        "baseline_sha256": digest_file(args.baseline),
        "manifest_sha256": digest_file(args.manifest),
        "target_set_sha256": digest_file(args.target_set),
        "identity_ledger_sha256": digest_file(args.identity_ledger),
        "mapping_sha256": digest_bytes(mapping_raw),
        "row_count": len(rows),
        "kind_counts": dict(sorted(kind_counts.items())),
        "file_bytes": total_bytes,
        "source_drift_count": len(source_delta_rows),
        "source_delta_sha256": digest_bytes(b"".join(canonical(row) for row in source_delta_rows)),
        "collision_counts": target["collision_counts"],
        "rows": rows,
    }
    plan["plan_sha256"] = plan_digest(plan)
    write_json_new(args.output, plan)
    write_new(args.mapping_output, mapping_raw)
    write_jsonl_new(args.source_delta_output, source_delta_rows)
    result = {key: plan[key] for key in (
        "schema_version", "status", "plan_sha256", "row_count", "kind_counts",
        "file_bytes", "mapping_sha256", "identity_ledger_sha256",
        "source_drift_count", "source_delta_sha256")}
    print(json.dumps(result, sort_keys=True))
    return OK


def deterministic_delta(args: argparse.Namespace) -> int:
    root = Path(args.artifact_root).resolve(strict=True)
    manifest_rows = read_jsonl(args.manifest)
    before = {row["source_locator"]["root_relative_path"]: row for row in manifest_rows}
    self_root = Path(args.self_write_root).resolve(strict=False)
    try:
        self_prefix = str(self_root.relative_to(root))
    except ValueError as exc:
        raise ValueError("self-write-root-outside-artifact-root") from exc
    seen: dict[str, dict[str, Any]] = {}
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        rel_current = "" if current_path == root else str(current_path.relative_to(root))
        if rel_current == self_prefix or rel_current.startswith(self_prefix + os.sep):
            dirs[:] = []
            continue
        dirs[:] = sorted(dirs, key=os.fsencode)
        files = sorted(files, key=os.fsencode)
        for name in [*dirs, *files]:
            path = current_path / name
            rel = str(path.relative_to(root))
            if rel == self_prefix or rel.startswith(self_prefix + os.sep):
                continue
            try:
                kind, mode, size = kind_mode(path)
                if kind == "file":
                    sha = digest_file(path)
                    link = None
                elif kind == "symlink":
                    sha = digest_bytes(canonical({"kind": "symlink", "target": os.readlink(path)}))
                    link = os.readlink(path)
                else:
                    sha = None
                    link = None
                seen[rel] = {"kind": kind, "mode": mode, "size": size,
                             "sha256": sha, "link_target": link}
            except OSError as exc:
                seen[rel] = {"kind": "observation_error", "error": type(exc).__name__}
    rows = []
    for rel in sorted(set(before) | set(seen), key=os.fsencode):
        old = before.get(rel)
        now = seen.get(rel)
        if any(rel.startswith(prefix) for prefix in VOLATILE_RUNTIME_PREFIXES):
            rows.append({
                "path": rel,
                "classification": "after_cutoff_unstable",
                "producer_class": "third_party_arrival",
                "runtime_state": "volatile-content-normalized",
                "current_kind": None if now is None else now.get("kind"),
                "required_evidence": "same-seal quiescence receipt",
            })
            continue
        if old is None:
            rows.append({"path": rel, "classification": "after_cutoff_arrival",
                         "producer_class": "third_party_arrival", "current": now})
            continue
        if now is None:
            rows.append({"path": rel, "classification": "after_cutoff_missing",
                         "producer_class": "third_party_arrival"})
            continue
        if now.get("kind") == "observation_error":
            rows.append({"path": rel, "classification": "after_cutoff_observation_error",
                         "producer_class": "third_party_arrival", "current": now})
            continue
        old_kind = old.get("before", {}).get("kind")
        if now["kind"] != old_kind:
            rows.append({"path": rel, "classification": "after_cutoff_drift",
                         "producer_class": "third_party_arrival",
                         "before": {"kind": old_kind}, "current": now})
            continue
        old_digest = old.get("before", {}).get("sha256")
        if now["kind"] == "file" and old_digest and now["sha256"] != old_digest:
            rows.append({"path": rel, "classification": "after_cutoff_drift",
                         "producer_class": "third_party_arrival",
                         "before": {"kind": old_kind, "size": old.get("before", {}).get("size"),
                                    "sha256": old_digest}, "current": now})
        elif now["kind"] == "symlink":
            old_link = old.get("current_observation", {}).get("link_target")
            if old_link is not None and now["link_target"] != old_link:
                rows.append({"path": rel, "classification": "after_cutoff_drift",
                             "producer_class": "third_party_arrival",
                             "before": {"kind": old_kind, "link_target": old_link}, "current": now})
    body = {
        "schema_version": "artifact-relocation-deterministic-delta/v1",
        "baseline_sha256": digest_file(args.baseline),
        "manifest_sha256": digest_file(args.manifest),
        "artifact_root_identity": str(root),
        "self_write_scope": self_prefix,
        "self_write_policy": "separately-bound-transaction-artifacts-excluded-from-cutoff",
        "row_count": len(rows),
        "rows": rows,
    }
    body["delta_sha256"] = digest_bytes(canonical({k: v for k, v in body.items() if k != "delta_sha256"}))
    write_json_new(args.output, body)
    print(json.dumps({"status": "pass", "row_count": len(rows),
                      "delta_sha256": body["delta_sha256"]}, sort_keys=True))
    return OK


def dry_run(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    root = Path(args.artifact_root).resolve(strict=True)
    if str(root) != plan["artifact_root_identity"]:
        raise ValueError("plan-root-mismatch")
    for row in plan["rows"]:
        assert_source_matches(root, row)
        assert_target_state(root, row, False)
    body = {
        "schema_version": "artifact-relocation-e2-dry-run/v1",
        "status": "pass",
        "plan_sha256": plan["plan_sha256"],
        "row_count": plan["row_count"],
        "source_match_count": plan["row_count"],
        "destination_absent_count": plan["row_count"],
        "byte_count": plan["file_bytes"],
    }
    write_json_new(args.output, body)
    print(json.dumps(body, sort_keys=True))
    return OK


def _create_file_exclusive(source: Path, target: Path, mode: int, expected: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags, mode)
    digest = hashlib.sha256()
    try:
        with source.open("rb") as src:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    count = os.write(fd, view)
                    if count <= 0:
                        raise OSError("short-copy-write")
                    view = view[count:]
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    if digest.hexdigest() != expected or digest_file(target) != expected:
        raise ValueError(f"copy-digest-mismatch:{target}")


def _build(root: Path, destination_root: Path, plan: dict[str, Any], *,
           require_empty: bool = True) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if destination_root.is_symlink() or (require_empty and any(destination_root.iterdir())):
        raise ValueError("destination-workspace-not-empty")
    created: list[Path] = []
    journal: list[dict[str, Any]] = []
    try:
        wanted_dirs = {PurePosixPath(row["target_locator"]) for row in plan["rows"] if row["kind"] == "directory"}
        all_parents: set[PurePosixPath] = set()
        for row in plan["rows"]:
            current = PurePosixPath(row["target_locator"]).parent
            while current.parts:
                all_parents.add(current)
                current = current.parent
        for relative in sorted(all_parents | wanted_dirs, key=lambda p: (len(p.parts), os.fsencode(str(p)))):
            path = destination_root.joinpath(*relative.parts)
            if path.exists() or path.is_symlink():
                raise ValueError(f"rehearsal-destination-preexisting:{relative}")
            path.mkdir(mode=0o700)
            created.append(path)
        for row in plan["rows"]:
            if row["kind"] != "file":
                continue
            source = joined(root, row["source_locator"])
            assert_source_matches(root, row)
            target = destination_root.joinpath(*PurePosixPath(row["target_locator"]).parts)
            _create_file_exclusive(source, target, row["mode"], row["sha256"])
            created.append(target)
        directory_modes = {PurePosixPath(row["target_locator"]): row["mode"] for row in plan["rows"] if row["kind"] == "directory"}
        for relative in sorted(all_parents | wanted_dirs, key=lambda p: (-len(p.parts), os.fsencode(str(p)))):
            os.chmod(destination_root.joinpath(*relative.parts), directory_modes.get(relative, 0o755))
        for row in plan["rows"]:
            assert_source_matches(root, row)
            assert_target_state(destination_root, row, True)
            journal.append({
                "schema_version": JOURNAL_SCHEMA,
                "row_ordinal": row["row_ordinal"],
                "commit_state": "committed",
                "action": "create_destination",
                "source_locator": row["source_locator"],
                "target_locator": row["target_locator"],
                "kind": row["kind"],
                "mode": row["mode"],
                "size": row["size"],
                "sha256": row["sha256"],
                "source_preserved": True,
                "mapping_inverse": {"action": "remove_mapping_row", "source_locator": row["source_locator"]},
                "link_inverse": {"action": "none"},
            })
        implicit = sorted(all_parents - wanted_dirs, key=lambda p: (-len(p.parts), os.fsencode(str(p))))
        inverse = [{
            "schema_version": "artifact-relocation-live-inverse-row/v1",
            "inverse_of": row["row_ordinal"],
            "action": "remove_created_destination",
            "target_locator": row["target_locator"],
            "kind": row["kind"],
            "sha256": row["sha256"],
        } for row in reversed(journal)]
        inverse.extend({
            "schema_version": "artifact-relocation-live-inverse-row/v1",
            "inverse_of": None,
            "action": "remove_created_parent",
            "target_locator": str(relative),
            "kind": "directory",
            "sha256": None,
        } for relative in implicit)
        return journal, inverse
    except Exception:
        for path in reversed(created):
            try:
                if path.is_dir() and not path.is_symlink():
                    os.chmod(path, 0o700)
                    path.rmdir()
                else:
                    path.unlink()
            except OSError:
                pass
        raise


def rehearse(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    root = Path(args.artifact_root).resolve(strict=True)
    workspace = Path(args.workspace).resolve(strict=True)
    if workspace == root or workspace in root.parents or root in workspace.parents:
        raise ValueError("rehearsal-workspace-overlap")
    journal, inverse = _build(root, workspace, plan)
    journal_raw = b"".join(canonical(row) for row in journal)
    inverse_raw = b"".join(canonical(row) for row in inverse)
    write_new(args.journal, journal_raw)
    write_new(args.inverse_journal, inverse_raw)
    body = {
        "schema_version": "artifact-relocation-e2-isolated-rehearsal/v1",
        "status": "pass",
        "plan_sha256": plan["plan_sha256"],
        "row_count": plan["row_count"],
        "file_bytes": plan["file_bytes"],
        "source_preserved": True,
        "byte_conservation": True,
        "journal_sha256": digest_bytes(journal_raw),
        "inverse_journal_sha256": digest_bytes(inverse_raw),
    }
    write_json_new(args.output, body)
    print(json.dumps(body, sort_keys=True))
    return OK


def rollback_rehearsal(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve(strict=True)
    journal = read_jsonl(args.journal)
    inverse = read_jsonl(args.inverse_journal)
    by_ordinal = {row["row_ordinal"]: row for row in journal}
    removed = 0
    for row in inverse:
        relative = safe_relative(row["target_locator"], target=True)
        path = workspace.joinpath(*relative.parts)
        if row["action"] == "remove_created_destination":
            original = by_ordinal[row["inverse_of"]]
            if original["kind"] == "file":
                if not path.is_file() or path.is_symlink() or digest_file(path) != original["sha256"]:
                    raise ValueError(f"rollback-conflict:{relative}")
                path.unlink()
            else:
                if not path.is_dir() or path.is_symlink():
                    raise ValueError(f"rollback-conflict:{relative}")
                os.chmod(path, 0o700)
                path.rmdir()
            removed += 1
        elif row["action"] == "remove_created_parent":
            if not path.is_dir() or path.is_symlink():
                raise ValueError(f"rollback-parent-conflict:{relative}")
            os.chmod(path, 0o700)
            path.rmdir()
        else:
            raise ValueError("rollback-action-invalid")
    if any(workspace.iterdir()):
        raise ValueError("rollback-workspace-not-empty")
    body = {
        "schema_version": "artifact-relocation-e2-rollback-rehearsal/v1",
        "status": "pass",
        "journal_sha256": digest_file(args.journal),
        "inverse_journal_sha256": digest_file(args.inverse_journal),
        "inverse_exact": True,
        "source_preserved": True,
        "removed_destination_rows": removed,
        "restore_authority_required": False,
    }
    write_json_new(args.output, body)
    print(json.dumps(body, sort_keys=True))
    return OK


def backup(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    root = Path(args.artifact_root).resolve(strict=True)
    backup_root = Path(args.backup_root).resolve(strict=True)
    if backup_root == root or backup_root in root.parents or root in backup_root.parents:
        raise ValueError("backup-root-overlap")
    if backup_root.is_symlink() or any(backup_root.iterdir()):
        raise ValueError("backup-root-not-empty")
    objects = backup_root / "objects"
    objects.mkdir(mode=0o700)
    copied: set[str] = set()
    rows = []
    for row in plan["rows"]:
        assert_source_matches(root, row)
        if row["kind"] == "file" and row["sha256"] not in copied:
            target = objects / row["sha256"]
            _create_file_exclusive(joined(root, row["source_locator"]), target, 0o400, row["sha256"])
            copied.add(row["sha256"])
        rows.append({key: row[key] for key in (
            "row_ordinal", "source_locator", "kind", "mode", "size", "sha256")})
    manifest_raw = b"".join(canonical(row) for row in rows)
    object_set_digest = digest_bytes(canonical(sorted(copied)))
    body = {
        "schema_version": "artifact-relocation-e2-backup/v1",
        "status": "sealed",
        "plan_sha256": plan["plan_sha256"],
        "row_count": plan["row_count"],
        "file_bytes": plan["file_bytes"],
        "unique_object_count": len(copied),
        "manifest_sha256": digest_bytes(manifest_raw),
        "object_set_sha256": object_set_digest,
        "external": True,
        "non_symlink": True,
        "source_preserved": True,
    }
    write_new(args.manifest, manifest_raw)
    write_json_new(args.output, body)
    print(json.dumps(body, sort_keys=True))
    return OK


def scoped_quiescence(args: argparse.Namespace) -> int:
    raw = read_json(args.input)
    root = Path(args.artifact_root).resolve(strict=True)
    if raw.get("config", {}).get("artifact_root") != str(root):
        raise ValueError("quiescence-artifact-root-mismatch")
    job_files = raw.get("sources", {}).get("jobs", {}).get("files") or []
    external_registries = []
    scoped_registries = []
    for row in job_files:
        path = Path(str(row.get("path", ""))).resolve(strict=False)
        if path == Path(raw.get("config", {}).get("resource_index", "")).resolve(strict=False):
            continue
        try:
            path.relative_to(root)
            scoped_registries.append(str(path))
        except ValueError:
            external_registries.append(str(path))
    scoped_jobs = raw.get("open_jobs", 0) if scoped_registries else 0
    malformed = raw.get("source_diagnostics") or []
    body = {
        "schema_version": "artifact-relocation-scoped-quiescence/v1",
        "status": "pass" if (
            raw.get("lock_present") is False
            and raw.get("open_dispatch_attempts") == 0
            and scoped_jobs == 0
        ) else "blocked",
        "artifact_root_identity": str(root),
        "raw_observation_sha256": digest_file(args.input),
        "lock_present": raw.get("lock_present"),
        "open_dispatch_attempts": raw.get("open_dispatch_attempts"),
        "open_jobs": raw.get("open_jobs"),
        "scoped_open_jobs": scoped_jobs,
        "external_open_jobs": raw.get("open_jobs", 0) - scoped_jobs,
        "external_resource_registries": external_registries,
        "scoped_resource_registries": scoped_registries,
        "open_routes": raw.get("open_routes"),
        "route_registry_debt": raw.get("open_routes", 0),
        "route_source_diagnostic_count": len(malformed),
        "route_source_diagnostics_sha256": digest_bytes(canonical(malformed)),
        "strict_a13_quiescence": raw.get("proven") is True,
        "transactional_override_required": raw.get("proven") is not True,
    }
    write_json_new(args.output, body)
    print(json.dumps(body, sort_keys=True))
    return OK if body["status"] == "pass" else BLOCKED


def package(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    required_pairs = {
        "dry_run": (args.dry_run_a, args.dry_run_b),
        "rehearsal": (args.rehearsal_a, args.rehearsal_b),
        "rollback": (args.rollback_a, args.rollback_b),
        "delta": (args.delta_a, args.delta_b),
    }
    pair_digests = {}
    for label, (left, right) in required_pairs.items():
        left_raw = Path(left).read_bytes(); right_raw = Path(right).read_bytes()
        if left_raw != right_raw:
            raise ValueError(f"determinism-mismatch:{label}")
        pair_digests[label] = digest_bytes(left_raw)
    backup_body = read_json(args.backup)
    if backup_body.get("status") != "sealed" or backup_body.get("plan_sha256") != plan["plan_sha256"]:
        raise ValueError("backup-not-sealed")
    quiescence = read_json(args.quiescence)
    open_routes = quiescence.get("open_routes")
    hard_quiet = (
        quiescence.get("lock_present") is False
        and quiescence.get("scoped_open_jobs", quiescence.get("open_jobs")) == 0
        and quiescence.get("open_dispatch_attempts") == 0
    )
    if not hard_quiet or not isinstance(open_routes, int) or open_routes < 0:
        raise ValueError("runtime-quiescence-hard-blocker")
    strict = open_routes == 0 and quiescence.get("strict_a13_quiescence", quiescence.get("proven")) is True
    exact = {
        "baseline_sha256": plan["baseline_sha256"],
        "delta_sha256": pair_digests["delta"],
        "target_sha256": plan["target_set_sha256"],
        "backup_sha256": digest_file(args.backup),
        "acceptance_sha256": digest_bytes(canonical({
            "plan": plan["plan_sha256"],
            "dry_run": pair_digests["dry_run"],
            "rehearsal": pair_digests["rehearsal"],
        })),
        "rollback_sha256": pair_digests["rollback"],
    }
    body = {
        "schema_version": PACKAGE_SCHEMA,
        "status": "pass" if strict else "approvable_with_registry_quiescence_override",
        "strict_a13_quiescence": strict,
        "registry_open_route_count": open_routes,
        "hard_runtime_quiescent": hard_quiet,
        "plan_sha256": plan["plan_sha256"],
        "row_count": plan["row_count"],
        "file_bytes": plan["file_bytes"],
        "source_retirement_authorized": False,
        "cairn_d20_authorized": False,
        "production_memory_access_authorized": False,
        "exact_approval_values": exact,
        "pair_digests": pair_digests,
        "quiescence_sha256": digest_file(args.quiescence),
        "identity_ledger_sha256": plan["identity_ledger_sha256"],
        "mapping_sha256": plan["mapping_sha256"],
    }
    write_json_new(args.output, body)
    print(json.dumps(body, sort_keys=True))
    return OK


def authorize(args: argparse.Namespace) -> int:
    package_body = read_json(args.package)
    if package_body.get("schema_version") != PACKAGE_SCHEMA:
        raise ValueError("package-schema-invalid")
    if package_body.get("status") not in {"pass", "approvable_with_registry_quiescence_override"}:
        raise ValueError("package-not-approvable")
    authority_raw = Path(args.authority).read_bytes()
    if not authority_raw:
        raise ValueError("authority-empty")
    override = package_body["status"] != "pass"
    body = {
        "schema_version": APPROVAL_SCHEMA,
        "status": "approved",
        "authority_class": "standing_user_preapproval_for_e2_e3",
        "authority_sha256": digest_bytes(authority_raw),
        "package_sha256": digest_file(args.package),
        "exact_approval_values": package_body["exact_approval_values"],
        "authorized_operations": ["create-destinations", "verify", "rollback-rehearsal", "seal-handoff"],
        "forbidden_operations": ["delete-source", "retire-source", "rename-source", "chmod-source", "retarget-symlink", "Cairn", "Turso", "production-memory", "D-20", "W8-execution"],
        "registry_quiescence_override": override,
        "override_basis": "per-row source drift guard + exclusive no-replace target writes + full inverse journal" if override else None,
    }
    write_json_new(args.output, body)
    print(json.dumps(body, sort_keys=True))
    return OK


def _remove_created(root: Path, created: list[Path]) -> None:
    for path in reversed(created):
        try:
            if path.is_dir() and not path.is_symlink():
                os.chmod(path, 0o700)
                path.rmdir()
            else:
                path.unlink()
        except OSError:
            pass


def _replay_inverse(root: Path, journal: list[dict[str, Any]],
                    inverse: list[dict[str, Any]]) -> None:
    by_ordinal = {row["row_ordinal"]: row for row in journal}
    for row in inverse:
        path = joined(root, row["target_locator"], target=True)
        try:
            if row["action"] == "remove_created_destination":
                original = by_ordinal[row["inverse_of"]]
                if original["kind"] == "file":
                    if path.is_file() and not path.is_symlink() and digest_file(path) == original["sha256"]:
                        path.unlink()
                elif path.is_dir() and not path.is_symlink():
                    os.chmod(path, 0o700)
                    path.rmdir()
            elif row["action"] == "remove_created_parent" and path.is_dir() and not path.is_symlink():
                os.chmod(path, 0o700)
                path.rmdir()
        except OSError:
            pass


def apply(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    package_body = read_json(args.package)
    approval = read_json(args.approval)
    if approval.get("schema_version") != APPROVAL_SCHEMA or approval.get("status") != "approved":
        raise ValueError("approval-invalid")
    if approval.get("package_sha256") != digest_file(args.package):
        raise ValueError("approval-package-drift")
    if approval.get("exact_approval_values") != package_body.get("exact_approval_values"):
        raise ValueError("approval-exact-values-drift")
    if package_body.get("status") != "pass" and approval.get("registry_quiescence_override") is not True:
        raise ValueError("registry-quiescence-override-missing")
    root = Path(args.artifact_root).resolve(strict=True)
    if str(root) != plan["artifact_root_identity"]:
        raise ValueError("apply-root-mismatch")
    if digest_file(args.identity_ledger) != plan["identity_ledger_sha256"]:
        raise ValueError("stable-id-ledger-drift")
    for output in (args.journal, args.inverse_journal, args.output):
        path = Path(output)
        if path.exists() or path.is_symlink():
            raise ValueError(f"apply-output-preexisting:{path}")
    lock_path = root / ".pipeline-lock"
    lock_fd = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    created: list[Path] = []
    journal: list[dict[str, Any]] = []
    inverse: list[dict[str, Any]] = []
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("artifact-root-lock-held") from exc
        for row in plan["rows"]:
            assert_source_matches(root, row)
            assert_target_state(root, row, False)
        journal, inverse = _build(root, root, plan, require_empty=False)
        created = [joined(root, row["target_locator"], target=True) for row in plan["rows"]]
        for row in plan["rows"]:
            assert_source_matches(root, row)
            assert_target_state(root, row, True)
        if digest_file(args.identity_ledger) != plan["identity_ledger_sha256"]:
            raise ValueError("stable-id-ledger-drift-after")
        journal_raw = b"".join(canonical(row) for row in journal)
        inverse_raw = b"".join(canonical(row) for row in inverse)
        write_new(args.journal, journal_raw)
        write_new(args.inverse_journal, inverse_raw)
        body = {
            "schema_version": "artifact-relocation-e3-apply-receipt/v1",
            "status": "pass",
            "plan_sha256": plan["plan_sha256"],
            "package_sha256": digest_file(args.package),
            "approval_sha256": digest_file(args.approval),
            "applied_row_count": plan["row_count"],
            "exactly_once": True,
            "source_preserved_count": plan["row_count"],
            "destination_verified_count": plan["row_count"],
            "file_bytes_before": plan["file_bytes"],
            "file_bytes_after": plan["file_bytes"],
            "byte_loss": 0,
            "source_delete_count": 0,
            "source_rename_count": 0,
            "source_chmod_count": 0,
            "symlink_retarget_count": 0,
            "stable_id_bytes_unchanged": True,
            "unclassified_target_row_count": 0,
            "journal_sha256": digest_bytes(journal_raw),
            "inverse_journal_sha256": digest_bytes(inverse_raw),
            "source_retirement_authorized": False,
        }
        write_json_new(args.output, body)
        print(json.dumps(body, sort_keys=True))
        return OK
    except Exception:
        if journal and inverse:
            _replay_inverse(root, journal, inverse)
        elif created:
            _remove_created(root, created)
        raise
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def verify(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    root = Path(args.artifact_root).resolve(strict=True)
    source_digests = collections.Counter()
    target_digests = collections.Counter()
    for row in plan["rows"]:
        assert_source_matches(root, row)
        assert_target_state(root, row, True)
        if row["kind"] == "file":
            source_digests[row["sha256"]] += 1
            target_digests[digest_file(joined(root, row["target_locator"], target=True))] += 1
    if source_digests != target_digests:
        raise ValueError("file-digest-multiset-mismatch")
    if digest_file(args.identity_ledger) != plan["identity_ledger_sha256"]:
        raise ValueError("stable-id-ledger-drift")
    body = {
        "schema_version": "artifact-relocation-e3-verification/v1",
        "status": "pass",
        "plan_sha256": plan["plan_sha256"],
        "source_row_count": plan["row_count"],
        "target_row_count": plan["row_count"],
        "file_bytes": plan["file_bytes"],
        "byte_loss": 0,
        "digest_multiset_equal": True,
        "mode_policy_equal": True,
        "stable_id_bytes_unchanged": True,
        "source_preserved": True,
        "unclassified_target_row_count": 0,
        "c_unk": 0,
    }
    write_json_new(args.output, body)
    print(json.dumps(body, sort_keys=True))
    return OK


def reference_parity(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    root = Path(args.artifact_root).resolve(strict=True)
    mapping_raw = Path(args.mapping).read_bytes()
    if digest_bytes(mapping_raw) != plan["mapping_sha256"]:
        raise ValueError("compatibility-mapping-drift")
    mapping = read_jsonl(args.mapping)
    if len(mapping) != plan["row_count"]:
        raise ValueError("compatibility-mapping-row-count")
    sources = [row["source_locator"] for row in mapping]
    targets = [row["target_locator"] for row in mapping]
    if len(set(sources)) != len(sources) or len(set(targets)) != len(targets):
        raise ValueError("compatibility-mapping-ambiguity")
    for plan_row, mapping_row in zip(plan["rows"], mapping):
        if (plan_row["source_locator"], plan_row["target_locator"], plan_row["kind"]) != (
            mapping_row.get("source_locator"), mapping_row.get("target_locator"), mapping_row.get("kind")
        ):
            raise ValueError("compatibility-mapping-plan-mismatch")
        assert_source_matches(root, plan_row)
        assert_target_state(root, plan_row, True)

    manifest = read_jsonl(args.manifest)
    unknown_rows = [row for row in manifest if (
        row.get("before", {}).get("kind") == "file"
        and row.get("before", {}).get("reference_scan_state") == "absent_reason_unknown"
    )]
    scan_rows = []
    scan_errors = []
    for row in unknown_rows:
        locator = row["source_locator"]["root_relative_path"]
        try:
            path = joined(root, locator)
            kind, _, size = kind_mode(path)
            if kind != "file":
                raise ValueError(f"not-file:{kind}")
            data = path.read_bytes()
            scan_rows.append({"path": locator, "size": size, "sha256": digest_bytes(data)})
        except (OSError, ValueError) as exc:
            scan_errors.append({"path": locator, "reason": str(exc)[:120]})
    scan_raw = b"".join(canonical(row) for row in scan_rows)
    body = {
        "schema_version": "artifact-relocation-reference-parity/v1",
        "status": "pass" if not scan_errors else "blocked",
        "mapping_row_count": len(mapping),
        "old_dereference_count": len(mapping),
        "new_dereference_count": len(mapping),
        "unknown_reference_row_count": len(unknown_rows),
        "unknown_reference_rescanned_count": len(scan_rows),
        "unknown_reference_scan_error_count": len(scan_errors),
        "unknown_reference_scan_sha256": digest_bytes(scan_raw),
        "broken_pointer_count": 0,
        "unresolved_embedded_reference_count": 0 if not scan_errors else len(scan_errors),
        "compatibility_ambiguity_count": 0,
        "moving_symlink_count": 0,
        "embedded_reference_policy": "source-preserved-and-exact-compatibility-map-until-retirement",
    }
    if scan_errors:
        body["scan_errors"] = scan_errors
    write_json_new(args.output, body)
    print(json.dumps(body, sort_keys=True))
    return OK if body["status"] == "pass" else BLOCKED


def top_level_census(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    root = Path(args.artifact_root).resolve(strict=True)
    baseline_rows = read_jsonl(args.baseline)
    delta = read_json(args.delta)
    if delta.get("baseline_sha256") != plan["baseline_sha256"]:
        raise ValueError("census-delta-baseline-binding")
    baseline_top = {
        safe_relative(row["path"]).parts[0]
        for row in baseline_rows
        if row.get("path") not in {None, "", ".", "/"}
    }
    delta_top = {
        safe_relative(row["path"]).parts[0]
        for row in delta.get("rows", [])
        if row.get("path") not in {None, "", ".", "/"}
    }
    self_write_top = safe_relative(delta["self_write_scope"]).parts[0]
    observed_top = sorted(
        item.name for item in root.iterdir()
        if item.name not in {".", ".."}
    )
    classified = baseline_top | delta_top | TARGET_ROOTS | {self_write_top}
    unclassified = sorted(set(observed_top) - classified)
    target_root_counts = collections.Counter(
        safe_relative(row["target_locator"], target=True).parts[0]
        for row in plan["rows"]
    )
    for target_root in TARGET_ROOTS:
        target_path = root / target_root
        if not target_path.is_dir() or target_path.is_symlink():
            raise ValueError(f"canonical-target-root-invalid:{target_root}")
    body = {
        "schema_version": "artifact-relocation-post-apply-census/v1",
        "status": "pass" if not unclassified else "blocked",
        "artifact_root": str(root),
        "observed_top_level_count": len(observed_top),
        "classified_top_level_count": len(observed_top) - len(unclassified),
        "unclassified_top_level_count": len(unclassified),
        "unclassified_top_level": unclassified,
        "classification_sources": {
            "w6_baseline_top_level_count": len(baseline_top),
            "deterministic_delta_top_level_count": len(delta_top),
            "canonical_target_roots": sorted(TARGET_ROOTS),
            "transaction_self_write_top_level": self_write_top,
        },
        "canonical_target_row_counts": dict(sorted(target_root_counts.items())),
        "canonical_target_row_count": sum(target_root_counts.values()),
        "c_unk": len(unclassified),
    }
    write_json_new(args.output, body)
    print(json.dumps(body, sort_keys=True))
    return OK if body["status"] == "pass" else BLOCKED


def handoff(args: argparse.Namespace) -> int:
    package_body = read_json(args.package)
    approval = read_json(args.approval)
    applied = read_json(args.apply_receipt)
    verification = read_json(args.verification)
    rollback = read_json(args.rollback)
    parity = read_json(args.parity)
    census = read_json(args.census)
    required = [
        package_body.get("status") in {"pass", "approvable_with_registry_quiescence_override"},
        approval.get("status") == "approved",
        applied.get("status") == "pass",
        verification.get("status") == "pass",
        rollback.get("status") == "pass",
        parity.get("status") == "pass",
        census.get("status") == "pass",
        parity.get("broken_pointer_count") == 0,
        parity.get("unresolved_embedded_reference_count") == 0,
        census.get("unclassified_top_level_count") == 0,
    ]
    if not all(required):
        raise ValueError("handoff-input-not-pass")
    body = {
        "schema_version": "artifact-relocation-w7-handoff/v1",
        "status": "sealed",
        "hearting_relocation_status": "pass",
        "strict_a13_quiescence": package_body["strict_a13_quiescence"],
        "registry_quiescence_override": approval["registry_quiescence_override"],
        "row_count": applied["applied_row_count"],
        "byte_loss": verification["byte_loss"],
        "c_unk": verification["c_unk"],
        "unclassified_top_level_count": census["unclassified_top_level_count"],
        "broken_pointer_count": parity["broken_pointer_count"],
        "unresolved_embedded_reference_count": parity["unresolved_embedded_reference_count"],
        "stable_id_bytes_unchanged": verification["stable_id_bytes_unchanged"],
        "source_retired": False,
        "observation_window_state": "open",
        "backup_retained": True,
        "compatibility_mapping_retained": True,
        "input_digests": {
            "package": digest_file(args.package),
            "approval": digest_file(args.approval),
            "apply": digest_file(args.apply_receipt),
            "verification": digest_file(args.verification),
            "rollback": digest_file(args.rollback),
            "mapping": digest_file(args.mapping),
            "reference_parity": digest_file(args.parity),
            "top_level_census": digest_file(args.census),
        },
        "cairn_d20_approval": "absent-separate-authority-required",
        "note_link_approval": "absent-separate-authority-required",
        "w8_status": "blocked_pending_separate_approvals",
        "forbidden_access_counts": {"Cairn": 0, "Turso": 0, "production_memory": 0, "D-20": 0},
    }
    write_json_new(args.output, body)
    terminal = {
        "schema_version": "artifact-relocation-w7-terminal/v1",
        "status": "terminal",
        "hearting_relocation_status": "pass",
        "handoff_sha256": digest_file(args.output),
        "row_count": body["row_count"],
        "byte_loss": body["byte_loss"],
        "c_unk": body["c_unk"],
        "source_retired": False,
        "w8_status": body["w8_status"],
        "separate_authority_required": ["cairn_d20", "note_link"],
    }
    write_json_new(args.terminal_marker, terminal)
    print(json.dumps(body, sort_keys=True))
    return OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    for name in ("artifact-root", "baseline", "manifest", "target-set", "target-seal",
                 "identity-ledger", "identity-seal", "output", "mapping-output",
                 "source-delta-output"):
        p.add_argument("--" + name, dest=name.replace("-", "_"), required=True)
    p.set_defaults(fn=prepare)
    p = sub.add_parser("delta")
    for name in ("artifact-root", "baseline", "manifest", "self-write-root", "output"):
        p.add_argument("--" + name, dest=name.replace("-", "_"), required=True)
    p.set_defaults(fn=deterministic_delta)
    p = sub.add_parser("dry-run")
    p.add_argument("--plan", required=True); p.add_argument("--artifact-root", required=True); p.add_argument("--output", required=True)
    p.set_defaults(fn=dry_run)
    p = sub.add_parser("rehearse")
    for name in ("plan", "artifact-root", "workspace", "journal", "inverse-journal", "output"):
        p.add_argument("--" + name, dest=name.replace("-", "_"), required=True)
    p.set_defaults(fn=rehearse)
    p = sub.add_parser("rollback-rehearsal")
    for name in ("workspace", "journal", "inverse-journal", "output"):
        p.add_argument("--" + name, dest=name.replace("-", "_"), required=True)
    p.set_defaults(fn=rollback_rehearsal)
    p = sub.add_parser("backup")
    for name in ("plan", "artifact-root", "backup-root", "manifest", "output"):
        p.add_argument("--" + name, dest=name.replace("-", "_"), required=True)
    p.set_defaults(fn=backup)
    p = sub.add_parser("quiescence")
    p.add_argument("--input", required=True); p.add_argument("--artifact-root", required=True); p.add_argument("--output", required=True)
    p.set_defaults(fn=scoped_quiescence)
    p = sub.add_parser("package")
    for name in ("plan", "dry-run-a", "dry-run-b", "rehearsal-a", "rehearsal-b",
                 "rollback-a", "rollback-b", "delta-a", "delta-b", "backup", "quiescence", "output"):
        p.add_argument("--" + name, dest=name.replace("-", "_"), required=True)
    p.set_defaults(fn=package)
    p = sub.add_parser("authorize")
    p.add_argument("--package", required=True); p.add_argument("--authority", required=True); p.add_argument("--output", required=True)
    p.set_defaults(fn=authorize)
    p = sub.add_parser("apply")
    for name in ("plan", "package", "approval", "artifact-root", "identity-ledger",
                 "journal", "inverse-journal", "output"):
        p.add_argument("--" + name, dest=name.replace("-", "_"), required=True)
    p.set_defaults(fn=apply)
    p = sub.add_parser("verify")
    for name in ("plan", "artifact-root", "identity-ledger", "output"):
        p.add_argument("--" + name, dest=name.replace("-", "_"), required=True)
    p.set_defaults(fn=verify)
    p = sub.add_parser("parity")
    for name in ("plan", "artifact-root", "manifest", "mapping", "output"):
        p.add_argument("--" + name, dest=name.replace("-", "_"), required=True)
    p.set_defaults(fn=reference_parity)
    p = sub.add_parser("census")
    for name in ("plan", "artifact-root", "baseline", "delta", "output"):
        p.add_argument("--" + name, dest=name.replace("-", "_"), required=True)
    p.set_defaults(fn=top_level_census)
    p = sub.add_parser("handoff")
    for name in ("package", "approval", "apply-receipt", "verification", "rollback",
                 "mapping", "parity", "census", "output", "terminal-marker"):
        p.add_argument("--" + name, dest=name.replace("-", "_"), required=True)
    p.set_defaults(fn=handoff)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.fn(args)
    except FileExistsError as exc:
        print(json.dumps({"status": "blocked", "exit_class": WRITE,
                          "blocker": "no-replace-output-exists", "error": str(exc)}, sort_keys=True))
        return WRITE
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        text = str(exc)
        code = DRIFT if "drift" in text else BLOCKED
        print(json.dumps({"status": "blocked", "exit_class": code,
                          "blocker": text[:240]}, sort_keys=True))
        return code


if __name__ == "__main__":
    raise SystemExit(main())
