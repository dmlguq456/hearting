"""Artifact producer: spec publication checks and the shared-revision journal.

Component sets, the sealed source inventory, spec publication provenance and
base checks, and validating/committing a shared-revision journal.

Split out of `artifact_producer.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping as _MappingABC
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import artifact_identity
import artifact_lifecycle
import artifact_manifest
import artifact_directory_bundle

from artifact_producer_store import (
    _canonical, _digest, _ensure_dir, _json_bytes, _read_json, _record_cycle_manifest_path,
    _read_pass_entry, _reference_path, _stream_file_facts, _walk_files, _write_atomic,
    CONTRACT, cycle_dir, ProducerError, shared_revision_read_scope, read_cycle_record, REVISION_RECORD_NAME, shared_journal_path, SHARED_KINDS,
)


# --- D-87: component-set preservation -------------------------------------
#
# A shared reference may carry several top-level components (`stage-dispatch/`,
# `agent-fleet-dashboard/`, ...).  `admit_shared` copies only the source tree,
# so a partial admit silently drops every component the source omits and
# `latest_revision_id` then points at the reduced set.  On 2026-09-03 two
# admits one minute apart took `ref_4d540b57...` from 246 files / 3 components
# to 41 files / 1 component.  The contract (a) is a typed refusal, (b) an
# explicit `--drop-component`, and (d) a read-only adjacent-pair check.
# Carry-forward is deliberately NOT implemented: (c) rejects it, because a
# revision must only contain what its admit actually carried.


def component_set(paths: Iterable[str]) -> Set[str]:
    """The component set of a revision or tree: the first path segment of every
    relative path.  A top-level file is its own component -- `rrev_15cf1d9f`
    admitted `prd.md` and friends flat, so defining components as "top-level
    directories" would read that revision as empty.  `revision.json` is
    revision metadata, not content."""
    components: Set[str] = set()
    for path in paths:
        rel = str(path).strip().lstrip("./")
        if not rel or rel == REVISION_RECORD_NAME:
            continue
        components.add(rel.split("/", 1)[0])
    return components


SPEC_BASE_RECEIPT = "_internal/shared-base.json"


def _check_shared_base(expected: Optional[str], actual: Optional[str]) -> None:
    if expected != actual:
        raise ProducerError("shared-base-mismatch",
                            f"base={expected or 'none'} latest={actual or 'none'}; "
                            "merge against latest in a new cycle before admission")


def _check_requested_base(requested: Optional[str], receipt_base: Optional[str]) -> None:
    """--base-revision against the source's own base receipt (not the reference's latest)."""
    if requested != receipt_base:
        raise ProducerError("shared-base-mismatch",
                            f"--base-revision={requested or 'none'} but the source's {SPEC_BASE_RECEIPT} "
                            f"names base={receipt_base or 'none'}; reseed the source from the intended base")


def _sealed_source_files(directory: Path, record: Mapping[str, Any], source_rel: str,
                         source_path: Path) -> List[Tuple[str, str, int]]:
    """Bind both the receipt and payload to the sealed manifest, not mutable disk."""
    document = _read_json(directory / "manifest.json")
    if document is None or artifact_manifest.manifest_digest(document) != record.get("manifest_digest"):
        raise ProducerError("source-manifest-mismatch", source_rel)
    expected = {}
    bundle_coverage: Dict[str, Tuple[str, int]] = {}
    bundle_artifacts = {row.get("artifact_id") for row in document.get("artifacts", [])
                        if isinstance(row, Mapping) and row.get("type") == "directory-bundle"}
    for row in document.get("artifact_revisions", []):
        path = row.get("locator", {}).get("path", "")
        if path == source_rel or path.startswith(source_rel + "/"):
            rel = path[len(source_rel) + 1:] if path != source_rel else source_path.name
            expected[rel] = (row["content_digest"], row["byte_size"])
        if row.get("artifact_id") not in bundle_artifacts or row.get("media_type") != artifact_directory_bundle.MEDIA_TYPE:
            continue
        artifact = next(item for item in document.get("artifacts", [])
                        if isinstance(item, Mapping) and item.get("artifact_id") == row.get("artifact_id"))
        title = artifact.get("title")
        if not isinstance(title, str) or not title:
            continue
        tree_rel = "artifacts/" + title
        intersects = (tree_rel == source_rel or tree_rel.startswith(source_rel + "/")
                      or source_rel.startswith(tree_rel + "/"))
        if not intersects:
            continue
        if source_rel.startswith(tree_rel + "/"):
            raise ProducerError("shared-bundle-subtree-unsupported", source_rel)
        descriptor_rel = row.get("locator", {}).get("path", "")
        try:
            descriptor_bytes = (directory / descriptor_rel).read_bytes()
            if (len(descriptor_bytes) != row.get("byte_size")
                    or "sha256:" + hashlib.sha256(descriptor_bytes).hexdigest() != row.get("content_digest")):
                raise ProducerError("bundle-descriptor-revision-mismatch", descriptor_rel)
            body = artifact_directory_bundle.parse_descriptor(descriptor_bytes)
        except (OSError, artifact_directory_bundle.DirectoryBundleError) as exc:
            raise ProducerError("source-bundle-descriptor-invalid", descriptor_rel) from exc
        if body["directory"]["path"] != tree_rel:
            raise ProducerError("bundle-title-directory-mismatch", descriptor_rel)
        try:
            tree = artifact_directory_bundle.scan_tree(directory, tree_rel)
        except artifact_directory_bundle.DirectoryBundleError as exc:
            raise ProducerError(exc.code, exc.path or descriptor_rel) from exc
        if tree.body != body:
            raise ProducerError("bundle-tree-mismatch", tree_rel)
        for leaf in tree.leaves:
            full = f"{tree_rel}/{leaf.path}"
            rel = full[len(source_rel) + 1:] if full != source_rel else source_path.name
            bundle_coverage[rel] = (leaf.digest, leaf.size)
    expected.update(bundle_coverage)
    paths = sorted(source_path.rglob("*")) if source_path.is_dir() else [source_path]
    actual = {}
    for path in paths:
        if path.is_symlink():
            raise ProducerError("source-invalid", str(path))
        if path.is_file():
            rel = path.relative_to(source_path).as_posix() if source_path.is_dir() else path.name
            try:
                digest_value, size = _stream_file_facts(path)
            except (OSError, ProducerError) as exc:
                raise ProducerError("source-invalid", str(path)) from exc
            actual[rel] = (digest_value, size)
    if actual != expected:
        raise ProducerError("source-manifest-mismatch", source_rel)
    return [(rel, digest, size) for rel, (digest, size) in sorted(actual.items())]


def _cycle_document_for_digest(root: Path, cycle_id: str, record: Optional[Mapping[str, Any]],
                               digest: Optional[str]) -> Optional[Mapping[str, Any]]:
    """The cycle's manifest document carrying `digest`: the current one, else a preserved copy."""
    if not digest:
        return None
    if record is not None:
        try:
            current = _read_json(_record_cycle_manifest_path(root, record))
        except ProducerError:
            current = None
        if current is not None and artifact_manifest.manifest_digest(current) == digest:
            return current
    try:
        return artifact_lifecycle.find_manifest_snapshot(root, cycle_id, manifest_digest=digest)
    except artifact_lifecycle.LifecycleError:
        return None


