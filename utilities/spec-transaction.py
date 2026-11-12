#!/usr/bin/env python3
"""Hold the canonical spec lock across one route-declared transaction."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
READABILITY_PATH=ROOT/"utilities/prd_readability.py"
SPEC=importlib.util.spec_from_file_location("worker_route_guard",ROOT/"utilities/worker-route-guard.py")
GUARD=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(GUARD)
sys.path.insert(0,str(ROOT/"utilities"))
import artifact_producer as PRODUCER  # noqa: E402


def emit(event, events=None):
    line=json.dumps(event,sort_keys=True)
    print(line,flush=True)
    if events:
        with open(events,"a",encoding="utf-8") as fh: fh.write(line+"\n")


def readability_event(prd: Path, preimage: bytes | None, postimage: bytes, route_id: str, version: int) -> tuple[dict, str | None]:
    """Warn-only PRD readability receipt. Never raises; the write result is not its business."""
    base={"status":"readability","route_id":route_id,"version":version,"path":str(prd)}
    try:
        spec=importlib.util.spec_from_file_location("prd_readability",READABILITY_PATH)
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        report=module.check_bytes(postimage,preimage)
        return {**base,**report},module.stderr_line(report)
    except Exception as exc:
        return {**base,"error":f"{type(exc).__name__}: {exc}"[:200]},f"prd-readability: check skipped ({type(exc).__name__})"


def select_seed_reference(artifact: Path, receipt: dict | None, *, reference_id: str | None = None,
                          key: str | None = None):
    """Resolve a shared spec once; an existing receipt owns its original base."""
    references=PRODUCER.list_references(artifact,"spec")
    by_id={row["shared_reference_id"]:row for row in references}
    if receipt is not None:
        pinned=receipt.get("reference_id")
        if reference_id is not None and reference_id!=pinned:
            raise PRODUCER.ProducerError("shared-base-reference-mismatch",reference_id)
        if pinned is None:
            if key is not None:
                raise PRODUCER.ProducerError("shared-base-reference-mismatch",key)
            return None
        selected=by_id.get(pinned)
        if selected is None:
            raise PRODUCER.ProducerError("reference-unknown",str(pinned))
        if key is not None and selected.get("key")!=key:
            raise PRODUCER.ProducerError("shared-base-reference-mismatch",key)
        return selected
    if reference_id is not None:
        selected=by_id.get(reference_id)
        if selected is None:
            raise PRODUCER.ProducerError("reference-unknown",reference_id)
        if key is not None and selected.get("key")!=key:
            raise PRODUCER.ProducerError("shared-reference-selector-conflict",key)
        return selected
    if key is not None:
        matching=[row for row in references if row.get("key")==key]
        if not matching:
            raise PRODUCER.ProducerError("shared-reference-exists",key)
        if len(matching)>1:
            raise PRODUCER.ProducerError("shared-reference-ambiguous",key)
        return matching[0]
    canonical=[row for row in references if row.get("key")=="spec"]
    if len(canonical)==1:
        return canonical[0]
    if len(references)==1:
        return references[0]
    if references:
        raise PRODUCER.ProducerError("shared-reference-ambiguous","seed requires one canonical spec reference")
    return None


def revision_history(artifact, revision, *, excluded=(), components=None):
    """Only missing history bytes; never reread obsolete current PRDs."""
    from artifact_spec_storage import revision_address, read_inventory, read_files, StorageError
    def wanted(name):
        parts = Path(name).parts
        return (name not in excluded and len(parts) >= 4 and parts[-4] == '_internal'
            and parts[-3] == 'versions' and re.fullmatch(r'v[0-9]+', parts[-2])
            and parts[-1] == 'prd.md' and (components is None or parts[0] in components))
    address = revision_address(artifact, revision)
    if address is not None:
        try:
            record = read_inventory(artifact, address[0], address[1])
            paths = [row['path'] for row in record['files'] if wanted(row['path'])]
            if not paths: return {}
            files, _ = read_files(artifact, address[0], address[1], paths=paths)
            return {name: file.data for name, file in files.items()}
        except StorageError as error:
            raise PRODUCER.ProducerError(error.code, str(error)) from error
    return {src.relative_to(revision).as_posix(): src.read_bytes()
            for src in sorted(revision.rglob('prd.md'))
            if wanted(src.relative_to(revision).as_posix()) and src.is_file() and not src.is_symlink()}


def seed_component_spec(spec_base, artifact, reference, receipt, components):
    """Prepare only the selected components, continuing the original base on retry."""
    receipt_path = spec_base/PRODUCER.SPEC_BASE_RECEIPT
    ref_id = (reference or {}).get("shared_reference_id")
    base = PRODUCER._spec_admission_base(artifact, spec_base, ref_id, None) if receipt else (reference or {}).get("latest_revision_id")
    if base and base not in (reference or {}).get("revisions", []):
        raise PRODUCER.ProducerError("shared-base-invalid", str(base))
    tree, record = PRODUCER._verified_shared_spec(artifact, ref_id, base) if base else ({}, {})
    if receipt is not None and receipt.get("components") is None:
        components = None  # a completed whole-tree upgrade cannot narrow again
    states = (receipt or {}).get("component_seeds", {})
    if not isinstance(states, dict):
        raise PRODUCER.ProducerError("shared-base-invalid", "component seed")
    done = dict(states)
    recorded = (receipt or {}).get("components")
    if receipt is not None and (
            not isinstance(receipt.get("seed_complete"), bool)
            or (receipt["seed_complete"] and not all(done.values()))
            or (recorded is not None and (not isinstance(recorded, list)
                or not recorded or any(not isinstance(c, str) for c in recorded)
                or recorded != sorted(set(recorded)) or set(recorded) != set(done)))):
        raise PRODUCER.ProducerError("shared-base-invalid", "component seed")
    if any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", k)
           or not isinstance(v, bool) for k, v in done.items()):
        raise PRODUCER.ProducerError("shared-base-invalid", "component seed")
    # A later whole-tree transaction prepares the remainder without resurrecting
    # an edit or deletion in a component whose first seed already completed.
    wanted = set(components) if components is not None else PRODUCER.component_set(tree) - {"revision.json"}
    pending = wanted - {c for c, complete in done.items() if complete}
    seed_tree = dict(tree)
    history_paths = set()
    if base:
        revisions = artifact/"shared/spec"/ref_id/"revisions"
        for other in sorted(revisions.iterdir()):
            if not other.is_dir() or other.name == base:
                continue
            for name, data in revision_history(artifact, other, excluded=seed_tree, components=pending).items():
                rel = Path(name)
                parts = rel.parts
                if (len(parts) < 4 or parts[0] not in pending or parts[-4] != "_internal"
                        or parts[-3] != "versions" or not re.fullmatch(r"v[0-9]+", parts[-2])):
                    continue
                if parts[-1] == 'prd.md' and name not in seed_tree:
                    seed_tree[name] = data
                    history_paths.add(rel.as_posix())
    for path in spec_base.rglob("*"):
        rel = path.relative_to(spec_base).as_posix()
        if rel == PRODUCER.SPEC_BASE_RECEIPT or "_internal/research/" in rel:
            continue
        if path.is_symlink():
            raise PRODUCER.ProducerError("shared-base-unproven", rel)
        if not path.is_file() or done.get(rel.split("/", 1)[0]) is True:
            continue
        if rel not in seed_tree or path.read_bytes() != seed_tree[rel]:
            raise PRODUCER.ProducerError("shared-seed-state-unproven" if receipt else "shared-base-unproven", rel)
    scoped = sorted(set((receipt or {}).get("components") or []) | wanted) if components is not None else None
    for c in pending:
        done[c] = False
    receipt = {"schema_version": 1, "reference_id": ref_id, "revision_id": base,
               "content_digest": record.get("content_digest"), "components": scoped,
               "component_seeds": done, "seed_complete": not pending}
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    PRODUCER._write_atomic(receipt_path, PRODUCER._json_bytes(receipt))
    copied = history = 0
    for rel, data in sorted(seed_tree.items()):
        if rel == PRODUCER.SPEC_BASE_RECEIPT or rel.split("/", 1)[0] not in pending:
            continue
        dst = spec_base/rel
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            PRODUCER._write_atomic(dst, data)
            if rel in history_paths:
                history += 1
            else:
                copied += 1
    for c in pending:
        done[c] = True
    receipt.update(component_seeds=done, seed_complete=True)
    PRODUCER._write_atomic(receipt_path, PRODUCER._json_bytes(receipt))
    return {"status": "seeded" if copied or history else "seed-skipped", "components": scoped,
            "files": copied, "history_versions": history, "spec_base": str(spec_base), "base_revision_id": base}



def _seed_cycle_spec(spec_base: Path, artifact: Path, spec_root: Path | None = None,
                     *, reference_id: str | None = None, key: str | None = None,
                     components: tuple[str, ...] | None = None):
    """W7C cycle layout: a fresh cycle's `artifacts/spec` is empty, so the
    transaction would see no pre-image and write no `_internal/versions/vN`
    snapshot -- which is why operators copied the previous version by hand
    (cairn v169/v170, defect K). Seed the whole latest shared/spec revision
    (every component, D-87) plus the `_internal/versions/v*` history of every
    earlier revision of that reference, so the pre-image, the version counter
    and the snapshot all come from the tool. Returns an event dict."""
    # SD-OPEN-50 (H1): the seed predicate is "no prd.md", not "no file". A
    # transactional standard+ owner's worker writes `_internal/research/**`
    # under the route's spec write scope BEFORE the transaction runs, so the
    # old any-file test skipped the seed on a bucket that held research notes
    # and no PRD: no pre-image, `next_version=1`, no snapshot (cairn v173,
    # 2026-09-07 -- the owner worked around it by mv -> staging -> restore).
    # Research residue is preserved. Other existing files require a recorded
    # base or an exact match to the canonical seed; seeding never overwrites.
    # `prd_present` is judged where the transaction writes (`spec_root`: the
    # component subtree for a scoped transaction, else the bucket top), and it
    # is an event label, not an early return: the copy loop always runs and
    # `kept_existing` keeps it idempotent, so a half-seeded bucket (crash
    # mid-seed) or a bucket where only another component holds a prd.md is
    # completed on the next run (review finding 6).
    scope=spec_root if spec_root is not None else spec_base
    has_prd=(scope/"prd.md").is_file() or (spec_root is None and any(p.name=="prd.md" and p.parent.parent==spec_base for p in spec_base.rglob("prd.md")))
    preexisting=sum(1 for p in spec_base.rglob("*") if p.is_file()) if spec_base.is_dir() else 0
    receipt_path=spec_base/PRODUCER.SPEC_BASE_RECEIPT
    receipt=PRODUCER._read_json(receipt_path)
    if receipt is None and os.path.lexists(receipt_path):
        raise PRODUCER.ProducerError("shared-base-invalid",PRODUCER.SPEC_BASE_RECEIPT)
    if receipt is not None and not isinstance(receipt,dict):
        raise PRODUCER.ProducerError("shared-base-invalid",PRODUCER.SPEC_BASE_RECEIPT)
    reference=select_seed_reference(artifact,receipt,reference_id=reference_id,key=key)
    if components is None and spec_root is not None:
        rel = spec_root.relative_to(spec_base)
        components = (rel.parts[0],) if rel.parts else None
    if (receipt is None and components is not None) or (receipt is not None and "components" in receipt):
        return seed_component_spec(spec_base, artifact, reference, receipt, components)
    latest=(reference or {}).get("latest_revision_id")
    ref_id=(reference or {}).get("shared_reference_id")
    revision=artifact/"shared"/"spec"/ref_id/"revisions"/latest if ref_id and latest else None
    original_tree = {}
    if receipt is not None:
        base=PRODUCER._spec_admission_base(artifact,spec_base,ref_id,None)
        # The receipt is the original input, not a pin to today's pointer.
        # A completed seed must never resurrect a candidate's intentional deletion.
        if reference and latest not in reference.get("revisions", []):
            raise PRODUCER.ProducerError("shared-base-invalid",str(latest))
        revision=artifact/"shared/spec"/ref_id/"revisions"/base if ref_id and base else None
        original_tree=(PRODUCER._verified_shared_spec(artifact,ref_id,base)[0] if revision else {})
        if receipt.get("seed_complete") is True:
            return {"status":"seed-skipped","reason":"prd-present" if has_prd else "seed-complete",
                    "prd_present":has_prd,"preexisting_files":preexisting,"spec_base":str(spec_base),
                    "kept_existing":max(0,preexisting-1),"kept_existing_paths":[],
                    "base_revision_id":base,"latest_revision_id":latest}
        if "seed_complete" not in receipt:
            # Legacy receipts do not distinguish half-copy from later deletion.
            # A complete old tree can be adopted without copying any bytes.
            missing=[p for p in original_tree if p!=PRODUCER.SPEC_BASE_RECEIPT and not (spec_base/p).is_file()]
            if missing:
                raise PRODUCER.ProducerError("shared-seed-state-unproven",",".join(sorted(missing)))
            receipt["seed_complete"]=True
            PRODUCER._write_atomic(receipt_path,PRODUCER._json_bytes(receipt))
            return {"status":"seed-skipped","reason":"prd-present" if has_prd else "seed-complete",
                    "prd_present":has_prd,"preexisting_files":preexisting,"spec_base":str(spec_base),
                    "kept_existing":max(0,preexisting-1),"kept_existing_paths":[]}
        if receipt.get("seed_complete") is not False:
            raise PRODUCER.ProducerError("shared-base-invalid","seed_complete")
        for path in spec_base.rglob("*"):
            rel=path.relative_to(spec_base).as_posix()
            if path.is_symlink():
                raise PRODUCER.ProducerError("shared-base-unproven",rel)
            if not path.is_file() or rel==PRODUCER.SPEC_BASE_RECEIPT or "_internal/research/" in rel:
                continue
            if rel in original_tree and path.read_bytes()==original_tree[rel]:
                continue
            if "/_internal/versions/" in "/"+rel:
                continue
            raise PRODUCER.ProducerError("shared-seed-state-unproven",rel)
    else:
        if revision is not None:
            original_tree = PRODUCER._verified_shared_spec(artifact, ref_id, latest)[0]
        # Only known unchanged seed files and explicit worker research can
        # predate the receipt. Unknown/deleted old files must not be resurrected.
        for path in spec_base.rglob("*"):
            rel=path.relative_to(spec_base)
            if path.is_symlink():
                raise PRODUCER.ProducerError("shared-base-unproven",str(rel))
            if not path.is_file() or "_internal/research/" in rel.as_posix():
                continue
            original=(original_tree.get(rel.as_posix()) if revision is not None else
                      (artifact/"spec"/rel).read_bytes() if (artifact/"spec"/rel).is_file() else None)
            if original is None or path.read_bytes()!=original:
                raise PRODUCER.ProducerError("shared-base-unproven",str(rel))
        metadata=PRODUCER._read_json(revision/PRODUCER.REVISION_RECORD_NAME) if revision else None
        if revision is not None and (metadata is None or metadata.get("shared_reference_revision_id")!=latest
                                     or metadata.get("shared_reference_id")!=ref_id):
            raise PRODUCER.ProducerError("shared-base-invalid",str(revision))
        receipt={"schema_version":1,"reference_id":ref_id,"revision_id":latest,
                 "content_digest":metadata["content_digest"] if metadata else None,"seed_complete":False}
        receipt_path.parent.mkdir(parents=True,exist_ok=True)
        PRODUCER._write_atomic(receipt_path,PRODUCER._json_bytes(receipt))
    if revision is None:
        receipt["seed_complete"]=True
        PRODUCER._write_atomic(receipt_path,PRODUCER._json_bytes(receipt))
        return {"status":"seed-skipped","reason":"prd-present" if has_prd else "no-shared-revision","prd_present":has_prd,"preexisting_files":preexisting,"spec_base":str(spec_base)}
    copied=0; kept=0; kept_paths=[]
    for name, data in sorted(original_tree.items()):
        rel=Path(name)
        if rel.as_posix() in {"revision.json",PRODUCER.SPEC_BASE_RECEIPT}:
            continue
        dst=spec_base/rel
        if dst.exists():
            # A worker copy of a file the shared revision also carries wins
            # silently only in count; the names ride on the event so an owner
            # can diff them before admit (review finding 8).
            kept+=1
            if len(kept_paths)<20: kept_paths.append(rel.as_posix())
            continue
        dst.parent.mkdir(parents=True,exist_ok=True)
        PRODUCER._write_atomic(dst, data); copied+=1
    history=0
    history_present = {path.relative_to(spec_base).as_posix() for path in spec_base.rglob('*') if path.is_file()}
    revisions_dir=revision.parent
    if revisions_dir.name=="revisions":
        for other in sorted(revisions_dir.iterdir()):
            if other==revision or not other.is_dir():
                continue
            for name, data in revision_history(artifact, other, excluded=history_present).items():
                rel=Path(name)
                parts=rel.parts
                # <component?>/_internal/versions/vN/prd.md -- immutable history rows only
                if len(parts)<4 or parts[-4]!="_internal" or parts[-3]!="versions" or not re.fullmatch(r"v[0-9]+",parts[-2]):
                    continue
                dst=spec_base/rel
                if dst.exists() or parts[-1] != 'prd.md':
                    continue
                dst.parent.mkdir(parents=True,exist_ok=True); PRODUCER._write_atomic(dst, data); history+=1
                history_present.add(name)
    receipt["seed_complete"]=True
    PRODUCER._write_atomic(receipt_path,PRODUCER._json_bytes(receipt))
    if has_prd and copied==0 and history==0:
        return {"status":"seed-skipped","reason":"prd-present","prd_present":True,"preexisting_files":preexisting,
                "kept_existing":kept,"kept_existing_paths":kept_paths,"source":str(revision),"spec_base":str(spec_base)}
    return {"status":"seeded","source":str(revision),"files":copied,"history_versions":history,"prd_present":has_prd,
            "preexisting_files":preexisting,"kept_existing":kept,"kept_existing_paths":kept_paths,"spec_base":str(spec_base)}


def _record_code_seed_pin(artifact: Path, spec_base: Path) -> None:
    """Retain the exact base already chosen by this normal seed operation."""
    receipt = PRODUCER._read_json(Path(spec_base) / PRODUCER.SPEC_BASE_RECEIPT)
    if not isinstance(receipt, dict) or receipt.get("seed_complete") is not True:
        return
    reference_id = receipt.get("reference_id")
    revision_id = receipt.get("revision_id")
    content_digest = receipt.get("content_digest")
    if reference_id is None and revision_id is None:
        if content_digest is not None:
            raise PRODUCER.ProducerError("shared-base-invalid", "seed revision digest")
        return
    if revision_id is None:
        if content_digest is not None:
            raise PRODUCER.ProducerError("shared-base-invalid", "seed revision digest")
        return
    if (not isinstance(reference_id, str) or not isinstance(revision_id, str)
            or not PRODUCER.artifact_identity.is_well_formed(reference_id, "shared_reference")
            or not PRODUCER.artifact_identity.is_well_formed(revision_id, "shared_reference_revision")
            or not isinstance(content_digest, str)):
        raise PRODUCER.ProducerError("shared-base-invalid", "seed reference lineage")

    cycle_dir = Path(spec_base).resolve().parent.parent
    binding = PRODUCER.artifact_locator.read_cycle_binding(cycle_dir)
    if binding is None:
        return
    record = PRODUCER.read_cycle_record(artifact, binding["cycle_id"])
    if (not isinstance(record, dict) or record.get("capability") != "autopilot-code"
            or record.get("state") != "open"):
        return

    pin = {"kind": "spec", "shared_reference_id": reference_id,
           "shared_reference_revision_id": revision_id, "content_digest": content_digest}
    PRODUCER._resolve_shared_pin(artifact, pin)
    reference = PRODUCER._read_json(
        Path(artifact) / "shared/spec" / reference_id / "reference.json")
    if (not isinstance(reference, dict) or reference.get("shared_reference_id") != reference_id
            or revision_id not in reference.get("revisions", [])):
        raise PRODUCER.ProducerError("shared-base-invalid", revision_id)
    pins = record.get("shared_reference_pins") or []
    if not isinstance(pins, list):
        raise PRODUCER.ProducerError("shared-reference-pin-invalid", "cycle pins")
    for existing in pins:
        if (isinstance(existing, dict) and existing.get("kind") == "spec"
                and existing.get("shared_reference_id") == reference_id
                and existing.get("shared_reference_revision_id") == revision_id):
            if existing.get("content_digest") not in (None, content_digest):
                raise PRODUCER.ProducerError("shared-reference-pin-digest-mismatch", revision_id)
            return
    record["shared_reference_pins"] = [*pins, pin]
    PRODUCER._write_cycle_record(artifact, record, exclusive=False)


def seed_cycle_spec(spec_base: Path, artifact: Path, spec_root: Path | None = None,
                    *, reference_id: str | None = None, key: str | None = None,
                    components: tuple[str, ...] | None = None):
    """Seed the route's spec tree and retain its exact shared base when present."""
    result = _seed_cycle_spec(spec_base, artifact, spec_root,
                              reference_id=reference_id, key=key, components=components)
    _record_code_seed_pin(artifact, spec_base)
    return result


