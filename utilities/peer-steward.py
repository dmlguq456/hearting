#!/usr/bin/env python3
"""SD-122 steward surfaces over herdr: (9) wait/start, (10) watch/join/status/rearm/ack.

Checked wrapper around `herdr agent wait|get|start` — no self-written sleep or
poll loop, event-driven only. Ledger writes go through
`utilities/peer-message.py`'s own `cmd_record`, so the ledger root is always
resolved via `dispatch_contract.resolve_dispatch_state_root` exactly as the
writer of every other peer_message_v1 record resolves it — never a
hardcoded stable literal.

(10) adds three separable lifetimes. `watch` spawns a detached watcher whose
lifetime is not the caller's tool-task lifetime (backgrounding `wait` lost it
to Claude Code interrupt/lifecycle kills 3/3, hearting-21 2026-09-02~03); the
watcher fixes the completion fact in an immutable disk receipt that outlives
it; and `ack` makes an at-least-once wake idempotent to display. `wait` keeps
its bounded-foreground semantics unchanged.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_UTILITIES_DIR = Path(__file__).resolve().parent
_UNSET_NAME = object()

_PM_SPEC = importlib.util.spec_from_file_location(
    "peer_message", str(_UTILITIES_DIR / "peer-message.py")
)
peer_message = importlib.util.module_from_spec(_PM_SPEC)
_PM_SPEC.loader.exec_module(peer_message)

sys.path.insert(0, str(_UTILITIES_DIR))
from dispatch_contract import process_start_ticks  # noqa: E402
from parent_next_directive import steward_fields  # noqa: E402

_DEFAULTS_SPEC = importlib.util.spec_from_file_location(
    "dispatch_defaults", str(_UTILITIES_DIR / "dispatch-defaults.py")
)
DEFAULTS = importlib.util.module_from_spec(_DEFAULTS_SPEC)
_DEFAULTS_SPEC.loader.exec_module(DEFAULTS)

# herdr runs one server per session (`herdr session list`), each with its own
# socket and its own pane namespace -- `w1:p1` names a different pane in every
# one of them. A bare `herdr ...` call always reaches the default server, so on
# a machine running more than one session every steward surface either reports
# `agent_not_found` for a pane that plainly exists, or resolves the id against
# the wrong server. Every herdr invocation below therefore carries the selected
# session, and the environment variable is what a detached watcher re-armed
# from a hook inherits.
_HERDR_SESSION = os.environ.get("AGENT_HERDR_SESSION") or None


def _herdr_argv(*args):
    """`herdr [--session <name>] <args...>` for the selected herdr session."""
    argv = ["herdr"]
    if _HERDR_SESSION:
        argv += ["--session", _HERDR_SESSION]
    return argv + list(args)


_PERMISSION_FLAGS = {
    "claude": ["--permission-mode", "bypassPermissions"],
    "codex": ["--dangerously-bypass-approvals-and-sandbox"],
    "opencode": ["--auto"],
}


def _current_session_identity():
    """`(session_id, harness)` — delegates to `dispatch_parent_completion
    .interactive_parent_identity` (the portable first-source-of-truth, F-<next> plan §3 B-3)
    when it can resolve one unambiguous caller harness; falls back to the prior
    claude > codex > opencode > AGENT_SESSION_ID priority otherwise (an explicit caller
    harness env still wins there too, so a genuinely ambiguous or unset environment keeps
    its exact prior behavior)."""
    try:
        from dispatch_parent_completion import interactive_parent_identity
        harness, sid = interactive_parent_identity()
        if sid:
            return sid, harness
    except Exception:   # caller-harness-ambiguous/invalid -> fall through to legacy order
        pass
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


def _session_registry():
    """Lazy-import `tools/fleet/session_registry.py` (B-1). Fleet's `tools/` tree
    is not on `sys.path` by default here, so this inserts it the same way
    `peer-message.py:412 steward_marker_roots` reaches `fleet.collectors.peer_messages`
    — `Path(__file__).resolve().parent.parent / "tools"` — rather than at module
    import time, so an install without the Fleet tree does not take down
    peer-steward entirely. Any failure (missing tree, import error) yields None."""
    try:
        tools_dir = Path(__file__).resolve().parent.parent / "tools"
        if tools_dir.is_dir() and str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))
        from fleet import session_registry
        return session_registry
    except Exception:
        return None


def _from_name(from_harness, from_sid):
    """The sender's stable registry name for the ledger (`hearting-46`). Claude reads
    its native session registry directly; Codex/OpenCode (C-3) read the hearting-owned
    `session_registry` (B-1) the same way. Any other harness, or any failure along
    either path, yields None — a name is never guessed (F-100c)."""
    if from_harness == "claude":
        try:
            return peer_message.claude_session_name(from_sid)
        except Exception:
            return None
    if from_harness in ("codex", "opencode"):
        registry = _session_registry()
        if registry is None:
            return None
        try:
            return registry.name_for_session_id(from_sid, from_harness)
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
        proc = subprocess.run(_herdr_argv("agent", "get", target), capture_output=True,
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


def _record(*, to_harness, to_name, kind, ref=None, summary_text=None,
            receipt=None, status="sent", from_identity=None, to_session_id=None,
            to_pane=None, transfer_ref=None, from_name=_UNSET_NAME):
    """Write one peer_message_v1 row.

    `from_identity` exists for the detached watcher: `_current_session_identity`
    reads the *environment*, and a watcher re-armed from a hook does not
    necessarily inherit the steward's environment. The watcher therefore carries
    the steward identity on argv and passes it here explicitly. `wait`/`start`
    keep the environment-derived default. `to_session_id` (F-100c) is the
    target's exact session id resolved through `herdr agent get`.
    """
    if from_identity is not None:
        from_sid, from_harness, from_project = from_identity
    else:
        from_sid, from_harness = _current_session_identity()
        from_project = _project_of(os.getcwd())
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
        from_project=from_project,
        from_name=_from_name(from_harness, from_sid) if from_name is _UNSET_NAME else from_name,
        to_harness=to_harness,
        to_session_id=to_session_id,
        to_name=to_name,
        to_pane=to_pane,
        kind=kind,
        surface="herdr",
        status=status,
        receipt=receipt,
        ref=list(ref or []),
        body_file=body_file,
        body_stdin=False,
        transfer_ref=transfer_ref,
    )
    try:
        peer_message.cmd_record(ns)
    finally:
        if tmp_path is not None:
            try:
                # destructive-ok: reason=this call's own mkstemp summary file, already consumed by cmd_record; boundary=<TMPDIR>/peer-steward-summary-<random>
                os.unlink(tmp_path)
            except OSError:
                pass


def _herdr_missing():
    return shutil.which("herdr") is None


def _unavailable(reason):
    print(f"herdr-unavailable reason={reason} fallback={_fallback()}")
    return 4


def _run_herdr_wait(target, until, timeout_ms):
    cmd = _herdr_argv("agent", "wait", target)
    for state in until or []:
        cmd += ["--until", state]
    if timeout_ms is not None:
        cmd += ["--timeout", str(timeout_ms)]
    global _LAST_HERDR_EXIT
    _LAST_HERDR_EXIT = None
    try:
        # A wedged herdr socket must not hang the caller past its own bound
        # (review round 1, minor 3); an unbounded wait stays unbounded.
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=(timeout_ms / 1000 + 15) if timeout_ms is not None else None)
    except (OSError, subprocess.SubprocessError):
        return None
    _LAST_HERDR_EXIT = proc.returncode
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


_AGENT_STATES = ("idle", "done", "blocked", "working", "unknown")
_LAST_HERDR_EXIT = None          # real `herdr agent wait` return code of the last call (m3)
_HERDR_GET_TIMEOUT_SECONDS = 15  # `watch` pre-check must never hang the caller (M3)
_CLAIM_TIMEOUT_MS = 15_000       # dedupe claim acquisition bound (M3)


def _typed_line(state, harness, session_id, name, pane):
    """The (9) five-field typed line. Extracted from `cmd_wait` verbatim so
    `wait`, `watch`'s watcher, `join` and `status` cannot drift apart; the
    pre-existing A51-1 assertions are the byte-identity guard."""
    return f"state={state} agent={harness} session_id={session_id} name={name} pane={pane}"


def _interpret_payload(payload, target):
    """Map a herdr payload onto ((9) state, agent fields, exit code).

    Returns `(state, agent, exit_code, unavailable_reason)`. `unavailable_reason`
    is non-None only when the caller should render `herdr-unavailable`.
    """
    blank = {"harness": "-", "session_id": "-", "name": target, "pane": "-"}
    if not isinstance(payload, dict):
        return "herdr-unavailable", blank, 4, "herdr-protocol-error"

    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        if code == "timeout":
            return "timeout", blank, 3, None
        if code == "agent_not_found":
            return "agent-not-found", blank, 2, None
        return "herdr-unavailable", blank, 4, f"herdr-error-code-{code or 'unknown'}"

    result = payload.get("result")
    agent = result.get("agent") if isinstance(result, dict) else None
    if not isinstance(agent, dict):
        return "herdr-unavailable", blank, 4, "herdr-malformed-result"

    state = agent.get("agent_status") or "unknown"
    if state not in _AGENT_STATES:
        state = "unknown"
    return state, {
        "harness": agent.get("agent") or "-",
        "session_id": (agent.get("agent_session") or {}).get("value") or "-",
        "name": agent.get("name") or "-",
        "pane": agent.get("pane_id") or "-",
    }, 0, None


def _mark_observed(to_harness, to_session_id, to_name, from_identity=None):
    """F-100c-2 — the ONE place `source=watch` steward evidence is written: after
    herdr answered about a real target (`wait` returned a typed state other than
    agent-not-found, or `watch` armed its watcher). The `kind=watch` ledger row itself
    is only the message; `record` never marks (review round 1, #1/#2)."""
    if from_identity is not None:
        from_sid, from_harness = from_identity[0], from_identity[1]
    else:
        from_sid, from_harness = _current_session_identity()
    to = {"harness": to_harness if to_harness and to_harness != "-" else "unknown",
          "session_id": to_session_id if to_session_id and to_session_id != "-" else None,
          "name": to_name}
    return peer_message.mark_steward(from_harness, from_sid, to, "watch", _utc_now(),
                                     source="watch")


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
    state, agent, code, reason = _interpret_payload(payload, target)
    if reason is not None:
        return _unavailable(reason)
    # Role evidence only now: herdr answered about a real target. A mistyped target
    # (agent-not-found) or an unavailable herdr leaves the ledger row and no flag.
    if state != "agent-not-found":
        _mark_observed(agent["harness"] if agent["harness"] != "-" else t_harness,
                       agent["session_id"] if agent["session_id"] != "-" else t_sid, target)
    print(_typed_line(state, agent["harness"], agent["session_id"], agent["name"], agent["pane"]))
    return code


# Hearting installs a launcher wrapper for Codex at `$CODEX_HOME/.harness/bin/codex` and
# puts that directory first on the shell PATH ("protected ingress"). Only that wrapper
# reaches `codex-managed-entry.py`, and only the managed entry writes the tier-1 session
# record — so a Codex started around the wrapper has no session id anywhere: no ledger
# endpoint, no board badge, nothing to steer by name.
#
# A shell only reads its startup files once. A herdr pane opened BEFORE the ingress was
# installed keeps the PATH it started with forever, and nothing can reach it afterwards —
# the profile cannot touch a process that is already running. Measured 2026-09-10 on this
# machine: `command -v codex` in a pane whose shell started 2026-08-24 answered
# `~/.local/bin/codex` (the vendor binary), while a pane opened 2026-09-09 answered with
# the wrapper. The install is correct; the old pane is simply older than it. Those panes
# can live for weeks, and every Codex started in one is silently unmanaged.
#
# So the launch re-establishes the ingress in the PANE, on the line before the agent
# starts. `herdr agent start` types a bare `codex …` into that same shell (measured: the
# started process's argv is `codex`, not an absolute path), so the pane's own PATH is what
# decides, and a terminal processes its input in order — the export is applied before the
# next line runs, without waiting on a clock. It is idempotent: prepending a directory
# that is already first changes nothing. Claude and OpenCode have no such wrapper and need
# nothing here.
_MANAGED_INGRESS = {"codex": ("CODEX_HOME", "~/.codex", ".harness/bin")}


def _managed_ingress_dir(kind):
    """The directory holding hearting's launcher wrapper for `kind`, or ``None``.

    Existence-checked: a harness with no wrapper installed must not have a PATH entry
    typed into someone's terminal on its behalf.
    """
    spec = _MANAGED_INGRESS.get(kind)
    if not spec:
        return None
    env_name, default_home, suffix = spec
    home = os.environ.get(env_name) or os.path.expanduser(default_home)
    directory = os.path.join(home, suffix)
    wrapper = os.path.join(directory, kind)
    return directory if os.path.isfile(wrapper) and os.access(wrapper, os.X_OK) else None


def _pane_has_agent(pane):
    """True when herdr already sees an agent in this pane.

    Typing into a pane that is running an agent would inject text into that agent's
    prompt. `herdr agent start` requires a bare shell prompt for the same reason, so this
    only declines to act where the start itself is going to refuse.
    """
    try:
        proc = subprocess.run(_herdr_argv("pane", "get", pane), capture_output=True,
                              text=True, timeout=5)
        payload = json.loads(proc.stdout or "")
    except Exception:
        return True          # unreadable pane: assume occupied, type nothing
    block = (payload.get("result") or {}).get("pane") if isinstance(payload, dict) else None
    if not isinstance(block, dict):
        return True
    return bool(block.get("agent"))


def _ensure_pane_ingress(pane, kind):
    """Put hearting's launcher wrapper first on the PANE's PATH. Returns a typed reason.

    ``None`` means nothing was needed or nothing was typed; any other value names why, and
    is carried into the launch receipt so an unmanaged launch can never be silent.
    """
    directory = _managed_ingress_dir(kind)
    if directory is None:
        return None
    if _pane_has_agent(pane):
        return "pane-occupied"
    line = 'export PATH="%s:$PATH"' % directory
    try:
        text = subprocess.run(_herdr_argv("pane", "send-text", pane, line),
                              capture_output=True, text=True, timeout=5)
        if text.returncode != 0:
            return "ingress-send-failed"
        enter = subprocess.run(_herdr_argv("pane", "send-keys", pane, "Enter"),
                               capture_output=True, text=True, timeout=5)
        if enter.returncode != 0:
            return "ingress-send-failed"
    except Exception:
        return "ingress-send-failed"
    return None


_MANAGED_ENTRY_MARK = "codex-managed-entry"
_MANAGED_ENV_MARK = "AGENT_CODEX_MANAGED_GATEWAY"


def _pane_is_managed(pane):
    """Did a hearting-managed entry actually run in this pane? Read, never inferred.

    Two shapes, because the managed launch has two: the entry process itself is
    `codex-managed-entry.py` in its own argv, and the app-server and TUI client it spawns
    carry `AGENT_CODEX_MANAGED_GATEWAY` in their environment instead. The first version of
    this check looked only for the env var on the foreground process and reported
    `managed=false` for a launch that was, in fact, managed (measured 2026-09-10) — the
    entry sets that variable for its CHILDREN, not for itself.
    """
    try:
        proc = subprocess.run(_herdr_argv("pane", "process-info", "--pane", pane),
                              capture_output=True, text=True, timeout=5)
        payload = json.loads(proc.stdout or "")
        info = (payload.get("result") or {}).get("process_info") or {}
        processes = info.get("foreground_processes") or []
    except Exception:
        return None
    for process in processes:
        if not isinstance(process, dict):
            continue
        argv = " ".join(str(part) for part in (process.get("argv") or []))
        if _MANAGED_ENTRY_MARK in argv or _MANAGED_ENTRY_MARK in str(process.get("cmdline") or ""):
            return True
        pid = process.get("pid")
        if not pid:
            continue
        try:
            with open("/proc/%d/environ" % int(pid), "rb") as fh:
                raw = fh.read()
        except Exception:
            continue
        if any(entry.startswith(_MANAGED_ENV_MARK.encode() + b"=")
               for entry in raw.split(b"\0")):
            return True
    return False if processes else None


# The harness flag that puts a launched session in a chosen directory. `herdr agent
# start` has none of its own — the agent it starts inherits the PANE's shell cwd — so
# `--cwd` used to move nothing but this CLI process: a session started with
# `--cwd <hearting>` came up in `SR_CorrNet`, the pane's own directory (measured
# 2026-09-10). Codex takes `-C/--cd`; Claude Code and OpenCode have no equivalent today,
# so for those `--cwd` is REFUSED rather than silently ignored. Launching an agent
# somewhere other than where the caller said is the failure this exists to prevent, and a
# refusal the caller can read beats a session quietly working in the wrong repository.
_CWD_FLAG = {"codex": "--cd"}


def cmd_start(args):
    if _herdr_missing():
        return _unavailable("herdr-not-found")

    pane_cwd = None
    cwd_flag = []
    if args.cwd:
        flag = _CWD_FLAG.get(args.kind)
        pane_cwd = os.path.realpath(os.path.expanduser(str(args.cwd)))
        if not flag:
            print(f"started=false reason=cwd-unsupported-by-{args.kind} "
                  f"agent={args.kind} name={args.name} pane={args.pane} cwd={pane_cwd} "
                  f"hint=start the pane in that directory, then start the agent")
            return 1
        if not os.path.isdir(pane_cwd):
            print(f"started=false reason=cwd-not-a-directory agent={args.kind} "
                  f"name={args.name} pane={args.pane} cwd={pane_cwd}")
            return 1
        cwd_flag = [flag, pane_cwd]

    # Before the agent is started, not after: this is the line that decides whether the
    # session that comes up is hearting-managed at all.
    ingress_note = _ensure_pane_ingress(args.pane, args.kind)

    mode = args.permission_mode or _default_permission_mode()
    agent_args = list(getattr(args, "agent_args", None) or [])
    prefix = list(_PERMISSION_FLAGS.get(args.kind, [])) if mode == "bypass" else []
    full_agent_args = prefix + cwd_flag + agent_args

    # herdr `agent start <NAME> --kind --pane` — the display name is a required
    # positional (herdr 0.8+ prints `unknown option: <kind>` and starts nothing when
    # it is missing; measured 2026-09-03, F-100 comms test).
    cmd = _herdr_argv("agent", "start", args.name, "--kind", args.kind, "--pane", args.pane)
    if full_agent_args:
        cmd += ["--"] + full_agent_args

    try:
        # `cwd=` here moves only this CLI process, never the launched agent — the agent
        # is put in place by `_CWD_FLAG` above. Kept because herdr itself resolves some
        # relative paths against its caller.
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=args.cwd or None)
    except (OSError, subprocess.SubprocessError):
        return _unavailable("herdr-invocation-failed")

    # F-100c: herdr answers `agent_started` with the agent block; a Claude/Codex id is
    # usually present already, OpenCode's never is (measured) — record what we got.
    # `started` needs exit 0 AND no error body (review round 1, #9): a herdr that exits
    # 0 with `{"error": …}` launched nobody.
    started_sid = None
    agent_block = None
    payload_error = None
    try:
        payload = json.loads(proc.stdout or "")
        if isinstance(payload, dict):
            payload_error = payload.get("error")
            agent_block = (payload.get("result") or {}).get("agent")
        if isinstance(agent_block, dict):
            started_sid = (agent_block.get("agent_session") or {}).get("value") or None
    except Exception:
        agent_block = None
    started = proc.returncode == 0 and not payload_error
    _record(
        to_harness=args.kind, to_name=args.name, kind="steer",
        summary_text=f"[start] {args.name} kind={args.kind} mode={mode}",
        to_session_id=started_sid,
    )
    # Launching a session is steward-role evidence (source=start); the `[start]` steer
    # row above is only the message and raises nothing by itself. Marked only from an
    # error-free payload that carries the agent block — a refused start, an error body
    # or an unparsable answer launched nothing we can point at.
    if started and isinstance(agent_block, dict):
        from_sid, from_harness = _current_session_identity()
        peer_message.mark_steward(
            from_harness, from_sid,
            {"harness": args.kind, "session_id": started_sid, "name": args.name},
            "start", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), source="start",
        )
    # `session_id=-` is the launch-time signal that this session will have no identity
    # anywhere downstream: no ledger endpoint, no board badge, nothing to steer by name.
    # It used to be visible only hours later as a nameless row (measured 2026-09-10: a
    # codex session started here came up unmanaged because the pane's PATH had no
    # hearting wrapper on it, so no tier-1 record was ever written). Saying it at the
    # launch is the difference between a known gap and a mystery.
    #
    # `cwd=` only when one was asked for: it is the receipt that the flag was honored,
    # and an unasked-for value would cost an extra herdr call on every start.
    # `managed=` is read off the started process, not inferred from how it was launched.
    # An unmanaged Codex writes no session record, so it has no id, no badge and no way to
    # be addressed later — that has to be visible at the launch, not discovered hours
    # later as a nameless row on the board.
    managed = "-"
    if started and _MANAGED_INGRESS.get(args.kind):
        verdict = _pane_is_managed(args.pane)
        managed = "unknown" if verdict is None else str(verdict).lower()
    print(
        f"started={str(started).lower()} agent={args.kind} name={args.name} "
        f"pane={args.pane} permission_mode={mode} session_id={started_sid or '-'} "
        f"managed={managed}"
        + (f" ingress={ingress_note}" if ingress_note else "")
        + (f" cwd={pane_cwd}" if pane_cwd else "")
    )
    return 0


