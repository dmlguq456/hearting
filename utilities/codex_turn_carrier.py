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
   bridge may ACK are rendered, each whole and at most once per turn, and only
   the records whose text fits the output budget whole are ACKed; the rest stay
   pending for the next boundary. A Stop continuation therefore always ends: an
   ACKed record is gone, an unACKable one (a completion without the exact
   running turn) is left unclaimed for the native queue and the next prompt
   exactly as before, and a notice beside it is still carried.
2. **One follow-through pass.** When this turn received a Hearting record whose
   required action is not plain reading (or a verified peer report addressed to
   this thread, not a quoted trailer), and the model ended
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
import re
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
# The answer to these is the turn's own final message to the person, so a turn
# that ends with no tool after them has done what they ask.
NON_ACTION_NOTICE = frozenset({"read-notice", "report-to-user", "report-capacity-wait",
                               "report-unresolved-verification"})
# Exact handles a receipt's own command names: a tool that runs one of them has
# acted on that record, an unrelated tool has not.
_HANDLE_RE = re.compile(r"\b(?:rt-[0-9a-f]{16}|att-[0-9a-f]{8,})\b")
SESSION_STATE_DIR = "codex-turn-carrier-session"
STOP_HEADER = "[hearting] Arrived before this turn ended:"
WATCH_HEADER = "[peer-steward] undelivered watch receipts:"


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


def tool_command(payload: dict) -> str:
    """The text of the tool call a PostToolUse payload reports ("" when absent)."""
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    if isinstance(tool_input, str):
        return tool_input
    if not isinstance(tool_input, dict):
        return ""
    value = tool_input.get("command") or tool_input.get("cmd") or ""
    if isinstance(value, list):
        value = " ".join(str(part) for part in value)
    return value if isinstance(value, str) else ""


def note_tool(sid: str, tid: str, root: Path | None = None, command: str = "") -> None:
    """Count one tool; an owed record with exact handles is met only by a tool naming one."""
    if not sid or not tid:
        return
    with _locked_state(sid, tid, root) as state:
        state["tools"] = int(state.get("tools") or 0) + 1
        owed = list(state.get("owed") or [])
        remaining = [item for item in owed
                     if not (isinstance(item, dict) and item.get("handles")
                             and any(handle in command for handle in item["handles"]))]
        if len(remaining) != len(owed):
            state["owed"] = remaining
        if not any(isinstance(item, dict) and item.get("handles") for item in remaining):
            # Records without an exact handle keep the earlier rule: any tool after them.
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


def unmet(state: dict) -> list:
    """Owed items the turn has not acted on: a handled item until a tool named it,
    the rest until any tool ran after them."""
    owed = [item for item in state.get("owed") or [] if isinstance(item, dict)]
    handled = [item for item in owed if item.get("handles")]
    if int(state.get("tools_after_receipt") or 0) == 0:
        return owed
    return handled


@contextlib.contextmanager
def _locked_session(sid: str, root: Path | None = None):
    """Session-wide carrier state (not reset per turn), under an exclusive lock."""
    base = root if root is not None else _roots()[0]
    digest = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:32]
    path = Path(base) / SESSION_STATE_DIR / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        yield state
        state["updated"] = time.time()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        os.close(fd)


def shown_unbound(sid: str, root: Path | None = None) -> set[str]:
    try:
        with _locked_session(sid, root) as state:
            return set(state.get("shown_unbound") or [])
    except OSError:
        return set()


def note_shown_unbound(sid: str, delivery_ids, root: Path | None = None) -> None:
    ids = [str(item) for item in delivery_ids if item]
    if not ids:
        return
    with _locked_session(sid, root) as state:
        known = list(state.get("shown_unbound") or [])
        for item in ids:
            if item not in known:
                known.append(item)
        state["shown_unbound"] = known[-200:]


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
        base = root if root is not None else _roots()[0]
        paths = [*(_state_path("x", root).parent.glob("*.json")),
                 *((Path(base) / SESSION_STATE_DIR).glob("*.json"))]
        for path in paths:
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
                    attempt = str(child.get("attempt_id") or "")
                    handles = sorted({attempt} if attempt else set()) + sorted(
                        set(_HANDLE_RE.findall(json.dumps(child, sort_keys=True))) - {attempt})
                    items.append({"kind": "completion", "delivery_id": delivery_id,
                                  "attempt_id": attempt or "-",
                                  "required_action": action, "handles": handles[:6]})
            continue
        action = str(receipt.get("required_action") or "")
        if action and action not in NON_ACTION_NOTICE:
            items.append({"kind": str(receipt.get("subject") or receipt.get("kind") or "notice"),
                          "delivery_id": delivery_id, "required_action": action})
    return items


