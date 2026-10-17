#!/usr/bin/env python3
"""Artifact Contract v2 route/cycle lifecycle policy (rollout step 2).

The immutable step-1 identity, manifest, index, and admission modules remain the
only lineage authorities.  This module composes them with the runtime route
surface: exact route names, fixed-input cycle decisions, terminal-evidence
joins, publication reporting, and the direct/quick production transaction.

For a durable direct/quick transaction, this trusted lifecycle boundary derives
the terminal marker/outcome digests after closing the independent runtime
outcome, writes those derived values into a private document copy, verifies the
completed join, and only then submits that copy to step-1 admission.  Callers
declare the route/event identities; they do not get to forge runtime digests.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import artifact_admission
import artifact_identity
import artifact_index
import artifact_locator
import artifact_manifest
import artifact_directory_bundle


PUBLICATION_RESULTS = frozenset(
    {"not-offered", "skipped", "succeeded", "failed"}
)
_ROUTE_ID_RE = re.compile(r"^rt-[A-Za-z0-9][A-Za-z0-9._-]{0,126}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CAPABILITY_ROUTE_MODULE = None


def _violation(code: str, path: str = "$", detail: str = "") -> artifact_manifest.Violation:
    return artifact_manifest.Violation(code, path, detail or code)


@dataclass(frozen=True)
class Decision:
    status: str
    reasons: Tuple[artifact_manifest.Violation, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.reasons and self.status not in {"reject", "incomplete"}

    def to_payload(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "reasons": [reason.to_payload() for reason in self.reasons],
            "detail": dict(self.detail),
        }


class LifecycleError(ValueError):
    """Compatibility exception used by the effectful wrappers and CLI."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail or code


@dataclass(frozen=True)
class RouteBinding:
    artifact_root_id: Optional[str]
    route_id: str
    route_hash: str
    route_file: str
    outcome_file: str

    def to_payload(self) -> Dict[str, Any]:
        return {
            "artifact_root_id": self.artifact_root_id,
            "route_id": self.route_id,
            "route_hash": self.route_hash,
            "route_file": self.route_file,
            "outcome_file": self.outcome_file,
        }


@dataclass(frozen=True)
class DirectQuickRequest:
    route: Mapping[str, Any]
    route_file: Path
    document: Optional[Mapping[str, Any]] = None
    staging_source: Optional[Path] = None
    idempotency_key: Optional[str] = None
    publication: str = "not-offered"
    commit: Optional[str] = None
    summary: Optional[str] = None


def _load_capability_route():
    global _CAPABILITY_ROUTE_MODULE
    if _CAPABILITY_ROUTE_MODULE is not None:
        return _CAPABILITY_ROUTE_MODULE
    path = Path(__file__).with_name("capability-route.py")
    spec = importlib.util.spec_from_file_location("artifact_lifecycle_capability_route", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    _CAPABILITY_ROUTE_MODULE = module
    return _CAPABILITY_ROUTE_MODULE


def canonical_route_path(artifact_root: Path, route_id: str) -> Path:
    if not isinstance(route_id, str) or not _ROUTE_ID_RE.fullmatch(route_id):
        raise LifecycleError("invalid-route-id", str(route_id))
    return Path(artifact_root).resolve() / ".runtime" / "routes" / f"{route_id}.json"


def canonical_outcome_path(artifact_root: Path, route_id: str) -> Path:
    return canonical_route_path(artifact_root, route_id).with_name(f"{route_id}.outcome.json")


def canonical_route_paths(artifact_root: Path, route_id: str) -> Tuple[Path, Path]:
    return canonical_route_path(artifact_root, route_id), canonical_outcome_path(
        artifact_root, route_id
    )


def validate_route_target(
    path: Path, artifact_root: Path, route_id: str, *, kind: str = "route"
) -> Decision:
    if kind not in {"route", "outcome"}:
        return Decision("reject", (_violation("route-target-unknown-sidecar"),))
    route_path, outcome_path = canonical_route_paths(artifact_root, route_id)
    actual = Path(path).resolve()
    expected = route_path if kind == "route" else outcome_path
    if actual.parent != route_path.parent:
        return Decision(
            "reject",
            (_violation("route-target-outside-canonical-dir", detail=str(actual)),),
        )
    if actual != expected:
        if kind == "outcome" or actual.name.endswith(".outcome.json"):
            code = "route-target-unknown-sidecar"
        elif actual.name.startswith(f"{route_id}."):
            # right route_id, wrong suffix, e.g. `<route_id>.foo.json` (N3) --
            # distinct from an alias basename naming a different route entirely.
            code = "route-target-unknown-sidecar"
        else:
            code = "route-target-alias-basename"
        return Decision("reject", (_violation(code, detail=str(actual)),))
    return Decision("accept", detail={"path": str(expected)})


def classify_route_record(path: Path, artifact_root: Path, route_id: str) -> str:
    route_module = _load_capability_route()
    location = route_module.classify_route_location(path, artifact_root)
    if location == "canonical":
        return (
            "canonical"
            if Path(path).resolve() == canonical_route_path(artifact_root, route_id)
            else "alias-basename"
        )
    if location in route_module._LEGACY_LOCATIONS:
        return "legacy-location"
    return "outside"


def read_root_identity(artifact_root: Path) -> Optional[artifact_identity.RootIdentity]:
    """Read the frozen root identity without allocating it or creating a folder."""

    path = (
        Path(artifact_root)
        / artifact_admission.ADMISSION_REL
        / "root-identity.json"
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError) as exc:
        raise LifecycleError("root-identity-unreadable", str(exc)) from exc
    try:
        return artifact_identity.RootIdentity.parse(payload)
    except artifact_identity.IdentityError as exc:
        raise LifecycleError("root-identity-invalid", str(exc)) from exc


def scan_runtime_routes(artifact_root: Path) -> Tuple[Mapping[str, Any], ...]:
    """Read route-shaped canonical-directory records; ignore auxiliary JSON."""

    directory = Path(artifact_root).resolve() / ".runtime" / "routes"
    rows = []
    if not directory.is_dir():
        return ()
    identity = read_root_identity(artifact_root)
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".outcome.json"):
            continue
        try:
            route = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        if not isinstance(route, dict) or "route_id" not in route or "nodes" not in route:
            continue
        route_id = route.get("route_id")
        rows.append(
            {
                "artifact_root_id": identity.artifact_root_id if identity else None,
                "route_id": route_id,
                "route_hash": route.get("route_hash"),
                "route_file": str(path.resolve()),
                "classification": classify_route_record(path, artifact_root, route_id),
            }
        )
    return tuple(rows)


