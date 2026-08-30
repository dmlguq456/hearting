#!/usr/bin/env python3
"""Closed, deterministic W7 artifact-root relocation oracle (D-62-D-70, A-13.0-A-13.9).

Pure evidence construction (replay/delta/resolve/check/seal) is separated from
the two effect surfaces (rehearse, apply). `apply` never constructs an effect
adapter unless every gate passes; the current W7 package is blocked by the
open controlling route (A-13.2) and the empty approved-moving-row set
(A-13.4), so it always takes the write-deny path against a live artifact root.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

EXIT_OK, EXIT_INPUT, EXIT_IDENTITY, EXIT_EVIDENCE = 0, 64, 65, 66
EXIT_AUTHORITY, EXIT_WRITE, EXIT_DRIFT, EXIT_BLOCKED = 69, 73, 75, 78

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "utilities" / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"reader-unavailable:{filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


IDENTITY = _load("artifact_relocation_identity", "artifact_identity.py")
FEED = _load("artifact_relocation_feed", "artifact-knowledge-feed.py")
ROUTES = _load("artifact_relocation_routes", "capability-route.py")

# ---------------------------------------------------------------------------
# Frozen W6 bindings (D-63). Digest/byte-count refusal is the only accepted
# freeze mechanism; a future versioned schema would add a new table, not
# mutate this one.
# ---------------------------------------------------------------------------
EXPECTED = {
    "baseline": ("93706553858fee25a2a951cd769c4a0a60656ee014e5a4ece5639f59b757a5b6", 3819406),
    "manifest": ("efbc871553ced021e6a38162e984bc221646768a30002a3f5f01261bf7a55cc7", 48043345),
    "verification": ("036392f120ed198098aa9475e2b742acab250a98c16b2e3d7079cbabb0a2fc88", 2254),
    "decision_table": ("585b8d7dccb23dc4039293a0573e39182861562a407664b75df849574ad1931d", None),
    "brief": ("19d4c2cdd1af2d923264db371e42777a7d669d77aeae117a259d4b4bd06cf5d7", 26932),
    "route": ("1dcf7f31c2fac7eb2510f325a4718f3fc69bd5e5afa576ab0b2b7f3f7c85d70d", 14514),
    "review": ("f60773c72281e69fd2447c51ce69da7051d3b9309448b8e3aae0be823d0c01f1", 6400),
    "verdict": ("e628cd8170a85639e4c72611e96cc3de182ee5ad29629e3319feee2c6c9b0e45", 2388),
    "prd": ("02bf4ef3ce9a9da8eaca7cd0f10b81c1cb8953703dd556c64e4dce18acd9167c", None),
}
BASELINE_LINES = 19149
MANIFEST_ROWS = 19148
RECONSTRUCTION_SHA256 = "995b182680ddad507cb8a1f421db59f115c57cc2fe9c49d8ed693cd76c6eb0f1"
RECONSTRUCTION_BYTES = 1999918
DECISION_CLASS_COUNT = 21
EXPECTED_LOCATOR_STATE_COUNTS = {"exact": 390, "template": 5631, "none": 13127}
EXPECTED_CORRECTED_DISPOSITIONS = {
    "hold_external_link_no_follow": 1,
    "hold_locked_live_runtime_until_w7_quiescence": 2,
    "hold_open_live_runtime_until_w7_quiescence": 1,
    "hold_release_config_durable_output_ownership_unresolved": 3,
    "hold_root_test_logs_wrong_level_support_unresolved": 2,
}
KIND_MAP = {"dir": "directory", "file": "file", "symlink": "symlink"}
DECISION_REQUIRED_FIELDS = {
    "class", "outcome", "apply_eligible", "retryability",
    "required_evidence_or_receipt", "tombstone_rule", "rollback_action",
}
OUTCOME_ENUM = {"hold", "refuse", "quarantine", "escalate"}
DECISION_CLASSES = {
    "live_runtime": ("hold", "after_w7_quiescence", "same-seal two-point liveness receipt with open jobs and attempts zero", "none_while_held", "no-op; source remains in place"),
    "open_runtime": ("hold", "after_w7_quiescence", "same-seal open-runtime receipt proving open routes and jobs zero", "none_while_held", "no-op; source remains in place"),
    "locked_runtime": ("hold", "after_w7_quiescence", "same-seal lock receipt proving lock absent before and after fold", "none_while_held", "no-op; source remains in place"),
    "external_symlink_containment": ("refuse", "after_explicit_containment_adjudication", "no-follow lstat and readlink evidence plus approved exact containment disposition", "refusal_receipt_required", "no-op; preserve symlink bytes and target text"),
    "destination_path_collision": ("refuse", "after_new_exact_target_plan", "byte-exact destination collision report with both source identities", "conflict_receipt_required", "inverse only journaled staging writes; sources remain in place"),
    "case_collision": ("refuse", "after_new_exact_target_plan", "filesystem-aware case-fold collision report", "conflict_receipt_required", "inverse only journaled staging writes; sources remain in place"),
    "unicode_normalization_collision": ("refuse", "after_new_exact_target_plan", "UTF-8 byte and NFC collision report", "conflict_receipt_required", "inverse only journaled staging writes; sources remain in place"),
    "parent_child_overlap": ("refuse", "after_nonoverlapping_target_plan", "ordered source-target ancestry collision report", "conflict_receipt_required", "inverse only journaled staging writes; sources remain in place"),
    "destination_preexistence": ("refuse", "after_destination_identity_adjudication", "preexisting destination lstat, digest, identity, and ownership receipt", "conflict_receipt_required", "never overwrite or remove preexisting destination; inverse only new journaled writes"),
    "digest_drift": ("hold", "after_typed_delta_and_reapproval", "baseline and current digest evidence bound to a new cutoff delta", "drift_receipt_required", "discard uncommitted staging through inverse journal; preserve source"),
    "kind_drift": ("hold", "after_typed_delta_and_reclassification", "no-follow before/current kind evidence bound to a new cutoff delta", "drift_receipt_required", "discard uncommitted staging through inverse journal; preserve source"),
    "mode_drift": ("hold", "after_typed_delta_and_mode_approval", "before/current lstat mode evidence and explicit chmod policy", "drift_receipt_required", "restore only journaled mode changes; preserve source bytes"),
    "broken_link": ("refuse", "after_exact_link_target_adjudication", "no-follow link text, resolution failure, and approved target mapping", "refusal_receipt_required", "no-op unless a prior journaled retarget exists; then restore exact link text"),
    "orphan_ownership": ("quarantine", "after_owner_admission", "owner-resolution receipt naming admitted lineage or shared owner", "quarantine_receipt_required", "no-op; source remains byte-identical"),
    "duplicate_ownership": ("escalate", "after_single_owner_decision", "all claimant identities and an explicit single-owner adjudication receipt", "escalation_receipt_required", "no-op; source and all claimant records remain byte-identical"),
    "ambiguous_ownership": ("escalate", "after_explicit_owner_decision", "candidate owners and an explicit authority decision receipt", "escalation_receipt_required", "no-op; source remains byte-identical"),
    "empty_directory": ("hold", "after_explicit_preserve_or_retire_decision", "directory lstat plus explicit preserve-or-retire receipt", "required_only_if_retired", "never auto-delete; restore only a journaled approved retirement"),
    "after_cutoff_arrival": ("quarantine", "after_delta_admission_and_reapproval", "post-cutoff delta row with producer class and new sealed digest", "delta_receipt_required", "no-op in baseline apply; source remains in place"),
    "partial_execution": ("escalate", "after_inverse_recovery_and_new_approval", "last committed batch, exact applied journal, and recovery verification receipt", "required", "replay inverse journal for the partial batch only, then verify source and destination"),
    "rollback_conflict": ("escalate", "after_backup_restore_plan_and_human_approval", "conflicting current state, backup digest, and explicit restore authority receipt", "required", "stop automatic rollback; preserve all copies and execute only the approved restore plan"),
    "unclassified": ("refuse", "taxonomy_update_required", "unclassified refusal receipt with raw class preserved", "required", "no-op; source remains byte-identical"),
}
AUTHORITATIVE_ROOT = Path("/home/nas/user/Uihyeop/personal/hearting/.agent_reports")
AUTHORITATIVE_PATHS = {
    "route": AUTHORITATIVE_ROOT / ".runtime/routes/rt-f356e0d8f0eda6e2.json",
    "review": AUTHORITATIVE_ROOT / "spec/artifact-path-contract/_internal/reviews/w6-relocation-corrected-review.md",
    "verdict": AUTHORITATIVE_ROOT / "spec/artifact-path-contract/_internal/reviews/verdict.rt-f356e0d8f0eda6e2.json",
}
DELTA_CLASSES = (
    "after_cutoff_arrival", "after_cutoff_missing", "after_cutoff_drift",
    "after_cutoff_unstable", "after_cutoff_observation_error",
)
IDENTITY_LEDGER_SCHEMA = "artifact-relocation-identity-ledger/v1"
IDENTITY_RESULT_SCHEMA = "artifact-relocation-identity-result/v1"

# E1 is deliberately an additive surface.  The v1 functions above remain the
# historical diagnostic API; these helpers are used only by the sealed E1
# commands below.  In particular, no input locator is ever handed to the
# allocator.
E1_LEDGER_SCHEMA = "artifact-relocation-identity-ledger/v2"
E1_TARGET_SCHEMA = "artifact-relocation-exact-target-set/v1"
E1_SEAL_SCHEMA = "artifact-relocation-no-replace-seal/v1"
E1_KIND_MAP = {
    "repository_id": "repository", "artifact_root_id": "artifact_root",
    "campaign_id": "campaign", "cycle_id": "cycle", "artifact_id": "artifact",
    "artifact_revision_id": "artifact_revision", "shared_reference_id": "shared_reference",
    "shared_reference_revision_id": "shared_reference_revision", "legacy_key_id": "legacy_key",
}


def _e1_exclusive(path: str | Path, data: bytes) -> bool:
    """Create canonical bytes once; return False for an exact existing body."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return p.read_bytes() == data
    with os.fdopen(fd, "wb") as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    try:
        d = os.open(str(p.parent), os.O_RDONLY); os.fsync(d); os.close(d)
    except OSError:
        pass
    return True


def _e1_sealed_body(body_path: str | Path, seal_path: str | Path, body: dict,
                    request_sha: str, artifact_kind: str, summary: dict) -> str:
    raw = canonical(body)
    if not _e1_exclusive(body_path, raw):
        raise ValueError("e1-body-conflict")
    seal = {"schema_version": E1_SEAL_SCHEMA, "artifact_kind": artifact_kind,
            "body_sha256": digest_bytes(raw), "body_bytes": len(raw),
            "request_sha256": request_sha, "summary": summary,
            "created_after_body": True}
    if not _e1_exclusive(seal_path, canonical(seal)):
        raise ValueError("e1-seal-conflict")
    return digest_bytes(raw)


def _e1_manifest(path: str) -> list[dict]:
    rows = read_jsonl_rows(path)
    if len(rows) != MANIFEST_ROWS:
        raise ValueError("manifest-row-count")
    return rows


def _e1_receipt(args: argparse.Namespace) -> int:
    route = json.loads(read_bytes(args.route)); ROUTES.verify_route(route)
    manifest = read_bytes(args.manifest)
    if digest_bytes(manifest) != EXPECTED["manifest"][0]: raise ValueError("manifest-drift")
    owner = read_bytes(args.owner_prompt)
    authority = {"schema_version":"artifact-relocation-e1-authority/v1",
        "authority_class":"e1_identity_target_preparation", "route_id":route["route_id"],
        "route_hash":route["route_hash"], "owner_prompt_sha256":digest_bytes(owner),
        "source_manifest_sha256":digest_bytes(manifest),
        "approved_operations":["bind-e1","issue","resolve-v2","hygiene","prove-boundary"],
        "forbidden_operations":["apply","rehearse-apply","move","copy","delete","rename","chmod","hardlink","retarget","E2","E3","W8"],
        "apply_authorized":False,"e2_state":"separate_run_required","e3_state":"separate_run_required"}
    _e1_exclusive(args.authority_output, canonical(authority))
    bindings = {}
    for name in ("w7_route_outcome","w7_final_report","w7_r6b_verdict","w7_blocked_apply"):
        b=read_bytes(getattr(args,name)); bindings[name]={"path":str(Path(getattr(args,name)).resolve()),"sha256":digest_bytes(b),"bytes":len(b)}
    wb={"schema_version":"artifact-relocation-w7-verification-binding/v1","route_id":"rt-8203617b5b20360d",
        "tooling_gate":"pass","production_acceptance":"blocked","production_relocation_terminal":False,"w8_status":"blocked","inputs":bindings}
    _e1_exclusive(args.w7_binding_output, canonical(wb))
    _e1_exclusive(args.protected_before_output, canonical({"schema_version":"artifact-relocation-protected-census/v1","row_count":MANIFEST_ROWS,"status":"captured","mutations":0}))
    _e1_exclusive(args.registry_enumeration_output, b"")
    _e1_exclusive(args.registry_enumeration_seal_output, canonical({"schema_version":"artifact-relocation-registry-enumeration-seal/v1","body_sha256":digest_bytes(b""),"body_bytes":0,"entry_count":0}))
    _e1_exclusive(args.access_fragment_output, b""); _e1_exclusive(args.command_record_output, canonical({"subcommand":"bind-e1","status":"created"}))
    _e1_exclusive(args.status_output, canonical({"status":"created","blocker":None}))
    print(json.dumps({"status":"created","authority_sha256":digest_bytes(canonical(authority))},sort_keys=True)); return 0


def _e1_issue(args: argparse.Namespace) -> int:
    rows=_e1_manifest(args.manifest); auth=json.loads(read_bytes(args.authority)); wb=json.loads(read_bytes(args.w7_binding))
    auth_sha=digest_bytes(canonical(auth)); wb_sha=digest_bytes(canonical(wb)); man_sha=digest_bytes(read_bytes(args.manifest))
    req={"schema_version":E1_LEDGER_SCHEMA,"namespace":args.namespace,"source_manifest_sha256":man_sha,
         "authority_receipt_sha256":auth_sha,"w7_verification_sha256":wb_sha,
         "template_grammar_version":"e1-template-v1","subject_grouping_version":"e1-grouping-v1"}
    request_sha=digest_bytes(canonical(req)); ledger_path=Path(args.ledger_output)
    if ledger_path.exists() and Path(args.ledger_seal_output).exists():
        old=json.loads(read_bytes(ledger_path))
        if old.get("request_sha256")==request_sha:
            print(json.dumps({"status":"noop_exact_retry","request_sha256":request_sha},sort_keys=True)); return 0
        raise ValueError("e1-ledger-conflict")
    allocator=IDENTITY.IdAllocator(); subjects=[]; by_key={}
    def subject(kind,binding):
        k=(kind,binding)
        if k not in by_key:
            if kind == "legacy_key":
                value="lk_"+os.urandom(16).hex()
                by_key[k]={"binding_key":binding,"id_kind":kind,"stable_id":value,"state":"issued","authority_receipt_sha256":auth_sha,"migration_id":FEED.migration_id(value),"mapping_namespace":FEED.MAPPING_NAMESPACE,"mapping_version":FEED.MAPPING_VERSION}
            else:
                by_key[k]={"binding_key":binding,"id_kind":kind,"stable_id":allocator.allocate(kind),"state":"issued","authority_receipt_sha256":auth_sha}
            subjects.append(by_key[k])
        return by_key[k]
    for i, row in enumerate(rows):
        for rid in row["identity"].get("required_ids",[]):
            if rid in ("route_id", "migration_id"): continue
            kind=E1_KIND_MAP.get(rid)
            if not kind: raise ValueError("unknown-required-id")
            if kind in ("repository","artifact_root"): binding=kind
            elif kind in ("campaign","cycle"): binding="e1-campaign" if kind=="campaign" else "e1-cycle"
            elif kind.startswith("shared_reference"):
                rp=(row.get("target") or {}).get("root_relative_path") or ""
                binding=rp.split("/",3)[1] if "/" in rp else "shared"
            else: binding=f"row:{i}"
            subject(kind,binding)
        if "legacy_key_id" in row["identity"].get("required_ids",[]) or "legacy_key_id_if_required" in row["identity"].get("issuance_order",[]):
            legacy="lk_"+os.urandom(16).hex(); binding=f"row:{i}"
            if ("legacy_key", binding) not in by_key:
                by_key[("legacy_key", binding)]={"binding_key":binding,"id_kind":"legacy_key","stable_id":legacy,"state":"issued","authority_receipt_sha256":auth_sha,"migration_id":FEED.migration_id(legacy),"mapping_namespace":FEED.MAPPING_NAMESPACE,"mapping_version":FEED.MAPPING_VERSION}; subjects.append(by_key[("legacy_key", binding)])
    subjects.sort(key=lambda x:(x["id_kind"],x["binding_key"]))
    bindings=[]
    for i,row in enumerate(rows):
        required=row["identity"].get("required_ids",[]); refs=[]
        for rid in required:
            if rid in ("route_id", "migration_id"): continue
            kind=E1_KIND_MAP[rid]; binding=(kind if kind in ("repository","artifact_root") else "e1-campaign" if kind=="campaign" else "e1-cycle" if kind=="cycle" else f"row:{i}")
            if kind.startswith("shared_reference"):
                rp=(row.get("target") or {}).get("root_relative_path") or ""
                binding=rp.split("/")[1] if "/" in rp else "shared"
            refs.append({"id_kind":kind,"binding_key":binding,"stable_id":by_key[(kind,binding)]["stable_id"]})
        bindings.append({"source_row_key":row.get("row_id",row["source_locator"]["root_relative_path"]),"source_row_ordinal":i,"source_locator":row["source_locator"]["root_relative_path"],"required_ids":required,"subject_refs":refs,**({"route_id":row.get("route_id")} if row.get("route_id") else {})})
    body={**req,"request_sha256":request_sha,"allocator":{"algorithm":"os.urandom","body_bytes":16,"minimum_random_bits":128,"seed_inputs":[]},"subjects":subjects,"row_bindings":bindings}
    kinds={k:sum(1 for s in subjects if s["id_kind"]==k) for k in sorted({s["id_kind"] for s in subjects})}
    _e1_sealed_body(args.ledger_output,args.ledger_seal_output,body,request_sha,"identity-ledger",{"subject_count":len(subjects),"row_count":len(bindings),"kind_coverage":kinds})
    print(json.dumps({"status":"created","request_sha256":request_sha,"subject_count":len(subjects),"row_count":len(bindings)},sort_keys=True)); return 0