def preseed_owner_cycle(artifact: Path, cycle_dir: Path, *, wait_timeout: float = 600, route=None):
    """Seed before research/review, outside producer admission's lock.

    The transaction also takes this lock before reading producer state. Begin
    must have released its admission lock before calling here, so neither
    path can hold the locks in opposite orders.
    """
    lock_path=artifact/".pipeline-lock"
    with lock_path.open("a+",encoding="utf-8") as lock:
        deadline=time.monotonic()+wait_timeout
        while True:
            try:
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic()>=deadline:
                    raise PRODUCER.ProducerError("spec-lock-timeout",str(lock_path))
                time.sleep(.05)
        components = PRODUCER.spec_scope_components(
            scope for node in (route or {}).get("nodes", []) for scope in node.get("write_scope", [])) if route else None
        return seed_cycle_spec(cycle_dir/"artifacts/spec", artifact, components=components)


def legacy_spec_state(artifact: Path):
    legacy=artifact/"spec"
    state={}
    if not legacy.is_dir():
        return state
    for path in legacy.rglob("*"):
        if path.is_file() and not path.is_symlink() and path.name!=".pipeline-lock":
            st=path.stat(); state[path.relative_to(legacy).as_posix()]=(st.st_size,st.st_mtime_ns)
    return state


