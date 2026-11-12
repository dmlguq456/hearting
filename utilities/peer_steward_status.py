"""Peer steward: status rows and watch readiness/transition helpers.

Split out of `peer-steward.py`, which re-exports every name defined here.
"""
import contextlib
import errno
import fcntl
import json
import os
import re
import tempfile
import time
from pathlib import Path

from dispatch_contract import process_start_ticks, resolve_agent_home, resolve_dispatch_state_root
import peer_obligations
import shared_readiness
import herdr_observation

from peer_steward_base import (
    _plain, _screen_lines, _tmux_surface, peer_message,
)

from peer_steward_watch import (
    _alive, _confirmed_watch_successor, _read_json, _utc_now, _watch_paths,
    _WATCH_SERVER_ENDPOINT_PREFIX, _WATCH_TRANSITION_LOCK_BUDGET_SEC, _WatchDeadlineExpired,
    _WatchTransitionLockBusy, _write_watch_observation,
)


def _status_entry(watch_id, arm, root):
    paths = _watch_paths(watch_id, root)
    receipt = _read_json(paths.receipt)
    if arm is None and receipt is not None:
        arm = {
            "watch_id": watch_id, "target": receipt.get("target", "-"), "until": [],
            "steward": receipt.get("steward") or {}, "wake": "-",
            "watcher": receipt.get("watcher") or {}, "armed_ts": receipt.get("armed_ts", "-"),
            "refs": receipt.get("refs") or [],
        }
    arm = arm or {"watch_id": watch_id}
    recipient = {"harness": (arm.get("steward") or {}).get("harness"),
                 "session_id": (arm.get("steward") or {}).get("session_id")}
    resolved = _confirmed_watch_successor(watch_id, arm, include_acked=True)
    if resolved:
        holder = resolved.get("holder") or {}
        recipient = {"harness": holder.get("harness"), "session_id": holder.get("sid")}
    try:
        import dispatch_seat_handover as handover
        acked = handover.watch_ack_valid(paths.ack, watch_id,
                                         recipient.get("harness"), recipient.get("session_id"))
    except Exception:
        acked = False
    if acked:
        state = "acked"
    elif receipt is not None:
        state = "receipt"
    elif _alive(arm, root):
        state = "alive"
    else:
        state = "pending"
    observation = _read_json(paths.observation) or {}
    watcher = arm.get("watcher") or {}
    return {
        "watch_id": watch_id,
        "state": state,
        "target": arm.get("target", "-"),
        "until": arm.get("until") or [],
        "wake": arm.get("wake", "-"),
        "armed_ts": arm.get("armed_ts", "-"),
        "steward": arm.get("steward") or {},
        "recipient": recipient,
        "watcher": {"pid": watcher.get("pid", "-"), "pid_start": watcher.get("pid_start", "-")},
        "receipt": str(paths.receipt),
        "ack": str(paths.ack),
        "receipt_state": (receipt or {}).get("state"),
        "agent": (receipt or {}).get("agent") or {},
        "server": (arm or {}).get("server") or (receipt or {}).get("server") or "default",
        "observation_state": observation.get("state", "pending" if receipt is None else "settled"),
        "observation_reason": observation.get("reason"),
    }


_PEER_STATUS_MAX_ROWS = 20
_PEER_STATUS_MAX_THREADS = 8
_PEER_STATUS_RPC_TIMEOUT = 0.25
_PEER_STATUS_RPC_BUDGET = 1.0