# ---------------------------------------------------------------------------
# SD-122 (10) detached steward watch: watch / join / status / rearm / ack
# ---------------------------------------------------------------------------

_WATCH_SCHEMA = 1
_WATCH_STATES = _AGENT_STATES + ("timeout", "agent-not-found", "herdr-unavailable")
_JOIN_EXIT = {"timeout": 3, "agent-not-found": 2, "herdr-unavailable": 4}


def _watch_root():
    """`peer-watches/` beside `peer-messages/`, under the one resolved state root.

    Routed through `peer_message.peer_state_root()` on purpose: resolving the
    state root a second time here would let the ledger root and the watch root
    diverge whenever the resolver's inputs differ, and a watch whose receipt
    lives beside a different ledger is unfindable.
    """
    return peer_message.peer_state_root() / "peer-watches"


@dataclass(frozen=True)
class _WatchPaths:
    arm: Path
    lock: Path
    log: Path
    receipt: Path
    ack: Path


def _watch_paths(watch_id, root=None):
    root = root or _watch_root()
    return _WatchPaths(
        arm=root / f"{watch_id}.json",
        lock=root / f"{watch_id}.lock",
        log=root / f"{watch_id}.log",
        receipt=root / f"{watch_id}.receipt.json",
        ack=root / f"{watch_id}.ack.json",
    )


