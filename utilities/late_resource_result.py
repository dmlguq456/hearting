"""Append-only adjudication of an exact successful resource result after close.

The outcome sidecar and earlier workflow events are history.  This module adds
one journal observation for a still-bound successful execution and exposes a
pure current projection over that observation.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any, Mapping

import resource_resume
import resource_run_registry as RR
import workflow_state as WS


ACTOR = "late-resource-result"
SCHEMA = "late-resource-result/v1"
TERMINAL_ACTOR = "late-terminal-result"
TERMINAL_SCHEMA = "late-terminal-result/v1"
_OUTPUT_FAILURE = "declared-artifact-missing"
_SHA256 = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
_MECHANICAL_GATE_REASONS = frozenset({
    "completion-marker-absent", "declared-artifact-missing", "missing-terminal-gate",
    "owner-terminal-not-pass", "terminal-gate-unproven",
})


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_regular(path: Path) -> bytes:
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("late-result-file-not-regular:" + str(path))
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read()
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("late-result-file-changed-during-read:" + str(path))
    return raw


def _file_digest(path: Path) -> str:
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("late-result-file-not-regular:" + str(path))
        digest = hashlib.sha256()
        with os.fdopen(os.dup(fd), "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("late-result-file-changed-during-read:" + str(path))
    return digest.hexdigest()


def _load_module(name: str, filename: str):
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location("late_result_" + name, path)
    if spec is None or spec.loader is None:
        raise ValueError("late-result-runtime-reader-unavailable:" + name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _route_module():
    return _load_module("capability_route", "capability-route.py")


def _supervisor():
    return _load_module("workflow_supervisor", "workflow-supervisor.py")


def _verification_node(node: Mapping[str, Any]) -> bool:
    node_id = str(node.get("id") or "").lower()
    gate = str(node.get("completion_gate") or node.get("terminal_gate") or "").lower()
    outputs = node.get("outputs") or []
    return ("verify" in node_id or "verify" in gate
            or any(Path(str(output)).name == "run-verdict.json" for output in outputs))


def _descendants(route: Mapping[str, Any], predecessor: str) -> list[dict]:
    reached = {predecessor}
    pending = list(route.get("nodes") or [])
    result = []
    while pending:
        rest = []
        for node in pending:
            node_id = node.get("id")
            if node_id in reached:
                continue
            if any(parent in reached for parent in node.get("depends_on") or []):
                reached.add(node_id)
                result.append(node)
            else:
                rest.append(node)
        if len(rest) == len(pending):
            break
        pending = rest
    return result


def _route_file_digest(route_file: Path, route: Mapping[str, Any]) -> str:
    raw = _read_regular(route_file)
    on_disk = json.loads(raw)
    if (not isinstance(on_disk, dict) or on_disk.get("route_id") != route.get("route_id")
            or on_disk.get("route_hash") != route.get("route_hash")):
        raise ValueError("late-result-route-binding-mismatch")
    return _sha(raw)


def _failure_rows(route: Mapping[str, Any], ledger) -> list[tuple[dict, dict]]:
    nodes = {node.get("id"): node for node in route.get("nodes") or []}
    failures = {}
    for entry in ledger.journal():
        node_id = entry.get("node")
        evidence = entry.get("evidence") or {}
        artifacts = evidence.get("artifacts") or {}
        node = nodes.get(node_id)
        missing_record = (artifacts.get("reason") == _OUTPUT_FAILURE
                          and isinstance(artifacts.get("missing"), list) and bool(artifacts["missing"]))
        runtime_record_missing = evidence.get("runtime_only_output") is True
        if (entry.get("state") == "FAILED_RETRYABLE" and node
                and node.get("kind") == "resource-runner"
                and evidence.get("succeeded") is True and evidence.get("exit_code") == 0
                and evidence.get("resource_sha256")
                and (missing_record or runtime_record_missing)):
            failures[node_id] = (node, evidence)
    return [failures[key] for key in sorted(failures)]


def _arm_file(ledger, node_id: str) -> tuple[dict, Path, str]:
    path = ledger.root / "armed" / f"{node_id}.json"
    raw = path.read_bytes()
    arm = json.loads(raw)
    if not isinstance(arm, dict) or arm.get("node") != node_id:
        raise ValueError("late-result-resource-watch-invalid:" + node_id)
    return arm, path, _sha(raw)


def _row_for(arm: Mapping[str, Any]) -> tuple[dict, Path]:
    path = Path(str(arm.get("resource_registry") or ""))
    raw = path.read_bytes()
    registry = json.loads(raw)
    row = ((registry.get("runs") or {}).get(arm.get("predecessor_id"))
           if isinstance(registry, dict) else None)
    if not isinstance(row, dict):
        raise ValueError("late-result-resource-row-missing")
    return row, path


def _marker_proofs(route: Mapping[str, Any], node: Mapping[str, Any], jobs: Path,
                   module) -> list[dict]:
    result = []
    for verify in _descendants(route, str(node["id"])):
        if not _verification_node(verify):
            continue
        verify_id = verify["id"]
        gate = verify.get("terminal_gate") if verify.get("terminal") else verify.get("completion_gate")
        proof = module._marker_identity_row(route, verify, verify_id, gate, jobs=jobs,
                                            exact_terminal=False)
        if proof.get("passed") is not True:
            raise ValueError(f"late-result-verification-unproven:{verify_id}:{proof.get('reason')}")
        marker_path = module.completion_dir(route["route_id"], jobs=jobs) / f"{verify_id}.json"
        marker_sha = _file_digest(marker_path)
        try:
            marker = json.loads(marker_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ValueError("late-result-verification-marker-invalid:" + verify_id) from exc
        evidence = marker.get("evidence") if isinstance(marker, dict) else None
        evidence_path = Path(str((evidence or {}).get("path") or ""))
        evidence_sha = str((evidence or {}).get("sha256") or "")
        if (not evidence_path.is_file() or not evidence_sha
                or _file_digest(evidence_path) != evidence_sha.removeprefix("sha256:")):
            raise ValueError("late-result-verification-evidence-changed:" + verify_id)
        result.append({"node": verify_id, "completion_gate": gate,
                       "marker_path": str(marker_path), "marker_sha256": marker_sha,
                       "evidence_path": str(evidence_path), "evidence_sha256": evidence_sha.removeprefix("sha256:")})
    if not result:
        raise ValueError("late-result-required-verification-absent:" + str(node.get("id")))
    return result


def _abandoned_cycle_snapshot(route: Mapping[str, Any], record: Mapping[str, Any]) -> dict | None:
    """Read the immutable producer snapshot that records this cycle's abandon event."""
    try:
        import artifact_lifecycle as AL
        import artifact_manifest as AM
        import artifact_producer as AP

        if (record.get("state") != "sealed" or record.get("deleted_at")
                or record.get("abandon_reason") not in (None, "route-unrecoverable")):
            return None
        root = Path(route["artifact_root"]).resolve(strict=True)
        manifest_path = AP._record_cycle_manifest_path(root, record)
        current_raw = _read_regular(manifest_path)
        current = json.loads(current_raw)
        if (current.get("cycle", {}).get("cycle_id") != record.get("cycle_id")
                or current.get("cycle", {}).get("state") != "abandoned"
                or AM.manifest_digest(current) != record.get("manifest_digest")):
            return None
        events = [event for event in current.get("events", [])
                  if isinstance(event, dict) and event.get("event_type") == "cycle.abandoned"
                  and event.get("target_id") == record.get("cycle_id")]
        if len(events) != 1:
            return None
        event = events[0]
        provenance = event.get("provenance") or {}
        revision_id = provenance.get("source_revision_id")
        event_reason = (event.get("payload") or {}).get("abandon_reason")
        if (event_reason != "route-unrecoverable"
                or (record.get("abandon_reason") is not None
                    and event_reason != record.get("abandon_reason"))
                or (event.get("actor") or {}).get("id") != record.get("producer_id")
                or not isinstance(revision_id, str)):
            return None
        snapshots = AL.read_manifest_snapshots(root, record["cycle_id"])
        matches = [(raw, document) for raw, document in snapshots
                   if document.get("manifest_revision_id") == revision_id]
        if len(matches) != 1:
            return None
        snapshot_raw, document = matches[0]
        if (document.get("cycle", {}).get("cycle_id") != record.get("cycle_id")
                or document.get("cycle", {}).get("state") != "abandoned"
                or not any(item.get("event_id") == event.get("event_id")
                           and item.get("payload") == event.get("payload")
                           for item in document.get("events", []) if isinstance(item, dict))):
            return None
        if not any(row.get("route_id") == route.get("route_id")
                   and row.get("route_hash") == route.get("route_hash")
                   for row in document.get("routes", []) if isinstance(row, dict)):
            return None
        cycle_dir = manifest_path.parent.resolve(strict=True)
        artifact_base = (cycle_dir / "artifacts").resolve(strict=True)
        artifact_digests = {}
        for item in document.get("artifact_revisions", []) or []:
            if not isinstance(item, dict):
                continue
            locator = (item.get("locator") or {}).get("path")
            digest = item.get("content_digest")
            if (not isinstance(locator, str) or not locator.startswith("artifacts/")
                    or not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
                continue
            candidate = (cycle_dir / locator).resolve(strict=False)
            if candidate.is_relative_to(artifact_base):
                artifact_digests[str(candidate)] = digest.removeprefix("sha256:")
        return {"record": record, "manifest_path": str(manifest_path),
                "snapshot_revision_id": revision_id, "snapshot_sha256": _sha(snapshot_raw),
                "cycle_dir": str(cycle_dir), "artifact_base": str(artifact_base),
                "artifact_digests": artifact_digests, "document": document}
    except (ImportError, OSError, ValueError, KeyError, TypeError, AttributeError,
            json.JSONDecodeError):
        return None


def _original_lab_run(route: Mapping[str, Any], arm: Mapping[str, Any],
                      row: Mapping[str, Any]) -> tuple[dict, dict]:
    """Find the exact run record retained by the producer snapshot at mechanical close."""
    import artifact_producer as AP

    root = Path(route["artifact_root"]).resolve(strict=True)
    record = AP.route_cycle_for(root, route)
    closure = _abandoned_cycle_snapshot(route, record) if record else None
    base = Path(str(arm.get("artifact_base") or "")).resolve(strict=True)
    if not closure or base != Path(closure["artifact_base"]):
        raise ValueError("late-result-lab-original-roster-unproven")
    cycle_dir = Path(closure["cycle_dir"])
    candidates = []
    for item in closure["document"].get("artifact_revisions", []) or []:
        if not isinstance(item, dict):
            continue
        locator = (item.get("locator") or {}).get("path")
        if not isinstance(locator, str) or Path(locator).name != "run.json":
            continue
        path = (cycle_dir / locator).resolve(strict=True)
        if not path.is_relative_to(base):
            continue
        expected_sha = str(item.get("content_digest") or "").removeprefix("sha256:")
        if not isinstance(expected_sha, str) or _file_digest(path) != expected_sha:
            continue
        document = json.loads(_read_regular(path))
        if not isinstance(document, dict):
            continue
        resource = document.get("resource")
        if (document.get("route") == route.get("route_id") and isinstance(resource, dict)
                and resource.get("run_id") == row.get("run_id")):
            candidates.append((path, expected_sha, document))
    if not candidates or len({item[1] for item in candidates}) != 1:
        raise ValueError("late-result-lab-original-roster-unproven")
    source_path, source_sha, source_document = candidates[0]
    if (not isinstance(source_document.get("arms"), dict) or not source_document["arms"]
            or source_document.get("status") != "complete"):
        raise ValueError("late-result-lab-original-roster-unproven")
    return source_document, {"closure": closure, "path": str(source_path), "sha256": source_sha}


def _lab_arm_proofs(route: Mapping[str, Any], arm: Mapping[str, Any], row: Mapping[str, Any],
                    run_path: Path, verification: list[dict]) -> dict | None:
    """Recheck the close-time producer roster against late run and verifier records."""
    if route.get("capability") != "autopilot-lab":
        return None
    try:
        run_document = json.loads(Path(run_path).read_bytes())
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError("late-result-lab-run-record-unreadable") from exc
    arms = run_document.get("arms") if isinstance(run_document, dict) else None
    resource = run_document.get("resource") if isinstance(run_document, dict) else None
    if (not isinstance(arms, dict) or not arms or not isinstance(resource, dict)
            or run_document.get("route") != route.get("route_id")
            or run_document.get("status") != "complete"
            or resource.get("run_id") != row.get("run_id")
            or resource.get("exit_code") != 0
            or any(resource.get(key) != row.get(row_key) for key, row_key in (
                ("pid", "pid"), ("starttime", "starttime"), ("command_hash", "command_hash"),
                ("sentinel", "sentinel"), ("log", "log"), ("exit_code", "exit_code")))
            or (row.get("source_commit") and run_document.get("source_commit")
                and row.get("source_commit") != run_document.get("source_commit"))):
        raise ValueError("late-result-lab-run-record-binding-mismatch")
    original_document, roster_source = _original_lab_run(route, arm, row)
    original_arms = original_document["arms"]
    original_resource = original_document.get("resource") or {}
    if (not isinstance(original_resource, dict)
            or (original_document.get("source_commit") is not None
                and original_document.get("source_commit") != run_document.get("source_commit"))
            or any(not isinstance(original_arms.get(arm_id), dict) for arm_id in original_arms)
            or set(arms) != set(original_arms)
            or any(original_resource.get(key) is not None
                   and original_resource.get(key) != resource.get(key) for key in (
                "run_id", "pid", "starttime", "command_hash", "sentinel", "log", "exit_code"))):
        raise ValueError("late-result-lab-original-required-arms-mismatch")
    verdict_markers = [item for item in verification
                       if item.get("node") == "run-verify"
                       or Path(str(item.get("evidence_path") or "")).name == "run-verdict.json"]
    if not verdict_markers:
        raise ValueError("late-result-lab-run-verify-marker-absent")
    required_arms = set(original_arms)
    arm_proofs = []
    checked = []
    base = Path(str(arm.get("artifact_base") or "")).resolve(strict=True)
    for marker in verdict_markers:
        verdict_path = Path(marker["evidence_path"])
        verdict = json.loads(verdict_path.read_bytes())
        verdict_arms = verdict.get("arms") if isinstance(verdict, dict) else None
        if (not isinstance(verdict_arms, dict) or set(verdict_arms) != required_arms
                or verdict.get("run_verified") is not True
                or verdict.get("verdict") not in {"PASS", "diagnosed"}
                or not Path(verdict_path).resolve(strict=True).is_relative_to(base)
                or (verdict.get("resource") or {}).get("run_id") != row.get("run_id")):
            raise ValueError("late-result-lab-verdict-required-arms-mismatch")
        seen_run_ids, seen_compute_ids, seen_trainers, seen_wrappers = set(), set(), set(), set()
        for arm_id in sorted(required_arms):
            execution = arms[arm_id]
            original_execution = original_arms[arm_id]
            arm_verdict = verdict_arms[arm_id]
            fixed_fields = ("run_id", "compute_run_id", "config_ref", "config_sha256",
                            "config_manifest", "run_dir", "run_verify", "counters",
                            "trainer_identity", "wrapper_identity", "ckpt_path", "best")
            if any(execution.get(key) != original_execution.get(key) for key in fixed_fields):
                raise ValueError("late-result-lab-original-arm-binding-mismatch:" + str(arm_id))
            if (not isinstance(arm_verdict, dict)
                    or any(key in arm_verdict and arm_verdict.get(key) != original_execution.get(source_key)
                           for key, source_key in (("run_id", "run_id"),
                                                   ("compute_run_id", "compute_run_id"),
                                                   ("config_ref", "config_ref"),
                                                   ("config_sha256", "config_sha256"),
                                                   ("run_dir", "run_dir"),
                                                   ("run_verify", "run_verify")))):
                raise ValueError("late-result-lab-verdict-arm-binding-mismatch:" + str(arm_id))
            trainer = execution.get("trainer_identity")
            wrapper = execution.get("wrapper_identity")
            best = execution.get("best")
            config_manifest_path = Path(str(execution.get("config_manifest") or ""))
            if not config_manifest_path.is_absolute():
                config_manifest_path = base / config_manifest_path
            config_manifest_path = config_manifest_path.resolve(strict=True)
            config_manifest = json.loads(config_manifest_path.read_bytes())
            config_sha = _SHA256.fullmatch(str(execution.get("config_sha256") or ""))
            snapshot_path = Path(str(config_manifest.get("snapshot_path") or ""))
            if not snapshot_path.is_absolute():
                snapshot_path = base / snapshot_path
            snapshot_path = snapshot_path.resolve(strict=True)
            run_id = execution.get("run_id")
            compute_id = execution.get("compute_run_id")
            compute_dir = Path(str(execution.get("compute_run_dir") or ""))
            run_dir = execution.get("run_dir")
            run_verify_path = Path(str(execution.get("run_verify") or ""))
            counters = execution.get("counters")
            trainer_key = (trainer or {}).get("pid"), (trainer or {}).get("starttime")
            wrapper_key = (wrapper or {}).get("pid"), (wrapper or {}).get("starttime")
            checkpoint = ((best or {}).get("checkpoint") or (best or {}).get("path")
                          or execution.get("ckpt_path"))
            checkpoint_sha = _SHA256.fullmatch(str((best or {}).get("sha256") or ""))
            selected = arm_verdict.get("selected_checkpoint") if isinstance(arm_verdict, dict) else None
            per_arm_path = Path(str((arm_verdict or {}).get("evidence") or ""))
            if not per_arm_path.is_absolute():
                per_arm_path = base / per_arm_path
            per_arm_path = per_arm_path.resolve(strict=True)
            expected_artifacts = roster_source["closure"]["artifact_digests"]
            for trusted_path in (config_manifest_path, snapshot_path, per_arm_path):
                trusted_sha = expected_artifacts.get(str(trusted_path))
                if trusted_sha is None or _file_digest(trusted_path) != trusted_sha:
                    raise ValueError("late-result-lab-original-input-changed:" + str(arm_id))
            trainer_pid, trainer_start = trainer_key
            wrapper_pid, wrapper_start = wrapper_key
            positive_identity = lambda value: ((type(value) is int and value > 0)
                                               or (isinstance(value, str) and value.isdigit()
                                                   and int(value) > 0))
            if (not isinstance(run_id, str) or not run_id or run_id in seen_run_ids
                    or not isinstance(compute_id, str) or not compute_id or compute_id in seen_compute_ids
                    or not compute_dir.is_absolute() or compute_dir.name != compute_id
                    or not isinstance(run_dir, str) or not run_dir
                    or not run_verify_path.is_absolute() or run_verify_path.resolve(strict=True) != per_arm_path
                    or not isinstance(counters, dict)
                    or arm_verdict.get("counters") != counters
                    or execution.get("compute_exit_code") != 0
                    or (execution.get("trainer_status") or {}).get("status") != "complete"
                    or not isinstance(trainer, dict) or not isinstance(wrapper, dict)
                    or not all(positive_identity(value) for value in
                               (trainer_pid, trainer_start, wrapper_pid, wrapper_start))
                    or not _SHA256.fullmatch(str(trainer.get("cmdline_sha256") or ""))
                    or not _SHA256.fullmatch(str(wrapper.get("cmdline_sha256") or ""))
                    or trainer_key in seen_trainers or wrapper_key in seen_wrappers
                    or not isinstance(config_sha, re.Match)
                    or not config_manifest_path.is_relative_to(base)
                    or not snapshot_path.is_relative_to(base)
                    or not per_arm_path.is_relative_to(base)
                    or config_manifest.get("run_id") != run_id
                    or config_manifest.get("config_ref") != execution.get("config_ref")
                    or config_manifest.get("source_sha256") != config_sha[1]
                    or config_manifest.get("snapshot_sha256") != config_sha[1]
                    or _file_digest(snapshot_path) != config_sha[1]
                    or arm_verdict.get("status") != "PASS"
                    or not isinstance(selected, dict)
                    or selected.get("path") != checkpoint
                    or not isinstance(checkpoint_sha, re.Match)
                    or selected.get("sha256") != checkpoint_sha[1]
                    or not isinstance(checkpoint, str)):
                raise ValueError("late-result-lab-arm-identity-or-config-mismatch:" + str(arm_id))
            per_arm = json.loads(per_arm_path.read_bytes())
            checks = per_arm.get("checks") or {}
            if (per_arm.get("arm") != arm_id or per_arm.get("status") != "PASS"
                    or per_arm.get("run_dir") != run_dir
                    or (per_arm.get("selected_checkpoint") or {}).get("path") != checkpoint
                    or (per_arm.get("selected_checkpoint") or {}).get("sha256") != checkpoint_sha[1]
                    or (checks.get("status-complete") or {}).get("ok") is not True
                    or ((checks.get("status-complete") or {}).get("status") or {}).get("status") != "complete"
                    or ((checks.get("status-complete") or {}).get("status") or {}).get("attempt") != counters.get("attempt")
                    or ((checks.get("status-complete") or {}).get("status") or {}).get("successful") != counters.get("successful")
                    or ((checks.get("status-complete") or {}).get("status") or {}).get("skipped") != counters.get("skipped")
                    or (checks.get("provenance-continuity") or {}).get("ok") is not True
                    or _file_digest(Path(checkpoint)) != checkpoint_sha[1]
                    or _file_digest(per_arm_path) != _SHA256.fullmatch(
                        str(arm_verdict.get("evidence_sha256") or ""))[1]):
                raise ValueError("late-result-lab-arm-verification-mismatch:" + str(arm_id))
            config_manifest_sha = _file_digest(config_manifest_path)
            snapshot_sha = _file_digest(snapshot_path)
            per_arm_sha = _file_digest(per_arm_path)
            checkpoint_sha_value = _file_digest(Path(checkpoint))
            evidence_sha = _SHA256.fullmatch(str(arm_verdict.get("evidence_sha256") or ""))
            if (snapshot_sha != config_sha[1] or checkpoint_sha_value != checkpoint_sha[1]
                    or not evidence_sha or per_arm_sha != evidence_sha[1]):
                raise ValueError("late-result-lab-arm-bytes-changed:" + str(arm_id))
            arm_proofs.append({"arm": arm_id, "run_id": run_id, "compute_run_id": compute_id,
                               "config_ref": execution.get("config_ref"), "config_sha256": config_sha[1],
                               "config_manifest_path": str(config_manifest_path),
                               "config_manifest_sha256": config_manifest_sha,
                               "snapshot_path": str(snapshot_path), "snapshot_sha256": snapshot_sha,
                               "trainer_identity": trainer, "wrapper_identity": wrapper,
                               "checkpoint_path": checkpoint, "checkpoint_sha256": checkpoint_sha_value,
                               "verification_path": str(per_arm_path), "verification_sha256": per_arm_sha})
            checked.extend([
                {"path": str(config_manifest_path), "sha256": config_manifest_sha},
                {"path": str(snapshot_path), "sha256": snapshot_sha},
                {"path": str(per_arm_path), "sha256": per_arm_sha},
                {"path": str(Path(checkpoint).resolve(strict=True)), "sha256": checkpoint_sha_value},
            ])
            seen_run_ids.add(run_id)
            seen_compute_ids.add(compute_id)
            seen_trainers.add(trainer_key)
            seen_wrappers.add(wrapper_key)
    return {"required_arms": sorted(required_arms),
            "original_roster": {"path": roster_source["path"], "sha256": roster_source["sha256"],
                                "manifest_revision_id": roster_source["closure"]["snapshot_revision_id"],
                                "manifest_snapshot_sha256": roster_source["closure"]["snapshot_sha256"]},
            "run_verdict_path": str(verdict_markers[0]["evidence_path"]),
            "run_verdict_sha256": str(verdict_markers[0]["evidence_sha256"]),
            "arms": arm_proofs, "checked_files": checked}


def _same_byte_verification(outputs: list[dict], verification: list[dict], base: Path) -> bool:
    """Require existing verifier evidence to name the exact output bytes it judged."""
    import resource_failure_resolution as RESOLUTION
    admitted = set()
    for marker in verification:
        path = Path(marker["evidence_path"])
        if path.suffix not in {".json", ".jsonl"}:
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            references = RESOLUTION.references(document)
        except (OSError, ValueError, TypeError):
            continue
        for name, expected in references:
            target = Path(name)
            target = target if target.is_absolute() else Path(base) / target
            try:
                resolved = target.resolve(strict=True)
            except OSError:
                continue
            admitted.add((str(resolved), str(expected).removeprefix("sha256:")))
    return all((str(Path(item["path"]).resolve(strict=True)), item["sha256"]) in admitted
               for item in outputs)


def _candidate(route: Mapping[str, Any], route_file: Path, outcome_raw: bytes,
               ledger, jobs: Path) -> dict:
    supervisor = _supervisor()
    module = _route_module()
    executions = []
    checked_files = []
    for node, failure in _failure_rows(route, ledger):
        node_id = node["id"]
        arm, arm_path, arm_sha = _arm_file(ledger, node_id)
        expected_jobs = str(Path(jobs).resolve())
        if (arm.get("predecessor_kind") != "resource"
                or arm.get("route_id") != route.get("route_id")
                or arm.get("route_hash") != route.get("route_hash")
                or arm.get("route_file") != str(Path(route_file).resolve())
                or arm.get("jobs") != expected_jobs):
            raise ValueError("late-result-resource-watch-binding-mismatch:" + node_id)
        current = supervisor.resource_evidence(arm)
        if (not current.get("terminal") or current.get("succeeded") is not True
                or current.get("liveness") != "exited" or current.get("exit_code") != 0
                or current.get("identity") != failure.get("identity")
                or current.get("resource_sha256") != failure.get("resource_sha256")
                or not current.get("sentinel_present")):
            raise ValueError("late-result-execution-no-longer-proven:" + node_id)
        supervisor.resource_output_evidence(route, arm, current)
        artifacts = supervisor.artifact_evidence(arm)
        if not artifacts.get("checked") or artifacts.get("missing"):
            raise ValueError("late-result-output-incomplete:" + node_id)
        import resource_run_evidence as RUN_EVIDENCE
        receipt = RUN_EVIDENCE.paths(arm)
        base = Path(str(arm.get("artifact_base") or "")).resolve(strict=True)
        missing = set((failure.get("artifacts") or {}).get("missing") or [])
        declared = set(RUN_EVIDENCE.outputs(arm))
        runtime_record_missing = failure.get("runtime_only_output") is True
        if ((not missing and not runtime_record_missing)
                or (missing and not missing <= declared)):
            raise ValueError("late-result-original-missing-output-not-declared:" + node_id)
        output_rows = []
        for name in sorted(declared):
            path = (base / name).resolve(strict=True)
            if not path.is_relative_to(base) or not path.is_file():
                raise ValueError("late-result-output-binding-invalid:" + name)
            output_rows.append({"path": str(path), "sha256": _file_digest(path)})
        if not receipt.get("runtime_output"):
            raise ValueError("late-result-runtime-record-unbound:" + node_id)
        output_path = Path(receipt["runtime_output"]).resolve(strict=True)
        if not output_path.is_relative_to(base):
            raise ValueError("late-result-runtime-record-outside-scope:" + node_id)
        if runtime_record_missing:
            runtime_document, _runtime_preimage = RUN_EVIDENCE.read_document(output_path)
            if RUN_EVIDENCE.runtime_only(runtime_document):
                raise ValueError("late-result-scientific-record-still-runtime-only:" + node_id)
        row, registry_path = _row_for(arm)
        if (row.get("route") != str(Path(route_file).resolve())
                or row.get("node") != node_id or row.get("jobs") != expected_jobs
                or row.get("run_id") != arm.get("predecessor_id")
                or row.get("status") != "succeeded" or type(row.get("exit_code")) is not int
                or row.get("exit_code") != 0 or RR.classify_identity(row)[0] != "exited"
                or RR.resource_never_started(row)
                or resource_resume.row_digest(row) != failure.get("resource_sha256")
                or (arm.get("resource_binding") and
                    arm.get("resource_binding") != supervisor.OWNER_RESOURCE.resource_body_digest(row))):
            raise ValueError("late-result-resource-registry-binding-mismatch:" + node_id)
        sentinel_path = Path(str(row.get("sentinel") or ""))
        sentinel_raw = sentinel_path.read_bytes()
        if sentinel_raw.strip() != b"0":
            raise ValueError("late-result-exit-sentinel-mismatch:" + node_id)
        verification = _marker_proofs(route, node, jobs, module)
        lab_proof = _lab_arm_proofs(route, arm, row, output_path, verification)
        if lab_proof is None and not _same_byte_verification(output_rows, verification, base):
            raise ValueError("late-result-verification-does-not-bind-output-bytes:" + node_id)
        executions.append({"node": node_id, "run_id": row["run_id"],
                           "identity": current["identity"],
                           "resource_sha256": failure["resource_sha256"],
                           "registry_path": str(registry_path),
                           "sentinel_path": str(sentinel_path), "sentinel_sha256": _sha(sentinel_raw),
                           "armed_path": str(arm_path), "armed_sha256": arm_sha,
                           "outputs": output_rows, "verification": verification,
                           "lab_arms": lab_proof, "original_failure": failure})
        checked_files.extend(output_rows)
        if lab_proof:
            checked_files.extend(lab_proof["checked_files"])
    if not executions:
        raise ValueError("late-result-no-exact-missing-output-failure")
    checked_files = sorted({(row["path"], row["sha256"]): row for row in checked_files}.values(),
                           key=lambda row: row["path"])
    payload = {"schema": SCHEMA, "route_id": route["route_id"], "route_hash": route["route_hash"],
               "route_file": str(Path(route_file).resolve()), "route_sha256": _route_file_digest(route_file, route),
               "original_outcome_sha256": _sha(outcome_raw),
               "executions": executions, "checked_files": checked_files}
    key_body = dict(payload)
    payload["idempotency_key"] = _sha(_canonical(key_body))
    payload["proof_sha256"] = _sha(_canonical(payload))
    return payload


def _proofs_from_journal(entries: list, route: Mapping[str, Any], outcome_sha: str) -> list[dict]:
    found = {}
    for entry in entries:
        if (entry.get("actor") != ACTOR or entry.get("route_id") != route.get("route_id")
                or entry.get("route_hash") != route.get("route_hash")):
            continue
        rows = ((entry.get("evidence") or {}).get("accepted_resource_results") or [])
        if isinstance(rows, dict):
            rows = [rows]
        for proof in rows if isinstance(rows, list) else []:
            if (isinstance(proof, dict) and proof.get("schema") == SCHEMA
                    and proof.get("original_outcome_sha256") == outcome_sha
                    and proof.get("route_hash") == route.get("route_hash")):
                found[proof.get("idempotency_key")] = proof
    return [found[key] for key in sorted(found) if key]


def _terminal_proofs_from_journal(entries: list, route: Mapping[str, Any], outcome_sha: str) -> list[dict]:
    found = {}
    for entry in entries:
        if (entry.get("actor") != TERMINAL_ACTOR or entry.get("route_id") != route.get("route_id")
                or entry.get("route_hash") != route.get("route_hash")):
            continue
        rows = ((entry.get("evidence") or {}).get("terminal_adjudications") or [])
        if isinstance(rows, dict):
            rows = [rows]
        for proof in rows if isinstance(rows, list) else []:
            if (isinstance(proof, dict) and proof.get("schema") == TERMINAL_SCHEMA
                    and proof.get("original_outcome_sha256") == outcome_sha):
                found[proof.get("idempotency_key")] = proof
    return [found[key] for key in sorted(found) if key]


def _terminal_candidate(route: Mapping[str, Any], route_file: Path, outcome_raw: bytes,
                        ledger, jobs: Path) -> dict:
    module = _route_module()
    state = ledger.read_only_state()
    if (state.get("workflow_state") in {"CANCELLED", "FAILED_TERMINAL", "BLOCKED_HUMAN_GATE"}
            or state.get("journal_unreadable")
            or any(row.get("state") in WS.vocabulary().get("failure_states", ())
                   or row.get("state") == "RUNNING" for row in state.get("nodes", {}).values())):
        raise ValueError("late-terminal-unresolved-workflow-condition")
    existing = json.loads(outcome_raw)
    if route.get("owner_dispatch_depth", 0) > 0 and not existing.get("terminal_owner_attempt_id"):
        raise ValueError("late-terminal-owner-identity-absent")
    terminal = module.terminal_gate_observation(
        route, jobs=jobs, exact_terminal=bool(existing.get("terminal_commit_id")))
    if module.terminal_gate_proven(terminal) is not True:
        raise ValueError("late-terminal-gates-still-unproven")
    identity = {}
    if existing.get("terminal_owner_attempt_id"):
        identity = module._current_terminal_identity(route, terminal, existing["terminal_owner_attempt_id"])
        if (not identity or any(existing.get(key) not in (None, identity.get(current_key))
                for key, current_key in (("terminal_owner_attempt_id", "terminal_owner_attempt_id"),
                                         ("terminal_commit_id", "terminal_commit_id"),
                                         ("producer_binding_digest", "producer_binding_digest"),
                                         ("terminal_marker_digest", "terminal_marker_digest")))):
            raise ValueError("late-terminal-owner-identity-unproven")
    elif any(existing.get(key) for key in ("terminal_commit_id", "producer_binding_digest")):
        raise ValueError("late-terminal-owner-identity-incomplete")
    if existing.get("inline_finish_id"):
        import inline_finish
        finish = inline_finish.pending_state(Path(route.get("artifact_root", "")), route["route_id"])
        if not finish or finish.get("inline_finish_id") != existing["inline_finish_id"] or finish.get("state") != "finished":
            raise ValueError("late-terminal-inline-finish-pending")
    journal = ledger.journal()
    failures = [{"node": entry.get("node"), "state": entry.get("state"),
                 "sha256": _sha(_canonical(entry))}
                for entry in journal if entry.get("state") in WS.vocabulary().get("failure_states", ())]
    proof = {"schema": TERMINAL_SCHEMA, "route_id": route["route_id"], "route_hash": route["route_hash"],
             "route_file": str(Path(route_file).resolve()), "route_sha256": _route_file_digest(route_file, route),
             "original_outcome_sha256": _sha(outcome_raw), "terminal_gates": terminal,
             "terminal_marker_digest": identity.get("terminal_marker_digest") or _sha(_canonical(terminal)),
             "terminal_identity": identity, "historical_failures": failures}
    proof["idempotency_key"] = _sha(_canonical(proof))
    proof["proof_sha256"] = _sha(_canonical(proof))
    return proof


def _verify_terminal_proof(proof: Mapping[str, Any], route_file: Path, route: Mapping[str, Any],
                           outcome_raw: bytes, jobs: Path | None) -> bool:
    try:
        if (proof.get("schema") != TERMINAL_SCHEMA or proof.get("route_id") != route.get("route_id")
                or proof.get("route_hash") != route.get("route_hash")
                or proof.get("route_file") != str(Path(route_file).resolve())
                or proof.get("original_outcome_sha256") != _sha(outcome_raw)
                or proof.get("route_sha256") != _route_file_digest(route_file, route)):
            return False
        body = dict(proof)
        proof_sha = body.pop("proof_sha256", None)
        if proof_sha != _sha(_canonical(body)):
            return False
        key_body = {key: value for key, value in proof.items()
                    if key not in {"idempotency_key", "proof_sha256"}}
        if proof.get("idempotency_key") != _sha(_canonical(key_body)):
            return False
        if not _eligible(route, json.loads(outcome_raw), jobs=jobs):
            return False
        module = _route_module()
        terminal = module.terminal_gate_observation(
            route, jobs=jobs, exact_terminal=bool(proof.get("terminal_identity", {}).get("terminal_commit_id")))
        terminal_identity = proof.get("terminal_identity") or {}
        marker_digest = terminal_identity.get("terminal_marker_digest") or _sha(_canonical(terminal))
        if (module.terminal_gate_proven(terminal) is not True
                or _canonical(terminal) != _canonical(proof.get("terminal_gates"))
                or marker_digest != proof.get("terminal_marker_digest")):
            return False
        state = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs).read_only_state()
        if (state.get("journal_unreadable")
                or state.get("workflow_state") in {"CANCELLED", "FAILED_TERMINAL", "BLOCKED_HUMAN_GATE"}
                or any(row.get("state") in WS.vocabulary().get("failure_states", ())
                       or row.get("state") == "RUNNING" for row in state.get("nodes", {}).values())):
            return False
        if proof.get("historical_failures"):
            journal = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs).journal()
            current = [{"node": entry.get("node"), "state": entry.get("state"),
                        "sha256": _sha(_canonical(entry))}
                       for entry in journal if entry.get("state") in WS.vocabulary().get("failure_states", ())]
            if current != proof["historical_failures"]:
                return False
        identity = proof.get("terminal_identity") or {}
        if identity.get("terminal_owner_attempt_id"):
            current_identity = module._current_terminal_identity(
                route, terminal, identity["terminal_owner_attempt_id"])
            if current_identity != identity:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError, json.JSONDecodeError):
        return False


