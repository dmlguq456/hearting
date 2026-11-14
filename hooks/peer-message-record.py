#!/usr/bin/env python3
"""SD-122 peer-message ledger recorder.

Two subcommands, dispatched by argv[1]:
  post-tool  -- PostToolUse / PostToolUseFailure hook. Records one
                peer_message_v1 for each SendMessage tool call (not a failed
                one), and hands the running turn the records
                owed to this session (a completion receipt, a due steward
                check, a report notice) through the same shared sweep as the
                prompt carrier, as Codex's in-turn carrier does at the same
                boundary. A record the asyncRewake hook already claimed or
                ACKed stays with it; whichever carrier claims first shows it.
  stop       -- Stop hook. Records still owed to this session when the turn
                would end (the rest a prompt boundary could not show whole)
                continue the same turn through Claude's documented
                ``{"decision": "block", "reason": ...}``, as Codex's in-turn
                carrier does at its Stop. Only what the reason shows whole is
                held, so each continuation removes what it showed and the
                turn ends once nothing more is owed. Claude Code can override
                a block and end the turn (its consecutive-Stop cap), so a
                shown record is ACKed only at the next call that proves the
                continuation ran (Stop with ``stop_hook_active``, or a tool
                boundary); a new prompt hands it back pending instead.
  prompt     -- UserPromptSubmit hook. Records one `notice` peer_message_v1
                when the injected prompt carries a cross-session message, and
                sweeps up to five un-acked detached-watch receipts into
                `additionalContext` (SD-122 (10) fallback carrier: the
                `asyncRewake` hook may die or the session may restart, but the
                receipt on disk does not).

Fail-soft over its entire body: any exception is swallowed and the process
exits 0. Never prints permissionDecision/deny. Stdout carries only the one
hook JSON object (UserPromptSubmit/PostToolUse context), and only when
something is pending.

Both hooks run under a 10 s harness timeout, and a cancelled hook loses the
whole context it was about to add. All reading and claiming before that
context is flushed therefore shares one budget (`_HOOK_BUDGET_S`) counted from
process start: work past it is left exactly as it is on disk for the next
prompt or tool call, the Stop path, the asyncRewake hook or the runner,
instead of making the user wait. What was claimed or shown is still settled.

After the flush the hook must also end in time, because a cancelled hook's
output is discarded even when its rows were already marked received. The
watch ACKs therefore run in a child with a hard timeout, and a run started
as a script exits on its own `_EXIT_AFTER_FLUSH_S` after spawn once its
output is flushed.
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path


# The owed-record share of a tool boundary: records wait one more boundary rather
# than make a tool call slow. Codex's in-turn carrier uses the same bound.
_RECORD_SHARE_S = 1.5
# Claude caps hook additionalContext; a notice that does not fit whole stays
# pending for the next boundary (the shared whole-record selection).
_RECORD_MAX_CHARS = 8000
_CLAUDE_RECIPIENT_KIND = "claude-parent-runtime"


def _process_started():
    """This process's spawn on the ``time.monotonic()`` scale (now when unknown).

    The harness timeout runs from spawn, and interpreter start plus imports can
    take seconds on a loaded machine."""
    try:
        with open("/proc/self/stat", "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[1].split()
        age = (time.clock_gettime(time.CLOCK_BOOTTIME)
               - int(fields[19]) / os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError, AttributeError):
        return time.monotonic()
    return time.monotonic() - min(max(age, 0.0), 60.0)


# Module load; a run started as a script rebinds it to the process spawn below.
_STARTED = time.monotonic()
_HOOKS_DIR = Path(__file__).resolve().parent
_UTILITIES_DIR = _HOOKS_DIR.parent / "utilities"
_PEER_MESSAGE_PY = _UTILITIES_DIR / "peer-message.py"
_PEER_STEWARD_PY = _UTILITIES_DIR / "peer-steward.py"
_SWEEP_MAX = 5
# Reading and claiming before the flush (the hook timeout is 10 s). A normal
# prompt needs well under a second of it; a loaded disk stops here.
_HOOK_BUDGET_S = 2.5
# The watch sweep is the fallback carrier: it runs after the exact peer-message
# claims and may use at most this much of what they left.
_SWEEP_SHARE_S = 1.0
# ACKs for receipts already shown run in a child that must end this long after
# spawn. One left undone shows its line again on the next prompt, which is
# better than a cancelled hook losing the whole context.
_ACK_END_S = 4.0
# A script run that is still busy settling this long after spawn exits once its
# output is flushed, so the harness (10 s) never discards what it showed.
_EXIT_AFTER_FLUSH_S = 8.5
# `record` children write one ledger row each; they keep their own bound.
_RECORD_TIMEOUT_S = 2.5
_MODULES = {}


def _deadline():
    return _STARTED + _HOOK_BUDGET_S


def _load_module(key, path, name):
    """One fail-soft load per process; ``None`` when the file cannot run."""
    if key not in _MODULES:
        try:
            spec = importlib.util.spec_from_file_location(name, str(path))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _MODULES[key] = mod
        except BaseException:  # a broken tool may even call sys.exit at import
            _MODULES[key] = None
    return _MODULES[key]


def _peer_message_module():
    """The ledger tool as a module (trailer parser + registry-name lookup), fail-soft."""
    return _load_module("peer-message", _PEER_MESSAGE_PY, "_peer_message")


def _peer_request_module():
    """`peer_request` (the message-kind table), loaded like the other tools."""
    return _load_module("peer_request", _UTILITIES_DIR / "peer_request.py", "_peer_request")


def _steward_module():
    """`peer-steward.py` in this process: a second interpreter and import per
    prompt cost more than the sweep itself once the machine was loaded."""
    return _load_module("peer-steward", _PEER_STEWARD_PY, "_peer_record_steward")

def _read_stdin_json():
    data = sys.stdin.read()
    if not data:
        return {}
    return json.loads(data)


def _project_of(cwd):
    if not cwd:
        return ""
    return os.path.basename(str(cwd).rstrip("/"))


def _address_session_id(mod, address):
    """A peer socket address → that session's id, fail-soft to ``None``."""
    if mod is None or not address:
        return None
    try:
        return mod.claude_session_id_for_address(str(address))
    except Exception:
        return None