class VersionChainError(Exception):
    """The v{N} chain could not be enumerated. A monotone counter must not guess low."""
    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}"); self.reason=reason; self.detail=detail


def versions_max(tree: Path) -> int:
    """Highest `_internal/versions/v{N}` directory under one spec tree (0 when none)."""
    # Physical payloads are sparse in a logical revision. Keep the counter on
    # the recorded history slots, not on its provider's directory names.
    from artifact_spec_storage import revision_address, read_inventory, StorageError
    for root in tree.parents:
        address = revision_address(root, tree)
        if address is not None:
            try:
                inventory = read_inventory(root, address[0], address[1])
            except StorageError as error:
                raise VersionChainError('version-chain-unenumerable', str(error)) from error
            prefix = (address[2] + '/' if address[2] else '') + '_internal/versions/'
            numbers = [int(match.group(1)) for name in (row['path'] for row in inventory['files']) if name.startswith(prefix)
                for match in [re.match(r'v([0-9]+)/', name[len(prefix):])] if match]
            return max(numbers, default=0)
    versions=tree/"_internal"/"versions"
    found=[]
    if versions.is_dir():
        for row in versions.iterdir():
            match=re.fullmatch(r"v([0-9]+)",row.name)
            if match and row.is_dir(): found.append(int(match.group(1)))
    return max(found,default=0)