def _new_watch_id(steward_sid, target, armed_ts, nonce):
    raw = f"{steward_sid}|{target}|{armed_ts}|{nonce}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def _dedupe_key(steward_sid, target, until):
    # `until=[]` (herdr's default set) and `until=["idle","done","blocked"]` are
    # the same *behaviour* but stay distinct keys on purpose: (10) dedupes on the
    # "until 집합" as given, and silently normalizing them would suppress a
    # legitimate second watch.
    raw = f"{steward_sid}|{target}|{'|'.join(sorted(until or []))}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_json_atomic(path, obj):
    """tmp in the same directory -> fsync file -> rename -> fsync directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    dirfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


def _open_lock(path):
    """Open the lock file without truncating it.

    Never `open(path, "w")`: a truncating open against a lock another process is
    holding is silent corruption of shared state.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_zombie(pid):
    """A reaped-but-not-yet-collected watcher is dead, not pending.

    A zombie still has `/proc/<pid>/stat` with matching start ticks, so PID
    identity alone would call it alive. It holds no file locks, so `_alive`
    already rejects it; `cmd_join` cannot use the lock condition (it holds the
    lock itself) and needs this explicitly.
    """
    try:
        raw = (Path("/proc") / str(int(pid)) / "stat").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    tail = raw[raw.rfind(")") + 2:].split()
    return bool(tail) and tail[0] == "Z"


