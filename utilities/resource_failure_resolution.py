"""Resolve workflow consumption of an unchanged failed run, never its exit."""
from __future__ import annotations

import hashlib
from collections import deque
import json
import os
from pathlib import Path
import re
import shlex

import dispatch_resource_wait as OWNER_RESOURCE
import resource_run_registry as RR
import resource_resume
import workflow_state as WS

SHA = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")


class UnresolvedResource(WS.WorkflowStateError):
    code = "resource-failure-unresolved"

    def __init__(self, node, reason, next_step):
        super().__init__(f"{node}:{reason}")
        self.next_step = next_step


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        before = stream_stat(stream)
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
        if before != stream_stat(stream):
            raise ValueError("artifact-changed-during-read")
    if before != file_stat(path):
        raise ValueError("artifact-replaced-during-read")
    return value.hexdigest()


def stream_stat(stream):
    row = os.fstat(stream.fileno())
    return row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns


def file_stat(path):
    row = Path(path).stat()
    return row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns


class Snapshot:
    """Parse only the bytes whose hash was checked; reject changes before append."""
    def __init__(self):
        self.files = {}
        self.digests = {}

    def read(self, path, expected=None):
        path = Path(path)
        before = file_stat(path)
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if before != file_stat(path) or (expected is not None and sha != expected):
            raise ValueError("bound-artifact-changed:" + str(path))
        old = self.files.setdefault(path, before)
        if old != before:
            raise ValueError("artifact-changed-during-resolution:" + str(path))
        self.digests[path] = sha
        return raw, sha

    def document(self, path, expected=None):
        raw, sha = self.read(path, expected)
        if Path(path).suffix == ".jsonl":
            value = [json.loads(line) for line in raw.splitlines() if line.strip()]
        else:
            value = json.loads(raw)
        return value, sha

    def check_digest(self, path, expected):
        path = Path(path)
        before = file_stat(path)
        if digest(path) != expected or file_stat(path) != before:
            raise ValueError("bound-artifact-changed:" + str(path))
        if self.files.setdefault(path, before) != before:
            raise ValueError("artifact-changed-during-resolution:" + str(path))
        self.digests[path] = expected

    def verify(self):
        if any(file_stat(path) != identity for path, identity in self.files.items()):
            raise ValueError("artifact-changed-before-resolution")