def _cycle_spec_trees(artifact_root: Path) -> list[Path]:
    """Every cycle-held spec tree, by a plain walk that follows symlinks.

    This is deliberately NOT the reader's enumeration (`artifact_reader.cycle_bucket_dirs`):
    that one is a read surface -- identity-bound (campaign/cycle records via
    `artifact_locator.scan_index`, which raises on a malformed root) and real
    directories only. A monotone counter must see a superset of what any reader
    sees: a symlinked bucket or cycle directory, a cycle whose identity record
    is missing or half-written, and the W7H `--include-spec-top` residue home
    `artifacts/_internal/spec` (where a retired legacy `spec/` chain now lives)
    all still hold real v{N} snapshots whose numbers must never be reused.
    Missing a tree costs a silent reuse; over-counting costs one skipped number.
    """
    out: list[Path]=[]
    campaigns=artifact_root/"campaigns"
    if not campaigns.is_dir(): return out
    for camp in sorted(campaigns.iterdir()):
        if camp.name.startswith(".") or not camp.is_dir(): continue
        cycles=[c for c in camp.iterdir() if c.is_dir() and not c.name.startswith(".") and c.name!="cycles"]
        legacy_layer=camp/"cycles"   # pre-W7I layout (D-88 removed the layer; sealed roots may keep it)
        if legacy_layer.is_dir(): cycles.extend(c for c in legacy_layer.iterdir() if c.is_dir() and not c.name.startswith("."))
        for cyc in sorted(cycles):
            for rel in ("artifacts/spec","artifacts/_internal/spec"):
                tree=cyc/rel
                if tree.is_dir(): out.append(tree)
    return out


