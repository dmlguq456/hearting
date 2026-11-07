"""Artifact producer: shared references and spec publication decisions.

Listing shared references, component-set checks, the code-cycle publication
obligation and decision, `completed_spec_publication`, and `admit_shared`.

Split out of `artifact_producer.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import artifact_admission
import artifact_identity
import artifact_lifecycle
import artifact_manifest
import artifact_directory_bundle

from artifact_producer_store import (
    _canonical, _copy_tree_files, _digest, _ensure_dir, _fsync_dir, _json_bytes, _KEY_RE,
    _published_cycle_state, _read_json, _record_cycle_manifest_path, _reference_path, _rfc3339,
    _walk_files, _write_atomic, _write_exclusive, CONTRACT, cycle_dir, ProducerError,
    read_cycle_record, REVISION_RECORD_NAME, shared_journal_path, SHARED_KINDS,
)

from artifact_producer_routes import (
    resolve_cycle_manifest_route, route_cycle_for,
)

from artifact_producer_manifest import (
    _output_placements, _placed_locator, official_spec_primary_path,
)

from artifact_producer_lease import (
    _path_entry_present,
)

from artifact_producer_refresh import (
    _prescan_cycle, _refresh_cycle_locked,
)

from artifact_producer_spec import (
    _check_shared_base, _commit_shared, _legacy_adopted_spec_base, _merge_spec_publication,
    _sealed_source_files, _spec_admission_base, _spec_bytes, _spec_inventory,
    _spec_seed_components, _verified_shared_spec, _verify_exact_spec_publication,
    _verify_spec_publication, component_set, SPEC_BASE_RECEIPT,
)

from artifact_producer_finalize import (
    _recover_locked,
)


# Shared kinds a root holds exactly one canonical reference of unless told
# otherwise. `analysis` lineages are per subject (a root legitimately carries
# several); `research` promotions are per promotion.
CANONICAL_SINGLE_REFERENCE_KINDS = ("spec",)


def list_references(root: Path, kind: str) -> List[Dict[str, Any]]:
    base = Path(root) / "shared" / kind
    if not base.is_dir():
        return []
    out = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        record = _read_json(entry / "reference.json")
        if record and record.get("shared_reference_id"):
            out.append(record)
    return out


def find_reference_by_key(root: Path, kind: str, key: str) -> Optional[Dict[str, Any]]:
    base = Path(root) / "shared" / kind
    if not base.is_dir():
        return None
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        record = _read_json(entry / "reference.json")
        if record and record.get("key") == key:
            return record
    return None


def _scan_component_set(source_path: Path) -> Set[str]:
    """Component set of the incoming tree, read-only.

    Deliberately does not reuse `_copy_tree_files`: contract (a) requires that a
    refusal create no staging directory at all, and that helper writes as it
    walks."""
    if source_path.is_file():
        return component_set([source_path.name])
    return component_set(
        entry.relative_to(source_path).as_posix() for entry in _walk_files(source_path)
    )


def _revision_component_set(
    root: Path, kind: str, ref_id: str, revision_id: str, *, fallback_to_disk: bool = False
) -> Set[str]:
    revision_dir = Path(root) / "shared" / kind / ref_id / "revisions" / revision_id
    record = _read_json(revision_dir / REVISION_RECORD_NAME)
    if record is None:
        # W7-relocated / adopted revisions (`artifact_cutover._adopt_reference`)
        # carry no revision record; the bytes on disk are their only truth.  Use
        # the directory's top-level entries so D-87 still guards them instead of
        # hard-blocking every later admit on the reference.  Only a revision
        # that is absent on disk as well is an error.
        # The read-only `check-components` surface keeps reporting such a
        # revision as unreadable; only the admit guard opts into the scan.
        if fallback_to_disk and revision_dir.is_dir():
            return _scan_component_set(revision_dir)
        raise ProducerError("revision-record-missing", f"{ref_id}/{revision_id}")
    return component_set(
        str(row.get("path", "")) for row in (record.get("files") or []) if isinstance(row, Mapping)
    )


def _latest_component_set(
    root: Path, kind: str, reference: Optional[Mapping[str, Any]]
) -> Optional[Set[str]]:
    """The previous latest revision's component set, or `None` when there is no
    predecessor to regress against (A17-6: the first revision is exempt)."""
    if not reference:
        return None
    latest = reference.get("latest_revision_id")
    if not latest:
        return None
    return _revision_component_set(
        root, kind, reference["shared_reference_id"], str(latest), fallback_to_disk=True
    )


def check_component_sets(
    root: Path,
    kind: str,
    reference_id: str,
    *,
    from_revision: Optional[str] = None,
    to_revision: Optional[str] = None,
) -> Dict[str, Any]:
    """D-87 (d): read-only check of `components(new) >= components(old) - dropped(new)`
    over adjacent revision pairs.  Writes nothing.

    `from_revision`/`to_revision` bound the inspected window.  The bound is not
    cosmetic: run unbounded over `ref_4d540b57...` this reports nine violations
    reaching back to seq 8, because a partial admit was a chronic pattern long
    before the 2026-09-03 incident.  A caller asking about one incident needs to
    ask about its window.
    """
    root = Path(root).resolve()
    reference = _read_json(_reference_path(root, kind, reference_id))
    if reference is None:
        raise ProducerError("reference-unknown", reference_id)
    revisions = [str(value) for value in (reference.get("revisions") or [])]
    if from_revision is not None:
        if from_revision not in revisions:
            raise ProducerError("revision-unknown", from_revision)
        revisions = revisions[revisions.index(from_revision):]
    if to_revision is not None:
        if to_revision not in revisions:
            raise ProducerError("revision-unknown", to_revision)
        revisions = revisions[: revisions.index(to_revision) + 1]
    pairs: List[Dict[str, Any]] = []
    unreadable: List[str] = []
    sets: Dict[str, Optional[Set[str]]] = {}
    for revision_id in revisions:
        try:
            sets[revision_id] = _revision_component_set(root, kind, reference_id, revision_id)
        except ProducerError:
            sets[revision_id] = None
            unreadable.append(revision_id)
    violations = 0
    for older, newer in zip(revisions, revisions[1:]):
        old_set, new_set = sets[older], sets[newer]
        if old_set is None or new_set is None:
            pairs.append({"old": older, "new": newer, "dropped": [], "missing": [],
                          "verdict": "unknown"})
            continue
        record = _read_json(
            Path(root) / "shared" / kind / reference_id / "revisions" / newer / REVISION_RECORD_NAME
        ) or {}
        dropped = sorted(
            str(row.get("name", ""))
            for row in (record.get("dropped_components") or [])
            if isinstance(row, Mapping)
        )
        missing = sorted(old_set - new_set - set(dropped))
        if missing:
            violations += 1
        pairs.append({"old": older, "new": newer, "dropped": dropped, "missing": missing,
                      "verdict": "regressed" if missing else "ok"})
    return {"status": "checked", "kind": kind, "reference_id": reference_id,
            "pairs": pairs, "violations": violations, "unreadable": sorted(unreadable)}


def _shared_source_intersects_bundle(directory: Path, document: Mapping[str, Any], source_rel: str) -> bool:
    artifacts = {row.get("artifact_id"): row for row in document.get("artifacts", [])
                 if isinstance(row, Mapping)}
    for row in document.get("artifact_revisions", []) or []:
        if not isinstance(row, Mapping) or row.get("media_type") != artifact_directory_bundle.MEDIA_TYPE:
            continue
        if artifacts.get(row.get("artifact_id"), {}).get("type") != "directory-bundle":
            continue
        title = artifacts[row.get("artifact_id")].get("title")
        if not isinstance(title, str) or not title:
            continue
        tree_rel = "artifacts/" + title
        intersects = (tree_rel == source_rel or tree_rel.startswith(source_rel + "/")
                      or source_rel.startswith(tree_rel + "/"))
        if not intersects:
            continue
        locator = row.get("locator")
        rel = locator.get("path") if isinstance(locator, Mapping) else None
        if not isinstance(rel, str):
            continue
        try:
            descriptor = (directory / rel).read_bytes()
            if (len(descriptor) != row.get("byte_size")
                    or "sha256:" + hashlib.sha256(descriptor).hexdigest() != row.get("content_digest")):
                raise ProducerError("bundle-descriptor-revision-mismatch", rel)
            body = artifact_directory_bundle.parse_descriptor(descriptor)
        except (OSError, artifact_directory_bundle.DirectoryBundleError) as exc:
            raise ProducerError("bundle-descriptor-invalid", rel) from exc
        if body["directory"]["path"] != tree_rel:
            raise ProducerError("bundle-title-directory-mismatch", rel)
        return True
    return False


_UNKNOWN_BASE = object()


def _finished_spec_publication(root: Path, reference: Mapping[str, Any], cycle_id: str, source_rel: str,
                               record: Mapping[str, Any], source_path: Path,
                               base_revision: Optional[str], known_publications=None):
    """The earlier publication this `admit-shared` call repeats, if any.

    It is a retry when a revision of this reference was published from this
    cycle's `source_rel` out of one of the cycle's manifest documents (the
    current one or a preserved earlier one) and the call names the same base
    the publication was made from.  A call that names a newer base is a new
    publication of whatever the source holds now, and is not a retry.  When the
    call names no base and the source is gone or unseeded, it can only be the
    retry."""
    digests = {record.get("manifest_digest")}
    digests.update(artifact_manifest.manifest_digest(doc)
                   for _raw, doc in artifact_lifecycle.read_manifest_snapshots(root, cycle_id))
    if base_revision is not None:
        caller_base: Any = None if base_revision == "none" else base_revision
    else:
        receipt = _read_json(source_path / SPEC_BASE_RECEIPT) if source_path.is_dir() else None
        caller_base = receipt.get("revision_id") if isinstance(receipt, dict) and "revision_id" in receipt \
            else _UNKNOWN_BASE
    publications = (known_publications if known_publications is not None else (
        (prior_id, _read_json(root / "shared/spec" / reference["shared_reference_id"] / "revisions" / prior_id
                             / REVISION_RECORD_NAME) or {})
        for prior_id in reversed(list(reference.get("revisions", [])))))
    for prior_id, prior in publications:
        provenance = prior.get("source", {})
        if (provenance.get("cycle_id") != cycle_id or provenance.get("path") != source_rel
                or provenance.get("manifest_digest") not in digests):
            continue
        prior_base = (prior["spec_merge"].get("base_revision_id") if isinstance(prior.get("spec_merge"), dict)
                      else prior.get("spec_base_revision_id"))
        if caller_base is not _UNKNOWN_BASE and caller_base != prior_base:
            continue
        return prior_id, prior
    return None, None


def _spec_source_manifest_hints(root: Path, record: Mapping[str, Any], source_rel: str) -> Tuple[bool, bool]:
    """Return whether a cycle manifest ever named this source and its seed receipt."""
    documents = []
    current = _read_json(_record_cycle_manifest_path(root, record))
    if isinstance(current, dict):
        documents.append(current)
    try:
        documents.extend(document for _raw, document in artifact_lifecycle.read_manifest_snapshots(
            root, record["cycle_id"]) if isinstance(document, dict))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    prefix = source_rel.rstrip("/") + "/"
    has_source = has_seed = False
    for document in documents:
        for row in document.get("artifact_revisions", []):
            if not isinstance(row, Mapping):
                continue
            locator = row.get("locator")
            path = locator.get("path") if isinstance(locator, Mapping) else None
            if not isinstance(path, str):
                continue
            if path == source_rel or path.startswith(prefix):
                has_source = True
            if path == prefix + SPEC_BASE_RECEIPT:
                has_seed = True
    return has_source, has_seed


def _code_cycle_snapshot_spec_hints(root: Path, record: Mapping[str, Any],
                                    source_rel: str) -> Optional[Dict[str, Any]]:
    """Read source/seed evidence from the exact sealed manifest history.

    The cycle payload may be ordinarily removed after completion. Its immutable
    manifest snapshot still distinguishes an unseeded code cycle from a cycle
    with a real spec transaction. Unknown or damaged evidence stays visible to
    the existing publication classifier.
    """
    cycle_id = record.get("cycle_id")
    campaign_id = record.get("campaign_id")
    digest = record.get("manifest_digest")
    if (not isinstance(cycle_id, str) or not isinstance(campaign_id, str)
            or not isinstance(digest, str)):
        return None
    try:
        snapshots = artifact_lifecycle.read_manifest_snapshots(root, cycle_id)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    exact = [document for _raw, document in snapshots
             if artifact_manifest.manifest_digest(document) == digest]
    if len(exact) != 1:
        return None
    sealed = exact[0]
    if not artifact_manifest.validate(sealed).ok:
        return None
    cycle = sealed.get("cycle")
    producer = sealed.get("producer")
    routes = sealed.get("routes")
    if (not isinstance(cycle, Mapping) or cycle.get("cycle_id") != cycle_id
            or cycle.get("campaign_id") != campaign_id
            or cycle.get("state") != record.get("cycle_state")
            or not isinstance(producer, Mapping)):
        return None
    revision = producer.get("source_revision")
    parts = revision.split("/") if isinstance(revision, str) else []
    if (len(parts) < 2 or parts[0] != record.get("capability")
            or parts[1] != record.get("intensity") or not isinstance(routes, list)
            or not any(isinstance(row, Mapping)
                       and row.get("route_id") == record.get("route_id")
                       and row.get("route_hash") == record.get("route_hash") for row in routes)):
        return None

    prefix = source_rel.rstrip("/") + "/"
    has_source = has_seed = False
    for _raw, document in snapshots:
        if not artifact_manifest.validate(document).ok:
            return None
        snapshot_cycle = document.get("cycle")
        snapshot_producer = document.get("producer")
        snapshot_routes = document.get("routes")
        if (not isinstance(snapshot_cycle, Mapping)
                or snapshot_cycle.get("campaign_id") != campaign_id
                or not isinstance(snapshot_producer, Mapping)
                or snapshot_producer.get("source_revision") != revision
                or not isinstance(snapshot_routes, list)
                or not any(isinstance(row, Mapping)
                           and row.get("route_id") == record.get("route_id")
                           and row.get("route_hash") == record.get("route_hash") for row in snapshot_routes)):
            return None
        rows = document.get("artifact_revisions")
        if not isinstance(rows, list):
            return None
        for row in rows:
            locator = row.get("locator") if isinstance(row, Mapping) else None
            path = locator.get("path") if isinstance(locator, Mapping) else None
            if not isinstance(path, str):
                return None
            if path == prefix + SPEC_BASE_RECEIPT:
                has_seed = True
    return {"has_seed": has_seed, "sealed": sealed}


def _code_cycle_snapshot_spec_facts(document: Mapping[str, Any], source_rel: str
                                    ) -> Optional[Dict[str, Tuple[str, int]]]:
    """Project the sealed manifest's current file revisions for one spec tree."""
    prefix = source_rel.rstrip("/") + "/"
    artifacts = [row for row in document.get("artifacts", []) if isinstance(row, Mapping)]
    active_ids = {row.get("artifact_id") for row in artifacts}
    for artifact in artifacts:
        if artifact.get("type") != "directory-bundle":
            continue
        title = artifact.get("title")
        if not isinstance(title, str) or not title:
            return None
        tree_rel = "artifacts/" + title
        if (tree_rel == source_rel or tree_rel.startswith(prefix)
                or source_rel.startswith(tree_rel.rstrip("/") + "/")):
            # The descriptor body is a payload, not part of immutable manifest
            # history. Keep this case on the existing refusal path.
            return None

    latest: Dict[str, Mapping[str, Any]] = {}
    for row in document.get("artifact_revisions", []):
        if not isinstance(row, Mapping) or row.get("artifact_id") not in active_ids:
            continue
        artifact_id = row.get("artifact_id")
        previous = latest.get(artifact_id)
        if previous is None or row.get("revision_sequence", 0) > previous.get("revision_sequence", 0):
            latest[artifact_id] = row

    facts: Dict[str, Tuple[str, int]] = {}
    for row in latest.values():
        locator = row.get("locator")
        path = locator.get("path") if isinstance(locator, Mapping) else None
        if not isinstance(path, str):
            return None
        if path == source_rel:
            return None
        if not path.startswith(prefix):
            continue
        relative = path[len(prefix):]
        digest = row.get("content_digest")
        byte_size = row.get("byte_size")
        if (not relative or not isinstance(digest, str) or not digest.startswith("sha256:")
                or not isinstance(byte_size, int) or byte_size < 0 or relative in facts):
            return None
        facts[relative] = (digest, byte_size)
    return facts


