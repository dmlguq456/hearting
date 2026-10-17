"""Read current obligations immediately before an existing carrier emits them.

False means positively superseded; unreadable/conflicting evidence raises so
the carrier retains its lease for recovery instead of discarding the notice.
This is not a second delivery queue or a workflow outcome writer.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from route_identity import route_hash


def closed_outcome(path: Path, route: dict, jobs: Path | None = None) -> dict | None:
    """Only the exact route generation's closure is evidence of resolution."""
    try:
        outcome = json.loads(path.with_name(path.stem + ".outcome.json").read_text())
    except FileNotFoundError:
        return None
    if (not isinstance(outcome, dict)
            or outcome.get("route_id") != route.get("route_id")
            or outcome.get("route_hash") != route.get("route_hash")):
        raise ValueError("notice-route-outcome-mismatch")
    import late_resource_result
    jobs_value = jobs if jobs is not None else os.environ.get("AGENT_DISPATCH_JOBS")
    current = late_resource_result.current_outcome(
        path, route, jobs=Path(jobs_value) if jobs_value else None)
    if current is not None:
        outcome = current
    if outcome.get("inline_finish_id"):
        import inline_finish
        pending = inline_finish.pending_state(Path(route.get("artifact_root", "")), route["route_id"])
        if not pending or pending.get("inline_finish_id") != outcome.get("inline_finish_id"):
            raise ValueError("notice-inline-finish-state-mismatch")
        if pending.get("state") != "finished":
            outcome = dict(outcome, finish_pending=True, finish_state=pending.get("state"),
                           terminal_gate_proven=False)
    from workflow_state import WorkflowLedger
    import route_parent_close
    ledger = WorkflowLedger(route["route_id"], route["route_hash"],
                            jobs=Path(jobs_value) if jobs_value else None)
    close_intent = route_parent_close.ledger_intent(route, ledger)
    close_result = route_parent_close.settled_result(route, ledger)
    if close_result and close_result.get("state") == "cancelled":
        outcome = dict(outcome, state="cancelled", disposition="cancelled",
                       cancellation_settled=True, parent_close_result=close_result,
                       terminal_gate_proven=False)
    elif close_intent:
        outcome = dict(outcome, state="cancellation-pending", cancellation_requested=True,
                       terminal_gate_proven=False)
    return outcome


def bound_route(metadata: dict, jobs: Path, expected_id: str = "", *, observer=None):
    """Use the row's exact binding, including a verified owner advance."""
    prefix = "" if metadata.get("route_file") else "owner_"
    path, rid, digest = (metadata.get(prefix + key) for key in ("route_file", "route_id", "route_hash"))
    if prefix == "owner_" and all((path, rid, digest, metadata.get("attempt_id"))):
        from owner_route_binding import resolve_owner_route_lifecycle
        binding, _ = resolve_owner_route_lifecycle(
            jobs, owner_attempt_id=metadata["attempt_id"], observer=observer)
        if binding:
            path, rid, digest = binding.route_file, binding.route_id, binding.route_hash
    if not path:
        return None  # Historical route-free rows have no closure authority.
    if observer is not None:
        observer("file", Path(path))
    route = json.loads(Path(path).read_text())
    if (not isinstance(route, dict) or route.get("route_id") != rid
            or route.get("route_hash") != digest or route_hash(route) != digest
            or (expected_id and rid != expected_id)):
        raise ValueError("notice-route-binding-mismatch")
    return Path(path), route


def route_obligation_closed(metadata: dict, jobs: Path) -> bool:
    bound = bound_route(metadata, jobs)
    outcome = closed_outcome(*bound, jobs=jobs) if bound else None
    return bool(outcome and not outcome.get("finish_pending"))