def peer_report_item(prompt: str, sid: str = "") -> dict | None:
    """A verified peer report that arrived as this turn's prompt.

    The existing receive classification (``peer-message.prompt_origin``, read
    through ``session_tidy``) decides: only a sealed transfer addressed to this
    exact thread counts. A quoted ``(peer-from: ...)`` in an ordinary prompt
    has no such envelope, and a runtime completion or human answer carries its
    own required action in the receipt the bridge already rendered, so neither
    adds a pass here.
    """
    if not prompt or not sid or ("peer-from:" not in prompt and "runtime-from:" not in prompt):
        return None
    try:
        import session_tidy
        origin = session_tidy._current_request_origin(prompt, "codex", sid)
    except Exception:  # noqa: BLE001 - an unreadable envelope is not a report
        return None
    if not isinstance(origin, dict) or origin.get("kind") != "peer":
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


def queue_endpoint_absent() -> bool:
    """No App Server socket for this Codex home: the native queue cannot carry anything.

    Only then may a completion be shown in the turn without an ACK; with a
    socket present the native queue still owns it and showing it here could
    repeat it as a queued turn.
    """
    try:
        from codex_queue_dispatch import queue_endpoint_present
        return queue_endpoint_present() is False
    except Exception:  # noqa: BLE001 - an unreadable endpoint is not proof of absence
        return False


def _is_completion(record: dict) -> bool:
    return isinstance((record.get("receipt") or {}).get("children"), list)


def fit(batches, render, max_chars: int):
    """The shared leading-units fit for a hard limit (``dispatch_session_sweep.fit_inline``).

    By characters. One record over ``max_chars`` alone is shown as the head
    plus the path of its whole copy, or, without that copy, stays pending.
    """
    from dispatch_session_sweep import fit_inline
    return fit_inline(batches, max_chars, render=render)


def collect(sid: str, tid: str, *, budget_seconds: float, skip: set[str] = frozenset(),
            request_context: str | None = None, max_chars: int = MAX_REASON_CHARS,
            shown: list | None = None):
    """Claim this thread's owed records; return ``(text, ackable_batches)``.

    Records the bridge cannot ACK (a completion without an exact running native
    turn) are not claimed or rendered here while the App Server socket exists:
    the native queue and prompt carriers keep them, so a Stop continuation never
    repeats one. Without that socket nothing else can reach the running turn, so
    such a completion is shown once per session and left pending, never ACKed:
    the prompt carrier still shows it at the next prompt exactly as before. Its
    record is appended to ``shown``. A notice in the same root needs no
    turn binding and is still carried. Only records whose text fits
    ``max_chars`` whole are returned for ACK; the rest are handed back pending.
    """
    from dispatch_notice_state import NoticeReadContext
    from dispatch_session_sweep import (addressed_records, delivery_context,
                                        retain_receiving_turn, sweep_deliver)
    deadline_ns = time.monotonic_ns() + int(budget_seconds * 1_000_000_000)
    notice_context = NoticeReadContext()
    batches = []
    receiving: dict | None = None
    receiving_read = False
    socketless: bool | None = None
    already_shown: set[str] = set()
    for root in _roots():
        if time.monotonic_ns() >= deadline_ns:
            break
        # Read before claiming: a completion needs the exact running native turn
        # to be ACKed, and claiming one this bridge cannot ACK would hold the
        # native queue courier off. Without that turn the completions are left
        # unclaimed to their existing queue and prompt carriers; the root's
        # notices, which need no turn binding, are still carried.
        addressed, _count = addressed_records(root, RECIPIENT_KIND, sid, deadline_ns=deadline_ns)
        fresh = [record for record in addressed if record.get("delivery_id") not in skip]
        if not fresh:
            continue
        select = None
        if any(_is_completion(record) for record in fresh):
            if not receiving_read:
                receiving, receiving_read = native_receiving_turn(sid, tid), True
            if receiving is None:
                if socketless is None:
                    socketless = queue_endpoint_absent()
                    already_shown = shown_unbound(sid) if socketless else set()
                if socketless:
                    select = lambda record: (not _is_completion(record)  # noqa: E731
                                             or record.get("delivery_id") not in already_shown)
                elif all(_is_completion(record) for record in fresh):
                    continue
                else:
                    select = lambda record: not _is_completion(record)  # noqa: E731
        records, _count = sweep_deliver(root, RECIPIENT_KIND, sid, notice_context=notice_context,
                                       deadline_ns=deadline_ns, select=select)
        records = [record for record in records if record.get("delivery_id") not in skip]
        if records:
            batches.append((root, records))
    if not batches:
        return "", []
    if not receiving_read:
        receiving = native_receiving_turn(sid, tid) if any(
            _is_completion(record)
            for _root, records in batches for record in records) else None
    ackable = retain_receiving_turn(batches, sid, receiving, request_context=request_context)
    display, left = [], []
    for root, records in batches:
        exact = [record for record in records if (str(root), record.get("delivery_id")) in ackable
                 or (socketless and _is_completion(record)
                     and record.get("delivery_id") not in already_shown)]
        other = [record for record in records if record not in exact]
        if exact:
            display.append((root, exact))
        if other:
            left.append((root, other))
    if not display:
        release(sid, left)
        return "", []
    text, chosen, over = fit(display, delivery_context, max_chars)
    kept, unbound = [], []
    for root, records in chosen:
        exact = [record for record in records if (str(root), record.get("delivery_id")) in ackable]
        if exact:
            kept.append((root, exact))
        unbound.extend((root, record) for record in records
                       if (str(root), record.get("delivery_id")) not in ackable)
    if unbound and shown is not None:
        shown.extend(record for _root, record in unbound)
    # A tool boundary comes far more often than a prompt: hand back at once the
    # claims this bridge will not ACK (or that did not fit whole), so the
    # native queue courier is not held off for the claim lease. A completion
    # shown without its turn binding is handed back too: it stays pending.
    release(sid, left + over + [(root, [record]) for root, record in unbound])
    return text, kept