def _kind_for_body(body, notify_when_idle):
    if notify_when_idle:
        return "watch"
    # One table for the ledger kind, the Codex follow-through pass and this hook.
    table = _peer_request_module()
    return table.message_kind(body or "")[0] if table else "steer"


def handle_post_tool(payload):
    if payload.get("tool_name") != "SendMessage":
        return
    tool_input = payload.get("tool_input") or {}
    to = tool_input.get("to")
    if to is None:
        return
    message = tool_input.get("message") or ""
    notify_when_idle = bool(tool_input.get("notify_when_idle"))
    kind = _kind_for_body(message, notify_when_idle)

    from_session_id = payload.get("session_id") or ""
    from_project = _project_of(payload.get("cwd"))
    from_name = None
    mod = _peer_message_module()
    if mod is not None:
        try:
            from_name = mod.claude_session_name(from_session_id)
        except Exception:
            from_name = None

    args_list = [
        "--from-harness", "claude",
        "--from-session-id", from_session_id,
        "--from-project", from_project,
        "--to-harness", "claude",
        "--kind", kind,
        "--surface", "claude-native",
        "--status", "sent",
        "--body-stdin",
    ]
    if from_name:
        args_list += ["--from-name", from_name]
    if isinstance(to, dict):
        to_session_id = to.get("session_id")
        to_name = to.get("name") or to.get("address")
    else:
        to_session_id, to_name = None, to
    if not to_session_id:
        # SendMessage addresses a peer by its socket, and the socket is named after that
        # session's PID, so the address resolves to an exact id. Without this the ledger
        # kept the address as a `name` and the board drew a bare `claude` — 222 rows
        # measured 2026-09-10.
        to_session_id = _address_session_id(mod, to_name)
    if to_session_id:
        args_list += ["--to-session-id", str(to_session_id)]
    if to_name:
        args_list += ["--to-name", str(to_name)]

    subprocess.run(
        [sys.executable, str(_PEER_MESSAGE_PY), "record"] + args_list,
        input=message.encode("utf-8", "replace"),
        timeout=_RECORD_TIMEOUT_S,
        capture_output=True,
    )