def framed_decision_consumed(record: dict, metadata: dict, jobs: Path) -> bool:
    """Prove that this recipient consumed this frame's exact completion notice.

    The terminal marker is useful only together with the immutable decision
    artifact, both current frame markers, and the pending notice's own identity.
    A route-wide PASS by itself is not a consumption receipt.
    """
    from dispatch_completion_join import pending_record_identity
    _route_id, route_node, parent_attempt = pending_record_identity(metadata, jobs)
    attempts = record.get("attempt_ids")
    if (record.get("parent_attempt_id") != parent_attempt
            or record.get("route_node") != route_node
            or route_node not in {"frame", "frame-alternative"}
            or not metadata.get("attempt_id")
            or attempts != [metadata["attempt_id"]]):
        return False
    parent_sid = metadata.get("parent_sid")
    if not isinstance(parent_sid, str) or not parent_sid:
        return False
    import dispatch_pending_delivery as pending
    if record.get("recipient_digest") != pending.recipient_digest(parent_sid):
        return False
    bound = bound_route(metadata, jobs, record.get("route_id", ""))
    if bound is None:
        return False
    _route_path, route = bound
    try:
        import route_plan
        if not route_plan.is_framed_route(route):
            return False
        import capability_route
    except ImportError:
        import importlib.util
        module_path = Path(__file__).with_name("capability-route.py")
        spec = importlib.util.spec_from_file_location("notice_capability_route", module_path)
        if spec is None or spec.loader is None:
            raise ValueError("notice-terminal-gate-reader-unavailable")
        capability_route = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(capability_route)
    # route-decision is the existing depth-0 runtime terminal, with no
    # registered process/attempt link. Read its semantic marker, then bind
    # those exact bytes below; the two frame workers still require the full
    # exact-attempt reader.
    gates = capability_route.terminal_gate_observation(route, jobs=jobs)
    terminal = gates.get("route-decision")
    if (not terminal or terminal.get("passed") is not True or terminal.get("current") is not True
            or not terminal.get("marker_digest") or not terminal.get("evidence")):
        return False

    # The terminal marker binds the actual route_decision_v1 file bytes. Read
    # that record through its canonical validator, and reject a changed or
    # unreadable evidence file instead of treating terminality as receipt.
    marker_path = capability_route.completion_dir(route["route_id"], jobs=jobs) / "route-decision.json"
    marker_bytes = marker_path.read_bytes()
    marker = json.loads(marker_bytes)
    if (hashlib.sha256(marker_bytes).hexdigest() != terminal.get("marker_digest")
            or marker.get("route_id") != route["route_id"]
            or marker.get("route_hash") != route["route_hash"]
            or marker.get("node_id") != "route-decision"
            or marker.get("completion_gate") != "route-decision"
            or marker.get("registered_worker") is not False
            or marker.get("attempt_id") is not None
            or marker.get("dispatch_depth") != 0
            or marker.get("execution_surface") != "inline"):
        return False
    marker_evidence = marker.get("evidence") or {}
    evidence_path = Path(marker_evidence.get("path") or "")
    if not evidence_path.is_absolute():
        return False
    import dispatch_contract
    if (str(evidence_path) != terminal.get("evidence")
            or dispatch_contract.evidence_digest(evidence_path) != marker_evidence.get("sha256")):
        return False
    decision_record = route_plan.read_record(evidence_path)
    frame_route = decision_record["decision"]["frame_route"]
    if (frame_route.get("route_id") != route.get("route_id")
            or frame_route.get("route_hash") != route.get("route_hash")
            or not frame_route.get("cycle_id")):
        return False
    import artifact_producer
    artifact_root = Path(route.get("artifact_root", "")).resolve()
    cycle = artifact_producer.read_cycle_record(artifact_root, frame_route["cycle_id"])
    if (not cycle or cycle.get("cycle_id") != frame_route["cycle_id"]
            or cycle.get("route_id") != route.get("route_id")
            or cycle.get("route_hash") != route.get("route_hash")):
        return False
    cycle_dir = artifact_producer.cycle_dir(artifact_root, cycle["campaign_id"], cycle["cycle_id"], cycle)
    expected_record = (cycle_dir / "artifacts" / route_plan.RECORD_RELATIVE).resolve()
    if evidence_path.resolve() != expected_record:
        return False

    # The two exact frame markers are the attempt proof the decision record
    # consumes. Their marker reader checks current evidence and exact registry
    # readiness; their evidence paths and bytes must also match the briefs that
    # the decision record sealed.
    nodes = {node.get("id"): node for node in route.get("nodes", [])}
    frame_attempts = {}
    briefs = {item.get("node"): item for item in decision_record["decision"].get("briefs", [])
              if isinstance(item, dict)}
    import route_plan as RP
    legs = RP.frame_legs(route)
    if not RP.valid_frame_legs(route, legs):
        return False
    for node_id in legs:
        node = nodes.get(node_id)
        if not node:
            return False
        proof = capability_route._marker_identity_row(
            route, node, node_id, node.get("completion_gate"), jobs=jobs, exact_terminal=True)
        if proof.get("passed") is not True or not proof.get("attempt_id"):
            return False
        brief = briefs.get(node_id)
        if not brief or not isinstance(brief.get("path"), str) or not isinstance(brief.get("sha256"), str):
            return False
        brief_path = cycle_dir / "artifacts" / "shards" / node_id / "direction-brief.md"
        expected_brief = brief_path.relative_to(artifact_root).as_posix()
        if brief["path"] != expected_brief:
            return False
        if (Path(proof.get("evidence") or "").resolve() != brief_path.resolve()
                or route_plan.file_digest(brief_path) != brief["sha256"]):
            return False
        frame_attempts[node_id] = proof["attempt_id"]

    # The existing producer creates one durable notice per frame attempt,
    # including the no-parent-attempt sentinel for depth-0 frame callers.
    # Both frame proofs must be current, but only this notice's own node and
    # attempt are its delivery obligation; it never owes its sibling's notice.
    return frame_attempts[route_node] == attempts[0]


