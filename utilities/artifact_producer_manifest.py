"""Artifact producer: output enumeration and manifest construction.

Enumerating a cycle's outputs, directory bundles, placed-output proof, the
primary-document choice, shared reference pins, and `build_manifest`.

Split out of `artifact_producer.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import artifact_payload
import artifact_identity
import artifact_lifecycle
import artifact_locator
import artifact_manifest
import artifact_directory_bundle
import route_lineage

from artifact_producer_store import (
    _canonical, _digest, _fsync_dir, _read_json, _reference_path, _rfc3339, _stream_file_facts,
    _walk_cycle_files, _write_exclusive, ALGORITHM_VERSION, BUCKET_TYPES, cycle_dir,
    journal_path, MEDIA_TYPES, PRIMARY_CANDIDATES, producer_dir, ProducerError, read_campaign,
    read_cycle_record, RECORD_SEGMENTS, SHARED_KINDS, STATUS_DOCUMENT_NAMES, SUPPORT_SEGMENTS,
    USER_DOCUMENT_NAMES,
)

from artifact_producer_routes import (
    _write_journal, route_is_closed,
)

from artifact_producer_interim import (
    checkpoint_state_path, open_manifest_path, read_interim_reservation,
)


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------


def _media_type(rel: str) -> str:
    return MEDIA_TYPES.get(Path(rel).suffix.lower(), "application/octet-stream")


def _bucket_type(rel: str) -> str:
    first = rel.split("/", 1)[0]
    return BUCKET_TYPES.get(first, "file")


def _unmanifestable_reason(rel: str) -> Optional[str]:
    """Why a relocated legacy file cannot carry a D-6 locator (None when it can)."""
    for part in rel.split("/"):
        if part.startswith("."):
            return "hidden-component"
        if not artifact_manifest._LOCATOR_COMPONENT_RE.match(part):
            return "invalid-component"
    return None


_TEMPORARY_FILE_SUFFIXES = (".pyc", ".swp", "~", ".tmp", ".part")


def _browser_profile(parts: Sequence[str], *, directory: bool = False) -> bool:
    """Reserved browser-review scratch, including profiles left by old reviewers."""
    payload = tuple(parts)
    if payload and payload[0] == "artifacts":
        payload = payload[1:]
    return bool(payload and payload[0] == "reviews" and any(
        payload[index - 1] == "browser"
        and (part == "profile" or part.startswith("profile-"))
        for index, part in enumerate(payload)
        if index > 1 and (directory or index < len(payload) - 1)
    ))


# How much of an `output-invalid` refusal names: its first violations, up to these bounds, then
# "and M more". The refusal is recorded and read back whole (a record over 64 KiB is not read),
# and a held refusal watches the files it names; once those are fixed the next close names the
# rest, so naming them all is never needed.
OUTPUT_INVALID_NAMED = 20
OUTPUT_INVALID_NAMED_BYTES = 8192


def output_invalid_detail(violations: Sequence[str]) -> str:
    """`code:path` items for the first violations, and `and M more` for the rest."""
    shown: List[str] = []
    size = 0
    for item in violations:
        if len(shown) >= OUTPUT_INVALID_NAMED or (shown and size + len(item) + 1 > OUTPUT_INVALID_NAMED_BYTES):
            break
        shown.append(item)
        size += len(item) + 1
    more = len(violations) - len(shown)
    return ";".join(shown + ([f"and {more} more"] if more else []))


def _payload_root(rel: str) -> Optional[str]:
    """Return the exact legal payload subtree for a non-empty cycle-relative locator."""
    root, separator, tail = rel.partition("/")
    return root if separator and tail and root in {"artifacts", "_internal"} else None


def _is_cycle_root_support(rel: str) -> bool:
    return _payload_root(rel) == "_internal"


def _payload_inner(rel: str) -> str:
    return rel[len("artifacts/"):] if rel.startswith("artifacts/") else rel


def _outside_inclusion_rule(rel: str) -> bool:
    """§45 D-123's one inclusion rule: what a cycle's manifest never lists.

    A hidden component (a dot-prefixed one, which covers `.git/`, `.pytest_cache/`
    and Emacs `.#*` locks), `__pycache__/`, `*.pyc`, editor temporaries (`*.swp`,
    `*~`) and `*.tmp`/`*.part` are runtime residue, not output.  `_internal/` and
    other support paths, binary files and large files are output.  Symbolic links
    are the other half of the rule and are judged by `lstat`, not by name.
    """
    parts = rel.split("/")
    return (any(part.startswith(".") or part == "__pycache__" for part in parts)
            or _browser_profile(parts)
            or parts[-1].endswith(_TEMPORARY_FILE_SUFFIXES))


def _enumerate_output(directory: Path, *, exclude_hidden: bool = False,
                      excluded: Optional[List[str]] = None,
                      exclude_symlinks: bool = False,
                      excluded_symlinks: Optional[List[str]] = None) -> Tuple[List[Tuple[str, bytes]], List[str]]:
    """Regular files under `artifacts/` that the inclusion rule keeps.

    Finalize and the later refreshes share one rule (`_outside_inclusion_rule`
    plus symbolic links): a path outside it is left out of the manifest and
    reported through `excluded`, a link through `excluded_symlinks`; it never
    fails the close and needs no flag.  A link is only lstat-ed, never followed
    or read.  `exclude_symlinks` is accepted for old callers and changes
    nothing.  `exclude_hidden` additionally leaves out files whose path cannot
    be a D-6 locator (a component longer than the locator limit or outside its
    alphabet), reported through `excluded` (W7E retrospective seal of relocated
    legacy trees)."""
    paths, violations = _output_paths(directory, exclude_hidden=exclude_hidden, excluded=excluded,
                                      excluded_symlinks=excluded_symlinks)
    if violations:
        return [], violations
    return [(rel, entry.read_bytes()) for rel, entry in paths], violations


def _output_paths(directory: Path, *, exclude_hidden: bool = False,
                  excluded: Optional[List[str]] = None,
                  excluded_symlinks: Optional[List[str]] = None,
                  marker_observations: Optional[Dict[str, Any]] = None,
                  excluded_roots: Optional[List[str]] = None,
                  skip_directories: Sequence[str] = (), read_markers: bool = True) -> Tuple[List[Tuple[str, Path]], List[str]]:
    """`_enumerate_output` without its reads: the kept `(path, file)` list and the violations.

    Nothing is opened here (links and kinds are `lstat`-ed), so a first close can list a cycle
    before the admission lock and read each file only once, in chunks."""
    paths: List[Tuple[str, Path]] = []
    violations: List[str] = []
    artifacts = directory / "artifacts"
    if not artifacts.is_dir() or artifacts.is_symlink():
        raise ProducerError("artifacts-dir-missing", str(artifacts))
    for entry in _walk_cycle_files(directory, marker_observations=marker_observations,
                                   excluded_roots=excluded_roots, excluded=excluded,
                                   skip_directories=skip_directories, read_markers=read_markers):
        rel = entry.relative_to(directory).as_posix()
        if rel == artifact_locator.CYCLE_BINDING:
            # Machine-owned locator binding, not user output or manifest data.
            continue
        if os.path.islink(str(entry)):
            if excluded_symlinks is not None:
                excluded_symlinks.append(rel)
            continue
        payload_root = _payload_root(rel)
        if payload_root is not None and _outside_inclusion_rule(rel):
            if excluded is not None:
                excluded.append(rel)
            continue
        if not entry.is_file():
            violations.append(f"non-regular-file:{rel}")
            continue
        if payload_root is None:
            violations.append(f"file-outside-artifacts:{rel}")
            continue
        if (exclude_hidden and payload_root == "artifacts"
                and _unmanifestable_reason(rel) is not None):
            if excluded is not None:
                excluded.append(rel)
            continue
        locator = artifact_manifest.validate_locator_path(rel)
        if not locator.ok:
            violations.extend(f"{v.code}:{rel}" for v in locator.violations)
            continue
        paths.append((rel, entry))
    # Validate the whole collection before reading payload bytes. A rejected
    # path must not silently vanish, or surface only after manifest allocation.
    return ([], violations) if violations else (paths, violations)


def _observe_marker_directory(directory: Path, path: Path, marker_observations: Dict[str, Any],
                             excluded_roots: List[str], excluded: List[str]) -> bool:
    rel = path.relative_to(directory).as_posix()
    observation = marker_observations.get(rel)
    if observation is None:
        observation = artifact_payload.observe_directory(path, payload_root=directory / "artifacts")
        marker_observations[rel] = observation
    if observation[0]:
        if rel not in excluded_roots:
            excluded_roots.append(rel)
        if rel + "/" not in excluded:
            excluded.append(rel + "/")
    return observation[0]


def _scan_directory_bundles(directory: Path, *, selected_out: Optional[List[str]] = None,
                            marker_observations: Optional[Dict[str, Any]] = None,
                            excluded_roots: Optional[List[str]] = None,
                            excluded: Optional[List[str]] = None) -> List[artifact_directory_bundle.Tree]:
    """Select candidate bundles; ordinary collection records excluded symlinks.

    Published descriptor verification uses the bundle reader's strict default
    selection instead of this candidate policy.
    """
    try:
        def prune(path: Path) -> bool:
            return _observe_marker_directory(
                directory, path, marker_observations if marker_observations is not None else {},
                excluded_roots if excluded_roots is not None else [], excluded if excluded is not None else [])

        selected = artifact_directory_bundle.select_outermost(
            Path(directory) / "artifacts", prune_directory=prune)
        if selected_out is not None:
            selected_out[:] = selected
        trees = []
        for rel in selected:
            try:
                trees.append(artifact_directory_bundle.scan_tree(directory, rel))
            except artifact_directory_bundle.DirectoryBundleError as exc:
                if exc.code == "tree-empty":
                    continue
                raise ProducerError("directory-bundle-" + exc.code, exc.path or rel) from exc
        return trees
    except artifact_directory_bundle.DirectoryBundleError as exc:
        raise ProducerError("directory-bundle-" + exc.code, exc.path or str(directory)) from exc


def _bundle_tree_fingerprints(tree: artifact_directory_bundle.Tree) -> Dict[str, List[int]]:
    base = tree.directory
    rows = {base: list(tree.directories[0][1])}
    for inner, fingerprint in tree.directories:
        if inner:
            rows[f"{base}/{inner}"] = list(fingerprint)
    for leaf in tree.leaves:
        rows[f"{base}/{leaf.path}"] = list(leaf.fingerprint)
    return rows


def _bundle_inventory_holds(directory: Path, tree: artifact_directory_bundle.Tree) -> bool:
    expected = []
    for inner, identity in tree.directories:
        if inner:
            expected.append((inner, "directory", tuple(identity)))
    for leaf in tree.leaves:
        expected.append((leaf.path, "file", tuple(leaf.fingerprint)))
    try:
        actual = artifact_directory_bundle.scan_inventory(directory, tree.directory)
    except artifact_directory_bundle.DirectoryBundleError:
        return False
    return actual == tuple(sorted(expected, key=lambda row: row[0].encode("utf-8")))


def _bundle_workflow_evidence_facts(root: Path, record: Mapping[str, Any], directory: Path,
                                    trees: Sequence[artifact_directory_bundle.Tree]) -> List[Tuple[str, str, int]]:
    selected = {f"{tree.directory}/{leaf.path}": leaf for tree in trees for leaf in tree.leaves}
    if not selected:
        return []
    try:
        import artifact_workflow_groups as workflow_groups
        declaration = workflow_groups._load_existing(
            workflow_groups.declaration_path(root, record["campaign_id"]))
    except Exception:
        return []
    if not isinstance(declaration, Mapping):
        return []
    candidate_refs = []
    for group in declaration.get("groups", []) if isinstance(declaration.get("groups"), list) else []:
        for relation in group.get("relations", []) if isinstance(group, Mapping) and isinstance(group.get("relations"), list) else []:
            if not isinstance(relation, Mapping):
                continue
            for ref in relation.get("evidence_refs", []) if isinstance(relation.get("evidence_refs"), list) else []:
                if not isinstance(ref, Mapping) or not isinstance(ref.get("path"), str):
                    continue
                try:
                    rel = (Path(root) / ref["path"]).relative_to(directory).as_posix()
                except (ValueError, OSError):
                    continue
                if rel in selected:
                    candidate_refs.append((ref, relation))
    if not candidate_refs:
        return []
    campaign = read_campaign(root, record["campaign_id"])
    identity = artifact_lifecycle.read_root_identity(root)
    if campaign is None or identity is None:
        return []
    reserved, status = read_interim_reservation(root, record)
    open_doc = _read_json(open_manifest_path(root, record["cycle_id"])) or {}
    synthetic = copy.deepcopy(open_doc) if isinstance(open_doc, Mapping) else {}
    synthetic.update({
        "artifact_root_id": identity.artifact_root_id,
        "repository_id": identity.repository_id,
        "campaign": {"campaign_id": campaign["campaign_id"]},
        "cycle": {"cycle_id": record["cycle_id"], "campaign_id": record["campaign_id"], "state": "open"},
    })
    synthetic.setdefault("artifacts", [])
    synthetic.setdefault("artifact_revisions", [])
    preserved = {}
    for rel, leaf in selected.items():
        reservation = reserved.artifacts.get(rel) if reserved is not None else None
        revision = reserved.revision_for(rel, leaf.digest) if reserved is not None else None
        if reservation is None or revision is None:
            continue
        existing = [row for row in synthetic["artifact_revisions"]
                    if isinstance(row, Mapping) and isinstance(row.get("locator"), Mapping)
                    and row["locator"].get("path") == rel]
        if existing:
            if (len(existing) != 1 or existing[0].get("artifact_id") != reservation.artifact_id
                    or existing[0].get("artifact_revision_id") != revision.artifact_revision_id
                    or existing[0].get("content_digest") != leaf.digest):
                return []
            preserved[rel] = leaf
            continue
        synthetic["artifacts"].append({"artifact_id": reservation.artifact_id, "cycle_id": record["cycle_id"]})
        synthetic["artifact_revisions"].append({
            "artifact_id": reservation.artifact_id,
            "artifact_revision_id": revision.artifact_revision_id,
            "content_digest": leaf.digest,
            "locator": {"kind": "cycle-relative", "path": rel},
        })
        preserved[rel] = leaf
    if status in ("unreadable", "identity-mismatch"):
        return []
    try:
        validated = workflow_groups._validate_document(
            root, campaign, directory, identity.artifact_root_id, identity.repository_id,
            declaration)
    except workflow_groups.WorkflowGroupError as exc:
        return []
    facts = {}
    for group in validated.get("groups", []):
        for relation in group.get("relations", []):
            for ref in relation.get("evidence_refs", []):
                try:
                    rel = (Path(root) / ref["path"]).relative_to(directory).as_posix()
                except (KeyError, ValueError, OSError):
                    continue
                leaf = preserved.get(rel)
                if not leaf:
                    continue
                endpoints = []
                for endpoint_id in (relation.get("from_cycle_id"), relation.get("to_cycle_id")):
                    if endpoint_id == record["cycle_id"]:
                        endpoints.append((endpoint_id, directory))
                        continue
                    endpoint_record = read_cycle_record(root, endpoint_id)
                    if endpoint_record is None:
                        return []
                    endpoints.append((endpoint_id, cycle_dir(
                        root, endpoint_record["campaign_id"], endpoint_id, endpoint_record)))
                try:
                    bound = workflow_groups._bind_evidence(
                        root, campaign, ref, tuple(endpoints),
                        manifest_overrides={record["cycle_id"]: synthetic})
                except workflow_groups.WorkflowGroupError as exc:
                    return []
                if bound.get("sha256") == leaf.digest:
                    facts[rel] = (rel, leaf.digest, leaf.size)
    candidate_paths = {(Path(root) / ref["path"]).relative_to(directory).as_posix()
                       for ref, _relation in candidate_refs}
    if len(facts) != len(candidate_paths):
        return []
    return [facts[rel] for rel in sorted(facts)]


def _bundle_exact_evidence_facts(root: Path, record: Mapping[str, Any], directory: Path,
                                trees: Sequence[artifact_directory_bundle.Tree],
                                route: Mapping[str, Any], primary: Optional[str]) -> List[Tuple[str, str, int]]:
    """Keep existing exact primary and terminal file evidence as individual rows."""
    selected = {f"{tree.directory}/{leaf.path}": leaf for tree in trees for leaf in tree.leaves}
    candidates = set()
    primary_rel = _cycle_relative_primary(primary, directory)
    if isinstance(primary_rel, str) and primary_rel in selected:
        candidates.add(primary_rel)
    try:
        route_module = artifact_lifecycle._load_capability_route()
        observations = route_module.terminal_gate_observation(
            route, jobs=os.environ.get("AGENT_DISPATCH_JOBS") or None)
    except Exception:
        observations = {}
    for proof in observations.values() if isinstance(observations, Mapping) else ():
        evidence = proof.get("evidence") if isinstance(proof, Mapping) and proof.get("passed") is True else None
        if not isinstance(evidence, str) or not os.path.isabs(evidence):
            continue
        try:
            rel = Path(evidence).relative_to(directory).as_posix()
        except ValueError:
            continue
        leaf = selected.get(rel)
        if leaf is not None and proof.get("evidence_digest") in (None, leaf.digest.removeprefix("sha256:")):
            candidates.add(rel)
    facts = [(rel, selected[rel].digest, selected[rel].size) for rel in sorted(candidates)]
    facts.extend(_bundle_workflow_evidence_facts(root, record, directory, trees))
    return list({rel: (rel, digest, size) for rel, digest, size in facts}.values())


def _bundle_locator(directory: str) -> str:
    if directory == "artifacts/raw-results":
        return "artifacts/_internal/directory-bundles/raw-results.json"
    suffix = hashlib.sha256(directory.encode("utf-8")).hexdigest()
    return f"artifacts/_internal/directory-bundles/nested-{suffix}.json"


def _publish_bundle_descriptors(root: Path, cycle_id: str, cycle_directory: Path,
                                trees: Sequence[artifact_directory_bundle.Tree]):
    """Journal ownership before publishing canonical descriptor bytes."""
    journal = _read_json(journal_path(root, cycle_id)) or {}
    old_rows = [dict(row) for row in journal.get("directory_bundles", []) if isinstance(row, Mapping)]
    if not trees:
        if old_rows:
            raise ProducerError("directory-bundle-owned-tree-missing", cycle_id)
        return [], {}
    old_by_directory = {row.get("directory"): row for row in old_rows}
    entries = []
    targets = []
    parent = cycle_directory / "artifacts" / "_internal" / "directory-bundles"
    try:
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise ProducerError("directory-bundle-parent-unsafe", str(parent))
        parent_fd = artifact_directory_bundle._open_directory_chain(parent)
        os.close(parent_fd)
    except OSError as exc:
        raise ProducerError("directory-bundle-write-failed", str(exc)) from exc
    for tree in trees:
        previous = old_by_directory.get(tree.directory)
        locator = previous.get("locator") if previous else _bundle_locator(tree.directory)
        if not isinstance(locator, str) or not locator.startswith("artifacts/_internal/directory-bundles/"):
            raise ProducerError("directory-bundle-journal-invalid", str(locator))
        if previous is None:
            stem = Path(locator).stem
            suffix = 1
            candidate = locator
            while os.path.lexists(cycle_directory / candidate):
                suffix += 1
                candidate = f"artifacts/_internal/directory-bundles/{stem}-{suffix}.json"
            locator = candidate
        destination = cycle_directory / locator
        old_digest = previous.get("descriptor_digest") if previous else None
        prior_digest = previous.get("previous_descriptor_digest") if previous else None
        existing_digest = None
        if os.path.lexists(destination):
            try:
                st = os.lstat(destination)
                if not stat.S_ISREG(st.st_mode):
                    raise ProducerError("directory-bundle-owned-file-unsafe", locator)
                existing_digest = _stream_file_facts(destination)[0]
            except OSError as exc:
                raise ProducerError("directory-bundle-owned-file-unreadable", locator) from exc
            if existing_digest not in {old_digest, prior_digest, tree.descriptor_digest}:
                raise ProducerError("directory-bundle-owned-file-changed", locator)
        row = {"directory": tree.directory, "locator": locator,
               "descriptor_digest": tree.descriptor_digest,
               "previous_descriptor_digest": existing_digest if existing_digest != tree.descriptor_digest else None}
        entries.append(row)
        targets.append((tree, row, destination, existing_digest))
    _write_journal(root, cycle_id, state="preparing", directory_bundles=entries)
    for tree, row, destination, existing_digest in targets:
        if existing_digest == tree.descriptor_digest:
            continue
        temp = destination.with_name("." + destination.name + ".tmp-" + os.urandom(6).hex())
        _write_exclusive(temp, tree.descriptor)
        try:
            if existing_digest is None:
                try:
                    os.link(temp, destination, follow_symlinks=False)
                except FileExistsError as exc:
                    raise ProducerError("directory-bundle-publish-collision", row["locator"]) from exc
                temp.unlink()
            else:
                if _stream_file_facts(destination)[0] != existing_digest:
                    raise ProducerError("directory-bundle-owned-file-changed", row["locator"])
                os.replace(temp, destination)
            _fsync_dir(destination.parent)
        finally:
            try: temp.unlink()
            except OSError: pass
    _write_journal(root, cycle_id, directory_bundles=[
        {key: value for key, value in row.items() if key != "previous_descriptor_digest"}
        for row in entries])
    facts = [(row["locator"], tree.descriptor_digest, len(tree.descriptor))
             for tree, row, _destination, _existing in targets]
    metadata = {}
    for tree, row, _destination, _existing in targets:
        metadata[row["locator"]] = {
            "type": "directory-bundle", "media_type": artifact_directory_bundle.MEDIA_TYPE,
            "title": tree.directory[len("artifacts/"):],
        }
    return facts, metadata


def _is_support_locator(rel: str) -> bool:
    """A path through a CORE §3 support name that is not a cycle bucket (`_internal/`, `shards/`)."""
    return any(part in SUPPORT_SEGMENTS for part in rel.split("/")[1:])


def _document_name(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


_STATUS_RANK = {_document_name(name): rank for rank, name in enumerate(STATUS_DOCUMENT_NAMES)}


def _is_record_locator(rel: str) -> bool:
    """A file inside a work-record or copy folder (`dev_logs/`, `evidence/`, `_internal/`, ...)."""
    return any(part in RECORD_SEGMENTS for part in rel.split("/")[1:-1])


def _status_rank(rel: str) -> Optional[int]:
    """The status document's place in `STATUS_DOCUMENT_NAMES`, only directly under `artifacts/`."""
    root, _, name = rel.partition("/")
    return _STATUS_RANK.get(_document_name(name)) if root == "artifacts" and "/" not in name else None