def _published_source_rows(document: Mapping[str, Any], source_rel: str) -> List[Tuple[str, str, int]]:
    """The files a manifest document declares under `source_rel`, as `_sealed_source_files` lists them."""
    rows = []
    for row in document.get("artifact_revisions", []) or []:
        path = row.get("locator", {}).get("path", "")
        if path == source_rel or path.startswith(source_rel + "/"):
            rel = path[len(source_rel) + 1:] if path != source_rel else Path(source_rel).name
            rows.append((rel, row["content_digest"], row["byte_size"]))
    return sorted(rows)


def _published_source_matches(root: Path, record: Optional[Mapping[str, Any]],
                              document: Mapping[str, Any], source_rel: str,
                              expected_rows: Sequence[Mapping[str, Any]]) -> bool:
    expected = [(row.get("path"), row.get("sha256"), row.get("byte_size"))
                for row in expected_rows if isinstance(row, Mapping)]
    actual = _published_source_rows(document, source_rel)
    artifacts = {row.get("artifact_id"): row for row in document.get("artifacts", [])
                 if isinstance(row, Mapping)}
    bundles = []
    for revision in document.get("artifact_revisions", []) or []:
        if (not isinstance(revision, Mapping)
                or revision.get("media_type") != artifact_directory_bundle.MEDIA_TYPE
                or artifacts.get(revision.get("artifact_id"), {}).get("type") != "directory-bundle"):
            continue
        title = artifacts[revision["artifact_id"]].get("title")
        if isinstance(title, str) and title:
            bundles.append(("artifacts/" + title, revision))
    intersecting = [(tree_rel, revision) for tree_rel, revision in bundles
                    if (tree_rel == source_rel or tree_rel.startswith(source_rel + "/")
                        or source_rel.startswith(tree_rel + "/"))]
    if not intersecting:
        return actual == sorted(expected)
    directory = None
    if record is not None:
        try:
            directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
        except (ProducerError, KeyError):
            directory = None
    # A deleted/missing source is judged from the immutable shared publication's
    # own inventory, without reopening raw source bytes.
    if directory is None or not directory.is_dir():
        return True
    coverage_prefixes = []
    for tree_rel, revision in intersecting:
        if source_rel.startswith(tree_rel + "/"):
            return False  # a shared source cut through a bundle cannot preserve closure
        locator = revision.get("locator")
        descriptor_rel = locator.get("path") if isinstance(locator, Mapping) else None
        if not isinstance(descriptor_rel, str):
            return True
        try:
            data = (directory / descriptor_rel).read_bytes()
            if (len(data) != revision.get("byte_size")
                    or "sha256:" + hashlib.sha256(data).hexdigest() != revision.get("content_digest")):
                return True
            body = artifact_directory_bundle.parse_descriptor(data)
        except (OSError, artifact_directory_bundle.DirectoryBundleError):
            return True
        if body["directory"]["path"] != tree_rel:
            return False
        prefix = "" if source_rel == tree_rel else tree_rel[len(source_rel) + 1:]
        covered = [(path, digest, size) for path, digest, size in expected
                   if isinstance(path, str) and (path.startswith(prefix + "/") if prefix else True)]
        leaves = [[path[len(prefix) + 1:] if prefix else path, digest, size]
                  for path, digest, size in covered]
        leaves.sort(key=lambda row: row[0].encode("utf-8"))
        tree_digest = "sha256:" + hashlib.sha256(artifact_directory_bundle._canonical(leaves)).hexdigest()
        if (len(leaves) != body["file_count"] or sum(row[2] for row in leaves) != body["total_bytes"]
                or tree_digest != body["tree_digest"]):
            return False
        coverage_prefixes.append(prefix)
    def covered_path(path: Any) -> bool:
        if not isinstance(path, str):
            return False
        for prefix in coverage_prefixes:
            if path.startswith(prefix + "/") if prefix else True:
                return True
        return False
    expected_direct = sorted(row for row in expected if not covered_path(row[0]))
    actual_direct = sorted(row for row in actual if not covered_path(row[0]))
    return actual_direct == expected_direct


