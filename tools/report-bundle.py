#!/usr/bin/env python3
"""Publish, verify, and plan dry-run backfills for report bundles."""

from __future__ import annotations

import argparse
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


def _tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.is_symlink():
            raise BundleError("symlink forbidden: " + str(path.relative_to(root)))
        rel = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(rel).to_bytes(8, "big")); digest.update(rel)
        data = path.read_bytes(); digest.update(len(data).to_bytes(8, "big")); digest.update(data)
    return digest.hexdigest()


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
        if _tree_digest(source) == _tree_digest(target):
            return {"status": "unchanged", "bundle_id": bundle_id(project, experiment), "version": version, "entrypoint": "report/index.html"}
        raise BundleError("bundle version collision: existing content differs")

    staging = parent / (".report.tmp-" + str(os.getpid()) + "-" + secrets.token_hex(6))
    try:
        shutil.copytree(source, staging, symlinks=False)
        VERIFY.verify(staging / "report_manifest.json")
        os.replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"status": "published", "bundle_id": bundle_id(project, experiment), "version": version, "entrypoint": "report/index.html"}


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


def parser():
    top = argparse.ArgumentParser(prog="report-bundle")
    sub = top.add_subparsers(dest="command", required=True)
    root = sub.add_parser("root"); root.add_argument("--optional", action="store_true")
    verify = sub.add_parser("verify"); verify.add_argument("manifest")
    publish_parser = sub.add_parser("publish")
    backfill = sub.add_parser("backfill")
    for command in (publish_parser, backfill):
        command.add_argument("--source", required=True)
        command.add_argument("--project", required=True)
        command.add_argument("--experiment", required=True)
        command.add_argument("--version", required=True)
    publish_parser.add_argument("--root")
    backfill.add_argument("--dry-run", action="store_true", required=True)
    return top


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "root":
        root = resolve_root(optional=args.optional)
        if root is not None: print(root)
        return 0
    if args.command == "verify":
        result = VERIFY.verify(args.manifest)
    elif args.command == "publish":
        result = publish(args.source, args.project, args.experiment, args.version, args.root)
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