def artifact_audience(rel: str) -> Optional[str]:
    """`user` for a reader-facing document of the cycle output, None for everything else.

    A report, summary or status document by name, or a PRD at the top of the cycle or of
    a spec component, outside any work-record or copy folder."""
    if _payload_root(rel) != "artifacts" or _is_record_locator(rel):
        return None
    parts = rel.split("/")[1:]
    name = _document_name(parts[-1])
    if name in USER_DOCUMENT_NAMES or (
            name == "prd.md" and (len(parts) <= 2 or (len(parts) == 3 and parts[0] == "spec"))):
        return artifact_manifest.USER_AUDIENCE
    return None


def _with_audience(row: Dict[str, Any], rel: str) -> Dict[str, Any]:
    """Add the optional `audience` to a non-support artifact row; other rows stay unchanged."""
    audience = artifact_audience(rel) if row.get("role") != "support" else None
    if audience is not None:
        row["audience"] = audience
    return row


def _output_placements(root: Path, record: Mapping[str, Any]) -> List[Dict[str, str]]:
    path = producer_dir(root) / "bucket-placements" / f"{record['cycle_id']}.json"
    rows = (_read_json(path) or {}).get("moves", [])
    return [row for row in rows if isinstance(row, dict) and all(
        isinstance(row.get(key), str) and row[key].startswith("artifacts/")
        and not any(part in {"", ".", ".."} for part in row[key].split("/"))
        for key in ("from", "to"))]