def evaluate_route_admission(
    artifact_root: Path,
    route: Mapping[str, Any],
    *,
    index: Optional[Any] = None,
    expected_root_id: Optional[str] = None,
) -> Decision:
    if not isinstance(route, Mapping):
        return Decision("reject", (_violation("route-not-object"),))
    route_id = route.get("route_id")
    route_hash = route.get("route_hash")
    try:
        target = canonical_route_path(artifact_root, route_id)
    except LifecycleError as exc:
        return Decision("reject", (_violation(exc.code, detail=exc.detail),))
    if not isinstance(route_hash, str) or not _DIGEST_RE.fullmatch(route_hash):
        return Decision("reject", (_violation("invalid-route-hash"),))
    root = Path(artifact_root).resolve()
    declared = route.get("artifact_root")
    if not isinstance(declared, str) or Path(declared).resolve() != root:
        return Decision("reject", (_violation("route-artifact-root-mismatch"),))
    identity = read_root_identity(root)
    if expected_root_id is not None and (
        identity is None or identity.artifact_root_id != expected_root_id
    ):
        return Decision("reject", (_violation("artifact-root-id-mismatch"),))
    existing = [row for row in scan_runtime_routes(root) if row["route_id"] == route_id]
    if existing:
        return Decision(
            "reject",
            (_violation("route-composite-duplicate-runtime", detail=str(target)),),
        )
    if index is None and identity is not None:
        index = artifact_admission.load_index(root)
    if identity is not None and index is not None:
        indexed = getattr(index, "routes", {}).get(identity.artifact_root_id, {}).get(route_id)
        if indexed is not None:
            return Decision("reject", (_violation("route-composite-duplicate-index"),))
    notes = [] if identity is not None else ["root-identity-unissued"]
    return Decision(
        "accept",
        detail={
            "artifact_root_id": identity.artifact_root_id if identity else None,
            "route_file": str(target),
            "notes": notes,
        },
    )


def admit_runtime_route(
    artifact_root: Path,
    route: Mapping[str, Any],
    *,
    route_file: Optional[Path] = None,
    expected_root_id: Optional[str] = None,
    index: Optional[Any] = None,
) -> RouteBinding:
    """Exclusively create a route after both runtime and index uniqueness checks."""

    target = route_file or canonical_route_path(artifact_root, route.get("route_id"))
    target_check = validate_route_target(target, artifact_root, route.get("route_id"))
    if not target_check.ok:
        reason = target_check.reasons[0]
        raise LifecycleError(reason.code, reason.detail)
    decision = evaluate_route_admission(
        artifact_root, route, index=index, expected_root_id=expected_root_id
    )
    if not decision.ok:
        reason = decision.reasons[0]
        raise LifecycleError(reason.code, reason.detail)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(route, indent=2, ensure_ascii=False) + "\n"
    try:
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise LifecycleError("route-composite-duplicate-runtime", str(target)) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    root_id = decision.detail.get("artifact_root_id")
    return RouteBinding(
        root_id,
        route["route_id"],
        route["route_hash"],
        str(target.resolve()),
        str(canonical_outcome_path(artifact_root, route["route_id"])),
    )


def bind_existing_runtime_route(
    artifact_root: Path,
    route_file: Path,
    *,
    expected_root_id: Optional[str] = None,
) -> Tuple[RouteBinding, Dict[str, Any]]:
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise LifecycleError("route-unreadable", str(exc)) from exc
    route_id = route.get("route_id") if isinstance(route, dict) else None
    check = validate_route_target(route_file, artifact_root, route_id)
    if not check.ok:
        reason = check.reasons[0]
        raise LifecycleError(reason.code, reason.detail)
    root = Path(artifact_root).resolve()
    if not isinstance(route, dict) or Path(str(route.get("artifact_root", ""))).resolve() != root:
        raise LifecycleError("route-artifact-root-mismatch")
    route_hash = route.get("route_hash")
    if not isinstance(route_hash, str) or not _DIGEST_RE.fullmatch(route_hash):
        raise LifecycleError("invalid-route-hash")
    identity = read_root_identity(root)
    if expected_root_id is not None and (
        identity is None or identity.artifact_root_id != expected_root_id
    ):
        raise LifecycleError("artifact-root-id-mismatch")
    binding = RouteBinding(
        identity.artifact_root_id if identity else None,
        route_id,
        route_hash,
        str(Path(route_file).resolve()),
        str(canonical_outcome_path(root, route_id)),
    )
    return binding, route