def _code_cycle_snapshot_publication_obligation(
    root: Path, record: Mapping[str, Any], source_rel: str,
    hints: Mapping[str, Any], reference_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Resolve one seed base from immutable evidence when its payload was removed."""
    document = hints.get("sealed")
    if not isinstance(document, Mapping):
        return {"obligation": True}
    source_facts = _code_cycle_snapshot_spec_facts(document, source_rel)
    if source_facts is None:
        return {"obligation": True}
    receipt_fact = source_facts.get(SPEC_BASE_RECEIPT)
    if receipt_fact is None:
        # A seed in an earlier manifest but absent from the exact sealed
        # revision is a missing-seed case, not proof of an unused transaction.
        return {"obligation": True}
    receipt_digest, receipt_size = receipt_fact
    source_components = tuple(sorted({path.split("/", 1)[0] for path in source_facts
                                      if not path.startswith("_internal/")}))

    def matches(reference_id: Optional[str], revision_id: Optional[str],
                content_digest: Optional[str], components: Optional[Tuple[str, ...]]) -> bool:
        raw = _json_bytes({
            "schema_version": 1, "reference_id": reference_id,
            "revision_id": revision_id, "content_digest": content_digest,
            **({"components": list(components),
               "component_seeds": {component: True for component in components}}
              if components is not None else {}),
            "seed_complete": True,
        })
        return len(raw) == receipt_size and _digest(raw) == receipt_digest

    component_receipt = source_components or None
    reference: Optional[Mapping[str, Any]] = None
    reference_id: Optional[str] = None
    revision_id: Optional[str] = None
    components: Optional[Tuple[str, ...]] = None
    if matches(None, None, None, None):
        base_tree: Mapping[str, bytes] = {}
    elif component_receipt is not None and matches(None, None, None, component_receipt):
        components = component_receipt
        base_tree = {}
    else:
        sealed_references = {
            row.get("shared_reference_id"): row
            for row in document.get("shared_references", [])
            if isinstance(row, Mapping) and row.get("kind") == "shared-spec"
            and isinstance(row.get("shared_reference_id"), str)
        }
        exact_matches: Dict[Tuple[str, str, Optional[Tuple[str, ...]]], str] = {}

        def add_exact(candidate_id: Any, candidate_revision: Any, content: Any) -> None:
            if (not isinstance(candidate_id, str) or candidate_id not in sealed_references
                    or not isinstance(candidate_revision, str) or not isinstance(content, str)):
                return
            if matches(candidate_id, candidate_revision, content, None):
                exact_matches[(candidate_id, candidate_revision, None)] = content
            elif (component_receipt is not None
                  and matches(candidate_id, candidate_revision, content, component_receipt)):
                exact_matches[(candidate_id, candidate_revision, component_receipt)] = content

        for row in document.get("shared_reference_revisions", []):
            if isinstance(row, Mapping):
                add_exact(row.get("shared_reference_id"),
                          row.get("shared_reference_revision_id"), row.get("content_digest"))

        if len(exact_matches) != 1:
            if exact_matches or sealed_references:
                return {"obligation": True}
            # Pre-pin completed records remain an ordinary no-op when their
            # campaign payload is gone. New seed operations retain their exact
            # base through the existing shared-reference pin surface.
            return {"obligation": False}

        (reference_id, revision_id, components), expected_content_digest = next(iter(exact_matches.items()))
        candidate = next((row for row in reference_rows or ()
                          if isinstance(row, Mapping)
                          and row.get("shared_reference_id") == reference_id), None)
        reference = candidate if isinstance(candidate, Mapping) else _read_json(
            _reference_path(root, "spec", reference_id))
        if (not isinstance(reference, Mapping)
                or reference.get("shared_reference_id") != reference_id
                or revision_id not in reference.get("revisions", [])):
            return {"obligation": True}
        revision_record = _read_json(
            root / "shared/spec" / reference_id / "revisions" / revision_id / REVISION_RECORD_NAME)
        if (not isinstance(revision_record, Mapping)
                or revision_record.get("shared_reference_id") != reference_id
                or revision_record.get("shared_reference_revision_id") != revision_id
                or revision_record.get("content_digest") != expected_content_digest):
            return {"obligation": True}
        try:
            base_tree, _base_record = _verified_shared_spec(root, reference_id, revision_id)
        except (ProducerError, OSError, ValueError, KeyError, TypeError):
            return {"obligation": True}

    exact_references = [dict(reference)] if isinstance(reference, Mapping) else []
    if exact_references:
        missing_source = root / ".runtime" / "missing-cycle-payload" / str(record.get("cycle_id")) / source_rel
        published_id, published = _finished_spec_publication(
            root, exact_references[0], str(record.get("cycle_id")), source_rel,
            record, missing_source, None)
        if published is not None:
            return {"obligation": True, "references": exact_references,
                    "finished": (exact_references[0], published_id, published)}

    def authoring_facts(files: Mapping[str, Tuple[str, int]]) -> Optional[Dict[str, Tuple[str, int]]]:
        selected: Dict[str, Tuple[str, int]] = {}
        for path, facts in files.items():
            if path == SPEC_BASE_RECEIPT or "/_internal/versions/" in "/" + path:
                continue
            if components is not None and path.split("/", 1)[0] not in components:
                return None
            selected[path] = facts
        return selected

    current_authoring = authoring_facts(source_facts)
    base_facts = {path: (_digest(data), len(data)) for path, data in base_tree.items()}
    base_authoring = authoring_facts(base_facts)
    if current_authoring is None or base_authoring is None:
        return {"obligation": True, "references": exact_references}
    return {"obligation": current_authoring != base_authoring,
            "references": exact_references}


def _code_cycle_publication_decision(root: Path, record: Mapping[str, Any],
                                     reference_rows: Optional[List[Dict[str, Any]]] = None
                                     ) -> Dict[str, Any]:
    """Decide the shared-spec duty from the code cycle's actual transaction evidence."""
    source_rel = _placed_locator("artifacts/spec", _output_placements(root, record))
    hints = _code_cycle_snapshot_spec_hints(root, record, source_rel)
    try:
        _published_cycle_state(root, record)
    except ProducerError as exc:
        if (exc.code == "sealed-cycle-state-unknown"
                and f"{record.get('cycle_id')}: campaign-locator=missing" in exc.detail):
            if hints is not None:
                if not hints["has_seed"]:
                    return {"obligation": False}
                return _code_cycle_snapshot_publication_obligation(
                    root, record, source_rel, hints, reference_rows)
        return {"obligation": True}

    try:
        manifest_path = _record_cycle_manifest_path(root, record)
        source_path = manifest_path.parent / source_rel
        if _path_entry_present(source_path / SPEC_BASE_RECEIPT):
            seed = _read_json(source_path / SPEC_BASE_RECEIPT)
            if not isinstance(seed, Mapping):
                return {"obligation": True}
            if _code_spec_tree_changed(root, record, source_rel, source_path, seed):
                return {"obligation": True}
            reference_id = seed.get("reference_id")
            if not isinstance(reference_id, str):
                return {"obligation": True}
            reference = _read_json(_reference_path(root, "spec", reference_id))
            if not isinstance(reference, Mapping):
                return {"obligation": True}
            revision_id, finished = _finished_spec_publication(
                root, reference, record["cycle_id"], source_rel, record, source_path, None)
            if finished is not None:
                return {"obligation": True, "finished": (reference, revision_id, finished)}
            return {"obligation": False}
        if source_path.is_symlink():
            return {"obligation": True}
        has_source, has_seed = _spec_source_manifest_hints(root, record, source_rel)
        return {"obligation": has_seed}
    except ProducerError as exc:
        if (exc.code == "sealed-cycle-state-unknown"
                and f"{record.get('cycle_id')}: campaign-locator=missing" in exc.detail):
            hints = _code_cycle_snapshot_spec_hints(root, record, source_rel)
            if hints is not None:
                if not hints["has_seed"]:
                    return {"obligation": False}
                return _code_cycle_snapshot_publication_obligation(
                    root, record, source_rel, hints, reference_rows)
        return {"obligation": True}
    except (OSError, ValueError, KeyError, TypeError):
        return {"obligation": True}


def _code_spec_tree_changed(root: Path, record: Mapping[str, Any], source_rel: str,
                            source_path: Path, seed: Mapping[str, Any]) -> bool:
    """Compare only seeded authoring content to its pinned base, not seed metadata/history."""
    if seed.get("seed_complete") is not True:
        raise ProducerError("shared-base-invalid", "incomplete seed")
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    source_rows = _sealed_source_files(directory, record, source_rel, source_path)
    source_tree = _spec_bytes(source_path)
    if source_rows != [(row["path"], row["sha256"], row["byte_size"])
                       for row in _spec_inventory(source_tree)]:
        raise ProducerError("source-manifest-mismatch", source_rel)
    components = _spec_seed_components(source_tree)
    if components is None:
        states = seed.get("component_seeds")
        if (not isinstance(states, dict)
                or any(not isinstance(name, str)
                       or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", name)
                       or value is not True for name, value in states.items())):
            raise ProducerError("shared-base-invalid", "incomplete seed")
    reference_id = seed.get("reference_id")
    base_id = _spec_admission_base(root, source_path, reference_id, None)
    base_tree: Mapping[str, bytes] = {}
    if base_id is not None:
        if not isinstance(reference_id, str):
            raise ProducerError("shared-base-invalid", "reference identity")
        base_tree, _base_record = _verified_shared_spec(root, reference_id, base_id)

    def authoring(tree: Mapping[str, bytes]) -> Dict[str, bytes]:
        selected = {}
        for path, data in tree.items():
            if path == SPEC_BASE_RECEIPT or "/_internal/versions/" in "/" + path:
                continue
            if components is not None and path.split("/", 1)[0] not in components:
                continue
            selected[path] = data
        return selected

    return authoring(source_tree) != authoring(base_tree)


def _covered_spec_publication(root, record, source_rel, source_path, seed, references,
                              known_publications=None):
    """A verified later transaction can satisfy its predecessor's contract duty."""
    if not isinstance(seed, Mapping) or seed.get("seed_complete") is not True:
        return None
    reference = next((row for row in references
                      if row.get("shared_reference_id") == seed.get("reference_id")), None)
    if reference is None:
        return None
    base_id = _spec_admission_base(root, source_path, reference["shared_reference_id"], None)
    if base_id not in reference.get("revisions", []):
        return None
    source = _spec_bytes(source_path)
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    rows = _sealed_source_files(directory, record, source_rel, source_path)
    if rows != [(row["path"], row["sha256"], row["byte_size"]) for row in _spec_inventory(source)]:
        raise ProducerError("source-manifest-mismatch", source_rel)
    components = _spec_seed_components(source)
    base, _ = _verified_shared_spec(root, reference["shared_reference_id"], base_id)
    if components is not None:
        base = {path: data for path, data in base.items() if path.split("/", 1)[0] in components}
    def descendant(cycle_id):
        seen = {record["cycle_id"]}
        while cycle_id and cycle_id not in seen:
            seen.add(cycle_id)
            current = read_cycle_record(root, cycle_id)
            if current is None:
                return False
            cycle_id = current.get("parent_cycle_id")
            if cycle_id == record["cycle_id"]:
                return True
        return False
    later = reference["revisions"][reference["revisions"].index(base_id) + 1:]
    cached = dict((known_publications or {}).get(reference["shared_reference_id"], []))
    import spec_merge
    for revision_id in reversed(later):
        revision = cached.get(revision_id) or _read_json(
            root / "shared/spec" / reference["shared_reference_id"] / "revisions" / revision_id
            / REVISION_RECORD_NAME)
        provenance = revision.get("source") if isinstance(revision, Mapping) else None
        publishing_cycle = provenance.get("cycle_id") if isinstance(provenance, Mapping) else None
        if not publishing_cycle or not descendant(publishing_cycle):
            continue
        if "spec_merge" in revision:
            _verify_spec_publication(root, reference, revision, completed=True)
        else:
            _verify_exact_spec_publication(root, reference, revision, completed=True)
        published, _ = _verified_shared_spec(root, reference["shared_reference_id"], revision_id)
        if spec_merge.changes_covered(base, source, published):
            return reference, revision_id, revision, publishing_cycle
    return None


def completed_spec_publication(root: Path, *, cycle_id: str, settle: bool = False,
                               references: Optional[Sequence[Mapping[str, Any]]] = None,
                               known_publications: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Finish publication after the work is complete, without changing its outcome.

    Spec routes keep their established primary-artifact contract. Code routes
    become eligible only when a completed cycle contains the real spec seed
    receipt and its sealed authoring files differ from that receipt's base.
    Both use the same admission transaction, CAS and immutable-lineage retry.
    """
    root = Path(root).resolve()
    result = {"cycle_id": cycle_id, "status": "not-applicable"}
    try:
        record = read_cycle_record(root, cycle_id)
        if record is None:
            return {**result, "status": "pending", "reason": "cycle-unknown"}
        capability = record.get("capability")
        if capability not in {"autopilot-spec", "autopilot-code"}:
            return result
        if record.get("state") != "sealed":
            return {**result, "reason": "spec-work-not-completed"}
        code_decision = {"obligation": True}
        if capability == "autopilot-code":
            code_decision = _code_cycle_publication_decision(root, record, references)
            if not code_decision["obligation"]:
                return result
            if code_decision.get("references") is not None and code_decision.get("finished") is not None:
                reference, revision_id, revision = code_decision["finished"]
                reference_id = reference["shared_reference_id"]
                result.update(status="admitted", shared_reference_id=reference_id,
                              shared_reference_revision_id=revision_id,
                              content_digest=revision.get("content_digest"))
                if settle:
                    if "spec_merge" in revision:
                        _verify_spec_publication(root, reference, revision, completed=True)
                    else:
                        _verify_exact_spec_publication(root, reference, revision, completed=True)
                    admission = {
                        "status": "reused", "kind": SHARED_KINDS["spec"],
                        "shared_reference_id": reference_id,
                        "shared_reference_revision_id": revision_id,
                        "reference_created": False,
                        "revision_dir": str(root / "shared/spec" / reference_id / "revisions" / revision_id),
                        "content_digest": revision.get("content_digest"),
                        "file_count": revision.get("file_count"),
                        "promotion": revision.get("promotion"),
                    }
                    if "spec_merge" in revision:
                        admission["spec_merge"] = revision["spec_merge"]
                    result["admission"] = admission
                    result["reason"] = None
                return result
        published_state = _published_cycle_state(root, record)
        if published_state != "completed":
            return {**result, "reason": "spec-work-not-completed"}
        directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
        source_rel = _placed_locator("artifacts/spec", _output_placements(root, record))
        source_path = directory / source_rel
        seed_path = source_path / SPEC_BASE_RECEIPT
        seed_present = _path_entry_present(seed_path)

        if capability == "autopilot-code" and not seed_present:
            has_source, has_seed = _spec_source_manifest_hints(root, record, source_rel)
            if not has_source:
                return result

        seed = _read_json(seed_path) if seed_present or source_path.is_dir() else None
        spec_reference_id = None
        if capability == "autopilot-spec":
            if seed_present and seed is None:
                raise ProducerError("shared-base-invalid", SPEC_BASE_RECEIPT)
            spec_reference_id = seed.get("reference_id") if seed is not None else None
            if (spec_reference_id is not None
                    and not artifact_identity.is_well_formed(spec_reference_id, "shared_reference")):
                raise ProducerError("shared-base-invalid", "reference identity")
        refs = list(references) if references is not None else list_references(root, "spec")
        if capability == "autopilot-spec":
            if spec_reference_id is not None:
                refs = [r for r in refs if r.get("shared_reference_id") == spec_reference_id]
                if len(refs) != 1:
                    raise ProducerError("reference-unknown", spec_reference_id)

        # An already admitted source remains finished even after edits/deletion
        # or a newer latest. Check the exact historical manifest lineage first.
        finished = []
        for reference in refs:
            revision_id, revision = _finished_spec_publication(
                root, reference, cycle_id, source_rel, record, source_path, None,
                known_publications=(known_publications.get(reference["shared_reference_id"], [])
                                    if known_publications is not None and not settle else None))
            if revision is not None:
                finished.append((reference, revision_id, revision))
        if len(finished) > 1:
            raise ProducerError("shared-reference-ambiguous", cycle_id)
        if finished:
            reference, revision_id, revision = finished[0]
            reference_id = reference["shared_reference_id"]
            result.update(status="admitted", shared_reference_id=reference_id,
                          shared_reference_revision_id=revision_id,
                          content_digest=revision.get("content_digest"))
            result.pop("reason", None)
            if not settle:
                return result
            admission = admit_shared(root, cycle_id=cycle_id, kind="spec", source="spec",
                                     reference_id=reference_id)
            return {**result, "status": "admitted", "admission": admission,
                    "shared_reference_id": admission["shared_reference_id"],
                    "shared_reference_revision_id": admission["shared_reference_revision_id"],
                    "content_digest": admission.get("content_digest"), "reason": None}

        if capability == "autopilot-code" and not seed_present:
            if source_path.is_symlink() or has_seed:
                raise ProducerError("shared-base-invalid", SPEC_BASE_RECEIPT)
            return result
        if capability == "autopilot-code":
            if seed_present and seed is None:
                raise ProducerError("shared-base-invalid", SPEC_BASE_RECEIPT)
            reference_id = seed.get("reference_id") if seed is not None else None
            if (reference_id is not None
                    and not artifact_identity.is_well_formed(reference_id, "shared_reference")):
                raise ProducerError("shared-base-invalid", "reference identity")
            if reference_id is not None:
                refs = [r for r in refs if r.get("shared_reference_id") == reference_id]
                if len(refs) != 1:
                    raise ProducerError("reference-unknown", reference_id)
            elif len(refs) > 1:
                raise ProducerError("shared-reference-ambiguous", "spec completion lacks a unique seed/reference")
        else:
            reference_id = spec_reference_id
            if reference_id is None and len(refs) > 1:
                raise ProducerError("shared-reference-ambiguous", "spec completion lacks a unique seed/reference")

        if capability == "autopilot-code":
            if seed is None or not _code_spec_tree_changed(root, record, source_rel, source_path, seed):
                return result
        covered = _covered_spec_publication(
            root, record, source_rel, source_path, seed, refs, known_publications)
        if covered is not None:
            reference, revision_id, revision, publishing_cycle = covered
            coverage = {"kind": "admitted-descendant", "publishing_cycle_id": publishing_cycle}
            result.update(status="admitted", shared_reference_id=reference["shared_reference_id"],
                          shared_reference_revision_id=revision_id,
                          content_digest=revision.get("content_digest"), coverage=coverage)
            if settle:
                result["admission"] = {"status": "reused", "kind": SHARED_KINDS["spec"],
                    "shared_reference_id": reference["shared_reference_id"],
                    "shared_reference_revision_id": revision_id,
                    "reference_created": False,
                    "revision_dir": str(root / "shared/spec" / reference["shared_reference_id"]
                                        / "revisions" / revision_id),
                    "content_digest": revision.get("content_digest"),
                    "file_count": revision.get("file_count"),
                    "promotion": revision.get("promotion"), "coverage": coverage}
                if "spec_merge" in revision:
                    result["admission"]["spec_merge"] = revision["spec_merge"]
            return result
        result.update(status="pending", reason="shared-publication-required")
        if capability == "autopilot-spec" and not refs:
            # Reuse the sealed manifest's canonical route admission, including
            # verified continuations of the cycle's original begin identity.
            document = _read_json(directory / "manifest.json") or {}
            _route_file, route = resolve_cycle_manifest_route(root, record, document)
            primary = official_spec_primary_path(root, record, route)
            if primary is None:
                return {**result, "reason": "official-spec-prd-missing"}
            result["primary_path"] = str(primary)
        if not settle:
            return result
        admission = admit_shared(root, cycle_id=cycle_id, kind="spec", source="spec",
                                 reference_id=reference_id)
        # The existing API validates and commits, or reuses the exact earlier
        # revision. Only its actual return makes publication successful.
        return {**result, "status": "admitted", "admission": admission,
                "shared_reference_id": admission["shared_reference_id"],
                "shared_reference_revision_id": admission["shared_reference_revision_id"],
                "content_digest": admission.get("content_digest"), "reason": None}
    except (ProducerError, artifact_admission.AdmissionBusy, artifact_admission.AdmissionRecoveryRequired,
            OSError, ValueError, KeyError, TypeError) as exc:
        return {**result, "status": "pending", "reason": getattr(exc, "code", type(exc).__name__),
                "detail": str(exc)}


def completed_route_spec_publication(route: Mapping[str, Any], *, settle: bool = False) -> Dict[str, Any]:
    """Resolve the route's completed-cycle publication without inventing a code obligation."""
    result = {"status": "not-applicable"}
    capability = route.get("capability")
    if capability not in {"autopilot-spec", "autopilot-code"}:
        return result
    root_value = route.get("artifact_root")
    if not isinstance(root_value, str) or not root_value.strip():
        return ({**result, "status": "pending", "reason": "spec-artifact-root-unavailable"}
                if capability == "autopilot-spec" else result)
    try:
        root = Path(root_value).resolve()
        cycle = route_cycle_for(root, route)
    except (ProducerError, OSError, ValueError, KeyError, TypeError) as exc:
        return {**result, "status": "pending", "reason": getattr(exc, "code", type(exc).__name__),
                "detail": str(exc)}
    if cycle is None:
        return ({**result, "status": "pending", "reason": "spec-cycle-unavailable"}
                if capability == "autopilot-spec" else result)
    publication = completed_spec_publication(root, cycle_id=cycle["cycle_id"], settle=settle)
    return publication if publication.get("status") != "not-applicable" else result


def admit_shared(
    root: Path,
    *,
    cycle_id: str,
    kind: str,
    source: str,
    reference_id: Optional[str] = None,
    key: Optional[str] = None,
    title: Optional[str] = None,
    promote_research: bool = False,
    promotion_evidence: Optional[str] = None,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    drop_components: Sequence[str] = (),
    drop_reason: Optional[str] = None,
    allow_new_reference: bool = False,
    base_revision: Optional[str] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    if kind not in SHARED_KINDS:
        raise ProducerError("shared-kind-not-admissible", kind)
    if kind == "research":
        if not promote_research:
            raise ProducerError("research-promotion-required",
                                "research is admitted to shared/ only with an explicit promotion")
        if not promotion_evidence:
            raise ProducerError("research-promotion-evidence-required")
    if reference_id and not artifact_identity.is_well_formed(reference_id, "shared_reference"):
        raise ProducerError("reference-id-malformed", reference_id)
    if key and not _KEY_RE.match(key):
        raise ProducerError("reference-key-invalid", key)
    alloc = allocator or artifact_identity.IdAllocator()
    prescan = _prescan_cycle(root, cycle_id)  # the cycle's own files, read before the lock (§45 D-124)
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        pre = read_cycle_record(root, cycle_id)
        sweep = _recover_locked(root, now=now, target_campaign_id=pre["campaign_id"] if pre else None)
        sweep_unresolved = sweep.get("unresolved", [])
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        if record.get("state") != "sealed":
            raise ProducerError("cycle-not-sealed", record.get("state", "?"))
        directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
        source_rel = source if source.startswith("artifacts/") else "artifacts/" + source
        if ".." in source_rel.split("/"):
            raise ProducerError("source-unsafe", source)
        source_rel = _placed_locator(source_rel, _output_placements(root, record))
        source_path = directory / source_rel
        reference: Optional[Dict[str, Any]] = None
        if reference_id:
            reference = _read_json(_reference_path(root, kind, reference_id))
            if reference is None:
                raise ProducerError("reference-unknown", reference_id)
        elif key:
            reference = find_reference_by_key(root, kind, key)
        if reference is None and kind in CANONICAL_SINGLE_REFERENCE_KINDS and not allow_new_reference:
            # Defect K (cairn 2026-09-03): `--key cairn-spec` missed the
            # canonical `spec` reference and silently minted a second one, after
            # which "the latest spec" flipped on every admit. A second reference
            # of a canonical-singular kind is an explicit act, never a miss.
            # The documented flow (`admit-shared --kind spec`, no key) keeps
            # working: with exactly one reference it is the canonical one.
            existing = list_references(root, kind)
            listing = ", ".join(f"{r['shared_reference_id']}(key={r.get('key')})" for r in existing)
            if key is None and len(existing) == 1:
                reference = existing[0]
            elif key is None and len(existing) > 1:
                raise ProducerError("shared-reference-ambiguous",
                                    f"{kind}: {listing}; pass --reference <id> or --key <key>")
            elif key is not None and existing:
                raise ProducerError(
                    "shared-reference-exists",
                    f"{kind}: {listing}; the key {key!r} matches none of them -- pass --reference <id> "
                    "(or --key of an existing one), or --new-reference to add another",
                )
        if reference is None:
            reference_id = alloc.allocate("shared_reference")
            created = True
        else:
            reference_id = reference["shared_reference_id"]
            created = False
        if kind == "spec" and reference is not None:
            # A publication that already finished is returned as it is (§45 D-127),
            # before the source is read: the report may have been edited, moved or
            # deleted since, and none of that makes the finished publication unfinished.
            finished_id, finished = _finished_spec_publication(
                root, reference, cycle_id, source_rel, record, source_path, base_revision)
            if finished is not None:
                if "spec_merge" in finished:
                    _verify_spec_publication(root, reference, finished, completed=True)
                else:
                    _verify_exact_spec_publication(root, reference, finished, completed=True)
                prior_dir = root / "shared" / kind / reference_id / "revisions" / finished_id
                return {"status": "reused", "kind": SHARED_KINDS[kind],
                        "shared_reference_id": reference_id, "shared_reference_revision_id": finished_id,
                        "reference_created": False, "revision_dir": str(prior_dir),
                        "content_digest": finished.get("content_digest"), "file_count": finished["file_count"],
                        "promotion": finished["promotion"],
                        **({"spec_merge": finished["spec_merge"]} if "spec_merge" in finished else {})}
        if os.path.islink(str(source_path)) or not source_path.exists():
            raise ProducerError("source-missing", source_rel)
        evidence_rel: Optional[str] = None
        evidence_digest: Optional[str] = None
        if kind == "research":
            assert promotion_evidence is not None
            evidence_rel = promotion_evidence if promotion_evidence.startswith("artifacts/") else "artifacts/" + promotion_evidence
            evidence_rel = _placed_locator(evidence_rel, _output_placements(root, record))
            evidence_path = directory / evidence_rel
            if os.path.islink(str(evidence_path)) or not evidence_path.is_file():
                raise ProducerError("research-promotion-evidence-missing", evidence_rel)
            evidence_digest = _digest(evidence_path.read_bytes())
        # §45 D-123/D-124: a new publication takes the files as they are now, so the
        # cycle's manifest is brought up to them first (it may have changed since the close).
        if _path_entry_present(_record_cycle_manifest_path(root, record)):
            if _refresh_cycle_locked(root, record, now=now, prescan=prescan, trigger="admit-shared")["refreshed"]:
                record = read_cycle_record(root, cycle_id)
        # An unresolved publication intent for this exact source cannot be
        # replaced by a second publication. Preserve it for checked recovery.
        for issue in sweep_unresolved:
            pending_id = issue.get("revision_id")
            if not pending_id:
                continue
            pending = _read_json(shared_journal_path(root, pending_id))
            if (pending is None or not all(pending.get(k) for k in
                    ("reference_id", "cycle_id", "source_path", "source_manifest_digest")) or (pending.get("reference_id") == reference_id
                    and pending.get("cycle_id") == cycle_id and pending.get("source_path") == source_rel
                    and pending.get("source_manifest_digest") == record.get("manifest_digest"))):
                raise ProducerError("shared-publication-unresolved", str(pending_id))
        dropped = sorted({str(name) for name in drop_components if str(name)})
        drop_decisions = [{"name": name, "reason": drop_reason or "unspecified"} for name in dropped]
        document = _read_json(directory / "manifest.json") or {}
        bundle_intersects = _shared_source_intersects_bundle(directory, document, source_rel)
        source_rows = (_sealed_source_files(directory, record, source_rel, source_path)
                       if kind == "spec" or bundle_intersects else None)
        merged_tree = None
        merge_proof = None
        if kind == "spec":
            # Initial unseeded publications have no predecessor to overwrite.
            expected = (_spec_admission_base(root, source_path, reference_id, base_revision)
                        if reference is not None or base_revision is not None or (source_path / SPEC_BASE_RECEIPT).exists()
                        else None)
            latest_id = (reference or {}).get("latest_revision_id")
            # Canonical revisions with an inventory are verified even on the
            # exact-base path; legacy adopted revisions retain their old path.
            if expected:
                if not _legacy_adopted_spec_base(root, reference or {}, expected):
                    _verified_shared_spec(root, reference_id, expected)
            source_tree = _spec_bytes(source_path)
            components = _spec_seed_components(source_tree)
            if expected != latest_id or (components is not None and expected is not None):
                if not expected or not latest_id:
                    _check_shared_base(expected, latest_id)
                if expected not in reference.get("revisions", []) or latest_id not in reference.get("revisions", []):
                    _check_shared_base(expected, latest_id)
                source_tree = _spec_bytes(source_path)
                if source_rows != [(r["path"], r["sha256"], r["byte_size"])
                                   for r in _spec_inventory(source_tree)]:
                    raise ProducerError("source-manifest-mismatch", source_rel)
                base_tree, _ = _verified_shared_spec(root, reference_id, expected)
                # Preserve the original omission guard relative to the actual
                # base. Only additions from latest have carry-forward authority.
                missing_base = (component_set(base_tree) - component_set(source_tree) - set(drop_components)
                                if components is None else set())
                if missing_base:
                    raise ProducerError("component-set-regressed", ",".join(sorted(missing_base)))
                merged_tree, merge_proof = _merge_spec_publication(root, reference, expected, latest_id, source_tree, drop_decisions)
        # D-87 (a): refuse before anything exists.  This sits above the id
        # allocation, the journal write and the staging directory on purpose --
        # the contract requires a refused admit to leave no revision, no journal
        # and no staging behind.
        previous = _latest_component_set(root, kind, reference)
        if previous is not None:
            unknown = sorted(set(dropped) - previous)
            if unknown:
                raise ProducerError("drop-component-unknown", ",".join(unknown))
            incoming_components = (component_set(merged_tree) if merged_tree is not None
                                   else _scan_component_set(source_path))
            missing = sorted(previous - incoming_components - set(dropped))
            if missing:
                raise ProducerError(
                    "component-set-regressed",
                    "source omits components carried by the previous latest revision: "
                    + ",".join(missing)
                    + "; admit the whole tree or drop them explicitly with --drop-component",
                )
        elif dropped:
            raise ProducerError("drop-component-unknown", ",".join(dropped))
        revision_id = alloc.allocate("shared_reference_revision")
        revisions_dir = Path(root) / "shared" / kind / reference_id / "revisions"
        _ensure_dir(revisions_dir)
        target = revisions_dir / revision_id
        if target.exists():
            raise ProducerError("revision-exists", str(target))
        staging = revisions_dir / f".admitting-{os.urandom(8).hex()}"
        journal = {
            "schema_version": 1, "state": "staging", "kind": kind, "reference_id": reference_id,
            "revision_id": revision_id, "key": key or (reference or {}).get("key"),
            "title": title or (reference or {}).get("title") or source_rel,
            "created_on": _rfc3339(now), "staging": os.path.relpath(str(staging), str(root)),
            "target": os.path.relpath(str(target), str(root)), "cycle_id": cycle_id,
            "expected_previous_revision_id": (reference or {}).get("latest_revision_id"),
            "source_path": source_rel, "source_manifest_digest": record.get("manifest_digest"),
        }
        if merge_proof is not None:
            journal["spec_merge"] = merge_proof
        _ensure_dir(shared_journal_path(root, revision_id).parent)
        _write_exclusive(shared_journal_path(root, revision_id), _json_bytes(journal), 0o600)
        os.makedirs(str(staging))
        try:
            if merged_tree is None:
                rows, violations = _copy_tree_files(source_path, staging)
            else:
                rows, violations = [], []
                for rel, data in sorted(merged_tree.items()):
                    dst = staging / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    _write_exclusive(dst, data)
                    rows.append((rel, _digest(data), len(data)))
                for current, _, _ in os.walk(staging):
                    _fsync_dir(Path(current))
            if violations:
                raise ProducerError("source-invalid", ";".join(violations))
            if not rows:
                raise ProducerError("source-empty", source_rel)
            if source_rows is not None and merged_tree is None and sorted(rows) != source_rows:
                raise ProducerError("source-manifest-mismatch", source_rel)
            content_digest = _digest(_canonical([[rel, digest, size] for rel, digest, size in rows]))
            sequence = len((reference or {}).get("revisions", [])) + 1
            revision = {
                "schema_version": 1, "contract": CONTRACT,
                "shared_reference_revision_id": revision_id, "shared_reference_id": reference_id,
                "kind": SHARED_KINDS[kind], "sequence": sequence, "content_digest": content_digest,
                "file_count": len(rows), "byte_size": sum(size for _, _, size in rows),
                "created_on": journal["created_on"],
                "source": {
                    "campaign_id": record["campaign_id"], "cycle_id": cycle_id,
                    "manifest_digest": record.get("manifest_digest"), "path": source_rel,
                    "capability": record.get("capability"), "route_id": record.get("route_id"),
                },
                "promotion": (
                    {"kind": "explicit", "evidence": evidence_rel, "evidence_digest": evidence_digest}
                    if kind == "research" else {"kind": "canonical-shared-kind"}
                ),
                "files": [{"path": rel, "sha256": digest, "byte_size": size} for rel, digest, size in rows],
            }
            if merge_proof is not None:
                revision["spec_merge"] = merge_proof
            elif kind == "spec":
                revision["spec_base_revision_id"] = expected
            # D-87 (b): an explicit removal is named and reasoned in the record.
            # The key is omitted entirely when nothing was dropped, so an
            # ordinary admit's revision record is byte-identical to before
            # (A17-4). `content_digest` covers `files[]` only, so this key never
            # moves the digest either way.
            if dropped:
                revision["dropped_components"] = drop_decisions
            _write_exclusive(staging / REVISION_RECORD_NAME, _json_bytes(revision))
            _fsync_dir(staging)
        except BaseException:
            shutil.rmtree(str(staging), ignore_errors=True)
            try:
                shared_journal_path(root, revision_id).unlink()
            except FileNotFoundError:
                pass
            raise
        if target.exists():
            shutil.rmtree(str(staging), ignore_errors=True)
            raise ProducerError("revision-exists", str(target))
        # COMMIT POINT: no-replace rename of the staged immutable revision.
        artifact_admission.atomic_publish.rename_directory_locked(staging, target)
        _fsync_dir(revisions_dir)
        journal["state"] = "published"
        _write_atomic(shared_journal_path(root, revision_id), _json_bytes(journal), 0o600)
        _commit_shared(root, journal)
        result = {
            "status": "admitted", "kind": SHARED_KINDS[kind], "shared_reference_id": reference_id,
            "shared_reference_revision_id": revision_id, "reference_created": created,
            "revision_dir": str(target), "content_digest": content_digest, "file_count": len(rows),
            "promotion": revision["promotion"],
        }
        if merge_proof is not None:
            result["spec_merge"] = merge_proof
        if sweep_unresolved:
            result["recovery_unresolved"] = sweep_unresolved
        return result
    finally:
        artifact_admission._release_lock(root, lock_fd)