_CROSS_SESSION_RE = re.compile(r"<cross-session-message\s+from=\"([^\"]*)\"")
_CROSS_SESSION_NAME_RE = re.compile(r"<cross-session-message\s[^>]*?from-name=\"([^\"]*)\"")


def tool_boundary_records(session_id, deadline, max_chars=_RECORD_MAX_CHARS):
    """Claim this session's owed records for the running turn: ``(text, batches)``.

    The same records and claim as the prompt sweep (``dispatch-session-sweep.py``):
    a completion that closed during the tool call reaches this turn instead of
    waiting for the next prompt. ``deadline`` is ``time.monotonic()``. Only
    records whose text fits ``max_chars`` whole are returned for ACK (one over
    it alone as its head plus a whole copy's path, or, without that copy,
    left pending); the rest are handed back pending.
    """
    if not session_id or time.monotonic() >= deadline:
        return "", []
    if str(_UTILITIES_DIR) not in sys.path:
        sys.path.insert(0, str(_UTILITIES_DIR))
    from dispatch_contract import dispatch_state_roots, resolve_agent_home
    from dispatch_notice_state import NoticeReadContext
    from dispatch_session_sweep import fit_inline, release_claims, sweep_deliver
    deadline_ns = time.monotonic_ns() + int(max(0.0, deadline - time.monotonic()) * 1e9)
    context = NoticeReadContext()
    batches = []

    for root in dict.fromkeys(dispatch_state_roots(resolve_agent_home())):
        if time.monotonic_ns() >= deadline_ns:
            break
        if not Path(root).is_dir():
            continue  # a missing fallback root holds nothing; touching it would create it
        try:
            records, _count = sweep_deliver(root, _CLAUDE_RECIPIENT_KIND, session_id,
                                           notice_context=context, deadline_ns=deadline_ns)
        except Exception:  # noqa: BLE001 - left for the prompt sweep
            continue
        if records:
            batches.append((root, records))
    if not batches:
        return "", []
    text, chosen, over = fit_inline(batches, max_chars)
    release_claims(session_id, over, _CLAUDE_RECIPIENT_KIND,
                   reason="claude-tool-boundary-unshown")
    return text, chosen


_STOP_HEADER = "[hearting] Arrived before this turn ended:"


def _turn(payload):
    """``(session_id, prompt_id)`` of a main-conversation hook, or ``None``.

    Claude Code gives every hook of one user prompt the same ``prompt_id``
    (hooks reference, common input fields); it is this turn's key for the
    follow-through state Codex keys by ``turn_id``. A subagent's hook is not
    the main turn.
    """
    sid, tid = payload.get("session_id"), payload.get("prompt_id")
    if (not isinstance(sid, str) or not sid or not isinstance(tid, str) or not tid
            or str(payload.get("agent_id") or "").strip()):
        return None
    return sid, tid


def _carrier():
    if str(_UTILITIES_DIR) not in sys.path:
        sys.path.insert(0, str(_UTILITIES_DIR))
    import codex_turn_carrier
    return codex_turn_carrier


def note_turn_tool(payload):
    """Count this tool for the shared follow-through rule (fail-soft)."""
    with contextlib.suppress(Exception):
        turn = _turn(payload)
        if turn:
            carrier = _carrier()
            carrier.note_tool(*turn, command=carrier.tool_command(payload))


def _peer_items(shown):
    """Owed items for the sealed peer bodies a flushed context showed (fail-soft)."""
    try:
        carrier = _carrier()
        return [carrier.peer_body_item(row["text"]) for row in shown or ()
                if isinstance(row, dict) and row.get("peer")]
    except Exception:  # noqa: BLE001
        return []