def read_admitted_cycle(
    artifact_root: Path, campaign_id: str, cycle_id: str
) -> Optional[Mapping[str, Any]]:
    # The index only ever offers a candidate; the record proves it every call.
    # An index that is stale, missing, or absent is never admission's answer --
    # it changes only whether that answer costs a cache hit or a record scan,
    # so readable locators, renamed sealed locators, and the historical ID
    # layout all still resolve.
    try:
        directory = artifact_locator.find_path_by_id(Path(artifact_root), cycle_id)
    except artifact_locator.LocatorError as exc:
        raise LifecycleError("cycle-prior-descriptor-unverified", str(exc)) from exc
    if directory is None:
        return None
    campaign_dir = directory.parent.parent if directory.parent.name == "cycles" else directory.parent
    if artifact_locator._exact_cycle_manifest(Path(artifact_root), campaign_dir, directory) is None:
        return None
    path = directory / "manifest.json"
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise LifecycleError("cycle-prior-descriptor-unverified", str(exc)) from exc
    report = artifact_manifest.validate(document)
    cycle = document.get("cycle", {}) if isinstance(document, dict) else {}
    if (
        not report.ok
        or cycle.get("campaign_id") != campaign_id
        or cycle.get("cycle_id") != cycle_id
    ):
        raise LifecycleError("cycle-prior-descriptor-unverified")
    return cycle