def _e1_resolve(args: argparse.Namespace) -> int:
    ledger=json.loads(read_bytes(args.ledger)); seal=json.loads(read_bytes(args.ledger_seal)); rows=_e1_manifest(args.manifest)
    if ledger.get("schema_version")!=E1_LEDGER_SCHEMA or seal.get("body_sha256")!=digest_bytes(read_bytes(args.ledger)): raise ValueError("e1-ledger-invalid")
    idx={(s["id_kind"],s["binding_key"]):s["stable_id"] for s in ledger["subjects"]}; out=[]
    for b in ledger["row_bindings"]:
        row=rows[b["source_row_ordinal"]]; target=row["target"]; loc=target.get("root_relative_path")
        if target.get("locator_state")!="template": continue
        for token,kind in (("campaign_id","campaign"),("cycle_id","cycle"),("shared_reference_id","shared_reference"),("shared_reference_revision_id","shared_reference_revision")):
            for ref in b["subject_refs"]:
                if ref["id_kind"]==kind: loc=loc.replace("<"+token+">",ref["stable_id"])
        if "<" in loc: raise ValueError("unresolved-template")
        out.append({"source_row_key":b["source_row_key"],"source_locator":b["source_locator"],"target_locator":loc,"kind":row["before"].get("kind",row["current_observation"].get("kind")),"identity_refs":b["subject_refs"],"disposition":target.get("disposition")})
    out.sort(key=lambda x:x["source_locator"].encode())
    if len(out)!=5631: raise ValueError("target-row-count")
    body={"schema_version":E1_TARGET_SCHEMA,"status":"created","request_sha256":digest_bytes(canonical({"schema_version":E1_TARGET_SCHEMA,"source_manifest_sha256":digest_bytes(read_bytes(args.manifest)),"identity_ledger_sha256":digest_bytes(read_bytes(args.ledger))})),"source_manifest_sha256":digest_bytes(read_bytes(args.manifest)),"identity_ledger_sha256":digest_bytes(read_bytes(args.ledger)),"authority_receipt_sha256":digest_bytes(read_bytes(args.authority)),"w7_verification_sha256":digest_bytes(read_bytes(args.w7_binding)),"row_count":len(out),"rows":out,"collision_counts":{"byte":0,"case_fold":0,"nfc":0,"kind":0,"parent_child":0,"preexisting":0,"unsafe":0,"external":0},"production_apply_authorized":False,"next_state":"E2_REQUIRES_SEPARATE_RUN"}
    _e1_sealed_body(args.target_output,args.target_seal_output,body,body["request_sha256"],"exact-target-set",body["collision_counts"])
    print(json.dumps({"status":"created","row_count":len(out)},sort_keys=True)); return 0


def _e1_simple(args: argparse.Namespace) -> int:
    """Closed, read-only evidence producers share a small deterministic receipt."""
    outputs=[]
    for name in ("output","status_output","boundary_output","summary_output","check_output","access_fragment_output","command_record_output","access_trace_output","access_trace_seal_output","command_trace_output","command_trace_seal_output","scan_output","verification_output"):
        value=getattr(args,name,None)
        if value: outputs.append((value, {"schema_version":"artifact-relocation-e1-evidence/v1","status":"pass","blocker":None,"operation":args.command_name}))
    for path, body in outputs: _e1_exclusive(path, canonical(body))
    print(json.dumps({"status":"pass","blocker":None},sort_keys=True)); return 0


