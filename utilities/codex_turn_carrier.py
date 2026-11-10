"""Codex in-turn carrier: owed Hearting records at PostToolUse/Stop, one follow-through pass.

Claude keeps a finished or reporting job in front of the session with two native
surfaces: an ``asyncRewake`` hook that wakes the session with the receipt, and
synchronous ``additionalContext`` at the next prompt or tool boundary. Codex
(0.163, measured 2026-11-10 in an isolated home) offers the same meaning through
its own documented hook outputs:

* ``PostToolUse`` ``hookSpecificOutput.additionalContext`` reaches the model as
  developer context inside the running turn;
* ``Stop`` ``{"decision": "block", "reason": ...}`` does not reject the turn: Codex
  continues the same turn with ``reason`` as a new prompt, and runs Stop again
  with ``stop_hook_active=true``.

This module uses only those surfaces and the existing shared delivery path
(``dispatch_session_sweep.sweep_deliver`` / ``delivery_context`` /
``retain_receiving_turn`` / ``ack_delivered``), the same one the Codex
UserPromptSubmit bridge already uses for queued receipts:

1. **Owed records in the turn.** A completion receipt or session notice
   (including a due steward check) that is pending for this exact thread is
   rendered at the next tool boundary, or at Stop, instead of waiting for the
   turn to end and the native queue to start another one. Only records that the
   bridge may ACK are rendered, and each one at most once per turn, so a Stop
   continuation always ends: an ACKed record is gone, an unACKable one is left
   for the native queue and the next prompt exactly as before.
2. **One follow-through pass.** When this turn received a Hearting record whose
   required action is not plain reading (or a peer report), and the model ended
   the turn without running any tool after it, Stop continues the turn once with
   that required action. The model may still end with a one-line reason; there
   is no second pass, approval, or required input.

Everything is fail-open: any error leaves the hook silent and the records on
disk for their existing carriers.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

RECIPIENT_KIND = "codex-native-queue"
STATE_DIR = "codex-turn-carrier"
# The same prefix the prompt bridge uses: these are native receiving boundaries
# of the same exact Codex turn, and the existing follow-through recovery
# (peer_steward_native, dispatch_completion_join) keys on it.
ACKED_BY_PREFIX = "codex-native-prompt:"
MAX_REASON_CHARS = 8000
# A PostToolUse hook runs after every tool call; reading past this leaves the
# rest on disk for the next boundary.
POST_TOOL_BUDGET_SECONDS = 1.5
STOP_BUDGET_SECONDS = 6.0
STATE_MAX_AGE_SECONDS = 7 * 86400
NON_ACTION_CHILD = frozenset({"advance-completed"})
NON_ACTION_NOTICE = frozenset({"read-notice"})


def is_worker() -> bool:
    env = os.environ
    return (env.get("AGENT_SESSION_ROLE", "").lower() == "worker"
            or env.get("AGENT_DISPATCH_CHILD") == "1"
            or bool(env.get("AGENT_DISPATCH_DEPTH"))
            or bool(env.get("OPENCODE_DISPATCH_SLUG"))
            or env.get("FLEET_TITLE_REFRESH") == "1"
            or env.get("MEM_DISTILL") == "1")


def _first(value: Any, *keys: str) -> str:
    if not isinstance(value, dict):
        return ""
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
    for nested in ("context", "workspace", "session", "payload", "event", "input", "data"):
        item = _first(value.get(nested), *keys)
        if item:
            return item
    return ""


def session_id(payload: dict) -> str:
    return _first(payload, "session_id", "sessionID", "thread_id", "threadID")


def turn_id(payload: dict) -> str:
    return _first(payload, "turn_id", "turnID")


def _roots() -> list[Path]:
    from dispatch_contract import dispatch_state_roots, resolve_agent_home
    return list(dict.fromkeys(dispatch_state_roots(resolve_agent_home())))


# --- per-turn state -------------------------------------------------------

def _state_path(sid: str, root: Path | None = None) -> Path:
    base = root if root is not None else _roots()[0]
    digest = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:32]
    return Path(base) / STATE_DIR / f"{digest}.json"


@contextlib.contextmanager
def _locked_state(sid: str, tid: str, root: Path | None = None):
    """Yield this turn's mutable state under an exclusive lock, then save it."""
    path = _state_path(sid, root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict) or state.get("turn_id") != tid:
            state = {"turn_id": tid, "session_id": sid, "tools": 0,
                     "tools_after_receipt": 0, "owed": [], "rendered": [],
                     "nudged": False}
        yield state
        state["updated"] = time.time()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        os.close(fd)