def _peer_status_codex_observer():
    """Read-only Codex queue/turn observer, cached per thread within one status call.

    Nothing is resolved or connected until the first Codex row asks.
    """
    import codex_queue_delivery
    cache = {}
    state = {"endpoint": None, "error": None, "session": None, "deadline": None, "ready": False}

    def prepare():
        state["ready"] = True
        try:
            from codex_queue_dispatch import resolve_queue_socket
            state["endpoint"] = resolve_queue_socket()
        except Exception as exc:  # noqa: BLE001 - status never fails on observation
            state["error"] = getattr(exc, "reason", None) or type(exc).__name__
            return
        state["deadline"] = time.monotonic() + _PEER_STATUS_RPC_BUDGET
        state["session"] = codex_queue_delivery.DeliverySession(
            state["endpoint"], timeout=_PEER_STATUS_RPC_TIMEOUT, deadline=state["deadline"])

    def observe(thread_id):
        if thread_id in cache:
            return cache[thread_id]
        if not state["ready"]:
            prepare()
        if state["error"] is not None:
            outcome = (None, None, state["error"])
        elif len(cache) >= _PEER_STATUS_MAX_THREADS:
            outcome = (None, None, "observation-cap")
        else:
            try:
                queued = codex_queue_delivery.list_queue(
                    state["endpoint"], thread_id, timeout=_PEER_STATUS_RPC_TIMEOUT,
                    session=state["session"], deadline=state["deadline"])
                turn = codex_queue_delivery._latest_turn(
                    state["endpoint"], thread_id, timeout=_PEER_STATUS_RPC_TIMEOUT,
                    session=state["session"])
                outcome = (queued, turn, None)
            except Exception as exc:  # noqa: BLE001
                outcome = (None, None, getattr(exc, "reason", None) or type(exc).__name__)
        cache[thread_id] = outcome
        return outcome

    def close():
        if state["session"] is not None:
            state["session"].close()

    return observe, close


def _peer_status_rows(sid, harness):
    """The caller's own unreceived peer messages, newest 20, with read-only Codex facts."""
    if not sid or not harness:
        return [], 0
    sender = {"harness": harness, "session_id": sid}
    rows = [row for row in peer_message._pending_rows(sender=sender)
            if peer_message._same_transfer_session(row.get("from"), sender)]
    rows.sort(key=lambda row: (row.get("created") or 0, row.get("ref") or ""))
    omitted = max(0, len(rows) - _PEER_STATUS_MAX_ROWS)
    rows = rows[omitted:]
    observe, close = _peer_status_codex_observer()
    now = time.time()
    result = []
    try:
        for row in rows:
            recipient = peer_message.pending_delivery_recipient(row) or row.get("to") or {}
            entry = {
                "ref": row["ref"],
                "to": {"harness": recipient.get("harness"),
                       "session_id": recipient.get("session_id"),
                       "name": recipient.get("name")},
                "state": row.get("state"), "receipt": row.get("receipt"),
                "age_s": max(0, int(now - (row.get("created") or now))),
                "steer": False,
            }
            if recipient.get("harness") == "codex" and recipient.get("session_id"):
                entry["steer"] = bool(peer_message._codex_steer_eligible(row))
                queued, turn, reason = observe(recipient["session_id"])
                client_id = "delivery-peer-" + row["ref"]
                codex = {"queue": "unknown", "turn": None, "turn_id": None,
                         "hearting_items": None, "other_items": None, "reason": reason}
                if queued is not None:
                    owners = [item.get("clientUserMessageId") for item in queued]
                    hearting = sum(isinstance(o, str) and o.startswith("delivery-") for o in owners)
                    codex.update(queue="present" if client_id in owners else "absent",
                                 hearting_items=hearting, other_items=len(owners) - hearting)
                    status = turn.get("status") if isinstance(turn, dict) else None
                    turn_id = turn.get("id") if isinstance(turn, dict) else None
                    codex.update(turn=status if isinstance(status, str) else None,
                                 turn_id=turn_id if isinstance(turn_id, str) else None)
                entry["codex"] = codex
            result.append(entry)
    finally:
        close()
    return result, omitted


def _peer_status_line(entry):
    to = entry["to"]
    line = (f"peer-message ref={entry['ref']} to={to.get('harness') or '-'}:"
            f"{to.get('name') or to.get('session_id') or '-'} state={entry['state']} "
            f"receipt={entry['receipt'] or '-'} age={entry['age_s']}s "
            f"steer={'yes' if entry['steer'] else 'no'}")
    codex = entry.get("codex")
    if codex:
        line += (f" queue={codex['queue']} turn={codex['turn'] or '-'} "
                 f"turn_id={codex['turn_id'] or '-'} "
                 f"hearting={'-' if codex['hearting_items'] is None else codex['hearting_items']} "
                 f"other={'-' if codex['other_items'] is None else codex['other_items']}")
        if codex["reason"]:
            line += f" reason={codex['reason']}"
    return line