def release(sid: str, batches) -> None:
    """Return this process's own sweep claims on records it did not ACK."""
    from dispatch_session_sweep import release_claims
    release_claims(sid, batches, RECIPIENT_KIND, reason="codex-turn-carrier-unacked")


def acknowledge(sid: str, batches) -> list[str]:
    from dispatch_session_sweep import ack_delivered
    delivered = []
    for root, records in batches:
        ack_delivered(root, sid, records, acked_by=ACKED_BY_PREFIX + sid)
        delivered.extend(str(record.get("delivery_id")) for record in records)
    return delivered


def _settle_shown(sid: str, tid: str, batches, shown: list[dict]) -> None:
    """After the output reached Codex: ACK what may be ACKed, remember the rest as shown."""
    delivered = acknowledge(sid, batches) if batches else []
    shown_ids = [str(record.get("delivery_id")) for record in shown]
    note_shown_unbound(sid, shown_ids)
    note_received(sid, tid, owed_items(_all_records(batches) + list(shown)),
                  rendered=delivered + shown_ids)


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
        note_tool(sid, tid, command=tool_command(payload))
    shown: list[dict] = []
    try:
        state = read_state(sid, tid)
        text, batches = collect(sid, tid, budget_seconds=POST_TOOL_BUDGET_SECONDS,
                                skip=set(state.get("rendered") or []), shown=shown)
    except Exception:  # noqa: BLE001
        return 0
    if not text:
        return 0
    if _emit({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                     "additionalContext": text}}, stream):
        with contextlib.suppress(Exception):
            _settle_shown(sid, tid, batches, shown)
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
    shown: list[dict] = []
    module, watches = None, []
    # The reason is never cut: each part is sized so that every record and
    # watch row it names is whole, and only those are ACKed.
    room = MAX_REASON_CHARS - len(STOP_HEADER)
    watch_block = ""
    try:
        module, watches = watch_receipts(sid, budget_seconds=1.5)
        lines = []
        for row in watches:
            line = _watch_line(row)
            if len(WATCH_HEADER) + sum(len(item) + 1 for item in lines) + len(line) + 1 > room // 2:
                break
            lines.append(line)
        watches = watches[:len(lines)]
        if lines:
            watch_block = "\n".join([WATCH_HEADER, *lines])
    except Exception:  # noqa: BLE001
        module, watches = None, []
    try:
        state = read_state(sid, tid)
        text, batches = collect(sid, tid, budget_seconds=STOP_BUDGET_SECONDS,
                                skip=set(state.get("rendered") or []), shown=shown,
                                max_chars=room - (len(watch_block) + 2 if watch_block else 0) - 2)
        if text:
            parts.append(text)
    except Exception:  # noqa: BLE001
        batches = []
    if watch_block:
        parts.append(watch_block)
    if parts:
        reason = "\n\n".join([STOP_HEADER, *parts])
        if _emit({"decision": "block", "reason": reason}, stream):
            with contextlib.suppress(Exception):
                _settle_shown(sid, tid, batches, shown)
            if module is not None and watches:
                with contextlib.suppress(Exception):
                    ack_watches(module, sid, watches)
        else:
            with contextlib.suppress(Exception):
                release(sid, batches)
        return 0
    try:
        state = read_state(sid, tid)
        owed = unmet(state)
        if owed and not state.get("nudged"):
            if mark_nudged(sid, tid):
                _emit({"decision": "block", "reason": nudge_text(owed)}, stream)
    except Exception:  # noqa: BLE001
        pass
    return 0
