"""Read current obligations immediately before an existing carrier emits them.

False means positively superseded; unreadable/conflicting evidence raises so
the carrier retains its lease for recovery instead of discarding the notice.
This is not a second delivery queue or a workflow outcome writer.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import time

from route_identity import route_hash


_CAPABILITY_ROUTE_CACHE = None
_BATCH_REGISTRY_SNAPSHOTS = {}
_BATCH_ROUTE_SNAPSHOTS = {}
DEFAULT_RECORD_GRACE_SECONDS = 900


def delivery_batch_key(*, registry_id: str, parent_sid: str, attempt_id: str,
                       route_id: str = "", route_hash_value: str = "",
                       route_node: str = "", parent_cycle_id: str = "",
                       sealed_batch_id: str = "", sealed_attempt_ids=()):
    """Return a key only for an exact, already-validated batch relationship.

    The caller supplies route identity only after ``bound_route`` succeeds and
    sealed membership only from the existing sealed declaration. Never infer
    membership from launch order, slug, a shared cycle value, or a batch label.
    """
    if not all(isinstance(value, str) and value for value in
               (registry_id, parent_sid, attempt_id)):
        return None
    if parent_cycle_id:
        return ("parent-cycle", registry_id, parent_sid, parent_cycle_id)
    if route_node in {"frame", "frame-alternative"} and route_id and route_hash_value:
        return ("frame-pair", registry_id, parent_sid, route_id, route_hash_value)
    members = tuple(sorted(set(sealed_attempt_ids))) if isinstance(
        sealed_attempt_ids, (list, tuple, set, frozenset)) else ()
    if (sealed_batch_id and len(members) > 1 and attempt_id in members):
        return ("sealed-parallel", registry_id, parent_sid, sealed_batch_id, members)
    return None


def resolve_delivery_batch(jobs: Path, record: dict, *, recipient_key: str = "",
                           delivery_root: Path | None = None,
                           now_ns: int | None = None) -> dict | None:
    """Read the exact successful sibling set for one registered completion.

    ``None`` means the extra batching evidence was unavailable or ambiguous;
    callers then retain the established singleton delivery.  This function
    never writes registry, route, pending-delivery, or lease state.
    """
    try:
        from dispatch_contract import parse_registry_metadata, attempt_process_quiescence
        from dispatch_attempt_policy import committed_outcome
        jobs = Path(jobs).resolve(strict=True)
        before = jobs.stat()
        registry_id = f"{jobs}:{before.st_dev}:{before.st_ino}"
        signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        cached_registry = _BATCH_REGISTRY_SNAPSHOTS.get(str(jobs))
        if cached_registry and cached_registry[0] == signature:
            parsed = cached_registry[1]
        else:
            lines = tuple(jobs.read_text(encoding="utf-8").splitlines())
            parsed_rows = []
            for line in lines:
                fields = line.split("\t")
                if len(fields) != 6:
                    continue
                meta = parse_registry_metadata(fields[5])
                if meta.get("attempt_id"):
                    parsed_rows.append((fields, meta))
            parsed = tuple(parsed_rows)
            _BATCH_REGISTRY_SNAPSHOTS[str(jobs)] = (signature, parsed)
        after = jobs.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns):
            return None
        seed_ids = record.get("attempt_ids")
        receipt = record.get("receipt") or {}
        if (not isinstance(seed_ids, list) or len(seed_ids) != 1
                or not isinstance(receipt, dict)
                or receipt.get("kind") in {"notice", "supervision", "human-gate"}
                or receipt.get("replacement_attention") or receipt.get("human_gate")
                or receipt.get("state") != "ready"
                or receipt.get("delivery_classification") != "success"
                or not isinstance(receipt.get("children"), list)
                or not receipt["children"]
                or any(not isinstance(child, dict)
                       or child.get("required_action") != "advance-completed"
                       for child in receipt["children"])):
            return None
        seed = seed_ids[0]
        matches = [(fields, meta) for fields, meta in parsed if meta.get("attempt_id") == seed]
        if len(matches) != 1:
            return None
        seed_fields, seed_meta = matches[0]
        if ((recipient_key and seed_meta.get("parent_sid") != recipient_key)
                or seed_meta.get("dispatch_depth") != "1"
                or seed_meta.get("delivery_intent") != "1"
                or seed_meta.get("failure_class") not in {None, "pass"}
                or seed_meta.get("note", "").startswith(("dead-", "review-blocking"))):
            return None
        route_cache: dict[tuple[str, str], dict] = {}
        def route_parent(meta):
            prefix = "owner_" if not meta.get("route_file") else ""
            path, rid, digest = (meta.get(prefix + key, "") for key in
                                 ("route_file", "route_id", "route_hash"))
            if not (path and rid and digest):
                return None
            cache_key = (path, digest)
            route = route_cache.get(cache_key)
            if route is None:
                route_path = Path(path)
                route_stat = route_path.stat()
                route_signature = (route_path, route_stat.st_ino, route_stat.st_size,
                                   route_stat.st_mtime_ns, digest)
                route = _BATCH_ROUTE_SNAPSHOTS.get(route_signature)
                if route is None:
                    route = json.loads(route_path.read_text(encoding="utf-8"))
                if (not isinstance(route, dict) or route.get("route_id") != rid
                        or route.get("route_hash") != digest or route_hash(route) != digest):
                    raise ValueError("batch-route-binding-mismatch")
                _BATCH_ROUTE_SNAPSHOTS[route_signature] = route
                route_cache[cache_key] = route
            return route
        seed_route = route_parent(seed_meta)
        if not seed_route:
            return None
        parent_cycle = seed_route.get("parent_cycle_id")
        key = delivery_batch_key(
            registry_id=registry_id, parent_sid=seed_meta.get("parent_sid", ""),
            attempt_id=seed, route_id=seed_meta.get("route_id", ""),
            route_hash_value=seed_meta.get("route_hash", ""),
            route_node=seed_meta.get("route_node", ""),
            parent_cycle_id=parent_cycle or "",
            sealed_batch_id=seed_meta.get("managed_sealed_batch_id", ""),
            sealed_attempt_ids=(seed_meta.get("managed_sealed_attempt_ids", "") or "").split(" "),
        )
        if key is None:
            return None
        siblings = []
        pending_states = {}
        for fields, meta in parsed:
            if (meta.get("dispatch_depth") != "1" or meta.get("registered_worker") != "1"
                    or meta.get("parent_sid") != seed_meta.get("parent_sid")
                    or meta.get("delivery_intent") != "1"):
                continue
            if committed_outcome(fields[1], meta) == "succeeded":
                did = meta.get("delivery_id")
                if did and delivery_root is not None:
                    try:
                        import dispatch_pending_delivery
                        materialized = dispatch_pending_delivery.read(
                            Path(delivery_root), seed_meta.get("parent_sid", ""), did)
                        pending_states[did] = materialized.get("state") if materialized else None
                    except Exception:
                        pending_states[did] = "unreadable"
                    if pending_states[did] in {"acked", "expired", "rejected"}:
                        continue
            route = route_parent(meta)
            if not route:
                continue
            candidate_key = delivery_batch_key(
                registry_id=registry_id, parent_sid=meta.get("parent_sid", ""),
                attempt_id=meta.get("attempt_id", ""), route_id=meta.get("route_id", ""),
                route_hash_value=meta.get("route_hash", ""), route_node=meta.get("route_node", ""),
                parent_cycle_id=route.get("parent_cycle_id") or "",
                sealed_batch_id=meta.get("managed_sealed_batch_id", ""),
                sealed_attempt_ids=(meta.get("managed_sealed_attempt_ids", "") or "").split(" "),
            )
            if candidate_key == key:
                siblings.append((fields, meta))
        if not any(meta.get("attempt_id") == seed for _, meta in siblings):
            return None
        for fields, meta in siblings:
            if fields[1] not in {"open", "running"}:
                continue
            process = attempt_process_quiescence(meta)
            if process.state in {"live", "unverifiable"}:
                return {"key": key, "delivery_ids": (),
                        "attempt_ids": tuple(sorted(row.get("attempt_id", "")
                                                     for _, row in siblings)),
                        "member_delivery_ids": tuple(sorted({row.get("delivery_id", "")
                                                               for row_fields, row in siblings
                                                               if row.get("delivery_id")
                                                               and (row_fields[1] in {"open", "running"}
                                                                    or committed_outcome(
                                                                        row_fields[1], row)
                                                                    == "succeeded")})),
                        "registry_path": str(jobs), "registry_signature": signature,
                        "seed_identity": (record.get("delivery_id"), record.get("receipt_digest"),
                                          tuple(record.get("attempt_ids") or ()),
                                          record.get("recipient_digest")),
                        "held": len(siblings) > 1,
                        "outstanding_attempt_ids": (meta.get("attempt_id", ""),)}
        outstanding = []
        delivery_ids = []
        now_mono_ns = time.monotonic_ns() if now_ns is None else now_ns
        for fields, meta in siblings:
            aid = meta.get("attempt_id", "")
            if committed_outcome(fields[1], meta) == "succeeded":
                did = meta.get("delivery_id")
                if did:
                    row_time_ns = None
                    try:
                        stamp = meta.get("delivery_intent_at_ns", "")
                        if stamp.isascii() and stamp.isdigit():
                            parsed_stamp = int(stamp)
                            if parsed_stamp > 0:
                                row_time_ns = parsed_stamp
                    except (ValueError, OverflowError, TypeError, AttributeError):
                        pass
                    state = pending_states.get(did)
                    if state == "unreadable":
                        continue
                    if state in {"acked", "expired", "rejected"}:
                        continue
                    if state is None:
                        if (row_time_ns is not None
                                and 0 <= now_mono_ns - row_time_ns
                                <= DEFAULT_RECORD_GRACE_SECONDS * 1_000_000_000):
                            outstanding.append(aid)
                        # Missing/unreadable terminal time fails open. Both
                        # operands are monotonic; never compare created_at_ns
                        # or a registry wall-clock field to this timestamp.
                        continue
                    if state in {"pending", "claimed", "sent-ambiguous"}:
                        delivery_ids.append(did)
            elif fields[1] in {"open", "running"}:
                continue
            elif fields[1] not in {"done", "killed", "cancelled"}:
                return None
        return {"key": key, "delivery_ids": tuple(sorted(set(delivery_ids))),
                "attempt_ids": tuple(sorted(meta.get("attempt_id", "") for _, meta in siblings)),
                "member_delivery_ids": tuple(sorted({meta.get("delivery_id", "")
                                                       for row_fields, meta in siblings
                                                       if meta.get("delivery_id")
                                                       and (row_fields[1] in {"open", "running"}
                                                            or committed_outcome(
                                                                row_fields[1], meta)
                                                            == "succeeded")})),
                "registry_path": str(jobs), "registry_signature": signature,
                "seed_identity": (record.get("delivery_id"), record.get("receipt_digest"),
                                  tuple(record.get("attempt_ids") or ()),
                                  record.get("recipient_digest")),
                "held": len(siblings) > 1 and bool(outstanding),
                "outstanding_attempt_ids": tuple(sorted(set(outstanding)))}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None


def _capability_route_reader():
    """Load the hyphenated route module once when it is not importable by name."""
    global _CAPABILITY_ROUTE_CACHE
    try:
        import capability_route
        return capability_route
    except ImportError:
        if _CAPABILITY_ROUTE_CACHE is not None:
            return _CAPABILITY_ROUTE_CACHE
        module_path = Path(__file__).with_name("capability-route.py")
        spec = importlib.util.spec_from_file_location("notice_capability_route", module_path)
        if spec is None or spec.loader is None:
            raise ValueError("notice-terminal-gate-reader-unavailable")
        capability_route = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(capability_route)
        _CAPABILITY_ROUTE_CACHE = capability_route
        return capability_route


def closed_outcome(path: Path, route: dict) -> dict | None:
    """Only the exact route generation's closure is evidence of resolution."""
    try:
        outcome = json.loads(path.with_name(path.stem + ".outcome.json").read_text())
    except FileNotFoundError:
        return None
    if (not isinstance(outcome, dict)
            or outcome.get("route_id") != route.get("route_id")
            or outcome.get("route_hash") != route.get("route_hash")):
        raise ValueError("notice-route-outcome-mismatch")
    if outcome.get("inline_finish_id"):
        import inline_finish
        pending = inline_finish.pending_state(Path(route.get("artifact_root", "")), route["route_id"])
        if not pending or pending.get("inline_finish_id") != outcome.get("inline_finish_id"):
            raise ValueError("notice-inline-finish-state-mismatch")
        if pending.get("state") != "finished":
            outcome = dict(outcome, finish_pending=True, finish_state=pending.get("state"),
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
    outcome = closed_outcome(*bound) if bound else None
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
    except ImportError:
        pass
    capability_route = _capability_route_reader()
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
    if closed_outcome(path, route):
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


def exact_completion_already_received(record: dict, metadata: dict, jobs: Path) -> bool:
    """Recognize an already ACKed exact result without treating ACK as work."""
    receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
    children = receipt.get("children")
    if (receipt.get("state") != "ready" or not isinstance(children, list) or not children
            or not metadata.get("parent_sid") or not metadata.get("parent_completion_delivery")):
        return False
    attempts = {child.get("attempt_id") for child in children if isinstance(child, dict)}
    if len(attempts) != len(children) or not attempts:
        return False
    try:
        from dispatch_completion_join import recover_acknowledged_batch_outcome
        from dispatch_receipt_identity import canonical_receipt
        from dispatch_seat_handover import effective_parent
        recipient = effective_parent(metadata, jobs)
        if not recipient:
            return False
        recovered = recover_acknowledged_batch_outcome(
            jobs, attempts, recipient_sid=recipient,
            parent_attempt_id=receipt.get("parent_attempt_id", ""),
            parent_completion_delivery=metadata["parent_completion_delivery"])
        return (recovered is not None
                and canonical_receipt(recovered) == canonical_receipt(receipt))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False


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
            if exact_completion_already_received(record, meta, jobs):
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


def project_parent_close(root, record, *, jobs=None) -> None:
    """Show children whose parent-requested close settled as completed.

    Only the carrier's in-memory view changes. The stored receipt,
    identity/digest, lease and replay remain the original record. Raises
    CompletionDeferred while that close is still terminating.
    """
    from dispatch_completion_join import CompletionDeferred
    receipt = record.get("receipt") or {}
    if receipt.get("kind") in {"human-gate", "supervision", "notice"}:
        return
    jobs = Path(receipt.get("job_registry") or jobs or Path(root) / "jobs.log")
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


def keep_claim(root, recipient, delivery_id, record, claim_owner, *, jobs=None) -> bool:
    """Retire only proven obsolete notices; unknown evidence stays recoverable."""
    import dispatch_pending_delivery as pending
    from dispatch_completion_join import CompletionDeferred
    jobs = Path((record.get("receipt") or {}).get("job_registry") or jobs or Path(root) / "jobs.log")
    try:
        if notice_is_current(record, jobs=jobs):
            project_parent_close(root, record, jobs=jobs)
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