def saved_output_admission(route, successor, jobs, owner_attempt_id, owner_session_id):
    """Prove exact failed-run inputs for the first declared verifier start.

    This is input readiness only. It creates no marker, PASS, stage transition,
    or resolution record; the ordinary successor gate rechecks the returned
    snapshot before it skips only this missing resource marker.
    """
    from dispatch_owner_input import _locked
    import resource_run_evidence as RUN_EVIDENCE

    if not owner_attempt_id or not owner_session_id or not jobs:
        return None
    jobs_path = Path(jobs).expanduser().resolve()
    node = WS.route_node(route, successor)
    if (node is None or node.get("kind") not in {"pipeline-stage", "review-worker"}
            or len(node.get("depends_on") or []) < 1):
        return None
    sup = OWNER_RESOURCE.supervisor()
    ledger = sup.ledger_for(route, str(jobs_path))
    armed_rows = sup.read_armed(ledger)
    candidates = []
    for predecessor in node.get("depends_on") or []:
        armed = armed_rows.get(predecessor)
        predecessor_node = WS.route_node(route, predecessor)
        if (not armed or not predecessor_node
                or predecessor_node.get("kind") != "resource-runner"
                or (predecessor_node.get("continuation") or {}).get("kind") != "supervised"
                or armed.get("predecessor_kind") != "resource"
                or successor not in (armed.get("successors") or [])):
            continue
        try:
            registry = json.loads(Path(armed["resource_registry"]).read_text(encoding="utf-8"))
            row = registry["runs"][armed["predecessor_id"]]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if row.get("resource_policy") == "supervised-owner":
            candidates.append((predecessor, armed, row))
    if len(candidates) != 1:
        return None

    predecessor, armed, row = candidates[0]
    stage = ledger.state().get("nodes", {}).get(predecessor, {})
    failure = stage.get("evidence") or {}
    if (stage.get("state") != "FAILED_RETRYABLE"
            or not isinstance(row, dict) or row.get("status") != "failed"
            or type(row.get("exit_code")) is not int or row["exit_code"] == 0
            or row.get("cancel_requested") or row.get("parent_close_requested")
            or row.get("parent_attempt_id") != (row.get("owner_wait") or {}).get("parent_attempt_id")
            or armed.get("route_id") != route.get("route_id")
            or armed.get("route_hash") != route.get("route_hash")
            or armed.get("jobs") != str(jobs_path)
            or row.get("route") != armed.get("route_file")
            or row.get("node") != predecessor or row.get("jobs") != str(jobs_path)
            or armed.get("resource_binding") != OWNER_RESOURCE.resource_body_digest(row)
            or failure.get("exit_code") != row.get("exit_code")
            or failure.get("resource_sha256") != resource_resume.row_digest(row)):
        return None
    evidence = sup.resource_evidence(armed)
    checked = sup._checked_resource_row(armed, evidence, require_success=False)
    if (checked != row or evidence.get("terminal") is not True
            or evidence.get("liveness") != "exited" or evidence.get("quiescent") is not True
            or evidence.get("exit_code") != row.get("exit_code")
            or evidence.get("resource_sha256") != resource_resume.row_digest(row)):
        return None

    owner = row.get("owner_wait") or {}
    input_module = __import__("dispatch_owner_input")
    with _locked(jobs_path, owner_attempt_id) as (_, value):
        if not sup._owner_input_matches_locked(
                route, armed, row, owner_attempt_id, owner_session_id, value, input_module):
            return None

    snapshot = Snapshot()
    armed_path = ledger.root / "armed" / f"{predecessor}.json"
    try:
        observed_armed, armed_sha = snapshot.document(armed_path)
        registry, registry_sha = snapshot.document(armed["resource_registry"])
        current_row = registry["runs"][armed["predecessor_id"]]
        sentinel_path = Path(current_row["sentinel"])
        sentinel_raw, sentinel_sha = snapshot.read(sentinel_path)
        if current_row != row or sentinel_raw.decode("utf-8", errors="strict").strip() != str(row["exit_code"]):
            return None
        if observed_armed != armed:
            return None
        if armed.get("resource_binding") != OWNER_RESOURCE.resource_body_digest(current_row):
            return None
        runtime_output = Path(RUN_EVIDENCE.paths(armed)["runtime_output"]).resolve(strict=True)
        artifact_base = Path(armed["artifact_base"]).resolve(strict=True)
        if not runtime_output.is_relative_to(artifact_base):
            return None
        receipt, receipt_sha = snapshot.document(runtime_output)
        config_sha = row.get("config_sha256")
        if (receipt.get("route_id") != route.get("route_id")
                or receipt.get("exit") != row.get("exit_code")
                or not isinstance(config_sha, str) or not config_sha
                or receipt.get("config_sha256") != config_sha
                or not isinstance(receipt.get("outputs"), list) or not receipt["outputs"]):
            return None
        launch_request = row.get("launch_request") or {}
        config_ref = row.get("config_ref") or launch_request.get("config_manifest")
        if config_ref:
            config_path = Path(config_ref).expanduser()
            if not config_path.is_absolute():
                config_path = artifact_base / config_path
            config_path = config_path.resolve(strict=True)
            config_digest = (launch_request.get("config_manifest_sha256")
                             or row.get("config_sha256"))
            match = SHA.fullmatch(config_digest) if isinstance(config_digest, str) else None
            if match is None:
                return None
            snapshot.read(config_path, match.group(1))
        outputs = []
        for item in receipt["outputs"]:
            if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                    or not isinstance(item.get("sha256"), str)
                    or not SHA.fullmatch(item["sha256"])):
                return None
            path = contained(artifact_base, item["path"])
            _raw, output_sha = snapshot.read(path, SHA.fullmatch(item["sha256"]).group(1))
            outputs.append({"path": str(path), "sha256": output_sha})
        snapshot.verify()
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, RuntimeError):
        return None
    return {
        "schema": "saved-resource-output-admission-v1",
        "route_id": route.get("route_id"), "route_hash": route.get("route_hash"),
        "resource_node": predecessor, "successor": successor,
        "jobs": str(jobs_path), "owner_attempt_id": owner_attempt_id,
        "owner_session_id": owner_session_id,
        "source_attempt_id": row.get("parent_attempt_id"),
        "run_id": row.get("run_id"), "exit_code": row.get("exit_code"),
        "identity": evidence.get("identity"),
        "resource_sha256": resource_resume.row_digest(row),
        "resource_binding": armed.get("resource_binding"),
        "armed_sha256": armed_sha, "registry_sha256": registry_sha,
        "sentinel": {"path": str(sentinel_path.resolve()), "sha256": sentinel_sha},
        "receipt": {"path": str(runtime_output), "sha256": receipt_sha},
        "outputs": sorted(outputs, key=lambda item: item["path"]),
        "checked_artifacts": [{"path": str(path), "sha256": sha}
                              for path, sha in sorted(snapshot.digests.items(), key=lambda pair: str(pair[0]))],
    }