def _check_published_provenance(root: Path, revision: Mapping[str, Any], expected_rows: Sequence[Mapping[str, Any]],
                                *, code: str) -> None:
    """A finished publication is judged by what it recorded (§45 D-127): the source
    manifest document it was published from, found as the current one or a
    preserved copy, must still declare the files the publication took.  A source
    that was edited, moved or deleted since changes nothing; a cycle that left no
    copy and is gone is taken on the publication's own record."""
    source = revision.get("source", {})
    cycle_id = source.get("cycle_id", "")
    record = read_cycle_record(root, cycle_id) if artifact_identity.is_well_formed(cycle_id, "cycle") else None
    document = _cycle_document_for_digest(root, cycle_id, record, source.get("manifest_digest"))
    if document is None:
        return
    rel = source.get("path", "")
    if not rel.startswith("artifacts/") or ".." in rel.split("/"):
        raise ProducerError(code, "source path")
    if not _published_source_matches(root, record, document, rel, expected_rows):
        raise ProducerError(code, "source identity")


def _verify_completed_spec_publication(root: Path, reference: Mapping[str, Any], revision: Mapping[str, Any]) -> None:
    """A finished spec publication: its immutable shared bytes and its own recorded proof.

    The merge is not recomputed (the source it read may have changed since).
    What was recorded must agree with itself and with the immutable revisions it
    names: the output holds the digest the proof claims, the proof's source
    inventory hashes to its own digest, and the base and latest revisions the
    merge read still carry the digests the proof recorded."""
    proof = revision.get("spec_merge")
    if not isinstance(proof, dict):
        raise ProducerError("shared-merge-proof-invalid", "missing merge proof")
    ref_id = reference["shared_reference_id"]
    output, _record = _verified_shared_spec_record(
        root, ref_id, revision["shared_reference_revision_id"])
    try:
        files = proof["source_files"]
        valid = (proof["source_content_digest"] == _spec_inventory_digest(files)
                 and proof["output_content_digest"] == _spec_inventory_digest(output))
        for revision_key, digest_key in (("base_revision_id", "base_content_digest"),
                                         ("latest_revision_id", "latest_content_digest")):
            _inventory, shared = _verified_shared_spec_record(root, ref_id, proof[revision_key])
            valid = valid and shared["content_digest"] == proof[digest_key]
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ProducerError("shared-merge-proof-invalid", revision["shared_reference_revision_id"])
    _check_published_provenance(root, revision, files, code="shared-merge-proof-invalid")


