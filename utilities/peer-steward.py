#!/usr/bin/env python3
"""SD-122 (9) steward wait/start over herdr.

Checked wrapper around `herdr agent wait|start` — no self-written sleep or
poll loop, event-driven only. Ledger writes go through
`utilities/peer-message.py`'s own `cmd_record`, so the ledger root is always
resolved via `dispatch_contract.resolve_dispatch_state_root` exactly as the
writer of every other peer_message_v1 record resolves it — never a
hardcoded stable literal.
"""
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_UTILITIES_DIR = Path(__file__).resolve().parent

_PM_SPEC = importlib.util.spec_from_file_location(
    "peer_message", str(_UTILITIES_DIR / "peer-message.py")
)
peer_message = importlib.util.module_from_spec(_PM_SPEC)
_PM_SPEC.loader.exec_module(peer_message)

_DEFAULTS_SPEC = importlib.util.spec_from_file_location(
    "dispatch_defaults", str(_UTILITIES_DIR / "dispatch-defaults.py")
)
DEFAULTS = importlib.util.module_from_spec(_DEFAULTS_SPEC)
_DEFAULTS_SPEC.loader.exec_module(DEFAULTS)

_PERMISSION_FLAGS = {
    "claude": ["--permission-mode", "bypassPermissions"],
    "codex": ["--dangerously-bypass-approvals-and-sandbox"],
    "opencode": ["--auto"],
}


def _current_session_identity():
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return os.environ["CLAUDE_CODE_SESSION_ID"], "claude"
    if os.environ.get("CODEX_THREAD_ID"):
        return os.environ["CODEX_THREAD_ID"], "codex"
    if os.environ.get("OPENCODE_SESSION_ID"):
        return os.environ["OPENCODE_SESSION_ID"], "opencode"
    if os.environ.get("AGENT_SESSION_ID"):
        return os.environ["AGENT_SESSION_ID"], "unknown"
    return "", "unknown"


def _project_of(cwd):
    if not cwd:
        return ""
    return os.path.basename(str(cwd).rstrip("/"))


def _fallback():
    return "claude-native-notify-idle" if os.environ.get("CLAUDE_CODE_SESSION_ID") else "poll-fallback"


def _default_permission_mode():
    try:
        cfg = DEFAULTS.load_and_validate(
            DEFAULTS.default_config_path(), DEFAULTS.default_topology_path()
        )
        return DEFAULTS.query_steward_child_permission_mode(cfg)
    except Exception:
        return DEFAULTS.DEFAULT_STEWARD_CHILD_PERMISSION_MODE


def _from_name(from_harness, from_sid):
    """The sender's stable registry name for the ledger (`hearting-46`); Claude only
    today — Codex/OpenCode mint no runtime name (F-100c keeps that gap honest: None)."""
    if from_harness == "claude":
        try:
            return peer_message.claude_session_name(from_sid)
        except Exception:
            return None
    return None


def _resolve_target(target):
    """`herdr agent get <target>` → (harness, session_id, name); every miss is None.
    herdr reports an id for Claude (UUID) and Codex (thread id), none for OpenCode
    (measured 2026-09-03) — so an OpenCode target keeps session_id=None and the pane
    pid probe in Fleet's herdr collector is what joins it."""
    if _herdr_missing():
        return None, None, None
    try:
        proc = subprocess.run(["herdr", "agent", "get", target], capture_output=True,
                              text=True, timeout=5)
        payload = json.loads(proc.stdout or "")
    except Exception:
        return None, None, None
    agent = (payload.get("result") or {}).get("agent") if isinstance(payload, dict) else None
    if not isinstance(agent, dict):
        return None, None, None
    harness = (agent.get("agent") or None)
    sid = (agent.get("agent_session") or {}).get("value") or None
    return harness, sid, agent.get("name") or None


