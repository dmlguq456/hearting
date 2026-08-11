#!/usr/bin/env python3
"""Publish, verify, and plan dry-run backfills for report bundles."""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "install"))
import report_bundle_config  # noqa: E402

VERIFY_SPEC = importlib.util.spec_from_file_location(
    "report_manifest_verify", ROOT / "tools" / "report-manifest-verify.py"
)
VERIFY = importlib.util.module_from_spec(VERIFY_SPEC)
VERIFY_SPEC.loader.exec_module(VERIFY)


class BundleError(ValueError):
    pass


def component(value, name):
    if not isinstance(value, str) or not VERIFY.COMPONENT_PAT.fullmatch(value):
        raise BundleError("invalid " + name)
    return value


def bundle_id(project, experiment):
    return component(project, "project") + "/" + component(experiment, "experiment")


def resolve_root(raw=None, optional=False):
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise BundleError("report bundle root must be absolute")
        if path.is_symlink():
            raise BundleError("report bundle root may not be a symlink")
        return path.resolve(strict=False)
    try:
        return report_bundle_config.resolve(optional=optional)
    except ValueError as exc:
        raise BundleError(str(exc))


def _manifest_digest(root):
    return hashlib.sha256((Path(root) / "report_manifest.json").read_bytes()).hexdigest()


def _safe_container(root, parts):
    if root.is_symlink() or not root.is_dir():
        raise BundleError("report bundle root is not a directory: " + str(root))
    current = root
    for part in parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if current.is_symlink() or not current.is_dir():
            raise BundleError("unsafe bundle destination component: " + str(current))
    return current


