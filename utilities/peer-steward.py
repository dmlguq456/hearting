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


def _record(*, to_harness, to_name, kind, ref=None, summary_text=None):
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
        to_harness=to_harness,
        to_session_id=None,
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
    # S6: one record at wait start, append-only, never updated on return.
    _record(to_harness="unknown", to_name=target, kind="watch", ref=args.ref)

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

    cmd = ["herdr", "agent", "start", "--kind", args.kind, "--pane", args.pane]
    if full_agent_args:
        cmd += ["--"] + full_agent_args

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=args.cwd or None)
    except (OSError, subprocess.SubprocessError):
        return _unavailable("herdr-invocation-failed")

    started = proc.returncode == 0
    _record(
        to_harness=args.kind, to_name=args.name, kind="steer",
        summary_text=f"[start] {args.name} kind={args.kind} mode={mode}",
    )
    print(
        f"started={str(started).lower()} agent={args.kind} name={args.name} "
        f"pane={args.pane} permission_mode={mode}"
    )
    return 0


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