def _pid_identity_ok(pid, pid_start):
    """§5.12 PID identity: the pid exists *and* its start ticks still match."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    observed = process_start_ticks(pid)
    if observed is None or str(observed) != str(pid_start):
        return False
    return not _is_zombie(pid)


def _lock_held(lock_path):
    if not lock_path.exists():
        return False
    try:
        fd = _open_lock(lock_path)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)   # one probe, one descriptor (review m1)


def _watcher_present(arm):
    """Is a watcher process for this watch still running?

    PID identity only, deliberately. `_alive` additionally requires the lock,
    which is correct for *reporting* (`status` must distinguish "spawned" from
    "actually watching") but wrong for any *decision to replace* the watch: for
    the few hundred milliseconds between spawn and flock a perfectly healthy
    watcher holds no lock, and treating that as death double-spawns on `watch`
    and replaces a live watch on `rearm`. This is the same spawn-latency trap
    that `cmd_join` avoids when it decides `watcher-dead`.
    """
    if not isinstance(arm, dict):
        return False
    watcher = arm.get("watcher") or {}
    return _pid_identity_ok(watcher.get("pid"), watcher.get("pid_start"))


def _alive(arm, root=None):
    """Both conditions, never either: a pid that matches but holds no lock is a
    watcher that died before flocking (or was never a watcher at all).

    This is the A56-4 reporting predicate. Use `_watcher_present` to decide
    whether a watch may be replaced.
    """
    if not _watcher_present(arm):
        return False
    return _lock_held(_watch_paths(arm["watch_id"], root).lock)


def _until_field(until):
    return "|".join(until) if until else "-"


def _armed_line(arm, paths):
    watcher = arm.get("watcher") or {}
    # The armed line states the caller's next action for the same reason a
    # launch receipt does (`utilities/parent_next_directive.py`): the watcher
    # carries this watch to one wake, so the caller yields instead of polling.
    return (
        f"watch_id={arm['watch_id']} state=armed target={arm['target']} "
        f"until={_until_field(arm.get('until'))} wake={arm.get('wake', 'none')} "
        f"pid={watcher.get('pid', '-')} pid_start={watcher.get('pid_start', '-')} "
        f"receipt={paths.receipt}\n"
        + steward_fields(
            arm.get("wake"), arm.get("watch_id"),
            agent_home=Path(__file__).resolve().parents[1],
            timeout_ms=arm.get("timeout"),
        )
    )


def _receipt_line(receipt, watch_id, paths):
    agent = receipt.get("agent") or {}
    return _typed_line(
        receipt.get("state", "unknown"), agent.get("harness", "-"),
        agent.get("session_id", "-"), agent.get("name", "-"), agent.get("pane", "-"),
    ) + f" watch_id={watch_id} receipt={paths.receipt}"


def _exit_for_state(state):
    return _JOIN_EXIT.get(state, 0)


def _herdr_get_timeout():
    try:
        value = float(os.environ.get("AGENT_PEER_STEWARD_HERDR_GET_TIMEOUT", _HERDR_GET_TIMEOUT_SECONDS))
    except ValueError:
        value = float(_HERDR_GET_TIMEOUT_SECONDS)
    return min(600.0, max(0.5, value))


def _run_herdr_get(target):
    try:
        proc = subprocess.run(
            _herdr_argv("agent", "get", target), capture_output=True, text=True,
            timeout=_herdr_get_timeout(),   # a wedged socket is `herdr-unavailable`, not a hang (M3)
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for stream in (proc.stdout, proc.stderr):
        try:
            payload = json.loads(stream)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def cmd_watch(args):
    root = _watch_root()
    root.mkdir(parents=True, exist_ok=True)
    identity = getattr(args, "steward_identity", None)
    if identity:
        # `rearm` (possibly running inside a hook whose environment carries no
        # session id) must keep the ORIGINAL steward identity: the dedupe key,
        # the receipt's `steward.session_id` and the sweep's `--undelivered`
        # filter all hang off it (review M1).
        steward_sid, steward_harness, steward_project = identity
    else:
        steward_sid, steward_harness = _current_session_identity()
        steward_project = _project_of(os.getcwd())
    target = args.target
    until = list(args.until or [])
    wake = args.wake
    if wake == "auto":
        wake = "hook" if os.environ.get("CLAUDE_CODE_SESSION_ID") else "none"

    # Dedupe is its own serialization point, entered BEFORE the herdr pre-checks
    # and held until the winning watch_id is published. A bare `O_EXCL` create
    # serializes only the first arm: two callers arriving while a claimed watcher
    # is dead would both decide to reclaim and both spawn -- the exact
    # double-spawn the claim exists to prevent. One blocking lock over
    # decide + spawn + publish removes that whole class, and it lives no longer
    # than this foreground call.
    #
    # `until=[]` (herdr's default set) and `until=["idle","done","blocked"]` are
    # the same *behaviour* but stay distinct keys on purpose: (10) dedupes on the
    # "until 집합" as given, and silently normalizing them would suppress a
    # legitimate second watch.
    claim = root / f"{_dedupe_key(steward_sid, target, until)}.arm"
    try:
        claim_fd = _open_lock(claim)
    except OSError as exc:
        return _unavailable(f"watch-claim-failed-{exc.errno}")
    try:
        if not _acquire_bounded(claim_fd, _CLAIM_TIMEOUT_MS):
            return _unavailable("watch-claim-contended")

        existing_id = ""
        try:
            existing_id = claim.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        if existing_id:
            existing_paths = _watch_paths(existing_id, root)
            existing_arm = _read_json(existing_paths.arm)
            if not existing_paths.receipt.exists() and _watcher_present(existing_arm):
                print(_already_armed_line(existing_id, existing_arm, existing_paths))
                return 0
            # Dead with no receipt, or already finished: the key is reclaimable.

        # herdr pre-checks. Nothing beyond the claim exists yet, so an early
        # return leaves no watch state behind.
        if _herdr_missing():
            return _unavailable("herdr-not-found")
        state, agent, _code, reason = _interpret_payload(_run_herdr_get(target), target)
        if state == "agent-not-found":
            print(_typed_line("agent-not-found", "-", "-", target, "-"))
            return 2
        if reason is not None:
            return _unavailable(reason)

        armed_ts = _utc_now()
        watch_id = _new_watch_id(steward_sid, target, armed_ts, os.urandom(8).hex())
        paths = _watch_paths(watch_id, root)
        log_fd = os.open(str(paths.log), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)

        # Take the watch lock here, before the spawn, and hand the open file
        # description to the watcher. A flock belongs to the description, not the
        # fd, so the child keeps holding it after this process closes its copy,
        # and the lock is held continuously from before `watch` prints its armed
        # line. Letting the watcher take its own lock leaves a spawn-latency
        # window in which a `join` wins the lock first, sees no receipt, and
        # reports a healthy watch as timed out or dead.
        lock_fd = _open_lock(paths.lock)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(lock_fd)
            os.close(log_fd)
            return _unavailable("watch-lock-contended")
        os.set_inheritable(lock_fd, True)

        argv = [
            sys.executable, str(Path(__file__).resolve()), "__watch-run",
            "--watch-id", watch_id, "--target", target,
            "--steward-harness", steward_harness, "--steward-session-id", steward_sid,
            "--steward-project", steward_project, "--armed-ts", armed_ts,
            "--rearm-count", str(args.rearm_count or 0),
            "--lock-fd", str(lock_fd),
        ]
        for state_name in until:
            argv += ["--until", state_name]
        if args.timeout is not None:
            argv += ["--timeout", str(args.timeout)]
        for ref in args.ref or []:
            argv += ["--ref", ref]
        if getattr(args, "rearmed_from", None):
            argv += ["--rearmed-from", args.rearmed_from]

        try:
            # Re-exec, never fork: a fork inherits the caller's process group,
            # file descriptors and interpreter state, which is precisely the
            # coupling this contract removes.
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True, pass_fds=(lock_fd,), cwd=str(root),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            os.close(log_fd)
            os.close(lock_fd)
            return _unavailable(f"watcher-spawn-failed-{getattr(exc, 'errno', 'unknown')}")
        os.close(log_fd)
        # Closing this copy does not release the flock: the child still holds a
        # descriptor for the same open file description.
        os.close(lock_fd)

        # Spawn first, then the immutable arm record already carrying the real
        # watcher identity. The reverse order would need a mutable record or a
        # second write. It is safe because the watcher takes target, until,
        # timeout and steward identity from argv and never reads the arm record.
        arm = {
            "schema_version": _WATCH_SCHEMA,
            "watch_id": watch_id,
            "target": target,
            "until": until,
            "timeout": args.timeout,
            "refs": list(args.ref or []),
            "steward": {
                "harness": steward_harness,
                "session_id": steward_sid,
                "project": steward_project,
            },
            "armed_ts": armed_ts,
            "wake": wake,
            "watcher": {"pid": proc.pid, "pid_start": process_start_ticks(proc.pid) or ""},
            "rearmed_from": getattr(args, "rearmed_from", None) or None,
            "rearm_count": int(args.rearm_count or 0),
        }
        _write_json_atomic(paths.arm, arm)

        # Publish the winning watch_id into the claim while still holding it.
        try:
            os.ftruncate(claim_fd, 0)
            os.lseek(claim_fd, 0, os.SEEK_SET)
            os.write(claim_fd, watch_id.encode("ascii"))
            os.fsync(claim_fd)
        except OSError:
            pass

        # `receipt=<watch_id>` is what distinguishes this `kind=watch` row from
        # (9) `wait`'s, which carries no receipt. Written while the claim is
        # still held so a caller killed after the spawn cannot leave a live
        # watcher with no arm row (review n4).
        _record(
            to_harness=agent["harness"] if agent["harness"] != "-" else "unknown",
            to_name=target, kind="watch", ref=args.ref, receipt=watch_id,
            from_identity=(steward_sid, steward_harness, steward_project),
        )
        # The watcher is armed on a target herdr resolved above: steward evidence.
        _mark_observed(agent["harness"], agent["session_id"], target,
                       from_identity=(steward_sid, steward_harness))
    finally:
        os.close(claim_fd)

    print(_armed_line(arm, paths))
    return 0


def _already_armed_line(watch_id, arm, paths):
    """`state=already-armed` must survive a missing arm record: the winner can
    die between spawn and the arm write, and crashing here would take down an
    otherwise healthy `watch` call."""
    if isinstance(arm, dict):
        watcher = arm.get("watcher") or {}
        target = arm.get("target", "-")
        until = _until_field(arm.get("until"))
        wake = arm.get("wake", "-")
        pid = watcher.get("pid", "-")
        pid_start = watcher.get("pid_start", "-")
    else:
        target = until = wake = pid = pid_start = "-"
    return (
        f"watch_id={watch_id} state=already-armed target={target} until={until} "
        f"wake={wake} pid={pid} pid_start={pid_start} receipt={paths.receipt}\n"
        # The hook arms only from a `watch` command printing `state=armed`.
        # This line is `already-armed` (or `alive`, from `rearm`), so it arms
        # nothing -- and it cannot prove the *earlier* arm succeeded either.
        # That earlier arm failing is exactly why a caller re-runs `watch`, so
        # answering `end-turn` here would hand back the most dangerous reply at
        # the moment the caller is trying to recover. A redundant wake is
        # absorbed by at-least-once ack; a missed one is lost work.
        + steward_fields(
            wake if wake != "-" else None, watch_id,
            agent_home=Path(__file__).resolve().parents[1],
            arms_hook=False,
            timeout_ms=arm.get("timeout") if isinstance(arm, dict) else None,
        )
    )


def cmd_watch_run(args):
    """The detached watcher. Reached only by re-exec from `cmd_watch`."""
    root = _watch_root()
    paths = _watch_paths(args.watch_id, root)
    if args.lock_fd is not None and args.lock_fd >= 0:
        lock_fd = args.lock_fd            # already flocked by the caller
    else:
        lock_fd = _open_lock(paths.lock)  # direct invocation fallback
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

    payload = _run_herdr_wait(args.target, args.until, args.timeout)  # exactly once
    state, agent, _code, reason = _interpret_payload(payload, args.target)
    if reason is not None:
        # A herdr invocation that cannot be interpreted still terminates the
        # watch: an un-receipted watcher waiting forever is the failure mode this
        # whole contract exists to remove.
        state = "herdr-unavailable"

    pid = os.getpid()
    receipt = {
        "schema_version": _WATCH_SCHEMA,
        "watch_id": args.watch_id,
        "target": args.target,
        "steward": {
            "harness": args.steward_harness,
            "session_id": args.steward_session_id,
            "project": args.steward_project,
        },
        "armed_ts": args.armed_ts,
        "done_ts": _utc_now(),
        "state": state,
        "agent": agent,
        "herdr_exit": _LAST_HERDR_EXIT if _LAST_HERDR_EXIT is not None else -1,
        # Self-describing: taken from this process, not from the arm record, so
        # a caller killed between spawn and the arm write still yields a usable
        # receipt.
        "watcher": {"pid": pid, "pid_start": process_start_ticks(pid) or ""},
        "rearmed_from": args.rearmed_from or None,
        "refs": list(args.ref or []),
    }
    _write_json_atomic(paths.receipt, receipt)

    _record(
        to_harness=agent["harness"] if agent["harness"] != "-" else "unknown",
        to_name=args.target, kind="notice", ref=args.ref, receipt=args.watch_id,
        status="received",
        summary_text=f"[notice] watch {args.watch_id} state={state} target={args.target}",
        from_identity=(args.steward_session_id, args.steward_harness, args.steward_project),
    )
    # No LOCK_UN, ever: lock release IS process exit, and the receipt rename
    # strictly precedes it. A watcher that unlocked before writing would make
    # every concurrent `join` report a dead watcher.
    os._exit(0)


class _JoinTimeout(Exception):
    pass


def _acquire_bounded(lock_fd, timeout_ms):
    """Blocking `flock`, bounded by a kernel timer. Zero polling.

    The SIGALRM handler must *raise*: measured on this runtime, a raising
    handler interrupts a blocking `fcntl.flock` (1.00s against a held lock),
    while a handler that only sets a flag would let the lock call resume and
    `--timeout` would never fire.
    """
    if timeout_ms is None:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return True

    def _alarm(_signum, _frame):
        raise _JoinTimeout()

    previous = signal.signal(signal.SIGALRM, _alarm)
    try:
        signal.setitimer(signal.ITIMER_REAL, max(0.001, timeout_ms / 1000.0))
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            return True
        except (_JoinTimeout, InterruptedError):
            return False
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def cmd_join(args):
    root = _watch_root()
    watch_id = args.watch_id
    paths = _watch_paths(watch_id, root)

    receipt = _read_json(paths.receipt)
    if receipt is not None:
        print(_receipt_line(receipt, watch_id, paths))
        return _exit_for_state(receipt.get("state", "unknown"))

    try:
        lock_fd = _open_lock(paths.lock)
    except OSError:
        print(f"state=watcher-dead watch_id={watch_id} pid=-")
        return 5
    try:
        # At most two passes, both event-driven. The second exists only for an
        # unbounded join that somehow won the lock ahead of the watcher: it must
        # not invent a timeout it was never asked to bound.
        for attempt in range(2):
            if not _acquire_bounded(lock_fd, args.timeout):
                print(f"state=join-timeout watch_id={watch_id}")
                return 6
            # Re-read AFTER acquiring: the watcher renames the receipt and only
            # then exits, so the receipt is guaranteed visible once its lock is
            # free.
            receipt = _read_json(paths.receipt)
            if receipt is not None:
                print(_receipt_line(receipt, watch_id, paths))
                return _exit_for_state(receipt.get("state", "unknown"))

            arm = _read_json(paths.arm)
            watcher = (arm or {}).get("watcher") or {}
            if arm is None or not _pid_identity_ok(watcher.get("pid"), watcher.get("pid_start")):
                print(f"state=watcher-dead watch_id={watch_id} pid={watcher.get('pid', '-')}")
                return 5

            # Lock acquired, no receipt, but the watcher's PID identity still
            # holds. Deciding `watcher-dead` from lock acquisition alone would
            # misreport a healthy watch as dead. `cmd_watch` now holds the lock
            # from before it returns, so this is unreachable for a `watch`-armed
            # id; a bounded join reports its timeout, an unbounded one releases
            # and waits once more for the real exit event.
            if args.timeout is not None or attempt == 1:
                print(f"state=join-timeout watch_id={watch_id}")
                return 6
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        print(f"state=join-timeout watch_id={watch_id}")
        return 6
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


def _watch_entries(root):
    if not root.is_dir():
        return []
    entries = []
    def _mtime(path):
        try:
            return path.stat().st_mtime
        except OSError:      # vanished between glob and stat (review n3)
            return 0.0

    for arm_path in sorted(root.glob("*.json"), key=_mtime, reverse=True):
        name = arm_path.name
        if name.endswith(".receipt.json") or name.endswith(".ack.json") or name.startswith("."):
            continue
        watch_id = name[: -len(".json")]
        arm = _read_json(arm_path)
        entries.append((watch_id, arm))
    seen = {wid for wid, _ in entries}
    # Tolerate receipt-without-arm: a caller killed between spawn and the arm
    # write leaves a self-describing receipt and no record.
    for receipt_path in sorted(root.glob("*.receipt.json")):
        watch_id = receipt_path.name[: -len(".receipt.json")]
        if watch_id not in seen:
            entries.append((watch_id, None))
    return entries


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
    if paths.ack.exists():
        state = "acked"
    elif receipt is not None:
        state = "receipt"
    elif _alive(arm, root):
        state = "alive"
    else:
        state = "armed"
    watcher = arm.get("watcher") or {}
    return {
        "watch_id": watch_id,
        "state": state,
        "target": arm.get("target", "-"),
        "until": arm.get("until") or [],
        "wake": arm.get("wake", "-"),
        "armed_ts": arm.get("armed_ts", "-"),
        "steward": arm.get("steward") or {},
        "watcher": {"pid": watcher.get("pid", "-"), "pid_start": watcher.get("pid_start", "-")},
        "receipt": str(paths.receipt),
        "ack": str(paths.ack),
        "receipt_state": (receipt or {}).get("state"),
        "agent": (receipt or {}).get("agent") or {},
    }


def cmd_status(args):
    root = _watch_root()
    entries = _watch_entries(root)
    if args.watch:
        entries = [(w, a) for w, a in entries if w == args.watch]
    rows = [_status_entry(w, a, root) for w, a in entries]
    if args.undelivered:
        sid, _harness = _current_session_identity()
        rows = [
            row for row in rows
            if row["state"] == "receipt" and (row["steward"] or {}).get("session_id") == sid
        ]
    if args.json:
        print(json.dumps({"watch_root": str(root), "watches": rows}, ensure_ascii=False))
        return 0
    for row in rows:
        agent = row["agent"] or {}
        print(
            _typed_line(
                row["receipt_state"] or row["state"], agent.get("harness", "-"),
                agent.get("session_id", "-"), agent.get("name", row["target"]),
                agent.get("pane", "-"),
            )
            + f" watch_id={row['watch_id']} receipt={row['receipt']}"
        )
    return 0


def cmd_rearm(args):
    root = _watch_root()
    watch_id = args.watch_id
    paths = _watch_paths(watch_id, root)
    arm = _read_json(paths.arm)

    if paths.receipt.exists():
        print(f"watch_id={watch_id} state=already-done receipt={paths.receipt}")
        return 0
    if _watcher_present(arm):
        print(_already_armed_line(watch_id, arm, paths).replace("state=already-armed", "state=alive"))
        return 0
    if arm is None:
        print(f"watch_id={watch_id} state=unknown receipt={paths.receipt}")
        return 0

    # No claim bookkeeping here: `cmd_watch` holds the dedupe lock, sees that the
    # claimed watch is dead with no receipt, and reclaims the key itself.
    steward = arm.get("steward") or {}
    ns = argparse.Namespace(
        target=arm["target"], until=list(arm.get("until") or []), timeout=arm.get("timeout"),
        ref=list(arm.get("refs") or []), wake=arm.get("wake", "none"),
        rearmed_from=watch_id, rearm_count=int(arm.get("rearm_count", 0)) + 1,
        steward_identity=(
            steward.get("session_id", ""), steward.get("harness", "unknown"),
            steward.get("project", ""),
        ),
    )
    import io
    buffer = io.StringIO()
    stdout, sys.stdout = sys.stdout, buffer
    try:
        code = cmd_watch(ns)
    finally:
        sys.stdout = stdout
    line = buffer.getvalue().strip()
    if code != 0 or "state=armed" not in line:
        print(line)
        return code
    # B1-a: the hook arms only from a `watch` command printing `state=armed`
    # (`hooks/peer-steward-rewake.py` `_is_watch_command`/`parse_arm`). This
    # line is neither, so it must not inherit the fresh arm's `end-turn` --
    # recompute the directive for a line that carries no carrier.
    watch_line = line.splitlines()[0].replace(
        "state=armed", f"state=rearmed rearmed_from={watch_id}"
    )
    print(watch_line)
    new_watch_id = next(
        (token.split("=", 1)[1] for token in watch_line.split()
         if token.startswith("watch_id=")),
        None,
    )
    print(
        steward_fields(
            ns.wake, new_watch_id,
            agent_home=Path(__file__).resolve().parents[1],
            arms_hook=False, timeout_ms=ns.timeout,
        )
    )
    return 0


def cmd_ack(args):
    """The single O_EXCL ack implementation.

    A second carrier is expected and is not an error: wake is at-least-once,
    display is idempotent. If `FileExistsError` ever escaped here, a carrier
    that lost the race would exit 0 where it must exit 2.
    """
    paths = _watch_paths(args.watch_id)
    session_id, _harness = _current_session_identity()
    try:
        fd = os.open(str(paths.ack), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        print(f"watch_id={args.watch_id} ack=already")
        return 0
    except OSError as exc:
        print(f"watch_id={args.watch_id} ack=failed reason=errno-{exc.errno}", file=sys.stderr)
        return 0
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(
            {"carrier": args.carrier, "session_id": session_id, "ts": _utc_now()},
            fh, ensure_ascii=False,
        )
    print(f"watch_id={args.watch_id} ack=created carrier={args.carrier}")
    return 0

_PROMPT_VERIFY_TIMEOUT_MS = 8000      # herdr `--wait --until working` bound (submission observed)
_PROMPT_STALL_FLOOR_MS = 5000         # herdr's own stall bound: a shorter --timeout hides `agent_prompt_stalled`
_PROMPT_SETTLE_MS = 1500              # bounded herdr wait before the prompt-box re-read (no sleep loop)
_PROMPT_RESIDUE_MIN_CHARS = 24        # shortest prompt-box residue (after the [kind] prefix) we call "our text"
_PROMPT_EXIT = {"true": 0, "failed": 1, "queued": 3, "unverified": 5}
_PROMPT_LEDGER_STATUS = {"true": "sent", "failed": "failed", "queued": "unknown", "unverified": "unknown"}
_KIND_PREFIX = re.compile(r"^\[(?:steer|handoff|gate|notice|start|probe)[^\]]*\]\s*")


def _agent_state(target):
    """(9) state word and pane id for `target` via one `herdr agent get`
    (never raises). The pane is what the ledger keeps: herdr's own server log
    records `cli:agent:prompt` with no target and no caller (measured
    2026-09-06, 324 such rows), so the wrapper's row is the only attribution."""
    state, agent, _code, _reason = _interpret_payload(_run_herdr_get(target), target)
    pane = agent.get("pane") if isinstance(agent, dict) else None
    return state, (pane if pane and pane != "-" else None)