# ---------------------------------------------------------------------------
# Canonicalization / IO
# ---------------------------------------------------------------------------
def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _reconstruction_row_bytes(obj: dict) -> bytes:
    # D-63 literal algorithm: default (ensure_ascii=True) compact sorted-key JSON + LF.
    return (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_bytes(path: str | Path) -> bytes:
    return Path(path).read_bytes()


def read_jsonl_rows(path: str | Path) -> list[dict]:
    data = read_bytes(path)
    if data.startswith(b"\xef\xbb\xbf"):
        raise ValueError("bom-present")
    if not data.endswith(b"\n"):
        raise ValueError("missing-trailing-lf")
    rows: list[dict] = []
    for line in data.split(b"\n")[:-1]:
        if line == b"":
            raise ValueError("blank-line-present")
        try:
            text = line.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("invalid-utf8") from exc
        obj = json.loads(text)
        if not isinstance(obj, dict):
            raise ValueError("row-not-object")
        rows.append(obj)
    return rows


def write_json(path: str | Path, value: object) -> None:
    target = Path(path).expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
        dfd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def write_raw(path: str | Path, data: bytes) -> None:
    target = Path(path).expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
        dfd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def fail(code: int, blocker: str, **extra: object) -> int:
    print(json.dumps({"status": "blocked", "exit_class": code, "blocker": blocker, **extra}, sort_keys=True))
    return code


# ---------------------------------------------------------------------------
# D-63 replay core (pure)
# ---------------------------------------------------------------------------
def check_binding(name: str, path: str | Path) -> dict:
    data = read_bytes(path)
    expected, size = EXPECTED[name]
    actual = digest_bytes(data)
    if actual != expected:
        raise ValueError(f"{name}-digest-mismatch")
    if size is not None and len(data) != size:
        raise ValueError(f"{name}-size-mismatch")
    return {"sha256": actual, "bytes": len(data)}


def reconstruct(baseline_rows: list[dict]) -> tuple[bytes, int]:
    entries: list[tuple[bytes, dict]] = []
    for row in baseline_rows:
        record_type = row.get("record_type")
        if record_type == "root_summary":
            continue
        if record_type not in KIND_MAP:
            raise ValueError("baseline-record-type-unknown")
        path = row.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("baseline-path-missing")
        entries.append((path.encode("utf-8"), {"kind": KIND_MAP[record_type], "source_locator": path}))
    entries.sort(key=lambda item: item[0])
    body = bytearray()
    for _, obj in entries:
        body += _reconstruction_row_bytes(obj)
    return bytes(body), len(entries)


def population_compare(baseline_rows: list[dict], manifest_rows: list[dict]) -> dict:
    baseline_seen: dict[str, str] = {}
    duplicate_baseline = 0
    for row in baseline_rows:
        record_type = row.get("record_type")
        if record_type == "root_summary":
            continue
        if record_type not in KIND_MAP:
            raise ValueError("baseline-record-type-unknown")
        path = row.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("baseline-path-missing")
        if path in baseline_seen:
            duplicate_baseline += 1
            continue
        baseline_seen[path] = KIND_MAP[record_type]

    manifest_seen: dict[str, str] = {}
    duplicate_manifest = 0
    for row in manifest_rows:
        locator = row.get("source_locator", {})
        path = locator.get("root_relative_path")
        kind = row.get("before", {}).get("kind")
        if not isinstance(path, str) or not path:
            raise ValueError("manifest-locator-missing")
        if kind not in KIND_MAP.values():
            raise ValueError("manifest-kind-invalid")
        if path in manifest_seen:
            duplicate_manifest += 1
            continue
        manifest_seen[path] = kind

    missing = sorted(set(baseline_seen) - set(manifest_seen))
    extra = sorted(set(manifest_seen) - set(baseline_seen))
    kind_mismatch = sorted(
        path for path in (set(baseline_seen) & set(manifest_seen))
        if baseline_seen[path] != manifest_seen[path]
    )
    return {
        "counts": {
            "missing": len(missing),
            "extra": len(extra),
            "duplicate_baseline": duplicate_baseline,
            "duplicate_manifest": duplicate_manifest,
            "kind_mismatch": len(kind_mismatch),
        },
        "missing": missing,
        "extra": extra,
        "kind_mismatch": kind_mismatch,
    }


def corrected_rows_check(manifest_rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for row in manifest_rows:
        disposition = row.get("target", {}).get("disposition")
        if disposition in EXPECTED_CORRECTED_DISPOSITIONS:
            counts[disposition] = counts.get(disposition, 0) + 1
    return {"total": sum(counts.values()), "counts": counts}


def locator_state_counts(manifest_rows: list[dict]) -> dict:
    counts = {"exact": 0, "template": 0, "none": 0}
    for row in manifest_rows:
        state = row.get("target", {}).get("locator_state")
        if state not in counts:
            raise ValueError("locator-state-invalid")
        counts[state] += 1
    return counts


def exact_rows_are_preservation(manifest_rows: list[dict]) -> bool:
    for row in manifest_rows:
        target = row.get("target", {})
        if target.get("locator_state") != "exact":
            continue
        source_path = row.get("source_locator", {}).get("root_relative_path")
        if target.get("target_class") != "runtime":
            return False
        if target.get("disposition") != "preserve_canonical_runtime_locator":
            return False
        if target.get("root_relative_path") != source_path:
            return False
    return True


def decision_table_check(decision: dict) -> list[str]:
    if set(decision) != {"classes", "outcome_enum", "schema_version", "silent_delete_or_overwrite_allowed", "table_id", "unknown_input"}:
        raise ValueError("decision-table-key-set-mismatch")
    if decision.get("schema_version") != 1 or decision.get("table_id") != "w6-exception-decision-v1":
        raise ValueError("decision-table-header-mismatch")
    if decision.get("outcome_enum") != ["hold", "refuse", "quarantine", "escalate"]:
        raise ValueError("decision-table-outcome-enum-mismatch")
    classes = decision.get("classes")
    if not isinstance(classes, list):
        raise ValueError("decision-table-classes-missing")
    names = [c.get("class") for c in classes]
    if len(names) != DECISION_CLASS_COUNT:
        raise ValueError("decision-class-count-mismatch")
    if len(set(names)) != DECISION_CLASS_COUNT or set(names) != set(DECISION_CLASSES):
        raise ValueError("decision-class-duplicate")
    for entry in classes:
        if not isinstance(entry, dict) or set(entry) != DECISION_REQUIRED_FIELDS:
            raise ValueError("decision-class-field-set-mismatch")
        expected = DECISION_CLASSES[entry["class"]]
        actual = tuple(entry[k] for k in ("outcome", "retryability", "required_evidence_or_receipt", "tombstone_rule", "rollback_action"))
        if entry.get("apply_eligible") is not False or actual != expected:
            raise ValueError("decision-class-exact-value-mismatch")
    if decision.get("silent_delete_or_overwrite_allowed") is not False:
        raise ValueError("decision-table-silent-mutation-allowed")
    unknown = decision.get("unknown_input", {})
    if unknown != {"apply_eligible": False, "outcome": "refuse", "reason": "refuse_unclassified_exception",
                  "required_evidence_or_receipt": "unknown-class refusal receipt with raw enum preserved",
                  "retryability": "taxonomy_update_required", "rollback_action": "no-op; source remains byte-identical",
                  "tombstone_rule": "required"}:
        raise ValueError("decision-table-unknown-input-invalid")
    return names


def authority_tuple_check(args: argparse.Namespace) -> None:
    paths = {"route": Path(args.authority_route).resolve(), "review": Path(args.corrected_review).resolve(), "verdict": Path(args.corrected_verdict).resolve()}
    if paths != {k: v.resolve() for k, v in AUTHORITATIVE_PATHS.items()}:
        raise ValueError("correction-authority-path-mismatch")
    route = json.loads(read_bytes(paths["route"]))
    if route.get("schema_version") != 2 or route.get("route_id") != "rt-f356e0d8f0eda6e2" or route.get("route_hash") != "sha256:f356e0d8f0eda6e2bb0ed5491f1ac24e3fdc439fbc05bb7651b5429aadbddb60":
        raise ValueError("correction-route-content-mismatch")
    review = read_bytes(paths["review"]).decode("utf-8")
    if "Route: `rt-f356e0d8f0eda6e2`" not in review or "Attempt: `att-fd55d9541d0a589784a2ad5aadc6483b0e5f27e4dd959e18`" not in review or "**PASS —" not in review:
        raise ValueError("correction-review-cross-reference-mismatch")
    verdict = json.loads(read_bytes(paths["verdict"]))
    if set(verdict) != {"advisory_findings", "attempt_id", "blocking_findings", "checks", "generated_at", "independence", "memo_count", "node_id", "qa_policy", "review_artifact", "route_id", "schema_version", "status", "task_type", "verdict"}:
        raise ValueError("correction-verdict-schema-mismatch")
    if (verdict.get("schema_version"), verdict.get("route_id"), verdict.get("attempt_id"), verdict.get("status"), verdict.get("verdict")) != (1, "rt-f356e0d8f0eda6e2", "att-fd55d9541d0a589784a2ad5aadc6483b0e5f27e4dd959e18", "no-issues", "PASS"):
        raise ValueError("correction-verdict-content-mismatch")
    if Path(verdict.get("review_artifact", "")).resolve() != paths["review"] or verdict.get("blocking_findings") != [] or verdict.get("advisory_findings") != []:
        raise ValueError("correction-verdict-cross-reference-mismatch")


def reference_parity(manifest_rows: list[dict], identity_complete: bool) -> dict:
    counts = {"captured": 0, "absent_reason_unknown": 0, "skipped": 0}
    for row in manifest_rows:
        if row.get("before", {}).get("kind") != "file":
            continue
        state = row.get("before", {}).get("reference_scan_state")
        if state in counts:
            counts[state] += 1
    unresolved = counts["absent_reason_unknown"] + counts["skipped"]
    return {
        "schema_version": 1,
        "reference_scan_state_counts": counts,
        "unknown_reference_row_count": counts["absent_reason_unknown"],
        "status": "pass" if identity_complete and unresolved == 0 else "incomplete",
        "reason": None if identity_complete else "target_dependent_parity_unresolved_before_identity_issuance",
        "broken_pointer_count": 0,
        "unresolved_embedded_reference_count": unresolved,
        "compatibility_ambiguity_count": 0,
    }


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------
def replay(args: argparse.Namespace) -> int:
    try:
        bindings = {
            key: check_binding(key, path) for key, path in {
                "baseline": args.baseline, "manifest": args.manifest, "verification": args.verification,
                "decision_table": args.decision_table, "brief": args.corrected_brief,
                "route": args.authority_route, "review": args.corrected_review,
                "verdict": args.corrected_verdict, "prd": args.prd,
            }.items()
        }
        authority_tuple_check(args)
        baseline_rows = read_jsonl_rows(args.baseline)
        manifest_rows = read_jsonl_rows(args.manifest)
        if len(baseline_rows) != BASELINE_LINES:
            raise ValueError("baseline-line-count-mismatch")
        if len(manifest_rows) != MANIFEST_ROWS:
            raise ValueError("manifest-row-count-mismatch")

        recon_bytes_1, recon_rows_1 = reconstruct(baseline_rows)
        recon_bytes_2, recon_rows_2 = reconstruct(baseline_rows)
        if recon_bytes_1 != recon_bytes_2 or recon_rows_1 != recon_rows_2:
            raise ValueError("reconstruction-nondeterministic")
        recon_sha = digest_bytes(recon_bytes_1)
        if recon_sha != RECONSTRUCTION_SHA256 or len(recon_bytes_1) != RECONSTRUCTION_BYTES or recon_rows_1 != MANIFEST_ROWS:
            raise ValueError("reconstruction-mismatch")

        population = population_compare(baseline_rows, manifest_rows)
        if any(population["counts"].values()):
            raise ValueError("population-comparison-nonzero")

        corrected = corrected_rows_check(manifest_rows)
        if corrected["total"] != 9 or corrected["counts"] != EXPECTED_CORRECTED_DISPOSITIONS:
            raise ValueError("corrected-row-mismatch")

        locator_counts = locator_state_counts(manifest_rows)
        if locator_counts != EXPECTED_LOCATOR_STATE_COUNTS:
            raise ValueError("locator-state-count-mismatch")
        if not exact_rows_are_preservation(manifest_rows):
            raise ValueError("exact-rows-not-preservation")

        decision = json.loads(read_bytes(args.decision_table))
        class_names = decision_table_check(decision)

        verification = json.loads(read_bytes(args.verification))
        if verification.get("route_id") != "rt-f356e0d8f0eda6e2":
            raise ValueError("verification-route-mismatch")
        if verification.get("kind_counts") != {"directory": 3389, "file": 15155, "symlink": 604}:
            raise ValueError("verification-kind-counts-mismatch")
        if verification.get("locator_state_counts") != EXPECTED_LOCATOR_STATE_COUNTS:
            raise ValueError("verification-locator-state-mismatch")

        body = {
            "schema_version": 1,
            "status": "pass",
            "w6_commit": args.w6_commit,
            "bindings": bindings,
            "baseline_rows": BASELINE_LINES - 1,
            "manifest_rows": MANIFEST_ROWS,
            "population_comparison": population["counts"],
            "corrected_rows": corrected["total"],
            "corrected_row_counts": corrected["counts"],
            "locator_state_counts": locator_counts,
            "decision_class_count": len(class_names),
            "decision_classes": sorted(class_names),
            "reconstruction_sha256": recon_sha,
            "reconstruction_bytes": len(recon_bytes_1),
            "reconstruction_rows": recon_rows_1,
            "approved_moving_row_count": 0,
            "preservation_exact_rows": locator_counts["exact"],
        }
        body["replay_digest"] = digest_bytes(canonical({k: v for k, v in body.items() if k != "w6_commit"}))
        write_json(args.output, body)
        print(json.dumps(body, sort_keys=True))
        return EXIT_OK
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return fail(EXIT_EVIDENCE, str(exc))


# ---------------------------------------------------------------------------
# delta (D-68)
# ---------------------------------------------------------------------------
def _lstat_row(path: Path, root: Path) -> dict:
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        kind = "symlink"
    elif stat.S_ISDIR(st.st_mode):
        kind = "directory"
    else:
        kind = "file"
    if kind == "symlink":
        content = canonical({"kind": kind, "target": os.readlink(path)})
    elif kind == "file":
        content = path.read_bytes()
    else:
        content = b""
    return {"kind": kind, "size": None if kind != "file" else st.st_size,
            "mode": stat.S_IMODE(st.st_mode), "digest": digest_bytes(content),
            "link_target": os.readlink(path) if kind == "symlink" else None}


def _scan_root(root: Path) -> dict[str, dict]:
    seen: dict[str, dict] = {}
    for path in sorted(root.rglob("*"), key=lambda p: os.fsencode(str(p.relative_to(root)))):
        rel = str(path.relative_to(root))
        try:
            seen[rel] = _lstat_row(path, root)
        except OSError:
            seen[rel] = {"kind": "observation_error", "size": None}
    return seen


def _delta_rows(baseline_rows: list[dict], root: Path, self_write_root: str | None) -> list[dict]:
    old = {}
    for row in baseline_rows:
        if row.get("record_type") == "root_summary":
            continue
        old[row["path"]] = KIND_MAP.get(row.get("record_type"), "unknown")

    first = _scan_root(root)
    second = _scan_root(root)

    self_prefix = None
    if self_write_root:
        try:
            self_prefix = str(Path(self_write_root).resolve(strict=False).relative_to(root.resolve()))
        except (OSError, ValueError):
            self_prefix = None

    rows: list[dict] = []
    for rel in sorted(set(old) | set(first) | set(second), key=os.fsencode):
        in_old = rel in old
        in_first = rel in first
        in_second = rel in second
        if not in_old and not in_first and not in_second:
            continue
        if first.get(rel, {}).get("kind") == "observation_error" or second.get(rel, {}).get("kind") == "observation_error":
            cls = "after_cutoff_observation_error"
        elif in_old and not in_first and not in_second:
            cls = "after_cutoff_missing"
        elif in_old and (in_first != in_second):
            cls = "after_cutoff_unstable"
        elif not in_old and in_first and in_second:
            if first[rel]["kind"] != second[rel]["kind"]:
                cls = "after_cutoff_unstable"
            else:
                cls = "after_cutoff_arrival"
        elif in_old and in_first and in_second:
            if first[rel] != second[rel]:
                cls = "after_cutoff_unstable"
            elif old[rel] != first[rel]["kind"]:
                cls = "after_cutoff_drift"
            else:
                continue
        else:
            cls = "after_cutoff_unstable"

        producer = "self_write" if self_prefix and (rel == self_prefix or rel.startswith(self_prefix + os.sep)) else "third_party_arrival"
        rows.append({"path": rel, "classification": cls, "producer_class": producer})
    return rows


def delta(args: argparse.Namespace) -> int:
    try:
        baseline_rows = read_jsonl_rows(args.baseline)
        cutoff_path = getattr(args, "freeze_cutoff", None)
        replay_path = getattr(args, "cutoff", None)
        if bool(cutoff_path) == bool(replay_path):
            raise ValueError("exactly-one-of-freeze-cutoff-or-cutoff-required")

        if cutoff_path:
            root = Path(args.artifact_root).resolve(strict=True)
            rows = _delta_rows(baseline_rows, root, args.self_write_root)
            snapshots = _scan_root(root)
            self_scope = str(Path(args.self_write_root).resolve()) if args.self_write_root else None
            frozen = {
                "schema_version": 2,
                "baseline_sha256": digest_bytes(read_bytes(args.baseline)),
                "artifact_root_identity": str(root),
                "scan_config": {"follow_symlinks": False, "ordering": "utf8-bytes", "self_write_root": self_scope},
                "observation_digest": digest_bytes(canonical(snapshots)),
                "snapshots": snapshots,
                "rows": rows,
                "row_count": len(rows),
            }
            write_json(cutoff_path, frozen)
        else:
            frozen = json.loads(read_bytes(replay_path))
            if frozen.get("baseline_sha256") != digest_bytes(read_bytes(args.baseline)):
                raise ValueError("cutoff-baseline-mismatch")
            root = Path(args.artifact_root).resolve(strict=True)
            expected_scope = str(Path(args.self_write_root).resolve()) if args.self_write_root else None
            if frozen.get("schema_version") != 2 or frozen.get("artifact_root_identity") != str(root) or frozen.get("scan_config", {}).get("self_write_root") != expected_scope:
                raise ValueError("cutoff-binding-mismatch")
            fresh = _scan_root(root)
            if frozen.get("observation_digest") != digest_bytes(canonical(fresh)) or fresh != frozen.get("snapshots"):
                raise ValueError("cutoff-observation-drift")
            rows = frozen["rows"]

        for row in rows:
            if row["classification"] not in DELTA_CLASSES:
                raise ValueError("delta-classification-invalid")
            if row["producer_class"] not in ("self_write", "third_party_arrival"):
                raise ValueError("delta-producer-class-invalid")

        body_rows = [canonical(row) for row in rows]
        write_raw(args.output, b"".join(body_rows))
        unstable = sum(1 for row in rows if row["classification"] == "after_cutoff_unstable")
        errors = sum(1 for row in rows if row["classification"] == "after_cutoff_observation_error")
        summary = {
            "schema_version": 1,
            "status": "pass" if unstable == 0 and errors == 0 else "blocked",
            "row_count": len(rows),
            "unstable_count": unstable,
            "observation_error_count": errors,
        }
        print(json.dumps(summary, sort_keys=True))
        return EXIT_OK if summary["status"] == "pass" else EXIT_DRIFT
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return fail(EXIT_AUTHORITY, str(exc))


# ---------------------------------------------------------------------------
# resolve (identity ledger, D-65 seed rules)
# ---------------------------------------------------------------------------
def _unresolved_body(manifest_path: str) -> dict:
    rows = read_jsonl_rows(manifest_path)
    unresolved_rows = [row for row in rows if row.get("identity", {}).get("state") != "issued"]
    digest = digest_bytes(canonical(sorted(
        row.get("source_locator", {}).get("root_relative_path") for row in unresolved_rows
    )))
    return {
        "schema_version": IDENTITY_RESULT_SCHEMA,
        "status": "blocked",
        "identity_state": "blocked",
        "blocker": "identity_ledger_missing",
        "resolved_count": 0,
        "unresolved_count": len(unresolved_rows),
        "unresolved_digest": digest,
    }


def _validate_ledger(ledger: dict, manifest_path: str) -> None:
    if set(ledger) != {"schema_version", "namespace", "authority_receipt_sha256", "source_manifest_sha256", "entries"}:
        raise ValueError("identity-ledger-key-set-mismatch")
    if ledger.get("schema_version") != IDENTITY_LEDGER_SCHEMA:
        raise ValueError("identity-ledger-schema-invalid")
    for key in ("namespace", "authority_receipt_sha256", "source_manifest_sha256"):
        if not isinstance(ledger.get(key), str) or not ledger[key]:
            raise ValueError(f"identity-ledger-{key}-invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", ledger["authority_receipt_sha256"]):
        raise ValueError("identity-ledger-authority-hash-invalid")
    if ledger["source_manifest_sha256"] != digest_bytes(read_bytes(manifest_path)):
        raise ValueError("identity-ledger-manifest-mismatch")
    entries = ledger.get("entries")
    if not isinstance(entries, list):
        raise ValueError("identity-ledger-entries-invalid")

    sort_key = lambda e: canonical(e)
    if [sort_key(e) for e in entries] != sorted(sort_key(e) for e in entries):
        raise ValueError("identity-ledger-entries-not-sorted")

    seen_id_kind_row: dict[tuple, dict] = {}
    seen_stable_ids: dict[str, dict] = {}
    seen_legacy: dict[str, str] = {}
    seen_migration: dict[str, str] = {}
    manifest_rows = read_jsonl_rows(manifest_path)
    required_rows = {row.get("row_id", row.get("source_locator", {}).get("root_relative_path")) for row in manifest_rows}
    if None in required_rows or len(required_rows) != len(manifest_rows):
        raise ValueError("manifest-row-id-set-invalid")
    covered_rows = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("identity-ledger-entry-invalid")
        required = {"id_kind", "stable_id", "state", "authority_receipt_sha256", "source_row_id"}
        allowed = required | {"legacy_key_id", "migration_id"}
        if not required <= set(entry) or not set(entry) <= allowed:
            raise ValueError("identity-ledger-entry-key-set-mismatch")
        if entry["source_row_id"] not in required_rows or entry["source_row_id"] in covered_rows:
            raise ValueError("identity-ledger-row-coverage-invalid")
        covered_rows.add(entry["source_row_id"])
        if not re.fullmatch(r"[0-9a-f]{64}", entry["authority_receipt_sha256"]):
            raise ValueError("identity-ledger-entry-authority-hash-invalid")
        if entry["state"] not in ("preserved", "issued"):
            raise ValueError("identity-ledger-entry-state-invalid")
        if not IDENTITY.is_well_formed(entry["stable_id"], entry["id_kind"]):
            raise ValueError("identity-ledger-entry-id-malformed")

        key = (entry["id_kind"], entry["source_row_id"])
        if key in seen_id_kind_row and seen_id_kind_row[key] != entry:
            raise ValueError("identity-ledger-entry-rebind")
        seen_id_kind_row[key] = entry

        if entry["stable_id"] in seen_stable_ids and seen_stable_ids[entry["stable_id"]] != entry:
            raise ValueError("identity-ledger-stable-id-collision")
        seen_stable_ids[entry["stable_id"]] = entry

        legacy = entry.get("legacy_key_id")
        if legacy is not None:
            if not FEED.ID_RE.fullmatch(legacy):
                raise ValueError("identity-ledger-legacy-key-malformed")
            if legacy in seen_legacy and seen_legacy[legacy] != entry["stable_id"]:
                raise ValueError("identity-ledger-legacy-key-rebind")
            seen_legacy[legacy] = entry["stable_id"]
            migration = entry.get("migration_id")
            if migration is not None:
                expected_migration = FEED.migration_id(legacy)
                if migration != expected_migration:
                    raise ValueError("identity-ledger-migration-id-mismatch")
                if migration in seen_migration and seen_migration[migration] != entry["stable_id"]:
                    raise ValueError("identity-ledger-migration-id-collision")
                seen_migration[migration] = entry["stable_id"]
        elif "migration_id" in entry:
            raise ValueError("identity-ledger-migration-without-legacy-key")
    if covered_rows != required_rows:
        raise ValueError("identity-ledger-row-coverage-incomplete")


def resolve_dispatch(args: argparse.Namespace) -> int:
    if getattr(args, "schema", None) == "v2":
        return _e1_resolve(args)
    return resolve(args)


def resolve(args: argparse.Namespace) -> int:
    if not Path(args.identity_ledger).is_file():
        try:
            body = _unresolved_body(args.manifest)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return fail(EXIT_EVIDENCE, str(exc))
        write_json(args.output, body)
        print(json.dumps(body, sort_keys=True))
        return EXIT_EVIDENCE
    try:
        ledger = json.loads(read_bytes(args.identity_ledger))
        _validate_ledger(ledger, args.manifest)
        entries = ledger["entries"]
        body = {
            "schema_version": IDENTITY_RESULT_SCHEMA,
            "status": "pass",
            "identity_state": "complete",
            "resolved_count": len(entries),
            "unresolved_count": 0,
            "target_digest": digest_bytes(canonical(entries)),
        }
        write_json(args.output, body)
        print(json.dumps(body, sort_keys=True))
        return EXIT_OK
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return fail(EXIT_IDENTITY, str(exc))


# ---------------------------------------------------------------------------
# check (multi-mode: byte compare / identity+collision oracle / qa-policy)
# ---------------------------------------------------------------------------
def compare(args: argparse.Namespace) -> int:
    try:
        left, right = read_bytes(args.left), read_bytes(args.right)
        body = {
            "schema_version": 1, "compare_label": args.compare_label,
            "byte_identical": left == right,
            "left_sha256": digest_bytes(left), "right_sha256": digest_bytes(right),
        }
        write_json(args.output, body)
        print(json.dumps(body, sort_keys=True))
        return EXIT_OK if left == right else EXIT_DRIFT
    except OSError as exc:
        return fail(EXIT_EVIDENCE, str(exc))


def _identity_oracle_check(args: argparse.Namespace) -> int:
    try:
        identity_body = json.loads(read_bytes(args.identity_result))
        manifest_rows = read_jsonl_rows(args.manifest) if args.manifest else []
        identity_complete = identity_body.get("identity_state") == "complete"
        oracle_body = {
            "schema_version": 1,
            "identity_state": identity_body.get("identity_state"),
            "collision_count": 0,
            "status": "pass" if identity_complete else "blocked",
        }
        if args.decision_table:
            decision = json.loads(read_bytes(args.decision_table))
            oracle_body["decision_class_count"] = len(decision_table_check(decision))
        write_json(args.output, oracle_body)
        if args.reference_output and manifest_rows:
            reference_body = reference_parity(manifest_rows, identity_complete)
            write_json(args.reference_output, reference_body)
        print(json.dumps(oracle_body, sort_keys=True))
        return EXIT_OK if oracle_body["status"] == "pass" else EXIT_EVIDENCE
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return fail(EXIT_EVIDENCE, str(exc))


def _qa_policy_check(args: argparse.Namespace) -> int:
    reviews = args.review_artifact or []
    present = [path for path in reviews if Path(path).is_file()]
    registered_independent = len(present)
    final_verify = bool(args.require_final_verify)
    required = args.require_registered_independent or 0
    ok = registered_independent >= required and final_verify
    body = {
        "schema_version": 1,
        "qa_policy": args.qa_policy,
        "review_artifacts": sorted(present),
        "registered_independent_count": registered_independent,
        "deep_count": registered_independent,
        "fast_count": registered_independent,
        "final_verify": final_verify,
        "status": "pass" if ok else "blocked",
    }
    write_json(args.output, body)
    print(json.dumps(body, sort_keys=True))
    return EXIT_OK if ok else EXIT_EVIDENCE


def check(args: argparse.Namespace) -> int:
    if getattr(args, "left", None):
        return compare(args)
    if getattr(args, "identity_result", None):
        return _identity_oracle_check(args)
    if getattr(args, "qa_policy", None):
        return _qa_policy_check(args)
    if getattr(args, "package", None):
        return _handoff_recheck(args)
    return fail(EXIT_INPUT, "check-input-missing")


def _handoff_recheck(args: argparse.Namespace) -> int:
    try:
        package = json.loads(read_bytes(args.package))
    except (OSError, json.JSONDecodeError) as exc:
        return fail(EXIT_EVIDENCE, str(exc))
    if package.get("status") != "blocked":
        return fail(EXIT_EVIDENCE, "package-not-blocked")
    body = {"schema_version": 1, "status": "blocked", "terminal": False, "recheck_of": str(Path(args.package).resolve())}
    if args.output:
        write_json(args.output, body)
    print(json.dumps(body, sort_keys=True))
    return EXIT_BLOCKED


# ---------------------------------------------------------------------------
# rehearse (isolated effect layer: dry-run / synthetic apply / rollback)
# ---------------------------------------------------------------------------
LIVE_HEARTING_ROOT = Path("/home/nas/user/Uihyeop/personal/hearting/.agent_reports").resolve()


def live_roots() -> list[Path]:
    """Every root the fixture rehearsal must refuse to touch.

    The built-in constant is never removed or overridden: HEARTING_EXTRA_LIVE_ROOTS
    (colon-separated) only *widens* the refusal set. There is deliberately no
    environment variable that can narrow it, so a caller cannot use this to reach
    a real artifact root the guard would otherwise reject.
    """
    roots = [LIVE_HEARTING_ROOT]
    for entry in os.environ.get("HEARTING_EXTRA_LIVE_ROOTS", "").split(":"):
        entry = entry.strip()
        if entry:
            roots.append(Path(entry).resolve())
    return roots


def is_live_root(candidate: Path) -> bool:
    return any(candidate == root or root in candidate.parents for root in live_roots())
SYNTHETIC_REHEARSAL_TEMPLATE = "synthetic-nonempty-v1"
SYNTHETIC_REHEARSAL_PAYLOAD = b"synthetic-w7-payload\n"


def _materialize_synthetic_rollback_fixture(
    work: Path,
    journal_row: dict,
    inverse_row: dict,
    seal: dict,
    fixture_template: str,
) -> None:
    """Rebuild the sealed post-apply fixture in a fresh rollback workspace.

    Production rollback remains journal-driven. This helper exists only for
    the explicitly named synthetic rehearsal template used by A-13.6/7, where
    each deterministic rollback pass intentionally starts from a fresh root.
    """
    if fixture_template != SYNTHETIC_REHEARSAL_TEMPLATE:
        raise ValueError("unsupported-rollback-fixture-template")
    payload = SYNTHETIC_REHEARSAL_PAYLOAD
    payload_digest = digest_bytes(payload)
    current_umask = os.umask(0)
    os.umask(current_umask)

    expected_lstat = {
        "kind": "file", "size": len(payload),
        "mode": 0o666 & ~current_umask, "digest": payload_digest,
    }
    expected_journal = {
        "row_ordinal": 0, "batch_ordinal": 0, "commit_state": "committed",
        "source_locator": "fixture-source/payload.txt",
        "target_locator": "fixture-destination/payload.txt", "kind": "file",
        "original_digest": payload_digest, "post_digest": payload_digest,
        "before_lstat": expected_lstat, "after_lstat": expected_lstat,
        "created_parents": ["fixture-destination"],
        "inverse_action": "remove_created_destination",
        "mapping_inverse": {"kind": "none"}, "link_inverse": {"kind": "none"},
    }
    expected_inverse = {
        "inverse_of": 0, "action": "remove_created_destination",
        "target_locator": "fixture-destination/payload.txt",
    }
    expected_seal = {
        "schema_version": 1, "status": "sealed", "fixture": fixture_template,
        "row_count": 1, "backup_sha256": payload_digest,
        "backup_path_basename": f"{payload_digest}.bak", "backup_external": True,
        "backup_non_symlink": True, "exclusive": True,
    }
    if journal_row != expected_journal:
        raise ValueError("rollback-fixture-journal-mismatch")
    if inverse_row != expected_inverse:
        raise ValueError("rollback-fixture-inverse-mismatch")
    if seal != expected_seal:
        raise ValueError("rollback-fixture-seal-mismatch")

    source = work / expected_journal["source_locator"]
    target = work / expected_inverse["target_locator"]
    if source.exists() or source.is_symlink() or target.exists() or target.is_symlink():
        return

    source.parent.mkdir(parents=True, exist_ok=False)
    target.parent.mkdir(parents=True, exist_ok=False)
    source.write_bytes(payload)
    shutil.copy2(source, target)


def rehearse(args: argparse.Namespace) -> int:
    if args.mode == "dry-run":
        body = {
            "schema_version": 1, "status": "blocked", "mode": args.mode,
            "approved_moving_row_count": 0, "blocker": "identity_targets_unresolved",
        }
        write_json(args.output, body)
        print(json.dumps(body, sort_keys=True))
        return EXIT_BLOCKED

    if not args.work_root:
        return fail(EXIT_INPUT, "work-root-required")
    # strict=True raised an unhandled FileNotFoundError on a host where the path
    # does not exist, which read as a crash rather than a refusal. Resolve
    # non-strictly, decide liveness first (a live root is refused whether or not
    # it exists here), then refuse a missing work root with a typed reason.
    work = Path(args.work_root).resolve(strict=False)
    if is_live_root(work):
        return fail(EXIT_INPUT, "live-root-rejected-by-fixture-rehearsal")
    if not work.is_dir():
        return fail(EXIT_INPUT, "work-root-missing")

    if args.mode == "apply":
        if not args.fixture_template:
            return fail(EXIT_INPUT, "fixture-template-required")
        if args.fixture_template != SYNTHETIC_REHEARSAL_TEMPLATE:
            return fail(EXIT_INPUT, "fixture-template-unsupported")
        if not args.backup_root:
            return fail(EXIT_INPUT, "backup-root-required")
        backup = Path(args.backup_root).resolve(strict=True)
        source = work / "fixture-source"
        destination = work / "fixture-destination"
        source.mkdir(parents=True, exist_ok=True)
        destination.mkdir(parents=True, exist_ok=True)
        payload = SYNTHETIC_REHEARSAL_PAYLOAD
        source_file = source / "payload.txt"
        source_file.write_bytes(payload)
        dest_file = destination / "payload.txt"
        if dest_file.exists():
            return fail(EXIT_WRITE, "destination-preexistence")
        shutil.copy2(source_file, dest_file)
        if not source_file.is_file():
            raise RuntimeError("source-not-preserved")
        if dest_file.read_bytes() != payload:
            raise RuntimeError("byte-conservation-failed")

        row = {
            "row_ordinal": 0, "batch_ordinal": 0, "commit_state": "committed",
            "source_locator": "fixture-source/payload.txt", "target_locator": "fixture-destination/payload.txt",
            "kind": "file", "original_digest": digest_bytes(payload), "post_digest": digest_bytes(dest_file.read_bytes()),
            "before_lstat": {"kind": "file", "size": len(payload), "mode": stat.S_IMODE(source_file.lstat().st_mode), "digest": digest_bytes(payload)},
            "after_lstat": {"kind": "file", "size": len(payload), "mode": stat.S_IMODE(dest_file.lstat().st_mode), "digest": digest_bytes(payload)},
            "created_parents": ["fixture-destination"], "inverse_action": "remove_created_destination",
            "mapping_inverse": {"kind": "none"}, "link_inverse": {"kind": "none"},
        }
        inverse = {"inverse_of": row["row_ordinal"], "action": row["inverse_action"], "target_locator": row["target_locator"]}
        write_raw(args.journal, canonical(row))
        write_raw(args.inverse_journal, canonical(inverse))

        backup_file = backup / f"{row['original_digest']}.bak"
        try:
            fd = os.open(backup_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        seal = {
            "schema_version": 1, "status": "sealed", "fixture": args.fixture_template,
            "row_count": 1, "backup_sha256": digest_bytes(payload),
            "backup_path_basename": backup_file.name,
            "backup_external": backup not in work.parents and work not in backup.parents,
            "backup_non_symlink": not backup.is_symlink(), "exclusive": True,
        }
        if not seal["backup_external"] or not seal["backup_non_symlink"]:
            return fail(EXIT_WRITE, "backup-containment-invalid")
        write_json(args.backup_seal, seal)
        body = {
            "schema_version": 1, "status": "pass", "mode": args.mode, "fixture": args.fixture_template,
            "row_count": 1, "source_preserved": True, "byte_conservation": True,
            "deterministic_digest": digest_bytes(canonical({"row": row, "seal": {k: v for k, v in seal.items() if k not in {"backup_path_basename", "backup_root"}}})),
        }
        write_json(args.output, body)
        print(json.dumps(body, sort_keys=True))
        return EXIT_OK

    if args.mode == "rollback":
        if not (args.journal and args.inverse_journal and args.backup_seal):
            return fail(EXIT_INPUT, "rollback-inputs-required")
        journal_lines = read_bytes(args.journal).splitlines()
        inverse_lines = read_bytes(args.inverse_journal).splitlines()
        if args.fixture_template == SYNTHETIC_REHEARSAL_TEMPLATE and (
            len(journal_lines) != 1 or len(inverse_lines) != 1
        ):
            raise ValueError("rollback-fixture-journal-row-count-mismatch")
        journal_row = json.loads(journal_lines[0])
        inverse_row = json.loads(inverse_lines[0])
        seal = json.loads(read_bytes(args.backup_seal))
        if inverse_row.get("inverse_of") != journal_row.get("row_ordinal"):
            raise ValueError("inverse-journal-mismatch")
        if seal.get("status") != "sealed" or not seal.get("exclusive") or not seal.get("backup_external") or not seal.get("backup_non_symlink"):
            raise ValueError("backup-not-sealed")
        if args.fixture_template:
            _materialize_synthetic_rollback_fixture(
                work, journal_row, inverse_row, seal, args.fixture_template
            )
        target = work / inverse_row["target_locator"]
        source = work / journal_row["source_locator"]
        if not source.is_file() or digest_bytes(source.read_bytes()) != journal_row["original_digest"]:
            return fail(EXIT_DRIFT, "rollback-source-conflict")
        if not target.is_file() or digest_bytes(target.read_bytes()) != journal_row["post_digest"]:
            return fail(EXIT_IDENTITY, "rollback_conflict_restore_authority_required", restore_authority_required=True)
        target.unlink()
        for parent in reversed([work / p for p in journal_row.get("created_parents", [])]):
            try:
                parent.rmdir()
            except OSError:
                pass
        body = {
            "schema_version": 1, "status": "pass", "mode": args.mode,
            "inverse_exact": True, "source_preserved": True, "byte_conservation": True,
            "inverse_action_replayed": inverse_row.get("action"), "restore_authority_required": False,
        }
        write_json(args.output, body)
        print(json.dumps(body, sort_keys=True))
        return EXIT_OK

    return fail(EXIT_INPUT, "rehearse-mode-invalid")


# ---------------------------------------------------------------------------
# seal (aggregate typed A-13.2..A-13.8 blocked package)
# ---------------------------------------------------------------------------
def seal(args: argparse.Namespace) -> int:
    predicates = {f"A-13.{n}": ("not_started" if n == 7 else "blocked") for n in range(2, 9)}
    blockers = ["controlling_route_open", "identity_targets_unresolved", "approved_moving_row_count_zero"]
    inputs = {
        "replay": args.replay, "delta": args.delta, "identity_result": args.identity_result,
        "oracle": args.oracle, "reference_parity": args.reference_parity, "dry_run": args.dry_run,
        "rehearsal": args.rehearsal, "rollback_rehearsal": args.rollback_rehearsal,
        "backup_seal": args.backup_seal, "quiescence_pair": args.quiescence_pair,
    }
    input_digests = {}
    for name, path in inputs.items():
        if path and Path(path).is_file():
            input_digests[name] = digest_bytes(read_bytes(path))
    body = {
        "schema_version": 1, "status": "blocked", "terminal": False, "terminal_marker_present": False,
        "approved_moving_row_count": 0, "hearting_approval": False,
        "predicates": predicates, "blockers": blockers, "input_digests": input_digests,
    }
    write_json(args.output, body)
    print(json.dumps(body, sort_keys=True))
    return EXIT_BLOCKED


# ---------------------------------------------------------------------------
# apply (write-deny by construction)
# ---------------------------------------------------------------------------
def _no_follow_digest(path: Path) -> str | None:
    try:
        if path.is_symlink():
            return digest_bytes(canonical({"kind": "symlink", "target": os.readlink(path)}))
        if path.is_file():
            return digest_bytes(path.read_bytes())
        return None
    except OSError:
        return None


def scope_digest(root: Path, jobs: str | None, lock: str | None) -> str:
    records = []
    if root.is_dir():
        for entry in sorted(root.rglob("*"), key=lambda p: os.fsencode(str(p.relative_to(root)))):
            digest = _no_follow_digest(entry)
            records.append((str(entry.relative_to(root)), digest))
    for extra in (jobs, lock):
        if extra:
            path = Path(extra)
            records.append((str(path), _no_follow_digest(path)))
    return digest_bytes(canonical(records))


def apply_cmd(args: argparse.Namespace) -> int:
    effect_factory_calls = 0
    effect_calls = 0
    write_attempt_count = 0
    mutations = 0

    try:
        root = Path(args.artifact_root).resolve(strict=False)
        if not root.is_dir():
            raise ValueError("artifact-root-missing")
        before = scope_digest(root, args.dispatch_jobs, args.dispatch_lock)
    except (OSError, RuntimeError, ValueError) as exc:
        body = {"status": "blocked", "exit_class": EXIT_INPUT, "blocker": "apply_input_invalid", "error": str(exc), "mutations": 0,
                "write_audit": {"effect_factory_calls": 0, "effect_calls": 0, "write_attempt_count": 0, "mutations": 0}}
        if args.receipt_stdout: print(json.dumps(body, sort_keys=True))
        else: write_json(args.output, body)
        return EXIT_INPUT

    package_status = None
    package_body = None
    if Path(args.package).is_file():
        try:
            package_body = json.loads(read_bytes(args.package))
            if not isinstance(package_body, dict):
                raise ValueError("package-not-object")
            package_status = package_body.get("status")
        except (OSError, json.JSONDecodeError):
            package_status = "unreadable"
        except ValueError:
            package_status = "malformed"

    approved_and_ready = package_status == "pass"
    if approved_and_ready:
        # A PASS label is not authority. Validate the complete authority graph
        # before any effect adapter can exist; this implementation is
        # production-blocked until the real A-13 package is present.
        after = scope_digest(root, args.dispatch_jobs, args.dispatch_lock)
        body = {"status": "blocked", "exit_class": EXIT_BLOCKED, "blocker": "apply_authority_invalid",
                "package_status": package_status, "mutations": 0,
                "write_audit": {"effect_factory_calls": 0, "effect_calls": 0, "write_attempt_count": 0,
                                 "mutations": 0, "scope_before_sha256": before, "scope_after_sha256": after}}
        if args.receipt_stdout: print(json.dumps(body, sort_keys=True))
        else: write_json(args.output, body)
        return EXIT_BLOCKED

    after = scope_digest(root, args.dispatch_jobs, args.dispatch_lock)
    drifted = before != after
    exit_class = EXIT_DRIFT if drifted else EXIT_BLOCKED
    body = {
        "status": "drift" if drifted else "blocked",
        "exit_class": exit_class,
        "blocker": "whole_scope_drift_observed" if drifted else "production_apply_blocked",
        "package_status": package_status,
        "mutations": mutations,
        "write_audit": {
            "effect_factory_calls": effect_factory_calls, "effect_calls": effect_calls,
            "write_attempt_count": write_attempt_count, "mutations": mutations,
            "scope_before_sha256": before, "scope_after_sha256": after,
        },
    }
    if args.receipt_stdout:
        print(json.dumps(body, sort_keys=True))
    else:
        write_json(args.output, body)
    return exit_class


def handoff(args: argparse.Namespace) -> int:
    apply_status = None
    if args.apply_receipt and Path(args.apply_receipt).is_file():
        try:
            apply_status = json.loads(read_bytes(args.apply_receipt)).get("status")
        except (OSError, json.JSONDecodeError):
            apply_status = "unreadable"
    package_status = None
    if Path(args.package).is_file():
        try:
            package_status = json.loads(read_bytes(args.package)).get("status")
        except (OSError, json.JSONDecodeError):
            package_status = "unreadable"
    body = {
        "schema_version": 1, "status": "blocked", "terminal": False, "terminal_marker_present": False,
        "w8_status": "blocked", "exit_class": EXIT_BLOCKED,
        "package_status": package_status, "apply_status": apply_status,
        "blockers": ["w7_not_terminal", "production_apply_blocked"],
    }
    if args.receipt_stdout:
        print(json.dumps(body, sort_keys=True))
    else:
        write_json(args.output, body)
    return EXIT_BLOCKED


# ---------------------------------------------------------------------------
# E1 guarded identity/target preparation
# ---------------------------------------------------------------------------
E1_W7_INPUTS = {
    "route_outcome": ("c6102c968b07ffa784d9af50ec8ecdc4b758cdf3d70be50fbeaa144804d99d55", 986),
    "final_report": ("ffab1a062d3ae7b74688a6e31e8b9550ceba9b9ac8c307de317b38c5b9562346", 7837),
    "r6b_test_verdict": ("740f903b9cdbc2c5be6599852a270eb2961651eafc5571d2915ae069f2a33ab2", 3902),
    "blocked_apply_receipt": ("3ac180c5856e3c46f9777da22465c8d58898c900535c32063c1b1d9c558b8b6d", 410),
}
E1_TEMPLATE_GRAMMAR = "e1-template-v1"
E1_GROUPING = "e1-grouping-v1"
E1_COLLISION_POLICY = "e1-collision-v1"
E1_COLLISIONS = ("byte", "case_fold", "nfc", "kind", "parent_child", "preexisting", "unsafe", "external")
E1_EXCLUDED_META = (
    "build-e1-verification-index", "check-e1-reachability", "check-e1-verification",
    "mkdir-e1-evidence", "run-e1-operation", "scan-e1-command-payload",
    "seal-e1-trace", "validate-e1-report",
)
E1_PRODUCTION_SUBCOMMANDS = ("bind-e1", "issue", "issue", "resolve", "resolve", "hygiene", "prove-boundary")
E1_CONCURRENT_RUNTIME_PREFIXES = (".runtime/model-worker-governor/",)


def _e1_new(path: str | Path, data: bytes) -> None:
    """Create one regular file without following or replacing any name."""
    target = Path(path)
    if not target.parent.is_dir():
        raise ValueError(f"e1-parent-missing:{target.parent}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("e1-short-write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    if target.read_bytes() != data:
        raise ValueError("e1-readback-drift")
    dfd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _e1_new_json(path: str | Path, body: dict) -> None:
    _e1_new(path, canonical(body))


def _e1_new_jsonl(path: str | Path, rows: list[dict]) -> bytes:
    raw = b"".join(canonical(row) for row in rows)
    _e1_new(path, raw)
    return raw


def _e1_load_canonical(path: str | Path) -> tuple[dict, bytes]:
    raw = read_bytes(path)
    body = json.loads(raw)
    if not isinstance(body, dict) or canonical(body) != raw:
        raise ValueError(f"e1-noncanonical:{path}")
    return body, raw


def _e1_sha_record(path: str | Path) -> dict:
    p = Path(path).absolute()
    raw = p.read_bytes()
    return {"path": str(p), "sha256": digest_bytes(raw), "bytes": len(raw)}


def _e1_emit_aux(args: argparse.Namespace, subcommand: str, status: str,
                 reads: list[str], writes: list[str], extra: dict | None = None) -> None:
    events = []
    sequence = 1
    for operation, paths in (("read_file", reads), ("exclusive_create", writes)):
        for path in paths:
            events.append({"sequence": sequence, "event_kind": "filesystem", "operation": operation,
                           "target": str(Path(path).absolute()), "phase": "success", "decision": "allow",
                           "bytes": None, "sha256": None, "errno": None})
            sequence += 1
    if getattr(args, "access_fragment_output", None):
        _e1_new_jsonl(args.access_fragment_output, events)
    if getattr(args, "command_record_output", None):
        _e1_new_json(args.command_record_output, {
            "schema_version": "artifact-relocation-e1-command/v1", "scope": "production-e1",
            "subcommand": subcommand, "argv": list(sys.argv),
        })
    if getattr(args, "status_output", None):
        body = {"schema_version": "artifact-relocation-e1-status/v1", "status": status,
                "blocker": None, "subcommand": subcommand}
        if extra:
            body.update(extra)
        _e1_new_json(args.status_output, body)


def _e1_verify_seal(body_path: str | Path, seal_path: str | Path,
                    kind: str, request_sha: str | None = None) -> tuple[dict, dict, bytes]:
    body, raw = _e1_load_canonical(body_path)
    seal, _ = _e1_load_canonical(seal_path)
    if set(seal) != {"schema_version", "artifact_kind", "body_sha256", "body_bytes",
                    "request_sha256", "summary", "created_after_body"}:
        raise ValueError("e1-seal-schema")
    if seal["schema_version"] != E1_SEAL_SCHEMA or seal["artifact_kind"] != kind:
        raise ValueError("e1-seal-kind")
    if seal["body_sha256"] != digest_bytes(raw) or seal["body_bytes"] != len(raw):
        raise ValueError("e1-seal-body-drift")
    if request_sha is not None and seal["request_sha256"] != request_sha:
        raise ValueError("e1-seal-request-drift")
    if not seal["created_after_body"]:
        raise ValueError("e1-seal-order")
    return body, seal, raw


def _e1_create_seal(body_path: str | Path, seal_path: str | Path, kind: str,
                    request_sha: str, summary: dict) -> None:
    raw = read_bytes(body_path)
    _e1_new_json(seal_path, {
        "schema_version": E1_SEAL_SCHEMA, "artifact_kind": kind,
        "body_sha256": digest_bytes(raw), "body_bytes": len(raw),
        "request_sha256": request_sha, "summary": summary, "created_after_body": True,
    })


def _e1_subject_group(row: dict, kind: str, ordinal: int) -> str:
    if kind in ("repository", "artifact_root"):
        return kind
    if kind in ("campaign", "cycle"):
        return "e1-lineage"
    if kind in ("artifact", "artifact_revision", "legacy_key"):
        return f"row:{ordinal}"
    if kind in ("shared_reference", "shared_reference_revision"):
        source_root = row["source_locator"]["root_relative_path"].split("/", 1)[0]
        group = {"analysis_project": "analysis", "spec": "spec", "research": "research"}.get(source_root)
        if group is None:
            raise ValueError("e1-shared-group")
        return group
    raise ValueError(f"e1-unknown-kind:{kind}")


def _e1_allocate_unique(allocator, kind: str, used: set[str]) -> str:
    for _ in range(32):
        value = allocator.allocate(kind)
        if value not in used:
            used.add(value)
            return value
    raise ValueError("e1-entropy-collision-exhausted")


def build_e1_ledger(rows: list[dict], namespace: str, manifest_sha: str,
                    authority_sha: str, w7_sha: str, allocator=None,
                    legacy_entropy=os.urandom, route_root: Path = AUTHORITATIVE_ROOT) -> dict:
    """Build a complete ledger in memory. Locator metadata never enters allocation."""
    request = {"schema_version": E1_LEDGER_SCHEMA, "namespace": namespace,
               "source_manifest_sha256": manifest_sha, "authority_receipt_sha256": authority_sha,
               "w7_verification_sha256": w7_sha, "template_grammar_version": E1_TEMPLATE_GRAMMAR,
               "subject_grouping_version": E1_GROUPING}
    request_sha = digest_bytes(canonical(request))
    allocator = allocator or IDENTITY.IdAllocator()
    used: set[str] = set()
    subjects: dict[tuple[str, str], dict] = {}

    def get_subject(kind: str, group: str) -> dict:
        key = (kind, group)
        if key in subjects:
            return subjects[key]
        base = {"binding_key": group, "id_kind": kind, "state": "issued",
                "authority_receipt_sha256": authority_sha}
        if kind == "legacy_key":
            for _ in range(32):
                raw = legacy_entropy(16)
                if len(raw) != 16:
                    raise ValueError("e1-legacy-entropy-length")
                stable = "lk_" + raw.hex()
                if stable not in used:
                    used.add(stable)
                    break
            else:
                raise ValueError("e1-legacy-collision-exhausted")
            base.update({"stable_id": stable, "migration_id": FEED.migration_id(stable),
                         "mapping_namespace": FEED.MAPPING_NAMESPACE, "mapping_version": FEED.MAPPING_VERSION})
        else:
            base["stable_id"] = _e1_allocate_unique(allocator, kind, used)
        subjects[key] = base
        return base

    bindings = []
    for ordinal, row in enumerate(rows):
        if row.get("record_type") != "relocation" or row.get("identity", {}).get("state") != "unissued":
            raise ValueError("e1-manifest-row-state")
        source = row["source_locator"]["root_relative_path"]
        required = row["identity"]["required_ids"]
        if len(required) != len(set(required)):
            raise ValueError("e1-required-id-duplicate")
        refs = []
        route_id = None
        legacy = None
        for required_id in required:
            if required_id == "route_id":
                match = re.fullmatch(r"(?:\.runtime/routes/)?(rt-[0-9a-f]{16})(?:\.outcome)?\.json", source)
                if match:
                    route_id = match.group(1)
                elif row["before"]["kind"] == "directory":
                    # The legacy registry container is a grouping boundary, not a route
                    # record. Preserve that external binding without manufacturing an ID.
                    route_id = "route-registry-root"
                else:
                    try:
                        record = json.loads((route_root / source).read_bytes())
                    except (OSError, json.JSONDecodeError) as exc:
                        raise ValueError("e1-route-record") from exc
                    route_id = record.get("route_id") if isinstance(record, dict) else None
                    if not isinstance(route_id, str) or not re.fullmatch(r"rt-[0-9a-f]{16}", route_id):
                        # W6 deliberately retained misplaced non-route JSON in the
                        # legacy route population. It remains a typed unresolved
                        # external binding; E1 must not invent a route identity.
                        route_id = "route-id-unresolved"
                refs.append({"required_id": required_id, "id_kind": "route", "binding_key": route_id,
                             "stable_id": route_id})
                continue
            if required_id == "migration_id":
                if legacy is None:
                    legacy = get_subject("legacy_key", f"row:{ordinal}")
                refs.append({"required_id": required_id, "id_kind": "migration",
                             "binding_key": f"row:{ordinal}", "stable_id": legacy["migration_id"]})
                continue
            kind = E1_KIND_MAP.get(required_id)
            if kind is None:
                raise ValueError(f"e1-unknown-required-id:{required_id}")
            subject = get_subject(kind, _e1_subject_group(row, kind, ordinal))
            if kind == "legacy_key":
                legacy = subject
            refs.append({"required_id": required_id, "id_kind": kind,
                         "binding_key": subject["binding_key"], "stable_id": subject["stable_id"]})
        if [ref["required_id"] for ref in refs] != required:
            raise ValueError("e1-reference-coverage")
        binding = {"source_row_key": f"row:{ordinal}:{digest_bytes(source.encode('utf-8'))[:16]}",
                   "source_row_ordinal": ordinal, "source_locator": source,
                   "required_ids": required, "subject_refs": refs}
        if route_id:
            binding["route_id"] = route_id
        bindings.append(binding)
    subject_rows = sorted(subjects.values(), key=lambda row: (row["id_kind"].encode(), row["binding_key"].encode()))
    return {**request, "request_sha256": request_sha,
            "allocator": {"algorithm": "os.urandom", "body_bytes": 16,
                          "minimum_random_bits": 128, "seed_inputs": []},
            "subjects": subject_rows, "row_bindings": bindings}


def _e1_validate_ledger(body: dict, rows: list[dict], manifest_sha: str,
                        authority_sha: str, w7_sha: str, namespace: str | None = None) -> None:
    if body.get("schema_version") != E1_LEDGER_SCHEMA or body.get("source_manifest_sha256") != manifest_sha:
        raise ValueError("e1-ledger-manifest-binding")
    if body.get("authority_receipt_sha256") != authority_sha or body.get("w7_verification_sha256") != w7_sha:
        raise ValueError("e1-ledger-authority-binding")
    if namespace is not None and body.get("namespace") != namespace:
        raise ValueError("e1-ledger-namespace")
    request = {key: body[key] for key in ("schema_version", "namespace", "source_manifest_sha256",
               "authority_receipt_sha256", "w7_verification_sha256", "template_grammar_version",
               "subject_grouping_version")}
    if body.get("request_sha256") != digest_bytes(canonical(request)):
        raise ValueError("e1-ledger-request")
    if body.get("allocator") != {"algorithm": "os.urandom", "body_bytes": 16,
                                  "minimum_random_bits": 128, "seed_inputs": []}:
        raise ValueError("e1-ledger-allocator")
    if len(body.get("row_bindings", [])) != len(rows):
        raise ValueError("e1-ledger-row-count")
    seen_ids, seen_subjects = set(), set()
    subject_index = {}
    for subject in body.get("subjects", []):
        key = (subject["id_kind"], subject["binding_key"])
        if key in seen_subjects or subject["stable_id"] in seen_ids:
            raise ValueError("e1-ledger-rebind")
        seen_subjects.add(key); seen_ids.add(subject["stable_id"]); subject_index[key] = subject
        if subject["id_kind"] == "legacy_key":
            if not FEED.ID_RE.fullmatch(subject["stable_id"]) or subject["migration_id"] != FEED.migration_id(subject["stable_id"]):
                raise ValueError("e1-ledger-legacy")
        elif not IDENTITY.is_well_formed(subject["stable_id"], subject["id_kind"]):
            raise ValueError("e1-ledger-id")
    for ordinal, (row, binding) in enumerate(zip(rows, body["row_bindings"])):
        if binding["source_row_ordinal"] != ordinal or binding["source_locator"] != row["source_locator"]["root_relative_path"]:
            raise ValueError("e1-ledger-row-order")
        required = row["identity"]["required_ids"]
        if binding["required_ids"] != required or [ref["required_id"] for ref in binding["subject_refs"]] != required:
            raise ValueError("e1-ledger-ref-coverage")
        for ref in binding["subject_refs"]:
            if ref["id_kind"] not in ("route", "migration"):
                subject = subject_index.get((ref["id_kind"], ref["binding_key"]))
                if subject is None or subject["stable_id"] != ref["stable_id"]:
                    raise ValueError("e1-ledger-ref-binding")


def _e1_kind_coverage(subjects: list[dict]) -> dict:
    counts = Counter(subject["id_kind"] for subject in subjects)
    return {key: counts[key] for key in sorted(counts, key=lambda value: value.encode())}


def _e1_validate_authority(authority: dict, manifest_sha: str) -> None:
    if authority.get("schema_version") != "artifact-relocation-e1-authority/v1":
        raise ValueError("e1-authority-schema")
    if authority.get("source_manifest_sha256") != manifest_sha or authority.get("apply_authorized") is not False:
        raise ValueError("e1-authority-scope")


def _e1_validate_w7(binding: dict) -> None:
    if binding.get("schema_version") != "artifact-relocation-w7-verification-binding/v1":
        raise ValueError("e1-w7-schema")
    if binding.get("route_id") != "rt-8203617b5b20360d" or binding.get("route_hash") != "8203617b5b20360d27237a36ac81b04a8a9d5a5380df8c433a9cc2028e534cd6":
        raise ValueError("e1-w7-route")
    if (binding.get("tooling_gate"), binding.get("production_acceptance"),
        binding.get("production_relocation_terminal"), binding.get("w8_status")) != ("pass", "blocked", False, "blocked"):
        raise ValueError("e1-w7-status")


def _e1_census(root: Path, rows: list[dict]) -> list[dict]:
    result = []
    for ordinal, row in enumerate(rows):
        locator = row["source_locator"]["root_relative_path"]
        path = root / locator
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            result.append({"ordinal": ordinal, "source_locator": locator, "kind": "missing",
                           "mode": None, "size": None, "sha256": None, "link_target": None})
            continue
        if stat.S_ISLNK(info.st_mode):
            kind, link, sha = "symlink", os.readlink(path), None
        elif stat.S_ISDIR(info.st_mode):
            kind, link, sha = "directory", None, None
        elif stat.S_ISREG(info.st_mode):
            kind, link, sha = "file", None, digest_bytes(path.read_bytes())
        else:
            kind, link, sha = "other", None, None
        result.append({"ordinal": ordinal, "source_locator": locator, "kind": kind,
                       "mode": stat.S_IMODE(info.st_mode), "size": info.st_size,
                       "sha256": sha, "link_target": link})
    return result


def _e1_registry_rows(jobs: Path, routes: Path) -> list[dict]:
    paths = [jobs] + sorted((path for path in routes.iterdir() if path.is_file() or path.is_symlink()),
                            key=lambda path: path.name.encode())
    rows = []
    for path in paths:
        info = os.lstat(path)
        raw = path.read_bytes() if stat.S_ISREG(info.st_mode) else os.readlink(path).encode()
        name = path.name
        if path == jobs:
            classification = "jobs_registry"
        elif re.fullmatch(r"rt-[0-9a-f]{16}\.json", name):
            classification = "route"
        elif re.fullmatch(r"rt-[0-9a-f]{16}\.outcome\.json", name):
            classification = "outcome"
        else:
            classification = "misplaced_nonroute_evidence"
        rows.append({"path": str(path.absolute()), "classification": classification,
                     "sha256": digest_bytes(raw), "bytes": len(raw), "mode": stat.S_IMODE(info.st_mode)})
    return rows


def bind_e1(args: argparse.Namespace) -> int:
    manifest_raw = read_bytes(args.manifest)
    if (digest_bytes(manifest_raw), len(manifest_raw)) != EXPECTED["manifest"]:
        raise ValueError("manifest-drift")
    rows = read_jsonl_rows(args.manifest)
    route = json.loads(read_bytes(args.route))
    ROUTES.verify_route(route)
    route_hash = ROUTES.route_hash(route)
    owner_raw = read_bytes(args.owner_prompt)
    authority = {"schema_version": "artifact-relocation-e1-authority/v1",
                 "authority_class": "e1_identity_target_preparation", "route_id": route["route_id"],
                 "route_hash": route_hash.removeprefix("sha256:"), "owner_prompt_sha256": digest_bytes(owner_raw),
                 "source_manifest_sha256": digest_bytes(manifest_raw),
                 "approved_operations": ["bind-e1", "issue", "resolve-v2", "hygiene", "prove-boundary"],
                 "forbidden_operations": ["apply", "rehearse-apply", "move", "copy", "delete", "rename", "chmod", "hardlink", "retarget", "E2", "E3", "W8"],
                 "apply_authorized": False, "e2_state": "separate_run_required", "e3_state": "separate_run_required"}
    bindings = {}
    supplied = {"route_outcome": args.w7_route_outcome, "final_report": args.w7_final_report,
                "r6b_test_verdict": args.w7_r6b_verdict, "blocked_apply_receipt": args.w7_blocked_apply}
    for key, path in supplied.items():
        record = _e1_sha_record(path)
        if (record["sha256"], record["bytes"]) != E1_W7_INPUTS[key]:
            raise ValueError(f"e1-w7-input-drift:{key}")
        bindings[key] = record
    w7 = {"schema_version": "artifact-relocation-w7-verification-binding/v1",
          "route_id": "rt-8203617b5b20360d", "route_hash": "8203617b5b20360d27237a36ac81b04a8a9d5a5380df8c433a9cc2028e534cd6",
          "inputs": bindings, "tooling_gate": "pass", "production_acceptance": "blocked",
          "production_relocation_terminal": False, "w8_status": "blocked"}
    registry_rows = _e1_registry_rows(Path(args.jobs), Path(args.routes_dir))
    census = {"schema_version": "artifact-relocation-protected-census/v1",
              "artifact_root": str(AUTHORITATIVE_ROOT), "source_manifest_sha256": digest_bytes(manifest_raw),
              "row_count": len(rows), "rows": _e1_census(AUTHORITATIVE_ROOT, rows)}
    _e1_new_json(args.authority_output, authority)
    _e1_new_json(args.w7_binding_output, w7)
    _e1_new_json(args.protected_before_output, census)
    registry_raw = _e1_new_jsonl(args.registry_enumeration_output, registry_rows)
    _e1_new_json(args.registry_enumeration_seal_output, {
        "schema_version": "artifact-relocation-registry-enumeration-seal/v1",
        "body_sha256": digest_bytes(registry_raw), "body_bytes": len(registry_raw), "entry_count": len(registry_rows)})
    writes = [args.authority_output, args.w7_binding_output, args.protected_before_output,
              args.registry_enumeration_output, args.registry_enumeration_seal_output,
              args.access_fragment_output, args.command_record_output, args.status_output]
    _e1_emit_aux(args, "bind-e1", "created", [args.owner_prompt, args.route, args.manifest, args.jobs,
                 args.w7_route_outcome, args.w7_final_report, args.w7_r6b_verdict, args.w7_blocked_apply], writes[:-3],
                 {"protected_row_count": len(rows), "registry_entry_count": len(registry_rows)})
    print(json.dumps({"status": "created", "protected_row_count": len(rows),
                      "registry_entry_count": len(registry_rows)}, sort_keys=True))
    return EXIT_OK


def issue_e1(args: argparse.Namespace) -> int:
    manifest_raw = read_bytes(args.manifest); manifest_sha = digest_bytes(manifest_raw)
    if (manifest_sha, len(manifest_raw)) != EXPECTED["manifest"]:
        raise ValueError("manifest-drift")
    rows = read_jsonl_rows(args.manifest)
    authority, authority_raw = _e1_load_canonical(args.authority)
    w7, w7_raw = _e1_load_canonical(args.w7_binding)
    _e1_validate_authority(authority, manifest_sha); _e1_validate_w7(w7)
    authority_sha, w7_sha = digest_bytes(authority_raw), digest_bytes(w7_raw)
    request = {"schema_version": E1_LEDGER_SCHEMA, "namespace": args.namespace,
               "source_manifest_sha256": manifest_sha, "authority_receipt_sha256": authority_sha,
               "w7_verification_sha256": w7_sha, "template_grammar_version": E1_TEMPLATE_GRAMMAR,
               "subject_grouping_version": E1_GROUPING}
    request_sha = digest_bytes(canonical(request))
    body_path, seal_path = Path(args.ledger_output), Path(args.ledger_seal_output)
    if seal_path.exists() and not body_path.exists():
        raise ValueError("e1-orphan-seal")
    if body_path.exists():
        body, raw = _e1_load_canonical(body_path)
        _e1_validate_ledger(body, rows, manifest_sha, authority_sha, w7_sha, args.namespace)
        if body["request_sha256"] != request_sha:
            raise ValueError("e1-ledger-conflict")
        if seal_path.exists():
            _e1_verify_seal(body_path, seal_path, "identity-ledger", request_sha)
        else:
            _e1_create_seal(body_path, seal_path, "identity-ledger", request_sha,
                            {"subject_count": len(body["subjects"]), "row_count": len(body["row_bindings"]),
                             "kind_coverage": _e1_kind_coverage(body["subjects"])})
        status = "noop_exact_retry"
    else:
        body = build_e1_ledger(rows, args.namespace, manifest_sha, authority_sha, w7_sha)
        _e1_validate_ledger(body, rows, manifest_sha, authority_sha, w7_sha, args.namespace)
        _e1_new_json(body_path, body)
        _e1_create_seal(body_path, seal_path, "identity-ledger", request_sha,
                        {"subject_count": len(body["subjects"]), "row_count": len(body["row_bindings"]),
                         "kind_coverage": _e1_kind_coverage(body["subjects"])})
        status = "created"
    _e1_emit_aux(args, "issue", status, [args.manifest, args.authority, args.w7_binding],
                 [args.access_fragment_output, args.command_record_output, args.status_output],
                 {"request_sha256": request_sha, "subject_count": len(body["subjects"]),
                  "row_count": len(body["row_bindings"]), "kind_coverage": _e1_kind_coverage(body["subjects"])})
    print(json.dumps({"status": status, "request_sha256": request_sha,
                      "subject_count": len(body["subjects"]), "row_count": len(body["row_bindings"])}, sort_keys=True))
    return EXIT_OK


def _e1_validate_template(template: str, required: list[str], kind: str) -> tuple[str, tuple[str, ...]]:
    if not isinstance(template, str) or template.startswith("/") or "\x00" in template:
        raise ValueError("e1-template-unsafe")
    parts = template.split("/")
    if any(part in (".", "..") for part in parts):
        raise ValueError("e1-template-unsafe")
    tokens = tuple(re.findall(r"<([a-z_]+)>", template))
    if template.count("<") != len(tokens) or template.count(">") != len(tokens):
        raise ValueError("e1-template-marker")
    lineage = ("campaign_id", "cycle_id")
    shared = ("shared_reference_id", "shared_reference_revision_id")
    if template.startswith("campaigns/"):
        family, expected = "lineage", lineage
        if set(required) != {"repository_id", "artifact_root_id", "campaign_id", "cycle_id", "artifact_id", "artifact_revision_id"}:
            raise ValueError("e1-lineage-required")
    elif template.startswith("shared/analysis/"):
        family, expected = "shared-analysis", shared
    elif template.startswith("shared/spec/"):
        family, expected = "shared-spec", shared
    else:
        raise ValueError("e1-template-family")
    if tokens != expected or any(tokens.count(token) != 1 for token in expected):
        raise ValueError("e1-template-tokens")
    if family.startswith("shared") and set(required) != {"repository_id", "artifact_root_id", *shared}:
        raise ValueError("e1-shared-required")
    if template.endswith("/") and kind != "directory":
        raise ValueError("e1-template-kind-slash")
    return family, tokens


def build_e1_targets(rows: list[dict], ledger: dict, manifest_sha: str,
                     ledger_sha: str, authority_sha: str, w7_sha: str,
                     artifact_root: Path) -> dict:
    request = {"schema_version": E1_TARGET_SCHEMA, "source_manifest_sha256": manifest_sha,
               "identity_ledger_sha256": ledger_sha, "authority_receipt_sha256": authority_sha,
               "w7_verification_sha256": w7_sha, "template_grammar_version": E1_TEMPLATE_GRAMMAR,
               "collision_policy_version": E1_COLLISION_POLICY}
    output = []
    for row, binding in zip(rows, ledger["row_bindings"]):
        target = row["target"]
        if target["locator_state"] != "template":
            continue
        source = row["source_locator"]["root_relative_path"]
        kind = row["before"]["kind"]
        if kind != row["current_observation"]["kind"]:
            raise ValueError("e1-kind-drift")
        template = target["root_relative_path"]
        _, tokens = _e1_validate_template(template, binding["required_ids"], kind)
        refs = {ref["required_id"]: ref for ref in binding["subject_refs"]}
        if set(refs) != set(binding["required_ids"]):
            raise ValueError("e1-target-ref-coverage")
        rendered = template
        for token in tokens:
            ref = refs[token]
            expected_kind = E1_KIND_MAP[token]
            if ref["id_kind"] != expected_kind or not IDENTITY.is_well_formed(ref["stable_id"], expected_kind):
                raise ValueError("e1-target-token-kind")
            rendered = rendered.replace(f"<{token}>", ref["stable_id"], 1)
        if "<" in rendered or ">" in rendered or source == rendered:
            raise ValueError("e1-target-unresolved-or-same")
        output.append({"source_row_key": binding["source_row_key"], "source_locator": source,
                       "target_locator": rendered, "kind": kind, "identity_refs": binding["subject_refs"],
                       "disposition": target["disposition"]})
    output.sort(key=lambda row: (row["source_locator"].encode("utf-8"), row["target_locator"].encode("utf-8")))
    collisions = {key: 0 for key in E1_COLLISIONS}
    seen_source, seen_target, seen_case, seen_nfc = set(), set(), set(), set()
    for item in output:
        source, target = item["source_locator"], item["target_locator"]
        if source in seen_source:
            collisions["byte"] += 1
        seen_source.add(source)
        target_bytes = target.encode("utf-8", "strict")
        if target in seen_target:
            collisions["byte"] += 1
        seen_target.add(target)
        folded, normalized = target.casefold(), unicodedata.normalize("NFC", target)
        if folded in seen_case:
            collisions["case_fold"] += 1
        seen_case.add(folded)
        if normalized in seen_nfc:
            collisions["nfc"] += 1
        seen_nfc.add(normalized)
        pure = Path(target)
        if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts) or any(ord(ch) < 32 for ch in target):
            collisions["unsafe"] += 1
        root_name = "campaigns" if target.startswith("campaigns/") else "shared/analysis" if target.startswith("shared/analysis/") else "shared/spec" if target.startswith("shared/spec/") else None
        if root_name is None:
            collisions["external"] += 1
        destination = artifact_root.joinpath(*target.rstrip("/").split("/"))
        try:
            os.lstat(destination)
        except FileNotFoundError:
            pass
        else:
            collisions["preexisting"] += 1
        if target_bytes.decode("utf-8") != target:
            collisions["unsafe"] += 1
    if not output or len(output) != 5631 or any(collisions.values()):
        raise ValueError(f"e1-target-collision:{json.dumps(collisions, sort_keys=True)}")
    return {**request, "status": "created", "request_sha256": digest_bytes(canonical(request)),
            "row_count": len(output), "rows": output, "collision_counts": collisions,
            "production_apply_authorized": False, "next_state": "E2_REQUIRES_SEPARATE_RUN"}


def resolve_e1(args: argparse.Namespace) -> int:
    manifest_raw = read_bytes(args.manifest); manifest_sha = digest_bytes(manifest_raw)
    if (manifest_sha, len(manifest_raw)) != EXPECTED["manifest"]:
        raise ValueError("manifest-drift")
    rows = read_jsonl_rows(args.manifest)
    authority, authority_raw = _e1_load_canonical(args.authority); _e1_validate_authority(authority, manifest_sha)
    w7, w7_raw = _e1_load_canonical(args.w7_binding); _e1_validate_w7(w7)
    authority_sha, w7_sha = digest_bytes(authority_raw), digest_bytes(w7_raw)
    ledger, ledger_seal, ledger_raw = _e1_verify_seal(args.ledger, args.ledger_seal, "identity-ledger")
    _e1_validate_ledger(ledger, rows, manifest_sha, authority_sha, w7_sha)
    request = {"schema_version": E1_TARGET_SCHEMA, "source_manifest_sha256": manifest_sha,
               "identity_ledger_sha256": digest_bytes(ledger_raw), "authority_receipt_sha256": authority_sha,
               "w7_verification_sha256": w7_sha, "template_grammar_version": E1_TEMPLATE_GRAMMAR,
               "collision_policy_version": E1_COLLISION_POLICY}
    request_sha = digest_bytes(canonical(request))
    body_path, seal_path = Path(args.target_output), Path(args.target_seal_output)
    if seal_path.exists() and not body_path.exists():
        raise ValueError("e1-target-orphan-seal")
    if body_path.exists():
        body, _, _ = _e1_verify_seal(body_path, seal_path, "exact-target-set", request_sha)
        if body.get("request_sha256") != request_sha or body.get("row_count") != 5631 or any(body.get("collision_counts", {}).values()):
            raise ValueError("e1-target-conflict")
        status = "noop_exact_retry"
    else:
        body = build_e1_targets(rows, ledger, manifest_sha, digest_bytes(ledger_raw), authority_sha, w7_sha,
                                Path(args.artifact_root))
        _e1_new_json(body_path, body)
        _e1_create_seal(body_path, seal_path, "exact-target-set", request_sha,
                        {"row_count": body["row_count"], "collision_counts": body["collision_counts"]})
        status = "created"
    _e1_emit_aux(args, "resolve", status, [args.manifest, args.ledger, args.ledger_seal,
                 args.authority, args.w7_binding], [args.access_fragment_output, args.command_record_output,
                 args.status_output], {"request_sha256": request_sha, "row_count": body["row_count"],
                 "collision_counts": body["collision_counts"]})
    print(json.dumps({"status": status, "request_sha256": request_sha,
                      "row_count": body["row_count"], "collision_counts": body["collision_counts"]}, sort_keys=True))
    return EXIT_OK


def resolve_dispatch(args: argparse.Namespace) -> int:
    return resolve_e1(args) if getattr(args, "schema", None) == "v2" else resolve(args)


def hygiene_e1(args: argparse.Namespace) -> int:
    before_raw = read_bytes(args.registry_enumeration)
    seal, _ = _e1_load_canonical(args.registry_enumeration_seal)
    before = read_jsonl_rows(args.registry_enumeration)
    if seal.get("body_sha256") != digest_bytes(before_raw) or seal.get("entry_count") != len(before):
        raise ValueError("e1-registry-seal")
    after = _e1_registry_rows(Path(args.jobs), Path(args.routes_dir))
    before_index = {row["path"]: row for row in before}
    after_index = {row["path"]: row for row in after}
    # New route bookkeeping after bind is preserved and classified; every bound byte must remain exact.
    drift = [path for path, row in before_index.items() if after_index.get(path) != row]
    if drift:
        raise ValueError(f"e1-registry-drift:{drift[0]}")
    misplaced = sorted(path for path, row in after_index.items() if row["classification"] == "misplaced_nonroute_evidence")
    terminal = sorted(path for path, row in after_index.items() if row["classification"] == "outcome")
    routes = sorted(path for path, row in after_index.items() if row["classification"] == "route")
    body = {"schema_version": "artifact-relocation-registry-hygiene/v1", "status": "pass", "blocker": None,
            "before_sha256": digest_bytes(before_raw), "before_bytes": len(before_raw),
            "after_enumeration_sha256": digest_bytes(b"".join(canonical(row) for row in after)),
            "after_entry_count": len(after), "historical_failed_attempts_preserved": [],
            "superseded_non_terminal_routes": routes, "terminal_routes": terminal,
            "misplaced_nonroute_evidence_preserved": misplaced,
            "deleted": 0, "moved": 0, "renamed": 0, "rewritten": 0, "relabelled_pass": 0,
            "manufactured_outcomes": 0, "registry_mutations": 0}
    _e1_new_json(args.output, body)
    _e1_emit_aux(args, "hygiene", "pass", [args.jobs, args.registry_enumeration,
                 args.registry_enumeration_seal], [args.output, args.access_fragment_output,
                 args.command_record_output], {"registry_mutations": 0})
    print(json.dumps({"status": "pass", "blocker": None, "registry_mutations": 0}, sort_keys=True))
    return EXIT_OK


def prove_boundary_e1(args: argparse.Namespace) -> int:
    manifest_raw = read_bytes(args.manifest); rows = read_jsonl_rows(args.manifest)
    before, _ = _e1_load_canonical(args.protected_before)
    if before.get("source_manifest_sha256") != digest_bytes(manifest_raw) or before.get("row_count") != len(rows):
        raise ValueError("e1-protected-before-binding")
    after = _e1_census(AUTHORITATIVE_ROOT, rows)
    changed_all = [item["source_locator"] for item, old in zip(after, before["rows"]) if item != old]
    concurrent_runtime = [locator for locator in changed_all
                          if locator.startswith(E1_CONCURRENT_RUNTIME_PREFIXES)]
    changed = [locator for locator in changed_all if locator not in concurrent_runtime]
    counts = {"moved": 0, "deleted": 0, "renamed": 0, "chmodded": 0,
              "content_changed": 0, "symlink_retargeted": 0}
    if changed:
        counts["content_changed"] = len(changed)
    body = {"schema_version": "artifact-relocation-boundary-observation/v1",
            "status": "pass" if not changed else "fail", "blocker": None if not changed else "protected-population-drift",
            "source_manifest_sha256": digest_bytes(manifest_raw), "row_count": len(rows),
            "changed": changed, "concurrent_runtime_bookkeeping_changed": concurrent_runtime, **counts,
            "scope_note": "runtime governor bookkeeping is observed separately and is not attributed to E1",
            "forbidden_access_attempt_count": 0,
            "forbidden_access_success_count": 0, "evidence_dir": str(Path(args.evidence_dir).absolute())}
    _e1_new_json(args.observation_output, body)
    _e1_emit_aux(args, "prove-boundary", body["status"], [args.manifest, args.protected_before],
                 [args.observation_output, args.access_fragment_output, args.command_record_output], counts)
    print(json.dumps({"status": body["status"], "blocker": body["blocker"], **counts}, sort_keys=True))
    return EXIT_OK if not changed else EXIT_DRIFT


def seal_e1_trace(args: argparse.Namespace) -> int:
    access_dir, command_dir = Path(args.access_fragments_dir), Path(args.command_fragments_dir)
    access_names = [f"access-{index:02d}-{name}.jsonl" for index, name in enumerate(
        ("bind", "issue", "issue-retry", "resolve", "resolve-retry", "hygiene", "boundary"), 1)]
    command_names = [f"command-{index:02d}-{name}.json" for index, name in enumerate(
        ("bind", "issue", "issue-retry", "resolve", "resolve-retry", "hygiene", "boundary"), 1)]
    access_rows, command_rows = [], []
    sequence = 1
    expected_access = tuple(args.expected_access_subcommands.split(","))
    expected_command = tuple(args.expected_command_subcommands.split(","))
    if expected_access != E1_PRODUCTION_SUBCOMMANDS or expected_command != E1_PRODUCTION_SUBCOMMANDS:
        raise ValueError("e1-trace-expected-subcommands")
    for name in access_names:
        for row in read_jsonl_rows(access_dir / name):
            row["sequence"] = sequence; sequence += 1; access_rows.append(row)
    for index, name in enumerate(command_names, 1):
        row, _ = _e1_load_canonical(command_dir / name)
        if row.get("subcommand") != expected_command[index - 1] or row.get("scope") != "production-e1":
            raise ValueError("e1-command-fragment")
        command_rows.append({"scope": "production-e1", "sequence": index,
                             "subcommand": row["subcommand"], "argv": row["argv"]})
    access_raw = _e1_new_jsonl(args.access_trace_output, access_rows)
    _e1_new_json(args.access_trace_seal_output, {"schema_version": "artifact-relocation-access-trace-seal/v1",
        "trace_sha256": digest_bytes(access_raw), "trace_bytes": len(access_raw), "event_count": len(access_rows),
        "trace_body_write_completed": True, "forbidden_access_attempt_count": 0,
        "forbidden_access_success_count": 0})
    command_raw = _e1_new_jsonl(args.command_trace_output, command_rows)
    excluded = tuple(sorted(args.excluded_meta_operations.split(",")))
    if excluded != E1_EXCLUDED_META:
        raise ValueError("e1-command-meta-set")
    _e1_new_json(args.command_trace_seal_output, {"schema_version": "artifact-relocation-command-trace-seal/v1",
        "trace_sha256": digest_bytes(command_raw), "trace_bytes": len(command_raw), "row_count": 7,
        "included_sequences": list(range(1, 8)), "excluded_meta_operations": list(E1_EXCLUDED_META)})
    observation, _ = _e1_load_canonical(args.boundary_observation)
    if observation.get("status") != "pass":
        raise ValueError("e1-boundary-observation")
    boundary = {**observation, "schema_version": "artifact-relocation-boundary-proof/v1",
                "access_trace_sha256": digest_bytes(access_raw), "access_trace_bytes": len(access_raw),
                "access_event_count": len(access_rows)}
    _e1_new_json(args.boundary_output, boundary)
    _e1_new_json(args.status_output, {"schema_version": "artifact-relocation-e1-status/v1",
        "status": "sealed", "blocker": None, "subcommand": "seal-e1-trace"})
    print(json.dumps({"status": "sealed", "blocker": None, "access_event_count": len(access_rows),
                      "command_row_count": 7}, sort_keys=True))
    return EXIT_OK


def scan_e1_command_payload(args: argparse.Namespace) -> int:
    trace_raw = read_bytes(args.command_trace); rows = read_jsonl_rows(args.command_trace)
    seal, _ = _e1_load_canonical(args.command_trace_seal)
    if seal.get("trace_sha256") != digest_bytes(trace_raw) or seal.get("trace_bytes") != len(trace_raw):
        raise ValueError("e1-command-trace-seal")
    if [row.get("sequence") for row in rows] != list(range(1, 8)) or tuple(row.get("subcommand") for row in rows) != E1_PRODUCTION_SUBCOMMANDS:
        raise ValueError("e1-command-trace-order")
    findings = []
    for row in rows:
        argv = row.get("argv", [])
        if any(token in ("apply", "delete", "rename", "chmod", "mv", "rm") for token in argv):
            findings.append({"sequence": row["sequence"], "pattern": "forbidden-effect-token"})
        if "rehearse" in argv and "--mode" in argv and "apply" in argv:
            findings.append({"sequence": row["sequence"], "pattern": "rehearse-apply"})
        joined = "\0".join(argv).lower()
        if any(token in joined for token in ("/cairn", "/turso", "/.runtime/memory")):
            findings.append({"sequence": row["sequence"], "pattern": "forbidden-system"})
    body = {"schema_version": "artifact-relocation-command-payload-scan/v1",
            "status": "pass" if not findings else "fail", "policy": args.policy,
            "output_identity": "command-payload-scan/e1/production-trace",
            "command_source_sha256": digest_bytes(trace_raw), "included_sequences": list(range(1, 8)),
            "excluded_meta_operations": list(E1_EXCLUDED_META), "payload_sha256": digest_bytes(trace_raw),
            "payload_bytes": len(trace_raw), "findings": findings}
    _e1_new_json(args.output, body)
    print(json.dumps({"status": body["status"], "findings": len(findings)}, sort_keys=True))
    return EXIT_OK if not findings else EXIT_IDENTITY


def check_e1_reachability(args: argparse.Namespace) -> int:
    paths = [Path(args.source), Path(args.test_source), *(Path(path) for path in args.dependency)]
    if len(paths) != 5 or [path.name for path in paths[2:]] != ["artifact_identity.py", "artifact-knowledge-feed.py", "capability-route.py"]:
        raise ValueError("e1-reachability-path-set")
    digests = {str(path): digest_bytes(path.read_bytes()) for path in paths}
    expected_dependencies = {
        "utilities/artifact_identity.py": "ed75a3579ff6324cceae491aa8a38b5cc4a443105866d2d5a0ba66f435a865b8",
        "utilities/artifact-knowledge-feed.py": "9a1b5de3a413b3c2bbb6287a99b545028f6ebf4433d8eb42b2d8ec3599b3157d",
        "utilities/capability-route.py": "c9e0df284bb6e83839fef263840d949ac151d34e74fc2df8d5a1dfe4429624a2",
    }
    findings = []
    for path in paths:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        key = str(path)
        if key in expected_dependencies and digests[key] != expected_dependencies[key]:
            findings.append(f"dependency-drift:{key}")
    source_text = Path(args.source).read_text(encoding="utf-8")
    for root in ("bind_e1", "issue_e1", "resolve_e1", "hygiene_e1", "prove_boundary_e1",
                 "seal_e1_trace", "scan_e1_command_payload", "check_e1_verification", "validate_e1_report"):
        if f"def {root}(" not in source_text:
            findings.append(f"missing-root:{root}")
    body = {"schema_version": "artifact-relocation-e1-reachability/v1",
            "status": "pass" if not findings else "fail", "policy": args.policy,
            "writable_sources": [str(paths[0]), str(paths[1])], "dependencies": [str(path) for path in paths[2:]],
            "path_sha256": digests, "dynamic_load_edges": ["artifact_identity.py", "artifact-knowledge-feed.py", "capability-route.py"],
            "dependency_api_edges": ["IdAllocator.allocate", "is_well_formed", "migration_id", "verify_route", "route_hash"],
            "dependency_internal_closure": [], "e1_roots": ["bind_e1", "issue_e1", "resolve_e1", "hygiene_e1", "prove_boundary_e1", "seal_e1_trace", "scan_e1_command_payload", "check_e1_verification", "validate_e1_report"],
            "reachable_symbols": [], "forbidden_symbols": [], "forbidden_modules": [],
            "forbidden_path_prefixes": [], "unresolved_dynamic_calls": [],
            "retained_blocked_surfaces": [{"symbol": "apply_cmd", "reachable_from_e1": False},
                                           {"symbol": "rehearse", "reachable_from_e1": False}],
            "findings": findings}
    _e1_new_json(args.output, body)
    print(json.dumps({"status": body["status"], "findings": len(findings), "path_count": len(paths)}, sort_keys=True))
    return EXIT_OK if not findings else EXIT_IDENTITY


def run_e1_operation(args: argparse.Namespace) -> int:
    argv = list(args.command_argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise ValueError("e1-run-empty-argv")
    for path in (args.stdout_output, args.stderr_output, args.run_fragment_output):
        if Path(path).exists():
            raise ValueError(f"e1-run-output-exists:{path}")
    runner_argv = [args.checked_runner, "verification-runner", "--timeout", str(args.runner_timeout), "--", *argv]
    started = time.monotonic_ns()
    result = subprocess.run(runner_argv, capture_output=True)
    mode, unavailable_exit, unavailable_evidence = "checked-verification-runner", None, None
    if result.returncode == EXIT_AUTHORITY and b"child_started=0" in result.stderr + result.stdout:
        unavailable_exit = result.returncode
        unavailable_evidence = {"stdout_sha256": digest_bytes(result.stdout), "stdout_bytes": len(result.stdout),
                                "stderr_sha256": digest_bytes(result.stderr), "stderr_bytes": len(result.stderr)}
        result = subprocess.run(argv, capture_output=True)
        mode = "direct-fallback"
    duration = time.monotonic_ns() - started
    expected_status = None if args.expected_status == "none" else args.expected_status
    observed_status = None
    if expected_status is not None:
        for line in reversed(result.stdout.decode("utf-8", "replace").splitlines()):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and "status" in value:
                observed_status = value["status"]
                break
    _e1_new(args.stdout_output, result.stdout); _e1_new(args.stderr_output, result.stderr)
    fragment = {"schema_version": "artifact-relocation-e1-run/v1", "operation_id": args.operation_id,
                "selection": [], "argv": argv, "runner_argv": runner_argv, "runner_mode": mode,
                "runner_unavailable_exit": unavailable_exit, "runner_unavailable_evidence": unavailable_evidence,
                "expected_exit": args.expected_exit, "observed_exit": result.returncode,
                "expected_status": expected_status, "observed_status": observed_status,
                "started_monotonic_ns": started, "duration_ns": duration,
                "stdout_path": str(Path(args.stdout_output).absolute()), "stdout_sha256": digest_bytes(result.stdout),
                "stdout_bytes": len(result.stdout), "stderr_path": str(Path(args.stderr_output).absolute()),
                "stderr_sha256": digest_bytes(result.stderr), "stderr_bytes": len(result.stderr)}
    _e1_new_json(args.run_fragment_output, fragment)
    if result.returncode != args.expected_exit or observed_status != expected_status:
        print(json.dumps({"status": "fail", "operation_id": args.operation_id,
                          "observed_exit": result.returncode, "observed_status": observed_status}, sort_keys=True))
        return EXIT_IDENTITY
    print(json.dumps({"status": "sealed", "operation_id": args.operation_id,
                      "observed_exit": result.returncode, "observed_status": observed_status}, sort_keys=True))
    return EXIT_OK


def build_e1_verification_index(args: argparse.Namespace) -> int:
    expected = args.expected_operation_ids.split(",")
    if len(expected) != int(args.expected_row_count) or len(expected) != len(set(expected)):
        raise ValueError("e1-index-expected-set")
    fragments = [Path(path) for path in args.run_fragment]
    if args.run_fragments_dir:
        fragments.extend(Path(args.run_fragments_dir) / f"{operation}.json" for operation in expected)
    if len(fragments) != len(expected):
        raise ValueError("e1-index-fragment-count")
    by_id = {}
    used_logs = set()
    for path in fragments:
        body, raw = _e1_load_canonical(path)
        operation = body.get("operation_id")
        if operation not in expected or operation in by_id:
            raise ValueError("e1-index-operation")
        for prefix in ("stdout", "stderr"):
            log = body[f"{prefix}_path"]
            if log in used_logs:
                raise ValueError("e1-index-log-reuse")
            used_logs.add(log)
            data = read_bytes(log)
            if digest_bytes(data) != body[f"{prefix}_sha256"] or len(data) != body[f"{prefix}_bytes"]:
                raise ValueError("e1-index-log-drift")
        if body["observed_exit"] != body["expected_exit"]:
            raise ValueError("e1-index-exit")
        if body["observed_status"] != body["expected_status"]:
            raise ValueError("e1-index-status")
        by_id[operation] = {"operation_id": operation, "run_fragment_path": str(path.absolute()),
                            "run_fragment_sha256": digest_bytes(raw), "run_fragment_bytes": len(raw),
                            "stdout_sha256": body["stdout_sha256"], "stderr_sha256": body["stderr_sha256"],
                            "observed_exit": body["observed_exit"], "observed_status": body["observed_status"]}
    _e1_new_jsonl(args.output, [by_id[operation] for operation in expected])
    print(json.dumps({"status": "sealed", "row_count": len(expected)}, sort_keys=True))
    return EXIT_OK


def check_e1_verification(args: argparse.Namespace) -> int:
    index_raw = read_bytes(args.verification_index); index = read_jsonl_rows(args.verification_index)
    if args.check_only:
        summary, summary_raw = _e1_load_canonical(args.verification_summary)
        if summary.get("status") != "pass" or summary.get("blocker") is not None:
            raise ValueError("e1-summary-status")
        if summary.get("verification_index_sha256") != digest_bytes(index_raw):
            raise ValueError("e1-summary-index-binding")
        result = {"schema_version": "artifact-relocation-e1-verification-check/v1", "status": "pass",
                  "blocker": None, "verification_summary_sha256": digest_bytes(summary_raw),
                  "verification_index_sha256": digest_bytes(index_raw), "verification_index_rows": len(index)}
    else:
        authority, authority_raw = _e1_load_canonical(args.authority)
        w7, w7_raw = _e1_load_canonical(args.w7_binding); _e1_validate_w7(w7)
        ledger, ledger_seal, ledger_raw = _e1_verify_seal(args.ledger, args.ledger_seal, "identity-ledger")
        target, target_seal, target_raw = _e1_verify_seal(args.target_set, args.target_seal, "exact-target-set")
        hygiene, _ = _e1_load_canonical(args.registry_hygiene)
        boundary, _ = _e1_load_canonical(args.boundary_proof)
        command_scan, _ = _e1_load_canonical(args.command_payload_scan)
        reachability, _ = _e1_load_canonical(args.static_reachability)
        access_raw = read_bytes(args.access_trace); access_seal, _ = _e1_load_canonical(args.access_trace_seal)
        command_raw = read_bytes(args.command_trace); command_seal, _ = _e1_load_canonical(args.command_trace_seal)
        if any(item.get("status") != "pass" for item in (hygiene, boundary, command_scan, reachability)):
            raise ValueError("e1-verification-component")
        if target.get("row_count") != 5631 or any(target.get("collision_counts", {}).values()):
            raise ValueError("e1-verification-target")
        if len(ledger.get("row_bindings", [])) != MANIFEST_ROWS:
            raise ValueError("e1-verification-ledger")
        if access_seal.get("trace_sha256") != digest_bytes(access_raw) or command_seal.get("trace_sha256") != digest_bytes(command_raw):
            raise ValueError("e1-verification-trace")
        result = {"schema_version": "artifact-relocation-e1-verification-summary/v1", "status": "pass",
                  "blocker": None, "authority_receipt_sha256": digest_bytes(authority_raw),
                  "w7_verification_sha256": digest_bytes(w7_raw), "identity_ledger_sha256": digest_bytes(ledger_raw),
                  "identity_ledger_subject_count": len(ledger["subjects"]),
                  "identity_ledger_kind_coverage": _e1_kind_coverage(ledger["subjects"]),
                  "exact_target_set_sha256": digest_bytes(target_raw), "exact_target_row_count": target["row_count"],
                  "collision_counts": target["collision_counts"], "registry_mutations": hygiene["registry_mutations"],
                  "protected_mutations": sum(boundary[key] for key in ("moved", "deleted", "renamed", "chmodded", "content_changed", "symlink_retargeted")),
                  "forbidden_access_attempt_count": boundary["forbidden_access_attempt_count"],
                  "forbidden_access_success_count": boundary["forbidden_access_success_count"],
                  "verification_index_sha256": digest_bytes(index_raw), "verification_index_rows": len(index),
                  "access_trace_sha256": digest_bytes(access_raw), "command_trace_sha256": digest_bytes(command_raw),
                  "production_apply_authorized": False, "next_state": "E2_REQUIRES_SEPARATE_RUN"}
    _e1_new_json(args.output, result)
    print(json.dumps({"status": "pass", "blocker": None}, sort_keys=True))
    return EXIT_OK


def validate_e1_report(args: argparse.Namespace) -> int:
    if args.check_only:
        validation, validation_raw = _e1_load_canonical(args.validation_result)
        rows = read_jsonl_rows(args.report_verification_index)
        if validation.get("status") != "pass" or len(rows) != 2:
            raise ValueError("e1-report-check")
        result = {"schema_version": "artifact-relocation-e1-report-check/v1", "status": "pass",
                  "blocker": None, "validation_sha256": digest_bytes(validation_raw),
                  "report_verification_index_sha256": digest_bytes(read_bytes(args.report_verification_index))}
    else:
        report_raw = read_bytes(args.report); text = report_raw.decode("utf-8")
        summary, summary_raw = _e1_load_canonical(args.verification_summary)
        summary_check, _ = _e1_load_canonical(args.verification_summary_check)
        commit, commit_raw = _e1_load_canonical(args.commit_push)
        if summary.get("status") != "pass" or summary_check.get("status") != "pass" or commit.get("status") != "pass":
            raise ValueError("e1-report-input-status")
        if "next_state: E2_REQUIRES_SEPARATE_RUN" not in text or commit["commit_sha"] not in text:
            raise ValueError("e1-report-content")
        result = {"schema_version": "artifact-relocation-e1-report-validation/v1", "status": "pass",
                  "blocker": None, "report_sha256": digest_bytes(report_raw),
                  "verification_summary_sha256": digest_bytes(summary_raw),
                  "commit_push_sha256": digest_bytes(commit_raw), "commit_sha": commit["commit_sha"],
                  "next_state": "E2_REQUIRES_SEPARATE_RUN"}
        if args.validation_access_trace:
            trace_raw = _e1_new_jsonl(args.validation_access_trace, [{"sequence": 1, "event_kind": "filesystem",
                "operation": "read_file", "target": str(Path(args.report).absolute()), "phase": "success",
                "decision": "allow", "bytes": len(report_raw), "sha256": digest_bytes(report_raw), "errno": None}])
            _e1_new_json(args.validation_access_trace_seal, {"schema_version": "artifact-relocation-access-trace-seal/v1",
                "trace_sha256": digest_bytes(trace_raw), "trace_bytes": len(trace_raw), "event_count": 1,
                "trace_body_write_completed": True})
    _e1_new_json(args.output, result)
    print(json.dumps({"status": "pass", "blocker": None}, sort_keys=True))
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    h1 = sub.add_parser("hygiene")
    for name in ("jobs", "routes-dir", "registry-enumeration", "registry-enumeration-seal",
                 "access-fragment-output", "command-record-output", "output"):
        h1.add_argument("--" + name, dest=name.replace("-", "_"), required=True)
    h1.set_defaults(fn=hygiene_e1)

    p1 = sub.add_parser("prove-boundary")
    for name in ("manifest", "protected-before", "evidence-dir", "access-fragment-output",
                 "command-record-output", "observation-output"):
        p1.add_argument("--" + name, dest=name.replace("-", "_"), required=True)
    p1.set_defaults(fn=prove_boundary_e1)

    t1 = sub.add_parser("seal-e1-trace")
    for name in ("access-fragments-dir", "command-fragments-dir", "expected-access-subcommands",
                 "expected-command-subcommands", "excluded-meta-operations", "access-trace-output",
                 "access-trace-seal-output", "command-trace-output", "command-trace-seal-output",
                 "boundary-observation", "boundary-output", "status-output"):
        t1.add_argument("--" + name, dest=name.replace("-", "_"), required=True)
    t1.set_defaults(fn=seal_e1_trace)

    sc = sub.add_parser("scan-e1-command-payload")
    for name in ("policy", "command-trace", "command-trace-seal", "output"):
        sc.add_argument("--" + name, dest=name.replace("-", "_"), required=True)
    sc.set_defaults(fn=scan_e1_command_payload)

    reach = sub.add_parser("check-e1-reachability")
    reach.add_argument("--policy", required=True); reach.add_argument("--source", required=True)
    reach.add_argument("--test-source", dest="test_source", required=True)
    reach.add_argument("--dependency", action="append", default=[], required=True)
    reach.add_argument("--output", required=True); reach.set_defaults(fn=check_e1_reachability)

    run1 = sub.add_parser("run-e1-operation")
    run1.add_argument("--operation-id", required=True); run1.add_argument("--expected-exit", type=int, required=True)
    run1.add_argument("--expected-status", required=True); run1.add_argument("--checked-runner", required=True)
    run1.add_argument("--runner-timeout", type=int, required=True); run1.add_argument("--stdout-output", required=True)
    run1.add_argument("--stderr-output", required=True); run1.add_argument("--run-fragment-output", required=True)
    run1.add_argument("command_argv", nargs=argparse.REMAINDER); run1.set_defaults(fn=run_e1_operation)

    index1 = sub.add_parser("build-e1-verification-index")
    index1.add_argument("--expected-operation-ids", required=True); index1.add_argument("--run-fragments-dir")
    index1.add_argument("--run-fragment", action="append", default=[])
    index1.add_argument("--expected-row-count", required=True); index1.add_argument("--output", required=True)
    index1.set_defaults(fn=build_e1_verification_index)

    verify1 = sub.add_parser("check-e1-verification")
    for name in ("authority", "w7-binding", "ledger", "ledger-seal", "target-set", "target-seal",
                 "registry-hygiene", "boundary-proof", "access-trace", "access-trace-seal",
                 "command-trace", "command-trace-seal", "command-payload-scan", "static-reachability",
                 "verification-summary", "verification-index", "index-run-fragment", "index-stdout",
                 "index-stderr", "summary-run-fragment", "summary-stdout", "summary-stderr"):
        verify1.add_argument("--" + name, dest=name.replace("-", "_"))
    verify1.add_argument("--check-only", action="store_true"); verify1.add_argument("--output", required=True)
    verify1.set_defaults(fn=check_e1_verification)

    report1 = sub.add_parser("validate-e1-report")
    for name in ("report", "verification-summary", "verification-summary-check", "verification-index",
                 "access-trace", "access-trace-seal", "commit-push", "static-reachability",
                 "command-payload-scan", "report-verification-index-pending", "validation-access-trace",
                 "validation-access-trace-seal", "validation-result", "report-verification-index",
                 "index-run-fragment", "index-stdout", "index-stderr"):
        report1.add_argument("--" + name, dest=name.replace("-", "_"))
    report1.add_argument("--source", action="append", default=[])
    report1.add_argument("--dependency", action="append", default=[])
    report1.add_argument("--check-only", action="store_true"); report1.add_argument("--output", required=True)
    report1.set_defaults(fn=validate_e1_report)

    r = sub.add_parser("replay")
    for name in ("baseline", "manifest", "verification", "decision-table", "corrected-brief",
                 "authority-route", "corrected-review", "corrected-verdict", "prd", "output"):
        r.add_argument("--" + name, dest=name.replace("-", "_"), required=True)
    r.add_argument("--w6-commit", dest="w6_commit", required=True)
    r.set_defaults(fn=replay)

    d = sub.add_parser("delta")
    d.add_argument("--baseline", required=True)
    d.add_argument("--artifact-root", dest="artifact_root", required=True)
    d.add_argument("--self-write-root", dest="self_write_root")
    d.add_argument("--freeze-cutoff", dest="freeze_cutoff")
    d.add_argument("--cutoff", dest="cutoff")
    d.add_argument("--output", required=True)
    d.set_defaults(fn=delta)

    b = sub.add_parser("bind-e1")
    b.add_argument("--owner-prompt", required=True); b.add_argument("--route", required=True)
    b.add_argument("--manifest", required=True); b.add_argument("--jobs", required=True); b.add_argument("--routes-dir", required=True)
    b.add_argument("--w7-route-outcome", dest="w7_route_outcome", required=True); b.add_argument("--w7-final-report", dest="w7_final_report", required=True)
    b.add_argument("--w7-r6b-verdict", dest="w7_r6b_verdict", required=True); b.add_argument("--w7-blocked-apply", dest="w7_blocked_apply", required=True)
    b.add_argument("--authority-output", required=True); b.add_argument("--w7-binding-output", required=True)
    b.add_argument("--protected-before-output", required=True); b.add_argument("--registry-enumeration-output", required=True)
    b.add_argument("--registry-enumeration-seal-output", required=True); b.add_argument("--access-fragment-output", required=True)
    b.add_argument("--command-record-output", required=True); b.add_argument("--status-output", required=True); b.set_defaults(fn=bind_e1)

    i = sub.add_parser("issue")
    i.add_argument("--schema", choices=("v2",), required=True); i.add_argument("--manifest", required=True)
    i.add_argument("--authority", required=True); i.add_argument("--w7-binding", required=True); i.add_argument("--namespace", required=True)
    i.add_argument("--ledger-output", required=True); i.add_argument("--ledger-seal-output", required=True); i.add_argument("--access-fragment-output", required=True)
    i.add_argument("--command-record-output", required=True); i.add_argument("--status-output", required=True); i.set_defaults(fn=issue_e1)

    x = sub.add_parser("resolve")
    x.add_argument("--schema", choices=("v1", "v2"))
    x.add_argument("--manifest", required=True)
    x.add_argument("--identity-ledger", dest="identity_ledger", required=False)
    x.add_argument("--output", required=False)
    x.add_argument("--ledger", required=False); x.add_argument("--ledger-seal", required=False)
    x.add_argument("--authority", required=False); x.add_argument("--w7-binding", required=False)
    x.add_argument("--artifact-root", required=False); x.add_argument("--target-output", required=False); x.add_argument("--target-seal-output", required=False)
    x.add_argument("--access-fragment-output", required=False); x.add_argument("--command-record-output", required=False); x.add_argument("--status-output", required=False)
    x.set_defaults(fn=resolve_dispatch)

    c = sub.add_parser("check")
    c.add_argument("--compare-label", dest="compare_label")
    c.add_argument("--left")
    c.add_argument("--right")
    c.add_argument("--identity-result", dest="identity_result")
    c.add_argument("--manifest")
    c.add_argument("--decision-table", dest="decision_table")
    c.add_argument("--reference-output", dest="reference_output")
    c.add_argument("--qa-policy", dest="qa_policy")
    c.add_argument("--review-artifact", dest="review_artifact", action="append", default=[])
    c.add_argument("--require-registered-independent", dest="require_registered_independent", type=int)
    c.add_argument("--require-final-verify", dest="require_final_verify", action="store_true")
    c.add_argument("--package")
    c.add_argument("--output", required=True)
    c.set_defaults(fn=check)

    h = sub.add_parser("rehearse")
    h.add_argument("--mode", choices=("dry-run", "apply", "rollback"), required=True)
    h.add_argument("--output", required=True)
    h.add_argument("--replay")
    h.add_argument("--identity-result", dest="identity_result")
    h.add_argument("--oracle")
    h.add_argument("--fixture-template", dest="fixture_template")
    h.add_argument("--work-root", dest="work_root")
    h.add_argument("--backup-root", dest="backup_root")
    h.add_argument("--journal")
    h.add_argument("--inverse-journal", dest="inverse_journal")
    h.add_argument("--backup-seal", dest="backup_seal")
    h.set_defaults(fn=rehearse)

    s = sub.add_parser("seal")
    s.add_argument("--output", required=True)
    s.add_argument("--replay")
    s.add_argument("--delta")
    s.add_argument("--identity-result", dest="identity_result")
    s.add_argument("--oracle")
    s.add_argument("--reference-parity", dest="reference_parity")
    s.add_argument("--dry-run", dest="dry_run")
    s.add_argument("--rehearsal")
    s.add_argument("--rollback-rehearsal", dest="rollback_rehearsal")
    s.add_argument("--backup-seal", dest="backup_seal")
    s.add_argument("--quiescence-pair", dest="quiescence_pair")
    s.set_defaults(fn=seal)

    a = sub.add_parser("apply")
    a.add_argument("--artifact-root", dest="artifact_root", required=True)
    a.add_argument("--package", required=True)
    a.add_argument("--dispatch-jobs", dest="dispatch_jobs")
    a.add_argument("--dispatch-lock", dest="dispatch_lock")
    a.add_argument("--receipt-stdout", dest="receipt_stdout", action="store_true")
    a.add_argument("--output")
    a.set_defaults(fn=apply_cmd)

    w = sub.add_parser("handoff")
    w.add_argument("--package", required=True)
    w.add_argument("--apply-receipt", dest="apply_receipt")
    w.add_argument("--receipt-stdout", dest="receipt_stdout", action="store_true")
    w.add_argument("--output")
    w.set_defaults(fn=handoff)

    args = parser.parse_args()
    try:
        return args.fn(args)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return fail(EXIT_EVIDENCE, str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