def _spec_admission_base(root: Path, source_path: Path, reference_id: str,
                         base_revision: Optional[str]) -> Optional[str]:
    receipt_path = source_path / SPEC_BASE_RECEIPT
    receipt = _read_json(receipt_path) if source_path.is_dir() else None
    if receipt is None and os.path.lexists(receipt_path):
        raise ProducerError("shared-base-invalid", SPEC_BASE_RECEIPT)
    if receipt is None:
        if base_revision is None:
            raise ProducerError("shared-base-required", "spec admission needs a seeded receipt or --base-revision")
        base = None if base_revision == "none" else base_revision
    else:
        if receipt.get("schema_version") != 1 or not all(k in receipt for k in ("reference_id", "revision_id", "content_digest")):
            raise ProducerError("shared-base-invalid", SPEC_BASE_RECEIPT)
        base = receipt["revision_id"]
        if base is not None and not artifact_identity.is_well_formed(base, "shared_reference_revision"):
            raise ProducerError("shared-base-invalid", str(base))
        if base is not None:
            if receipt["reference_id"] != reference_id:
                raise ProducerError("shared-base-reference-mismatch", reference_id)
            revision = _read_json(root / "shared/spec" / reference_id / "revisions" / str(base) / REVISION_RECORD_NAME)
            if revision is None or revision.get("content_digest") != receipt["content_digest"]:
                raise ProducerError("shared-base-invalid", str(base))
        elif receipt["reference_id"] is not None or receipt["content_digest"] is not None:
            raise ProducerError("shared-base-invalid", SPEC_BASE_RECEIPT)
        if base_revision is not None:
            _check_requested_base(None if base_revision == "none" else base_revision, base)
    if base is not None and not artifact_identity.is_well_formed(base, "shared_reference_revision"):
        raise ProducerError("shared-base-invalid", str(base))
    return base


def _spec_bytes(directory: Path, *, revision: bool = False) -> Dict[str, bytes]:
    """Read a complete regular-file tree, rejecting links and special files."""
    if directory.is_symlink() or not directory.is_dir():
        raise ProducerError("shared-base-invalid", str(directory))
    result = {}
    for path in _walk_files(directory):
        rel = path.relative_to(directory).as_posix()
        if path.is_symlink() or not path.is_file():
            raise ProducerError("shared-base-invalid", rel)
        if revision and rel == REVISION_RECORD_NAME:
            continue
        result[rel] = path.read_bytes()
    return result


def _spec_inventory(tree: Mapping[str, bytes]) -> List[Dict[str, Any]]:
    return [{"path": p, "sha256": _digest(data), "byte_size": len(data)}
            for p, data in sorted(tree.items())]


def _spec_inventory_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    return _digest(_canonical([[r["path"], r["sha256"], r["byte_size"]] for r in rows]))


def _verified_shared_spec(root: Path, ref_id: str, revision_id: str):
    """Validate each immutable input against its own inventory, including bytes."""
    from artifact_spec_storage import StorageError, read_revision
    try:
        return read_revision(root, ref_id, revision_id)
    except StorageError as error:
        raise ProducerError(error.code, str(error)) from error


# A shared revision is immutable once committed, yet every completed-publication
# check re-read and re-hashed whole revision trees (575 files / 66 MB each on the
# live store) for the output, base and latest revisions of every later
# publication, on every runner pass.  A revision whose bytes were verified once
# is recognised again by the stat identity of every entry of its tree (inode,
# size, mtime and ctime: any rewrite, rename or added entry changes it), so the
# bytes are not read again.  Only that identity and the record digest are kept.
_VERIFIED_REVISIONS: "OrderedDict[str, Tuple[str, str]]" = OrderedDict()
_VERIFIED_REVISIONS_MAX = 1024
_VERIFIED_REVISIONS_LOCK = threading.Lock()
# Within one read pass (`shared_revision_read_scope`) a revision already proven
# unchanged is not walked again; the next pass observes it afresh.


def _stored_sources(record: Any) -> Dict[str, Tuple[str, str]]:
    """Logical path -> (provider revision, source path) for files stored elsewhere.

    Storage schema 2 keeps an unchanged file in the earlier revision that first
    held its bytes (``stored_in``); schema 1 named the provider only.
    """
    stored = record.get("stored_in") if isinstance(record, dict) else None
    result = {}
    for name, value in (stored or {}).items() if isinstance(stored, dict) else ():
        if isinstance(value, dict):
            provider, source = value.get("revision_id"), value.get("source_path")
        else:
            provider, source = value, name
        if isinstance(provider, str) and isinstance(source, str):
            result[name] = (provider, source)
    return result