def version_history_trees(spec_root: Path, artifact_root: Path, component: str="") -> list[Path]:
    """Every tree this spec's `_internal/versions/v{N}` chain has lived in.

    The current spec root, the legacy read-only `spec/`, every cycle-held spec
    tree (`_cycle_spec_trees`: `artifacts/spec` of every cycle in both layouts,
    symlinks followed, plus the W7H residue home `artifacts/_internal/spec`),
    and every immutable `shared/spec/<ref>/revisions/<rrev>` of every
    reference. `component` narrows each tree to `<tree>/<component>` so a
    component spec stays on its own chain. Raises `VersionChainError` when the
    root cannot be walked; the caller must fail closed, never count low.
    """
    artifact_root=Path(artifact_root)
    def under(base: Path) -> Path: return base/component if component else base
    trees=[Path(spec_root),under(artifact_root/"spec")]
    try:
        trees.extend(under(tree) for tree in _cycle_spec_trees(artifact_root))
        shared=artifact_root/"shared"/"spec"
        if shared.is_dir():
            # Every reference, whatever its D-3-b lifecycle state (active /
            # superseded / retired) -- the immutable bytes of a superseded
            # reference are real v{N}s on this chain. Registered as an explicit
            # exception to the D-3-b exclusion list (artifact-path-contract
            # D-87-b); a future D-3-b filter must not reach this enumeration.
            for ref in sorted(shared.iterdir()):
                revisions=ref/"revisions"
                if not revisions.is_dir(): continue
                trees.extend(under(rev) for rev in sorted(revisions.iterdir()) if rev.is_dir())
    except OSError as exc:
        raise VersionChainError("version-chain-unenumerable",f"{type(exc).__name__}: {exc}") from exc
    seen=set(); unique=[]
    for tree in trees:
        key=str(tree)
        if key in seen: continue
        seen.add(key); unique.append(tree)
    return unique


def chain_next(trees: list[Path]) -> tuple[int, Path | None]:
    """`(max(N)+1, the first tree holding max(N))`; `(1, None)` on an empty chain.

    Raises `VersionChainError` when any tree's `_internal/versions` cannot be
    read: an unreadable tree may hold the maximum, so the counter must not
    answer at all (spec owner finding F1: this read used to sit outside the
    typed guard and after the seed).
    """
    best=0; source=None
    for tree in trees:
        try: found=versions_max(tree)
        except OSError as exc:
            raise VersionChainError("version-chain-unenumerable",f"{type(exc).__name__}: {exc}") from exc
        if found>best: best,source=found,tree
    return best+1,source


def version_source_layout(tree: Path | None, spec_root: Path, artifact_root: Path) -> str:
    if tree is None: return "none"
    tree=Path(tree); artifact_root=Path(artifact_root)
    if tree==Path(spec_root): return "spec-root"
    try: rel=tree.relative_to(artifact_root)
    except ValueError: return "outside-root"
    head=rel.parts[0] if rel.parts else ""
    return {"spec":"legacy","shared":"shared","campaigns":"cycle"}.get(head,"unknown")