def _verify_proof(proof: Mapping[str, Any], route_file: Path, route: Mapping[str, Any],
                  outcome_raw: bytes, jobs: Path | None) -> bool:
    try:
        if (proof.get("schema") != SCHEMA or proof.get("route_id") != route.get("route_id")
                or proof.get("route_hash") != route.get("route_hash")
                or proof.get("route_file") != str(Path(route_file).resolve())
                or proof.get("original_outcome_sha256") != _sha(outcome_raw)
                or proof.get("route_sha256") != _route_file_digest(route_file, route)):
            return False
        body = dict(proof)
        recorded_proof_sha = body.pop("proof_sha256", None)
        if recorded_proof_sha != _sha(_canonical(body)):
            return False
        key_body = {key: value for key, value in proof.items()
                    if key not in {"idempotency_key", "proof_sha256"}}
        if proof.get("idempotency_key") != _sha(_canonical(key_body)):
            return False
        supervisor = _supervisor()
        module = _route_module()
        verified_jobs = None
        for execution in proof.get("executions") or []:
            arm_path = Path(execution["armed_path"])
            if _file_digest(arm_path) != execution.get("armed_sha256"):
                return False
            arm = json.loads(arm_path.read_bytes())
            expected_jobs = str(Path(jobs).resolve()) if jobs is not None else str(Path(arm.get("jobs") or "").resolve())
            if verified_jobs is None:
                verified_jobs = Path(expected_jobs)
            elif verified_jobs != Path(expected_jobs):
                return False
            if (arm.get("route_id") != route.get("route_id")
                    or arm.get("route_hash") != route.get("route_hash")
                    or arm.get("route_file") != str(Path(route_file).resolve())
                    or arm.get("jobs") != expected_jobs
                    or arm.get("predecessor_id") != execution.get("run_id")):
                return False
            row, _registry_path = _row_for(arm)
            if (str(Path(arm["resource_registry"])) != execution.get("registry_path")
                    or row.get("run_id") != execution.get("run_id")
                    or row.get("status") != "succeeded" or type(row.get("exit_code")) is not int
                    or row.get("exit_code") != 0 or RR.classify_identity(row)[0] != "exited"
                    or resource_resume.row_digest(row) != execution.get("resource_sha256")
                    or f"{row['run_id']}:{row.get('pid')}:{row.get('starttime')}:{row.get('exit_code')}"
                       != execution.get("identity")):
                return False
            sentinel = Path(execution["sentinel_path"])
            sentinel_raw = sentinel.read_bytes()
            if sentinel_raw.strip() != b"0" or _sha(sentinel_raw) != execution.get("sentinel_sha256"):
                return False
            if arm.get("resource_binding") and arm["resource_binding"] != supervisor.OWNER_RESOURCE.resource_body_digest(row):
                return False
            outputs = {Path(item["path"]): item["sha256"] for item in execution.get("outputs") or []}
            import resource_run_evidence as RUN_EVIDENCE
            base = Path(str(arm.get("artifact_base") or "")).resolve(strict=True)
            if set(outputs) != {(base / name).resolve(strict=True) for name in RUN_EVIDENCE.outputs(arm)}:
                return False
            for path, expected in outputs.items():
                if not path.resolve(strict=True).is_relative_to(base) or _file_digest(path) != expected:
                    return False
            for item in execution.get("verification") or []:
                marker_path = Path(item["marker_path"])
                if _file_digest(marker_path) != item.get("marker_sha256"):
                    return False
                verify_node = WS.route_node(route, item["node"])
                if not verify_node:
                    return False
                proof_row = module._marker_identity_row(
                    route, verify_node, item["node"], item.get("completion_gate"), jobs=Path(expected_jobs),
                    exact_terminal=False)
                if proof_row.get("passed") is not True:
                    return False
                if _file_digest(Path(item["evidence_path"])) != item.get("evidence_sha256"):
                    return False
            lab_proof = _lab_arm_proofs(route, arm, row, Path(RUN_EVIDENCE.paths(arm)["runtime_output"]),
                                        execution.get("verification") or [])
            if lab_proof != execution.get("lab_arms"):
                return False
            if lab_proof is None and not _same_byte_verification(
                    list(outputs and ({"path": str(path), "sha256": digest}
                                      for path, digest in outputs.items()) or []),
                    execution.get("verification") or [], base):
                return False
        if verified_jobs is None:
            return False
        verified_ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=verified_jobs)
        current_outcome = json.loads(outcome_raw)
        if not _eligible(route, current_outcome, jobs=verified_jobs, ledger=verified_ledger):
            return False
        failure_rows = {node["id"]: failure for node, failure in _failure_rows(route, verified_ledger)}
        proof_nodes = {execution.get("node") for execution in proof.get("executions") or []}
        if (set(failure_rows) != proof_nodes
                or any(execution.get("original_failure") != failure_rows.get(execution.get("node"))
                       for execution in proof.get("executions") or [])):
            return False
        for item in proof.get("checked_files") or []:
            if _file_digest(Path(item["path"])) != item.get("sha256"):
                return False
        return bool(proof.get("executions"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, json.JSONDecodeError):
        return False


def _producer_publication(route: Mapping[str, Any], proof: Mapping[str, Any]) -> dict:
    try:
        import artifact_producer as AP
        root = Path(route["artifact_root"]).resolve()
        record = AP.route_cycle_for(root, route)
        if not record:
            return {"status": "not-bound"}
        if record.get("deleted_at"):
            return {"status": "refused", "reason": "producer-cycle-deleted"}
        manifest_path = AP._record_cycle_manifest_path(root, record)
        manifest = AP._read_json(manifest_path) if manifest_path.is_file() else None
        abandoned = (isinstance(manifest, dict)
                     and (manifest.get("cycle") or {}).get("state") == "abandoned")
        if ((record.get("abandon_reason") or abandoned)
                and not _abandoned_cycle_snapshot(route, record)):
            return {"status": "refused", "reason": "producer-cycle-explicitly-abandoned-or-ambiguous"}
        directory = manifest_path.parent.resolve(strict=True)
        artifacts_dir = directory / "artifacts"
        if artifacts_dir.is_symlink() or not artifacts_dir.is_dir():
            raise ValueError("late-result-producer-artifacts-dir-invalid")
        target = artifacts_dir / "late-results" / f"{proof['idempotency_key']}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        current = directory
        for part in target.relative_to(directory).parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("late-result-producer-proof-symlink")
        if not target.parent.resolve().is_relative_to(directory):
            raise ValueError("late-result-producer-proof-outside-cycle")
        raw = json.dumps(proof, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        try:
            fd, temporary = tempfile.mkstemp(prefix=".late-result-", dir=target.parent)
        except FileExistsError:
            if _read_regular(target) != raw:
                raise ValueError("late-result-producer-proof-conflict")
        else:
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    if _read_regular(target) != raw:
                        raise ValueError("late-result-producer-proof-conflict")
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        if record.get("state") != "sealed":
            return {"status": "deferred", "reason": "producer-cycle-open", "proof_path": str(target)}
        result = AP.refresh_cycle(root, record["cycle_id"], trigger="late-result-adjudication")
        return {**result, "proof_path": str(target)}
    except (ImportError, KeyError, OSError, ValueError, TypeError) as exc:
        return {"status": "pending", "reason": str(exc)}


def _mechanical_terminal_failure(route: Mapping[str, Any], outcome: Mapping[str, Any], ledger,
                                 state: Mapping[str, Any]) -> bool:
    failed = {node for node, row in (state.get("nodes") or {}).items()
              if row.get("state") == "FAILED_TERMINAL"}
    terminal = set(WS.route_terminal_nodes(route))
    gates = outcome.get("terminal_gates") or {}
    if not isinstance(gates, dict) or not failed or not failed <= terminal:
        return False
    journal = ledger.journal()
    for node in failed:
        gate = gates.get(node) or {}
        if (not isinstance(gate, dict) or gate.get("passed") is not False
                or gate.get("reason") not in _MECHANICAL_GATE_REASONS):
            return False
        entry = next((item for item in reversed(journal)
                      if item.get("node") == node and item.get("state") == "FAILED_TERMINAL"), None)
        evidence = (entry or {}).get("evidence") or {}
        artifacts = evidence.get("artifacts") or {}
        reason = evidence.get("reason") or evidence.get("failure_class") or evidence.get("code")
        missing_output = (artifacts.get("reason") == _OUTPUT_FAILURE
                          and isinstance(artifacts.get("missing"), list) and bool(artifacts["missing"]))
        if (entry is None or entry.get("actor") not in {"workflow-supervisor", "completion-controller"}
                or evidence.get("decision") in {"stop", "revise"}
                or (reason not in _MECHANICAL_GATE_REASONS and not missing_output)):
            return False
    return True


def _mechanical_missing_record_close(route: Mapping[str, Any], outcome: Mapping[str, Any],
                                     ledger, state: Mapping[str, Any]) -> bool:
    failures = _failure_rows(route, ledger)
    if (outcome.get("terminal_gate_proven") is not False or not failures
            or state.get("journal_unreadable")):
        return False
    gates = outcome.get("terminal_gates") or {}
    if not any(row.get("passed") is False and row.get("reason") in _MECHANICAL_GATE_REASONS
               for row in gates.values() if isinstance(row, dict)):
        return False
    failure_nodes = {node["id"] for node, _ in failures}
    mechanical_terminal_nodes = set()
    has_failed_terminal_node = any(row.get("state") == "FAILED_TERMINAL"
                                   for row in (state.get("nodes") or {}).values())
    if has_failed_terminal_node:
        if not _mechanical_terminal_failure(route, outcome, ledger, state):
            return False
        mechanical_terminal_nodes = {node for node, row in (state.get("nodes") or {}).items()
                                     if row.get("state") == "FAILED_TERMINAL"}
    failure_states = WS.vocabulary().get("failure_states", ())
    return all(row.get("state") not in failure_states
               or node in failure_nodes | mechanical_terminal_nodes
               for node, row in (state.get("nodes") or {}).items())


def _has_explicit_user_decision(route: Mapping[str, Any], ledger) -> bool:
    try:
        import route_parent_close
        if route_parent_close.ledger_intent(route, ledger):
            return True
    except (OSError, ValueError, KeyError, TypeError):
        return True
    return any(entry.get("actor") == "release"
               and (entry.get("evidence") or {}).get("decision") in {"stop", "revise"}
               for entry in ledger.journal())


def _eligible(route: Mapping[str, Any], outcome: Mapping[str, Any], *, jobs=None, ledger=None) -> bool:
    if (outcome.get("terminal_gate_proven") is not False or outcome.get("autoclose") is not None
            or outcome.get("disposition") in {"abandoned", "operator-decision", "cancelled"}
            or route.get("stop_reason") is not None or route.get("workflow_state") == "CANCELLED"):
        return False
    if ledger is None:
        ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
    state = ledger.read_only_state()
    if (state.get("journal_unreadable")
            or state.get("workflow_state") in {"CANCELLED", "COMPLETE", "BLOCKED_HUMAN_GATE"}
            or _has_explicit_user_decision(route, ledger)):
        return False
    has_failed_terminal_node = any(row.get("state") == "FAILED_TERMINAL"
                                   for row in (state.get("nodes") or {}).values())
    if (has_failed_terminal_node
            and not _mechanical_terminal_failure(route, outcome, ledger, state)):
        return False
    try:
        import artifact_producer as AP
        record = AP.route_cycle_for(Path(route["artifact_root"]).resolve(), route)
        if record:
            if record.get("deleted_at"):
                return False
            if record.get("state") == "sealed":
                manifest_path = AP._record_cycle_manifest_path(Path(route["artifact_root"]), record)
                manifest = AP._read_json(manifest_path) if manifest_path.is_file() else None
                cycle_state = (manifest.get("cycle") or {}).get("state") if isinstance(manifest, dict) else None
                if cycle_state == "abandoned":
                    if (not _abandoned_cycle_snapshot(route, record)
                            or not _mechanical_missing_record_close(route, outcome, ledger, state)):
                        return False
                elif record.get("abandon_reason"):
                    return False
                elif not isinstance(manifest, dict):
                    return False
    except (ImportError, KeyError, OSError, ValueError, TypeError):
        return False
    return True


def adjudicate(route: Mapping[str, Any], route_file: Path, jobs: Path, *, lock_held: bool = False) -> dict:
    """Recheck and append one exact result observation; never start or replay work."""
    supervisor = _supervisor()
    route_file = Path(route_file).resolve()
    jobs = Path(jobs).resolve() if jobs is not None else Path(supervisor.default_jobs_path()).resolve()
    outcome_path = _route_module().outcome_path(route_file)
    try:
        outcome_raw = _read_regular(outcome_path)
        outcome = json.loads(outcome_raw)
    except (OSError, ValueError, UnicodeError) as exc:
        return {"accepted": False, "reason": "closed-outcome-unreadable", "detail": str(exc)}
    ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
    if not isinstance(outcome, dict) or not _eligible(route, outcome, jobs=jobs, ledger=ledger):
        return {"accepted": False, "reason": "closed-outcome-not-eligible"}
    before = ledger.read_only_state()
    if before.get("journal_unreadable"):
        return {"accepted": False, "reason": "workflow-journal-unreadable"}
    if before.get("workflow_state") in {"CANCELLED", "COMPLETE", "BLOCKED_HUMAN_GATE"}:
        return {"accepted": False, "reason": "workflow-terminal-condition-preserved"}
    if (before.get("workflow_state") == "FAILED_TERMINAL"
            and not _mechanical_terminal_failure(route, outcome, ledger, before)):
        return {"accepted": False, "reason": "workflow-terminal-condition-preserved"}
    if not before.get("journal_exists"):
        try:
            _terminal_candidate(route, route_file, outcome_raw, ledger, jobs)
        except (OSError, ValueError, KeyError, TypeError, AttributeError,
                json.JSONDecodeError) as exc:
            return {"accepted": False, "reason": str(exc)}
    try:
        import route_parent_close
        if route_parent_close.intent(route, jobs):
            return {"accepted": False, "reason": "parent-close-intent-preserved"}
    except (OSError, ValueError):
        return {"accepted": False, "reason": "parent-close-state-unreadable"}

    def run_locked() -> dict:
        state = ledger.read_only_state()
        if (state.get("workflow_state") in {"CANCELLED", "COMPLETE", "BLOCKED_HUMAN_GATE"}
                or state.get("journal_unreadable")):
            return {"accepted": False, "reason": "workflow-terminal-condition-preserved"}
        if (state.get("workflow_state") == "FAILED_TERMINAL"
                and not _mechanical_terminal_failure(route, outcome, ledger, state)):
            return {"accepted": False, "reason": "workflow-terminal-condition-preserved"}
        if not _eligible(route, outcome, jobs=jobs, ledger=ledger):
            return {"accepted": False, "reason": "closed-outcome-not-eligible"}
        try:
            import route_parent_close
            if route_parent_close.ledger_intent(route, ledger):
                return {"accepted": False, "reason": "parent-close-intent-preserved"}
        except (OSError, ValueError):
            return {"accepted": False, "reason": "parent-close-state-unreadable"}
        journal = ledger.journal()
        outcome_now = _read_regular(outcome_path)
        if outcome_now != outcome_raw:
            return {"accepted": False, "reason": "closed-outcome-changed-during-adjudication"}
        existing = _proofs_from_journal(journal, route, _sha(outcome_raw))
        if existing:
            proof = existing[-1]
            if not _verify_proof(proof, route_file, route, outcome_raw, jobs):
                return {"accepted": False, "reason": "stored-late-result-proof-no-longer-current"}
            return {"accepted": True, "created": False, "proof": proof}
        terminal_proofs = _terminal_proofs_from_journal(journal, route, _sha(outcome_raw))
        if terminal_proofs:
            proof = terminal_proofs[-1]
            if not _verify_terminal_proof(proof, route_file, route, outcome_raw, jobs):
                return {"accepted": False, "reason": "stored-late-terminal-proof-no-longer-current"}
            return {"accepted": True, "created": False, "proof": proof}
        if not _failure_rows(route, ledger):
            try:
                proof = _terminal_candidate(route, route_file, outcome_raw, ledger, jobs)
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                return {"accepted": False, "reason": str(exc)}
            if not _verify_terminal_proof(proof, route_file, route, outcome_raw, jobs):
                return {"accepted": False, "reason": "late-terminal-snapshot-changed-before-append"}
            if _read_regular(outcome_path) != outcome_raw:
                return {"accepted": False, "reason": "closed-outcome-changed-before-append"}
            ledger._append({"at": WS.now_iso(), "route_id": route["route_id"],
                            "route_hash": route["route_hash"], "actor": TERMINAL_ACTOR,
                            "evidence": {"terminal_adjudications": [proof]}, "pid": os.getpid()})
            return {"accepted": True, "created": True, "proof": proof}
        try:
            proof = _candidate(route, route_file, outcome_raw, ledger, jobs)
        except (OSError, ValueError, KeyError, TypeError, AttributeError,
                json.JSONDecodeError) as exc:
            return {"accepted": False, "reason": str(exc)}
        if not _verify_proof(proof, route_file, route, outcome_raw, jobs):
            return {"accepted": False, "reason": "late-result-snapshot-changed-before-append"}
        if _read_regular(outcome_path) != outcome_raw:
            return {"accepted": False, "reason": "closed-outcome-changed-before-append"}
        ledger._append({"at": WS.now_iso(), "route_id": route["route_id"],
                        "route_hash": route["route_hash"], "actor": ACTOR,
                        "evidence": {"accepted_resource_results": [proof]}, "pid": os.getpid()})
        return {"accepted": True, "created": True, "proof": proof}

    if lock_held:
        result = run_locked()
    else:
        with ledger.lock():
            result = run_locked()
    if result.get("accepted"):
        result["producer_publication"] = _producer_publication(route, result["proof"])
    return result


def current_outcome(route_file: Path, route: Mapping[str, Any], jobs: Path | None = None) -> dict | None:
    """Purely fold exact current adjudications over the immutable raw outcome."""
    route_file = Path(route_file).resolve()
    raw_path = _route_module().outcome_path(route_file)
    try:
        raw = _read_regular(raw_path)
        outcome = json.loads(raw)
    except FileNotFoundError:
        return None
    if (not isinstance(outcome, dict) or outcome.get("route_id") != route.get("route_id")
            or outcome.get("route_hash") != route.get("route_hash")):
        raise ValueError("late-result-outcome-route-mismatch")
    if outcome.get("terminal_gate_proven") is not False:
        return outcome
    try:
        ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
        state = ledger.read_only_state()
        journal = ledger.journal()
    except (OSError, ValueError, TypeError):
        return outcome
    if state.get("journal_unreadable"):
        return outcome
    proofs = [proof for proof in _proofs_from_journal(journal, route, _sha(raw))
              if _verify_proof(proof, route_file, route, raw,
                               Path(jobs).resolve() if jobs is not None else None)]
    terminal_proofs = [proof for proof in _terminal_proofs_from_journal(journal, route, _sha(raw))
                       if _verify_terminal_proof(proof, route_file, route, raw, jobs)]
    if not proofs and not terminal_proofs:
        return outcome
    accepted_nodes = {execution["node"] for proof in proofs
                      for execution in proof.get("executions") or []}
    terminal = _route_module().terminal_gate_observation(
        route, jobs=jobs, exact_terminal=bool(outcome.get("terminal_commit_id")))
    no_other_failures = all(
        row.get("state") not in WS.vocabulary().get("failure_states", ())
        or node in accepted_nodes
        for node, row in state.get("nodes", {}).items())
    complete = bool(terminal_proofs) or (_eligible(route, outcome, jobs=jobs, ledger=ledger)
                and _route_module().terminal_gate_proven(terminal) is True
                and no_other_failures and state.get("workflow_state") != "CANCELLED"
                and route.get("stop_reason") is None)
    terminal_completion_pending = []
    if complete and route.get("owner_dispatch_depth", 0) > 0 and not outcome.get("terminal_owner_attempt_id"):
        complete = False
        terminal_completion_pending.append("terminal-owner-identity-unproven")
    current_identity = {}
    if complete and outcome.get("terminal_owner_attempt_id"):
        current_identity = _route_module()._current_terminal_identity(
            route, terminal, outcome["terminal_owner_attempt_id"])
        complete = bool(current_identity) and all(
            outcome.get(key) in (None, current_identity.get(current_key))
            for key, current_key in (("terminal_owner_attempt_id", "terminal_owner_attempt_id"),
                                     ("terminal_commit_id", "terminal_commit_id"),
                                     ("producer_binding_digest", "producer_binding_digest"),
                                     ("terminal_marker_digest", "terminal_marker_digest")))
    if complete and outcome.get("inline_finish_id"):
        try:
            import inline_finish
            finish = inline_finish.pending_state(Path(route.get("artifact_root", "")), route["route_id"])
            complete = bool(finish and finish.get("inline_finish_id") == outcome["inline_finish_id"]
                            and finish.get("state") == "finished")
        except (OSError, ValueError, KeyError):
            complete = False
    projected = dict(outcome)
    projected.update({"historical_terminal_gate_proven": False,
                      "historical_outcome_sha256": _sha(raw),
                      "terminal_gates": terminal,
                      "terminal_gate_proven": complete})
    if proofs:
        projected.update({"resource_result_accepted": True,
                          "resource_result_nodes": sorted(accepted_nodes),
                          "accepted_resource_results": proofs})
    if terminal_proofs:
        terminal_proof = terminal_proofs[-1]
        projected["terminal_adjudication"] = terminal_proof
        projected["terminal_marker_digest"] = terminal_proof["terminal_marker_digest"]
        projected.update(terminal_proof.get("terminal_identity") or {})
    elif complete and current_identity:
        projected.update(current_identity)
    elif complete:
        projected["terminal_marker_digest"] = "sha256:" + _sha(_canonical(terminal))
    if terminal_completion_pending:
        projected["terminal_completion_pending"] = terminal_completion_pending
    return projected