def _revision_tree_signature(directory: Path) -> str:
    """Stat identity of every entry beneath one revision directory.

    Files the revision stores in an earlier revision are part of its identity
    too, so a rewrite of a provider's bytes is never recognised as unchanged.
    """
    prefix = len(str(directory)) + 1
    rows = []
    for path in _walk_files(directory):
        info = os.lstat(path)
        rows.append((str(path)[prefix:], info.st_mode, info.st_dev, info.st_ino,
                     info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    record = _read_json(directory / REVISION_RECORD_NAME)
    for name, (provider, source) in sorted(_stored_sources(record).items()):
        info = os.lstat(directory.parent / provider / source)
        rows.append(("stored:" + name, provider, source, info.st_mode, info.st_dev,
                     info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def _record_inventory(record: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The verified record's inventory: equal to `_spec_inventory` of its tree."""
    return sorted(record["files"], key=lambda row: row["path"])


class _VerifiedRevisionTree(_MappingABC):
    """A verified immutable revision whose file bytes are read only when used.

    Each file read is checked against the record's digest and size, so a reader
    never receives bytes the revision's inventory does not name.
    """

    def __init__(self, directory: Path, revision_id: str, rows: Sequence[Mapping[str, Any]],
                 record: Any = None):
        self._directory = directory
        self._revision_id = revision_id
        self._rows = {row["path"]: row for row in rows}
        self._sources = _stored_sources(record)
        self._bytes: Dict[str, bytes] = {}

    def __getitem__(self, path: str) -> bytes:
        row = self._rows[path]
        data = self._bytes.get(path)
        if data is None:
            provider, source = self._sources.get(path, (None, path))
            target = (self._directory.parent / provider / source if provider
                      else self._directory / path)
            try:
                if target.is_symlink() or not target.is_file():
                    raise ProducerError("shared-revision-integrity", self._revision_id)
                data = target.read_bytes()
            except OSError as exc:
                raise ProducerError("shared-revision-integrity", self._revision_id) from exc
            if len(data) != row["byte_size"] or _digest(data) != row["sha256"]:
                raise ProducerError("shared-revision-integrity", self._revision_id)
            self._bytes[path] = data
        return data

    def __iter__(self):
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


def _verified_shared_revision(root: Path, ref_id: str, revision_id: str):
    """``(tree or None, inventory, record)`` with `_verified_shared_spec`'s checks.

    ``tree`` is ``None`` when the revision was recognised as already verified.
    """
    if not artifact_identity.is_well_formed(revision_id, "shared_reference_revision"):
        raise ProducerError("shared-base-invalid", str(revision_id))
    directory = root / "shared/spec" / ref_id / "revisions" / revision_id
    for parent in (directory, directory.parent, directory.parent.parent):
        if parent.is_symlink():
            raise ProducerError("shared-base-invalid", str(parent))
    key = str(directory)
    scope = _read_pass_entry("verified-revisions", set)

    def recognised(held):
        record = _read_json(directory / REVISION_RECORD_NAME)
        if (isinstance(record, dict) and record.get("shared_reference_id") == ref_id
                and record.get("shared_reference_revision_id") == revision_id
                and _digest(_canonical(record)) == held[1]):
            with _VERIFIED_REVISIONS_LOCK:
                if key in _VERIFIED_REVISIONS:
                    _VERIFIED_REVISIONS.move_to_end(key)
            if scope is not None:
                scope.add(key)
            return record
        return None

    with _VERIFIED_REVISIONS_LOCK:
        held = _VERIFIED_REVISIONS.get(key)
    if held is not None and scope is not None and key in scope:
        record = recognised(held)
        if record is not None:
            return None, _record_inventory(record), record
    try:
        signature = _revision_tree_signature(directory)
    except OSError:
        signature = None
    if signature is not None and held is not None and held[0] == signature:
        record = recognised(held)
        if record is not None:
            return None, _record_inventory(record), record
    tree, record = _verified_shared_spec(root, ref_id, revision_id)
    inventory = _spec_inventory(tree)
    try:
        settled = signature is not None and _revision_tree_signature(directory) == signature
    except OSError:
        settled = False
    with _VERIFIED_REVISIONS_LOCK:
        if settled:
            _VERIFIED_REVISIONS[key] = (signature, _digest(_canonical(record)))
            _VERIFIED_REVISIONS.move_to_end(key)
            if scope is not None:
                scope.add(key)
            while len(_VERIFIED_REVISIONS) > _VERIFIED_REVISIONS_MAX:
                _VERIFIED_REVISIONS.popitem(last=False)
        else:
            _VERIFIED_REVISIONS.pop(key, None)
    return tree, inventory, record


def _verified_shared_spec_record(root: Path, ref_id: str, revision_id: str):
    """`_verified_shared_spec` for callers that need only the inventory and record."""
    _tree, inventory, record = _verified_shared_revision(root, ref_id, revision_id)
    return inventory, record


def _verified_shared_spec_lazy(root: Path, ref_id: str, revision_id: str):
    """`_verified_shared_spec` whose tree reads only the files a caller looks at."""
    tree, inventory, record = _verified_shared_revision(root, ref_id, revision_id)
    if tree is None:
        directory = root / "shared/spec" / ref_id / "revisions" / revision_id
        tree = _VerifiedRevisionTree(directory, revision_id, inventory, record)
    return tree, record


def _spec_seed_components(source_tree: Mapping[str, bytes]) -> Optional[Tuple[str, ...]]:
    raw = source_tree.get(SPEC_BASE_RECEIPT)
    if raw is None:
        return None
    try:
        receipt = json.loads(raw)
        components = receipt.get("components")
        if components is None:
            return None  # existing whole-tree receipts retain their meaning
        valid = (isinstance(components, list) and bool(components)
                 and components == sorted(set(components))
                 and all(isinstance(c, str) and re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", c) for c in components)
                 and receipt.get("seed_complete") is True
                 and receipt.get("component_seeds") == {c: True for c in components})
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ProducerError("shared-base-invalid", "component seed")
    outside = [p for p in source_tree if p != SPEC_BASE_RECEIPT and p.split("/", 1)[0] not in components]
    if outside:
        raise ProducerError("source-manifest-mismatch", "outside seeded components: " + ",".join(sorted(outside)))
    return tuple(components)



def _merge_spec_publication(root: Path, reference: Mapping[str, Any], base_id: str,
                            latest_id: str, source_tree: Mapping[str, bytes], drop_decisions: Sequence[Mapping[str, str]] = ()):
    import spec_merge
    ids = reference.get("revisions", [])
    components = _spec_seed_components(source_tree)
    if (not base_id or not latest_id or base_id not in ids or latest_id not in ids
            or len(ids) != len(set(ids)) or ids.index(base_id) > ids.index(latest_id)
            or (base_id == latest_id and components is None)):
        raise ProducerError("shared-base-mismatch", f"unproven ancestry: {base_id} -> {latest_id}")
    ref_id = reference["shared_reference_id"]
    base, base_record = _verified_shared_spec(root, ref_id, base_id)
    latest, latest_record = _verified_shared_spec(root, ref_id, latest_id)
    candidate = dict(source_tree)
    if components is not None:
        # O is a scoped delta: absence outside its scope is no decision to delete.
        candidate = {**{p: b for p, b in base.items()
                        if p.split("/", 1)[0] not in components}, **candidate}
    dropped = {row["name"] for row in drop_decisions}
    missing = component_set(base) - component_set(candidate) - dropped
    if missing:
        raise ProducerError("component-set-regressed", ",".join(sorted(missing)))
    if dropped - component_set(latest):
        raise ProducerError("drop-component-unknown", ",".join(sorted(dropped - component_set(latest))))
    # A component deletion also conflicts with additions to that component.
    # File-wise merge alone could leave only the newly added files alive.
    for component in component_set(base):
        def subtree(tree):
            return {p: b for p, b in tree.items() if p.split("/", 1)[0] == component}
        b, o, l = subtree(base), subtree(candidate), subtree(latest)
        if (not o and l and l != b) or (not l and o and o != b):
            raise ProducerError("shared-spec-conflict", f"{component}: component-delete-modify")
    try:
        merged, evidence = spec_merge.merge_trees(base, candidate, latest)
    except spec_merge.MergeConflict as exc:
        raise ProducerError("shared-spec-conflict", str(exc)) from exc
    source_files = _spec_inventory(source_tree)
    proof = {"schema_version": 1, "base_revision_id": base_id,
             "base_content_digest": base_record["content_digest"],
             "latest_revision_id": latest_id,
             "latest_content_digest": latest_record["content_digest"],
             "source_files": source_files, "source_content_digest": _spec_inventory_digest(source_files),
             "dropped_components": list(drop_decisions),
             "evidence": evidence, "output_content_digest": _spec_inventory_digest(_spec_inventory(merged))}
    return merged, proof


def _verify_spec_publication(root: Path, reference: Mapping[str, Any], revision: Mapping[str, Any],
                             *, completed: bool = False):
    """Recompute a derived publication for recovery and exact retry; never rebase it.

    `completed` is a publication already in the reference's revision list: it is
    judged by its record and its immutable bytes (§45 D-127), not by the source
    as it is today.  A publication still waiting to be committed keeps the strict
    check that its source is the input it took."""
    if completed:
        return _verify_completed_spec_publication(root, reference, revision)
    proof = revision.get("spec_merge")
    if not isinstance(proof, dict):
        raise ProducerError("shared-merge-proof-invalid", "missing merge proof")
    source = revision.get("source", {})
    record = read_cycle_record(root, source.get("cycle_id", ""))
    if record is None or record.get("state") != "sealed" or record.get("manifest_digest") != source.get("manifest_digest"):
        raise ProducerError("shared-merge-proof-invalid", "source identity")
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    rel = source.get("path", "")
    if not rel.startswith("artifacts/") or ".." in rel.split("/"):
        raise ProducerError("shared-merge-proof-invalid", "source path")
    source_rows = _sealed_source_files(directory, record, rel, directory / rel)
    source_tree = _spec_bytes(directory / rel)
    if source_rows != [(r["path"], r["sha256"], r["byte_size"]) for r in _spec_inventory(source_tree)]:
        raise ProducerError("source-manifest-mismatch", rel)
    recorded_base = _spec_admission_base(root, directory / rel, reference["shared_reference_id"],
                                         proof.get("base_revision_id"))
    merged, expected = _merge_spec_publication(root, reference, recorded_base,
                                              proof.get("latest_revision_id"), source_tree, revision.get("dropped_components", []))
    output, _ = _verified_shared_spec(root, reference["shared_reference_id"],
                                      revision["shared_reference_revision_id"])
    if proof != expected or output != merged:
        raise ProducerError("shared-merge-proof-invalid", revision["shared_reference_revision_id"])


def _verify_exact_spec_publication(root: Path, reference: Mapping[str, Any], revision: Mapping[str, Any],
                                   *, completed: bool = False):
    """Absence of a merge proof must mean an exact source copy, not lost proof.

    `completed` judges a finished publication by its own record: the shared bytes
    are the files the source manifest it names declared (§45 D-127)."""
    ids = reference.get("revisions", [])
    revision_id = revision["shared_reference_revision_id"]
    if revision_id in ids:
        index = ids.index(revision_id)
        base_id = ids[index - 1] if index else None
    else:
        base_id = reference.get("latest_revision_id")
    if "spec_base_revision_id" in revision and revision["spec_base_revision_id"] != base_id:
        raise ProducerError("shared-journal-mismatch", "publication ancestry")
    if base_id and not _legacy_adopted_spec_base(root, reference, base_id):
        _verified_shared_spec_record(root, reference["shared_reference_id"], base_id)
    if completed:
        output, _record = _verified_shared_spec_record(root, reference["shared_reference_id"], revision_id)
        _check_published_provenance(root, revision, output, code="shared-merge-proof-invalid")
        return
    source = revision.get("source", {})
    record = read_cycle_record(root, source.get("cycle_id", ""))
    if record is None or record.get("manifest_digest") != source.get("manifest_digest"):
        raise ProducerError("shared-journal-mismatch", "source identity")
    rel = source.get("path", "")
    if not rel.startswith("artifacts/") or ".." in rel.split("/"):
        raise ProducerError("shared-journal-mismatch", "source path")
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    if os.path.lexists(directory / rel / SPEC_BASE_RECEIPT):
        source_base = _spec_admission_base(root, directory / rel, reference["shared_reference_id"], None)
        _check_shared_base(source_base, base_id)
    rows = _sealed_source_files(directory, record, rel, directory / rel)
    output, _ = _verified_shared_spec_record(root, reference["shared_reference_id"],
                                            revision["shared_reference_revision_id"])
    if rows != [(r["path"], r["sha256"], r["byte_size"]) for r in output]:
        raise ProducerError("shared-merge-proof-invalid", "nonexact publication lacks merge proof")


def _legacy_adopted_spec_base(root: Path, reference: Mapping[str, Any], base_id: str) -> bool:
    # Missing/malformed canonical metadata is corruption, not legacy evidence.
    # Old adoptions without an exact captured roster remain unproven.
    return (reference.get("adopted_from") == "w7-e2-e3-relocation"
            and base_id in reference.get("adopted_revision_ids", [])
            and base_id in reference.get("revisions", [])
            and not os.path.lexists(root / "shared/spec" / reference["shared_reference_id"]
                                   / "revisions" / base_id / REVISION_RECORD_NAME))


def _validate_shared_journal(root: Path, entry: Path, journal) -> None:
    """A malformed recovery record grants no deletion or publication authority."""
    if not isinstance(journal, dict):
        raise ProducerError("shared-journal-mismatch", "unreadable journal")
    kind, ref, rev = (journal.get(k) for k in ("kind", "reference_id", "revision_id"))
    if (not isinstance(kind, str) or kind not in SHARED_KINDS or not artifact_identity.is_well_formed(ref, "shared_reference")
            or not artifact_identity.is_well_formed(rev, "shared_reference_revision") or entry.stem != rev
            or not isinstance(journal.get("state"), str) or journal.get("state") not in {"staging", "published"}
            or "expected_previous_revision_id" not in journal):
        raise ProducerError("shared-journal-mismatch", entry.name)
    prefix = Path("shared") / kind / ref / "revisions"
    staging, target = journal.get("staging"), journal.get("target")
    if (not isinstance(staging, str) or Path(staging).parent != prefix
            or not re.fullmatch(r"\.admitting-[0-9a-f]{16}", Path(staging).name)
            or target != (prefix / rev).as_posix()):
        raise ProducerError("shared-journal-mismatch", "recovery paths")
    for rel in (Path(staging), Path(target)):
        for part in (rel, *rel.parents):
            if (root / part).is_symlink():
                raise ProducerError("shared-journal-mismatch", "symlink recovery path")
    source = journal.get("source_path", "")
    record = read_cycle_record(root, journal.get("cycle_id", ""))
    if not isinstance(source, str) or not source.startswith("artifacts/") or ".." in source.split("/"):
        raise ProducerError("shared-journal-mismatch", "source identity")
    if journal.get("state") == "published" or (root / target).is_dir():
        # The immutable revision is in place: the journal only waits to be
        # committed.  It names the source manifest document it was published
        # from, found as the current one or a preserved copy (§45 D-127); the
        # cycle may have been refreshed, edited or deleted since.
        digest = journal.get("source_manifest_digest")
        if record is not None and record.get("manifest_digest") != digest:
            copies = artifact_lifecycle.read_manifest_snapshots(root, journal.get("cycle_id", ""))
            if copies and not any(artifact_manifest.manifest_digest(doc) == digest for _raw, doc in copies):
                raise ProducerError("shared-journal-mismatch", "source identity")
        return
    if record is None or record.get("manifest_digest") != journal.get("source_manifest_digest"):
        raise ProducerError("shared-journal-mismatch", "source identity")
    if kind == "spec":
        directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
        _sealed_source_files(directory, record, source, directory / source)


def _commit_shared(root: Path, journal: Mapping[str, Any]) -> None:
    kind = journal["kind"]
    ref_id = journal["reference_id"]
    reference = _read_json(_reference_path(root, kind, ref_id))
    if reference is None:
        reference = {
            "schema_version": 1, "contract": CONTRACT, "shared_reference_id": ref_id,
            "kind": SHARED_KINDS[kind], "key": journal.get("key"), "title": journal.get("title"),
            "created_on": journal.get("created_on"), "latest_revision_id": None, "revisions": [],
        }
    revision = _read_json(root / "shared" / kind / ref_id / "revisions" / journal["revision_id"] / REVISION_RECORD_NAME)
    if (revision is None or revision.get("shared_reference_id") != ref_id
            or revision.get("shared_reference_revision_id") != journal["revision_id"]
            or revision.get("source", {}).get("cycle_id") != journal.get("cycle_id")
            or revision.get("source", {}).get("path") != journal.get("source_path")
            or revision.get("source", {}).get("manifest_digest") != journal.get("source_manifest_digest")):
        raise ProducerError("shared-journal-mismatch", journal["revision_id"])
    if journal["revision_id"] not in reference["revisions"]:
        if "expected_previous_revision_id" not in journal:
            raise ProducerError("shared-base-required", "recovery journal lacks base revision")
        _check_shared_base(journal["expected_previous_revision_id"], reference.get("latest_revision_id"))
    completed = journal["revision_id"] in reference["revisions"]
    if "spec_merge" in journal or "spec_merge" in revision:
        if journal.get("spec_merge") != revision.get("spec_merge"):
            raise ProducerError("shared-journal-mismatch", "merge proof")
        _verify_spec_publication(root, reference, revision, completed=completed)
        if journal.get("expected_previous_revision_id") != revision["spec_merge"]["latest_revision_id"]:
            raise ProducerError("shared-journal-mismatch", "merge parent")
    elif kind == "spec":
        _verify_exact_spec_publication(root, reference, revision, completed=completed)
        if "spec_base_revision_id" in revision and revision["spec_base_revision_id"] != journal.get("expected_previous_revision_id"):
            raise ProducerError("shared-journal-mismatch", "source base")
    if journal["revision_id"] not in reference["revisions"]:
        if "expected_previous_revision_id" not in journal:
            raise ProducerError("shared-base-required", "recovery journal lacks base revision")
        _check_shared_base(journal["expected_previous_revision_id"], reference.get("latest_revision_id"))
        reference["revisions"] = list(reference["revisions"]) + [journal["revision_id"]]
        reference["latest_revision_id"] = journal["revision_id"]
        reference["updated_on"] = journal.get("created_on")
        path = _reference_path(root, kind, ref_id)
        _ensure_dir(path.parent)
        _write_atomic(path, _json_bytes(reference))
    # An already committed journal is cleanup only: never rewind a newer latest.
    if kind == 'spec':
        from artifact_spec_current import refresh
        refresh(root, ref_id, journal['revision_id'])
    try:
        shared_journal_path(root, journal["revision_id"]).unlink()
    except FileNotFoundError:
        pass