def _watch_may_reach(watch_id, arm, sid, harness, seat_holders):
    """Whether ``_status_entry`` could name ``(harness, sid)`` this watch's recipient.

    Its recipient is the arm's steward unless the steward seat's ledger records
    a confirmed same-harness successor (`_confirmed_watch_successor`), so the
    arm and that one ledger decide it; ``seat_holders`` keeps each seat's
    watch holders for the rest of the pass. An arm without a steward, or a
    ledger that could not be read here, is left to the full status entry.
    """
    steward = arm.get("steward") if isinstance(arm, dict) else None
    if not isinstance(steward, dict):
        return True
    if steward.get("harness") != harness:
        return False
    if steward.get("session_id") == sid:
        return True
    seat = arm.get("steward_seat")
    seat_key = seat.get("key") if isinstance(seat, dict) else None
    if not seat_key:
        return False
    if seat_key not in seat_holders:
        try:
            import dispatch_seat_handover as handover
            seat_holders[seat_key] = {
                watch: relation.get("holder")
                for watch, (_key, relation) in handover._watch_states(seat_key).items()}
        except Exception:  # noqa: BLE001 - unread lineage never hides a receipt
            seat_holders[seat_key] = None
    holders = seat_holders[seat_key]
    if holders is None:
        return True
    holder = holders.get(str(watch_id))
    return (isinstance(holder, dict) and holder.get("harness") == harness
            and holder.get("sid") == sid)


_WATCH_STEWARD_INDEX = ".steward-index.json"
_WATCH_STEWARD_INDEX_MAX_BYTES = 8 * 1024 * 1024


def _arm_steward_facts(arm):
    """What decides a watch's recipient: its steward and steward seat.

    Both are written when the watch is armed and every later arm rewrite
    copies them (the watch id itself is derived from the steward), so they are
    fixed for the watch's life and safe to keep beside the arms.
    """
    steward = arm.get("steward")
    seat = arm.get("steward_seat")
    if not isinstance(steward, dict):
        return None  # no steward: the full status entry decides
    harness, sid = steward.get("harness"), steward.get("session_id")
    key = seat.get("key") if isinstance(seat, dict) else None
    return [harness if isinstance(harness, str) else None,
            sid if isinstance(sid, str) else None,
            key if isinstance(key, str) and key else None]


def _read_watch_steward_index(root):
    try:
        path = root / _WATCH_STEWARD_INDEX
        if path.is_symlink() or path.stat().st_size > _WATCH_STEWARD_INDEX_MAX_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    watches = data.get("watches") if isinstance(data, dict) and data.get("schema") == 1 else None
    if not isinstance(watches, dict):
        return {}
    known = {}
    for watch_id, facts in watches.items():
        if facts is None or (isinstance(facts, list) and len(facts) == 3 and all(
                value is None or isinstance(value, str) for value in facts)):
            known[watch_id] = facts
    return known