def _gate_resolution(entries: list, gate: str, delivery: str) -> dict:
    """Resolve this delivery's raise, never a previous or future question.

    The producer writes pending before BLOCKED_HUMAN_GATE. Absence of that
    exact raise is still publication in progress, not proof of resolution.
    Keep earlier resolved raises readable after a later question was raised.
    """
    from workflow_state import human_gate_resolution
    found = False
    end = len(entries)
    for index, entry in enumerate(entries):
        evidence = entry.get("evidence") or {}
        if not isinstance(evidence, dict):
            raise ValueError("notice-gate-journal-invalid")
        if entry.get("workflow_state") == "BLOCKED_HUMAN_GATE" and evidence.get("gate") == gate:
            if found:
                end = index
                break
            found = evidence.get("delivery") == delivery
    if not found:
        raise ValueError("notice-gate-publication-pending")
    resolution = human_gate_resolution(entries[:end], gate)
    if end < len(entries) and resolution["status"] == "blocked":
        raise ValueError("notice-gate-journal-conflict")
    return resolution


def _legacy_gate_current(record: dict, jobs: Path, child: dict, metadata: dict) -> bool:
    import dispatch_pending_delivery as pending
    from workflow_state import WorkflowLedger, node_raises_human_gate
    bound = bound_route(metadata, jobs, record.get("route_id", ""))
    if bound is None:
        raise ValueError("notice-gate-route-unbound")
    path, route = bound
    if closed_outcome(path, route, jobs=jobs):
        return False
    gate = child["required_action"].removeprefix("human-gate:")
    node = next((n for n in route.get("nodes", []) if n.get("id") == record.get("route_node")), {})
    if not node_raises_human_gate(node, gate):
        return False
    # Strict read: a corrupt journal is unknown, not an unraised gate.
    journal = WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs).journal_path
    entries = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    if any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("notice-gate-journal-invalid")
    expected = pending.record_directory(jobs.parent, metadata["parent_sid"]) / (record["delivery_id"] + ".json")
    resolution = _gate_resolution(entries, gate, str(expected))
    current = resolution["status"] == "blocked"
    if current:
        if resolution.get("artifact") != child.get("reason"):
            raise ValueError("notice-gate-artifact-mismatch")
        from human_gate_receipt import _absolute_regular
        _absolute_regular(child["reason"], "artifact")
        if resolution.get("artifact_sha256"):
            from workflow_state import require_gate_artifact_current
            require_gate_artifact_current(resolution)
    return current


