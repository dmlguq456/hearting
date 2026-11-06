"""Prepare an idle card in the existing memory job, before native compaction.

The booking, queue, card and ledger remain their existing stores. No native
prompt or prompt-arrival receipt is produced by this background operation.
"""
from __future__ import annotations

from pathlib import Path

import session_tidy as st


def enqueue(claim: dict) -> tuple[bool, str]:
    import session_tidy_clear as clear
    import session_tidy_runner as runner

    path = clear.reservation_path(claim["seat"]["key"])
    request = claim.get("idle_request") or {}
    checked, why = clear.validate_idle_request(path, claim["nonce"], _sending_claim=request)
    if checked is None:
        clear.release_unsent_idle_request(claim)
        return False, why
    seat = st.seat_from_dict(checked["seat"])
    with st.seat_lock(seat.key):
        checked, why = clear.recheck_idle_input(path, checked)
        if checked is None:
            return False, why
        operation = checked["idle"]["identity"]
        summary = st.session_summary(seat).get((checked["harness"], checked["sid"])) or {}
        item = runner.enqueue_item(seat, checked["harness"], checked["sid"], checked["cwd"],
                                   transcript=str(summary.get("transcript") or ""),
                                   operation_id=operation, origin="idle", start=False)
        item.setdefault("idle_card", {
            "nonce": checked["nonce"], "request_path": str(path),
            "source_prompt_seq": st.read_prompt_seq(seat),
            "source_card_generation": checked["card_generation"],
        })
        runner.write_item(item)
        checked.update(queue_id=item["id"], operation_id=operation, memory_state="queued")
        clear._write_reservation(checked)
    # A launch miss leaves the same ordinary queue row recoverable.
    runner._start_queued(item)
    return True, ""


def input_context(item: dict, sessions: list[dict]) -> dict | None:
    """Read card context without advancing the memory worker's watermarks."""
    import session_tidy_clear as clear
    import session_tidy_runner as runner
    import tidy_transcripts as transcripts

    context = item.get("idle_card")
    if not isinstance(context, dict):
        return None
    path = Path(context["request_path"])
    req = clear.read_reservation((item.get("seat") or {})["key"])
    if (not req or req.get("nonce") != context["nonce"] or req.get("status") != "reserved"
            or (req.get("idle_request") or {}).get("source") != "memory-helper"):
        return None
    checked, _why = clear.validate_idle_request(
        path, context["nonce"], _sending_claim=req.get("idle_request"))
    if checked is None:
        return None
    seat = runner._card_seat(item)
    card = st.read_latest_card(seat)
    if (st.read_prompt_seq(seat) != context["source_prompt_seq"]
            or int((card or {}).get("generation", 0)) != context["source_card_generation"]):
        return None
    previous = card or {}
    if previous.get("source_history"):
        expected = (st.state_root() / "card-history" / st.card_key(seat)
                    / f"{int(previous['generation']):08d}.json")
        if Path(previous["source_history"]) != expected:
            raise runner.RunnerFailure("previous card history binding changed")
        full = st.read_json(expected)
        if (not isinstance(full, dict) or full.get("generation") != previous["generation"]
                or full.get("author") != previous.get("author")):
            raise runner.RunnerFailure("previous card history is unreadable")
        previous = full
    source = item.get("transcript") or transcripts.locate_transcript(item["harness"], item["sid"])
    if not source:
        raise runner.RunnerFailure("idle card conversation is unavailable")
    # The existing reader extracts dialogue/choices, excluding tool output and
    # thinking. Card context is not restricted to the newest unread memory range.
    source_path = Path(source)
    size = source_path.stat().st_size
    if item["harness"] == "opencode":
        # A read-only SQLite snapshot may include pages still held in its WAL.
        try:
            size += Path(str(source_path) + "-wal").stat().st_size
        except FileNotFoundError:
            pass
    chunk = transcripts.read_chunk(item["harness"], source, cursor=0,
                                   sid=item["sid"], limit_bytes=max(1, size))
    if chunk.error:
        raise runner.RunnerFailure("idle card conversation: " + chunk.error)
    conversation = chunk.text
    tail = next((row["text"] for row in sessions
                 if (row["harness"], row["sid"]) == (item["harness"], item["sid"])), "")
    shared_tail = bool(tail and conversation.endswith(tail))
    if shared_tail:
        conversation = conversation[:-len(tail)]
    return {"previous_card": previous.get("body", ""), "conversation": conversation,
            "card_format": st.card_format.policy(),
            "append_caller_session_text": shared_tail, "coverage": transcripts.describe_coverage(chunk),
            "partial": bool(not chunk.eof or chunk.skipped_oversize), "source": str(source),
            "writing_guide": st.card_format.writing_guide(
                role=str(req.get("role") or ""), capability=str(req.get("capability") or ""),
                cwd=item["cwd"])}