def _sha256_path(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(artifact_directory_bundle.CHUNK_BYTES)
            if not chunk:
                break
            hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def verify_artifact_revisions(
    document: Mapping[str, Any], content_root: Path
) -> Tuple[str, ...]:
    failures = []
    root = Path(content_root).resolve()
    artifacts_by_id = {row.get("artifact_id"): row for row in document.get("artifacts", [])
                       if isinstance(row, Mapping)}
    bundle_directories = []
    for row in document.get("artifact_revisions", []) or []:
        locator = row.get("locator") if isinstance(row, Mapping) else None
        rel = locator.get("path") if isinstance(locator, Mapping) else None
        if not isinstance(rel, str):
            failures.append("locator-missing")
            continue
        classification = artifact_manifest.classify_artifact_path(
            str(root), None, None, "locator", rel, "regular",
        )
        if not classification.allowed:
            failures.append(f"locator-invalid:{rel}:{classification.reason}")
            continue
        path = root
        components = rel.split("/")
        info = None
        unsafe = False
        for index, component in enumerate(components):
            path = path / component
            try:
                info = os.lstat(path)
            except OSError:
                failures.append(f"artifact-missing:{rel}")
                unsafe = True
                break
            if stat.S_ISLNK(info.st_mode):
                failures.append(f"artifact-symlink-forbidden:{rel}")
                unsafe = True
                break
            if index < len(components) - 1 and not stat.S_ISDIR(info.st_mode):
                failures.append(f"artifact-parent-not-directory:{rel}")
                unsafe = True
                break
        if unsafe:
            continue
        if info is None:
            failures.append(f"artifact-missing:{rel}")
            continue
        if not stat.S_ISREG(info.st_mode):
            failures.append(f"artifact-not-regular:{rel}")
            continue
        try:
            path.relative_to(root)
        except ValueError:
            failures.append(f"locator-outside:{rel}")
            continue
        if info.st_size != row.get("byte_size"):
            failures.append(f"artifact-size-mismatch:{rel}")
        if _sha256_path(path) != row.get("content_digest"):
            failures.append(f"artifact-digest-mismatch:{rel}")
        media_type = row.get("media_type")
        if not isinstance(media_type, str) or "/" not in media_type:
            failures.append(f"artifact-media-type-invalid:{rel}")
        artifact = artifacts_by_id.get(row.get("artifact_id"), {})
        is_bundle = artifact.get("type") == "directory-bundle"
        if is_bundle or media_type == artifact_directory_bundle.MEDIA_TYPE:
            if not is_bundle or media_type != artifact_directory_bundle.MEDIA_TYPE:
                failures.append(f"bundle-type-media-mismatch:{rel}")
            else:
                try:
                    covered = artifact_directory_bundle.verify_coverage(root, rel, row)
                    body = artifact_directory_bundle.parse_descriptor((root / rel).read_bytes())
                    directory_rel = body["directory"]["path"]
                    if artifact.get("title") != directory_rel[len("artifacts/"):]:
                        failures.append(f"bundle-title-directory-mismatch:{rel}")
                    if any(directory_rel == prior or directory_rel.startswith(prior + "/")
                           or prior.startswith(directory_rel + "/") for prior in bundle_directories):
                        failures.append(f"bundle-directory-overlap:{directory_rel}")
                    bundle_directories.append(directory_rel)
                except artifact_directory_bundle.DirectoryBundleError as exc:
                    failures.append(f"{exc.code}:{exc.path or rel}")
    return tuple(sorted(failures))


def manifest_route_row(
    document: Mapping[str, Any], *, route_id: Optional[str] = None, route_hash: Optional[str] = None
) -> Optional[Mapping[str, Any]]:
    """The manifest's `routes[]` row for a route named by ID (or, failing that, by hash).

    A closed cycle that a later route closed again carries one row per closing route
    (§45 D-127); this is the one place that asks whether a route has its row."""
    rows = [row for row in (document.get("routes") or []) if isinstance(row, Mapping)] \
        if isinstance(document, Mapping) else []
    for key, wanted in (("route_id", route_id), ("route_hash", route_hash)):
        if wanted:
            return next((row for row in rows if row.get(key) == wanted), None)
    return None


MANIFEST_SNAPSHOT_REL = Path(".runtime/artifact-producer/v1/manifests")


def manifest_snapshot_dir(artifact_root: Path, cycle_id: str) -> Path:
    if not artifact_identity.is_well_formed(cycle_id, "cycle"):
        raise LifecycleError("cycle-id-invalid", str(cycle_id))
    return Path(artifact_root) / MANIFEST_SNAPSHOT_REL / cycle_id


def manifest_snapshot_path(artifact_root: Path, cycle_id: str, manifest_revision_id: str) -> Path:
    if not artifact_identity.is_well_formed(manifest_revision_id, "manifest_revision"):
        raise LifecycleError("manifest-revision-id-invalid", str(manifest_revision_id))
    return manifest_snapshot_dir(artifact_root, cycle_id) / f"{manifest_revision_id}.json"


def _fsync_path(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def preserve_manifest_snapshot(artifact_root: Path, cycle_id: str, raw: bytes) -> Path:
    """§45 D-124: keep one published manifest document's exact bytes (no replace).

    The file name is the document's own `manifest_revision_id`.  A copy that is
    already there must hold the same bytes; nothing is ever overwritten, and the
    caller's own raw bytes are stored, never a re-encoding of the parsed document.
    """
    try:
        document = json.loads(raw.decode("utf-8"))
        revision_id = document["manifest_revision_id"]
        document_cycle = document["cycle"]["cycle_id"]
    except (ValueError, UnicodeError, KeyError, TypeError) as exc:
        raise LifecycleError("manifest-snapshot-invalid", "unreadable manifest document") from exc
    if document_cycle != cycle_id:
        raise LifecycleError("manifest-snapshot-invalid", "cycle identity")
    path = manifest_snapshot_path(artifact_root, cycle_id, revision_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags, 0o644)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != raw:
            raise LifecycleError("manifest-snapshot-conflict", str(path))
        return path
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    _fsync_path(path.parent)
    return path


def read_manifest_snapshots(artifact_root: Path, cycle_id: str) -> Tuple[Tuple[bytes, Mapping[str, Any]], ...]:
    """Every preserved document of a cycle, as `(raw bytes, parsed document)`.

    An unreadable or foreign file is skipped: a snapshot is evidence that is
    read, never one that a reader repairs."""
    directory = manifest_snapshot_dir(artifact_root, cycle_id)
    try:
        names = sorted(directory.iterdir())
    except OSError:
        return ()
    found = []
    for entry in names:
        if entry.suffix != ".json" or entry.is_symlink() or not entry.is_file():
            continue
        try:
            raw = entry.read_bytes()
            document = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        if (isinstance(document, dict) and document.get("manifest_revision_id") == entry.stem
                and isinstance(document.get("cycle"), dict) and document["cycle"].get("cycle_id") == cycle_id):
            found.append((raw, document))
    return tuple(found)


def find_manifest_snapshot(
    artifact_root: Path, cycle_id: str, *, manifest_revision_id: Optional[str] = None,
    manifest_digest: Optional[str] = None,
) -> Optional[Mapping[str, Any]]:
    """The preserved document with this revision ID or `manifest_digest`, if any."""
    if manifest_revision_id is not None:
        for _raw, document in read_manifest_snapshots(artifact_root, cycle_id):
            if document.get("manifest_revision_id") == manifest_revision_id:
                return document
        return None
    for _raw, document in read_manifest_snapshots(artifact_root, cycle_id):
        if artifact_manifest.manifest_digest(document) == manifest_digest:
            return document
    return None


def verify_published_payload(cycle_dir: Path, document: Mapping[str, Any]) -> Decision:
    failures = verify_artifact_revisions(document, cycle_dir)
    if failures:
        return Decision(
            "reject",
            (_violation("completion-artifact-verification-failed", detail=";".join(failures)),),
        )
    return Decision("verified")


def validate_publication(value: str) -> Decision:
    if value not in PUBLICATION_RESULTS:
        return Decision(
            "reject", (_violation("publication-unknown-result", detail=str(value)),)
        )
    return Decision("accept", detail={"publication": value})


def _marker_digest(route_module: Any, route: Mapping[str, Any], *, recorded: bool = False,
                   recorded_gates: Optional[Mapping[str, Any]] = None) -> str:
    """Digest of the terminal completion markers.

    Live, it reads the route's gate observation, so a marker whose evidence file has
    changed is not "passed".  `recorded=True` is the proof of a cycle that is already
    closed (§45 D-127): the markers' own bytes are what was bound at the close, and the
    evidence file they pointed at is free to be edited, moved or removed afterwards."""
    terminal = sorted(
        node["id"] for node in route.get("nodes", []) if node.get("terminal") is True
    )
    if not terminal:
        raise LifecycleError("completion-terminal-node-missing")
    rows = []
    if recorded:
        live = None
        for node_id in terminal:
            path = route_module.completion_dir(route["route_id"]) / f"{node_id}.json"
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                # Only a verified recorded owner row can stand in for its
                # intentionally absent worker marker. Worker-marker absence and
                # other read errors remain fail-closed.
                node = next((item for item in route.get("nodes", [])
                             if item.get("id") == node_id), {})
                proof = (recorded_gates or {}).get(node_id, {})
                owner_node = getattr(route_module, "owner_executed_terminal", lambda _node: False)(node)
                digest = proof.get("marker_digest") if isinstance(proof, Mapping) else None
                valid_owner_row = (
                    isinstance(exc, FileNotFoundError) and owner_node
                    and isinstance(proof, Mapping) and proof.get("passed") is True
                    and proof.get("current") is True and proof.get("source") == "owner-terminal"
                    and proof.get("route_id") == route.get("route_id")
                    and proof.get("route_hash") == route.get("route_hash")
                    and proof.get("node_id") == node_id
                    and proof.get("completion_gate") == node.get("terminal_gate")
                    and isinstance(proof.get("attempt_id"), str)
                    and isinstance(proof.get("evidence_digest"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", proof["evidence_digest"])
                    and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
                )
                if not valid_owner_row:
                    raise LifecycleError("completion-terminal-marker-unverified", node_id) from exc
            rows.append({"node_id": node_id, "sha256": "sha256:" + digest})
    else:
        gates = route_module.terminal_gate_observation(route)
        for node_id in terminal:
            proof = gates.get(node_id, {})
            digest = proof.get("marker_digest")
            if not proof.get("passed") or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise LifecycleError("completion-terminal-marker-unverified", node_id)
            rows.append({"node_id": node_id, "sha256": "sha256:" + digest})
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def evaluate_cycle_completion(
    document: Mapping[str, Any],
    *,
    content_root: Path,
    route_file: Path,
    publication: str = "not-offered",
    expected_root_id: Optional[str] = None,
    inline_finish_id: Optional[str] = None,
    preserved: Optional[Sequence[Mapping[str, Any]]] = None,
    payload_verified: bool = False,
) -> Decision:
    """`preserved` marks a document the runtime has already refreshed after the
    close (§45 D-124): earlier documents of the cycle resolve its references,
    the first-close rules (required roles, payload bytes) are not asked again,
    and what is proved is the terminal record the close left.  `payload_verified`
    says the caller built `document` from files it had just read in chunks and
    confirmed unchanged, so the payload bytes are not read a second time."""
    publication_check = validate_publication(publication)
    if not publication_check.ok:
        return publication_check
    cycle = document.get("cycle", {}) if isinstance(document, Mapping) else {}
    events = document.get("events", []) if isinstance(document, Mapping) else []
    artifacts = document.get("artifacts", []) if isinstance(document, Mapping) else []
    revisions = document.get("artifact_revisions", []) if isinstance(document, Mapping) else []
    criterion = cycle.get("outcome_criterion", {}) if isinstance(cycle, Mapping) else {}
    roles_by_artifact = {
        row.get("artifact_id"): row.get("role")
        for row in artifacts
        if isinstance(row, Mapping)
    }
    revised_roles = {
        roles_by_artifact.get(row.get("artifact_id"))
        for row in revisions
        if isinstance(row, Mapping)
    }
    missing_roles = sorted(
        role for role in criterion.get("required_artifact_roles", [])
        if role not in revised_roles
    )
    if missing_roles and preserved is None:
        return Decision(
            "reject",
            (_violation("completion-required-artifact-missing", detail=",".join(missing_roles)),),
        )
    cycle_events = [
        event for event in events
        if isinstance(event, Mapping) and event.get("target_id") == cycle.get("cycle_id")
    ]
    if criterion.get("decision_required") and not any(
        event.get("event_type") == "decision.recorded" for event in cycle_events
    ):
        return Decision("reject", (_violation("completion-decision-outcome-missing"),))
    if cycle.get("state") == "completed" and not any(
        event.get("event_type") == "route.terminal.recorded" for event in cycle_events
    ):
        return Decision("reject", (_violation("completion-terminal-evidence-unbound"),))
    report = (artifact_manifest.validate(document) if preserved is None
              else artifact_manifest.validate_update(document, preserved=preserved, published=True))
    if not report.ok:
        details = " ".join(v.detail for v in report.violations)
        return Decision("reject", (_violation("completion-manifest-invalid", detail=details),))
    if document.get("cycle", {}).get("state") != "completed":
        return Decision("incomplete", (_violation("completion-cycle-not-completed"),))
    if preserved is None and not payload_verified:
        payload_check = verify_published_payload(content_root, document)
        if not payload_check.ok:
            return payload_check
    root = Path(route_file).resolve().parents[2]
    rows = document.get("routes", []) if isinstance(document, Mapping) else []
    if (not isinstance(rows, list) or not rows or not all(isinstance(row, Mapping) for row in rows)
            or (len(rows) != 1 and preserved is None)):
        # One row at the first close; a closed cycle another route closed again has one per route.
        return Decision("reject", (_violation("completion-route-composite-mismatch"),))
    selected_route_file = Path(route_file)
    cycle_id = cycle.get("cycle_id") if isinstance(cycle, Mapping) else None
    if isinstance(cycle_id, str) and artifact_identity.is_well_formed(cycle_id, "cycle"):
        record_path = root / ".runtime" / "artifact-producer" / "v1" / "cycles" / f"{cycle_id}.json"
        try:
            record_present = record_path.lstat() is not None
        except FileNotFoundError:
            record_present = False
        except OSError as exc:
            return Decision("reject", (_violation("cycle-prior-descriptor-unverified", detail=str(exc)),))
        if record_present:
            try:
                import artifact_producer
                record = artifact_producer.read_cycle_record(root, cycle_id)
                if record is None:
                    raise artifact_producer.ProducerError("cycle-record-unreadable", cycle_id)
                selected_route_file, _selected_route = artifact_producer.resolve_cycle_manifest_route(
                    root, record, document)
            except Exception as exc:  # noqa: BLE001 -- lineage failures are a closed lifecycle rejection
                code = getattr(exc, "code", "completion-route-lineage-unverified")
                detail = getattr(exc, "detail", str(exc))
                return Decision("reject", (_violation(code, detail=detail),))
    try:
        binding, route = bind_existing_runtime_route(
            root,
            selected_route_file,
            expected_root_id=expected_root_id or document.get("artifact_root_id"),
        )
    except LifecycleError as exc:
        return Decision("reject", (_violation(exc.code, detail=exc.detail),))
    route_rows = [row for row in rows
                  if row.get("artifact_root_id") == binding.artifact_root_id
                  and row.get("route_id") == binding.route_id]
    if len(route_rows) != 1:
        return Decision("reject", (_violation("completion-route-composite-mismatch"),))
    route_row = route_rows[0]
    if route_row.get("route_hash") != binding.route_hash:
        return Decision("reject", (_violation("completion-route-hash-mismatch"),))

    route_module = _load_capability_route()
    outcome_path = Path(binding.outcome_file)
    try:
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        return Decision(
            "reject", (_violation("completion-route-outcome-missing", detail=str(exc)),)
        )
    expected_outcome = {
        "route_id": binding.route_id,
        "route_hash": binding.route_hash,
        "route_file": binding.route_file,
    }
    if any(outcome.get(key) != value for key, value in expected_outcome.items()):
        return Decision(
            "reject", (_violation("completion-route-outcome-identity-mismatch"),)
        )
    import late_resource_result
    jobs_value = os.environ.get("AGENT_DISPATCH_JOBS")
    current_outcome = late_resource_result.current_outcome(
        Path(binding.route_file), route, jobs=Path(jobs_value) if jobs_value else None)
    if current_outcome is not None:
        outcome = current_outcome
    outcome_finish_id = outcome.get("inline_finish_id")
    if outcome_finish_id:
        try:
            import inline_finish
            finish_state = inline_finish.pending_state(root, binding.route_id)
        except Exception as exc:  # noqa: BLE001 -- a missing proof is a closed rejection
            return Decision("reject", (_violation("completion-inline-finish-unverified", detail=str(exc)),))
        if (not finish_state or finish_state.get("inline_finish_id") != outcome_finish_id
                or (finish_state.get("state") != "finished"
                    and not (inline_finish_id == outcome_finish_id
                             and finish_state.get("state") in {"route-closed", "producer-sealed"}))):
            return Decision("reject", (_violation("completion-inline-finish-pending"),))
    if outcome.get("terminal_gate_proven") is not True:
        return Decision("reject", (_violation("completion-terminal-gate-unproven"),))
    gates = outcome.get("terminal_gates")
    if not isinstance(gates, dict) or not gates or not all(
        isinstance(row, dict) and row.get("passed") is True for row in gates.values()
    ):
        return Decision("reject", (_violation("completion-terminal-gate-unproven"),))
    if preserved is None:
        live_gates = route_module.terminal_gate_observation(route)
        if not live_gates or not all(row.get("passed") is True for row in live_gates.values()):
            return Decision(
                "reject", (_violation("completion-terminal-marker-unverified"),)
            )
    try:
        marker_digest = _marker_digest(route_module, route, recorded=preserved is not None,
                                       recorded_gates=gates if preserved is not None else None)
    except LifecycleError as exc:
        return Decision("reject", (_violation(exc.code, detail=exc.detail),))
    outcome_digest = _sha256_path(outcome_path)
    if route_row.get("terminal_marker") != marker_digest:
        return Decision(
            "reject", (_violation("completion-terminal-marker-unverified"),)
        )
    event_id = route_row.get("terminal_evidence_id")
    matching = [event for event in document.get("events", []) if event.get("event_id") == event_id]
    if len(matching) != 1:
        return Decision(
            "reject", (_violation("completion-terminal-evidence-unbound"),)
        )
    terminal_event = matching[0]
    if (
        terminal_event.get("event_type") != "route.terminal.recorded"
        or terminal_event.get("target_id") != document["cycle"]["cycle_id"]
    ):
        return Decision(
            "reject", (_violation("completion-terminal-evidence-wrong-event-type"),)
        )
    expected_payload = {
        "artifact_root_id": binding.artifact_root_id,
        "route_id": binding.route_id,
        "route_hash": binding.route_hash,
        "outcome_digest": outcome_digest,
        "terminal_marker_digest": marker_digest,
    }
    if terminal_event.get("payload") != expected_payload:
        return Decision(
            "reject", (_violation("completion-terminal-evidence-unbound"),)
        )
    return Decision(
        "complete",
        detail={
            "primary_result": "completed",
            "publication": publication,
            "route": binding.to_payload(),
            "outcome_digest": outcome_digest,
            "terminal_marker_digest": marker_digest,
        },
    )


def _derive_terminal_evidence(
    document: Mapping[str, Any], binding: RouteBinding, route: Mapping[str, Any]
) -> Dict[str, Any]:
    """Derive trusted runtime digests into a private manifest copy.

    The producer still declares the exact route composite and referenced
    route.terminal.recorded event.  This function refuses a missing/ambiguous
    identity and owns only values that do not exist until route closure.
    """
    result = json.loads(json.dumps(document))
    route_module = _load_capability_route()
    import late_resource_result
    jobs_value = os.environ.get("AGENT_DISPATCH_JOBS")
    current = late_resource_result.current_outcome(
        Path(binding.route_file), route, jobs=Path(jobs_value) if jobs_value else None)
    if not current or current.get("terminal_gate_proven") is not True:
        raise LifecycleError("completion-terminal-gate-unproven")
    marker_digest = _marker_digest(route_module, route)
    outcome_digest = _sha256_path(Path(binding.outcome_file))
    rows = [
        row
        for row in result.get("routes", [])
        if row.get("artifact_root_id") == binding.artifact_root_id
        and row.get("route_id") == binding.route_id
        and row.get("route_hash") == binding.route_hash
    ]
    if len(rows) != 1:
        raise LifecycleError("completion-route-composite-mismatch")
    rows[0]["terminal_marker"] = marker_digest
    event_id = rows[0].get("terminal_evidence_id")
    events = [event for event in result.get("events", []) if event.get("event_id") == event_id]
    if len(events) != 1:
        raise LifecycleError("completion-terminal-evidence-unbound")
    events[0]["payload"] = {
        "artifact_root_id": binding.artifact_root_id,
        "route_id": binding.route_id,
        "route_hash": binding.route_hash,
        "outcome_digest": outcome_digest,
        "terminal_marker_digest": marker_digest,
    }
    return result


# One-window compatibility for tests/callers written during this rollout.
_fill_terminal_evidence = _derive_terminal_evidence


def finalize_direct_quick(artifact_root: Path, request: DirectQuickRequest) -> Decision:
    route_module = _load_capability_route()
    route = request.route
    if route.get("effective_intensity") not in {"direct", "quick"}:
        return Decision("reject", (_violation("d9-intensity-not-direct-or-quick"),))
    publication_check = validate_publication(request.publication)
    if not publication_check.ok:
        return publication_check
    has_document = request.document is not None
    has_staging = request.staging_source is not None
    if has_document != has_staging:
        code = (
            "d9-document-without-durable-output"
            if has_document
            else "d9-durable-output-without-document"
        )
        return Decision("reject", (_violation(code),))
    durable = has_document and has_staging
    if not durable and request.idempotency_key is not None:
        return Decision("reject", (_violation("d9-partial-lineage-request"),))
    if durable and not request.idempotency_key:
        return Decision("reject", (_violation("d9-partial-lineage-request"),))
    if durable and (
        not request.document.get("artifacts")
        or not request.document.get("artifact_revisions")
    ):
        return Decision("reject", (_violation("d9-empty-output-manifest"),))
    try:
        route_module.verify_route(dict(route), route.get("cwd"), allow_stale_registry=True)
    except ValueError as exc:
        return Decision("reject", (_violation("d9-route-invalid", detail=str(exc)),))
    try:
        binding, sealed_route = bind_existing_runtime_route(
            artifact_root,
            request.route_file,
            expected_root_id=(request.document or {}).get("artifact_root_id"),
        )
    except LifecycleError as exc:
        return Decision("reject", (_violation(exc.code, detail=exc.detail),))
    if sealed_route.get("route_hash") != route.get("route_hash"):
        return Decision("reject", (_violation("d9-route-file-mismatch"),))
    if not durable:
        outcome, _ = route_module.close_route(
            sealed_route,
            request.route_file,
            commit=request.commit,
            summary=request.summary,
            publication=request.publication,
        )
        return Decision(
            "route-only",
            detail={
                "route": binding.to_payload(),
                "route_outcome": outcome,
                "lineage_committed": False,
            },
        )

    report = artifact_manifest.validate(request.document)
    if not report.ok:
        return Decision("reject", report.violations)
    payload_check = verify_published_payload(request.staging_source, request.document)
    if not payload_check.ok:
        return payload_check
    gates = route_module.terminal_gate_observation(sealed_route)
    if not gates or not all(row.get("passed") is True for row in gates.values()):
        return Decision("reject", (_violation("completion-terminal-gate-unproven"),))
    # Close first because the immutable admitted manifest must bind the exact
    # outcome bytes (including closed_at).  The outcome is independent runtime
    # provenance: a later admission rejection may leave this sidecar, but never
    # a partial campaign/cycle/manifest/artifact/index/folder lineage.
    _route_outcome, freshly_closed = route_module.close_route(
        sealed_route,
        request.route_file,
        commit=request.commit,
        summary=request.summary,
        publication=request.publication,
    )
    try:
        document = _derive_terminal_evidence(request.document, binding, sealed_route)
    except LifecycleError as exc:
        return Decision("reject", (_violation(exc.code, detail=exc.detail),))
    if not freshly_closed:
        # A durable retry may reuse an immutable sidecar only when the exact
        # lineage it binds is already committed under this idempotency key.
        # Otherwise the sidecar can only belong to a prior failed attempt, and
        # admitting a new manifest would permanently bind that stale outcome.
        try:
            index = artifact_admission.load_index(artifact_root)
            exact_noop = artifact_index.idempotent_match(
                index,
                document,
                idempotency_key=request.idempotency_key,
                manifest_digest=artifact_manifest.manifest_digest(document),
            )
        except (OSError, ValueError) as exc:
            return Decision(
                "reject",
                (
                    _violation(
                        "d9-route-outcome-provenance-conflict",
                        detail=f"unable to prove an exact admitted retry: {exc}",
                    ),
                ),
            )
        if not exact_noop:
            return Decision(
                "reject",
                (
                    _violation(
                        "d9-route-outcome-provenance-conflict",
                        detail=str(binding.outcome_file),
                    ),
                ),
            )
    completion = evaluate_cycle_completion(
        document,
        content_root=request.staging_source,
        route_file=request.route_file,
        publication=request.publication,
        expected_root_id=binding.artifact_root_id,
    )
    if not completion.ok:
        return completion
    outcome = artifact_admission.admit(
        artifact_root,
        artifact_admission.AdmissionRequest(
            idempotency_key=request.idempotency_key,
            document=document,
            staging_source=request.staging_source,
        ),
    )
    if outcome.status not in {"admitted", "noop-idempotent"}:
        return Decision("reject", outcome.violations, {"admission": outcome.to_payload()})
    return Decision(
        "full-lineage",
        detail={
            "route": binding.to_payload(),
            "completion": completion.to_payload(),
            "admission": outcome.to_payload(),
            "lineage_committed": True,
        },
    )


def run_direct_quick(request: Mapping[str, Any]) -> Dict[str, Any]:
    """Compatibility JSON-request wrapper for the production transaction."""

    allowed = {
        "mode", "artifact_root", "artifact_root_id", "route_file", "durable_output",
        "document", "staging_source", "idempotency_key", "summary", "publication", "commit",
    }
    if not isinstance(request, Mapping) or set(request) - allowed:
        raise LifecycleError("direct-quick-request-unknown-key")
    if request.get("mode") not in {"direct", "quick"}:
        raise LifecycleError("invalid-direct-quick-mode")
    route_file = Path(str(request.get("route_file", "")))
    root = Path(str(request.get("artifact_root", "")))
    try:
        route = json.loads(route_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise LifecycleError("route-unreadable", str(exc)) from exc
    if route.get("effective_intensity") != request.get("mode"):
        raise LifecycleError("d9-intensity-not-direct-or-quick")
    durable_flag = request.get("durable_output")
    if not isinstance(durable_flag, bool):
        raise LifecycleError("durable-output-flag-required")
    if not durable_flag and any(
        request.get(key) is not None for key in ("document", "staging_source", "idempotency_key")
    ):
        raise LifecycleError("d9-document-without-durable-output")
    if durable_flag and (request.get("document") is None or request.get("staging_source") is None):
        raise LifecycleError("d9-durable-output-without-document")
    decision = finalize_direct_quick(
        root,
        DirectQuickRequest(
            route=route,
            route_file=route_file,
            document=request.get("document"),
            staging_source=Path(request["staging_source"]) if request.get("staging_source") else None,
            idempotency_key=request.get("idempotency_key"),
            publication=str(request.get("publication", "not-offered")),
            commit=request.get("commit"),
            summary=request.get("summary"),
        ),
    )
    if not decision.ok:
        reason = decision.reasons[0] if decision.reasons else _violation(decision.status)
        raise LifecycleError(reason.code, reason.detail)
    return decision.to_payload()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    direct = sub.add_parser("run-direct-quick")
    direct.add_argument("--request", type=Path)
    direct.add_argument("--route", type=Path)
    direct.add_argument("--artifact-root", type=Path)
    direct.add_argument("--document", type=Path)
    direct.add_argument("--staging-source", type=Path)
    direct.add_argument("--idempotency-key")
    direct.add_argument("--publication", default="not-offered")
    direct.add_argument("--commit")
    direct.add_argument("--summary")
    args = parser.parse_args(argv)
    try:
        if args.request:
            payload = json.loads(args.request.read_text(encoding="utf-8"))
            result = run_direct_quick(payload)
        else:
            if args.route is None:
                raise LifecycleError("d9-route-required")
            route = json.loads(args.route.read_text(encoding="utf-8"))
            document = (
                json.loads(args.document.read_text(encoding="utf-8"))
                if args.document
                else None
            )
            root = args.artifact_root or Path(route["artifact_root"])
            decision = finalize_direct_quick(
                root,
                DirectQuickRequest(
                    route=route,
                    route_file=args.route,
                    document=document,
                    staging_source=args.staging_source,
                    idempotency_key=args.idempotency_key,
                    publication=args.publication,
                    commit=args.commit,
                    summary=args.summary,
                ),
            )
            if not decision.ok:
                reason = decision.reasons[0] if decision.reasons else _violation(decision.status)
                raise LifecycleError(reason.code, reason.detail)
            result = decision.to_payload()
    except (LifecycleError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, LifecycleError) else "request-invalid"
        detail = exc.detail if isinstance(exc, LifecycleError) else str(exc)
        print(json.dumps({"status": "rejected", "reason": code, "detail": detail}, sort_keys=True))
        return 64
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