def next_version(spec_root: Path, artifact_root: Path, component: str="") -> int:
    """The next v{N} on this spec's single canonical chain.

    While the cutover is active `spec_root` is the open cycle's `artifacts/spec`
    bucket. `seed_cycle_spec` copies the latest shared revision and the
    history of its sibling revisions into it, but that copy is the pre-image
    source, not the counter: a root whose chain lives only in the legacy
    `spec/` bucket (no shared revision yet), a sealed cycle that was never
    admitted, or a concurrent cycle that already snapshotted v{N} are all
    invisible to the seeded copy, and the counter restarted at 1 or reused a
    number. The number therefore comes from every tree the spec has lived in;
    the snapshot itself still lands under the current `spec_root`.
    """
    return chain_next(version_history_trees(spec_root,artifact_root,component))[0]


def read_regular_file(path: Path, *, allow_missing: bool) -> bytes | None:
    if not path.exists():
        if allow_missing:
            return None
        raise ValueError("missing")
    if path.is_symlink() or not path.is_file():
        raise ValueError("not-regular")
    return path.read_bytes()


def persist_snapshot(spec_root: Path, version: int, preimage: bytes) -> tuple[str, Path]:
    """Create the exact pre-image once, or verify an idempotent existing copy."""
    internal=spec_root/"_internal"
    versions=internal/"versions"
    for parent in (internal,versions):
        if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
            raise ValueError("snapshot-parent-not-directory")
        parent.mkdir(exist_ok=True)
    version_dir=versions/f"v{version}"
    if version_dir.exists() and (version_dir.is_symlink() or not version_dir.is_dir()):
        raise ValueError("snapshot-version-not-directory")
    version_dir.mkdir(exist_ok=True)
    snapshot=version_dir/"prd.md"
    if snapshot.exists():
        if snapshot.is_symlink() or not snapshot.is_file():
            raise ValueError("snapshot-not-regular")
        if snapshot.read_bytes()!=preimage:
            raise ValueError("snapshot-mismatch")
        return "matched",snapshot
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
    if hasattr(os,"O_NOFOLLOW"):
        flags|=os.O_NOFOLLOW
    fd=os.open(snapshot,flags,0o644)
    try:
        with os.fdopen(fd,"wb",closefd=True) as fh:
            fh.write(preimage); fh.flush(); os.fsync(fh.fileno())
    except Exception:
        snapshot.unlink(missing_ok=True)
        raise
    return "created",snapshot


def discard_unused_snapshot(spec_root: Path, version: int, snapshot: Path) -> None:
    snapshot.unlink(missing_ok=True)
    version_dir=spec_root/"_internal"/"versions"/f"v{version}"
    try:
        version_dir.rmdir()
        version_dir.parent.rmdir()
        version_dir.parent.parent.rmdir()
    except OSError:
        pass