def note_turn_received(payload, batches, extra=()):
    """Record the owed actions this turn was shown (fail-soft)."""
    with contextlib.suppress(Exception):
        turn = _turn(payload)
        if turn:
            carrier = _carrier()
            records = [record for _root, rows in batches for record in rows]
            items = carrier.owed_items(records) + [item for item in extra if item]
            if items or records:
                carrier.note_received(*turn, items, rendered=[
                    str(record.get("delivery_id")) for record in records])


def handle_stop(payload, out):
    """Continue the turn once more with the records owed to this session, if any.

    Same claim, fit and settlement as a tool boundary; nothing is owed, nothing
    is printed and the turn ends. A record not shown whole stays pending.
    """
    session_id = payload.get("session_id")
    if not session_id or str(payload.get("agent_id") or "").strip():
        return
    if str(_UTILITIES_DIR) not in sys.path:
        sys.path.insert(0, str(_UTILITIES_DIR))
    from dispatch_session_sweep import CLAUDE_CONTEXT_CHARS, hold_stop_sends
    # An earlier block's records are ACKed only from the native transcript.
    settle_stop_records(payload)
    text, batches = tool_boundary_records(
        session_id, _deadline(), max_chars=CLAUDE_CONTEXT_CHARS - len(_STOP_HEADER) - 2)
    try:
        if text:
            out.write(json.dumps({"decision": "block", "reason": _STOP_HEADER + "\n\n" + text},
                                 ensure_ascii=False) + "\n")
            out.flush()
        else:
            # Nothing more is owed: the one follow-through pass Codex's Stop
            # gives an owed action no tool acted on (PG-44), same rule and state.
            reason = ""
            with contextlib.suppress(Exception):
                turn = _turn(payload)
                reason = _carrier().follow_through(*turn) if turn else ""
            if reason:
                out.write(json.dumps({"decision": "block", "reason": reason},
                                     ensure_ascii=False) + "\n")
                out.flush()
    finally:
        if batches:
            if out.flushed:
                # Printed is not read: Claude Code may override this block and
                # end the turn. Settled at the next call (above, or a tool boundary).
                hold_stop_sends(session_id, batches, _CLAUDE_RECIPIENT_KIND)
                note_turn_received(payload, batches)
            else:
                settle_tool_boundary_records(session_id, batches, False)


def settle_stop_records(payload):
    """ACK what an earlier Stop block showed once the session's transcript holds it."""
    with contextlib.suppress(Exception):
        if str(_UTILITIES_DIR) not in sys.path:
            sys.path.insert(0, str(_UTILITIES_DIR))
        from dispatch_contract import dispatch_state_roots, resolve_agent_home
        from dispatch_session_sweep import claude_hook_evidence, settle_stop_sends
        remaining = _deadline() - time.monotonic()
        if remaining <= 0:
            return
        session_id = payload.get("session_id")
        settle_stop_sends(session_id, dispatch_state_roots(resolve_agent_home()),
                          _CLAUDE_RECIPIENT_KIND, claude_hook_evidence(payload, session_id),
                          deadline_ns=time.monotonic_ns() + int(remaining * 1e9))


def settle_tool_boundary_records(session_id, batches, shown):
    """ACK what the flushed context showed; hand anything else back pending."""
    if str(_UTILITIES_DIR) not in sys.path:
        sys.path.insert(0, str(_UTILITIES_DIR))
    from dispatch_session_sweep import ack_delivered, release_claims
    if not shown:
        release_claims(session_id, batches, _CLAUDE_RECIPIENT_KIND,
                       reason="claude-tool-boundary-unshown")
        return
    for root, records in batches:
        with contextlib.suppress(Exception):
            ack_delivered(root, session_id, records, acked_by=f"session-sweep:{session_id}")