def _prompt_box_evidence(target):
    """`(evidence, readable)` from `herdr agent explain`.

    The `evidence:` line is the prompt-box body only when the rule that fired
    reads `region=prompt_box_body` (Claude idle). A working Claude pane is
    explained by `osc_title_working` (the terminal title), an OpenCode pane by
    `rule: none` with no evidence line at all -- neither is a box read, and a
    box that was not read is never "clear" (review round 1, B2/B3)."""
    try:
        proc = subprocess.run(_herdr_argv("agent", "explain", target),
                              capture_output=True, text=True, timeout=_herdr_get_timeout())
    except (OSError, subprocess.SubprocessError):
        return None, False
    if proc.returncode != 0:
        return None, False
    rule = evidence = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("rule:"):
            rule = line.split(":", 1)[1].strip()
        elif line.startswith("evidence:"):
            evidence = line.split(":", 1)[1].strip()
    readable = evidence is not None and rule is not None and "region=prompt_box_body" in rule
    return (evidence if readable else None), readable


def _strip_kind(text):
    return _KIND_PREFIX.sub("", (text or "").strip())


def _prompt_box_residue(evidence, first_line):
    """True when a *read* prompt box still shows our first line.

    Only the text after the `[kind]` prefix counts, at least
    `_PROMPT_RESIDUE_MIN_CHARS` of it (a narrow pane truncates with `…`; a
    box body shorter than that is undecidable and is not residue). Claude Code
    2.1.263 renders a *predicted* next prompt in an empty box; when that
    prediction equals what we sent, text alone cannot tell them apart -- which
    is why `_verify_after_send` consults the target transcript first and only
    then this heuristic (review round 1, M4).
    """
    if not evidence or not first_line:
        return False
    body = evidence.strip().strip('"')
    body = body.replace("\\n", "\n").replace("\\u{a0}", " ").replace(" ", " ")
    body = _strip_kind(body.lstrip("❯›>").strip().rstrip("…").strip())
    ours = _strip_kind(first_line)
    if len(body) < _PROMPT_RESIDUE_MIN_CHARS:
        return False
    return ours.startswith(body[:_PROMPT_RESIDUE_MIN_CHARS])