def main():
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest="command",required=True); run=sub.add_parser("run")
    run.add_argument("--artifact-root",required=True); run.add_argument("--worktree",required=True)
    run.add_argument("--route",required=True); run.add_argument("--node",required=True)
    run.add_argument("--spec-root",help="component spec root under <artifact-root>/spec")
    run.add_argument("--reference",help="existing shared spec reference ID")
    run.add_argument("--key",help="existing shared spec reference key")
    run.add_argument("--wait-timeout",type=float,default=600); run.add_argument("--poll",type=float,default=.05)
    run.add_argument("--events")
    run.add_argument("--require-snapshot",action="store_true",help=argparse.SUPPRESS)
    run.add_argument("transaction",nargs=argparse.REMAINDER)
    raw_argv=list(sys.argv[1:])
    if raw_argv and raw_argv[0]=="run" and (os.environ.get("AGENT_ROUTE_FILE") or os.environ.get("AGENT_ROUTE_ID")):
        flags=("--artifact-root","--worktree","--route","--node")
        missing=[flag for flag in flags if not any(item==flag or item.startswith(flag+"=") for item in raw_argv)]
        if missing:
            explicit_root=None
            for index,item in enumerate(raw_argv):
                if item=="--artifact-root" and index+1<len(raw_argv): explicit_root=raw_argv[index+1]
                elif item.startswith("--artifact-root="): explicit_root=item.split("=",1)[1]
            try:
                context=__import__("artifact_context").resolve_context(
                    environ=os.environ,explicit={"artifact_root":explicit_root} if explicit_root else None)
            except ValueError as exc:
                emit({"status":"blocked","reason":getattr(exc,"code","artifact-context-invalid"),"detail":str(exc)},
                     next((raw_argv[i+1] for i,item in enumerate(raw_argv[:-1]) if item=="--events"),None))
                return 65
            if context.route and context.node and context.artifact_root:
                defaults={"--artifact-root":str(context.artifact_root),
                    "--worktree":str(context.source_worktree or ""),
                    "--route":str(context.artifact_root/".runtime"/"routes"/f"{context.route['route_id']}.json"),
                    "--node":str(context.node_id or "")}
                insertion=1
                for flag in flags:
                    if flag in missing and defaults[flag]:
                        raw_argv[insertion:insertion]=[flag,defaults[flag]]; insertion+=2
    args=parser.parse_args(raw_argv)
    command=args.transaction[1:] if args.transaction[:1]==["--"] else args.transaction
    if not command: parser.error("transaction command required after --")
    artifact=Path(args.artifact_root).resolve(); worktree=Path(args.worktree)
    promoted_env = {}
    # Direct has no cycle until it actually authors a shared contract. Keep
    # this promotion inside the existing spec command, with its normal route
    # and scope validation; spec_touch alone is only capacity.
    import artifact_journal
    try:
        route_for_spec, node_for_spec, _ = GUARD.validate_route_contract(args.route,args.node,worktree,artifact)
    except GUARD.WorkerRouteError as exc:
        emit({"status":"blocked","reason":exc.reason,"detail":str(exc),"route_id":exc.route_id,"route_file":args.route},args.events); return 65
    if (artifact_journal.target(artifact, route_for_spec["route_id"])
            and route_for_spec.get("spec_touch")
            and any(s == "spec" or s.startswith("spec/") for s in node_for_spec["write_scope"])):
        prepared = PRODUCER.begin(artifact, route_file=Path(args.route), capability=route_for_spec["capability"],
            intensity=route_for_spec["effective_intensity"], require_cycle=True)
        promoted_env = prepared.get("env") or {}
    import artifact_context
    context=None
    if promoted_env or os.environ.get("AGENT_ROUTE_FILE") or os.environ.get("AGENT_ROUTE_ID"):
        try:
            context=artifact_context.resolve_context(environ={**os.environ, **promoted_env},route=args.route,node_id=args.node,
                explicit={"artifact_root":artifact,"source_worktree":worktree})
        except ValueError as exc:
            emit({"status":"blocked","reason":getattr(exc,"code","artifact-context-invalid"),"detail":str(exc),"artifact_root":str(artifact)},args.events); return 65
    # W7C: the spec bucket lives under the open producer cycle once the
    # cutover is active (AGENT_ARTIFACT_CYCLE_DIR); the legacy top-level
    # `spec/` is only reachable during the compatibility window.
    if context and context.route and context.node:
        if not context.output_dir:
            emit({"status":"blocked","reason":"artifact-cycle-unbound","route_id":context.route.get("route_id")},args.events); return 65
        if not any(scope=="spec" or scope=="spec/**" or scope.startswith("spec/")
                   for scope in context.write_scope):
            emit({"status":"blocked","reason":"route-node-scope-mismatch","route_id":context.route.get("route_id")},args.events); return 65
        spec_base=context.output_dir/"spec"; spec_layout="cycle"
    else:
        try: spec_base,spec_layout=PRODUCER.resolve_output_dir(artifact,"spec")
        except PRODUCER.ProducerError as exc:
            emit({"status":"blocked","reason":exc.code,"detail":exc.detail,"artifact_root":str(artifact)},args.events); return 65
    spec_base=spec_base.resolve()
    spec_root=(Path(args.spec_root).expanduser() if args.spec_root else spec_base)
    # A relative component root is relative to the resolved spec bucket, which
    # is the open cycle's `artifacts/spec` once the cutover is active (W7D fix:
    # `artifact/<component>` pointed outside the cycle and was always blocked).
    if not spec_root.is_absolute(): spec_root=spec_base/spec_root
    spec_root=spec_root.resolve()
    try: component=spec_root.relative_to(spec_base).as_posix()
    except ValueError:
        emit({"status":"blocked","reason":"spec-root-outside-artifact","spec_root":str(spec_root),"spec_base":str(spec_base),"layout":spec_layout},args.events); return 65
    if component==".": component=""
    try: route,node,_=GUARD.validate_route_contract(args.route,args.node,worktree,artifact)
    except GUARD.WorkerRouteError as exc:
        emit({"status":"blocked","reason":exc.reason,"detail":str(exc),"route_id":exc.route_id,"route_file":args.route},args.events); return 65
    if not route.get("spec_touch"):
        emit({"status":"blocked","reason":"spec-touch-not-declared","route_id":route["route_id"],"route_file":args.route},args.events); return 65
    if not any((scope[:-3] if scope.endswith("/**") else scope)=="spec" or (scope[:-3] if scope.endswith("/**") else scope).startswith("spec/") for scope in node["write_scope"]):
        emit({"status":"blocked","reason":"route-node-scope-mismatch","route_id":route["route_id"],"node_id":node["id"]},args.events); return 65
    node_components = PRODUCER.spec_scope_components(node["write_scope"])
    if node_components is not None:
        if not component and len(node_components) == 1:
            component = node_components[0]
            spec_root = spec_base/component
        if not component or component.split("/", 1)[0] not in node_components:
            emit({"status":"blocked","reason":"route-node-scope-mismatch","route_id":route["route_id"],"node_id":node["id"]},args.events); return 65
    lock_path=artifact/".pipeline-lock"; lock_path.parent.mkdir(parents=True,exist_ok=True)
    with lock_path.open("a+",encoding="utf-8") as lock:
        blocked=False
        waited=False
        try: fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            blocked=True; waited=True; lock.seek(0); owner=lock.read().strip()
            emit({"status":"BLOCKED","action":"wait","route_id":route["route_id"],"owner":owner},args.events)
        deadline=time.monotonic()+max(0,args.wait_timeout)
        while blocked:
            try: fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB); blocked=False
            except BlockingIOError:
                if time.monotonic()>=deadline:
                    emit({"status":"blocked","reason":"spec-lock-timeout","route_id":route["route_id"]},args.events); return 3
                time.sleep(max(.01,args.poll))
        # Enumerate the v{N} chain and read every tree's versions BEFORE seeding:
        # a root that cannot be walked or read is a typed refusal that leaves no
        # seed residue behind, and the counter never guesses low (review round 1
        # blocking 1; owner finding F1). The seeded copy only carries history
        # the chain already counted (the latest shared revision and its
        # siblings), so the number is final before the seed runs.
        try: version,source=chain_next(version_history_trees(spec_root,artifact,component))
        except VersionChainError as exc:
            emit({"status":"blocked","reason":exc.reason,"detail":exc.detail,"route_id":route["route_id"],"spec_root":str(spec_root)},args.events)
            lock.seek(0); lock.truncate(); lock.flush(); os.fsync(lock.fileno())
            return 65
        if spec_layout=="cycle":
            # Seed only once the route contract passed and the spec lock is ours:
            # a refused run must leave no admittable content behind, and two
            # transactions on one cycle must not interleave a half copy.
            try:
                seeded=seed_cycle_spec(spec_base,artifact,spec_root if spec_root!=spec_base else None,
                                       reference_id=args.reference,key=args.key)
            except PRODUCER.ProducerError as exc:
                emit({"status":"blocked","reason":exc.code,"detail":exc.detail,"route_id":route["route_id"]},args.events)
                lock.seek(0); lock.truncate(); lock.flush(); os.fsync(lock.fileno())
                return 65
            emit({**seeded,"route_id":route["route_id"]},args.events)
        if spec_layout=="cycle" and version==1 and (spec_root/"prd.md").is_file():
            # A pre-image with no history in ANY tree of this spec's chain
            # (current root, cycle buckets, shared revisions, legacy): the
            # counter starts at 1 even though the PRD is not new. Say so.
            emit({"status":"version-history-absent","route_id":route["route_id"],"spec_root":str(spec_root),"detail":"prd.md present but no _internal/versions history in any tree of this spec's chain (cycle buckets, shared revisions, legacy spec/); the counter starts at 1"},args.events)
        owner={"route_id":route["route_id"],"node_id":node["id"],"worktree":str(worktree.resolve()),"pid":os.getpid(),"next_version":version}
        lock.seek(0); lock.truncate(); lock.write(json.dumps(owner,sort_keys=True)+"\n"); lock.flush(); os.fsync(lock.fileno())
        emit({"status":"acquired","action":"latest-reread","route_id":route["route_id"],"next_version":version,"waited":waited,"layout":spec_layout,"spec_root":str(spec_root),
              "version_source":(str(source) if source is not None else None),"version_source_layout":version_source_layout(source,spec_root,artifact)},args.events)
        prd=spec_root/"prd.md"
        try:
            preimage=read_regular_file(prd,allow_missing=True)
        except ValueError as exc:
            emit({"status":"blocked","reason":"prd-preimage-invalid","detail":str(exc),"route_id":route["route_id"]},args.events)
            lock.seek(0); lock.truncate(); lock.flush(); os.fsync(lock.fileno())
            return 65
        prepared_status="not-required-new"
        prepared_path=None
        if preimage is not None:
            try:
                prepared_status,prepared_path=persist_snapshot(spec_root,version,preimage)
            except (OSError,ValueError) as exc:
                reason="version-snapshot-mismatch" if str(exc)=="snapshot-mismatch" else "version-snapshot-invalid"
                emit({"status":"blocked","reason":reason,"detail":str(exc),"route_id":route["route_id"],"version":version},args.events)
                lock.seek(0); lock.truncate(); lock.flush(); os.fsync(lock.fileno())
                return 65
            emit({"status":"snapshot-prepared","snapshot":prepared_status,"route_id":route["route_id"],"version":version,"path":str(prepared_path),"preimage_sha256":hashlib.sha256(preimage).hexdigest()},args.events)
        env={**os.environ,**promoted_env,"AGENT_SPEC_LOCK_HELD":"1","AGENT_SPEC_NEXT_VERSION":str(version),"AGENT_SPEC_ROOT":str(spec_root),"AGENT_ROUTE_FILE":str(Path(args.route).resolve()),"AGENT_ROUTE_ID":route["route_id"],"AGENT_ROUTE_NODE":node["id"]}
        legacy_before=legacy_spec_state(artifact) if spec_layout=="cycle" else {}
        result=subprocess.run(command,cwd=str(worktree),env=env)
        if spec_layout=="cycle":
            # Defect K: the transaction protected an empty cycle directory while
            # the child wrote the legacy `spec/` tree and reported success. A
            # cycle-layout transaction whose window changed the legacy bucket is
            # a typed failure, whatever the child's exit code said.
            legacy_after=legacy_spec_state(artifact)
            changed=sorted(set(k for k in set(legacy_before)|set(legacy_after) if legacy_before.get(k)!=legacy_after.get(k)))
            if changed:
                emit({"status":"blocked","reason":"legacy-spec-written","route_id":route["route_id"],"changed":changed[:20],"spec_root":str(spec_root)},args.events)
                result=subprocess.CompletedProcess(command,65)
        try:
            postimage=read_regular_file(prd,allow_missing=True)
        except ValueError as exc:
            emit({"status":"blocked","reason":"prd-result-invalid","detail":str(exc),"route_id":route["route_id"]},args.events)
            result=subprocess.CompletedProcess(command,65); postimage=None
        snapshot_status="not-required-new" if preimage is None else "not-required-unchanged"
        if preimage is not None and postimage!=preimage:
            try:
                snapshot_status,snapshot_path=persist_snapshot(spec_root,version,preimage)
            except (OSError,ValueError) as exc:
                reason="version-snapshot-mismatch" if str(exc)=="snapshot-mismatch" else "version-snapshot-invalid"
                emit({"status":"blocked","reason":reason,"detail":str(exc),"route_id":route["route_id"],"version":version},args.events)
                result=subprocess.CompletedProcess(command,65)
            else:
                emit({"status":"snapshot","snapshot":snapshot_status,"route_id":route["route_id"],"version":version,"path":str(snapshot_path),"preimage_sha256":hashlib.sha256(preimage).hexdigest()},args.events)
        elif preimage is not None and prepared_status=="created" and prepared_path is not None:
            discard_unused_snapshot(spec_root,version,prepared_path)
        if preimage is not None and postimage is None and result.returncode==0:
            emit({"status":"blocked","reason":"prd-missing-after-transaction","route_id":route["route_id"],"version":version},args.events)
            result=subprocess.CompletedProcess(command,65)
        if postimage is not None and postimage!=preimage:
            event,notice=readability_event(prd,preimage,postimage,route["route_id"],version)
            try:
                emit(event,args.events)
                if notice: print(notice,file=sys.stderr,flush=True)
            except Exception:
                pass
        emit({"status":"released","route_id":route["route_id"],"version":version,"result":result.returncode,"snapshot":snapshot_status},args.events)
        lock.seek(0); lock.truncate(); lock.flush(); os.fsync(lock.fileno())
        return result.returncode


if __name__=="__main__": raise SystemExit(main())