def resolve_placed_output(path: Path) -> Path:
    """Follow this cycle's recorded move without changing sealed evidence bytes."""
    path = Path(path)
    if not path.is_absolute() or os.path.lexists(path):
        return path
    for output in path.parents:
        if output.name != "artifacts":
            continue
        directory = output.parent
        binding = _read_json(directory / ".cycle.json") or {}
        if not isinstance(binding, dict):
            continue
        cid = binding.get("cycle_id", "")
        if not artifact_identity.is_well_formed(cid, "cycle"):
            continue
        for root in list(directory.parents)[:4]:
            record = read_cycle_record(root, cid)
            if record is None:
                continue
            if cycle_dir(root, record["campaign_id"], cid, record) != directory:
                continue
            relative = path.relative_to(directory).as_posix()
            placed = _placed_locator(relative, _output_placements(root, record))
            candidate = directory / placed
            if candidate.resolve().is_relative_to(output.resolve()) and candidate.exists():
                return candidate
    return path


def placed_output_proof(path: Path, *, route_id: str, route_hash: str) -> Optional[Dict[str, str]]:
    """Read a route-bound move and current manifest digest for terminal repair.

    The manifest digest describes the file *now*. Neither it nor the placement
    ledger claims to know the file's bytes at the worker's earlier handoff.
    """
    origin = Path(path)
    if not origin.is_absolute() or os.path.lexists(origin):
        return None
    for output in origin.parents:
        if output.name != "artifacts":
            continue
        directory = output.parent
        binding = _read_json(directory / ".cycle.json") or {}
        if not isinstance(binding, dict):
            continue
        cid = binding.get("cycle_id", "")
        if not artifact_identity.is_well_formed(cid, "cycle"):
            continue
        for root in list(directory.parents)[:4]:
            record = read_cycle_record(root, cid)
            if (record is None or record.get("route_id") != route_id
                    or record.get("route_hash") != route_hash
                    or record.get("campaign_id") != binding.get("campaign_id")
                    or cycle_dir(root, record["campaign_id"], cid, record) != directory):
                continue
            placement_path = producer_dir(root) / "bucket-placements" / f"{cid}.json"
            manifest_path = producer_dir(root) / "open-manifests" / f"{cid}.json"
            try:
                placement_bytes = placement_path.read_bytes()
                manifest_bytes = manifest_path.read_bytes()
                manifest = json.loads(manifest_bytes)
            except (OSError, ValueError):
                continue
            if not isinstance(manifest, dict):
                continue
            checkpoint = _read_json(checkpoint_state_path(root, cid)) or {}
            if not isinstance(checkpoint, dict):
                continue
            if (checkpoint.get("manifest_id") != manifest.get("manifest_id")
                    or checkpoint.get("manifest_revision_id") != manifest.get("manifest_revision_id")
                    or checkpoint.get("route_id") != route_id):
                continue
            relative = origin.relative_to(directory).as_posix()
            try:
                moves = _output_placements(root, record)
            except (AttributeError, TypeError):
                continue
            applicable = [row for row in moves if relative == row["from"]
                          or relative.startswith(row["from"] + "/")]
            if len(applicable) != 1:
                continue
            placed = _placed_locator(relative, moves)
            if placed == relative or not placed:
                continue
            target = directory / placed
            try:
                canonical_output = output.resolve()
                canonical_target = target.resolve(strict=True)
                canonical_target.relative_to(canonical_output)
                if canonical_target != target or not target.is_file() or not os.access(target, os.R_OK):
                    continue
                content = target.read_bytes()
            except (OSError, ValueError):
                continue
            digest = "sha256:" + hashlib.sha256(content).hexdigest()
            revisions = [row for row in (manifest.get("artifact_revisions") or [])
                         if isinstance(row, dict)
                         and isinstance(row.get("locator"), dict)
                         and row["locator"].get("path") == placed
                         and row.get("content_digest") == digest
                         and row.get("byte_size") == len(content)
                         and isinstance(row.get("provenance"), dict)
                         and row["provenance"].get("producer_route_id") == route_id]
            routes = [row for row in (manifest.get("routes") or [])
                      if isinstance(row, dict) and row.get("route_id") == route_id
                      and row.get("route_hash") == route_hash]
            cycle = manifest.get("cycle")
            if (not isinstance(cycle, dict)
                    or cycle.get("cycle_id") != cid
                    or cycle.get("campaign_id") != record["campaign_id"]
                    or manifest.get("manifest_id") is None
                    or not manifest.get("manifest_revision_id")
                    or len(revisions) != 1 or len(routes) != 1):
                continue
            return {
                "origin": str(origin), "destination": str(target),
                "cycle_id": cid, "manifest_revision_id": manifest["manifest_revision_id"],
                "artifact_revision_id": revisions[0]["artifact_revision_id"],
                "current_content_digest": digest,
                "placement_sha256": hashlib.sha256(placement_bytes).hexdigest(),
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            }
    return None


