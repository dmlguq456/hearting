#!/usr/bin/env python3
"""SD-122 peer-message ledger recorder.

Two subcommands, dispatched by argv[1]:
  post-tool  -- PostToolUse hook. Records one peer_message_v1 for each
                SendMessage tool call.
  prompt     -- UserPromptSubmit hook. Records one `notice` peer_message_v1
                when the injected prompt carries a cross-session message, and
                sweeps up to five un-acked detached-watch receipts into
                `additionalContext` (SD-122 (10) fallback carrier: the
                `asyncRewake` hook may die or the session may restart, but the
                receipt on disk does not).

Fail-soft over its entire body: any exception is swallowed and the process
exits 0. Never prints permissionDecision/deny. The prompt path never writes
to stdout (UserPromptSubmit stdout is injected into the prompt).
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent
_UTILITIES_DIR = _HOOKS_DIR.parent / "utilities"
_PEER_MESSAGE_PY = _UTILITIES_DIR / "peer-message.py"
_PEER_STEWARD_PY = _UTILITIES_DIR / "peer-steward.py"
_SWEEP_MAX = 5


def _peer_message_module():
    """The ledger tool as a module (trailer parser + registry-name lookup), fail-soft."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_peer_message", str(_PEER_MESSAGE_PY))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None

_PREFIX_KIND = {
    "[steer]": "steer",
    "[handoff]": "handoff",
    "[gate]": "gate-relay",
}


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
    first_line = (body or "").splitlines()[0].strip() if body else ""
    for prefix, kind in _PREFIX_KIND.items():
        if first_line.startswith(prefix):
            return kind
    return "steer"


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
        timeout=5,
        capture_output=True,
    )


_CROSS_SESSION_RE = re.compile(r"<cross-session-message\s+from=\"([^\"]*)\"")
_CROSS_SESSION_NAME_RE = re.compile(r"<cross-session-message\s[^>]*?from-name=\"([^\"]*)\"")


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
        timeout=5,
        capture_output=True,
    )


def _typed_watch_line(row):
    agent = row.get("agent") or {}
    return (
        f"state={row.get('receipt_state') or 'unknown'} agent={agent.get('harness', '-')} "
        f"session_id={agent.get('session_id', '-')} name={agent.get('name') or row.get('target', '-')} "
        f"pane={agent.get('pane', '-')} watch_id={row.get('watch_id', '-')} "
        f"receipt={row.get('receipt', '-')}"
    )


def _sweep_env(session_id):
    env = dict(os.environ)
    env["CLAUDE_CODE_SESSION_ID"] = str(session_id)
    return env


def sweep_undelivered(payload):
    """List un-acked watch receipts for THIS session (bounded, read-only).

    Fail-soft in its own right: a broken or slow `peer-steward.py` must never
    cost the user a prompt. Acking is a separate step (`ack_rows`) that runs
    only AFTER the context has been written: an ack with no display behind it
    would silence the fallback forever (review M2).
    """
    session_id = payload.get("session_id")
    if not session_id:
        return []
    try:
        proc = subprocess.run(
            [sys.executable, str(_PEER_STEWARD_PY), "status", "--undelivered", "--json"],
            capture_output=True, text=True, timeout=5, env=_sweep_env(session_id),
        )
        rows = (json.loads(proc.stdout or "{}") or {}).get("watches") or []
    except Exception:
        return []
    return rows[:_SWEEP_MAX]


def ack_rows(payload, rows):
    session_id = payload.get("session_id")
    if not session_id:
        return
    env = _sweep_env(session_id)
    for row in rows:
        watch_id = row.get("watch_id")
        if not watch_id:
            continue
        try:
            subprocess.run(
                [sys.executable, str(_PEER_STEWARD_PY), "ack", str(watch_id),
                 "--carrier", "userprompt-sweep"],
                capture_output=True, timeout=5, env=env,
            )
        except Exception:
            continue
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


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if _is_helper_process():
        return
    payload = _read_stdin_json()
    if mode == "post-tool":
        try:
            handle_post_tool(payload)
        finally:
            mod = _peer_message_module()
            if mod:
                mod.emit_peer_context({"harness": "claude", "session_id": payload.get("session_id")},
                                      "PostToolUse")
    elif mode == "prompt":
        # The cross-session `notice` record path runs first and unconditionally:
        # the sweep must never be able to swallow it.
        try:
            handle_prompt(payload)
        except Exception:
            pass
        rows = sweep_undelivered(payload)
        extra = ""
        if rows:
            # The only stdout this path ever writes, and only when something is
            # actually pending. Written and flushed BEFORE the acks (review M2).
            extra = "\n".join(
                    ["[peer-steward] undelivered watch receipts:"]
                    + [_typed_watch_line(row) for row in rows]
                )
        mod = _peer_message_module()
        if mod:
            mod.emit_peer_context({"harness": "claude", "session_id": payload.get("session_id")},
                                  "UserPromptSubmit", extra_context=extra)
        elif extra:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit", "additionalContext": extra}}, ensure_ascii=False))
            sys.stdout.flush()
        if rows:
            ack_rows(payload, rows)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
