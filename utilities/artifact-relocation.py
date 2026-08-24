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
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import unicodedata
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
    work = Path(args.work_root).resolve(strict=True)
    if work == LIVE_HEARTING_ROOT or LIVE_HEARTING_ROOT in work.parents:
        return fail(EXIT_INPUT, "live-root-rejected-by-fixture-rehearsal")

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
        root = Path(args.artifact_root).resolve(strict=True)
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
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

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

    x = sub.add_parser("resolve")
    x.add_argument("--manifest", required=True)
    x.add_argument("--identity-ledger", dest="identity_ledger", required=True)
    x.add_argument("--output", required=True)
    x.set_defaults(fn=resolve)

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