def hashes(value):
    """Read structured SHA fields, excluding prose and unrelated identifiers."""
    result = set()
    if isinstance(value, list):
        for item in value:
            result.update(hashes(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and (key.endswith("sha256") or
                    (key in {"expected", "actual"} and value.get("expected") == value.get("actual"))):
                match = SHA.fullmatch(item)
                if match:
                    result.add(match[1])
            if isinstance(item, dict) and ("hash" in key or key == "artifact_sha256"):
                result.update(match[1] for text in item.values() if isinstance(text, str)
                              and (match := SHA.fullmatch(text)))
            result.update(hashes(item))
    return result


def references(value):
    """Existing path/hash manifests can carry an independent admission transitively."""
    result = []
    if isinstance(value, list):
        for item in value:
            result.extend(references(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, dict) and ("hash" in key or key == "artifact_sha256"):
                result.extend((name, SHA.fullmatch(sha)[1]) for name, sha in item.items()
                              if isinstance(sha, str) and SHA.fullmatch(sha))
            result.extend(references(item))
        path = value.get("path", value.get("evidence_path", value.get("source")))
        sha = value.get("sha256", value.get("input_sha256", value.get("source_sha256")))
        if sha is None and value.get("expected") == value.get("actual"):
            sha = value.get("actual")
        if isinstance(path, str) and isinstance(sha, str) and SHA.fullmatch(sha):
            result.append((path.split("#", 1)[0], SHA.fullmatch(sha)[1]))
    return result


def admitted_hashes(base, document, snapshot, targets=()):
    pending = deque([document])
    seen = set()
    bindings = set()
    while pending:
        value = pending.popleft()
        bindings.update(hashes(value))
        if set(targets) & bindings:
            return bindings
        for name, sha in references(value):
            path = Path(name)
            path = (path if path.is_absolute() else base / path).resolve()
            if not path.is_relative_to(base) or path.suffix not in {".json", ".jsonl"}:
                continue
            if (path, sha) in seen:
                continue
            seen.add((path, sha))
            child, _ = snapshot.document(path, sha)
            pending.append(child)
    return bindings


def contained(base, name):
    path = Path(name)
    path = path if path.is_absolute() else base / path
    path = path.resolve(strict=True)
    if not path.is_relative_to(base):
        raise ValueError("artifact-outside-resource-cycle")
    return path


def descendants(route, predecessor):
    result = []
    reached = {predecessor}
    pending = list(route["nodes"])
    while pending:
        next_pending = []
        for node in pending:
            if node["id"] in reached:
                continue
            if any(dep in reached for dep in node.get("depends_on", [])):
                reached.add(node["id"])
                result.append(node)
            else:
                next_pending.append(node)
        if len(next_pending) == len(pending):
            break
        pending = next_pending
    return result


def _retry_registry(armed):
    """Return the current resource runs and failed row when readable."""
    try:
        value = json.loads(Path(armed.get("resource_registry") or "").read_text())
        runs = value.get("runs") if isinstance(value, dict) else None
        if not isinstance(runs, dict):
            return {}, {}
        row = runs.get(armed.get("predecessor_id"))
        return runs, row if isinstance(row, dict) else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}, {}


def _route_context(route, armed, failed_row):
    """Resolve the exact route record and retain a best-effort resume handle."""
    fallback = None
    for source, key in ((armed, "route_file"), (failed_row, "route")):
        candidate = source.get(key)
        if candidate is None or candidate == "":
            continue
        if not isinstance(candidate, str):
            return fallback, route, False
        path = Path(candidate)
        if fallback is None:
            fallback = path
        try:
            observed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return path, route, False
        if (not isinstance(observed, dict)
                or observed.get("route_id") != route.get("route_id")
                or observed.get("route_hash") != route.get("route_hash")):
            return path, route, False
        return path, observed, True

    try:
        import artifact_lifecycle
        canonical = artifact_lifecycle.canonical_route_path(
            Path(route["artifact_root"]), route["route_id"])
    except (ImportError, KeyError, TypeError, ValueError, OSError):
        return fallback, route, False
    if fallback is None:
        fallback = canonical
    try:
        observed = json.loads(canonical.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return canonical, route, False
    if (isinstance(observed, dict)
            and observed.get("route_id") == route.get("route_id")
            and observed.get("route_hash") == route.get("route_hash")):
        return canonical, observed, True
    return canonical, route, False


def _owner_continuation(route, route_path, failed_row, jobs):
    """Use the receipt's effective owner and shared continuation judgment."""
    parent = failed_row.get("parent_attempt_id")
    if not isinstance(parent, str) or not parent or route_path is None:
        return None
    try:
        import dispatch_replacement as DR
        import route_authority
        import owner_route_binding as ORB
        import route_lineage
        from dispatch_contract import parse_registry_metadata
        from dispatch_registry_cache import registry_snapshot

        jobs_path = Path(jobs).expanduser()
        snapshot = registry_snapshot(jobs_path)
        if not snapshot.complete:
            return None
        rows = []
        for line in snapshot.lines:
            fields = line.split("\t")
            if len(fields) != 6:
                return None
            rows.append((fields, parse_registry_metadata(fields[5])))
        effective, _lineage = DR.effective_attempts(jobs_path, {parent})
        if len(effective) != 1:
            return None
        owner_id = next(iter(effective))
        if not snapshot.matches_jobs(jobs_path):
            return None
        owner_rows = [(fields, metadata) for fields, metadata in rows
                      if metadata.get("attempt_id") == owner_id]
        if len(owner_rows) != 1:
            return None
        fields, metadata = owner_rows[0]
        if (metadata.get("worker_type") != "owner"
                or metadata.get("attempt_id") != owner_id
                or metadata.get("dispatch_depth") != "1"
                or len(fields) < 2):
            return None

        binding, binding_status = ORB.resolve_owner_route_lifecycle(
            jobs_path, owner_attempt_id=owner_id, registry_rows=rows)
        if (binding is None or binding_status not in {
                "owner-route-launch-binding", "owner-route-post-launch-attachment",
                "owner-route-advance-current"}):
            return None
        active_raw = json.loads(Path(binding.route_file).read_text(encoding="utf-8"))
        active_verified = ORB.ROUTE.verify_route(active_raw)
        if (active_verified.get("route_id"), active_verified.get("route_hash")) != (
                binding.route_id, binding.route_hash):
            return None
        # The lifecycle resolver verified this exact binding and every adopted
        # generation; recheck the reread bytes and their route ancestry before
        # using them for guidance.
        ancestry = route_lineage.verified_route_lineage(active_raw, observe=True)
        if not any((item.get("route_id"), item.get("route_hash")) ==
                   (route.get("route_id"), route.get("route_hash")) for item in ancestry):
            return None
        if not snapshot.matches_jobs(jobs_path):
            return None
        answered = bool(DR._retained_corrections(jobs_path, owner_id))
        decision = route_authority.decide_continuation(
            active_raw, action="owner", jobs=jobs_path,
            status=fields[1], metadata=metadata, facts={"answered": answered})
        if not snapshot.matches_jobs(jobs_path):
            return None
        return decision, owner_id, metadata, active_raw, Path(binding.route_file)
    except Exception:
        # Guidance generation must preserve the typed resource failure even when
        # registry, lineage, retained-answer, or process evidence is unavailable.
        return None


def _retry_message(route, run_id, retry, nodes, reason):
    revalidate = ", ".join(nodes[1:]) or "후속 단계 없음"
    return (f"실패한 resource {run_id}의 사유는 {reason}입니다. 실패 기록과 기존 산출물은 그대로 보존합니다."
            f" 같은 검증 구성과 smoke inputs를 사용해 route {route['route_id']}에서 새 resource attempt {retry}를"
            " 별도 output directory에 실행하고,"
            f" 새 출력 bytes를 기준으로 후속 단계({revalidate})를 다시 검증합니다."
            " 과거 PASS 표시는 새 bytes의 검증 근거가 아닙니다.")


def retry_step(route, armed, jobs, reason):
    """Select correction, resume, or compose from route and owner receipt state.

    Ordinary exact closed routes compose; cancellation remains protected even
    after closure. A live or unanswered BLOCKED/FAIL owner gets its existing
    correction command. Other open or unobservable states,
    including retained answers, gates, cancellation, and waits, resume the
    route. Only a proven PASS owner's rejected settlement keeps the agreed
    compose exception until same-route settlement recovery is implemented.
    """
    nodes = [armed["node"], *[node["id"] for node in descendants(route, armed["node"])]]
    run_id = armed["predecessor_id"]
    suffix = re.search(r"__a([1-9][0-9]*)$", run_id)
    base = run_id[:suffix.start()] if suffix else run_id
    runs, failed_row = _retry_registry(armed)
    # Even a missing registry cannot reuse the attempt that just failed.
    ordinal = int(suffix.group(1)) + 1 if suffix else 1
    while f"{base}__a{ordinal}" in runs:
        ordinal += 1
    retry = f"{base}__a{ordinal}"
    compute_hint = ""
    try:
        import route_parent_close
        route_file = armed.get("route_file") or route.get("_route_file")
        attempts = {value for value in (
            armed.get("parent_attempt_id"),
            (armed.get("owner_wait") or {}).get("parent_attempt_id"),
            (failed_row.get("owner_wait") or {}).get("parent_attempt_id"),
            failed_row.get("parent_attempt_id"),
        ) if isinstance(value, str) and value}
        if route_file and attempts:
            candidates = route_parent_close.linked_resources(
                route, Path(route_file), jobs, attempts)
            ids = sorted({row["run_id"] for row in candidates
                          if row.get("kind") == "compute" and row.get("state") == "finished"
                          and isinstance(row.get("run_id"), str)})
            if ids:
                compute_hint = (
                    "\nCompleted compute-hosts run candidates (unverified; choose explicitly): "
                    + ", ".join(f"{run_id}: resource-runner start --compute-run {shlex.quote(run_id)}"
                                 for run_id in ids)
                    + "."
                )
    except Exception:
        # An advisory discovery failure never hides the original recovery.
        compute_hint = ""
    route_path, observed_route, route_valid = _route_context(route, armed, failed_row)

    resume_route_path = route_path

    def resume(continuation="resume"):
        try:
            from parent_next_directive import resume_command
            command = resume_command(resume_route_path, jobs, agent_home=Path(__file__).resolve().parents[1])
        except Exception:
            command = ""
        return {"command": command, "run_id": retry, "revalidate_nodes": nodes[1:],
                "meaning": "route 상태를 닫힘 또는 답변 가능한 owner로 확인하지 못했습니다. 기존 resume_command를 따릅니다.",
                "continuation": continuation}

    owner = None
    guidance_route = observed_route
    guidance_path = route_path
    if route_valid and route_path is not None:
        owner = _owner_continuation(observed_route, route_path, failed_row, jobs)
        if owner is not None:
            _decision, _owner_id, _metadata, active_route, active_path = owner
            if any(node.get("id") == armed["node"]
                   for node in active_route.get("nodes", [])):
                guidance_route, guidance_path = active_route, active_path
                resume_route_path = active_path
                nodes = [armed["node"], *[node["id"] for node in
                                           descendants(active_route, armed["node"])]]
            else:
                owner = None

    outcome = None
    if route_valid and guidance_path is not None:
        try:
            import dispatch_notice_state
            outcome = dispatch_notice_state.closed_outcome(guidance_path, guidance_route)
            if outcome:
                import route_authority
                protection = route_authority.decide_continuation(
                    guidance_route, action="owner", facts={"cancelled":
                        outcome.get("disposition") == "cancelled"
                        or outcome.get("state") == "cancelled"})
                if protection.state == "protected":
                    return resume("resume-protected")
        except Exception:
            return resume("resume-unobservable")
    if outcome and not outcome.get("finish_pending"):
        continuation = "closed-compose"
    elif not route_valid or route_path is None:
        return resume("resume-unobservable")
    else:
        if owner is None:
            return resume()
        decision, owner_id, metadata, _active_route, _active_path = owner
        if decision.state in {"answer", "running"}:
            try:
                from parent_next_directive import correction_command
                command = (correction_command(owner_id, jobs,
                    agent_home=Path(__file__).resolve().parents[1]) + " --message-file /dev/stdin")
            except Exception:
                return resume("resume-unobservable")
            message = _retry_message(guidance_route, run_id, retry, nodes, reason) + compute_hint
            return {"command": shlex.join(["printf", "%s", message]) + " | " + command,
                    "run_id": retry, "revalidate_nodes": nodes[1:],
                    "meaning": ("BLOCKED owner이면 먼저 owner_report를 확인하고, 답으로 맞는 경우 이 재시도 안내를 전달합니다. "
                                "같은 route의 새 attempt만 지시하며 과거 PASS를 새 bytes의 근거로 삼지 않습니다."),
                    "continuation": "correct"}
        if decision.state == "settle" and metadata.get("worker_type") == "owner":
            try:
                from dispatch_attempt_policy import verdict_pass
                pass_settlement = verdict_pass(metadata)
            except Exception:
                pass_settlement = False
            if pass_settlement:
                continuation = "pass-settlement-compose"
            else:
                return resume()
        else:
            return resume()

    compose_route = guidance_route if owner is not None else route
    task = (f"Continue the approved task from {compose_route['route_id']}. Resource {run_id} remains failed: {reason}. "
            f"Retain its registry, sentinel, outputs and all historical PASS/FAIL records unchanged. "
            f"Reuse its checked configuration and smoke inputs, run a distinct resource attempt {retry} "
            "in this new cycle's output directory, and revalidate every selected downstream stage against "
            "the new output bytes. Historical PASS markers do not complete this leg. Original task:\n"
            + str((compose_route.get("work_request") or {}).get("text") or compose_route.get("slug", ""))
            + compute_hint)
    argv = ["hearting", "run", "capability-route", "compose", "--start", "--shape", "staged",
            "--capability", compose_route["capability"], "--capability-mode", compose_route["capability_mode"],
            "--intensity", compose_route["effective_intensity"], "--graph", ",".join(nodes),
            # A pipe can be read only once. Supplying the existing slug option
            # keeps compose's slug inference from consuming the task first.
            "--slug", f"{compose_route['route_id']}-{armed['node']}-retry-a{ordinal}",
            "--cwd", compose_route["cwd"], "--artifact-root", compose_route["artifact_root"],
            "--jobs", str(jobs), "--prompt-file", "/dev/stdin"]
    if compose_route.get("campaign_key"):
        argv += ["--campaign-key", compose_route["campaign_key"]]
    else:
        argv += ["--unassigned"]
    return {"command": shlex.join(["printf", "%s", task]) + " | " + shlex.join(argv),
            "run_id": retry, "revalidate_nodes": nodes[1:],
            "meaning": "원본 실패를 보존하고 새 실행의 산출물로 후속 단계를 다시 검증합니다.",
            "continuation": continuation}


def prove(route, ledger, armed, failure, gates, jobs, module, *, snapshots=None,
          compute_observations=None, armed_path=None):
    snapshot = Snapshot()
    if armed_path is None:
        armed_path = ledger.root / "armed" / f"{armed['node']}.json"
    if Path(armed_path).is_file():
        observed_armed, _ = snapshot.document(armed_path)
        if observed_armed != armed:
            raise ValueError("resource-watch-changed")
    else:
        # Historical bindings are already pinned in the append-only journal;
        # current additive watches use their own exact path, never node lookup.
        observed_armed = armed
    node_id = armed["node"]
    node = WS.route_node(route, node_id)
    if (node is None or node.get("kind") != "resource-runner"
            or armed.get("route_id") != route["route_id"]
            or armed.get("route_hash") != route["route_hash"]
            or armed.get("jobs") != str(Path(jobs).resolve())):
        raise ValueError("resource-watch-binding-mismatch")
    registry, _ = snapshot.document(armed["resource_registry"])
    row = registry["runs"][armed["predecessor_id"]]
    sentinel, _ = snapshot.read(row["sentinel"])
    identity = f"{row['run_id']}:{row.get('pid')}:{row.get('starttime')}:{row.get('exit_code')}"
    execution = row.get("execution") if isinstance(row, dict) else None
    if isinstance(execution, dict) and execution.get("kind") == "compute-hosts":
        # The caller captures this before writer locks; require exact armed and
        # row identity here, then the snapshot digest is checked before append.
        key = OWNER_RESOURCE.supervisor().resource_watch_key(armed)
        captured = (compute_observations or {}).get(key)
        if not captured or captured.get("armed") != armed or captured.get("row") != row:
            raise ValueError("compute-resource-observation-changed")
        observation = captured.get("observation") or {}
        identity_finished = observation.get("state") == "quiescent"
    else:
        identity_finished = RR.classify_identity(row)[0] == "exited"
    if (row.get("route") != armed.get("route_file") or row.get("node") != node_id
            or row.get("jobs") != armed.get("jobs") or row.get("status") != "failed"
            or type(row.get("exit_code")) is not int or row["exit_code"] == 0
            or row.get("cancel_requested") or row.get("parent_close_requested")
            or not identity_finished
            or sentinel.decode().strip() != str(row["exit_code"])
            or failure.get("identity") != identity
            or failure.get("resource_sha256") != resource_resume.row_digest(row)
            or failure.get("exit_code") != row["exit_code"]
            or armed.get("resource_binding") != OWNER_RESOURCE.resource_body_digest(row)):
        raise ValueError("failed-execution-changed")
    chain = descendants(route, node_id)
    if not any(n.get("terminal") and gates.get(n["id"], {}).get("passed") is True for n in chain):
        raise ValueError("no-passed-downstream-terminal")
    directory = module.completion_dir(route["route_id"], jobs=jobs)
    markers = {}
    proofs = {}
    independent = []
    for part in [node, *chain]:
        key = part["id"]
        raw, marker_sha = snapshot.read(directory / f"{key}.json")
        marker = json.loads(raw)
        proof = module._marker_identity_row(route, part, key,
            part.get("terminal_gate") if part.get("terminal") else part.get("completion_gate"), jobs=jobs,
            exact_terminal=part.get("kind") != "resource-runner")
        if proof.get("passed") is not True:
            raise ValueError(f"downstream-marker-unproven:{key}:{proof.get('reason')}")
        markers[key] = marker
        proofs[key] = {"marker_sha256": marker_sha,
                       "evidence": marker["evidence"]}
        if key != node_id and marker.get("review_independence") == "independent":
            independent.append(key)
    if not independent:
        raise ValueError("independent-pass-absent")
    base = Path(armed["artifact_base"]).resolve(strict=True)
    documents = {}
    for key, marker in markers.items():
        evidence = marker["evidence"]
        if Path(evidence['path']).suffix in {'.json', '.jsonl'}:
            documents[key], _ = snapshot.document(evidence['path'], evidence['sha256'])
        else:
            snapshot.read(evidence['path'], evidence['sha256'])
            documents[key] = {}
    judgment = documents[node_id]
    if (judgment.get("gate_evidence_verdict", judgment.get("verdict")) != "PASS"
            or judgment.get("route_id") != route["route_id"]
            or judgment.get("node", judgment.get("node_id")) != node_id):
        raise ValueError("resource-judgment-not-pass")
    import resource_run_evidence as RUN_EVIDENCE
    receipt_path = Path(RUN_EVIDENCE.paths(armed)["runtime_output"]).resolve(strict=True)
    if not receipt_path.is_relative_to(base):
        raise ValueError("failed-output-receipt-outside-scope")
    receipt, receipt_sha = snapshot.document(receipt_path)
    if (receipt.get("route_id") != route["route_id"] or receipt.get("exit") != row["exit_code"]
            or receipt.get("config_sha256") != row.get("config_sha256")
            or not receipt.get("outputs")):
        raise ValueError("failed-output-receipt-unbound")
    produced = {contained(base, item["path"]): item["sha256"] for item in receipt["outputs"]}
    consumed = {}
    admitted = set()
    consumers = {markers[p['id']]['evidence']['sha256'] for p in chain
                 if node_id in p.get('depends_on', [])}
    for key in independent:
        if documents[key].get("verdict") != "PASS":
            raise ValueError("independent-verdict-not-pass")
        admitted.update(admitted_hashes(base, documents[key], snapshot, consumers))
    for part in chain:
        if node_id not in part.get("depends_on", []):
            continue
        evidence_path = contained(base, markers[part["id"]]["evidence"]["path"])
        if markers[part["id"]]["evidence"]["sha256"] not in admitted:
            continue
        document = documents[part["id"]]
        bindings = hashes(document)
        # A marker-bound summary may bind a declared JSON/JSONL sidecar.
        for name in part.get("outputs", []):
            if any(c in name for c in "*?[") or Path(name).suffix not in {".json", ".jsonl"}:
                continue
            path = contained(base, name)
            if path == evidence_path:
                continue
            sidecar_sha = digest(path)
            if sidecar_sha in bindings:
                sidecar, _ = snapshot.document(path, sidecar_sha)
                bindings.update(hashes(sidecar))
        if receipt_sha not in bindings:
            continue
        consumed.update({path: expected for path, expected in produced.items() if expected in bindings})
    if not consumed:
        raise ValueError("same-byte-consumer-binding-absent")
    # The accepted correction also names preserved artifacts. Do not substitute
    # corrected bytes for any originally produced bytes, even when PASS is kept.
    for name, expected in judgment.get("artifact_sha256", {}).items():
        path = contained(base, name)
        if path in produced and produced[path] != expected:
            raise ValueError("correction-output-binding-mismatch")
        snapshot.check_digest(path, expected)
    for path, expected in consumed.items():
        snapshot.check_digest(path, expected)
    snapshot.verify()
    if snapshots is not None:
        snapshots.append(snapshot)
    return {"basis": "downstream-independent-verification", "identity": identity,
            "resource_sha256": failure["resource_sha256"], "original_failure": failure,
            "run": {"path": str(receipt_path), "sha256": receipt_sha},
            "consumed_artifacts": [{"path": str(path), "sha256": sha} for path, sha in sorted(consumed.items())],
            "checked_artifacts": [{"path": str(path), "sha256": sha}
                                  for path, sha in sorted(snapshot.digests.items())],
            "stage_chain": proofs, "independent_nodes": independent}


def reconcile(route, ledger, gates, jobs, module, *, compute_observations=None):
    """Validate all candidates first; append only legal, restartable transitions."""
    current = ledger.state()
    if current["workflow_state"] not in {"CREATED", "READY", "RUNNING", "FAILED_RETRYABLE"}:
        return []
    if not gates or any(row.get("passed") is not True for row in gates.values()):
        return []
    planned = {}
    retries = {}
    snapshots = []
    supervisor = OWNER_RESOURCE.supervisor()
    for node_id, stage in current["nodes"].items():
        prior = (stage.get("evidence") or {}).get("resolved_resource_failure")
        if stage["state"] != "FAILED_RETRYABLE" and not prior:
            continue
        failure = prior["original_failure"] if prior else stage.get("evidence") or {}
        if failure.get("exit_code") in {None, 0}:
            continue  # Existing successful-exit/missing-output recovery owns this.
        node = WS.route_node(route, node_id)
        if not node or node.get('kind') != 'resource-runner':
            continue
        retry_armed = {'node': node_id, 'predecessor_id':
                 str(failure.get('identity') or f"{route['route_id']}__{node_id}").split(':')[0]}
        try:
            run_id = retry_armed["predecessor_id"]
            pairs = supervisor.resource_predecessors(ledger, node_id)
            pair = next(((armed, row) for armed, row in pairs
                         if row.get("run_id") == run_id
                         and resource_resume.row_digest(row) == failure.get("resource_sha256")), None)
            if pair is None:
                raise ValueError('resource-watch-unreadable')
            armed, failed_row = pair
            primary = supervisor.read_armed(ledger).get(node_id)
            armed_path = (ledger.root / "armed" / f"{node_id}.json" if armed == primary
                          else supervisor.resource_watch_path(ledger, armed))
            if not Path(armed_path).is_file():
                armed_path = None
            for key in ('resource_registry', 'route_file'):
                if isinstance(armed.get(key), str):
                    retry_armed[key] = armed[key]
            if armed.get("predecessor_kind") != "resource":
                raise ValueError('resource-watch-binding-mismatch')
            observation_key = supervisor.resource_watch_key(armed)
            if compute_observations and observation_key in compute_observations:
                planned[node_id] = prove(route, ledger, armed, failure, gates, jobs, module,
                                         snapshots=snapshots,
                                         compute_observations=compute_observations,
                                         armed_path=armed_path)
            else:
                planned[node_id] = prove(route, ledger, armed, failure, gates, jobs, module,
                                         snapshots=snapshots, armed_path=armed_path)
            retries[node_id] = retry_armed
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise UnresolvedResource(node_id, str(exc), retry_step(route, retry_armed, jobs, str(exc))) from exc
    if any(stage["state"] in WS.vocabulary(ledger.registry_path)["failure_states"]
           and node not in planned for node, stage in current["nodes"].items()):
        return []
    for node, snapshot in zip(planned, snapshots):
        try:
            snapshot.verify()
        except (OSError, ValueError) as exc:
            raise UnresolvedResource(node, str(exc), retry_step(route, retries[node], jobs, str(exc))) from exc
    for node, proof in planned.items():
        evidence = {"resolved_resource_failure": proof}
        state = ledger.state()["nodes"][node]["state"]
        if state == "FAILED_RETRYABLE":
            ledger.record(node, "READY", evidence=evidence, actor="resource-failure-resolution")
            state = "READY"
        for step in WS.completion_transition_path(state, "STAGE_SUCCEEDED", ledger.registry_path):
            ledger.record(node, step, evidence=evidence, actor="resource-failure-resolution")
    workflow_failure = next((row for row in reversed(ledger.journal()) if row.get("workflow_state")), {})
    if (planned and ledger.state()["workflow_state"] == "FAILED_RETRYABLE"
            and (workflow_failure.get("evidence") or {}).get("node") in planned):
        ledger.set_workflow_state("READY", evidence={"resolved_resource_failures": list(planned)},
                                  actor="resource-failure-resolution")
    return list(planned)