def notice_is_current(record: dict, *, jobs: Path | None = None) -> bool:
    receipt = record.get("receipt") or {}
    if receipt.get("kind") == "supervision":
        from dispatch_supervision import notice_is_current as current
        return current(record)
    if receipt.get("kind") == "notice":
        from session_notice import notice_is_current as current
        return current(record)
    if receipt.get("kind") == "human-gate":
        import human_gate_receipt as gate
        try:
            gate._load_route(receipt)
        except gate.HumanGateReceiptError as exc:
            if str(exc) == "route-already-closed":
                return False
            raise
        gate._validate_shape(receipt)
        resolution = _gate_resolution(gate._load_journal(receipt, Path(receipt["job_registry"])),
            receipt["gate"], str(gate.pending_delivery.record_path(Path(receipt["job_registry"]).parent,
                receipt["recipient_thread_id"], record["delivery_id"])))
        if resolution["epoch"] != receipt["gate_epoch"]:
            raise ValueError("notice-gate-epoch-unproved")
        return resolution["status"] == "blocked"
    from dispatch_supervision import _rows
    # Terminal-intent receipts deliberately omit job_registry. The carrier
    # already owns the exact queue root; never resolve an empty string to cwd.
    jobs = Path(receipt.get("job_registry") or jobs or "")
    if not jobs.is_absolute():
        raise ValueError("notice-jobs-unbound")
    rows = _rows(jobs)
    current = not receipt.get("children")
    for child in receipt.get("children", []):
        action = str(child.get("required_action", ""))
        row = rows.get(child.get("attempt_id"))
        if row is None:
            if action.startswith("human-gate:"):
                raise ValueError("notice-gate-attempt-missing")
            current = True
            continue  # Old route-free completion records remain deliverable.
        status, meta = row
        import route_parent_close
        settled = route_parent_close.cancellation_settled(meta, jobs)
        if settled is False:
            from dispatch_completion_join import CompletionDeferred
            raise CompletionDeferred("termination-pending")
        if settled is True:
            current = True
            continue
        if action.startswith("human-gate:"):
            if not _legacy_gate_current(record, jobs, child, meta):
                return False
        elif action == "advance-completed":
            if framed_decision_consumed(record, meta, jobs):
                continue
            current = True
        elif route_obligation_closed(meta, jobs):
            if meta.get("workflow_completion") == "runtime-v1":
                from dispatch_terminal_commit import owner_completion_pending
                if owner_completion_pending(jobs, status, meta):
                    current = True
                    continue  # Closure alone does not prove cycle settlement.
            continue
        current = True
    return current


def keep_claim(root, recipient, delivery_id, record, claim_owner, *, jobs=None) -> bool:
    """Retire only proven obsolete notices; unknown evidence stays recoverable."""
    import dispatch_pending_delivery as pending
    from dispatch_completion_join import CompletionDeferred
    jobs = Path((record.get("receipt") or {}).get("job_registry") or jobs or Path(root) / "jobs.log")
    try:
        if notice_is_current(record, jobs=jobs):
            # Only the carrier's in-memory view changes. The stored receipt,
            # identity/digest, lease and replay remain the original record.
            receipt = record.get("receipt") or {}
            if receipt.get("kind") not in {"human-gate", "supervision", "notice"}:
                from dispatch_supervision import _rows
                import route_parent_close
                rows = _rows(jobs)
                children = []
                projected = False
                for child in receipt.get("children", []):
                    row = rows.get(child.get("attempt_id"))
                    settled = route_parent_close.cancellation_settled(row[1], jobs) if row else None
                    if settled is False:
                        raise CompletionDeferred("termination-pending")
                    if settled is True:
                        projected = True
                        child = dict(child, status="done", readiness="ready", reason="cancelled-by-parent",
                                     required_action="advance-completed", delivery_classification="success")
                        child.pop("next_leg", None)
                    children.append(child)
                if projected:
                    record["receipt"] = dict(receipt, children=children,
                        delivery_classification=("attention" if any(c.get("delivery_classification") == "attention"
                            for c in children) else "success"))
            return True
        pending.reject_claimed(root, recipient, delivery_id, claim_owner=claim_owner,
                               reason=("supervision-resolved" if record.get("receipt", {}).get("kind") == "supervision"
                                       else "notice-obligation-resolved"))
    except CompletionDeferred:
        try:
            pending.release_claim(root, recipient, delivery_id, claim_owner=claim_owner)
        except pending.PendingDeliveryError:
            pass  # A lost claim stays with its existing carrier/lease recovery.
    except (OSError, ValueError, KeyError, pending.PendingDeliveryError):
        pass
    return False