def publish(item: dict, body: str) -> bool:
    """Save only the still-booked session's card; a late result stays memory-only."""
    import session_tidy_clear as clear
    import session_tidy_runner as runner
    from dispatch_seat_handover import LINEAGE_WRITE_KEY

    context = item["idle_card"]
    path = Path(context["request_path"])
    req, seat, _why = clear._booking_at(path)
    if not req or req.get("nonce") != context["nonce"]:
        return False
    if (req.get("idle_request") or {}).get("state") == "accepted":
        return bool(accepted(req, seat))
    checked, _why = clear.validate_idle_request(
        path, context["nonce"], _sending_claim=req.get("idle_request"))
    if checked is None:
        return False
    # All seat locks precede lineage, as in the existing lifecycle writers.
    with st.session_state_lock(seat), st.seat_lock(LINEAGE_WRITE_KEY):
        checked, _why = clear.recheck_idle_input(path, checked)
        if checked is None or clear._idle_lineage_reason(item["harness"], item["sid"]):
            return False
        observed = clear._card_frontier(seat)
        if observed is None:
            return False
        old_card, sequence = observed
        if (sequence != context["source_prompt_seq"]
                or int((old_card or {}).get("generation", 0)) != context["source_card_generation"]):
            return False
        generation = int((old_card or {}).get("generation", 0)) + 1
        st.record_event(seat, item["harness"], item["sid"], "card", cwd=item["cwd"],
                        card_generation=generation)
        card = st.write_card(seat, item["harness"], item["sid"], body, cwd=item["cwd"],
                             prompt_seq=sequence, origin="idle")
        item["card_generation"] = card["generation"]
        runner.write_item(item)
        acceptance = {"schema": 1, "source": "memory-helper", "nonce": context["nonce"],
            "operation_id": item["operation_id"], "queue_id": item["id"], "seat_key": seat.key,
            "harness": item["harness"], "sid": item["sid"], "prompt_seq": sequence,
            "source_generation": context["source_card_generation"], "card_generation": card["generation"]}
        checked.update(card_generation=card["generation"], prompt_seq=sequence,
            source_card_generation=context["source_card_generation"], acceptance=acceptance,
            snapshot_state="not-applicable", followup_kind="none", continue_off=True,
            deadline=st.now_epoch() + clear.COMPACT_CONFIRM_SEC)
        checked["idle_request"] = {**checked["idle_request"], "state": "accepted",
                                   "accepted_at": st.now_epoch(), "queue_id": item["id"]}
        clear._write_reservation(checked)
    return True


def accepted(req: dict, seat) -> bool | None:
    import session_tidy_clear as clear
    import session_tidy_runner as runner

    try:
        item = runner.read_item(str(req.get("queue_id") or ""))
        context = (item or {}).get("idle_card") or {}
        observed = clear._card_frontier(seat)
        if observed is None:
            return None
        card, sequence = observed
        expected = {"schema": 1, "source": "memory-helper", "nonce": req["nonce"],
            "operation_id": (item or {}).get("operation_id"), "queue_id": (item or {}).get("id"),
            "seat_key": seat.key, "harness": req["harness"], "sid": req["sid"],
            "prompt_seq": sequence, "source_generation": context.get("source_card_generation"),
            "card_generation": (card or {}).get("generation")}
        return bool(item and card and req.get("origin") == "idle" and req.get("status") == "reserved"
            and req.get("acceptance") == expected
            and req.get("operation_id") == item.get("operation_id")
            and (req.get("idle_request") or {}).get("state") == "accepted"
            and (item["harness"], item["sid"]) == (req["harness"], req["sid"])
            and (item.get("seat") or {}).get("key") == seat.key
            and context.get("nonce") == req["nonce"]
            and context.get("source_prompt_seq") == sequence == req.get("prompt_seq")
            and item.get("card_generation") == card.get("generation") == req.get("card_generation")
            and card.get("origin") == "idle" and card.get("prompt_seq") == sequence
            and (card.get("author") or {}).get("harness") == req["harness"]
            and (card.get("author") or {}).get("sid") == req["sid"])
    except OSError:
        return None
    except (KeyError, TypeError, ValueError):
        return False