def _rename_noreplace(source, target):
    """Atomically publish a directory only while the destination is absent."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise BundleError("atomic no-replace rename is unavailable")
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise BundleError("bundle version collision: destination appeared during publication")
    raise BundleError("atomic no-replace rename failed: " + os.strerror(error))


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp-" + str(os.getpid()) + "-" + secrets.token_hex(4))
    try:
        temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def publish(source, project, experiment, version, root=None):
    project = component(project, "project")
    experiment = component(experiment, "experiment")
    version = component(version, "version")
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise BundleError("source must be a regular directory")
    source = source.resolve()
    manifest = source / "report_manifest.json"
    try:
        result = VERIFY.verify(manifest)
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise BundleError("source bundle verification failed: " + str(exc))
    if result.get("bundle_classification") != "bundle/v2":
        raise BundleError("publishing requires report manifest schema v2")
    if result["bundle_id"] != bundle_id(project, experiment) or result["version"] != version:
        raise BundleError("source manifest identity does not match explicit publish identity")

    root = resolve_root(root)
    parent = _safe_container(root, (project, experiment, version))
    target = parent / "report"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise BundleError("unsafe existing bundle target")
        try:
            VERIFY.verify(target / "report_manifest.json")
        except Exception as exc:
            raise BundleError("existing bundle is invalid: " + str(exc))
        if _manifest_digest(source) == _manifest_digest(target):
            return {"status": "unchanged", "bundle_id": bundle_id(project, experiment), "version": version, "entrypoint": "report/index.html"}
        raise BundleError("bundle version collision: existing content differs")

    staging = parent / (".report.tmp-" + str(os.getpid()) + "-" + secrets.token_hex(6))
    try:
        shutil.copytree(source, staging, symlinks=False)
        VERIFY.verify(staging / "report_manifest.json")
        _rename_noreplace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"status": "published", "bundle_id": bundle_id(project, experiment), "version": version, "entrypoint": "report/index.html"}


def _integrity_reason(error):
    if isinstance(error, OSError):
        if error.errno in VERIFY.TRANSIENT_ERRNOS:
            return "transient"
        if error.errno == errno.ENOENT:
            return "missing"
    message = str(error).lower()
    for token, reason in (
        ("storage transient", "transient"), ("timed out", "transient"),
        ("missing", "missing"), ("no such file", "missing"), ("hash", "hash"), ("playback", "playback"),
        ("decode", "decode"), ("active html", "active-content"),
        ("link", "link"), ("escape", "root"), ("symlink", "root"), ("unsafe", "root"),
    ):
        if token in message:
            return reason
    return "root"


def integrity_check(root, previous=None, checked_at=None):
    """Read-only full sweep; callers persist only returned transitions."""
    root = resolve_root(root)
    if root.is_symlink() or not root.is_dir():
        raise BundleError("report bundle root unavailable")
    previous = previous or {"schema_version": 1, "bundles": {}}
    if set(previous) != {"schema_version", "bundles"} or previous["schema_version"] != 1 or not isinstance(previous["bundles"], dict):
        raise BundleError("invalid integrity state")
    bundles = {}
    effective_bundles = {}
    transient_identities = set()
    for version_dir in sorted(path for path in root.glob("*/*/*") if path.is_dir() or path.is_symlink()):
        rel = version_dir.relative_to(root)
        if len(rel.parts) != 3:
            continue
        project, experiment, version = rel.parts
        identity = bundle_id(project, experiment) + "/" + component(version, "version")
        manifest = version_dir / "report" / "report_manifest.json"
        try:
            if version_dir.is_symlink():
                raise ValueError("unsafe bundle version symlink")
            result = VERIFY.verify(manifest)
            if result["bundle_id"] + "/" + result["version"] != identity:
                raise ValueError("bundle identity mismatch")
            bundles[identity] = {"status": "healthy", "reason": None}
            effective_bundles[identity] = bundles[identity]
        except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            reason = _integrity_reason(exc)
            if reason == "transient":
                bundles[identity] = {"status": "checking", "reason": "transient"}
                transient_identities.add(identity)
            else:
                bundles[identity] = {"status": "broken", "reason": reason}
                effective_bundles[identity] = bundles[identity]
    previous_bundles = previous["bundles"]
    for identity in transient_identities:
        if identity in previous_bundles:
            effective_bundles[identity] = previous_bundles[identity]
    for identity in set(previous_bundles) - set(effective_bundles) - transient_identities:
        effective_bundles[identity] = {"status": "broken", "reason": "missing"}
        bundles[identity] = effective_bundles[identity]
    transitions = []
    for identity in sorted(set(previous_bundles) | set(effective_bundles)):
        before = previous_bundles.get(identity)
        after = effective_bundles.get(identity)
        if before != after:
            transitions.append({"bundle": identity, "before": before, "after": after})
    return {
        "schema_version": 1,
        "checked_at": checked_at or datetime.now(timezone.utc).isoformat(),
        "bundles": bundles,
        "effective_bundles": effective_bundles,
        "transitions": transitions,
    }


def run_integrity(root, state_path=None, heartbeat_path=None, checked_at=None):
    previous = None
    if state_path and Path(state_path).is_file():
        previous = json.loads(Path(state_path).read_text(encoding="utf-8"))
    result = integrity_check(root, previous=previous, checked_at=checked_at)
    state_writes = 0
    if state_path and result["transitions"]:
        _atomic_json(state_path, {"schema_version": 1, "bundles": result["effective_bundles"]})
        state_writes = len(result["transitions"])
    heartbeat_writes = 0
    if heartbeat_path:
        _atomic_json(heartbeat_path, {"schema_version": 1, "checked_at": result["checked_at"], "bundle_count": len(result["bundles"])})
        heartbeat_writes = 1
    return dict(result, bundle_state_writes=state_writes, heartbeat_writes=heartbeat_writes)


def backfill_plan(source, project, experiment, version):
    identity = bundle_id(project, experiment)
    version = component(version, "version")
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise BundleError("source must be a regular directory")
    source = source.resolve()
    manifests = sorted(source.glob("report_manifest.json"))
    reports = sorted(source.glob("REPORT.md"))
    briefing = source / "00_briefing.md"
    documents = sorted(
        path for path in source.glob("*.md")
        if path.name != "pipeline_summary.md" and path.is_file() and not path.is_symlink()
    )
    html = sorted(path for path in source.glob("*.html") if path.is_file() and not path.is_symlink())
    status = "needs-canonicalization"
    classification = "manifestless"
    errors = []
    if manifests:
        try:
            verified = VERIFY.verify(manifests[0])
            classification = verified["bundle_classification"]
            status = "ready-v2" if classification == "bundle/v2" else "legacy-v1"
        except Exception as exc:
            status = "invalid"; errors.append(str(exc))
    if len(html) > 1 or len(reports) > 1:
        status = "ambiguous"
    if not reports and briefing.is_file() and not briefing.is_symlink():
        status = "needs-canonicalization" if status not in {"invalid", "ambiguous"} else status
    elif not reports:
        status = "incomplete" if status != "invalid" else status
    return {
        "schema_version": 1,
        "operation": "backfill-dry-run",
        "status": status,
        "bundle_id": identity,
        "version": version,
        "destination": identity + "/" + version + "/report",
        "classification": classification,
        "candidates": {
            "manifest": [path.name for path in manifests],
            "markdown": [path.name for path in reports],
            "html": [path.name for path in html],
            "documents": [path.name for path in documents],
        },
        "renames": {"00_briefing.md": "REPORT.md"} if not reports and briefing in documents else {},
        "generated": ["index.html"] if not html else [],
        "excluded": ["pipeline_summary.md"] if (source / "pipeline_summary.md").is_file() else [],
        "errors": errors,
        "mutation": False,
    }


def link_existing_plan(inventory_path):
    """Validate the authoritative local census and emit IDs-only Cairn requests."""
    inventory_path = Path(inventory_path)
    data = json.loads(inventory_path.read_text(encoding="utf-8"))
    if set(data) != {"schema_version", "candidate_count", "bundles"} or data.get("schema_version") != 1:
        raise BundleError("invalid link-only inventory")
    bundles = data.get("bundles")
    if data.get("candidate_count") != 38 or not isinstance(bundles, list) or len(bundles) != 38:
        raise BundleError("authoritative link-only inventory must contain exactly 38 bundles")
    requests = []
    aliases = {}
    seen_identities = set()
    all_note_ids = set()
    total_documents = 0
    for bundle in bundles:
        expected_bundle_keys = {"source_root", "project_root", "project", "experiment_id", "version", "already_generated", "documents"}
        if not isinstance(bundle, dict) or set(bundle) != expected_bundle_keys:
            raise BundleError("invalid link-only bundle record")
        if bundle["already_generated"] is not False:
            raise BundleError("already-generated bundle mapping rejected")
        project = component(bundle["project"], "project")
        experiment = component(bundle["experiment_id"], "experiment")
        version = component(bundle["version"], "version")
        identity = bundle_id(project, experiment)
        identity_version = identity + "/" + version
        if identity_version in seen_identities:
            raise BundleError("duplicate link-only bundle identity")
        seen_identities.add(identity_version)
        project_root = Path(bundle["project_root"])
        if not project_root.is_absolute() or project_root.is_symlink() or not project_root.is_dir() or str(project_root) != str(project_root.resolve()):
            raise BundleError("project_root must be an exact canonical directory")
        project_stat = project_root.stat()
        alias_key = (project_stat.st_dev, project_stat.st_ino)
        previous_project = aliases.setdefault(alias_key, project)
        if previous_project != project:
            raise BundleError("cross-project canonical-root alias collision")
        source_root = Path(bundle["source_root"])
        if not source_root.is_absolute() or source_root.is_symlink() or not source_root.is_dir() or str(source_root) != str(source_root.resolve()):
            raise BundleError("source_root must be an exact canonical directory")
        try:
            source_root.relative_to(project_root)
        except ValueError:
            raise BundleError("source_root must be canonically contained by project_root")
        verified = VERIFY.verify(source_root / "report_manifest.json")
        if verified.get("bundle_id") != identity or verified.get("version") != version:
            raise BundleError("link-only source manifest identity mismatch")
        manifest_data = VERIFY._read_manifest(source_root / "report_manifest.json")
        file_hashes = {row["path"]: row["sha256"] for row in manifest_data["files"]}
        documents = bundle["documents"]
        if not isinstance(documents, list) or not documents:
            raise BundleError("link-only bundle requires documents")
        source_paths = [row.get("source_path") for row in documents if isinstance(row, dict)]
        expected_source_paths = ["REPORT.md"] + sorted(
            path for path in file_hashes if path != "REPORT.md" and Path(path).suffix.lower() == ".md"
        )
        if len(source_paths) != len(set(source_paths)) or source_paths != expected_source_paths:
            raise BundleError("link-only documents must exactly match the complete manifest document set in deterministic order")
        seen_documents = set()
        seen_notes = set()
        mappings = []
        for index, row in enumerate(documents):
            expected_document_keys = {"document_id", "source_path", "source_sha256", "parent_document_id", "note_id", "note_body_sha256", "note_revision"}
            if not isinstance(row, dict) or set(row) != expected_document_keys:
                raise BundleError("invalid link-only document record")
            document_id = component(row["document_id"], "document_id")
            note_id = component(row["note_id"], "note_id")
            source_path = _link_inventory_path(row["source_path"])
            if index == 0 and (document_id != "report" or source_path != "REPORT.md" or row["parent_document_id"] is not None):
                raise BundleError("link-only report document must be first and parentless")
            parent = row["parent_document_id"]
            if index and parent != "report":
                raise BundleError("link-only child hierarchy must use the report representative as parent")
            if document_id in seen_documents or note_id in seen_notes or note_id in all_note_ids:
                raise BundleError("duplicate link-only document or note identity")
            if source_path not in file_hashes or file_hashes[source_path] != row["source_sha256"]:
                raise BundleError("link-only source hash is not manifest-bound")
            if not VERIFY.HASH_PAT.fullmatch(str(row["note_body_sha256"])) or not isinstance(row["note_revision"], (int, str)):
                raise BundleError("invalid existing-note snapshot proof")
            seen_documents.add(document_id); seen_notes.add(note_id); all_note_ids.add(note_id)
            mappings.append({"document_id": document_id, "note_id": note_id})
        total_documents += len(mappings)
        requests.append({
            "schema_version": 2,
            "bundle_id": identity,
            "version": version,
            "entrypoint": "report/index.html",
            "mode": "dry-run",
            "documents": mappings,
        })
    return {
        "schema_version": 1,
        "operation": "existing-note-link-only-census",
        "candidate_count": 38,
        "document_count": total_documents,
        "requests": requests,
        "proof": {
            "l2_notes_insert_rows": 0,
            "l2_notes_update_rows": 0,
            "l2_notes_body_revision_changes": 0,
            "absolute_paths_in_requests": False,
        },
        "mutation": False,
    }


def _link_inventory_path(value):
    path = VERIFY._v2_relpath(value, "source_path")
    return path.as_posix()


def parser():
    top = argparse.ArgumentParser(prog="report-bundle")
    sub = top.add_subparsers(dest="command", required=True)
    root = sub.add_parser("root"); root.add_argument("--optional", action="store_true")
    verify = sub.add_parser("verify"); verify.add_argument("manifest")
    integrity = sub.add_parser("integrity")
    integrity.add_argument("--root"); integrity.add_argument("--state"); integrity.add_argument("--heartbeat")
    publish_parser = sub.add_parser("publish")
    backfill = sub.add_parser("backfill")
    link_existing = sub.add_parser("link-existing")
    for command in (publish_parser, backfill):
        command.add_argument("--source", required=True)
        command.add_argument("--project", required=True)
        command.add_argument("--experiment", required=True)
        command.add_argument("--version", required=True)
    publish_parser.add_argument("--root")
    backfill.add_argument("--dry-run", action="store_true", required=True)
    link_existing.add_argument("--inventory", required=True)
    link_existing.add_argument("--dry-run", action="store_true", required=True)
    return top


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "root":
        root = resolve_root(optional=args.optional)
        if root is not None: print(root)
        return 0
    if args.command == "verify":
        result = VERIFY.verify(args.manifest)
    elif args.command == "integrity":
        result = run_integrity(args.root, args.state, args.heartbeat)
    elif args.command == "publish":
        result = publish(args.source, args.project, args.experiment, args.version, args.root)
    elif args.command == "link-existing":
        result = link_existing_plan(args.inventory)
    else:
        result = backfill_plan(args.source, args.project, args.experiment, args.version)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BundleError, ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print("report-bundle:", exc, file=sys.stderr)
        raise SystemExit(65)
