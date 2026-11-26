#!/usr/bin/env python3
"""Read-only projection of recorded report verification and completion state."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any, Iterable, Mapping

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import artifact_lifecycle
import artifact_locator
import artifact_manifest
import artifact_producer
import artifact_reader
import dispatch_contract
import route_identity

MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_REGISTRY_BYTES = 32 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024
_SHA256 = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_ROUTE_ID = re.compile(r"rt-[A-Za-z0-9._-]{1,128}\Z")
_NODE_ID = re.compile(r"[A-Za-z0-9._-]{1,160}\Z")


class ProjectionProblem(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _stat_key(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


class ReadSnapshot:
    """Bounded byte cache for one projection call, with a final stat check."""

    def __init__(self) -> None:
        self._bytes: dict[Path, bytes] = {}
        self._stats: dict[Path, tuple[int, int, int, int, int, int]] = {}

    def read(self, path: Path, limit: int = MAX_JSON_BYTES) -> bytes:
        path = Path(path)
        if path in self._bytes:
            data = self._bytes[path]
            if len(data) > limit:
                raise ProjectionProblem("input-exceeds-read-bound")
            return data
        try:
            before = path.lstat()
        except OSError as exc:
            raise ProjectionProblem("input-unreadable") from exc
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise ProjectionProblem("input-kind-invalid")
        if before.st_size > limit:
            raise ProjectionProblem("input-exceeds-read-bound")
        if before.st_size + sum(len(value) for value in self._bytes.values()) > MAX_SNAPSHOT_BYTES:
            raise ProjectionProblem("snapshot-exceeds-read-bound")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
            with os.fdopen(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if _stat_key(opened) != _stat_key(before):
                    raise ProjectionProblem("input-changed-during-read")
                data = stream.read(limit + 1)
                after = os.fstat(stream.fileno())
        except ProjectionProblem:
            raise
        except OSError as exc:
            raise ProjectionProblem("input-unreadable") from exc
        if len(data) > limit:
            raise ProjectionProblem("input-exceeds-read-bound")
        if _stat_key(after) != _stat_key(before):
            raise ProjectionProblem("input-changed-during-read")
        self._bytes[path] = data
        self._stats[path] = _stat_key(before)
        return data

    def json(self, path: Path, limit: int = MAX_JSON_BYTES) -> Any:
        try:
            return json.loads(self.read(path, limit).decode("utf-8"))
        except ProjectionProblem:
            raise
        except (UnicodeError, ValueError, TypeError) as exc:
            raise ProjectionProblem("input-malformed") from exc

    def unchanged(self) -> bool:
        for path, expected in self._stats.items():
            try:
                current = path.lstat()
            except OSError:
                return False
            if _stat_key(current) != expected:
                return False
        return True


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest_value(value: Any) -> str | None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        return None
    return value.removeprefix("sha256:")


def _safe_rel(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ProjectionProblem("artifact-locator-invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ProjectionProblem("artifact-locator-invalid")
    return path.as_posix()


def _safe_file(root: Path, relative: str) -> Path:
    relative = _safe_rel(relative)
    current = root
    for index, part in enumerate(relative.split("/")):
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ProjectionProblem("required-input-missing") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ProjectionProblem("artifact-symlink-forbidden")
        if index < len(relative.split("/")) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ProjectionProblem("artifact-parent-not-directory")
    if not stat.S_ISREG(info.st_mode):
        raise ProjectionProblem("artifact-not-regular")
    try:
        current.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ProjectionProblem("artifact-outside-cycle") from exc
    return current


def _revision_for(document: Mapping[str, Any], cycle_dir: Path, locator: str,
                  snapshot: ReadSnapshot, *, producer_route: str | None = None) -> dict[str, Any]:
    candidates = []
    for row in document.get("artifact_revisions", ()):
        if not isinstance(row, Mapping):
            continue
        loc = row.get("locator")
        if not isinstance(loc, Mapping) or loc.get("path") != locator:
            continue
        provenance = row.get("provenance")
        if producer_route and (not isinstance(provenance, Mapping)
                               or provenance.get("producer_route_id") != producer_route):
            continue
        candidates.append(dict(row))
    if not candidates:
        raise ProjectionProblem("artifact-revision-missing")
    path = _safe_file(cycle_dir, locator)
    raw = snapshot.read(path, MAX_REPORT_BYTES)
    digest = _digest(raw)
    matching = [row for row in candidates
                if _digest_value(row.get("content_digest")) == digest
                and row.get("byte_size") == len(raw)]
    if len(matching) != 1:
        raise ProjectionProblem("artifact-revision-conflict" if matching
                                else "artifact-revision-stale")
    return {**matching[0], "_path": path, "_sha256": digest}


def _unresolved(reason: str = "subject-unresolved") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "subject": {"state": "unresolved"},
        "verification": {"verdict": "unresolved", "state": "unresolved", "reason": reason,
                          "peers": [], "history": []},
        "completion": {"state": "unknown", "reason": reason,
                       "obligations": {"state": "unknown", "items": []}},
        "required_input_observation": {"state": "unresolved", "reasons": [reason]},
        "integrity": {"state": "unresolved", "reason": reason},
        "display": {},
    }


def _display(payload: dict[str, Any]) -> dict[str, Any]:
    verdict = payload.get("verification", {}).get("verdict", "unresolved")
    completion = payload.get("completion", {}).get("state", "unknown")
    input_state = payload.get("required_input_observation", {}).get("state", "unresolved")
    verification_labels = {"PASS": "검증 통과", "FAIL": "검증 실패", "unresolved": "검증 미확정"}
    completion_labels = {"complete": "작업 완료", "pending": "마감 대기", "blocked": "완료 차단",
                         "unknown": "완료 미확정", "not-applicable": "완료 상태 해당 없음"}
    input_labels = {"confirmed": "필수 입력 확인", "failed": "필수 입력 확인 실패",
                    "unresolved": "필수 입력 미확정"}
    reasons = payload.get("required_input_observation", {}).get("reasons") or []
    reason = reasons[0] if reasons else payload.get("verification", {}).get("reason")
    reason_labels = {
        "report-cycle-unadmitted": "담당 작업의 검증·마감 확인 필요",
        "route-hash-binding-mismatch": "작업과 보고서 기록이 일치하지 않음 · 담당 작업 확인 필요",
        "required-input-digest-mismatch": "검증 입력이 바뀜 · 담당 작업 재확인 필요",
        "artifact-revision-stale": "보고서 변경 뒤 검증 기록 확인 필요",
        "report-source-kind-invalid": "보고서 경로 확인 필요",
    }
    detail = None
    if (reason == "report-cycle-unadmitted"
            and payload.get("report_observation", {}).get("state") == "present"):
        detail = "보고서 있음 · 담당 작업의 검증·마감 확인 필요"
    payload["display"] = {
        "verification_label": verification_labels.get(verdict, "검증 미확정"),
        "completion_label": completion_labels.get(completion, "완료 미확정"),
        "required_input_label": input_labels.get(input_state, "필수 입력 미확정"),
        "detail_label": detail,
        "reason_label": reason_labels.get(reason, "보고서 상태 확인 필요") if reason else None,
        "limitations": [
            "기존 보고서·검토 본문과 원래 판정은 변경하지 않았습니다.",
            "조회는 새 과학 검증이나 매체 해독을 수행하지 않습니다.",
        ],
        "entrypoints": payload.get("entrypoints", []),
    }
    return payload


def report_detail_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """An absent report is normal; retain all observed verification problems."""
    if payload.get("verification", {}).get("reason") == "report-source-unavailable":
        return None
    return _display(payload)


def _route_nodes(route: Mapping[str, Any]) -> list[dict[str, Any]]:
    nodes = route.get("nodes")
    if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
        raise ProjectionProblem("route-nodes-invalid")
    return nodes


def _find_report_node(route: Mapping[str, Any], selected: Mapping[str, Any]) -> dict[str, Any]:
    outputs = {Path(str(row["locator"]["path"])).as_posix().removeprefix("artifacts/")
               for row in selected.values()}
    matches = [node for node in _route_nodes(route)
               if outputs.issubset({_safe_rel(x) for x in node.get("outputs", [])
                                    if isinstance(x, str)})]
    if len(matches) != 1:
        raise ProjectionProblem("report-node-ambiguous")
    return matches[0]


def _report_peers(route: Mapping[str, Any], report_node: Mapping[str, Any]) -> list[dict[str, Any]]:
    nodes = _route_nodes(route)
    candidates = [node for node in nodes
                  if node.get("kind") == "review-worker"
                  and node.get("leg_class") == "peer"
                  and node.get("parallel_group_kind") == "verify"
                  and report_node.get("id") in (node.get("depends_on") or [])]
    group_ids = {node.get("parallel_group") for node in candidates if node.get("parallel_group")}
    peers = [node for node in nodes
             if node.get("parallel_group") in group_ids
             and node.get("kind") == "review-worker"
             and node.get("leg_class") == "peer"]
    unique = {node.get("id"): node for node in peers if isinstance(node.get("id"), str)}
    if not candidates or len(unique) != len(peers):
        raise ProjectionProblem("required-verification-peers-missing")
    if any(node.get("parallel_join_policy") != "all" for node in peers):
        raise ProjectionProblem("verification-join-policy-unsupported")
    return [unique[key] for key in sorted(unique)]


def _verdict_history(node: Mapping[str, Any], marker: Mapping[str, Any], marker_path: Path,
                     snapshot: ReadSnapshot, route_id: str) -> list[dict[str, Any]]:
    history = []
    current = marker
    current_path = marker_path
    seen: set[int] = set()
    for _ in range(32):
        revision = current.get("revision")
        if current.get("stage_authority") != "revision" or not isinstance(revision, Mapping):
            break
        sequence = revision.get("of_sequence")
        expected_marker_hash = _digest_value(revision.get("of_marker_sha256"))
        if not isinstance(sequence, int) or sequence < 1 or sequence in seen or not expected_marker_hash:
            break
        seen.add(sequence)
        prior_path = marker_path.with_name(f"{node['id']}.{sequence}.json")
        try:
            prior_raw = snapshot.read(prior_path, 2 * 1024 * 1024)
            if _digest(prior_raw) != expected_marker_hash:
                break
            prior = json.loads(prior_raw.decode("utf-8"))
        except (ProjectionProblem, UnicodeError, ValueError, TypeError):
            break
        evidence = prior.get("evidence") if isinstance(prior, dict) else None
        evidence_path = evidence.get("path") if isinstance(evidence, dict) else None
        try:
            verdict_path = Path(evidence_path)
            raw = snapshot.read(verdict_path, MAX_REPORT_BYTES)
            if _digest(raw) != _digest_value(evidence.get("sha256")):
                raise ProjectionProblem("historical-evidence-unbound")
            verdict = json.loads(raw.decode("utf-8"))
        except (ProjectionProblem, TypeError, UnicodeError, ValueError):
            verdict = None
        if (isinstance(verdict, dict) and verdict.get("schema") == "acfu-eval-verdict-v1"
                and verdict.get("route") == route_id and verdict.get("leg") == node.get("id")
                and verdict.get("verdict") in {"PASS", "FAIL"}):
            history.append({"sequence": sequence, "round": verdict.get("round"),
                            "verdict": verdict.get("verdict")})
        else:
            history.append({"sequence": sequence, "verdict": "unresolved",
                            "reason": "historical-evidence-unbound"})
        current, current_path = prior, prior_path
    return history


def _registry_rows(jobs: Path, route: Mapping[str, Any], snapshot: ReadSnapshot) -> list[tuple[str, dict[str, str]]]:
    raw = snapshot.read(jobs, MAX_REGISTRY_BYTES)
    rows = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = dispatch_contract.parse_registry_metadata(fields[5])
        rid = metadata.get("owner_route_id") or metadata.get("route_id")
        rhash = metadata.get("owner_route_hash") or metadata.get("route_hash")
        if rid == route.get("route_id") and rhash == route.get("route_hash"):
            rows.append((fields[1], metadata))
    return rows


def _obligations(jobs: Path, rows: Iterable[tuple[str, Mapping[str, Any]]]) -> dict[str, Any]:
    ids: set[str] = set()
    for _status, metadata in rows:
        for key in ("batch_obligation_id", "peer_obligation_id", "obligation_id", "duty_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                ids.add(value)
        for key in ("obligation_ids", "peer_obligation_ids"):
            value = metadata.get(key)
            if isinstance(value, list):
                ids.update(item for item in value if isinstance(item, str) and item)
    if not ids:
        return {"state": "not-applicable", "items": []}
    items = []
    try:
        import dispatch_batch_obligations
        import peer_obligations
        store = peer_obligations.ObligationStore(jobs.parent)
        for duty_id in sorted(ids):
            record = (dispatch_batch_obligations.read(jobs, duty_id)
                      if duty_id.startswith("batch-") else store.get(duty_id))
            items.append({"id": duty_id, "state": record.get("state") if record else "unknown",
                          "reason": (record.get("reason") if record else "obligation-unreadable")})
    except Exception:
        return {"state": "unknown", "items": [{"state": "unknown", "reason": "obligation-read-failed"}]}
    states = {item["state"] for item in items}
    if states and states <= {"complete", "cancelled"}:
        state = "complete"
    elif "unknown" in states or not states <= {
            "complete", "cancelled", "observing", "delivery-pending",
            "pending", "cleanup-pending"}:
        state = "unknown"
    else:
        state = "pending"
    return {"state": state, "items": items}


def _completion(root: Path, cycle_dir: Path, document: Mapping[str, Any], route: Mapping[str, Any],
                route_path: Path, jobs: Path | None, snapshot: ReadSnapshot) -> dict[str, Any]:
    state = "unknown"
    reason = "completion-unobserved"
    rows: list[tuple[str, dict[str, str]]] = []
    if jobs is not None:
        try:
            rows = _registry_rows(jobs, route, snapshot)
            owners = [(status, meta) for status, meta in rows
                      if meta.get("worker_type") == "owner" and meta.get("dispatch_depth") == "1"]
            if len(owners) == 1:
                import dispatch_terminal_commit
                observed = dispatch_terminal_commit.owner_completion_state(jobs, *owners[0])
                if observed.state != "not-applicable":
                    state, reason = observed.state, observed.reason or "owner-completion-observed"
        except Exception:
            state, reason = "unknown", "owner-completion-read-failed"
    if state in {"unknown", "not-applicable"}:
        try:
            snapshots = artifact_lifecycle.read_manifest_snapshots(
                root, str(document.get("cycle", {}).get("cycle_id")))
            preserved = [item[1] for item in snapshots]
            result = artifact_lifecycle.evaluate_cycle_completion(
                document, content_root=cycle_dir, route_file=route_path,
                expected_root_id=str(document.get("artifact_root_id") or ""),
                preserved=preserved if preserved else None,
                payload_verified=False,
            )
            if result.state == "complete":
                state, reason = "complete", "sealed-cycle-lifecycle-verified"
            elif result.state == "incomplete":
                state, reason = "pending", "cycle-not-completed"
            else:
                reason = (result.reasons[0].code if result.reasons else "cycle-lifecycle-unverified")
        except Exception:
            reason = "cycle-lifecycle-unverified"
    obligations = (_obligations(jobs, rows) if jobs is not None
                   else {"state": "unknown", "items": []})
    return {"state": state, "reason": reason, "obligations": obligations}


def _verdict_limitations(verdict: Mapping[str, Any]) -> dict[str, Any]:
    """Carry bounded existing scope disclosures without interpreting them."""
    result = {}
    for key in ("unverified", "fallback", "limitations"):
        value = verdict.get(key)
        if value is not None and len(json.dumps(value, ensure_ascii=False)) <= 8192:
            result[key] = value
    return result


def _peer_result(root: Path, route: Mapping[str, Any], node: Mapping[str, Any],
                 cycle_dir: Path, document: Mapping[str, Any], input_revisions: Mapping[str, dict],
                 jobs: Path | None, snapshot: ReadSnapshot) -> tuple[dict[str, Any], str | None]:
    node_id = node.get("id")
    if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id):
        return ({"leg": str(node_id or "unknown"), "verdict": "unresolved",
                 "reason": "peer-identity-invalid"}, "peer-identity-invalid")
    outputs = node.get("outputs")
    if not isinstance(outputs, list) or len(outputs) != 1 or not isinstance(outputs[0], str):
        return ({"leg": node_id, "verdict": "unresolved", "reason": "peer-output-ambiguous"},
                "peer-output-ambiguous")
    try:
        output = _safe_rel("artifacts/" + _safe_rel(outputs[0]))
        output_rev = _revision_for(document, cycle_dir, output, snapshot,
                                   producer_route=str(route.get("route_id")))
        verdict_path = output_rev["_path"]
        verdict = json.loads(snapshot.read(verdict_path, MAX_REPORT_BYTES).decode("utf-8"))
    except (ProjectionProblem, UnicodeError, ValueError, TypeError):
        return ({"leg": node_id, "verdict": "unresolved", "reason": "peer-output-unreadable"},
                "peer-output-unreadable")
    if (not isinstance(verdict, dict) or verdict.get("schema") != "acfu-eval-verdict-v1"
            or verdict.get("route") != route.get("route_id") or verdict.get("leg") != node_id
            or verdict.get("verdict") not in {"PASS", "FAIL"}
            or not isinstance(verdict.get("round"), int) or isinstance(verdict.get("round"), bool)
            or verdict.get("round") < 1):
        return ({"leg": node_id, "verdict": "unresolved", "reason": "peer-verdict-identity-invalid"},
                "peer-verdict-identity-invalid")
    raw_inputs = verdict.get("inputs")
    declared = node.get("inputs")
    if not isinstance(raw_inputs, dict) or not isinstance(declared, list):
        return ({"leg": node_id, "verdict": "unresolved", "reason": "peer-inputs-malformed"},
                "peer-inputs-malformed")
    required_report_inputs = {"report/REPORT.md", "report/index.html",
                              "report/report_manifest.json"}
    declared_report_inputs = {item for item in declared
                              if isinstance(item, str) and item.startswith("report/")}
    if not required_report_inputs.issubset(declared_report_inputs):
        return ({"leg": node_id, "verdict": "unresolved",
                 "reason": "required-input-declaration-incomplete"},
                "required-input-failed")
    for relative in declared:
        if not isinstance(relative, str):
            return ({"leg": node_id, "verdict": "unresolved", "reason": "peer-inputs-malformed"},
                    "peer-inputs-malformed")
        try:
            key = _safe_rel(relative)
            revision = (input_revisions[key] if key.startswith("report/") else
                        _revision_for(document, cycle_dir, "artifacts/" + key, snapshot))
            expected_path = str(revision["_path"].resolve(strict=True))
        except (ProjectionProblem, KeyError, OSError, ValueError):
            return ({"leg": node_id, "verdict": "unresolved", "reason": "required-input-missing",
                     "input": relative}, "required-input-failed")
        digest = raw_inputs.get(expected_path)
        if _digest_value(digest) != revision["_sha256"]:
            return ({"leg": node_id, "verdict": "unresolved", "reason": "required-input-digest-mismatch",
                     "input": relative}, "required-input-failed")
    if jobs is None:
        return ({"leg": node_id, "verdict": "unresolved", "reason": "dispatch-jobs-unavailable",
                 "round": verdict["round"]}, None)
    try:
        capability_route = artifact_lifecycle._load_capability_route()
        marker_path = capability_route.completion_dir(str(route["route_id"]), jobs=jobs) / f"{node_id}.json"
        if verdict["verdict"] == "FAIL" and not marker_path.exists():
            registry_lines = snapshot.read(jobs, MAX_REGISTRY_BYTES).decode(
                "utf-8", errors="replace").splitlines()
            dispatch_contract.observe_terminal_review_failure(
                route, dict(node), str(verdict.get("attempt_id") or ""),
                verdict_path, output_rev["_sha256"], registry_lines)
            return ({"leg": node_id, "verdict": "FAIL", "round": verdict["round"],
                     "attempt_id": verdict["attempt_id"], "currency": "current",
                     "evidence_digest": output_rev["_sha256"], "history": [],
                     "limitations": _verdict_limitations(verdict)}, "FAIL")
        marker = snapshot.json(marker_path, 2 * 1024 * 1024)
        evidence = marker.get("evidence") if isinstance(marker, dict) else None
        evidence_path = Path(str(evidence.get("path") or "")) if isinstance(evidence, dict) else None
        if (marker.get("schema_version") != 2
                or marker.get("route_id") != route.get("route_id")
                or marker.get("route_hash") != route.get("route_hash")
                or marker.get("node_id") != node_id
                or marker.get("attempt_id") != verdict.get("attempt_id", marker.get("attempt_id"))
                or evidence_path is None
                or evidence_path.resolve(strict=False) != verdict_path.resolve(strict=False)
                or _digest_value(evidence.get("sha256")) != output_rev["_sha256"]):
            raise ProjectionProblem("completion-marker-evidence-conflict")
        currency = dispatch_contract.evidence_currency(
            route, dict(node), marker_path, marker, observe=True, jobs=jobs,
        )
        gate = dispatch_contract.gate_currency(
            route, dict(node), marker_path, marker, observe=True, jobs=jobs,
        )
        if currency.state != "current" or gate.state != "current":
            raise ProjectionProblem(getattr(currency if currency.state != "current" else gate,
                                            "reason", "completion-marker-not-current"))
        registry_lines = snapshot.read(jobs, MAX_REGISTRY_BYTES).decode(
            "utf-8", errors="replace").splitlines()
        readiness = dispatch_contract.completion_attempt_readiness(
            route, dict(node), marker, jobs, registry_lines=registry_lines, observe=True)
        if readiness.state != "ready":
            raise ProjectionProblem(getattr(readiness, "reason", "completion-attempt-not-current"))
    except ProjectionProblem as exc:
        return ({"leg": node_id, "verdict": "unresolved", "reason": exc.code,
                 "round": verdict["round"], "attempt_id": (marker.get("attempt_id") if "marker" in locals() else None)},
                None)
    except Exception:
        return ({"leg": node_id, "verdict": "unresolved", "reason": "completion-currency-unverified",
                 "round": verdict["round"]}, None)
    history = _verdict_history(node, marker, marker_path, snapshot, str(route.get("route_id")))
    return ({"leg": node_id, "verdict": verdict["verdict"], "round": verdict["round"],
             "attempt_id": marker.get("attempt_id"), "currency": gate.state,
             "evidence_digest": output_rev["_sha256"], "history": history,
             "limitations": _verdict_limitations(verdict)}, verdict["verdict"])


def _resolve_source(source: Path, root: Path, jobs: Path | None,
                    snapshot: ReadSnapshot) -> dict[str, Any]:
    report_observation = {"state": "unknown"}
    try:
        if root.is_symlink() or not root.is_dir():
            raise ProjectionProblem("artifact-root-unavailable")
        if source.is_symlink() or (source.exists() and not source.is_dir()):
            raise ProjectionProblem("report-source-kind-invalid")
        if not source.is_dir():
            raise ProjectionProblem("report-source-unavailable")
        # File presence is an observation, not an admitted revision or a verdict.
        report_files = []
        for name in ("REPORT.md", "index.html"):
            try:
                info = (source / name).lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ProjectionProblem("report-input-unreadable") from exc
            if not stat.S_ISREG(info.st_mode):
                raise ProjectionProblem("report-input-kind-invalid")
            if stat.S_ISREG(info.st_mode) and info.st_size > 0:
                report_files.append(name)
        report_observation = {"state": "present" if report_files else "absent"}
        if not report_files:
            raise ProjectionProblem("report-source-unavailable")
        root = root.resolve(strict=True)
        source = source.resolve(strict=True)
        candidates = artifact_reader.bucket_dirs(root, "report", include_shared=False,
                                                 include_legacy=False)
        matches = [(path, meta) for path, meta in candidates
                   if path.resolve(strict=True) == source]
        if len(matches) != 1:
            raise ProjectionProblem("report-source-lineage-unresolved")
        _bucket, meta = matches[0]
        campaign_id, cycle_id = meta.get("campaign_id"), meta.get("cycle_id")
        if not isinstance(campaign_id, str) or not isinstance(cycle_id, str):
            raise ProjectionProblem("report-cycle-identity-unresolved")
        admitted = artifact_lifecycle.read_admitted_cycle(root, campaign_id, cycle_id)
        if not isinstance(admitted, Mapping):
            raise ProjectionProblem("report-cycle-unadmitted")
        cycle_dir = artifact_locator.find_path_by_id(root, cycle_id)
        if cycle_dir is None or (cycle_dir / "artifacts" / "report").resolve(strict=True) != source:
            raise ProjectionProblem("report-cycle-path-mismatch")
        manifest_path = cycle_dir / "manifest.json"
        raw_manifest = snapshot.read(manifest_path, MAX_JSON_BYTES)
        document = json.loads(raw_manifest.decode("utf-8"))
        if not isinstance(document, dict) or not artifact_manifest.validate(document).ok:
            raise ProjectionProblem("cycle-manifest-invalid")
        cycle = document.get("cycle")
        identity = artifact_lifecycle.read_root_identity(root)
        if (not isinstance(cycle, dict) or cycle.get("cycle_id") != cycle_id
                or cycle.get("campaign_id") != campaign_id
                or identity is None or document.get("artifact_root_id") != identity.artifact_root_id):
            raise ProjectionProblem("report-cycle-identity-mismatch")
        output_paths = ("artifacts/report/REPORT.md", "artifacts/report/index.html",
                        "artifacts/report/report_manifest.json")
        report_revisions = {}
        producers = set()
        for locator in output_paths:
            row = _revision_for(document, cycle_dir, locator, snapshot)
            report_revisions[locator.removeprefix("artifacts/")] = row
            provenance = row.get("provenance")
            producer = provenance.get("producer_route_id") if isinstance(provenance, Mapping) else None
            if not isinstance(producer, str):
                raise ProjectionProblem("report-producer-lineage-missing")
            producers.add(producer)
        if len(producers) != 1:
            raise ProjectionProblem("report-producer-lineage-conflict")
        route_id = next(iter(producers))
        if not _ROUTE_ID.fullmatch(route_id):
            raise ProjectionProblem("route-identity-invalid")
        route_rows = [row for row in document.get("routes", [])
                      if isinstance(row, Mapping) and row.get("route_id") == route_id
                      and row.get("artifact_root_id") == identity.artifact_root_id]
        if len(route_rows) != 1:
            raise ProjectionProblem("manifest-route-binding-conflict")
        route_path = artifact_lifecycle.canonical_route_path(root, route_id)
        if route_path.stat().st_size > MAX_JSON_BYTES:
            raise ProjectionProblem("route-exceeds-read-bound")
        snapshot.read(route_path, MAX_JSON_BYTES)
        binding, route = artifact_lifecycle.bind_existing_runtime_route(
            root, route_path, expected_root_id=identity.artifact_root_id)
        route_hash = route.get("route_hash")
        if (route.get("route_id") != route_id or route.get("artifact_root") != str(root)
                or route_rows[0].get("route_hash") != route_hash
                or route_identity.route_hash(route) != route_hash):
            raise ProjectionProblem("route-hash-binding-mismatch")
        from route_lineage import RouteLineageError, verified_route_lineage
        try:
            lineage = verified_route_lineage(route, artifact_root=root, observe=True)
        except RouteLineageError as exc:
            raise ProjectionProblem(exc.code) from exc
        if not lineage:
            raise ProjectionProblem("route-lineage-unverified")
        report_node = _find_report_node(route, report_revisions)
        bundle_path = report_revisions["report/report_manifest.json"]["_path"]
        bundle = snapshot.json(bundle_path, MAX_REPORT_BYTES)
        if (not isinstance(bundle, dict) or bundle.get("schema_version") != 2
                or not isinstance(bundle.get("files"), list)
                or len(bundle["files"]) > 256):
            raise ProjectionProblem("report-bundle-manifest-invalid")
        bundle_files = {}
        for item in bundle["files"]:
            if not isinstance(item, dict):
                raise ProjectionProblem("report-bundle-inventory-invalid")
            rel = _safe_rel(item.get("path"))
            path = _safe_file(source, rel)
            raw = snapshot.read(path, MAX_REPORT_BYTES)
            digest = _digest(raw)
            if _digest_value(item.get("sha256")) != digest:
                raise ProjectionProblem("report-bundle-digest-mismatch")
            bundle_files[rel] = {"path": path, "sha256": digest}
        entrypoint = _safe_rel(bundle.get("entrypoint"))
        if entrypoint not in bundle_files or "REPORT.md" not in bundle_files:
            raise ProjectionProblem("report-entrypoint-unresolved")
        report_outputs = {Path(str(row["locator"]["path"])).as_posix().removeprefix("artifacts/")
                          for row in report_revisions.values()}
        input_revisions = {rel: row for rel, row in report_revisions.items()}
        for name, item in bundle_files.items():
            full = "report/" + name
            if full in input_revisions:
                if input_revisions[full]["_sha256"] != item["sha256"]:
                    raise ProjectionProblem("report-input-revision-conflict")
            else:
                locator = "artifacts/report/" + name
                row = _revision_for(document, cycle_dir, locator, snapshot)
                if row["_sha256"] != item["sha256"]:
                    raise ProjectionProblem("report-input-revision-conflict")
                input_revisions[full] = row
        peers = _report_peers(route, report_node)
        peer_results = []
        valid_verdicts = []
        input_failures = []
        for peer in peers:
            result, authority = _peer_result(root, route, peer, cycle_dir, document,
                                              input_revisions, jobs, snapshot)
            peer_results.append(result)
            if authority in {"PASS", "FAIL"}:
                valid_verdicts.append(authority)
            if result.get("reason", "").startswith("required-input"):
                input_failures.append(result["reason"])
        verdict = ("FAIL" if "FAIL" in valid_verdicts else
                   "PASS" if len(valid_verdicts) == len(peers) and valid_verdicts
                   and all(value == "PASS" for value in valid_verdicts) else "unresolved")
        reasons = [row["reason"] for row in peer_results if row.get("reason")]
        unreadable = any(row.get("reason") == "peer-output-unreadable" for row in peer_results)
        required_state = "failed" if input_failures or unreadable else (
            "confirmed" if all(row.get("verdict") in {"PASS", "FAIL"}
                               for row in peer_results) else "unresolved")
        uri = lambda name: (source / name).resolve(strict=True).as_uri()
        entrypoints = [
            {"label": "보고서 HTML", "path": entrypoint, "href": uri(entrypoint)},
            {"label": "보고서 Markdown", "path": "REPORT.md", "href": uri("REPORT.md")},
        ]
        subject = {
            "state": "bound", "artifact_root_id": identity.artifact_root_id,
            "campaign_id": campaign_id, "cycle_id": cycle_id,
            "manifest_revision_id": document.get("manifest_revision_id"),
            "manifest_digest": artifact_manifest.manifest_digest(document),
            "route_id": route_id, "route_hash": route_hash,
            "report_node": report_node.get("id"),
            "artifacts": [{"artifact_id": row.get("artifact_id"),
                           "artifact_revision_id": row.get("artifact_revision_id"),
                           "path": str(row["locator"]["path"]),
                           "sha256": row["_sha256"]}
                          for row in report_revisions.values()],
        }
        completion = _completion(root, cycle_dir, document, route, route_path, jobs, snapshot)
        payload = {
            "schema_version": 1,
            "subject": subject,
            "verification": {"verdict": verdict, "state": "current" if verdict != "unresolved" else "unresolved",
                              "reason": None if verdict != "unresolved" else
                              (reasons[0] if reasons else "required-peer-currentness-unverified"),
                              "peers": peer_results,
                              "history": [entry for peer in peer_results for entry in peer.get("history", [])]},
            "completion": completion,
            "required_input_observation": {"state": required_state,
                                           "reasons": reasons,
                                           "artifacts": [{"path": name, "sha256": row["_sha256"]}
                                                         for name, row in input_revisions.items()]},
            "integrity": {"state": "verified", "schema": bundle.get("schema_version")},
            "entrypoints": entrypoints,
        }
        if not snapshot.unchanged():
            raise ProjectionProblem("subject-changed-during-read")
        return _display(payload)
    except ProjectionProblem as exc:
        payload = _unresolved(exc.code)
        payload["report_observation"] = report_observation
        payload["integrity"] = {"state": "unresolved", "reason": exc.code}
        return _display(payload)
    except Exception:
        payload = _unresolved("projection-read-failed")
        return _display(payload)


def project_report(source: str | Path, *, artifact_root: str | Path | None = None,
                   jobs: str | Path | None = None) -> dict[str, Any]:
    source = Path(source).expanduser()
    root_value = artifact_root or os.environ.get("AGENT_ARTIFACT_ROOT")
    if not root_value:
        return _display(_unresolved("artifact-root-unavailable"))
    root = Path(root_value).expanduser()
    jobs_value = jobs or os.environ.get("AGENT_DISPATCH_JOBS")
    jobs_path = Path(jobs_value).expanduser() if jobs_value else None
    try:
        with artifact_reader.read_scope():
            return _resolve_source(source, root, jobs_path, ReadSnapshot())
    except Exception:
        return _display(_unresolved("projection-read-failed"))


def project_route(artifact_root: str | Path, route_id: str, route_hash: str | None = None,
                  *, jobs: str | Path | None = None) -> dict[str, Any]:
    """Resolve the report subject for one exact route through existing lineage APIs."""
    root = Path(artifact_root).expanduser()
    if not isinstance(route_id, str) or not _ROUTE_ID.fullmatch(route_id):
        return _display(_unresolved("route-identity-invalid"))
    try:
        route_path = artifact_lifecycle.canonical_route_path(root, route_id)
        route_stat = route_path.lstat()
        if not stat.S_ISREG(route_stat.st_mode) or route_stat.st_size > MAX_JSON_BYTES:
            raise ProjectionProblem("route-unreadable")
        snapshot = ReadSnapshot()
        route = json.loads(snapshot.read(route_path, MAX_JSON_BYTES).decode("utf-8"))
        if route_hash and route.get("route_hash") != route_hash:
            raise ProjectionProblem("route-hash-binding-mismatch")
        if route.get("route_id") != route_id or route_identity.route_hash(route) != route.get("route_hash"):
            raise ProjectionProblem("route-hash-binding-mismatch")
        with artifact_reader.read_scope():
            record = artifact_producer.route_cycle_for(root, route)
            if not isinstance(record, Mapping) or not isinstance(record.get("cycle_id"), str):
                raise ProjectionProblem("report-cycle-for-route-unresolved")
            cycle_dir = artifact_locator.find_path_by_id(root, record["cycle_id"])
            if cycle_dir is None:
                raise ProjectionProblem("report-cycle-for-route-unresolved")
            source = cycle_dir / "artifacts" / "report"
            payload = _resolve_source(source, root, Path(jobs).expanduser() if jobs else None,
                                      snapshot)
            subject = payload.get("subject", {})
            if (route_hash and subject.get("state") == "bound"
                    and subject.get("route_hash") != route_hash):
                return _display(_unresolved("route-hash-binding-mismatch"))
            return payload
    except ProjectionProblem as exc:
        return _display(_unresolved(exc.code))
    except Exception:
        return _display(_unresolved("projection-read-failed"))


def _human(payload: Mapping[str, Any]) -> str:
    display = payload.get("display") or {}
    verification = payload.get("verification") or {}
    completion = payload.get("completion") or {}
    required = payload.get("required_input_observation") or {}
    lines = [display.get("verification_label", "검증 미확정"),
             display.get("completion_label", "완료 미확정"),
             display.get("required_input_label", "필수 입력 미확정")]
    for peer in verification.get("peers", []):
        lines.append(f"검토 {peer.get('leg', 'unknown')}: {peer.get('verdict', 'unresolved')}")
    for reason in (required.get("reasons") or []):
        lines.append("확인 사항: " + str(reason))
    if completion.get("reason"):
        lines.append("운영 근거: " + str(completion["reason"]))
    for entry in display.get("entrypoints", []):
        lines.append(f"{entry.get('label', '보고서')}: {entry.get('href', '')}")
    lines.extend(display.get("limitations", []))
    return "\n".join(lines)


def _html(payload: Mapping[str, Any]) -> str:
    display = payload.get("display") or {}
    links = "".join(f'<li><a href="{html.escape(str(item.get("href", "")), quote=True)}">'
                     f'{html.escape(str(item.get("label", "보고서")))}</a></li>'
                     for item in display.get("entrypoints", []))
    labels = "".join(f'<li>{html.escape(str(display.get(key, "")))}</li>'
                     for key in ("verification_label", "completion_label", "required_input_label"))
    limits = "".join(f'<li>{html.escape(str(item))}</li>' for item in display.get("limitations", []))
    reason = payload.get("verification", {}).get("reason") or ""
    reason_html = f'<p>{html.escape(str(reason))}</p>' if reason else ""
    return ("<!doctype html>\n<html lang=\"ko\"><meta charset=\"utf-8\">\n"
            "<title>보고서 상태</title><main><h1>보고서 상태</h1><ul>" + labels
            + "</ul>" + reason_html + "<h2>원문</h2><ul>" + links
            + "</ul><h2>한계</h2><ul>" + limits + "</ul></main></html>\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "read"):
        item = sub.add_parser(name)
        item.add_argument("--source", required=True)
        item.add_argument("--artifact-root")
        item.add_argument("--jobs", default=os.environ.get("AGENT_DISPATCH_JOBS"))
        item.add_argument("--json", action="store_true")
        if name == "read":
            item.add_argument("--format", choices=("text", "html"), default="text")
    args = parser.parse_args(argv)
    payload = project_report(args.source, artifact_root=args.artifact_root, jobs=args.jobs)
    if args.json:
        print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    elif args.command == "read" and args.format == "html":
        sys.stdout.write(_html(payload))
    else:
        print(_human(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
