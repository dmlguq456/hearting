"""Peer steward: native receiving-turn resume and settled native receipts.

Split out of `peer-steward.py`, which re-exports every name defined here.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path


from peer_steward_base import (
    _interpret_payload, _UTILITIES_DIR,
)


_FLUSH_MAX_ROWS = 4
_FLUSH_ROW_TIMEOUT_S = 12
_FLUSH_STUCK_HOURS = 1.0


def _message_obligation_id(ref):
    return "message-" + ref


def native_agent(harness, session_id, *, pane=None, server=None):
    """Return the existing agent-info shape from one exact native session.

    A shell attachment is preferred because it binds native PID birth and SID.
    The process census fallback must yield exactly one matching runtime process.
    Missing, conflicting, or unreadable evidence is unknown, never absent.
    """
    if (harness not in {"claude", "codex", "opencode"}
            or not isinstance(session_id, str) or not session_id or session_id == "-"):
        return None
    try:
        import session_shell
        import peer_address
        binding, binding_reason = session_shell.exact_binding(harness, session_id)
        if binding_reason == "attachment-ambiguous":
            return None
        rows = peer_address.native_sessions()
        if binding is not None:
            pid = int(binding["native_pid"])
            birth = str(binding["native_start"])
            row = next((item for item in rows if item.get("pid") == pid
                        and item.get("harness") == harness and item.get("sid") == session_id), None)
            if row is None:
                return None
            pane = pane or row.get("env", {}).get("TMUX_PANE")
        else:
            matches = [item for item in rows if item.get("harness") == harness
                       and item.get("sid") == session_id]
            if len(matches) != 1:
                return None
            row = matches[0]
            pid = int(row["pid"])
            import pane_ownership
            proc = pane_ownership._process(pid)
            if not proc:
                return None
            birth = str(proc[5])
            pane = pane or row.get("env", {}).get("HERDR_PANE_ID") or row.get("env", {}).get("TMUX_PANE")
        import pane_ownership
        live = pane_ownership._process(pid)
        if not live or str(live[5]) != birth:
            return None
        namespace = os.readlink(f"/proc/{pid}/ns/pid")
        if namespace != os.readlink("/proc/self/ns/pid"):
            return None
        from terminal_surface import native_turn
        state = native_turn(harness, pid, session_id, pane=pane, server=server)
        name = None
        try:
            from peer_steward_base import _from_name
            name = _from_name(harness, session_id)
        except Exception:
            pass
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = None
        agent = {"agent": harness, "agent_status": state, "agent_session": {"value": session_id},
                 "name": name or session_id[:8], "pane_id": pane or "-", "pid": pid,
                 "pid_start": birth, "pid_namespace": namespace, "cwd": cwd}
        return {"result": {"type": "agent_info", "agent": agent}}
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, ImportError):
        return None


def end_observation():
    """A fresh per-pass reading of the live session holders for ``native_lifetime``.

    The reading is lazy and taken at most once per harness, so a pass that asks about
    many sessions scans the process table once instead of once per session. ``None``
    (collector unavailable) leaves each call to take its own reading.
    """
    try:
        tools = str(_UTILITIES_DIR.parent / "tools")
        if tools not in os.sys.path:
            os.sys.path.insert(0, tools)
        from fleet.collectors.steward import end_observation as fresh
        return fresh()
    except Exception:
        return None


def native_lifetime(harness, session_id, *, pid=None, pid_start=None, pid_namespace=None,
                    observation=None):
    """Return live/ended/unknown using exact process birth or complete native proof.

    ``observation`` is one pass's shared ``end_observation()``; without it the call
    takes its own reading.
    """
    if pid is not None and pid_start and pid_namespace:
        try:
            if pid_namespace != os.readlink("/proc/self/ns/pid"):
                return "unknown"
            from dispatch_contract import process_identity_disposition
            disposition = process_identity_disposition(int(pid), str(pid_start))
            return {"live": "live", "dead": "ended"}.get(disposition, "unknown")
        except (OSError, RuntimeError, TypeError, ValueError):
            return "unknown"
    if harness not in {"claude", "codex", "opencode"} or not session_id:
        return "unknown"
    try:
        tools = str(_UTILITIES_DIR.parent / "tools")
        if tools not in os.sys.path:
            os.sys.path.insert(0, tools)
        from fleet.collectors.steward import session_liveness
        options = {} if observation is None else {"observation": observation}
        state = session_liveness(harness, session_id, agents=(), **options)
        return state if state in {"live", "ended", "unknown"} else "unknown"
    except Exception:
        return "unknown"


def _resume_native_receiving_turn(duty, store, row, record, recipient_sid, storage_recipient):
    """Settle or resume one ACKed Codex hook turn through the existing queue."""
    from dispatch_completion_join import current_attempt_row
    acked_by = record.get("acked_by")
    if not (isinstance(acked_by, str) and acked_by.startswith((
            "codex-native-prompt:", "codex-native-queue:delivery-"))):
        return False
    observation = duty.get("observation") or {}
    jobs_path = Path((duty.get("intent") or {}).get("identity", {}).get("jobs", ""))
    binding = observation.get("native_receiving_turn")
    if not isinstance(binding, dict):
        store.update(duty["id"], state="unknown", delivery="pending",
                     observation={**observation, "reason": "native-receiving-turn-binding-unknown"})
        return True
    delivery_id = row.metadata.get("delivery_id", "")
    if (binding.get("thread_id") != recipient_sid
            or binding.get("recipient_sid") != recipient_sid
            or binding.get("storage_recipient") != storage_recipient
            or binding.get("delivery_id") != delivery_id
            or binding.get("receipt_digest") != row.metadata.get("delivery_receipt_digest")
            or binding.get("row_revision") != row.metadata.get("delivery_row_revision")):
        store.update(duty["id"], state="unknown", delivery="pending",
                     observation={**observation, "reason": "native-receiving-turn-identity-changed"})
        return True
    try:
        import dispatch_pending_delivery as pending
        from dispatch_completion_join import pending_record_identity
        root = jobs_path.resolve(strict=True).parent
        exact = pending.read(root, storage_recipient, delivery_id)
        if (exact is None or exact.get("state") != "acked"
                or exact.get("receipt_digest") != row.metadata.get("delivery_receipt_digest")
                or exact.get("row_revisions", {}).get(row.attempt_id)
                    != row.metadata.get("delivery_row_revision")):
            store.update(duty["id"], state="unknown", delivery="pending",
                         observation={**observation, "reason": "native-receipt-identity-unavailable"})
            return True
        route_id, route_node, parent_attempt_id = pending_record_identity(row.metadata, jobs_path)
        ack_identity = pending.received_ack_identity(exact, storage_recipient, delivery_id)
        if (ack_identity.get("route_id") != route_id
                or ack_identity.get("route_node") != route_node
                or ack_identity.get("parent_attempt_id") != parent_attempt_id):
            store.update(duty["id"], state="unknown", delivery="pending",
                         observation={**observation, "reason": "native-receipt-binding-conflict"})
            return True
        from codex_queue_dispatch import resolve_queue_socket
        import codex_queue_delivery
        endpoint = resolve_queue_socket()
        source_turn_id = observation.get("continuation_source_turn") or binding.get("turn_id")
        turn_id = source_turn_id
        try:
            source_turn = codex_queue_delivery.read_exact_turn(
                endpoint, recipient_sid, source_turn_id, timeout=5.0)
        except codex_queue_delivery.QueueDeliveryError:
            source_turn = None
        current_turn = source_turn
        continuation_id = observation.get("continuation_id")
        if continuation_id:
            continuation = codex_queue_delivery.find_turn_by_client_message_id(
                endpoint, recipient_sid, continuation_id, timeout=5.0)
            if continuation is not None:
                current_turn = continuation
                turn_id = continuation.get("id")
            elif any(item.get("clientUserMessageId") == continuation_id for item in
                     codex_queue_delivery.list_queue(endpoint, recipient_sid, timeout=5.0)):
                store.update(duty["id"], state="pending", delivery="native-continuation-accepted",
                             observation={**observation, "reason": "native-continuation-still-queued"})
                return True
        if current_turn is None:
            store.update(duty["id"], state="unknown", delivery="pending",
                         observation={**observation, "reason": "native-receiving-turn-unavailable"})
            return True
        status = current_turn["status"]
        if status in {"completed", "complete"}:
            store.update(duty["id"], state="complete", delivery="accepted",
                         observation={**observation, "receiving_turn_status": status})
            return True
        if status in {"inProgress", "in_progress", "running"}:
            store.update(duty["id"], state="pending", delivery="pending",
                         observation={**observation, "reason": "native-receiving-turn-active",
                                      "receiving_turn_status": status})
            return True
        if status != "interrupted":
            store.update(duty["id"], state="unknown", delivery="pending",
                         observation={**observation, "reason": "native-receiving-turn-status-unknown",
                                      "receiving_turn_status": status})
            return True
        latest = codex_queue_delivery._latest_turn(endpoint, recipient_sid, timeout=5.0)
        if not isinstance(latest, dict) or latest.get("id") != turn_id:
            store.update(duty["id"], state="unknown", delivery="pending",
                         observation={**observation, "reason": "native-receiving-turn-no-longer-current"})
            return True
        try:
            source_messages = _native_user_message_sources(source_turn)
        except codex_queue_delivery.QueueDeliveryError:
            source_messages = []
        receipt = exact.get("receipt")
        if not isinstance(receipt, dict):
            store.update(duty["id"], state="unknown", delivery="pending",
                         observation={**observation, "reason": "native-receipt-body-unavailable"})
            return True
        from dispatch_completion_join import completion_followup_text
        encoded = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        followup = completion_followup_text(
            receipt, jobs=str(jobs_path),
            surface=str(_UTILITIES_DIR.parent / "adapters/codex/bin/preflight.sh"))
        request_context = observation.get("native_request_context")
        original_request = None
        request_reference = None
        source_reason = "native-original-user-message-unavailable"
        source_record = None
        receiving_binding = observation.get("native_receiving_turn") or binding
        if (isinstance(request_context, dict)
                and request_context.get("source") == "codex-userprompt-submit"
                and request_context.get("status") == "available"
                and isinstance(request_context.get("text"), str)
                and request_context.get("text")
                and request_context.get("thread_id") == recipient_sid
                and request_context.get("turn_id") == receiving_binding.get("turn_id")
                and request_context.get("delivery_id") == delivery_id
                and request_context.get("receipt_digest")
                    == row.metadata.get("delivery_receipt_digest")
                and request_context.get("row_revision")
                    == row.metadata.get("delivery_row_revision")):
            original_request = request_context["text"]
            source_record = {"source": "codex-userprompt-submit", "status": "available",
                             "thread_id": recipient_sid,
                             "turn_id": receiving_binding.get("turn_id")}
            request_reference = (
                "\nOriginal user request source: read the exact JSON field "
                "observation.native_request_context.text in the existing durable obligation "
                f"file {store._record_path(duty['id']).resolve()} (duty {duty['id']}; "
                f"Codex thread {recipient_sid}, turn {receiving_binding.get('turn_id')}; "
                f"delivery {delivery_id}; receipt revision "
                f"{row.metadata.get('delivery_row_revision')}). Fulfill it verbatim, including "
                "all requested content and output-format requirements."
            )
        elif source_messages:
            source_record = {"source": "codex-native-userMessage", "status": "available",
                             "item_ids": [item["item_id"] for item in source_messages]}
        else:
            if (isinstance(request_context, dict)
                    and request_context.get("status") == "unavailable"
                    and request_context.get("thread_id") == recipient_sid
                    and request_context.get("turn_id") == receiving_binding.get("turn_id")
                    and request_context.get("delivery_id") == delivery_id
                    and request_context.get("receipt_digest")
                        == row.metadata.get("delivery_receipt_digest")
                    and request_context.get("row_revision")
                        == row.metadata.get("delivery_row_revision")):
                source_reason = request_context.get("reason", source_reason)
            elif isinstance(request_context, dict):
                source_reason = "hook-request-context-binding-unavailable"
            source_record = {"source": "unavailable", "status": "unavailable",
                             "reason": source_reason}
        message = _native_continuation_message(
            thread_id=recipient_sid, turn_id=turn_id, source_turn_id=source_turn_id,
            receipt_text=encoded,
            followup=followup, source_messages=source_messages,
            original_request=original_request,
            request_reference=request_reference,
            source_unavailable_reason=(source_reason if source_record["status"] == "unavailable"
                                       else None))
        stable = "delivery-" + hashlib.sha256((
            "codex-native-recovery-v1\0" + delivery_id + "\0" + turn_id + "\0"
            + str(row.metadata.get("delivery_receipt_digest", ""))
        ).encode("utf-8")).hexdigest()[:32]
        prepared_record, prepared = store.prepare_native_continuation(
            duty["id"], continuation_id=stable, source_turn_id=turn_id,
            source=source_record, message=message)
        observation = dict(prepared_record.get("observation") or {})
        stable = prepared["id"]
        message = prepared["message"]

        def before_add():
            fresh = current_attempt_row(jobs_path, row.attempt_id)
            from dispatch_seat_handover import effective_parent
            from dispatch_contract import terminal_conflict_pending
            fresh_receiving_turn = codex_queue_delivery.read_exact_turn(
                endpoint, recipient_sid, turn_id, timeout=5.0)
            newest = codex_queue_delivery._latest_turn(endpoint, recipient_sid, timeout=5.0)
            latest_record = pending.read(root, storage_recipient, delivery_id)
            if (fresh is None or fresh.status != row.status
                    or fresh.metadata.get("delivery_id") != delivery_id
                    or fresh.metadata.get("delivery_receipt_digest")
                        != row.metadata.get("delivery_receipt_digest")
                    or fresh.metadata.get("delivery_row_revision") != row.metadata.get("delivery_row_revision")
                    or effective_parent(fresh.metadata, jobs_path) != recipient_sid
                    or terminal_conflict_pending(fresh.metadata)
                    or pending_record_identity(fresh.metadata, jobs_path)
                        != (route_id, route_node, parent_attempt_id)
                    or not fresh_receiving_turn
                    or fresh_receiving_turn.get("status") != "interrupted"
                    or fresh_receiving_turn.get("id") != turn_id
                    or not isinstance(newest, dict) or newest.get("id") != turn_id
                    or not latest_record or latest_record.get("state") != "acked"
                    or pending.received_ack_identity(latest_record, storage_recipient, delivery_id)
                        != ack_identity):
                raise codex_queue_delivery.QueueDeliveryError("native-continuation-identity-changed")

        sent = codex_queue_delivery.send_at_least_once(
            endpoint, thread_id=recipient_sid, client_message_id=stable, message=message,
            timeout=5.0, require_message_match=True, before_add=before_add)
        if sent.get("status") in {"queued", "consumed"}:
            # Queue acceptance transfers transport, not the still-unfinished
            # receiving turn. Keep observing its exact native turn on this duty.
            store.update(duty["id"], state="pending", delivery="native-continuation-accepted",
                         observation={**observation, "reason": "native-continuation-awaiting-turn",
                                      "continuation_status": sent["status"]})
        else:
            store.update(duty["id"], state="unknown", delivery="pending",
                         observation={**observation, "reason": "native-continuation-unconfirmed"})
        return True
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        store.update(duty["id"], state="unknown", delivery="pending",
                     observation={**observation, "reason": "native-continuation-observation-unavailable",
                                  "detail": str(exc)[:160]})
        return True


_NATIVE_QUEUE_MESSAGE_LIMIT = 16 * 1024


def _native_user_message_sources(turn):
    """Return exact text userMessage items from one exact native turn."""
    import codex_queue_delivery

    items = turn.get("items") if isinstance(turn, dict) else None
    if not isinstance(items, list):
        raise codex_queue_delivery.QueueDeliveryError(
            "native-original-user-message-unavailable")
    sources = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "userMessage":
            continue
        item_id = item.get("id")
        content = item.get("content")
        if (not isinstance(item_id, str) or not item_id
                or not isinstance(content, list) or len(content) != 1
                or not isinstance(content[0], dict)
                or content[0].get("type") != "text"
                or not isinstance(content[0].get("text"), str)):
            raise codex_queue_delivery.QueueDeliveryError(
                "native-original-user-message-unavailable")
        sources.append({"item_id": item_id, "text": content[0]["text"]})
    if len({item["item_id"] for item in sources}) != len(sources):
        raise codex_queue_delivery.QueueDeliveryError(
            "native-original-user-message-unavailable")
    return sources


def _native_continuation_message(*, thread_id, turn_id, source_turn_id,
                                 receipt_text, followup, source_messages,
                                 original_request=None, request_reference=None,
                                 source_unavailable_reason=None):
    import codex_queue_delivery

    prefix = ("AGENT_HARNESS_COMPLETION_V1\n" + receipt_text + "\n" + followup
              + f"\nThe exact receiving turn {turn_id} was interrupted. Continue its approved follow-through "
                "and report once; check existing exact artifact and next-leg lineage before repeating "
                "any action. Preserve new failures, conflicts, and unfinished work.")
    if original_request is not None:
        source_header = (
            "\nOriginal user request received by Codex UserPromptSubmit, verbatim. "
            "Continue and fulfill that original task, preserving all requested content and output "
            "formats; this receipt is context and does not replace or narrow the task:\n")
        source_body = original_request
    elif source_messages:
        source_header = (
            "\nOriginal user request from the exact interrupted turn, verbatim. "
            "Continue and fulfill that original task, preserving all requested content and output formats; "
            "this receipt is context and does not replace or narrow the task:\n")
        source_body = "".join(
            f"\n[original userMessage item {item['item_id']}]\n{item['text']}"
            for item in source_messages)
    else:
        source_header = (
            "\nThe original user request could not be recovered from the Codex hook or exact turn "
            f"({source_unavailable_reason or 'source-unavailable'}). Continue the existing approved "
            "follow-through in this receipt and record any missing original-task context honestly.\n")
        source_body = ""
    full = prefix + source_header + source_body
    if len(full.encode("utf-8")) <= _NATIVE_QUEUE_MESSAGE_LIMIT:
        return full
    if original_request is not None and request_reference:
        bounded = prefix + request_reference
        if len(bounded.encode("utf-8")) <= _NATIVE_QUEUE_MESSAGE_LIMIT:
            return bounded
    references = ", ".join(item["item_id"] for item in source_messages)
    if not references:
        raise codex_queue_delivery.QueueDeliveryError(
            "native-continuation-message-over-limit")
    bounded = (prefix + f"\nOriginal task source: read the exact userMessage item(s) {references} "
               f"in Codex thread {thread_id}, turn {source_turn_id}. Fulfill the entire original request, "
               "including every requested content and output-format requirement. This receipt is only "
               "continuation context; it does not replace, narrow, or summarize the original task.")
    if len(bounded.encode("utf-8")) > _NATIVE_QUEUE_MESSAGE_LIMIT:
        raise codex_queue_delivery.QueueDeliveryError(
            "native-continuation-message-over-limit")
    return bounded


def _settle_claude_native_dispatch_receipt(root, storage_recipient, row,
                                          recipient_sid, record) -> bool:
    """Reuse only the exact Claude transcript bound to the registered cwd.

    The record is addressed to its registered parent (`storage_recipient`). When
    the effective recipient moved on (a `/clear` or resume gave a new SID) the
    registered parent's own transcript is still the place its exact receipt
    marker lives, so it is read too; a result the parent already took is not
    delivered a second time to its successor.
    """
    cwd = row.metadata.get("parent_cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return False
    sessions = [recipient_sid]
    if storage_recipient and storage_recipient != recipient_sid:
        from dispatch_seat_handover import _registered_parent_harness
        if _registered_parent_harness(row.metadata) == "claude":
            sessions.append(storage_recipient)
    runtime_home = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    try:
        from claude_project_dir import resolve_session_transcript
        from dispatch_recipient_receipt import (
            claude_transcript_evidence, legacy_claude_receipt_bindings,
            settle_native_receipt,
        )
        for session_id in sessions:
            transcript, _reason = resolve_session_transcript(
                session_id, cwd, expected_cwd=cwd, runtime_home=runtime_home)
            if transcript is None:
                continue
            evidence = claude_transcript_evidence(
                transcript_path=transcript, session_id=session_id, cwd=cwd,
                expected_cwd=cwd, runtime_home=runtime_home)
            if evidence is not None:
                from dispatch_session_sweep import addressed_records
                candidates, _count = addressed_records(
                    Path(root), record.get("recipient_kind", ""), session_id)
                legacy = legacy_claude_receipt_bindings(
                    root, record.get("recipient_kind", ""), session_id,
                    candidates, evidence)
                binding = legacy.get((storage_recipient, record.get("delivery_id")))
                if binding is not None:
                    evidence = {**evidence,
                                "bindings": [*(evidence.get("bindings") or []), binding]}
            outcome = settle_native_receipt(
                root, storage_recipient, "claude", session_id, record, evidence,
                acked_by=f"native-receipt:claude:{session_id}")
            if outcome.get("received"):
                return True
        return False
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def _closed_registered_route_outcome(row, jobs):
    """Read route closure only when the exact current row binding matches it."""
    if getattr(row, "status", None) != "done":
        return None
    metadata = row.metadata
    try:
        if (metadata.get("dispatch_depth") == "1"
                and metadata.get("worker_type") == "owner"):
            from owner_route_binding import OwnerRouteBinding, resolve_owner_route_lifecycle
            sealed = (metadata.get("owner_route_file"), metadata.get("owner_route_id"),
                      metadata.get("owner_route_hash"))
            if not all(sealed):
                return None
            binding, _status = resolve_owner_route_lifecycle(
                jobs, owner_attempt_id=row.attempt_id, sealed_binding=OwnerRouteBinding(*sealed))
            if binding is None:
                return None
            route_id, route_hash = binding.route_id, binding.route_hash
        else:
            route_id, route_hash = metadata.get("route_id"), metadata.get("route_hash")
        artifact_root = metadata.get("artifact_root")
        if (not isinstance(artifact_root, str) or not Path(artifact_root).is_absolute()
                or not route_id or not route_hash):
            return None
        from route_identity import route_id_from_hash
        if route_id_from_hash(route_hash) != route_id:
            return None
        import artifact_campaign
        import artifact_lifecycle
        root = Path(artifact_root).resolve()
        path = artifact_lifecycle.canonical_outcome_path(root, route_id)
        try:
            outcome, _raw = artifact_campaign.read_json(root, path)
        except artifact_campaign.CampaignError:
            return None
        closed_at = outcome.get("closed_at")
        if (outcome.get("route_id") != route_id or outcome.get("route_hash") != route_hash
                or not isinstance(closed_at, str) or not closed_at):
            return None
        datetime.datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
        return {"route_id": route_id, "route_hash": route_hash, "closed_at": closed_at}
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, ImportError):
        return None




def _target_from_readiness_view(view, target):
    """Compatibility parser for old in-memory views; new watches do not call it."""
    if view is None or not isinstance(target, str) or not target:
        return "unknown", {"harness": "-", "session_id": "-", "name": target or "-",
                            "pane": "-"}, 4, "target-census-unavailable"
    matches = []
    for agent in getattr(view, "agents", ()):
        sid = (agent.get("agent_session") or {}).get("value")
        if target in {agent.get("name"), sid, agent.get("pane_id")}:
            matches.append(agent)
    if len(matches) != 1:
        return "unknown", {"harness": "-", "session_id": "-", "name": target,
                            "pane": "-"}, 4, "target-census-ambiguous"
    return _interpret_payload({"result": {"agent": matches[0]}}, target)