def _record(*, to_harness, to_name, kind, ref=None, summary_text=None, to_session_id=None):
    from_sid, from_harness = _current_session_identity()
    body_file = None
    tmp_path = None
    if summary_text is not None:
        fd, tmp_path = tempfile.mkstemp(prefix="peer-steward-summary-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(summary_text)
        body_file = tmp_path
    ns = peer_message.argparse.Namespace(
        from_harness=from_harness,
        from_session_id=from_sid,
        from_project=_project_of(os.getcwd()),
        from_name=_from_name(from_harness, from_sid),
        to_harness=to_harness,
        to_session_id=to_session_id,
        to_name=to_name,
        kind=kind,
        surface="herdr",
        status="sent",
        receipt=None,
        ref=list(ref or []),
        body_file=body_file,
        body_stdin=False,
    )
    try:
        peer_message.cmd_record(ns)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _herdr_missing():
    return shutil.which("herdr") is None


def _unavailable(reason):
    print(f"herdr-unavailable reason={reason} fallback={_fallback()}")
    return 4


def _run_herdr_wait(target, until, timeout_ms):
    cmd = ["herdr", "agent", "wait", target]
    for state in until or []:
        cmd += ["--until", state]
    if timeout_ms is not None:
        cmd += ["--timeout", str(timeout_ms)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    # Measured 2026-09-02 (herdr 0.8.0): a successful wait prints its
    # `agent_info` JSON on stdout and exits 0, but `{"error":{"code":...}}`
    # goes to STDERR with exit 1.  Reading stdout alone therefore turns every
    # real `timeout` and `agent_not_found` into `herdr-unavailable`, which is
    # the wrong exit code (4 instead of 3/2) and the wrong fallback advice.
    for stream in (proc.stdout, proc.stderr):
        try:
            payload = json.loads(stream)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def cmd_wait(args):
    target = args.target
    # S6: one record at wait start, append-only, never updated on return. F-100c: the
    # target is resolved through herdr first so the record carries an exact session id
    # (Fleet joins sent/recv and the `←` subtitle on it), the name being only a label.
    t_harness, t_sid, _t_name = _resolve_target(target)
    _record(to_harness=t_harness or "unknown", to_name=target, kind="watch", ref=args.ref,
            to_session_id=t_sid)

    if _herdr_missing():
        return _unavailable("herdr-not-found")

    payload = _run_herdr_wait(target, args.until, args.timeout)
    if not isinstance(payload, dict):
        return _unavailable("herdr-protocol-error")

    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        if code == "timeout":
            print(f"state=timeout agent=- session_id=- name={target} pane=-")
            return 3
        if code == "agent_not_found":
            print(f"state=agent-not-found agent=- session_id=- name={target} pane=-")
            return 2
        return _unavailable(f"herdr-error-code-{code or 'unknown'}")

    result = payload.get("result")
    agent = result.get("agent") if isinstance(result, dict) else None
    if not isinstance(agent, dict):
        return _unavailable("herdr-malformed-result")

    state = agent.get("agent_status") or "unknown"
    if state not in {"idle", "done", "blocked", "working", "unknown"}:
        state = "unknown"
    harness = agent.get("agent") or "-"
    session_id = (agent.get("agent_session") or {}).get("value") or "-"
    name = agent.get("name") or "-"
    pane = agent.get("pane_id") or "-"
    print(f"state={state} agent={harness} session_id={session_id} name={name} pane={pane}")
    return 0


def cmd_start(args):
    if _herdr_missing():
        return _unavailable("herdr-not-found")

    mode = args.permission_mode or _default_permission_mode()
    agent_args = list(getattr(args, "agent_args", None) or [])
    prefix = list(_PERMISSION_FLAGS.get(args.kind, [])) if mode == "bypass" else []
    full_agent_args = prefix + agent_args

    # herdr `agent start <NAME> --kind --pane` — the display name is a required
    # positional (herdr 0.8+ prints `unknown option: <kind>` and starts nothing when
    # it is missing; measured 2026-09-03, F-100 comms test).
    cmd = ["herdr", "agent", "start", args.name, "--kind", args.kind, "--pane", args.pane]
    if full_agent_args:
        cmd += ["--"] + full_agent_args

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=args.cwd or None)
    except (OSError, subprocess.SubprocessError):
        return _unavailable("herdr-invocation-failed")

    started = proc.returncode == 0
    # F-100c: herdr answers `agent_started` with the agent block; a Claude/Codex id is
    # usually present already, OpenCode's never is (measured) — record what we got.
    started_sid = None
    try:
        agent = (json.loads(proc.stdout or "").get("result") or {}).get("agent") or {}
        started_sid = (agent.get("agent_session") or {}).get("value") or None
    except Exception:
        started_sid = None
    _record(
        to_harness=args.kind, to_name=args.name, kind="steer",
        summary_text=f"[start] {args.name} kind={args.kind} mode={mode}",
        to_session_id=started_sid,
    )
    print(
        f"started={str(started).lower()} agent={args.kind} name={args.name} "
        f"pane={args.pane} permission_mode={mode}"
    )
    return 0


def cmd_prompt(args):
    """F-100c — the harness-neutral steward send: `herdr agent prompt <target> <body +
    trailer>`, recorded with the target's exact session id (herdr `agent get`) and the
    sender's name. The trailer lets the receiving harness write its own `notice`."""
    if _herdr_missing():
        return _unavailable("herdr-not-found")
    if args.body_file:
        body = open(args.body_file, encoding="utf-8", errors="replace").read()
    elif args.body_stdin:
        body = sys.stdin.read()
    else:
        body = args.text or ""
    if not body.strip():
        print("prompted=false reason=empty-body")
        return 1
    from_sid, from_harness = _current_session_identity()
    text = body.rstrip("\n")
    if not args.no_trailer:
        text += "\n\n" + peer_message.peer_trailer(
            from_harness, from_sid, _from_name(from_harness, from_sid))
    t_harness, t_sid, _t_name = _resolve_target(args.target)
    try:
        proc = subprocess.run(["herdr", "agent", "prompt", args.target, text],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return _unavailable("herdr-invocation-failed")
    prompted = proc.returncode == 0
    first = body.strip().splitlines()[0] if body.strip() else ""
    kind = "steer"
    for prefix, k in (("[steer]", "steer"), ("[handoff]", "handoff"), ("[gate]", "gate-relay")):
        if first.startswith(prefix):
            kind = k
    _record(to_harness=t_harness or "unknown", to_name=args.target, kind=kind,
            summary_text=body, to_session_id=t_sid, ref=args.ref)
    print(f"prompted={str(prompted).lower()} target={args.target} "
          f"to_harness={t_harness or '-'} to_session_id={t_sid or '-'} kind={kind}")
    return 0 if prompted else 1


def cmd_steward(args):
    """F-100c — explicit steward mode switch. Every steer/handoff/watch SEND already
    raises the flag implicitly; `on` raises it before the first send (so Fleet shows
    the yellow tag the moment a session takes the role), `off` releases it."""
    sid, harness = _current_session_identity()
    if not sid:
        print("steward=unchanged reason=no-session-identity")
        return 1
    if args.state == "on":
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ok = peer_message.mark_steward(harness, sid, {"harness": "unknown", "name": "-"},
                                       "watch", ts)
        print(f"steward={'on' if ok else 'unchanged'} harness={harness} session_id={sid}")
        return 0 if ok else 1
    rc = peer_message.cmd_release(peer_message.argparse.Namespace(harness=harness, session_id=sid))
    print(f"steward=off harness={harness} session_id={sid}")
    return rc


def build_parser():
    parser = argparse.ArgumentParser(prog="peer-steward")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_wait = sub.add_parser("wait")
    p_wait.add_argument("target")
    p_wait.add_argument("--until", action="append", default=[])
    p_wait.add_argument("--timeout", type=int, default=None)
    p_wait.add_argument("--ref", action="append", default=[])
    p_wait.set_defaults(func=cmd_wait)

    p_start = sub.add_parser("start")
    p_start.add_argument("name")
    p_start.add_argument("--kind", required=True, choices=("claude", "codex", "opencode"))
    p_start.add_argument("--pane", required=True)
    p_start.add_argument("--cwd", default=None)
    p_start.add_argument("--permission-mode", choices=("bypass", "inherit"), default=None)
    p_start.set_defaults(func=cmd_start)

    p_prompt = sub.add_parser("prompt")
    p_prompt.add_argument("target")
    p_prompt.add_argument("text", nargs="?", default=None)
    p_prompt.add_argument("--body-file", default=None)
    p_prompt.add_argument("--body-stdin", action="store_true")
    p_prompt.add_argument("--no-trailer", action="store_true")
    p_prompt.add_argument("--ref", action="append", default=[])
    p_prompt.set_defaults(func=cmd_prompt)

    p_mode = sub.add_parser("steward")
    p_mode.add_argument("state", choices=("on", "off"))
    p_mode.set_defaults(func=cmd_steward)

    return parser


def _split_agent_args(argv):
    """Split raw argv on a literal `--`: everything after it is agent-args,
    passed through untouched. argparse's own REMAINDER nargs greedily
    swallows *every* remaining token — including options meant for
    peer-steward itself, like `--kind`/`--pane` — from the first positional
    onward, so the split must happen before argparse ever sees the args.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in argv:
        idx = argv.index("--")
        return argv[:idx], argv[idx + 1:]
    return argv, []


def main(argv=None):
    parsed_argv, agent_args = _split_agent_args(argv)
    args = build_parser().parse_args(parsed_argv)
    args.agent_args = agent_args
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
