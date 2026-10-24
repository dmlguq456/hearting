#!/usr/bin/env python3
"""Hermetic before/after measurement for depth-1 completion delivery batches.

Each source tree is imported in a fresh Python process. The fixture deliberately
uses the public pending-delivery and sweep APIs plus production-shaped route and
registry records, so the frozen tree reports its actual per-completion behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest import mock


SEED = 20261023
SIBLINGS = 24
FAILURE_INDICES = (7, 18)
PARENT = "att-measurement-parent"
PARENT_SID = "ses-measurement-parent"


def _child(source_root: Path, output: Path) -> int:
    source_root = source_root.resolve()
    sys.path.insert(0, str(source_root / "utilities"))
    sys.path.insert(0, str(source_root / "hooks"))
    import dispatch_pending_delivery as pending
    import dispatch_session_sweep as sweep
    import dispatch_notice_state as notice_state
    from route_identity import route_hash

    rows = []
    real_monotonic_ns = time.monotonic_ns
    fake_clock = [SEED * 1_000_000_000]
    time.monotonic_ns = lambda: fake_clock[0]
    with tempfile.TemporaryDirectory(prefix="delivery-batch-fixture-") as temporary:
        base = Path(temporary)
        state = base / "state"
        state.mkdir()
        jobs = state / "jobs.log"
        jobs.write_text("", encoding="utf-8")
        histories = {"all_success": [], "two_failures": []}

        for variant in histories:
            scenario = state / variant
            scenario.mkdir()
            dispatch_root = scenario
            registry = scenario / "jobs.log"
            registry.write_text("", encoding="utf-8")
            records: dict[str, dict] = {}
            route_files = []
            attempts = []
            for index in range(SIBLINGS):
                attempt_id = f"att-measure-{index:032x}"
                route_id = f"rt-measure-{variant}-{index:02d}"
                route = {"route_id": route_id,
                         "parent_cycle_id": f"cycle-measure-{variant}",
                         "nodes": [{"id": "execute"}]}
                route["route_hash"] = route_hash(route)
                route_file = scenario / f"{route_id}.json"
                route_file.write_text(json.dumps(route, sort_keys=True), encoding="utf-8")
                route_files.append((route_id, route_file, route["route_hash"]))
                attempts.append(attempt_id)
                histories[variant].append({"attempt_id": attempt_id, "route_id": route_id,
                                           "route_file": str(route_file),
                                           "route_hash": route["route_hash"]})

            # A parent has 24 registered depth-1 siblings from the outset. Rows
            # become terminal one at a time; later completions are not materialized
            # until their position in this deterministic timeline.
            registry.write_text("".join(
                "2026-10-23T00:00:00Z\trunning"
                + f"\t{scenario}\t{scenario}\tframe\tattempt_id={attempts[index]},"
                + f"dispatch_depth=1,registered_worker=1,worker_type=execute,parent_sid={PARENT_SID},"
                + f"parent_attempt_id={PARENT},parent_harness=claude,route_file={route_files[index][1]},"
                + f"route_id={route_files[index][0]},route_hash={route_files[index][2]},route_node=execute,"
                + f"delivery_intent=1,delivery_intent_at_ns={fake_clock[0]},"
                + f"failure_class=pass,note=completed-marker,"
                + f"delivery_id=delivery-measure-{variant}-{index:02d}\n"
                for index in range(SIBLINGS)
            ), encoding="utf-8")

            # Registry state is rewritten as each sibling finishes. This keeps the
            # route membership real while the fixed fake clock advances by index.
            raw_rows = registry.read_text(encoding="utf-8").splitlines()
            timeline = []
            recipient_kind = "claude-parent-runtime"
            previous_fixed = 0
            for index in range(SIBLINGS):
                fake_clock[0] += 1_000_000_000
                failed = variant == "two_failures" and index in FAILURE_INDICES
                fields = raw_rows[index].split("\t")
                fields[1] = "done"
                fields[0] = f"2026-10-23T00:00:{index:02d}Z"
                metadata = fields[5].split(",")
                metadata = [part for part in metadata if not part.startswith(("failure_class=", "note="))]
                metadata.extend(("failure_class=fail", "note=dead-worker-fail") if failed else
                                ("failure_class=pass", "note=completed-marker"))
                fields[5] = ",".join(metadata)
                raw_rows[index] = "\t".join(fields)
                registry.write_text("\n".join(raw_rows) + "\n", encoding="utf-8")

                attempt_id = attempts[index]
                route_id, _route_file, _route_digest = route_files[index]
                delivery_id = f"delivery-measure-{variant}-{index:02d}"
                classification = "attention" if failed else "success"
                child = {"attempt_id": attempt_id, "status": "done", "readiness": "ready",
                         "reason": "terminal-failure-or-unclosed" if failed else "terminal-complete",
                         "required_action": "inspect-done-failure" if failed else "advance-completed",
                         "harness": "claude", "delivery_classification": classification}
                receipt = {"schema_version": 2, "state": "ready", "parent_attempt_id": PARENT,
                           "job_registry": str(registry), "children": [child],
                           "delivery_classification": classification}
                record = pending.create(
                    dispatch_root, recipient_kind=recipient_kind, recipient_key=PARENT_SID,
                    delivery_id=delivery_id, session_generation="",
                    session_generation_supported="0", attempt_ids=[attempt_id],
                    parent_attempt_id=PARENT, route_id=route_id, route_node="execute",
                    receipt=receipt, receipt_digest=pending._canonical_receipt_digest(receipt),
                    row_revisions={attempt_id: f"revision-{index:02d}"})
                records[delivery_id] = record

                resolver = getattr(notice_state, "resolve_delivery_batch", None)
                decision = (resolver(registry, record, recipient_key=PARENT_SID,
                                     delivery_root=dispatch_root) if resolver else None)

                delivered, _count = sweep.sweep_deliver(
                    dispatch_root, recipient_kind, PARENT_SID)
                body = sweep.delivery_context([(dispatch_root, delivered)]) if delivered else ""
                if delivered:
                    # The sweep's exact prompt-context acknowledgement is the
                    # native receipt boundary for this carrier.
                    sweep.ack_delivered(dispatch_root, PARENT_SID, delivered,
                                        acked_by="fixture-parent-turn")
                transitions = []
                for record_id, before in records.items():
                    after = pending.read(dispatch_root, PARENT_SID, record_id)
                    if before.get("state") != after.get("state"):
                        transitions.append({"delivery_id": record_id,
                                            "from": before.get("state"), "to": after.get("state")})
                    records[record_id] = after
                timeline.append({"completion": index + 1, "attempt_id": attempt_id,
                                 "failed": failed, "wakes": 1 if delivered else 0,
                                 "emissions": 1 if delivered else 0,
                                 "injected_characters": len(body),
                                 "fixed_instruction_characters": _fixed_instruction_length(body),
                                 "ack_transitions": transitions,
                                 "ack_sources": {transition["delivery_id"]: "fixture-ack"
                                                 for transition in transitions
                                                 if transition["to"] == "acked"},
                                 "delivered_ids": [item["delivery_id"] for item in delivered],
                                 "batch_decision": ({"held": decision.get("held"),
                                     "delivery_ids": list(decision.get("delivery_ids", ())),
                                     "outstanding_attempt_ids": list(decision.get(
                                         "outstanding_attempt_ids", ())) } if decision else None)})

            unresolved = [did for did in records
                          if pending.read(dispatch_root, PARENT_SID, did)["state"] != "acked"]
            total = {
                "wakes": sum(item["wakes"] for item in timeline),
                "emissions": sum(item["emissions"] for item in timeline),
                "injected_characters": sum(item["injected_characters"] for item in timeline),
                "fixed_instruction_characters": sum(item["fixed_instruction_characters"] for item in timeline),
                "ack_transition_count_by_record": {
                    did: sum(1 for item in timeline for transition in item["ack_transitions"]
                             if transition["delivery_id"] == did and transition["to"] == "acked")
                    for did in records
                },
                "unsettled_record_ids": unresolved,
                "failure_delivery_completion": {
                    item["attempt_id"]: item["completion"] for item in timeline if item["failed"]
                    and item["wakes"]
                },
                "timeline": timeline,
            }
            rows.append({"carrier": "shared_sweep", "variant": variant,
                         "observed": total, "status": "measured",
                         "ack_source": "fixture-ack via sweep.ack_delivered",
                         "seams": []})

            rows.append(_measure_rewake(source_root, variant))
            rows.append(_measure_codex_queue(source_root, variant))

    output.parent.mkdir(parents=True, exist_ok=True)
    time.monotonic_ns = real_monotonic_ns
    output.write_text(json.dumps({"schema_version": 1, "seed": SEED,
                                  "source_root": str(source_root), "measurements": rows},
                                 indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(rows, sort_keys=True))
    return 0


def _fixed_instruction_length(body: str) -> int:
    if not body:
        return 0
    # Count only the fixed shared completion header and deduplicated follow-up,
    # excluding per-record receipt lines and markers.
    import dispatch_session_sweep as sweep
    return len(sweep.COMPLETION_DELIVERY_HEADER) + sum(
        len(line) for line in body.splitlines()
        if line.startswith("Original request/format apply.")
        or line.startswith("Required action:")
    )


def _carrier_fixed_instruction_length(carrier: str, body: str) -> int:
    """Count shared instruction text, excluding per-delivery receipt content."""
    lines = body.splitlines()
    if carrier == "claude_async_rewake":
        # The test's classified receipt is a per-attempt receipt line; the
        # remaining text is only the per-record native receipt marker.
        return sum(len(line) for line in lines
                   if not line.startswith(("RECEIPT ", "HEARTING_RECEIPT_V1 ")))
    if carrier == "codex_native_queue":
        return sum(len(line) for line in lines
                   if line.startswith(("Completion bookkeeping for the listed attempts:",
                                       "The commands use the shared, runtime-neutral")))
    raise ValueError(f"unknown delivery carrier: {carrier}")


def _measurement_row(carrier: str, variant: str, timeline: list[dict], records: dict,
                     pending, root: Path, recipient: str) -> dict:
    counts = {delivery_id: sum(
        1 for item in timeline for transition in item["ack_transitions"]
        if transition["delivery_id"] == delivery_id and transition["to"] == "acked")
        for delivery_id in records}
    unresolved = [delivery_id for delivery_id in records
                  if pending.read(root, recipient, delivery_id)["state"] != "acked"]
    return {"carrier": carrier, "variant": variant, "status": "measured", "observed": {
        "wakes": sum(item["wakes"] for item in timeline),
        "emissions": sum(item["emissions"] for item in timeline),
        "injected_characters": sum(item["injected_characters"] for item in timeline),
        "fixed_instruction_characters": sum(item["fixed_instruction_characters"] for item in timeline),
        "ack_transition_count_by_record": counts,
        "unsettled_record_ids": unresolved,
        "failure_delivery_completion": {
            item["attempt_id"]: item["completion"] for item in timeline
            if item["failed"] and item["wakes"]},
        "timeline": timeline,
    }}


def _write_sibling_route(root: Path, variant: str, index: int) -> str:
    from route_identity import route_hash
    route_id = f"rt-measure-{variant}-{index:02d}"
    route = {"route_id": route_id, "parent_cycle_id": f"cycle-measure-{variant}",
             "nodes": [{"id": "execute"}]}
    route["route_hash"] = route_hash(route)
    (root / f"{route_id}.json").write_text(json.dumps(route, sort_keys=True), encoding="utf-8")
    return route_id


def _carrier_scenario(root: Path, variant: str, recipient_kind: str,
                      carrier: str):
    """Build the same route-bound sibling registry for a native carrier."""
    import dispatch_pending_delivery as pending
    from route_identity import route_hash

    recipient = "ses-measurement-parent"
    jobs = root / "jobs.log"
    records, members = {}, []
    cycle = f"cycle-measure-{variant}"
    raw_rows = []
    for index in range(SIBLINGS):
        attempt = f"att-measure-{index:032x}"
        delivery_id = f"delivery-measure-{variant}-{index:02d}"
        route_id = f"rt-measure-{variant}-{index:02d}"
        route = {"route_id": route_id, "parent_cycle_id": cycle,
                 "nodes": [{"id": "execute"}]}
        route["route_hash"] = route_hash(route)
        route_file = root / f"{route_id}.json"
        route_file.write_text(json.dumps(route, sort_keys=True), encoding="utf-8")
        failed = variant == "two_failures" and index in FAILURE_INDICES
        classification = "attention" if failed else "success"
        receipt = {"schema_version": 2, "state": "ready",
                   "parent_attempt_id": PARENT, "job_registry": str(jobs),
                   "parent_cycle_id": cycle, "delivery_classification": classification,
                   "children": [{"attempt_id": attempt, "status": "done", "readiness": "ready",
                                 "reason": "terminal-failure-or-unclosed" if failed else "terminal-complete",
                                 "required_action": "inspect-done-failure" if failed else "advance-completed",
                                 "harness": "claude" if carrier == "claude_async_rewake" else "codex",
                                 "delivery_classification": classification}]}
        row_revision = f"revision-{index:02d}"
        fields = ["2026-10-23T00:00:00Z", "running", str(root), str(root), "execute", ""]
        metadata = {
            "attempt_id": attempt, "dispatch_depth": "1", "registered_worker": "1",
            "worker_type": "execute", "parent_sid": recipient,
            "parent_attempt_id": PARENT, "parent_harness": "claude",
            "route_file": str(route_file), "route_id": route_id,
            "route_hash": route["route_hash"], "route_node": "execute",
            "delivery_intent": "1", "delivery_intent_at_ns": str(SEED * 1_000_000_000),
            "failure_class": "pass", "note": "completed-marker",
            "delivery_id": delivery_id, "parent_completion_delivery": recipient_kind,
            "delivery_recipient_kind": recipient_kind,
            "delivery_row_revision": row_revision,
            "delivery_receipt_digest": pending._canonical_receipt_digest(receipt),
        }
        if carrier == "codex_native_queue":
            metadata.update({"attempt_schema_version": "2",
                             "execution_surface": "registered-headless",
                             "launch_claimed": "1", "harness": "codex"})
        fields[5] = ",".join(f"{key}={value}" for key, value in metadata.items())
        raw_rows.append(fields)
        record = pending.create(
            root, recipient_kind=recipient_kind, recipient_key=recipient,
            delivery_id=delivery_id, session_generation="",
            session_generation_supported="0", attempt_ids=[attempt],
            parent_attempt_id=PARENT, route_id=route_id, route_node="execute",
            receipt=receipt, receipt_digest=metadata["delivery_receipt_digest"],
            row_revisions={attempt: row_revision})
        records[delivery_id] = record
        members.append({"attempt_id": attempt, "delivery_id": delivery_id,
                        "failed": failed, "receipt": receipt, "row": fields})
    jobs.write_text("\n".join("\t".join(row) for row in raw_rows) + "\n", encoding="utf-8")
    return jobs, recipient, records, members


def _terminalize(jobs: Path, member: dict, completion: int):
    """Move one real registry row over its terminal edge in the fake timeline."""
    from dispatch_contract import parse_registry_metadata
    rows = jobs.read_text(encoding="utf-8").splitlines()
    fields = rows[completion - 1].split("\t")
    fields[1] = "done"
    fields[0] = f"2026-10-23T00:00:{completion - 1:02d}Z"
    metadata = parse_registry_metadata(fields[5])
    metadata["failure_class"] = "fail" if member["failed"] else "pass"
    metadata["note"] = "dead-worker-fail" if member["failed"] else "completed-marker"
    fields[5] = ",".join(f"{key}={value}" for key, value in metadata.items())
    rows[completion - 1] = "\t".join(fields)
    jobs.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _measure_rewake(source_root: Path, variant: str) -> dict:
    import dispatch_pending_delivery as pending
    import dispatch_recipient_receipt as recipient_receipt

    spec = importlib.util.spec_from_file_location(
        "dispatch_owner_rewake_fixture", source_root / "hooks" / "dispatch-owner-rewake.py")
    rewake = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = rewake
    spec.loader.exec_module(rewake)
    recipient = "ses-measurement-parent"
    with tempfile.TemporaryDirectory(prefix="delivery-rewake-fixture-") as temporary:
        root = Path(temporary)
        jobs, recipient, records, members = _carrier_scenario(
            root, variant, "claude-parent-runtime", "claude_async_rewake")
        timeline, held = [], []
        known = {member["delivery_id"]: member["failed"] for member in members}
        for index, member in enumerate(members):
            attempt, delivery_id, failed = (member["attempt_id"], member["delivery_id"],
                                            member["failed"])
            _terminalize(jobs, member, index + 1)
            candidates = [member] + list(held)
            held = []
            before = {key: pending.read(root, recipient, key)["state"] for key in records}
            completion_texts = []
            wakes = chars = fixed = 0
            ack_sources = {}
            for candidate in candidates:
                candidate_id = candidate["delivery_id"]
                candidate_attempt = candidate["attempt_id"]
                candidate_launch = rewake.Launch(attempt_id=candidate_attempt, jobs=jobs,
                                                  session_id=recipient, armed="registry")
                metadata = {"delivery_id": candidate_id, "parent_sid": recipient}
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(rewake, "_incarnation_binding_matches", return_value=True), \
                     mock.patch.object(rewake, "classified_receipt", side_effect=lambda l, st, r, _root:
                                       ("attention" if known[candidate_id] else "success",
                                        f"RECEIPT {l.attempt_id} state={'attention' if known[candidate_id] else 'success'}")), \
                     mock.patch.object(rewake, "_gate_notices", return_value=[]), \
                     mock.patch.object(rewake, "settle_arm", return_value=True), \
                     mock.patch.object(rewake, "_ended", side_effect=lambda _claim, code, **_kw: code), \
                     mock.patch.object(rewake.sys, "stdout", stdout), \
                     mock.patch.object(rewake.sys, "stderr", stderr):
                    win = rewake._carrier_one_claim(candidate_launch, metadata)
                    if isinstance(win, getattr(rewake, "ClaimHeld", ())):
                        held.append(candidate)
                        continue
                    if win is None:
                        continue
                    message = f"RECEIPT {candidate_attempt} state={'attention' if known[candidate_id] else 'success'}"
                    if getattr(win, "members", ()) and hasattr(rewake, "_batch_message"):
                        message = rewake._batch_message(candidate_launch, win, message)
                    else:
                        from dispatch_recipient_receipt import receipt_marker
                        claimed_record = pending.read(root, recipient, win.delivery_id)
                        message += "\n" + receipt_marker(claimed_record, recipient)
                    code = rewake._emit_with_gates(candidate_launch, mock.sentinel.arm,
                                                  "attention" if known[candidate_id] else "success",
                                                  message, block=False, owner_closed=True)
                    output = stderr.getvalue() or stdout.getvalue()
                    if code == 2:
                        completion_texts.append(output)
                        wakes += 1
                        chars += len(output)
                        fixed += _carrier_fixed_instruction_length("claude_async_rewake", output)
                        win_members = getattr(win, "members", ())
                        for member_id, claimed_record in (win_members if win_members
                                                          else ((win.delivery_id,
                                                                 pending.read(root, recipient,
                                                                              win.delivery_id)),)):
                            try:
                                pending.mark_sent_ambiguous(root, recipient, member_id,
                                                            claim_owner=win.claim_owner)
                                current = pending.read(root, recipient, member_id)
                                marker = recipient_receipt.receipt_marker(current, recipient)
                                notification_body = "<task-notification><task-id>fixture</task-id>" \
                                    "<summary>Stop hook feedback</summary>" + output
                                if marker not in notification_body:
                                    notification_body += "\n" + marker
                                event = {"type": "user", "sessionId": recipient,
                                         "origin": {"kind": "task-notification",
                                                    "producer": "session-task"},
                                         "promptSource": "system", "turnOrigin": "task_notification",
                                         "message": {"content": notification_body +
                                                      "</task-notification>"}}
                                transcript = root / f"transcript-{member_id}.jsonl"
                                transcript.write_text(json.dumps(event) + "\n", encoding="utf-8")
                                transcript_events = [json.loads(line) for line in
                                                     transcript.read_text(encoding="utf-8").splitlines()]
                                evidence = recipient_receipt.claude_feedback_evidence(
                                    transcript_events, recipient)
                                settled = recipient_receipt.settle_native_receipt(
                                    root, recipient, "claude", recipient, current, evidence,
                                    acked_by="claude-transcript:" + recipient)
                                if settled["settled"]:
                                    ack_sources[member_id] = "claude-task-notification-transcript"
                                else:
                                    # Frozen pre-batching Claude has no native marker path;
                                    # preserve its singleton before/after ACK observation.
                                    pending.ack(root, recipient, member_id,
                                                acked_by="fixture-ack-legacy-claude")
                                    ack_sources[member_id] = "fixture-ack"
                            except pending.PendingDeliveryError:
                                pass
            after = {key: pending.read(root, recipient, key)["state"] for key in records}
            transitions = [{"delivery_id": key, "from": before[key], "to": value}
                           for key, value in after.items() if before[key] != value]
            body = "\n".join(completion_texts)
            timeline.append({"completion": index + 1, "attempt_id": attempt, "failed": failed,
                             "wakes": wakes, "emissions": wakes, "injected_characters": chars,
                             "fixed_instruction_characters": fixed,
                             "ack_transitions": transitions,
                             "ack_sources": ack_sources,
                             "delivered_ids": [t["delivery_id"] for t in transitions if t["to"] == "acked"],
                             "captured_text": body})
        result = _measurement_row("claude_async_rewake", variant, timeline, records,
                                  pending, root, recipient)
        result["seams"] = [
            "_incarnation_binding_matches: fixture supplies the already-registered native session binding; it does not select siblings.",
            "classified_receipt: fixture supplies captured receipt wording; claim_ready_batch and resolve_delivery_batch read the real registry and routes.",
            "_gate_notices: suppresses unrelated UI gates after the completion claim; it does not affect batch membership.",
            "settle_arm: avoids a live arm-ledger write; it runs after the batch claim and does not affect sibling selection.",
            "_ended: captures exit status without ending a real hook process; it runs after claim/render.",
            "stdout/stderr: captures emitted text and wake count; transport observation only.",
        ]
        result["ack_source"] = "claude task-notification settlement; fixture-ack only when the frozen baseline has no marker binding"
        return result


def _measure_codex_queue(source_root: Path, variant: str) -> dict:
    import dispatch_pending_delivery as pending

    spec = importlib.util.spec_from_file_location(
        "codex_managed_completion_fixture", source_root / "utilities" / "codex-managed-completion.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    recipient = "ses-measurement-parent"
    with tempfile.TemporaryDirectory(prefix="delivery-codex-queue-fixture-") as temporary:
        root = Path(temporary)
        jobs, recipient, records, members = _carrier_scenario(
            root, variant, "codex-native-queue", "codex_native_queue")

        sent, timeline, held = [], [], []
        def send(_path, **kwargs):
            sent.append(kwargs)
            return {"status": "consumed", "turn_id": f"turn-{len(sent)}", "turn_status": "completed"}

        for index, member in enumerate(members):
            attempt, delivery_id, receipt, failed = (member["attempt_id"], member["delivery_id"],
                                                       member["receipt"], member["failed"])
            _terminalize(jobs, member, index + 1)
            before = {key: pending.read(root, recipient, key)["state"] for key in records}
            args = SimpleNamespace(queue_socket=Path("/tmp/app-server.sock"), thread_id=recipient,
                                   jobs=jobs, timeout=2.0, delivery_retry_interval=0.01)
            candidates = [member] + list(held)
            held = []
            sends_before = len(sent)
            for candidate in candidates:
                candidate_attempt, candidate_id, candidate_receipt = (
                    candidate["attempt_id"], candidate["delivery_id"], candidate["receipt"])
                with mock.patch.object(module.codex_queue_delivery, "_rpc", return_value={
                        "thread": {"id": recipient, "cwd": "/tmp/repo"}}), \
                     mock.patch.object(module.codex_queue_delivery, "send_at_least_once", side_effect=send):
                    try:
                        result = module._native_queue_delivery_once(
                            args, candidate_receipt, {candidate_attempt})
                    except pending.PendingDeliveryError as exc:
                        if exc.detail == "batch-hold":
                            held.append(candidate)
                            continue
                        raise
                if result.get("status") == "sent-ambiguous" and result.get("reason") == "native-queue-batch-held":
                    held.append(candidate)
            after = {key: pending.read(root, recipient, key)["state"] for key in records}
            transitions = [{"delivery_id": key, "from": before[key], "to": value}
                           for key, value in after.items() if before[key] != value]
            new_sends = sent[sends_before:]
            relevant = "\n".join(item["message"] for item in new_sends)
            timeline.append({"completion": index + 1, "attempt_id": attempt, "failed": failed,
                             "wakes": len(new_sends), "emissions": len(new_sends),
                             "injected_characters": len(relevant),
                             "fixed_instruction_characters": _carrier_fixed_instruction_length(
                                 "codex_native_queue", relevant),
                             "ack_transitions": transitions,
                             "ack_sources": {t["delivery_id"]: "codex-consumed-evidence"
                                             for t in transitions if t["to"] == "acked"},
                             "delivered_ids": [t["delivery_id"] for t in transitions if t["to"] == "acked"],
                             "captured_text": relevant})
        result = _measurement_row("codex_native_queue", variant, timeline, records,
                                  pending, root, recipient)
        result["seams"] = [
            "codex_queue_delivery._rpc: returns the requested temporary recipient thread identity; network transport only.",
            "codex_queue_delivery.send_at_least_once: captures the queue item and returns a consumed turn; transport only; production codex_consumed_evidence and settle_native_receipt process it.",
        ]
        result["ack_source"] = "codex_consumed_evidence + settle_native_receipt"
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.source_root is None or args.output is None:
        parser.error("--source-root and --output are required")
    if args.child:
        return _child(args.source_root, args.output)

    source_root = args.source_root.resolve()
    with tempfile.TemporaryDirectory(prefix="delivery-batch-runtime-") as temporary:
        temp = Path(temporary)
        env = os.environ.copy()
        for name in ("HERDR_SOCKET_PATH", "HERDR_PANE_ID", "HERDR_TAB_ID",
                     "HERDR_WORKSPACE_ID", "HERDR_ENV"):
            env.pop(name, None)
        env.update({"HOME": str(temp / "home"), "XDG_CONFIG_HOME": str(temp / "xdg-config"),
                    "XDG_STATE_HOME": str(temp / "xdg-state"), "PYTHONDONTWRITEBYTECODE": "1"})
        Path(env["HOME"]).mkdir()
        Path(env["XDG_CONFIG_HOME"]).mkdir()
        Path(env["XDG_STATE_HOME"]).mkdir()
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                 "--child", "--source-root", str(source_root),
                                 "--output", str(args.output.resolve())],
                                cwd=source_root, env=env, text=True, capture_output=True)
        if result.stdout:
            print(result.stdout, end="")
        if result.returncode:
            print(result.stderr, file=sys.stderr, end="")
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