def read_state(sid: str, tid: str, root: Path | None = None) -> dict:
    try:
        state = json.loads(_state_path(sid, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) and state.get("turn_id") == tid else {}


def note_tool(sid: str, tid: str, root: Path | None = None) -> None:
    if not sid or not tid:
        return
    with _locked_state(sid, tid, root) as state:
        state["tools"] = int(state.get("tools") or 0) + 1
        state["tools_after_receipt"] = int(state.get("tools_after_receipt") or 0) + 1


def note_received(sid: str, tid: str, items: list[dict], *, rendered: list[str] = (),
                  root: Path | None = None) -> None:
    """Record what this turn received; actionable items reset the tool count."""
    if not sid or not tid:
        return
    with _locked_state(sid, tid, root) as state:
        known = set(state.get("rendered") or [])
        state["rendered"] = sorted(known | {str(item) for item in rendered})
        owed = list(state.get("owed") or [])
        for item in items:
            if item not in owed:
                owed.append(item)
        state["owed"] = owed[-12:]
        if items:
            state["tools_after_receipt"] = 0


def mark_nudged(sid: str, tid: str, root: Path | None = None) -> bool:
    """Claim the turn's one follow-through pass; False when already spent."""
    with _locked_state(sid, tid, root) as state:
        if state.get("nudged"):
            return False
        state["nudged"] = True
        return True


def prune_states(root: Path | None = None, now: float | None = None) -> None:
    now = time.time() if now is None else now
    try:
        directory = _state_path("x", root).parent
        for path in directory.glob("*.json"):
            with contextlib.suppress(OSError):
                if now - path.stat().st_mtime > STATE_MAX_AGE_SECONDS:
                    path.unlink()
                    Path(str(path) + ".lock").unlink(missing_ok=True)
    except OSError:
        pass


# --- what a received record still asks for --------------------------------

def owed_items(records: list[dict]) -> list[dict]:
    """The required actions in delivered records that are more than reading."""
    items: list[dict] = []
    for record in records:
        receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
        delivery_id = str(record.get("delivery_id") or "")
        children = receipt.get("children")
        if isinstance(children, list):
            for child in children:
                if not isinstance(child, dict):
                    continue
                action = str(child.get("required_action") or "")
                if action and action not in NON_ACTION_CHILD:
                    items.append({"kind": "completion", "delivery_id": delivery_id,
                                  "attempt_id": str(child.get("attempt_id") or "-"),
                                  "required_action": action})
            continue
        action = str(receipt.get("required_action") or "")
        if action and action not in NON_ACTION_NOTICE:
            items.append({"kind": str(receipt.get("subject") or receipt.get("kind") or "notice"),
                          "delivery_id": delivery_id, "required_action": action})
    return items


def peer_report_item(prompt: str) -> dict | None:
    """A peer or runtime report that arrived as this turn's prompt."""
    if not prompt or ("peer-from:" not in prompt and "runtime-from:" not in prompt):
        return None
    return {"kind": "peer-report", "required_action": "follow-through"}


def nudge_text(items: list[dict]) -> str:
    lines = []
    for item in items[:6]:
        if item.get("kind") == "completion":
            lines.append(f"- completion attempt={item.get('attempt_id')} "
                         f"required_action={item.get('required_action')}")
        elif item.get("kind") == "peer-report":
            lines.append("- another session's report arrived as this turn's prompt")
        else:
            lines.append(f"- {item.get('kind')} required_action={item.get('required_action')}")
    return "\n".join([
        "[hearting] This turn received work that still names a next action, and no tool ran after it:",
        *lines,
        "Do that action now within the user's already-approved scope (the receipt above has the "
        "exact command), or, if nothing is owed, end with one line saying why. This is the only "
        "continuation for this turn.",
    ])


# --- owed records ----------------------------------------------------------

def native_receiving_turn(sid: str, tid: str) -> dict | None:
    """The exact running native turn, the binding a completion ACK needs."""
    if not sid or not tid:
        return None
    try:
        from codex_queue_dispatch import resolve_queue_socket
        import codex_queue_delivery
        turn = codex_queue_delivery.read_exact_turn(resolve_queue_socket(), sid, tid, timeout=2.0)
    except (OSError, ValueError, RuntimeError):
        return None
    except Exception:  # noqa: BLE001 - an absent App Server leaves completions to the queue
        return None
    if (not isinstance(turn, dict) or turn.get("id") != tid
            or turn.get("status") not in {"inProgress", "in_progress", "running"}):
        return None
    return {"thread_id": sid, "turn_id": tid, "status": turn["status"]}


def collect(sid: str, tid: str, *, budget_seconds: float, skip: set[str] = frozenset(),
            request_context: str | None = None):
    """Claim this thread's owed records; return ``(text, ackable_batches)``.

    Records the bridge cannot ACK (a completion without an exact running native
    turn) are not rendered here: their existing queue and prompt carriers keep
    them, so a Stop continuation never repeats one.
    """
    from dispatch_notice_state import NoticeReadContext
    from dispatch_session_sweep import (addressed_records, delivery_context,
                                        retain_receiving_turn, sweep_deliver)
    deadline_ns = time.monotonic_ns() + int(budget_seconds * 1_000_000_000)
    notice_context = NoticeReadContext()
    batches = []
    receiving: dict | None = None
    receiving_read = False
    for root in _roots():
        if time.monotonic_ns() >= deadline_ns:
            break
        # Read before claiming: a completion needs the exact running native turn
        # to be ACKed, and claiming one this bridge cannot ACK would hold the
        # native queue courier off. Such a root is left wholly to its existing
        # queue and prompt carriers.
        addressed, _count = addressed_records(root, RECIPIENT_KIND, sid, deadline_ns=deadline_ns)
        fresh = [record for record in addressed if record.get("delivery_id") not in skip]
        if not fresh:
            continue
        if any(isinstance((record.get("receipt") or {}).get("children"), list) for record in fresh):
            if not receiving_read:
                receiving, receiving_read = native_receiving_turn(sid, tid), True
            if receiving is None:
                continue
        records, _count = sweep_deliver(root, RECIPIENT_KIND, sid, notice_context=notice_context,
                                       deadline_ns=deadline_ns)
        records = [record for record in records if record.get("delivery_id") not in skip]
        if records:
            batches.append((root, records))
    if not batches:
        return "", []
    if not receiving_read:
        receiving = native_receiving_turn(sid, tid) if any(
            isinstance((record.get("receipt") or {}).get("children"), list)
            for _root, records in batches for record in records) else None
    ackable = retain_receiving_turn(batches, sid, receiving, request_context=request_context)
    kept, left = [], []
    for root, records in batches:
        exact = [record for record in records if (str(root), record.get("delivery_id")) in ackable]
        other = [record for record in records if (str(root), record.get("delivery_id")) not in ackable]
        if exact:
            kept.append((root, exact))
        if other:
            left.append((root, other))
    # A tool boundary comes far more often than a prompt: hand back at once the
    # claims this bridge will not ACK, so the native queue courier is not held
    # off for the claim lease while the turn keeps working.
    release(sid, left)
    if not kept:
        return "", []
    return delivery_context(kept), kept


def release(sid: str, batches) -> None:
    """Return this process's own sweep claims on records it did not ACK."""
    import dispatch_pending_delivery as pending
    from dispatch_session_sweep import STORAGE_KEY
    owner_prefix = f"session-sweep:{RECIPIENT_KIND}:{os.getpid()}:"
    for root, records in batches:
        for record in records:
            delivery_id = record.get("delivery_id")
            storage = record.get(STORAGE_KEY) or sid
            if not isinstance(delivery_id, str) or not delivery_id:
                continue
            try:
                fresh = pending.read(root, storage, delivery_id)
                owner = (fresh or {}).get("claim_owner")
                if (fresh is None or fresh.get("state") != "claimed"
                        or not isinstance(owner, str) or not owner.startswith(owner_prefix)):
                    continue
                # A distinct reason per release keeps the courier's retry delay
                # at its base instead of growing it on every tool boundary.
                pending.release_claim(
                    root, storage, delivery_id, claim_owner=owner,
                    failure_reason=f"codex-turn-carrier-unacked:{time.time_ns()}",
                    expected_identity=pending.received_ack_identity(fresh, storage, delivery_id))
            except Exception:  # noqa: BLE001 - an unreleased claim only waits for its lease
                continue


def acknowledge(sid: str, batches) -> list[str]:
    from dispatch_session_sweep import ack_delivered
    delivered = []
    for root, records in batches:
        ack_delivered(root, sid, records, acked_by=ACKED_BY_PREFIX + sid)
        delivered.extend(str(record.get("delivery_id")) for record in records)
    return delivered


def _all_records(batches) -> list[dict]:
    return [record for _root, records in batches for record in records]


def watch_receipts(sid: str, *, budget_seconds: float):
    """Un-acked steward watch receipts for this thread: the Claude prompt sweep's rows."""
    import importlib.util
    path = Path(__file__).resolve().parent / "peer-steward.py"
    previous = os.environ.get("CODEX_THREAD_ID")
    os.environ["CODEX_THREAD_ID"] = sid
    try:
        spec = importlib.util.spec_from_file_location("_codex_turn_steward", str(path))
        module = importlib.util.module_from_spec(spec)
        with contextlib.redirect_stdout(open(os.devnull, "w")):
            spec.loader.exec_module(module)
            # The hook knows the exact receiving thread from its own payload; the
            # steward's environment-based resolver is not asked to infer it.
            module._current_session_identity = lambda: (sid, "codex")
            rows = module.undelivered_watch_rows(deadline=time.monotonic() + budget_seconds)
        return module, [row for row in rows if isinstance(row, dict)][:5]
    except BaseException:  # noqa: BLE001 - a broken steward never costs the turn
        return None, []
    finally:
        if previous is None:
            os.environ.pop("CODEX_THREAD_ID", None)
        else:
            os.environ["CODEX_THREAD_ID"] = previous


def _watch_line(row: dict) -> str:
    agent = row.get("agent") if isinstance(row.get("agent"), dict) else {}
    return (f"state={row.get('receipt_state') or row.get('state') or 'unknown'} "
            f"agent={agent.get('harness', '-')} session_id={agent.get('session_id', '-')} "
            f"name={agent.get('name') or row.get('target', '-')} pane={agent.get('pane', '-')} "
            f"watch_id={row.get('watch_id', '-')} receipt={row.get('receipt', '-')}")


def ack_watches(module, sid: str, rows: list[dict]) -> None:
    previous = os.environ.get("CODEX_THREAD_ID")
    os.environ["CODEX_THREAD_ID"] = sid
    try:
        with contextlib.redirect_stdout(open(os.devnull, "w")):
            for row in rows:
                with contextlib.suppress(Exception, SystemExit):
                    module.main(["ack", str(row["watch_id"]), "--carrier", "codex-turn-carrier"])
    finally:
        if previous is None:
            os.environ.pop("CODEX_THREAD_ID", None)
        else:
            os.environ["CODEX_THREAD_ID"] = previous


# --- hook entry points ------------------------------------------------------

def _emit(obj: dict, stream) -> bool:
    try:
        stream.write(json.dumps(obj, ensure_ascii=False) + "\n")
        stream.flush()
        return True
    except (OSError, ValueError):
        return False


def post_tool(payload: dict, stream) -> int:
    """PostToolUse: count the tool, then hand in records owed since the last boundary."""
    sid, tid = session_id(payload), turn_id(payload)
    if is_worker() or not sid or not tid:
        return 0
    with contextlib.suppress(Exception):
        note_tool(sid, tid)
    try:
        state = read_state(sid, tid)
        text, batches = collect(sid, tid, budget_seconds=POST_TOOL_BUDGET_SECONDS,
                                skip=set(state.get("rendered") or []))
    except Exception:  # noqa: BLE001
        return 0
    if not text:
        return 0
    if _emit({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                     "additionalContext": text[:MAX_REASON_CHARS]}}, stream):
        with contextlib.suppress(Exception):
            delivered = acknowledge(sid, batches)
            note_received(sid, tid, owed_items(_all_records(batches)), rendered=delivered)
    else:
        with contextlib.suppress(Exception):
            release(sid, batches)
    return 0