def _write_watch_steward_index(root, watches):
    """Derived and rebuildable: a lost or torn write only costs the next call arm reads."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(root), prefix=".tmp-steward-index-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"schema": 1, "watches": watches}, handle, separators=(",", ":"))
        os.replace(tmp, root / _WATCH_STEWARD_INDEX)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _receipt_watch_entries(root, sid, harness, seat_holders):
    """The watches the undelivered sweep reads in full, newest arm first.

    Only a watch with a receipt can be undelivered, and only its steward (or
    that seat's confirmed successor) can receive it. One directory listing
    names the receipts; `.steward-index.json` gives each one's steward, and
    only an arm the index does not know yet is read, once, then kept there.
    The cost is this session's watches plus arms new since the last call,
    not every arm and every file's stat.
    """
    try:
        with os.scandir(root) as directory:
            names = {entry.name for entry in directory}
    except FileNotFoundError:
        return []
    arms = {name[: -len(".json")] for name in names
            if name.endswith(".json") and not name.startswith(".")
            and not name.endswith((".receipt.json", ".ack.json", ".observation.json"))}
    receipts = {name[: -len(".receipt.json")] for name in names
                if name.endswith(".receipt.json") and not name.startswith(".")}
    known = _read_watch_steward_index(root)
    kept, read_arms, reachable = {}, {}, []
    for watch_id in sorted(receipts & arms):
        if watch_id in known:
            facts = known[watch_id]
        else:
            arm = _read_json(root / f"{watch_id}.json")
            if not isinstance(arm, dict):
                reachable.append(watch_id)  # unreadable: the full status entry decides
                continue
            read_arms[watch_id] = arm
            facts = _arm_steward_facts(arm)
        kept[watch_id] = facts
        if facts is None or _watch_may_reach(
                watch_id, {"steward": {"harness": facts[0], "session_id": facts[1]},
                           "steward_seat": {"key": facts[2]}},
                sid, harness, seat_holders):
            reachable.append(watch_id)
    if kept != known:
        _write_watch_steward_index(root, kept)

    def _mtime(watch_id):
        try:
            return (root / f"{watch_id}.json").stat().st_mtime
        except OSError:
            return 0.0

    entries = [(watch_id, read_arms[watch_id] if watch_id in read_arms
                else _read_json(root / f"{watch_id}.json"))
               for watch_id in sorted(reachable, key=_mtime, reverse=True)]
    # A receipt without an arm (killed between spawn and arm write) describes itself.
    entries.extend((watch_id, None) for watch_id in sorted(receipts - arms))
    return entries

_PROMPT_VERIFY_TIMEOUT_MS = 8000      # bounded native /clear command invocation
# Queued is ordinary acceptance (exit 0); the printed `prompted=` word tells it from receipt.
_PROMPT_EXIT = {"true": 0, "failed": 1, "queued": 0, "unverified": 5}
_PROMPT_LEDGER_STATUS = {"true": "sent", "failed": "failed", "queued": "unknown", "unverified": "unknown"}


def _herdr_prompt_payload(stdout, stderr):
    for stream in (stdout, stderr):
        try:
            candidate = json.loads(stream)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(candidate, dict):
            return candidate
    return None


# `execve` failures happen before the child runs, so they typed nothing. Any
# other `OSError` (EPIPE/ECONNRESET on the pipe, an unknown errno) may have
# already delivered the prompt and must stay ambiguous.
_EXEC_NEVER_STARTED_ERRNOS = frozenset(
    value for value in (getattr(errno, name, None) for name in
                        ("ENOENT", "EACCES", "ENOTDIR", "ENOEXEC"))
    if value is not None)


def _tmux_prompt(target, text, *, wait, timeout_ms, start_aware=False):
    """`_herdr_prompt` for a tmux pane: one paste and Enter, then (``wait``) the turn start."""
    tmux = _tmux_surface(target)
    if tmux is None:
        return None, ({"error": {"code": "herdr-not-started"}} if start_aware else None)
    rc, payload = tmux.prompt(target, text)
    if rc is None:
        return None, (payload if start_aware else None)
    if rc != 0 or not wait:
        return rc, payload
    waited = tmux.wait_agent(target, ("working",), timeout_ms)
    if isinstance(waited, dict) and waited.get("result"):
        return 0, waited
    return 1, waited if isinstance(waited, dict) else {"error": {"code": "timeout"}}


_FORM_TOKENS = ("esctocancel", "entertoselect", "entertoconfirm", "doyouwanttoproceed",
                "tab/arrowkeys", "arrowkeystonavigate")
_CODEX_LOCATION_TOKENS = (
    "whereshouldthenewconversationrun", "whereshouldtheforkedconversationrun",
    "usecurrentgitworktree", "keepusingthecurrentworkingdirectory", "createnewgitworktree",
)
_NATIVE_TRUST_TOKENS = {
    "codex": ("trustthisfolder", "trustthisworkspace", "doyoutrustthisproject"),
    "claude": ("trustthisfolder", "trustthisproject", "doyoutrustthecontentsofthisfolder"),
    "opencode": ("trustthisfolder", "trustthisproject", "doyoutrustthisproject"),
}


def _blocking_screen(harness, lines, *, window=15):
    """Return the existing wait reason for a visibly active native form.

    Unknown/unreadable content is deliberately non-blocking. Codex's hook
    review is recognized only as the standalone top-origin TUI view; quoted
    transcript text and a composer following the quote are not a form.
    """
    if isinstance(lines, str):
        lines = _screen_lines(lines)
    if not isinstance(lines, (list, tuple)):
        return None
    rendered = []
    for line in lines:
        value = _plain(line) if isinstance(line, list) else str(line)
        rendered.append(value.rstrip())
    while rendered and not rendered[-1].strip():
        rendered.pop()
    meaningful = [line for line in rendered if line.strip()]
    if not meaningful:
        return None
    flat_bottom = "".join("".join(meaningful[-window:]).split()).lower()
    harness = str(harness or "").lower()
    if harness == "codex":
        normalized = ["".join(line.split()).lower() for line in meaningful]
        title = normalized[0].strip("─━│╭╮╰╯┌┐└┘")
        if title == "hooksneedreview" and len(normalized) > 1:
            # This dialog owns the top of the cleared TUI. Its rows may wrap
            # and may include progress/error text before the three options.
            count = re.fullmatch(r"\d+hooks?(?:isnew|arenew)orchanged\.?", normalized[1])
            option_rows = []
            for index, row in enumerate(normalized[2:], start=2):
                option_rows.append((index, re.sub(r"^(?:[❯›>])?(?:[123][.)])?", "", row)))
            labels = ("reviewhooks", "trustallandcontinue", "continuewithouttrusting")
            positions = []
            cursor = 0
            for label in labels:
                found = next((i for i in range(cursor, len(option_rows))
                              if option_rows[i][1].startswith(label)), None)
                positions.append(found)
                if found is not None:
                    cursor = found + 1
            if count and all(pos is not None for pos in positions):
                third = positions[-1]
                tail = "".join(row for _index, row in option_rows[third + 1:])
                # Only the wrapped parenthetical and the native keymap footer
                # may follow the final choice in the standalone view.
                tail = tail.replace("(hookswon'trun)", "")
                if (not tail or tail.startswith("won'trun)") or "entertoconfirm" in tail
                        or "enterselect" in tail or "escskip" in tail):
                    return "native-trust-wait"

        # The /new and /fork picker is a bottom-pane popup that replaces the
        # composer while open. A composer row (Codex `›`, or `❯`) below the
        # title means the title is a transcript quote, not the live popup.
        active_rows = normalized[-window:]
        menu_choices = ("usecurrentgitworktree", "keepusingthecurrentworkingdirectory",
                        "createnewgitworktree")

        def composer_row(row):
            return (row[:1] in ("›", "❯") and len(row) > 1
                    and not any(choice in row for choice in menu_choices))

        live_composer = composer_row(active_rows[-1])
        location_title = next((i for i, row in enumerate(active_rows)
                               if "whereshouldthenewconversation" in row
                               or "whereshouldtheforkedconversation" in row), None)
        if location_title is not None:
            area_rows = active_rows[location_title:]
            area = "".join(area_rows)
            if (not any(composer_row(row) for row in area_rows[1:])
                    and any(token in area for token in menu_choices)):
                return "form-open"
    else:
        live_composer = False
    if not live_composer and any(token in flat_bottom for token in _FORM_TOKENS):
        return "form-open"
    if any(token in flat_bottom for token in _NATIVE_TRUST_TOKENS.get(harness, ())):
        return "native-trust-wait"
    return None


def _codex_location_menu(lines):
    """Recognize Codex new/fork location forms after ANSI and line-wrap removal."""
    if isinstance(lines, str):
        lines = _screen_lines(lines)
    if not isinstance(lines, (list, tuple)):
        return False
    flat = "".join("".join(_plain(line) if isinstance(line, list) else str(line)
                          for line in lines).split()).lower()
    return any(token in flat for token in _CODEX_LOCATION_TOKENS)


def _watch_selected_jobs_path():
    """Return the selected dispatch registry used by all readiness authority."""
    return resolve_dispatch_state_root(resolve_agent_home()) / "jobs.log"


def _watch_readiness_fields_match(expected, observed):
    """Let the common classifier contribute only a proven SID for a sidless pane."""
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        return False
    if any(observed.get(key) != expected.get(key)
           for key in ("server", "pane", "harness", "pid_birth")):
        return False
    expected_sid = expected.get("session_id", "")
    observed_sid = observed.get("session_id", "")
    return (observed_sid == expected_sid
            or (expected_sid == "" and isinstance(observed_sid, str) and bool(observed_sid)))


@contextlib.contextmanager
def _watch_transition_authority_lock(identity, server, *, deadline=None):
    """Join jobs then target-seat locks within one bounded receipt-edge budget."""
    import dispatch_seat_handover

    now = time.monotonic()
    transition_deadline = now + _WATCH_TRANSITION_LOCK_BUDGET_SEC
    if deadline is not None:
        transition_deadline = min(transition_deadline, float(deadline))
        if now >= deadline:
            raise _WatchDeadlineExpired("watch-deadline")
    jobs = _watch_selected_jobs_path()
    lock_path = Path(str(jobs) + ".lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if time.monotonic() >= transition_deadline:
        raise _WatchTransitionLockBusy("jobs-lock-budget-expired")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    jobs_locked = False
    try:
        while time.monotonic() < transition_deadline:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                jobs_locked = True
                break
            except BlockingIOError as exc:
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    raise _WatchDeadlineExpired("watch-deadline") from exc
                remaining = transition_deadline - now
                if remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))
        if not jobs_locked:
            if deadline is not None and time.monotonic() >= deadline:
                raise _WatchDeadlineExpired("watch-deadline")
            raise _WatchTransitionLockBusy("jobs-lock-busy")
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            raise _WatchDeadlineExpired("watch-deadline")
        if now >= transition_deadline:
            raise _WatchTransitionLockBusy("jobs-lock-budget-expired")
        endpoint = identity.get("endpoint") if isinstance(identity, dict) else None
        socket_path = (None if isinstance(endpoint, str)
                       and endpoint.startswith((_WATCH_SERVER_ENDPOINT_PREFIX, "native:"))
                       else endpoint if isinstance(endpoint, str) and endpoint
                       else herdr_observation.socket_path_for_endpoint(server))
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            raise _WatchDeadlineExpired("watch-deadline")
        remaining = transition_deadline - now
        if remaining <= 0:
            raise _WatchTransitionLockBusy("seat-lock-budget-expired")
        seat_lock = dispatch_seat_handover.readiness_transition_lock(
            identity.get("pane"), server, socket_path, wait=remaining)
        try:
            seat_lock.__enter__()
        except BlockingIOError as exc:
            if deadline is not None and time.monotonic() >= deadline:
                raise _WatchDeadlineExpired("watch-deadline") from exc
            raise _WatchTransitionLockBusy("seat-lock-busy") from exc
        try:
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                raise _WatchDeadlineExpired("watch-deadline")
            if now >= transition_deadline:
                raise _WatchTransitionLockBusy("seat-lock-budget-expired")
            yield
        finally:
            seat_lock.__exit__(None, None, None)
    finally:
        if jobs_locked:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _watch_note_transition_lock_contention(root, watch_id, paths):
    """Keep this watch pending and coalesce unchanged runner refresh requests."""
    latest = _read_json(paths.observation) or {}
    request = latest.get("readiness_transition")
    request_token = request.get("token") if isinstance(request, dict) else None
    contention = latest.get("readiness_contention")
    same_request = (isinstance(request_token, str) and request_token
                    and isinstance(contention, dict)
                    and contention.get("request_token") == request_token)
    if not same_request:
        latest.update({
            "state": "pending",
            "reason": "shared-readiness-authority-lock-busy",
            "observed_ts": _utc_now(),
        })
        if request_token:
            latest["readiness_contention"] = {"request_token": request_token}
        _write_watch_observation(
            paths, latest, control_updates={"readiness_contention"})
    try:
        shared_readiness.clear_watch_transition(root, watch_id)
        if not same_request:
            _wake_shared_runner(root)
        else:
            marker = _read_json(Path(root) / "readiness-v1" / "runner.json") or {}
            try:
                runner_pid = int(marker.get("pid"))
                runner_start = str(marker.get("pid_start") or "")
                runner_live = bool(runner_start and
                                   str(process_start_ticks(runner_pid) or "") == runner_start)
            except (TypeError, ValueError):
                runner_live = False
            if not runner_live:
                # Preserve the existing crash/restart path without waking a
                # current runner for an identical response and unchanged input.
                peer_obligations.ensure_normal_runner(reconciliation_required=True)
    except Exception:
        pass


def _wake_shared_runner(root):
    """Ask the existing runner for a pass now; start one if none holds its lock."""
    try:
        shared_readiness.wake_runner(root)
        peer_obligations.ensure_normal_runner(reconciliation_required=True)
    except Exception:
        pass