def _transcript_rows_with(path, needle, since_epoch):
    """Rows of one Claude transcript that carry `needle` at/after `since_epoch`:
    a `user` row (delivered) or a `queue-operation` `enqueue` row (accepted
    mid-turn, delivered when the turn ends -- measured 2026-09-06 06:25Z on
    the steward pane). Either proves the text left the input box."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 512 * 1024))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in tail.splitlines():
        # No raw-line prefilter: a writer may `\uXXXX`-escape non-ASCII, so
        # the needle is compared against the decoded text only.
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        kind = row.get("type")
        if kind == "user":
            content = (row.get("message") or {}).get("content")
            if isinstance(content, list):
                text = " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
            else:
                text = str(content or "")
        elif kind == "queue-operation" and row.get("operation") == "enqueue":
            text = str(row.get("content") or "")
        else:
            continue
        if needle not in text:
            continue
        ts = row.get("timestamp") or ""
        try:
            epoch = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        except ValueError:
            epoch = None
        if epoch is None or epoch >= since_epoch - 2:
            return ts
    return None


def _transcript_arrival(t_harness, t_sid, first_line, since_epoch):
    """Ground truth for a Claude target: our exact first line in the target's
    own transcript at/after `since_epoch` (see `_transcript_rows_with`).
    herdr may report a stale session id for a pane (measured: w1:p15 reported
    16a75687 while 7a001534 was running), so after the named transcript every
    transcript written since the send is scanned too -- the first line is a
    unique needle. Other harnesses: None (no transcript contract known here)."""
    if t_harness != "claude" or not first_line:
        return None
    import glob as _glob
    needle = first_line.strip()
    seen = []
    if t_sid:
        seen += _glob.glob(os.path.expanduser(f"~/.claude/projects/*/{t_sid}.jsonl"))
    for path in _glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        if path in seen:
            continue
        try:
            if os.stat(path).st_mtime < since_epoch - 2:
                continue
        except OSError:
            continue
        seen.append(path)
    for path in seen:
        ts = _transcript_rows_with(path, needle, since_epoch)
        if ts:
            return ts
    return None


def _herdr_prompt(target, text, *, wait, timeout_ms):
    cmd = _herdr_argv("agent", "prompt", target, text)
    if wait:
        cmd += ["--wait", "--until", "working", "--timeout", str(timeout_ms)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_ms / 1000 + 15)
    except (OSError, subprocess.SubprocessError):
        return None, None
    payload = None
    for stream in (proc.stdout, proc.stderr):
        try:
            candidate = json.loads(stream)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    return proc.returncode, payload


def _settle(target, bound_ms=_PROMPT_SETTLE_MS):
    """Bounded, event-driven pause before re-reading the prompt box: one
    `herdr agent wait --until idle|done|blocked --timeout` call (S3: no
    self-written sleep or poll loop). Its result is irrelevant -- only the
    elapsed bound matters."""
    _run_herdr_wait(target, ["idle", "done", "blocked"], bound_ms)


_FORM_TOKENS = ("esctocancel", "entertoselect", "entertoconfirm", "doyouwanttoproceed",
                "tab/arrowkeys", "arrowkeystonavigate")


def _form_open(target):
    """True when the target's visible pane shows an open selection/permission
    form (AskUserQuestion, permission prompt). Measured 2026-09-06: text
    injected into such a form is lost and its `Enter` picks the form's
    default answer -- the one injection path that silently destroys state.
    herdr reports `blocked` for a normal-width pane; a narrow pane wraps the
    footer words so the state stays `idle` (or `working` for a mid-turn
    permission prompt), hence this whitespace-free scan of the whole visible
    buffer, run for every state (review round 1, M3/minor 5)."""
    try:
        proc = subprocess.run(_herdr_argv("agent", "read", target, "--source", "visible"),
                              capture_output=True, text=True, timeout=_herdr_get_timeout())
    except (OSError, subprocess.SubprocessError):
        return False
    flat = "".join((proc.stdout or "").split()).lower()
    return any(token in flat for token in _FORM_TOKENS)


def _send_enter(target):
    try:
        subprocess.run(_herdr_argv("agent", "send-keys", target, "Enter"),
                       capture_output=True, text=True, timeout=_herdr_get_timeout())
    except (OSError, subprocess.SubprocessError):
        pass


def _verify_after_send(target, first, t_harness, t_sid, sent_at):
    """(outcome, verify, reason) once herdr itself could not prove the submission.

    Order: the target transcript (exact, Claude only) → a *read* prompt box
    (`region=prompt_box_body`; residue → one Enter retry → re-check) → nothing
    observed is `unverified`, never `true`."""
    if _transcript_arrival(t_harness, t_sid, first, sent_at):
        return "true", "transcript-arrival", None
    evidence, readable = _prompt_box_evidence(target)
    if not readable:
        return "unverified", "prompt-box-unavailable", "submission-not-observed"
    if not _prompt_box_residue(evidence, first):
        return "true", "prompt-box-clear", None
    _send_enter(target)
    _settle(target)
    if _transcript_arrival(t_harness, t_sid, first, sent_at):
        return "true", "transcript-arrival", None
    evidence, readable = _prompt_box_evidence(target)
    if not readable:
        return "unverified", "prompt-box-unavailable", "submission-not-observed"
    if _prompt_box_residue(evidence, first):
        return "queued", "prompt-box", "prompt-box-residue"
    return "true", "prompt-box-clear", None


def cmd_prompt(args):
    """F-100c — the harness-neutral steward send: `herdr agent prompt <target> <body +
    trailer>`, recorded with the target's exact session id (herdr `agent get`) and the
    sender's name. The trailer lets the receiving harness write its own `notice`.

    SD-122 (11) v67/v70 — `prompted=true` is printed only after the submission was
    observed, never from herdr's exit code alone:

    * target `blocked`, or its visible pane shows a selection/permission form
      (checked for every state): refused as `prompted=failed
      reason=target-form-open` -- typed text would be lost and the Enter would
      answer the form with its default (measured 3/3).
    * target not working (idle/done/unknown): `herdr agent prompt --wait --until
      working` must see the state change; herdr's `agent_prompt_stalled` is
      `prompted=failed reason=agent-prompt-stalled`; a herdr `timeout` falls
      through to `_verify_after_send`.
    * target already working (a state change proves nothing): after a bounded
      herdr wait, `_verify_after_send` -- the target transcript first, then a
      prompt box that was actually read; residue after one Enter retry is
      `prompted=queued` (exit 3); nothing observed is `prompted=unverified`
      (exit 5), ledger `unknown`.

    Measured 2026-09-06 on a probe Claude session: every path (herdr prompt,
    this command, send-text+Enter) submitted within 1 s whether the target was
    idle or working; the verification exists so that a future failure is a
    typed refusal instead of a false `prompted=true`. Every send leaves one
    ledger row (`to.pane`, caller session, digest, verdict receipt) -- the only
    attribution that exists for a pane prompt.
    """
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
    from_identity = (from_sid, from_harness, _project_of(os.getcwd()))
    from_name = _from_name(from_harness, from_sid)
    t_harness, t_sid, _t_name = _resolve_target(args.target)
    transfer_ref = None
    if not args.no_trailer:
        try:
            text, transfer_ref = peer_message.prepare_peer_message(
                body, {"harness": from_harness, "session_id": from_sid, "name": from_name},
                {"harness": t_harness, "session_id": t_sid, "name": _t_name})
        except (OSError, ValueError):
            print("prompted=false reason=peer-transfer-record-unavailable")
            return 1
    first = body.strip().splitlines()[0] if body.strip() else ""
    kind = "steer"
    for prefix, k in (("[steer]", "steer"), ("[handoff]", "handoff"), ("[gate]", "gate-relay")):
        if first.startswith(prefix):
            kind = k

    started = time.monotonic()
    sent_at = time.time()
    verify = "none"
    reason = None
    rc = None
    verify_timeout_ms = max(_PROMPT_STALL_FLOOR_MS, int(args.verify_timeout_ms))
    state_before, target_pane = _agent_state(args.target)
    if args.no_verify:
        state_before = "-"
        rc, _payload = _herdr_prompt(args.target, text, wait=False,
                                     timeout_ms=_PROMPT_VERIFY_TIMEOUT_MS)
        if rc is None:
            return _unavailable("herdr-invocation-failed")
        outcome = "true" if rc == 0 else "failed"
        reason = None if rc == 0 else f"herdr-exit-{rc}"
    else:
        if state_before in {"working", "blocked"} and args.wait_idle_ms > 0:
            _run_herdr_wait(args.target, ["idle", "done"], args.wait_idle_ms)
            state_before, target_pane = _agent_state(args.target)
        if state_before == "blocked" or _form_open(args.target):
            outcome, reason = "failed", "target-form-open"
        elif state_before == "working":
            rc, payload = _herdr_prompt(args.target, text, wait=False,
                                        timeout_ms=_PROMPT_VERIFY_TIMEOUT_MS)
            if rc is None:
                return _unavailable("herdr-invocation-failed")
            if rc != 0:
                outcome, reason = "failed", _herdr_error_reason(payload, rc)
            else:
                _settle(args.target, max(_PROMPT_SETTLE_MS, int(args.wait_idle_ms)))
                outcome, verify, reason = _verify_after_send(
                    args.target, first, t_harness, t_sid, sent_at)
        else:
            rc, payload = _herdr_prompt(args.target, text, wait=True,
                                        timeout_ms=verify_timeout_ms)
            if rc is None:
                return _unavailable("herdr-invocation-failed")
            if rc == 0:
                outcome, verify = "true", "state-flip"
            else:
                reason = _herdr_error_reason(payload, rc)
                if reason == "timeout":
                    # herdr saw a state change (else it would have said
                    # stalled) but no `working` within the bound.
                    outcome, verify, reason = _verify_after_send(
                        args.target, first, t_harness, t_sid, sent_at)
                else:
                    outcome = "failed"
    elapsed_ms = int((time.monotonic() - started) * 1000)
    ledger_status = _PROMPT_LEDGER_STATUS[outcome]
    # SD-122 (11): one ledger row per send, whatever happened -- target pane,
    # caller session (from `_record`), time, body digest, and the submission
    # verdict as the receipt. This row is the only caller attribution that
    # exists for a pane prompt.
    receipt = (f"prompted={outcome} state_before={state_before} verify={verify} "
               f"herdr_rc={'-' if rc is None else rc} ms={elapsed_ms}"
               + (f" reason={reason}" if reason else ""))
    _record(to_harness=t_harness or "unknown", to_name=args.target, kind=kind,
            summary_text=text, to_session_id=t_sid, to_pane=target_pane,
            ref=args.ref, status=ledger_status, receipt=receipt,
            from_identity=from_identity, from_name=from_name, transfer_ref=transfer_ref)
    line = (f"prompted={outcome} target={args.target} "
            f"to_harness={t_harness or '-'} to_alias={peer_message.peer_alias(t_harness, t_sid)} kind={kind} "
            f"state_before={state_before} verify={verify} ms={elapsed_ms}")
    if reason:
        line += f" reason={reason}"
    print(line)
    return _PROMPT_EXIT[outcome]


def _herdr_error_reason(payload, rc):
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if code == "agent_prompt_stalled":
        return "agent-prompt-stalled"
    if code:
        return str(code).replace("_", "-")
    return f"herdr-exit-{rc}"


def cmd_steward(args):
    """F-100c-2 — explicit steward mode switch. The flag is a role, not a side effect of
    sending: only `steward on` (source=explicit), a `wait`/`watch` that observed a real
    target (source=watch) or a `start` that launched a session (source=start) raise it;
    `off` releases it. A session taking the role runs `steward on` once."""
    sid, harness = _current_session_identity()
    if not sid:
        print("steward=unchanged reason=no-session-identity")
        return 1
    if args.state == "on":
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ok = peer_message.mark_steward(harness, sid, {"harness": "unknown", "name": "-"},
                                       "explicit", ts, source="explicit")
        print(f"steward={'on' if ok else 'unchanged'} harness={harness} session_id={sid}")
        return 0 if ok else 1
    rc = peer_message.cmd_release(peer_message.argparse.Namespace(harness=harness, session_id=sid))
    print(f"steward=off harness={harness} session_id={sid}")
    return rc


def build_parser():
    parser = argparse.ArgumentParser(prog="peer-steward")
    parser.add_argument("--herdr-session", default=None,
                        help="herdr session holding the target pane "
                             "(default: AGENT_HERDR_SESSION, else herdr's default session)")
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

    # --- SD-122 (10) detached steward watch ---
    p_watch = sub.add_parser("watch")
    p_watch.add_argument("target")
    p_watch.add_argument("--until", action="append", default=[])
    p_watch.add_argument("--timeout", type=int, default=None)
    p_watch.add_argument("--ref", action="append", default=[])
    p_watch.add_argument("--wake", choices=("auto", "hook", "none"), default="auto")
    p_watch.set_defaults(func=cmd_watch, rearmed_from=None, rearm_count=0)

    # Hidden: the detached watcher's own entry point, reached only by re-exec.
    p_run = sub.add_parser("__watch-run")
    p_run.add_argument("--watch-id", required=True)
    p_run.add_argument("--target", required=True)
    p_run.add_argument("--until", action="append", default=[])
    p_run.add_argument("--timeout", type=int, default=None)
    p_run.add_argument("--ref", action="append", default=[])
    p_run.add_argument("--steward-harness", default="unknown")
    p_run.add_argument("--steward-session-id", default="")
    p_run.add_argument("--steward-project", default="")
    p_run.add_argument("--armed-ts", default="")
    p_run.add_argument("--rearmed-from", default=None)
    p_run.add_argument("--rearm-count", type=int, default=0)
    p_run.add_argument("--lock-fd", type=int, default=None)
    p_run.set_defaults(func=cmd_watch_run)

    p_join = sub.add_parser("join")
    p_join.add_argument("watch_id")
    p_join.add_argument("--timeout", type=int, default=None)
    p_join.set_defaults(func=cmd_join)

    p_status = sub.add_parser("status")
    p_status.add_argument("--watch", default=None)
    p_status.add_argument("--json", action="store_true")
    p_status.add_argument("--undelivered", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_rearm = sub.add_parser("rearm")
    p_rearm.add_argument("watch_id")
    p_rearm.set_defaults(func=cmd_rearm)

    p_ack = sub.add_parser("ack")
    p_ack.add_argument("watch_id")
    p_ack.add_argument("--carrier", required=True)
    p_ack.set_defaults(func=cmd_ack)
    p_prompt = sub.add_parser("prompt")
    p_prompt.add_argument("target")
    p_prompt.add_argument("text", nargs="?", default=None)
    p_prompt.add_argument("--body-file", default=None)
    p_prompt.add_argument("--body-stdin", action="store_true")
    p_prompt.add_argument("--no-trailer", action="store_true")
    p_prompt.add_argument("--ref", action="append", default=[])
    p_prompt.add_argument("--no-verify", action="store_true",
                          help="legacy: report herdr's exit code as prompted=true (no submission check)")
    p_prompt.add_argument("--verify-timeout-ms", type=int, default=_PROMPT_VERIFY_TIMEOUT_MS,
                          help="bound for observing the target's state flip after submission (clamped to herdr's 5000 ms stall bound)")
    p_prompt.add_argument("--wait-idle-ms", type=int, default=0,
                          help="defer the send until a working target settles (0 = send now; measured: mid-turn sends submit)")
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
    if args.herdr_session:
        global _HERDR_SESSION
        _HERDR_SESSION = args.herdr_session
        # A detached watcher is re-execed without this argv, so carry the
        # selection in the environment it inherits.
        os.environ["AGENT_HERDR_SESSION"] = args.herdr_session
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