def _placed_locator(locator: Optional[str], moved: Sequence[Mapping[str, str]]) -> Optional[str]:
    if not locator:
        return locator
    relative = locator if locator.startswith("artifacts/") else "artifacts/" + locator
    for row in moved:
        if relative == row["from"] or relative.startswith(row["from"] + "/"):
            return row["to"] + relative[len(row["from"]):]
    return locator


def official_spec_primary(route: Mapping[str, Any], names: Sequence[str],
                          preferred: Optional[str] = None) -> Optional[str]:
    """An actual root/scoped component PRD, never a snapshot or terminal report."""
    if route.get("capability") != "autopilot-spec":
        return None
    components = spec_scope_components(
        scope for node in route.get("nodes", []) for scope in node.get("write_scope", []))
    available = set(names)
    candidates = []
    if not components:
        candidates.append("artifacts/spec/prd.md")
    if components is None:
        components = tuple(sorted({Path(name).parts[2] for name in names
            if len(Path(name).parts) == 4 and name.startswith("artifacts/spec/")
            and name.endswith("/prd.md") and Path(name).parts[2] != "_internal"}))
    candidates.extend(f"artifacts/spec/{component}/prd.md" for component in components)
    actual = [name for name in candidates if name in available]
    return preferred if preferred in actual else next(iter(actual), None)