def handle_prompt(payload):
    prompt = payload.get("prompt") or ""
    session_id = payload.get("session_id")
    if not session_id:
        # M1: the receiving session's own id is the only verifiable identity
        # source here. Without it we cannot claim an exact to.session_id, and
        # a name fallback would defeat F-98b's exact-session-id join contract.
        return
    mod = _peer_message_module()
    match = _CROSS_SESSION_RE.search(prompt)
    if match:
        from_name = match.group(1)
        named = _CROSS_SESSION_NAME_RE.search(prompt)
        args_list = [
            "--from-harness", "claude",
            # F-101h: Claude's native envelope carries neither sender session_id nor
            # kind. Filled in below from the sender's socket address when that resolves;
            # otherwise it stays empty and exact correlation fails soft, while the receive
            # counter and the notice record remain honest.
            "--from-session-id", "",
            "--from-project", _project_of(payload.get("cwd")),
            "--to-harness", "claude",
            "--to-session-id", str(session_id),
            "--kind", "notice",
            "--surface", "claude-native",
            "--status", "received",
            "--body-stdin",
        ]
        if named and named.group(1):
            args_list += ["--from-name", named.group(1)]
        # `from=` is the sender's socket address, which names the sender's PID and so its
        # session. Claude's native envelope carries no sender id of its own, and this is
        # the only thing in it that resolves to one.
        sender_sid = _address_session_id(mod, from_name)
        if sender_sid:
            args_list[args_list.index("--from-session-id") + 1] = str(sender_sid)
        # The RECEIVER's own name belongs in the `to` block. Until 2026-09-10 the sender's
        # address was written here instead, which put one endpoint's identity under the
        # other endpoint's key.
        to_name = mod.claude_session_name(str(session_id)) if mod is not None else None
        if to_name:
            args_list += ["--to-name", str(to_name)]
        _record(args_list, "cross-session message received")
        return
    # F-100c: a herdr-delivered steer carries the sender trailer instead of an envelope —
    # the same rule the Codex hook and the OpenCode plugin apply, so all three harnesses
    # write the same `notice` shape.
    trailer = mod.parse_peer_trailer(prompt, {"harness": "claude", "session_id": str(session_id)}) if mod is not None else None
    if not trailer:
        return
    mod.receive_peer_message(prompt, {"harness": "claude", "session_id": str(session_id)},
                             _project_of(payload.get("cwd")), summary_text="herdr steer received")


def _record(args_list, body):
    subprocess.run(
        [sys.executable, str(_PEER_MESSAGE_PY), "record"] + args_list,
        input=body.encode("utf-8"),
        timeout=_RECORD_TIMEOUT_S,
        capture_output=True,
    )


def _typed_watch_line(row):
    agent = row.get("agent") if isinstance(row.get("agent"), dict) else {}
    return (
        f"state={row.get('receipt_state') or 'unknown'} agent={agent.get('harness', '-')} "
        f"session_id={agent.get('session_id', '-')} name={agent.get('name') or row.get('target', '-')} "
        f"pane={agent.get('pane', '-')} watch_id={row.get('watch_id', '-')} "
        f"receipt={row.get('receipt', '-')}"
    )


@contextlib.contextmanager
def _steward_call(session_id):
    """Run in-process `peer-steward.py` code as the prompt's own session, with
    stdout (and any child's fd 1) kept out of the hook's JSON output."""
    previous = os.environ.get("CLAUDE_CODE_SESSION_ID")
    os.environ["CLAUDE_CODE_SESSION_ID"] = str(session_id)
    saved = None
    try:
        sys.stdout.flush()
        saved = os.dup(1)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.close(devnull)
    except (OSError, ValueError):
        if saved is not None:
            os.close(saved)
        saved = None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            yield
    finally:
        if saved is not None:
            os.dup2(saved, 1)
            os.close(saved)
        if previous is None:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_CODE_SESSION_ID"] = previous