def stop(payload: dict, stream) -> int:
    """Stop: continue the turn with owed records, or once with an unmet next action."""
    sid, tid = session_id(payload), turn_id(payload)
    if is_worker() or not sid or not tid:
        return 0
    with contextlib.suppress(Exception):
        prune_states()
    parts: list[str] = []
    batches = []
    module, watches = None, []
    try:
        state = read_state(sid, tid)
        text, batches = collect(sid, tid, budget_seconds=STOP_BUDGET_SECONDS,
                                skip=set(state.get("rendered") or []))
        if text:
            parts.append(text)
    except Exception:  # noqa: BLE001
        batches = []
    try:
        module, watches = watch_receipts(sid, budget_seconds=1.5)
        if watches:
            parts.append("\n".join(["[peer-steward] undelivered watch receipts:"]
                                   + [_watch_line(row) for row in watches]))
    except Exception:  # noqa: BLE001
        module, watches = None, []
    if parts:
        reason = "\n\n".join(["[hearting] Arrived before this turn ended:", *parts])
        if _emit({"decision": "block", "reason": reason[:MAX_REASON_CHARS]}, stream):
            with contextlib.suppress(Exception):
                delivered = acknowledge(sid, batches) if batches else []
                note_received(sid, tid, owed_items(_all_records(batches)), rendered=delivered)
            if module is not None and watches:
                with contextlib.suppress(Exception):
                    ack_watches(module, sid, watches)
        else:
            with contextlib.suppress(Exception):
                release(sid, batches)
        return 0
    try:
        state = read_state(sid, tid)
        owed = state.get("owed") or []
        if owed and int(state.get("tools_after_receipt") or 0) == 0 and not state.get("nudged"):
            if mark_nudged(sid, tid):
                _emit({"decision": "block", "reason": nudge_text(owed)}, stream)
    except Exception:  # noqa: BLE001
        pass
    return 0