def official_spec_primary_path(root: Path, record: Mapping[str, Any], route: Mapping[str, Any],
                               preferred: Optional[Path] = None) -> Optional[Path]:
    """Read the known canonical paths in this exact cycle; no recursive report search."""
    if route.get("capability") != "autopilot-spec":
        return None
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record).resolve()
    directory.relative_to(Path(root).resolve())
    spec = directory / "artifacts/spec"
    components = spec_scope_components(
        scope for node in route.get("nodes", []) for scope in node.get("write_scope", []))
    paths = [spec / "prd.md"]
    if components is None:
        if spec.is_dir() and not spec.is_symlink():
            paths.extend(p / "prd.md" for p in spec.iterdir() if p.is_dir() and not p.is_symlink()
                         and p.name != "_internal")
    else:
        paths.extend(spec / component / "prd.md" for component in components)
    names = []
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                continue
            path.resolve(strict=True).relative_to(directory / "artifacts")
            names.append(path.relative_to(directory).as_posix())
        except (OSError, ValueError):
            continue
    try:
        wanted = preferred.relative_to(directory).as_posix() if preferred is not None else None
    except ValueError:
        wanted = None
    selected = official_spec_primary(route, names, wanted)
    return directory / selected if selected is not None else None


def _choose_primary(rows: Sequence[Tuple[str, bytes]], primary: Optional[str],
                    support: Sequence[str] = (), route: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    # A `support` row is attached evidence, not this cycle's output, so it is never
    # auto-nominated as the primary artifact -- an explicit `primary` still wins.
    # Support-material paths are skipped the same way while any durable output
    # exists; a cycle holding nothing else keeps its first row so a completed
    # cycle still carries the primary role its outcome criterion requires.
    support_set = set(support)
    names = [rel for rel, _ in rows if rel not in support_set]
    if primary:
        candidate = primary if primary.startswith("artifacts/") else "artifacts/" + primary
        if candidate not in names:
            shown = ", ".join(names[:6]) + (", ..." if len(names) > 6 else "")
            raise ProducerError(
                "primary-artifact-missing",
                f"{primary} (expected a cycle-relative path under artifacts/; cycle outputs: {shown or 'none'})",
            )
        return candidate
    primary_names = [rel for rel in names if not _is_cycle_root_support(rel)]
    if not primary_names:
        return None
    durable = [rel for rel in primary_names if not _is_support_locator(rel)] or primary_names
    official = official_spec_primary(route or {}, durable)
    if official is not None:
        return official
    status = sorted((_status_rank(rel), rel) for rel in durable if _status_rank(rel) is not None)
    if status:
        return status[0][1]
    # A copy under a work-record folder (`dev_logs/.../final_report.md`) never outranks the
    # same name outside one; the order is otherwise unchanged.
    durable = ([rel for rel in durable if not _is_record_locator(rel)]
               + [rel for rel in durable if _is_record_locator(rel)])
    for wanted in PRIMARY_CANDIDATES:
        for rel in durable:
            if rel.endswith("/" + wanted) or rel == "artifacts/" + wanted:
                return rel
    documents = [rel for rel in durable if Path(rel).suffix.lower() in {".md", ".html", ".htm"}]
    return next(iter(documents or durable), None)


def _shared_pin_reference_path(root: Path, kind: str, ref_id: str) -> Path:
    return _reference_path(root, kind, ref_id)


def _shared_pin_revision_path(root: Path, kind: str, ref_id: str, rrev_id: str) -> Path:
    return Path(root) / "shared" / kind / ref_id / "revisions" / rrev_id / "revision.json"


def _resolve_shared_pin(
    root: Path, pin: Mapping[str, Any], provenance_fn: Optional[Any] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """D-78-a: resolve one `shared_reference_pins[]` entry from disk.

    Returns (shared_reference_row, shared_reference_revision_row). Raises a
    typed `ProducerError` when the pin is malformed or does not resolve --
    the finalize caller must never silently drop a pin (D-78-a: unresolved or
    digest-mismatched pins hold the seal, they do not fall back to `[]`).
    """
    if not isinstance(pin, Mapping):
        raise ProducerError("shared-reference-pin-invalid", "pin-not-an-object")
    kind = pin.get("kind")
    ref_id = pin.get("shared_reference_id")
    rrev_id = pin.get("shared_reference_revision_id")
    expected_digest = pin.get("content_digest")
    if kind not in SHARED_KINDS:
        raise ProducerError("shared-reference-pin-invalid", f"kind:{kind}")
    if not isinstance(ref_id, str) or not artifact_identity.is_well_formed(ref_id, "shared_reference"):
        raise ProducerError("shared-reference-pin-invalid", f"shared_reference_id:{ref_id}")
    if not isinstance(rrev_id, str) or not artifact_identity.is_well_formed(rrev_id, "shared_reference_revision"):
        raise ProducerError("shared-reference-pin-invalid", f"shared_reference_revision_id:{rrev_id}")
    if expected_digest is not None and not isinstance(expected_digest, str):
        raise ProducerError("shared-reference-pin-invalid", "content_digest")
    reference = _read_json(_shared_pin_reference_path(root, kind, ref_id))
    if reference is None:
        raise ProducerError("shared-reference-pin-unresolved", f"reference:{kind}:{ref_id}")
    revision = _read_json(_shared_pin_revision_path(root, kind, ref_id, rrev_id))
    if revision is None:
        raise ProducerError("shared-reference-pin-unresolved", f"revision:{kind}:{ref_id}:{rrev_id}")
    content_digest = revision.get("content_digest")
    if not isinstance(content_digest, str):
        raise ProducerError("shared-reference-pin-unresolved", f"revision-digest:{kind}:{ref_id}:{rrev_id}")
    if expected_digest is not None and expected_digest != content_digest:
        raise ProducerError("shared-reference-pin-digest-mismatch", f"{ref_id}:{rrev_id}")
    ref_row = {
        "shared_reference_id": ref_id, "kind": reference.get("kind"),
        "title": str(reference.get("title") or ""),
    }
    rev_row: Dict[str, Any] = {
        "shared_reference_revision_id": rrev_id, "shared_reference_id": ref_id,
        "content_digest": content_digest, "updated_at": revision.get("created_on"),
    }
    if provenance_fn is not None:
        rev_row["provenance"] = provenance_fn(content_digest)
    return ref_row, rev_row


def validate_shared_reference_pins(root: Path, pins: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Pure D-78-a validation (no manifest, no provenance) -- R2 calls this
    before finalize so an unresolved pin holds early. Returns a list of
    `{"index", "code", "detail"}` violation rows; empty means every pin
    resolves."""
    root = Path(root).resolve()
    violations: List[Dict[str, Any]] = []
    for i, pin in enumerate(pins):
        try:
            _resolve_shared_pin(root, pin)
        except ProducerError as exc:
            violations.append({"index": i, "code": exc.code, "detail": exc.detail})
    return violations


def _cycle_relative_primary(primary: Optional[str], directory: Path) -> Optional[str]:
    """Map an absolute `--primary` that points inside this cycle's `artifacts/`
    onto the cycle-relative form `_choose_primary` expects. Anything else is
    returned unchanged so the existing `primary-artifact-missing` verdict still
    names what the caller passed (2026-09-16 DX report: an absolute path failed
    with no hint that only cycle-relative locators are accepted)."""
    if not primary or not os.path.isabs(primary):
        return primary
    try:
        rel = Path(primary).resolve().relative_to(Path(directory).resolve())
    except (OSError, ValueError):
        return primary
    rel_posix = rel.as_posix()
    return rel_posix if rel_posix.startswith("artifacts/") else primary


def build_manifest(
    root: Path,
    record: Mapping[str, Any],
    route: Mapping[str, Any],
    rows: Sequence[Tuple[str, bytes]],
    *,
    state: str,
    primary: Optional[str],
    allow_open_route: bool,
    allocator: artifact_identity.IdAllocator,
    now: Optional[float],
    abandon_reason: Optional[str] = None,
    support_locators: Sequence[str] = (),
    reserved: Optional["InterimReservation"] = None,
    interim: bool = False,
    facts: Optional[Sequence[Tuple[str, str, int]]] = None,
    bundle_metadata: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> Dict[str, Any]:
    """Build the D-6 cycle document.

    `facts` rows are `(locator, content_digest, byte_size)`; when absent they are
    computed from `rows`.  `reserved` carries the IDs an open-cycle checkpoint
    already published, so the sealed document keeps them.  `interim` builds that
    checkpoint document itself: `cycle.state` is `open`, with no cycle or route
    terminal event and no route-closure requirement.
    """
    identity = artifact_lifecycle.read_root_identity(root)
    if identity is None:
        raise ProducerError("root-identity-missing")
    campaign = read_campaign(root, record["campaign_id"])
    if campaign is None:
        raise ProducerError("campaign-unknown", record["campaign_id"])
    if facts is None:
        facts = [(rel, _digest(data), len(data)) for rel, data in rows]
    man_id = (reserved.manifest_id if reserved is not None and reserved.manifest_id
              else allocator.allocate("manifest"))
    mrev_id = allocator.allocate("manifest_revision")
    when = _rfc3339(now)
    # `support_locators` are `artifacts/`-relative locators the caller marks as
    # attached evidence rather than cycle output (W7G D-79 relocates lump-external
    # loose files into a cycle this way). Empty by default, so an ordinary cycle's
    # manifest bytes are unchanged.
    support_rels = {"artifacts/" + rel.lstrip("/") for rel in support_locators}
    primary_rel = _choose_primary([(rel, None) for rel, _d, _s in facts], primary,
                                  support=support_rels, route=route)

    def provenance(digest: str, recorded_in: Optional[str] = None) -> Dict[str, Any]:
        return {
            "source_manifest_id": man_id, "source_revision_id": recorded_in or mrev_id,
            "producer_route_id": route["route_id"], "algorithm_version": ALGORITHM_VERSION,
            "schema_version": 1, "source_digest": digest,
        }

    artifacts: List[Dict[str, Any]] = []
    revisions: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    for rel, digest, byte_size in facts:
        # A path the open-cycle checkpoint already published keeps its artifact
        # ID; its revision ID is kept only while the content is unchanged.
        kept = reserved.artifacts.get(rel) if reserved is not None else None
        art_id = kept.artifact_id if kept else allocator.allocate("artifact")
        # A revision already reserved for this content (current, or earlier and
        # returned to) keeps its ID, provenance and recording event, so its rows
        # are identical in every interim document and the sealed one.
        same = reserved.revision_for(rel, digest) if reserved is not None else None
        arev_id = same.artifact_revision_id if same else allocator.allocate("artifact_revision")
        revision_provenance = provenance(digest, same.recorded_in if same else None)
        inner = _payload_inner(rel)
        root_support = _is_cycle_root_support(rel)
        bundle = (bundle_metadata or {}).get(rel)
        artifacts.append(_with_audience({
            "artifact_id": art_id, "cycle_id": record["cycle_id"],
            "role": "support" if rel in support_rels or root_support else ("primary" if rel == primary_rel else "output"),
            "type": bundle.get("type") if bundle else ("file" if root_support else _bucket_type(inner)),
            "capability": record["capability"],
            "title": bundle.get("title", inner) if bundle else (rel if root_support else inner),
        }, rel))
        revisions.append({
            "artifact_revision_id": arev_id, "artifact_id": art_id, "revision_sequence": 1,
            "content_digest": digest, "byte_size": byte_size,
            "media_type": bundle.get("media_type") if bundle else _media_type(rel),
            "locator": {"kind": "cycle-relative", "path": rel}, "provenance": revision_provenance,
        })
        reuse_event = same is not None and same.event_id and same.stream_id and same.recorded_at
        events.append({
            "event_id": same.event_id if reuse_event else allocator.allocate("event"),
            "stream_id": same.stream_id if reuse_event else allocator.allocate("stream"),
            "stream_sequence": 1, "event_type": "artifact.revision.recorded", "target_id": art_id,
            "actor": {"kind": "producer", "id": record["producer_id"]},
            "recorded_at": same.recorded_at if reuse_event else when,
            "provenance": revision_provenance, "evidence_ids": [], "payload": {"locator": rel},
        })
    cycle_digest = _digest(_canonical([[rel, digest] for rel, digest, _size in facts]))
    routes_row = {
        "artifact_root_id": identity.artifact_root_id, "route_id": route["route_id"],
        "route_hash": route["route_hash"], "terminal_marker": "pending",
        "terminal_evidence_id": "",
    }
    closed = False if interim else route_is_closed(root, route)
    if not closed and not allow_open_route and not interim:
        raise ProducerError(
            "route-not-closed",
            f"{route['route_id']}: required order: complete -> close -> finalize -> admit-shared; "
            "complete the terminal node using verified cycle-local evidence, then close the route",
        )
    # D-6: a `completed` cycle must bind a route.terminal.recorded event, which
    # only exists once the route is closed.  Sealing an open route therefore
    # records a provisional `active` cycle (lineage committed, completion not
    # claimed); `abandoned` needs no terminal evidence.
    if interim:
        cycle_state = artifact_manifest.INTERIM_CYCLE_STATE
    elif state == "abandoned":
        cycle_state = "abandoned"
    elif closed:
        cycle_state = "completed"
    else:
        cycle_state = "active"
    if cycle_state not in ("active", artifact_manifest.INTERIM_CYCLE_STATE):
        payload = {"abandon_reason": abandon_reason} if cycle_state == "abandoned" else {}
        events.append({
            "event_id": allocator.allocate("event"), "stream_id": allocator.allocate("stream"),
            "stream_sequence": 1, "event_type": f"cycle.{cycle_state}", "target_id": record["cycle_id"],
            "actor": {"kind": "producer", "id": record["producer_id"]}, "recorded_at": when,
            "provenance": provenance(cycle_digest), "evidence_ids": [], "payload": payload,
        })
    if closed and cycle_state == "completed":
        terminal_event_id = allocator.allocate("event")
        events.append({
            "event_id": terminal_event_id, "stream_id": allocator.allocate("stream"),
            "stream_sequence": 1, "event_type": "route.terminal.recorded", "target_id": record["cycle_id"],
            "actor": {"kind": "system", "id": "capability-route"}, "recorded_at": when,
            "provenance": provenance(cycle_digest), "evidence_ids": [], "payload": {},
        })
        routes_row["terminal_evidence_id"] = terminal_event_id
    # D-78-a: pins are the sole source of shared_references[]/shared_reference_revisions[].
    # No pins => both stay `[]` and the manifest bytes are unchanged from before this feature.
    shared_references: List[Dict[str, Any]] = []
    shared_reference_revisions: List[Dict[str, Any]] = []
    seen_shared_reference_ids: set = set()
    for pin in record.get("shared_reference_pins") or []:
        ref_row, rev_row = _resolve_shared_pin(root, pin, provenance)
        if ref_row["shared_reference_id"] not in seen_shared_reference_ids:
            shared_references.append(ref_row)
            seen_shared_reference_ids.add(ref_row["shared_reference_id"])
        shared_reference_revisions.append(rev_row)
    document = {
        "schema_version": 2, "manifest_kind": "artifact.cycle",
        "manifest_id": man_id, "manifest_revision_id": mrev_id,
        "repository_id": identity.repository_id, "artifact_root_id": identity.artifact_root_id,
        "campaign": {
            "campaign_id": campaign["campaign_id"], "goal": str(campaign.get("goal", "")),
            "completion_criterion": {"statement": str((campaign.get("completion_criterion") or {}).get("statement", ""))},
            "title": str(campaign.get("title", "")), "state": str(campaign.get("state", "active")),
        },
        "cycle": {
            "cycle_id": record["cycle_id"], "campaign_id": campaign["campaign_id"],
            "parent_cycle_id": record.get("parent_cycle_id"),
            "started_on": record["started_on"], "input_digest": _digest(_canonical({
                # D-120 fixed-input boundary: the begin route's identity, not
                # the route sealing the cycle (`route`, which may be a
                # continuation's rebound lineage extension).
                "route_id": record["route_id"], "route_hash": record["route_hash"],
                "capability": record["capability"], "intensity": record["intensity"],
            })),
            "outcome_criterion": {"required_artifact_roles": ["primary"] if primary_rel is not None else [], "decision_required": False},
            "state": cycle_state,
        },
        "artifacts": artifacts, "artifact_revisions": revisions,
        "shared_references": shared_references, "shared_reference_revisions": shared_reference_revisions,
        "routes": [routes_row], "events": events,
        "producer": {
            "producer_id": record["producer_id"], "contract_version": artifact_manifest.CONTRACT_VERSION,
            "source_revision": f"{record['capability']}/{record['intensity']}/{ALGORITHM_VERSION}",
        },
    }
    if closed and cycle_state == "completed":
        # D-120: the route sealing this cycle (`route`, R) may be a
        # continuation the begin record's own `route_file` never names --
        # rebind the terminal evidence from R's own canonical file, not the
        # begin route's.
        binding, sealed_route = artifact_lifecycle.bind_existing_runtime_route(
            root, route_lineage.canonical_route_path(root, route["route_id"]),
            expected_root_id=identity.artifact_root_id
        )
        if sealed_route.get("route_hash") != route["route_hash"]:
            raise ProducerError("route-hash-drift", route["route_id"])
        try:
            document = artifact_lifecycle._derive_terminal_evidence(document, binding, sealed_route)
        except artifact_lifecycle.LifecycleError as exc:
            raise ProducerError(exc.code, exc.detail)
    return document


def spec_scope_components(scopes: Iterable[str]) -> Optional[Tuple[str, ...]]:
    """The components already named by a spec write scope; None means the whole tree."""
    selected = set()
    for scope in scopes:
        parts = str(scope).split("/")
        if parts[0] != "spec":
            continue
        if (len(parts) < 3 or any(c in parts[1] for c in "*?[")
                or parts[1] in {"_internal", "<component>"}):
            return None
        selected.add(parts[1])
    return tuple(sorted(selected))