def sweep_undelivered(payload, deadline=None):
    """List un-acked watch receipts for THIS session (bounded, read-only).

    Fail-soft in its own right: a broken or slow `peer-steward.py` must never
    cost the user a prompt, so ``deadline`` stops it and an unreached receipt
    simply waits for the next prompt. Acking is a separate step (`ack_rows`)
    that runs only AFTER the context has been written: an ack with no display
    behind it would silence the fallback forever (review M2).
    """
    session_id = payload.get("session_id")
    if not session_id or (deadline is not None and time.monotonic() >= deadline):
        return []  # Startup already spent the share: the receipts wait on disk.
    try:
        steward = _steward_module()
        if steward is None:
            return []
        with _steward_call(session_id):
            rows = steward.undelivered_watch_rows(deadline=deadline)
        return [row for row in rows if isinstance(row, dict)][:_SWEEP_MAX]
    except BaseException:
        return []


_ACK_SCRIPT = """
import runpy, signal, sys
signal.alarm(int(float(sys.argv[2])) + 1)  # ends itself even if its parent is gone
steward = runpy.run_path(sys.argv[1])
for watch_id in sys.argv[3:]:
    try:
        steward["main"](["ack", watch_id, "--carrier", "userprompt-sweep"])
    except (Exception, SystemExit):
        continue
"""


def ack_rows(payload, rows, deadline=None):
    """ACK the receipts just shown through the normal `ack` command.

    Each watch is revalidated by `ack` itself, which may wait on its seat lock,
    so the ACKs run in one child with a hard timeout ending at ``deadline``
    (process spawn + `_ACK_END_S` by default). An unacknowledged receipt is
    listed again on the next prompt.
    """
    session_id = payload.get("session_id")
    if not session_id:
        return
    watch_ids = [str(row["watch_id"]) for row in rows[:_SWEEP_MAX] if row.get("watch_id")]
    if not watch_ids:
        return
    deadline = _STARTED + _ACK_END_S if deadline is None else deadline
    remaining = deadline - time.monotonic()
    if remaining < 0.5:
        return
    env = dict(os.environ)
    env["CLAUDE_CODE_SESSION_ID"] = str(session_id)
    try:
        subprocess.run(
            [sys.executable, "-c", _ACK_SCRIPT, str(_PEER_STEWARD_PY),
             str(remaining), *watch_ids],
            capture_output=True, timeout=remaining, env=env)
    except Exception:
        pass


class _FlushMark:
    """Stdout that remembers whether the hook output reached the harness."""

    def __init__(self, stream):
        self.stream = stream
        self.flushed = False

    def write(self, text):
        return self.stream.write(text)

    def flush(self):
        self.stream.flush()
        self.flushed = True


def _exit_after_flush(mark):
    """Past `_EXIT_AFTER_FLUSH_S`, a run ends as soon as its output is flushed,
    even inside a blocked lock wait; until then it keeps its cooperative path."""
    def on_alarm(_signum, _frame):
        if mark.flushed:
            os._exit(0)
    try:
        signal.signal(signal.SIGALRM, on_alarm)
        signal.setitimer(signal.ITIMER_REAL,
                         max(0.01, _STARTED + _EXIT_AFTER_FLUSH_S - time.monotonic()), 0.25)
    except (OSError, ValueError, AttributeError):
        pass


def _is_helper_process():
    """F-100c: a title/summary refresher or memory distiller runs `claude -p` with the
    user's prompt text inside ITS prompt, so the trailer would make the helper look like
    a receiver (measured 2026-09-03: a phantom `notice` under a cairn summary worker's
    sid). Registered workers and child sessions are not receivers either."""
    # NOT `CLAUDE_CODE_CHILD_SESSION`: a herdr-started interactive depth-0 child carries
    # it too (measured 2026-09-03 on hearting-46 itself), and such a child is exactly the
    # receiver this record exists for.
    env = os.environ
    return (env.get("FLEET_TITLE_REFRESH") == "1" or env.get("MEM_DISTILL") == "1"
            or env.get("AGENT_SESSION_ROLE", "").lower() == "worker"
            or bool(env.get("AGENT_DISPATCH_DEPTH")))


def main(exit_guard=False):
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if _is_helper_process():
        return
    payload = _read_stdin_json()
    out = _FlushMark(sys.stdout)
    if exit_guard:
        _exit_after_flush(out)
    if mode == "post-tool":
        # Registered on PostToolUse and PostToolUseFailure (PG-48): a failed
        # tool's end is a boundary too, but a failed SendMessage sent nothing.
        failed = payload.get("hook_event_name") == "PostToolUseFailure"
        event = "PostToolUseFailure" if failed else "PostToolUse"
        try:
            if not failed:
                handle_post_tool(payload)
        finally:
            mod = _peer_message_module()
            # PostToolUse runs inside subagents too, but its session_id names the
            # parent conversation. Leave parent-directed messages pending until
            # that conversation's next receiving hook. SendMessage recording above
            # remains active for subagent calls.
            if mod and not str(payload.get("agent_id") or "").strip():
                session_id = payload.get("session_id")
                owed = []
                note_turn_tool(payload)
                settle_stop_records(payload)

                def owed_context():
                    # After the exact peer-body claims, within what they left.
                    try:
                        text, batches = tool_boundary_records(
                            session_id, min(_deadline(), time.monotonic() + _RECORD_SHARE_S))
                    except Exception:  # noqa: BLE001 - the prompt sweep still carries them
                        return ""
                    owed.extend(batches)
                    return text

                shown = None
                try:
                    shown = mod.emit_peer_context({"harness": "claude", "session_id": session_id},
                                                  event, extra_context=owed_context,
                                                  native_delivery=True, deadline=_deadline(),
                                                  stream=out)
                finally:
                    if owed:
                        settle_tool_boundary_records(session_id, owed, out.flushed)
                    if out.flushed:
                        note_turn_received(payload, owed, _peer_items(shown))
    elif mode == "stop":
        handle_stop(payload, out)
    elif mode == "prompt":
        # The cross-session `notice` record path runs first and unconditionally:
        # the sweep must never be able to swallow it.
        try:
            handle_prompt(payload)
        except Exception:
            pass
        prompt = payload.get("prompt") or ""
        with contextlib.suppress(Exception):
            turn = _turn(payload)
            peer = _carrier().peer_report_item(prompt, turn[0], "claude") if turn else None
            if peer:
                note_turn_received(payload, [], [peer])
        mod = _peer_message_module()
        guidance = ""
        if mod and ("peer-from:" in prompt or "runtime-from:" in prompt
                    or "cross-session-message" in prompt):
            guidance = mod.receiver_guidance(
                prompt, {"harness": "claude", "session_id": payload.get("session_id")},
                cwd=payload.get("cwd") or os.getcwd())
        rows = []

        def extra_context():
            # The watch sweep is the fallback carrier (asyncRewake is the primary),
            # so it runs after the exact peer-message claims, in what they left.
            found = sweep_undelivered(
                payload, deadline=min(_deadline(), time.monotonic() + _SWEEP_SHARE_S))
            sweep = ""
            if found:
                sweep = "\n".join(
                    ["[peer-steward] undelivered watch receipts:"]
                    + [_typed_watch_line(row) for row in found])
            # Only receipts whose line was built may be ACKed after the flush
            # (review M2): an ACK with no display behind it would silence them.
            rows.extend(found)
            return "\n".join(filter(None, [sweep, guidance]))

        if mod:
            shown = mod.emit_peer_context(
                {"harness": "claude", "session_id": payload.get("session_id")},
                "UserPromptSubmit", extra_context=extra_context,
                native_delivery=True, deadline=_deadline(), stream=out)
            if out.flushed:
                note_turn_received(payload, [], _peer_items(shown))
        else:
            extra = extra_context()
            if extra:
                out.write(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit", "additionalContext": extra}},
                    ensure_ascii=False) + "\n")
                out.flush()
        if rows and out.flushed:
            ack_rows(payload, rows)


if __name__ == "__main__":
    _STARTED = _process_started()
    try:
        main(exit_guard=True)
    except BaseException:
        pass
    sys.exit(0)
