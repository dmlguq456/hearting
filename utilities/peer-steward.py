#!/usr/bin/env python3
"""SD-122 steward surfaces over herdr: (9) wait/start, (10) watch/join/status/rearm/ack.

Checked wrapper around `herdr agent wait|get|start` — completion watching is
event-driven with no self-written sleep/poll loop. Foreground launch/retire
bookkeeping uses fixed monotonic deadlines. Ledger writes go through
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
import calendar
import contextlib
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
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
import peer_obligations  # noqa: E402
import dispatch_batch_obligations  # noqa: E402

_DEFAULTS_SPEC = importlib.util.spec_from_file_location(
    "dispatch_defaults", str(_UTILITIES_DIR / "dispatch-defaults.py")
)
DEFAULTS = importlib.util.module_from_spec(_DEFAULTS_SPEC)
_DEFAULTS_SPEC.loader.exec_module(DEFAULTS)

_PATHS_SPEC = importlib.util.spec_from_file_location(
    "hearting_install_paths", str(_UTILITIES_DIR.parent / "tools/install/paths.py")
)
INSTALL_PATHS = importlib.util.module_from_spec(_PATHS_SPEC)
_PATHS_SPEC.loader.exec_module(INSTALL_PATHS)

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

# A shared-daemon Codex TUI holds no rollout file of its own, so two same-cwd TUIs
# started moments apart leave Fleet with no per-process proof (fd-less + start-time
# tie stays anonymous under F-26). A fresh steward-started TUI is therefore launched
# Embedded (`--no-daemon`): it owns its transcript fd, and the existing fd resolver
# attributes each TUI exactly no matter how close together they started. Native probe
# (2026-10-05): `codex --no-daemon --cd <cwd>` holds its rollout fd and the board
# resolver returns its thread; context telemetry is unchanged.
_CODEX_NO_DAEMON_FLAG = "--no-daemon"
# A caller-stated daemon/connection stance is never second-guessed.
_CODEX_DAEMON_STANCE_OPTIONS = {"--no-daemon", "--remote"}
# Root-level Codex options the freshness scan skips (sibling source of truth:
# `tools/fleet/collectors/procscan.py::codex_effective_cwd` — keep in step).
_CODEX_FRESH_VALUE_OPTIONS = {
    "--config", "-c", "--enable", "--disable", "--remote-auth-token-env",
    "--local-provider", "--model", "-m", "--profile", "-p", "--sandbox", "-s",
    "--add-dir", "--ask-for-approval", "-a", "--cd", "-C", "--image", "-i",
}
_CODEX_FRESH_KNOWN_FLAGS = {
    "--oss", "--strict-config", "--approve-for-me",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust", "--worktree", "--no-alt-screen",
    "--no-daemon", "--search", "--help", "-h", "--version", "-V",
}
_CODEX_FRESH_ATTACHED_SHORTS = ("-c", "-m", "-p", "-s", "-a", "-C", "-i")
# Any subcommand means this is not a fresh interactive TUI (same sibling list).
_CODEX_SUBCOMMANDS = {
    "exec", "app-server", "login", "logout", "mcp", "completion", "features",
    "debug", "apply", "resume", "fork", "cloud", "agents", "remote-control",
    "update", "doctor", "sandbox", "queue", "archive", "delete",
    "migrate-rollouts", "unarchive", "help", "review", "exec-server", "plugin",
}
# Native single-letter aliases (`codex --help`: exec→e, apply→a). A bare `e` is far
# more likely the exec alias than a one-letter prompt, so aliases fail closed too.
_CODEX_SUBCOMMAND_ALIASES = {"e", "a"}
# Informational commands never launch a TUI; reshaping them is always wrong.
_CODEX_INFORMATIONAL_OPTIONS = {"--help", "-h", "--version", "-V"}


def _codex_fresh_tui_args(agent_args):
    """True when ``agent_args`` is a fresh interactive TUI invocation.

    The scan never stops at the first positional: official 0.160 parses
    ``codex hello resume`` with ``resume`` as the subcommand (``hello`` sits in
    the PROMPT slot), so a later positional can still be a subcommand that must
    keep its existing execution path. Only these stay fresh: no positional at
    all, prompt text (bare words — a subcommand name never contains a space, and
    ``hello`` alone really is a prompt per ``codex --help``'s
    ``codex [OPTIONS] [PROMPT]``), or a literal prompt after an explicit native
    ``--``. Anything unrecognized — an unknown option, a dangling value, an
    informational command, a subcommand in any positional slot — fails closed
    (not fresh) rather than risk reshaping a command this scan does not
    understand."""
    index = 0
    while index < len(agent_args):
        token = agent_args[index]
        if token == "--":
            return True
        if token in _CODEX_INFORMATIONAL_OPTIONS:
            return False
        if token in _CODEX_SUBCOMMANDS or token in _CODEX_SUBCOMMAND_ALIASES:
            return False
        if not token.startswith("-") or token == "-":
            index += 1
            continue
        if token in _CODEX_FRESH_VALUE_OPTIONS:
            if index + 1 >= len(agent_args):
                return False
            index += 2
            continue
        if token in _CODEX_FRESH_KNOWN_FLAGS:
            index += 1
            continue
        if token.startswith("--") and "=" in token:
            if token.partition("=")[0] in _CODEX_FRESH_VALUE_OPTIONS | _CODEX_FRESH_KNOWN_FLAGS:
                index += 1
                continue
            return False
        attached = next((short for short in _CODEX_FRESH_ATTACHED_SHORTS
                         if token.startswith(short) and len(token) > len(short)), None)
        if attached is not None and not token.startswith("--"):
            index += 1
            continue
        return False
    return True


def _codex_supports_no_daemon():
    """Whether the local `codex` accepts `--no-daemon` — fail-closed.

    The pane resolves `codex` through its own PATH, so this local probe is only an
    approximation; any probe failure keeps the previous behavior (no flag)."""
    binary = shutil.which("codex")
    if not binary:
        return False
    try:
        proc = subprocess.run([binary, "--help"], capture_output=True, text=True,
                              timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    return _CODEX_NO_DAEMON_FLAG in (proc.stdout or "")


def _codex_embedded_args(agent_args):
    """`["--no-daemon"]` for a fresh steward-started Codex TUI, else `[]`.

    Embedded is skipped (previous behavior) when the managed launcher ingress is
    installed (its `--remote` client would conflict with the flag, and the managed
    path already attributes via its registry and rollout transfer), when the caller
    stated a daemon/connection stance of its own, when the invocation is not a fresh
    TUI (resume/fork and any subcommand must keep reaching their existing thread,
    including shared-daemon ones), or when local `codex` support cannot be confirmed.
    A caller stance in `agent_args` and the support probe are already the opt-outs;
    no separate environment switch exists. Never fails a start: every refusal
    returns `[]`."""
    for token in agent_args:
        if token in _CODEX_DAEMON_STANCE_OPTIONS or token.startswith("--remote="):
            return []
    if not _codex_fresh_tui_args(agent_args):
        return []
    if _managed_ingress_dir("codex") is not None:
        return []
    if not _codex_supports_no_daemon():
        return []
    return [_CODEX_NO_DAEMON_FLAG]


def _codex_stated_no_daemon(agent_args):
    """True when the caller itself selected Embedded execution (root-level ``--no-daemon``).

    Injection (`_codex_embedded_args`) returns ``[]`` for this shape only to avoid a
    duplicate flag — but the launch really is Embedded, so it must skip the time
    binder exactly like an injected one. Parsed strictly: option values are skipped
    (a model literally named ``--no-daemon`` is not a stance) and ``--`` ends flag
    parsing (a prompt typing ``--no-daemon`` runs on the shared daemon)."""
    index = 0
    while index < len(agent_args):
        token = agent_args[index]
        if token == "--":
            return False
        if token == _CODEX_NO_DAEMON_FLAG:
            return True
        if token in _CODEX_FRESH_VALUE_OPTIONS:
            index += 2
            continue
        if token in _CODEX_FRESH_KNOWN_FLAGS:
            index += 1
            continue
        if token.startswith("--") and "=" in token:
            if token.partition("=")[0] in _CODEX_FRESH_VALUE_OPTIONS | _CODEX_FRESH_KNOWN_FLAGS:
                index += 1
                continue
        index += 1
    return False


def _current_session_identity():
    """`(session_id, harness)` — delegates to `dispatch_parent_completion
    .interactive_parent_identity`, the one resolver every identity consumer shares, so the
    sender of a peer message is the harness the route-chain writer and compose name.

    Never guesses a native id: when the resolver finds the caller ambiguous or invalid, or
    an explicit harness name has no session id of its own, the sender stays unknown
    (`AGENT_SESSION_ID` if set, else `("", "unknown")`) and the message is still recorded.
    The old claude > codex > opencode fallback named a Codex thread `claude [bc]`."""
    try:
        from dispatch_parent_completion import interactive_parent_identity
        harness, sid = interactive_parent_identity()
        if sid:
            return sid, harness
    except Exception:   # caller-harness-ambiguous/invalid -> sender unknown, never guessed
        pass
    if os.environ.get("AGENT_SESSION_ID"):
        return os.environ["AGENT_SESSION_ID"], "unknown"
    return "", "unknown"


def _project_of(cwd):
    if not cwd:
        return ""
    return os.path.basename(str(cwd).rstrip("/"))


def _caller_pane():
    """The common live ownership judgment, never the inherited pane label."""
    from pane_ownership import verified_pane
    sid, harness = _current_session_identity()
    return verified_pane(os.environ.get("HERDR_PANE_ID", ""), harness, sid,
                         server=_HERDR_SESSION)


def _caller_is_claude():
    from session_identity import identity
    return identity().harness == "claude"


def _fallback():
    return "claude-native-notify-idle" if _caller_is_claude() else "poll-fallback"


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
            to_pane=None, transfer_ref=None, from_name=_UNSET_NAME, surface="herdr"):
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
        surface=surface,
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


_AGENT_STATES = ("idle", "done", "blocked", "working", "finalizing", "unknown")
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
    """Return pane-occupied, pane-unknown, or None for an observed empty pane.

    Typing into a pane that is running an agent would inject text into that agent's
    prompt. `herdr agent start` requires a bare shell prompt for the same reason, so this
    only declines to act where the start itself is going to refuse.
    """
    try:
        proc = subprocess.run(_herdr_argv("pane", "get", pane), capture_output=True,
                              text=True, timeout=5)
        payload = json.loads(proc.stdout or "")
    except Exception:
        return "pane-unknown"  # unreadable pane: type nothing
    result = payload.get("result") if isinstance(payload, dict) else None
    block = result.get("pane") if isinstance(result, dict) else None
    if (proc.returncode != 0 or not isinstance(payload, dict) or payload.get("error")
            or not isinstance(block, dict)):
        return "pane-unknown"
    return "pane-occupied" if block.get("agent") else None


def _wait_for_shell_prompt(pane, timeout_ms=None):
    """Observe an idle shell prompt before sending cwd/PATH bootstrap text."""
    timeout_ms = int(_herdr_get_timeout() * 1000 if timeout_ms is None else timeout_ms)
    try:
        proc = subprocess.run(
            _herdr_argv("pane", "wait-output", pane, "--regex",
                        r"(?m)(?:^|[ ])(?:[$#%❯])\s*(?-m:$)",
                        "--source", "visible", "--timeout", str(timeout_ms)),
            capture_output=True, text=True, timeout=max(1, timeout_ms / 1000 + 1))
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _pane_foreground_shell(pane):
    """Observe the actual shell, not an old prompt left by a foreground job."""
    try:
        proc = subprocess.run(_herdr_argv("pane", "process-info", "--pane", pane),
                              capture_output=True, text=True, timeout=5)
        payload = json.loads(proc.stdout or "")
        result = payload.get("result") if isinstance(payload, dict) else None
        info = result.get("process_info") if isinstance(result, dict) else None
        if (proc.returncode != 0 or not isinstance(payload, dict) or payload.get("error")
                or not isinstance(info, dict) or info.get("pane_id") != pane):
            return "pane-unknown"
        shell = info.get("shell_pid")
        group = info.get("foreground_process_group_id")
        processes = info.get("foreground_processes")
        if (type(shell) is not int or shell <= 0 or type(group) is not int or group <= 0
                or not isinstance(processes, list)):
            return "pane-unknown"
        if group != shell:
            return "pane-busy"
        if not processes or any(not isinstance(item, dict) or type(item.get("pid")) is not int
                                or item["pid"] <= 0 for item in processes):
            return "pane-unknown"
        return None if all(item["pid"] == shell for item in processes) else "pane-busy"
    except (OSError, subprocess.SubprocessError, ValueError):
        return "pane-unknown"


def _ensure_pane_ingress(pane, kind, cwd=None):
    """Put hearting's launcher wrapper first on the PANE's PATH. Returns a typed reason.

    ``None`` means nothing was needed or nothing was typed; any other value names why, and
    is carried into the launch receipt so an unmanaged launch can never be silent.
    """
    directory = _managed_ingress_dir(kind)
    bootstrap_cwd = cwd if kind == "claude" and cwd else None
    if directory is None and bootstrap_cwd is None:
        return None
    pane_state = _pane_has_agent(pane)
    if pane_state:
        return pane_state
    if not _wait_for_shell_prompt(pane):
        return _native_trust_reason(kind, _read_screen(pane)) or "shell-readiness-timeout"
    # The wait searches existing screen output too. Recheck occupancy and the native
    # foreground identity after it, before typing into what may now be a job or form.
    pane_state = _pane_has_agent(pane) or _pane_foreground_shell(pane)
    if pane_state:
        return _native_trust_reason(kind, _read_screen(pane)) or pane_state
    commands = []
    if bootstrap_cwd:
        commands.append("cd -- %s" % shlex.quote(bootstrap_cwd))
    if directory:
        commands.append('export PATH="%s:$PATH"' % directory)
    line = " && ".join(commands)
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


# A Codex TUI attached to the shared app-server daemon holds no rollout file of its own, and
# the daemon creates the thread's rollout about a second after the TUI starts. Fleet can then
# only match a TUI to its thread by start time, which several same-cwd starts a few seconds
# apart defeat on purpose (no guessing), so the row stays anonymous and herdr learns nothing.
# `start` is the one party that saw the launch: it notes which rollouts exist before the
# launch and afterwards takes exactly ONE new root rollout for the target cwd as the session.
# Zero or several is no proof, so nothing is bound and the receipt says why. Bookkeeping, not
# a gate: it adds no flag or required input and never fails the start; the bound is the wait.
_BIND_SECONDS = 15.0
_BIND_POLL_SECONDS = 0.5
_BIND_PROCESS_SECONDS = 3.0      # how long to wait for the pane's `codex` process to appear
_BIND_START_SLACK_SECONDS = 2.0


def _codex_home_dir():
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def _rollout_paths(home):
    """Every rollout file name under the Codex sessions tree (names only, nothing is read)."""
    found = set()
    for root, _dirs, names in os.walk(os.path.join(home, "sessions")):
        for name in names:
            if name.startswith("rollout-") and name.endswith(".jsonl"):
                found.add(os.path.join(root, name))
    return found


def _fleet_codex_collector():
    """`fleet.collectors.codex`, whose rollout helpers (`_rollout_meta`, `_is_subagent`,
    `_sid`) are reused instead of re-parsing rollouts here. ``None`` on any failure."""
    if _session_registry() is None:
        return None
    try:
        from fleet.collectors import codex as collector
        return collector
    except Exception:
        return None


def _pane_codex_pid(pane):
    """Pid of the pane's foreground `codex` process, from `herdr pane process-info`."""
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
        argv = [str(part) for part in (process.get("argv") or [])]
        if argv and os.path.basename(argv[0]) == "codex" and process.get("pid"):
            try:
                return int(process["pid"])
            except (TypeError, ValueError):
                continue
    return None


def _proc_cwd(pid):
    try:
        return os.path.realpath(os.readlink("/proc/%d/cwd" % int(pid)))
    except (OSError, ValueError):
        return None


def _rollout_created_at(meta, path):
    stamp = meta.get("timestamp")
    if isinstance(stamp, str):
        try:
            return calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            pass
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _new_root_rollouts(collector, home, before, cwd, launched_at):
    """Thread ids of root rollouts for ``cwd`` created at/after the launch and not in ``before``."""
    found = []
    for path in sorted(_rollout_paths(home) - before):
        meta = collector._rollout_meta(path)
        if not meta or collector._is_subagent(meta):
            continue
        if os.path.realpath(str(meta.get("cwd") or "")) != cwd:
            continue
        if _rollout_created_at(meta, path) < launched_at - _BIND_START_SLACK_SECONDS:
            continue
        sid = collector._sid(path)
        if sid:
            found.append(sid)
    return found


def _bind_codex_session(pane, pane_cwd, home, before, launched_at):
    """`(state, session_id, pid, cwd)`; state is bound|ambiguous|timeout|no-process|unavailable.

    A single candidate is confirmed once more one poll later, so a second same-cwd launch that
    lands a moment after the first is seen as ambiguous rather than silently taken. A candidate
    first seen on the final poll never got that second look, so the bind ends as `timeout`."""
    collector = _fleet_codex_collector()
    if collector is None:
        return "unavailable", None, None, None
    began = time.monotonic()
    deadline = began + _BIND_SECONDS
    pid = cwd = None
    candidate = None
    while time.monotonic() < deadline:
        if pid is None:
            pid = _pane_codex_pid(pane)
        if pid is not None and cwd is None:
            cwd = pane_cwd or _proc_cwd(pid)
        if cwd:
            found = _new_root_rollouts(collector, home, before, cwd, launched_at)
            if len(found) > 1:
                return "ambiguous", None, pid, cwd
            if len(found) == 1:
                if candidate == found[0]:
                    return "bound", found[0], pid, cwd
                candidate = found[0]
            else:
                candidate = None
        if pid is None and time.monotonic() - began >= _BIND_PROCESS_SECONDS:
            break
        time.sleep(_BIND_POLL_SECONDS)
    return ("timeout" if pid is not None and cwd else "no-process"), None, pid, cwd


def _proc_start_ticks(pid):
    """/proc/<pid>/stat field 22 as a str (the registry's PID-reuse guard), else None."""
    try:
        raw = Path("/proc/%d/stat" % int(pid)).read_text()
        rest = raw[raw.rindex(")") + 1:].split()
        return None if rest[0] in ("Z", "X") else rest[19]
    except (OSError, ValueError, IndexError):
        return None


def _write_bound_registry(pid, session_id, cwd, name):
    """Fleet's tier-1 record for the launched TUI, so Fleet (and through it herdr) names it.

    Written only with a nonempty procStart: Fleet rejects a record without a matching process
    start time, so one written without it would claim a binding nobody can use. Returns whether
    a usable record was written."""
    proc_start = _proc_start_ticks(pid)
    if not proc_start:
        return False
    registry = _session_registry()
    if registry is None:
        return False
    try:
        registry.write("codex", pid, {
            "sessionId": session_id,
            "cwd": cwd,
            "startedAt": int(time.time() * 1000),
            "procStart": proc_start,
            "name": name,
            "nameSource": "user",
            "kind": "codex-tui",
            "entrypoint": "peer-steward-start",
            "harness": "codex",
        })
        return True
    except Exception:
        return False


# The harness flag that puts a launched session in a chosen directory. `herdr agent
# start` has none of its own — the agent it starts inherits the PANE's shell cwd — so
# `--cwd` used to move nothing but this CLI process: a session started with
# `--cwd <hearting>` came up in `SR_CorrNet`, the pane's own directory (measured
# 2026-09-10). Codex takes `-C/--cd`; Claude Code and OpenCode have no equivalent today,
# so for those `--cwd` is REFUSED rather than silently ignored. Launching an agent
# somewhere other than where the caller said is the failure this exists to prevent, and a
# refusal the caller can read beats a session quietly working in the wrong repository.
_CWD_FLAG = {"codex": "--cd"}


def _start_stderr_code(stderr):
    """A bounded single-line native diagnostic, never raw terminal controls."""
    raw = (stderr or "").encode("utf-8", "replace")[:1024].decode("utf-8", "replace")
    lines = [_plain(line).strip() for line in _screen_lines(raw)]
    first = next((line for line in lines if line), "")
    return re.sub(r"[^A-Za-z0-9_-]+", "-", first).strip("-")[:80].lower()


def _start_shell_identity(pane):
    info = _retire_pane_info(pane)
    if (info is None or info["foreground_process_group_id"] != info["shell_pid"]
            or len(info["foreground_processes"]) != 1
            or not isinstance(info["foreground_processes"][0], dict)
            or type(info["foreground_processes"][0].get("pid")) is not int
            or info["foreground_processes"][0]["pid"] != info["shell_pid"]):
        return None
    start = _proc_start_ticks(info["shell_pid"])
    return (info["shell_pid"], start) if start else None


def _close_pane(pane):
    try:
        proc = subprocess.run(_herdr_argv("pane", "close", pane),
                              capture_output=True, text=True, timeout=5)
        payload = json.loads(proc.stdout or "")
        result = payload.get("result") if isinstance(payload, dict) else None
        return (proc.returncode == 0 and isinstance(payload, dict) and not payload.get("error")
                and isinstance(result, dict) and result.get("type") == "ok")
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def _start_pane_screen(pane):
    """Return the visible screen verbatim; never interpret it as a prompt."""
    try:
        proc = subprocess.run(_herdr_argv("pane", "read", pane, "--source", "visible",
                                          "--format", "ansi"),
                              capture_output=True, text=True, timeout=5)
        if (proc.returncode or not isinstance(proc.stdout, str)
                or len(proc.stdout.encode("utf-8")) > 65536):
            return None
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and payload.get("error"):
            return None
        return proc.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _start_shell_snapshot(pane, original_shell, deadline=None):
    """Two equal observations before native start, within one second."""
    if original_shell is None:
        return None
    previous = None
    deadline = min(time.monotonic() + 1, deadline) if deadline is not None else time.monotonic() + 1
    while time.monotonic() < deadline:
        if (_pane_has_agent(pane) is not None
                or _start_shell_identity(pane) != original_shell):
            return None
        screen = _start_pane_screen(pane)
        if screen is None:
            return None
        if (screen.strip() and screen == previous
                and _start_shell_identity(pane) == original_shell
                and _pane_has_agent(pane) is None):
            return screen
        previous = screen
        time.sleep(min(.05, max(0, deadline - time.monotonic())))
    return None


# NAS-backed prompt initialization can spend minutes in a git child. Keep one
# finite deadline and the same shell/prompt checks through bootstrap and start.
_BESIDE_READY_SECONDS = 300


def _wait_for_created_shell(pane, cwd, original_shell, deadline):
    """Finish this split's cwd/bootstrap before snapshot and the one start request."""
    while time.monotonic() < deadline:
        occupied = _pane_has_agent(pane)
        if occupied:
            return occupied, original_shell
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        info = _retire_pane_info(pane, timeout=min(5, remaining))
        if info is None:
            return "beside-shell-unverified", original_shell
        start = _proc_start_ticks(info["shell_pid"])
        if not start:
            return "beside-shell-unverified", original_shell
        current = (info["shell_pid"], start)
        if original_shell is None:
            original_shell = current
        if current != original_shell:
            return "beside-shell-changed", original_shell
        remaining = deadline - time.monotonic()
        if (remaining > 0 and _start_shell_identity(pane) == original_shell
                and (cwd is None or _proc_cwd(original_shell[0]) == cwd)
                and _wait_for_shell_prompt(pane, timeout_ms=max(1, min(200, int(remaining * 1000))))
                and time.monotonic() < deadline
                and _start_shell_identity(pane) == original_shell
                and _pane_has_agent(pane) is None):
            return None, original_shell
        time.sleep(min(.05, max(0, deadline - time.monotonic())))
    return "beside-shell-readiness-timeout", original_shell


def _failed_start_cleanup_observation(pane, original_shell, original_screen):
    if (_pane_has_agent(pane) is not None
            or _proc_start_ticks(original_shell[0]) != original_shell[1]):
        return "retained"
    info = _retire_pane_info(pane)
    if info is None or info["shell_pid"] != original_shell[0]:
        return "retained"
    if _start_shell_identity(pane) == original_shell:
        if _start_pane_screen(pane) != original_screen:
            return "retained"
        if (_start_shell_identity(pane) != original_shell
                or _pane_has_agent(pane) is not None
                or _start_pane_screen(pane) != original_screen):
            return "retained"
        return "closed" if _close_pane(pane) else "close-failed"
    return None


def _failed_start_cleanup(pane, original_shell, original_screen, deadline=None):
    # Never close a caller-provided pane, a replaced shell or a late-starting
    # agent. The fresh split is the only pane owned by this failed invocation.
    if original_shell is None or original_screen is None:
        return "retained"
    deadline = time.monotonic() + 5 if deadline is None else deadline
    while time.monotonic() < deadline:
        result = _failed_start_cleanup_observation(pane, original_shell, original_screen)
        if result is not None:
            return result
        time.sleep(min(.1, max(0, deadline - time.monotonic())))
    # If late-start observation spent the shared wait budget, still perform the
    # ordinary last cleanup observation, without another wait or a new deadline.
    return _failed_start_cleanup_observation(pane, original_shell, original_screen) or "retained"


def _opencode_tui_scoped_config():
    """Hearting-owned scoped TUI config for owned OpenCode launches, or None.

    Activation projects ``tui/hearting-owned-tui.json`` into the managed
    OpenCode runtime home; pointing OPENCODE_TUI_CONFIG at it adds only the
    hearting TUI identity entry through the official global, override,
    project, .opencode merge order, so user config, explicit overrides,
    disables, plugins, and options are preserved.
    """
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    scoped = os.path.join(config_home, "opencode", "tui", "hearting-owned-tui.json")
    return scoped if os.path.isfile(scoped) else None


def _export_opencode_tui_scoped(pane):
    """Export OPENCODE_TUI_CONFIG into the pane before an owned OpenCode start.

    Best-effort enhancement, never a launch refusal: any failure degrades to
    a receipt note and the start proceeds. Mirrors the ingress send-text
    shape (prompt wait, occupancy recheck, ordered typing, no clock wait).
    Codex/Claude paths and binder semantics are untouched.
    """
    scoped = _opencode_tui_scoped_config()
    if scoped is None:
        return "missing"
    if _pane_has_agent(pane):
        return "pane-occupied"
    if not _wait_for_shell_prompt(pane):
        return "shell-readiness-timeout"
    state = _pane_has_agent(pane) or _pane_foreground_shell(pane)
    if state:
        return state
    # An explicit OPENCODE_TUI_CONFIG the pane already carries wins: the
    # scoped file only fills an unset-or-empty value, so user overrides keep
    # their options, plugins, and disables through the official merge order.
    line = ("export OPENCODE_TUI_CONFIG=${OPENCODE_TUI_CONFIG:-%s}"
            % shlex.quote(scoped))
    try:
        text = subprocess.run(_herdr_argv("pane", "send-text", pane, line),
                              capture_output=True, text=True, timeout=5)
        if text.returncode != 0:
            return "send-failed"
        enter = subprocess.run(_herdr_argv("pane", "send-keys", pane, "Enter"),
                               capture_output=True, text=True, timeout=5)
        if enter.returncode != 0:
            return "send-failed"
    except Exception:
        return "send-failed"
    return "exported"


def _seat_successor_path(pane):
    digest = hashlib.sha256(str(pane).encode("utf-8")).hexdigest()[:32]
    return peer_message.peer_state_root() / "seat-successors" / f"{digest}.json"


def _seat_successor_lock(pane):
    import session_tidy
    return session_tidy.seat_lock("peer-successor-" + _seat_successor_path(pane).stem)


def _mark_seat_successor(pane, beside, kind, session_id):
    """`start --beside`: the started pane is the seat successor of the agent beside it.

    The one fact a later `retire` from this pane needs to hand that agent's routes on
    (OPERATIONS same-seat change). Best effort: a missing mark only means no handover."""
    try:
        with _seat_successor_lock(pane):
            path = _seat_successor_path(pane)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"schema": 1, "pane": pane, "beside": beside,
                                       "successor": {"harness": kind, "session_id": session_id or ""},
                                       "at": time.time()}, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
    except OSError:
        pass


def _seat_handover(ident, own_sid, own_harness, *, requester=None, accepted_at=None):
    """After `retire` proved the predecessor exited: when this session runs in the pane started
    beside the predecessor, its routes become this session's, across harnesses
    (`dispatch_seat_handover.record_retire_handover`). Returns the receipt value, or None when
    this is not a seat change (the receipt then stays as it was); it never fails the retire."""
    pane = requester.get("pane") if requester is not None else _caller_pane()
    if not pane:
        return None
    try:
        with _seat_successor_lock(pane):
            return _seat_handover_locked(ident, own_sid, own_harness, pane,
                                         requester=requester, accepted_at=accepted_at)
    except OSError:
        return "error"


def _seat_handover_locked(ident, own_sid, own_harness, pane, *, requester=None, accepted_at=None):
    path = _seat_successor_path(pane)
    try:
        mark_bytes = path.read_bytes()
        mark = json.loads(mark_bytes)
    except (OSError, ValueError):
        return None
    if not isinstance(mark, dict) or mark.get("pane") != pane or mark.get("beside") != ident["pane"]:
        return None
    successor = mark.get("successor") or {}
    recorded = successor.get("session_id")
    if (not own_sid or own_sid == "-" or ident.get("session_id") in {None, "", "-"}
            or own_harness not in {"claude", "codex", "opencode"}
            or successor.get("harness") != own_harness or (recorded and recorded != own_sid)):
        return "skipped:successor-unverified"
    if requester is not None:
        try:
            if not accepted_at or not (0 < float(mark.get("at", 0)) <= float(accepted_at)):
                return "skipped:successor-unverified"
        except (TypeError, ValueError):
            return "skipped:successor-unverified"
    try:
        import dispatch_seat_handover as handover
        booked = {}
        if requester is not None:
            import session_tidy
            booked["successor_seat"] = session_tidy._pane_seat_of(pane)
        row = handover.record_retire_handover(
            ident["session_id"], ident["harness"], own_sid, own_harness, env=os.environ, **booked)
    except Exception:  # noqa: BLE001 - the retire itself already succeeded
        return "error"
    try:
        if row is not None and path.read_bytes() == mark_bytes:
            path.unlink()
    except OSError:
        pass
    return str(len(row["bindings"])) if row else "none"


def _retire_handover(duty):
    """Consume only the accepted retirement's original succession, on any observer."""
    intent = duty.get("intent") or {}
    requester = intent.get("requester") or {}
    foreground = _retire_booked_foreground(duty)
    if (not isinstance(foreground, dict) or not foreground.get("pid") or not foreground.get("start")
            or (intent.get("identity") or {}).get("session_id") in {None, "", "-"}):
        return None
    return _seat_handover(intent.get("identity") or {}, requester.get("session_id"),
                          requester.get("harness"), requester=requester,
                          accepted_at=duty.get("accepted_at"))


def _observed_start_agent(args, deadline=None, wait=False):
    """Settle an existing/late start from this pane; never launch or type again."""
    deadline = time.monotonic() + 5 if deadline is None else deadline
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            proc = subprocess.run(_herdr_argv("agent", "get", args.pane),
                                  capture_output=True, text=True,
                                  timeout=max(.01, min(_herdr_get_timeout(), remaining)))
            payload = json.loads(proc.stdout or "")
        except (OSError, subprocess.SubprocessError, ValueError):
            payload = None
        result = payload.get("result") if isinstance(payload, dict) else None
        agent = result.get("agent") if isinstance(result, dict) else None
        if isinstance(agent, dict) and proc.returncode == 0 and not payload.get("error"):
            if agent.get("pane_id") != args.pane or agent.get("agent") != args.kind:
                return None
            cwd = os.path.realpath(os.path.expanduser(args.cwd)) if args.cwd else None
            if cwd and os.path.realpath(str(agent.get("cwd") or "")) != cwd:
                return None
            name = agent.get("name")
            if name and name != args.name:
                return None
            if not name:
                # Metadata API only: no Enter, bootstrap, or prompt input in a live agent.
                try:
                    renamed = subprocess.run(_herdr_argv("agent", "rename", args.pane, args.name),
                                             capture_output=True, text=True,
                                             timeout=max(.01, min(5, deadline - time.monotonic())))
                    answer = json.loads(renamed.stdout or "")
                    if renamed.returncode or not isinstance(answer, dict) or answer.get("error"):
                        return None
                except (OSError, subprocess.SubprocessError, ValueError):
                    return None
                # Re-read after naming; conflicting name/kind/cwd stays unclaimed.
                continue
            return agent
        if not wait or deadline - time.monotonic() <= .05:
            break
        time.sleep(min(.05, max(0, deadline - time.monotonic())))
    return None


def _mark_started(args, sid, mode):
    _record(to_harness=args.kind, to_name=args.name, kind="steer",
            summary_text=f"[start] {args.name} kind={args.kind} mode={mode}",
            to_session_id=sid)
    from_sid, from_harness = _current_session_identity()
    peer_message.mark_steward(
        from_harness, from_sid,
        {"harness": args.kind, "session_id": sid, "name": args.name, "pane": args.pane},
        "start", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), source="start")


def _grid_split(layout):
    """Match partial 2×2 grids, including herdr's odd-cell rounding."""
    area, panes = layout.get("area"), layout.get("panes")
    if not isinstance(area, dict) or not isinstance(panes, list) or not 1 <= len(panes) <= 3:
        return None
    keys = ("x", "y", "width", "height")
    if any(type(area.get(k)) is not int for k in keys) or min(area["width"], area["height"]) < 4:
        return None
    for pane in panes:
        rect = pane.get("rect") if isinstance(pane, dict) else None
        if not isinstance(rect, dict) or any(type(rect.get(k)) is not int for k in keys):
            return None
    panes = sorted(panes, key=lambda p: (p["rect"]["x"], p["rect"]["y"]))
    # Expected rectangles in x/y order, index of the full cell, split direction.
    full = (0, 0, 1, 1)
    left, right = (0, 0, .5, 1), (.5, 0, .5, 1)
    top, bottom = (0, 0, 1, .5), (0, .5, 1, .5)
    tl, bl, tr, br = (0, 0, .5, .5), (0, .5, .5, .5), (.5, 0, .5, .5), (.5, .5, .5, .5)
    patterns = (([full], 0, "right"), ([left, right], 1, "down"),
                ([left, tr, br], 0, "down"), ([tl, bl, right], 2, "down"),
                ([top, bottom], 0, "right"), ([tl, bottom, tr], 1, "right"),
                ([top, bl, br], 0, "right"))
    for expected, index, direction in patterns:
        if len(expected) != len(panes):
            continue
        matches = True
        for pane, (x, y, w, h) in zip(panes, expected):
            desired = (area["x"] + x * area["width"], area["y"] + y * area["height"],
                       w * area["width"], h * area["height"])
            if any(abs(pane["rect"][k] - v) > .5 for k, v in zip(keys, desired)):
                matches = False
                break
        if matches:
            return panes[index]["pane_id"], direction
    return None


def _placement_result(*argv):
    proc = subprocess.run(_herdr_argv(*argv), capture_output=True, text=True,
                          timeout=_herdr_get_timeout())
    payload = json.loads(proc.stdout or "")
    result = payload.get("result") if isinstance(payload, dict) else None
    if proc.returncode or not isinstance(result, dict) or payload.get("error"):
        return {}
    return result


def _start_layout(beside):
    layout = _placement_result("pane", "layout", "--pane", beside).get("layout")
    if not isinstance(layout, dict):
        return None
    panes, workspace = layout.get("panes"), layout.get("workspace_id")
    if (not isinstance(workspace, str) or not workspace.strip()
            or not isinstance(layout.get("tab_id"), str) or not isinstance(panes, list)
            or not panes or any(not isinstance(p, dict) or not isinstance(p.get("pane_id"), str)
                                or not p["pane_id"].strip() for p in panes)):
        return None
    ids = [p["pane_id"] for p in panes]
    if (beside not in ids or len(ids) != len(set(ids))
            or not layout["tab_id"].startswith(workspace + ":")
            or any(not pane_id.startswith(workspace + ":") for pane_id in ids)):
        return None
    return layout


def _empty_shell_placement(panes):
    for pane in panes:
        pane_id = pane["pane_id"]
        if (_pane_has_agent(pane_id) is None and _pane_foreground_shell(pane_id) is None
                and _wait_for_shell_prompt(pane_id, timeout_ms=1)
                and _pane_has_agent(pane_id) is None and _pane_foreground_shell(pane_id) is None):
            return "reuse", pane_id, None
    return None


def _tab_placement(layout):
    """One grid/shell decision for both the caller tab and its workspace peers."""
    split = _grid_split(layout)
    if split:
        return "split", split[0], split[1]
    return _empty_shell_placement(layout["panes"])


def _project_start_workspace():
    """A directory match locates a workspace; it never proves caller ownership."""
    hint = os.environ.get("HERDR_PANE_ID")
    if not hint:
        return None
    try:
        pane = _placement_result("agent", "get", hint).get("agent")
        if not isinstance(pane, dict) or pane.get("pane_id") != hint:
            return None
        workspace, cwd = pane.get("workspace_id"), pane.get("foreground_cwd")
        if (not isinstance(workspace, str) or not workspace.strip()
                or not hint.startswith(workspace + ":") or not isinstance(cwd, str)
                or not os.path.isabs(cwd) or not os.path.isdir(cwd)):
            return None
        caller = INSTALL_PATHS.primary_checkout(os.getcwd()).resolve()
        foreground = INSTALL_PATHS.primary_checkout(cwd).resolve()
        return workspace if caller == foreground else None
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError):
        return None


def _start_workspace_placement(workspace):
    """Project seats reuse empty shells or add a tab, without splitting a peer."""
    try:
        panes = _placement_result("pane", "list", "--workspace", workspace).get("panes")
        if (not isinstance(panes, list) or not panes
                or any(not isinstance(p, dict) or p.get("workspace_id") != workspace
                       or not isinstance(p.get("pane_id"), str)
                       or not p["pane_id"].startswith(workspace + ":") for p in panes)
                or len({p["pane_id"] for p in panes}) != len(panes)):
            return None
        return _empty_shell_placement(reversed(panes)) or ("tab", workspace, None)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _start_placement(beside):
    """Fill the caller tab, then the newest available tab in the same workspace."""
    try:
        layout = _start_layout(beside)
        if layout is None:
            return None
        placement = _tab_placement(layout)
        if placement:
            return placement
        workspace = layout["workspace_id"]
        tabs = _placement_result("tab", "list", "--workspace", workspace).get("tabs")
        if not isinstance(tabs, list):
            return None
        others = []
        for tab in tabs:
            if (not isinstance(tab, dict) or tab.get("workspace_id") != workspace
                    or not isinstance(tab.get("tab_id"), str)
                    or not tab["tab_id"].startswith(workspace + ":")
                    or type(tab.get("number")) is not int):
                return None
            if tab["tab_id"] != layout["tab_id"]:
                others.append(tab)
        if others:
            panes = _placement_result("pane", "list", "--workspace", workspace).get("panes")
            if not isinstance(panes, list):
                return None
            for tab in sorted(others, key=lambda t: t["number"], reverse=True):
                pane_id = next((p.get("pane_id") for p in panes if isinstance(p, dict)
                                and p.get("workspace_id") == workspace and p.get("tab_id") == tab["tab_id"]
                                and isinstance(p.get("pane_id"), str)
                                and p["pane_id"].startswith(workspace + ":")), None)
                if pane_id is None:
                    return None
                other = _start_layout(pane_id)
                if other is None or other["workspace_id"] != workspace or other["tab_id"] != tab["tab_id"]:
                    return None
                placement = _tab_placement(other)
                if placement:
                    return placement
        return "tab", workspace, None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def cmd_start(args):
    if len(args.name) > 32:
        print(f"started=false reason=agent-name-too-long agent={args.kind} name={args.name} "
              "max_length=32 detail=이름은 최대 32자까지 사용할 수 있습니다")
        return 1
    if not args.pane and not args.beside:
        args.beside = _caller_pane() or None
        if not args.beside:
            args.project_workspace = _project_start_workspace()
    workspace = (args.beside.split(":")[0] if args.beside else
                 getattr(args, "project_workspace", None))
    if not workspace:
        return _start_in_pane(args)
    # Hold the workspace claim until native start settles, including shell reuse.
    # A second start then reads the new layout instead of splitting the old cell.
    key = hashlib.sha256(f"{_HERDR_SESSION or 'default'}:{workspace}".encode()).hexdigest()
    root = peer_message.peer_state_root() / "peer-starts"
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"{key}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _start_in_pane(args)


def _start_in_pane(args):
    if _herdr_missing():
        return _unavailable("herdr-not-found")
    workspace = getattr(args, "project_workspace", None)
    if not args.pane and not args.beside and not workspace:
        print(f"started=false reason=pane-unknown agent={args.kind} name={args.name} "
              "pane=- detail=호출자의 창 또는 같은 프로젝트 작업공간을 확인하지 못했습니다")
        return 1
    if (args.beside or workspace) and not args.cwd:
        try:
            args.cwd = str(INSTALL_PATHS.primary_checkout(os.getcwd()))
        except OSError:
            args.cwd = None

    pane_cwd = None
    cwd_flag = []
    if args.cwd:
        flag = _CWD_FLAG.get(args.kind)
        pane_cwd = os.path.realpath(os.path.expanduser(str(args.cwd)))
        if not os.path.isdir(pane_cwd):
            print(f"started=false reason=cwd-not-a-directory agent={args.kind} "
                  f"name={args.name} pane={args.pane or '-'} cwd={pane_cwd}")
            return 1
        if flag:
            cwd_flag = [flag, pane_cwd]

    if args.pane:
        # The normal retained-pane reuse command may arrive after the runtime did.
        # Observe first so a retry never bootstraps or starts over that live agent.
        existing = _observed_start_agent(args, deadline=time.monotonic() + .5)
        if existing is not None:
            sid = peer_message.usable_session_id((existing.get("agent_session") or {}).get("value"))
            mode = args.permission_mode or _default_permission_mode()
            _mark_started(args, sid, mode)
            trust_wait = _native_trust_reason(args.kind, _read_screen(args.pane))
            print(f"started=true agent={args.kind} name={args.name} pane={args.pane} "
                  f"permission_mode={mode} session_id={sid or '-'} managed=- "
                  "start_verify=observed-pane" + (f" cwd={pane_cwd}" if pane_cwd else "")
                  + (f" ready=false reason={trust_wait}" if trust_wait else ""))
            return 0

    created_shell = created_screen = created_deadline = None
    created_pane = False
    failure_deadline = None

    def cleanup():
        if not created_pane:
            return ""
        result = (_failed_start_cleanup(args.pane, created_shell, created_screen)
                  if failure_deadline is None else
                  _failed_start_cleanup(args.pane, created_shell, created_screen,
                                        deadline=failure_deadline))
        note = f" pane_cleanup={result}"
        if result in {"retained", "close-failed"}:
            retry = ["hearting", "run", "peer-steward", "start", args.name,
                     "--kind", args.kind, "--pane", args.pane]
            if pane_cwd:
                retry += ["--cwd", pane_cwd]
            if args.permission_mode:
                retry += ["--permission-mode", args.permission_mode]
            if getattr(args, "agent_args", None):
                retry += ["--"] + list(args.agent_args)
            note += " reuse_command=" + shlex.quote(shlex.join(retry))
        return note

    if args.beside or workspace:
        placement = (_start_workspace_placement(workspace) if workspace else
                     _start_placement(args.beside))
        if placement is None:
            print(f"started=false reason=pane-layout-unavailable agent={args.kind} "
                  f"name={args.name} pane=- beside={args.beside}")
            return 1
        action, target, direction = placement
        if action == "reuse":
            args.pane = target
        else:
            cmd = (_herdr_argv("pane", "split", "--pane", target, "--direction", direction,
                               "--ratio", "0.5", "--no-focus") if action == "split" else
                   _herdr_argv("tab", "create", "--workspace", target, "--no-focus"))
            if pane_cwd:
                cmd += ["--cwd", pane_cwd]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=_herdr_get_timeout())
                payload = json.loads(proc.stdout or "")
                result = payload.get("result") if isinstance(payload, dict) else None
                pane = result.get("pane" if action == "split" else "root_pane") if isinstance(result, dict) else None
                new_id = pane.get("pane_id") if isinstance(pane, dict) else None
                split_ok = (proc.returncode == 0 and isinstance(payload, dict) and not payload.get("error")
                            and isinstance(new_id, str) and bool(new_id.strip())
                            and new_id != args.beside and pane.get("focused") is False)
            except (OSError, subprocess.SubprocessError, ValueError):
                split_ok = False
            if not split_ok:
                reason = "pane-split-failed" if action == "split" else "pane-tab-create-failed"
                print(f"started=false reason={reason} agent={args.kind} "
                      f"name={args.name} pane=- beside={args.beside}")
                return 1
            args.pane = new_id
            created_pane = True
            created_shell = _start_shell_identity(new_id)
            created_deadline = time.monotonic() + _BESIDE_READY_SECONDS
            readiness, created_shell = _wait_for_created_shell(
                args.pane, pane_cwd, created_shell, created_deadline)
            if readiness:
                print(f"started=false reason={readiness} agent={args.kind} name={args.name} "
                      f"pane={args.pane}" + (f" cwd={pane_cwd}" if pane_cwd else "") + cleanup())
                return 1

    # Before the agent is started, not after: this is the line that decides whether the
    # session that comes up is hearting-managed at all.
    # split --cwd already supplies cwd in our new shell. Do not type a second
    # Claude cd; existing caller-provided panes still use their normal bootstrap.
    ingress_note = _ensure_pane_ingress(args.pane, args.kind, None if created_pane else pane_cwd)
    if ingress_note:
        print(f"started=false reason={ingress_note} agent={args.kind} name={args.name} "
              f"pane={args.pane}" + (f" cwd={pane_cwd}" if pane_cwd else "") + cleanup())
        return 1

    # Owned scoped TUI config for OpenCode only: best-effort export, never a
    # refusal. Every other kind keeps its exact previous behavior.
    tui_scoped = None
    if args.kind == "opencode":
        tui_scoped = _export_opencode_tui_scoped(args.pane)

    mode = args.permission_mode or _default_permission_mode()
    agent_args = list(getattr(args, "agent_args", None) or [])
    prefix = list(_PERMISSION_FLAGS.get(args.kind, [])) if mode == "bypass" else []
    # Fresh steward-started Codex TUIs run Embedded so each one holds its own rollout
    # fd (same-cwd simultaneous starts stay exactly attributable); resume/fork and
    # managed-remote launches keep their previous behavior (see _codex_embedded_args).
    embedded = _codex_embedded_args(agent_args) if args.kind == "codex" else []
    project_arg = [pane_cwd] if args.kind == "opencode" and pane_cwd else []
    full_agent_args = embedded + prefix + cwd_flag + project_arg + agent_args

    # An Embedded TUI is attributed only by its own rollout fd (or the exact
    # resolver), never by a launch-time time candidate: while its own thread does
    # not exist yet the before/after binder could take another same-cwd execution's
    # new root rollout for this PID, and a tier-1 record outranks the later own fd.
    # Until the fd exists the session stays unknown and nothing is written. This
    # covers injected and caller-stated Embedded alike (`--no-daemon` in `agent_args`
    # means no injection, but the execution still is Embedded).
    embedded_execution = bool(embedded) or (
        args.kind == "codex" and _codex_stated_no_daemon(agent_args))
    bind_home = bind_before = None
    launched_at = time.time()
    if args.kind == "codex" and not embedded_execution:
        bind_home = _codex_home_dir()
        bind_before = _rollout_paths(bind_home)

    # herdr `agent start <NAME> --kind --pane` — the display name is a required
    # positional (herdr 0.8+ prints `unknown option: <kind>` and starts nothing when
    # it is missing; measured 2026-09-03, F-100 comms test).
    cmd = _herdr_argv("agent", "start", args.name, "--kind", args.kind, "--pane", args.pane)
    if full_agent_args:
        cmd += ["--"] + full_agent_args

    if created_pane:
        readiness = "beside-shell-readiness-timeout"
        while time.monotonic() < created_deadline:
            readiness, created_shell = _wait_for_created_shell(
                args.pane, pane_cwd, created_shell, created_deadline)
            if readiness:
                break
            created_screen = _start_shell_snapshot(
                args.pane, created_shell, deadline=created_deadline)
            if created_screen is not None:
                break
            time.sleep(min(.05, max(0, created_deadline - time.monotonic())))
        if readiness or created_screen is None:
            readiness = readiness or "beside-shell-readiness-timeout"
            print(f"started=false reason={readiness} agent={args.kind} name={args.name} "
                  f"pane={args.pane}" + (f" cwd={pane_cwd}" if pane_cwd else "") + cleanup())
            return 1

    try:
        # `cwd=` here moves only this CLI process, never the launched agent — the agent
        # is put in place by `_CWD_FLAG` above. Kept because herdr itself resolves some
        # relative paths against its caller.
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=args.cwd or None)
    except (OSError, subprocess.SubprocessError):
        if created_pane:
            print(f"started=false reason=herdr-invocation-failed agent={args.kind} "
                  f"name={args.name} pane={args.pane}" + cleanup())
            return 1
        return _unavailable("herdr-invocation-failed")

    # herdr answers `agent_started` with the agent block; any harness can initially
    # omit its SID. Record it when available, otherwise keep the named pane join.
    # `started` needs exit 0 AND no error body (review round 1, #9): a herdr that exits
    # 0 with `{"error": …}` launched nobody.
    started_sid = None
    agent_block = None
    payload_error = None
    try:
        for stream in (proc.stdout, proc.stderr):
            try:
                payload = json.loads(stream or "")
            except ValueError:
                continue
            if isinstance(payload, dict):
                payload_error = payload.get("error")
                agent_block = (payload.get("result") or {}).get("agent")
                break
        if isinstance(agent_block, dict):
            started_sid = peer_message.usable_session_id((agent_block.get("agent_session") or {}).get("value"))
    except Exception:
        agent_block = None
    started = proc.returncode == 0 and not payload_error
    start_verify = None
    code = payload_error.get("code") if isinstance(payload_error, dict) else None
    if not started and code in {"timeout", "agent_pane_busy", "pane_busy"}:
        failure_deadline = time.monotonic() + 5
        observed = _observed_start_agent(args, deadline=failure_deadline, wait=True)
        if observed is not None:
            agent_block = observed
            started_sid = peer_message.usable_session_id((observed.get("agent_session") or {}).get("value"))
            started = True
            start_verify = "observed-pane"
    # herdr can report an error body with exit 0 (e.g. agent_name_taken).
    # Preserve its bounded machine-readable code instead of silently discarding
    # the only explanation of a refused start. A non-JSON native stderr becomes
    # a bounded control-free code; the full stream is never printed here.
    failure_reason = ""
    if not started:
        code = payload_error.get("code") if isinstance(payload_error, dict) else None
        stderr_code = _start_stderr_code(proc.stderr)
        failure_reason = (code if isinstance(code, str)
                          and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", code)
                          else "herdr-start-error" if payload_error
                          else "herdr-stderr-" + stderr_code if stderr_code
                          else "herdr-start-failed")
    cleanup_note = cleanup() if not started else ""
    trust_wait = _native_trust_reason(args.kind, _read_screen(args.pane)) if started else None
    # herdr names no Codex thread for a daemon-attached TUI; the launcher proves it from the
    # one new root rollout (see the block comment above `_BIND_SECONDS`). Embedded
    # starts never enter here (see above): no time candidate becomes a tier-1 record.
    session_bind = None
    if (started and isinstance(agent_block, dict) and args.kind == "codex"
            and not started_sid and not embedded_execution):
        session_bind, bound_sid, tui_pid, tui_cwd = _bind_codex_session(
            args.pane, pane_cwd, bind_home, bind_before, launched_at)
        if session_bind == "bound":
            started_sid = bound_sid
            # The thread id stays in the ledger and receipt either way; only the Fleet registry
            # projection needs the process start time.
            if not _write_bound_registry(tui_pid, bound_sid, tui_cwd, args.name):
                session_bind = "bound-unregistered"
    # A successful agent block or this pane's observed matching runtime proves the
    # start. An unresolved refusal or unparsable answer grants no role marker.
    if started and isinstance(agent_block, dict):
        _mark_started(args, started_sid, mode)
    else:
        _record(to_harness=args.kind, to_name=args.name, kind="steer",
                summary_text=f"[start] {args.name} kind={args.kind} mode={mode}",
                to_session_id=started_sid)
    # `session_id=-` is the launch-time signal that nothing proved which session this is
    # (no ledger endpoint, no board badge, nothing to steer by name); `session_bind=` says
    # why for a Codex start (timeout | ambiguous | no-process | unavailable), `bound` when
    # the launcher itself proved the thread id, and `bound-unregistered` when it did but Fleet's
    # registry record could not be written (no process start time to guard PID reuse).
    #
    # `cwd=` only when one was asked for: it is the receipt that the flag was honored,
    # and an unasked-for value would cost an extra herdr call on every start.
    # `managed=` is read off the started process, not inferred from how it was launched.
    # Interactive managed ingress is retired, so `managed=false` is no longer a defect
    # signal; the field stays for receipt compatibility and `session_id=` is what says
    # whether the session got an identity.
    if started and getattr(args, "beside", None) and args.pane != args.beside:
        _mark_seat_successor(args.pane, args.beside, args.kind, started_sid)
    managed = "-"
    if started and _MANAGED_INGRESS.get(args.kind):
        verdict = _pane_is_managed(args.pane)
        managed = "unknown" if verdict is None else str(verdict).lower()
    print(
        f"started={str(started).lower()} agent={args.kind} name={args.name} "
        f"pane={args.pane} permission_mode={mode} session_id={started_sid or '-'} "
        f"managed={managed}"
        + (f" ready=false reason={trust_wait}" if trust_wait else "")
        + (f" session_bind={session_bind}" if session_bind else "")
        + (f" reason={failure_reason} herdr_rc={proc.returncode}" if failure_reason else "")
        + (f" ingress={ingress_note}" if ingress_note else "")
        + (f" cwd={pane_cwd}" if pane_cwd else "")
        + cleanup_note
        + (f" tui_scoped={tui_scoped}" if tui_scoped else "")
        + (f" start_verify={start_verify}" if start_verify else "")
        + (f" placement=project-workspace workspace={workspace}" if workspace else "")
    )
    return 0


# One normal exit action, without retry or force: Claude's documented command
# avoids its two-key Ctrl+D confirmation. Other harnesses retain one Ctrl+D.
_RETIRE_ACTIONS = {"codex": (("send-keys", "ctrl+d"),),
                   "claude": (("send-text", "/exit"), ("send-keys", "enter")),
                   "opencode": (("send-keys", "ctrl+d"),)}
_RETIRE_SECONDS = 5
# OpenCode can remain foreground while it finishes its normal exit summary.
_OPENCODE_RETIRE_SECONDS = 60


def _retire_process_record(pid):
    """Local kernel identity, including argv, for an observed foreground process."""
    try:
        root = Path(f"/proc/{pid}")
        stat = (root / "stat").read_text().rsplit(") ", 1)[1].split()
        raw = (root / "cmdline").read_bytes()
        if len(raw) > 65536 or not raw or not raw.endswith(b"\0"):
            return None
        argv = [part.decode("utf-8", errors="strict") for part in raw[:-1].split(b"\0")]
        start = stat[19]
        if (not start.isdigit() or _proc_start_ticks(pid) != start
                or os.readlink(root / "ns/pid") != os.readlink("/proc/self/ns/pid")):
            return None
        return {"start": start, "group": int(stat[2]), "argv": argv}
    except (OSError, ValueError, IndexError, UnicodeError):
        return None


def _retire_pane_info(pane, timeout=5):
    try:
        proc = subprocess.run(_herdr_argv("pane", "process-info", "--pane", pane),
                              capture_output=True, text=True, timeout=timeout)
        payload = json.loads(proc.stdout or "")
        result = payload.get("result") if isinstance(payload, dict) else None
        info = result.get("process_info") if isinstance(result, dict) else None
        if (proc.returncode or not isinstance(payload, dict) or payload.get("error")
                or not isinstance(info, dict) or info.get("pane_id") != pane
                or any(type(info.get(key)) is not int or info[key] <= 0
                       for key in ("shell_pid", "foreground_process_group_id"))
                or not isinstance(info.get("foreground_processes"), list)
                or not info["foreground_processes"]):
            return None
        return info
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _retire_foreground(pane, harness, *, strict=True):
    info = _retire_pane_info(pane)
    if info is None or (strict and len(info["foreground_processes"]) != 1):
        return None
    leaders = [item for item in info["foreground_processes"]
               if isinstance(item, dict)
               and item.get("pid") == info["foreground_process_group_id"]]
    if len(leaders) != 1:
        return None
    item = leaders[0]
    pid = item.get("pid") if isinstance(item, dict) else None
    argv = item.get("argv") if isinstance(item, dict) else None
    if (type(pid) is not int or pid <= 0 or pid == info["shell_pid"]
            or pid != info["foreground_process_group_id"]
            or not isinstance(argv, list) or not argv
            or any(not isinstance(arg, str) for arg in argv)
            or os.path.basename(argv[0]) != harness):
        return None
    identity = _retire_process_record(pid)
    shell_start = _proc_start_ticks(info["shell_pid"])
    if (identity is None or not shell_start or identity["argv"] != argv
            or identity["group"] != info["foreground_process_group_id"]):
        return None
    return {"pid": pid, **identity, "shell_pid": info["shell_pid"],
            "shell_start": shell_start}


def _retire_shell_returned(info, identity):
    if info is None or info["shell_pid"] != identity["shell_pid"]:
        return False
    shell = identity["shell_pid"]
    if (info["foreground_process_group_id"] != shell
            or _proc_start_ticks(shell) != identity["shell_start"]):
        return False
    # Prompt helpers can share the original shell's foreground group. The
    # recorded agent's lifetime, not the number of foreground processes, decides
    # whether it exited. Unreadability is distinct from absence or PID reuse.
    try:
        os.stat(f"/proc/{identity['pid']}")
    except FileNotFoundError:
        return True
    except OSError:
        return False
    start = _proc_start_ticks(identity["pid"])
    return start is not None and start != identity["start"]


def _retire_target(target):
    try:
        state, ident, rc, reason = _interpret_payload(_run_herdr_get(target), target)
        if any(not isinstance(value, str) or not value for value in ident.values()):
            raise ValueError("malformed target")
        return state, ident, rc, reason
    except (AttributeError, TypeError, ValueError):
        return ("herdr-unavailable", {"harness": "-", "session_id": "-",
                "name": target, "pane": "-"}, 4, "herdr-protocol-error")


def _retire_request_identity(ident, foreground):
    exact = {"server": _HERDR_SESSION or "default", "pane": ident["pane"],
             "harness": ident["harness"], "session_id": ident["session_id"],
             "name": ident["name"], "foreground": foreground}
    return exact


def _retire_booked_foreground(duty):
    intent = duty.get("intent") or {}
    original = (intent.get("identity") or {}).get("foreground")
    observation = duty.get("observation") or {}
    if not original and observation.get("phase") in {"exit-requested", "shell-returned"}:
        # An older release recorded the exact lifetime when it sent the exit.
        original = observation.get("foreground")
    return original


def _retire_subject_state(duty):
    """The booked agent's kernel lifetime, never the pane's current occupant."""
    original = _retire_booked_foreground(duty)
    if not isinstance(original, dict) or not original.get("pid") or not original.get("start"):
        return "unknown"
    current = _retire_process_record(original["pid"])
    start = current["start"] if current is not None else _proc_start_ticks(original["pid"])
    if start is not None:
        return "same" if start == original["start"] else "gone"
    try:
        os.stat(f"/proc/{original['pid']}")
    except FileNotFoundError:
        return "gone"
    except OSError:
        pass
    return "unknown"


def _complete_gone_retire(store, duty):
    handover = _retire_handover(duty)
    phase = (duty.get("observation") or {}).get("phase", "waiting")
    settled = store.update(duty["id"], state="complete", result="target-already-gone",
                           delivery="completed", cleanup="complete", expected_phases={phase},
                           observation={"phase": "complete", "reason": "target-already-gone"})
    if settled.get("state") == "complete":
        ident = (duty.get("intent") or {}).get("identity") or {}
        print(f"retired=true reason=target-already-gone agent={ident.get('harness', '-')} "
              f"name={ident.get('name', '-')} pane={ident.get('pane', '-')}"
              + (f" handover={handover}" if handover else ""))
        return True
    return False


def _retire_request(target, exact):
    duty_id = peer_obligations.stable_duty_id("retire", exact, target)
    store = peer_obligations.ObligationStore()
    duty = store.get(duty_id)
    retry_of = None
    # Cancellation is history, not an outstanding exit claim. Derive the next
    # id from that history so repeated requests still converge on one duty.
    while duty is not None and duty.get("state") == "cancelled":
        retry_of = duty_id
        duty_id = peer_obligations.stable_duty_id("retire", exact, retry_of)
        duty = store.get(duty_id)
    if duty is None:
        own_sid, own_harness = _current_session_identity()
        intent = {"target": target, "requester": {
            "session_id": own_sid, "harness": own_harness,
            "pane": _caller_pane(), "server": _HERDR_SESSION or "default"}}
        if retry_of:
            intent["retry_of"] = retry_of
        duty = store.create(duty_id, "retire", exact, intent)
    peer_obligations.ensure_runner()
    return store, duty


def _finish_retire_cleanup(store, duty, ident, foreground):
    """Resume only shell-return/close work; never send an exit key from here."""
    pane = ident.get("pane", "")
    server = (duty.get("intent") or {}).get("identity", {}).get("server") or "default"
    global _HERDR_SESSION
    old_server = _HERDR_SESSION
    _HERDR_SESSION = None if server == "default" else server
    try:
        info = _retire_pane_info(pane)
        still_exact = (
            isinstance(info, dict)
            and info.get("pane_id") == pane
            and info.get("shell_pid") == foreground.get("shell_pid")
            and info.get("foreground_process_group_id") == foreground.get("shell_pid")
            and _proc_start_ticks(foreground.get("shell_pid")) == foreground.get("shell_start")
        )
        if not still_exact or not _retire_shell_returned(info, foreground):
            if _retire_subject_state(duty) == "gone":
                return 0 if _complete_gone_retire(store, duty) else 1
            current = store.get(duty["id"]) or duty
            observation = {**(current.get("observation") or {}),
                           "phase": "shell-returned", "reason": "shell-return-unverified"}
            store.update(duty["id"], state="cleanup-pending", observation=observation,
                         cleanup="pending")
            return 1
        final_info = _retire_pane_info(pane)
        if (not final_info or final_info.get("shell_pid") != foreground.get("shell_pid")
                or _proc_start_ticks(foreground.get("shell_pid")) != foreground.get("shell_start")
                or not _retire_shell_returned(final_info, foreground)):
            if _retire_subject_state(duty) == "gone":
                return 0 if _complete_gone_retire(store, duty) else 1
            return 1
        handover = _retire_handover(duty)
        if not _close_pane(pane):
            current = store.get(duty["id"]) or duty
            store.update(duty["id"], state="cleanup-pending",
                         observation={**(current.get("observation") or {}),
                                      "phase": "shell-returned", "reason": "pane-close-failed"},
                         cleanup="pending")
            return 1
        store.update(duty["id"], state="complete", result="normal-exit",
                     delivery="completed", cleanup="complete",
                     observation={"phase": "complete", "reason": "normal-exit"})
        _record(to_harness=ident["harness"], to_name=ident["name"], kind="notice",
                to_session_id=ident["session_id"], to_pane=pane,
                summary_text=f"[retire] {ident['name']} normal-exit",
                receipt="normal-exit", status="sent")
        print(f"retired=true reason=normal-exit agent={ident['harness']} "
              f"name={ident['name']} pane={pane}"
              + (f" handover={handover}" if handover else ""))
        return 0
    finally:
        _HERDR_SESSION = old_server


def _resume_retire_obligation(duty, store):
    global _HERDR_SESSION
    intent = duty.get("intent") or {}
    ident = dict((duty.get("intent") or {}).get("identity") or {})
    target = intent.get("target") or ""
    observation = duty.get("observation") or {}
    phase = observation.get("phase", "waiting")
    if duty.get("state") == "complete":
        if duty.get("result") in {"normal-exit", "target-already-gone"}:
            _retire_handover(duty)
        return
    if duty.get("state") == "cancelled":
        return
    if phase == "shell-returned":
        foreground = observation.get("foreground") or {}
        if foreground:
            _finish_retire_cleanup(store, duty, ident, foreground)
        return
    if phase == "exit-requested":
        foreground = observation.get("foreground") or {}
        server = ident.get("server") or "default"
        old_server = _HERDR_SESSION
        _HERDR_SESSION = None if server == "default" else server
        try:
            info = _retire_pane_info(ident.get("pane", ""))
            if info is not None and foreground and _retire_shell_returned(info, foreground):
                claimed = store.claim_phase(
                    duty["id"], {"exit-requested"}, "shell-returned",
                    state="cleanup-pending", extra={"foreground": foreground})
                if claimed:
                    _finish_retire_cleanup(store, claimed, ident, foreground)
            elif _retire_subject_state(duty) == "gone":
                _complete_gone_retire(store, duty)
            else:
                store.update(duty["id"], state="pending",
                             expected_phases={"exit-requested"},
                             observation={**observation, "reason": "exit-result-unobserved"})
        finally:
            _HERDR_SESSION = old_server
        return
    server = ident.get("server") or "default"
    old_server = _HERDR_SESSION
    _HERDR_SESSION = None if server == "default" else server
    try:
        subject = _retire_subject_state(duty)
        if subject == "gone":
            _complete_gone_retire(store, duty)
            return
        if subject == "unknown":
            store.update(duty["id"], state="unknown", expected_phases={"waiting"},
                         observation={"phase": "waiting", "reason": "foreground-unverified"})
            return
        state, observed, _code, reason = _retire_target(target)
        if (reason or observed.get("harness") != ident.get("harness")
                or (ident.get("session_id") != "-"
                    and observed.get("session_id") != ident.get("session_id"))
                or observed.get("pane") != ident.get("pane")):
            store.update(duty["id"], state="unknown",
                         expected_phases={"waiting"},
                         observation={"phase": "waiting", "reason": reason or "target-identity-changed"})
            return
        readiness = _pane_readiness(target, state,
                                    expected_harness=ident["harness"],
                                    expected_sid=observed["session_id"])
        if readiness.state != "ready":
            store.update(duty["id"], state="unknown" if readiness.state == "unknown" else "pending",
                         expected_phases={"waiting"},
                         observation={"phase": "waiting", "reason": readiness.reason,
                                      "state": readiness.state})
            return
        cmd_retire(argparse.Namespace(target=target, _resume_request_id=duty["id"]))
    finally:
        _HERDR_SESSION = old_server


def cmd_retire(args):
    target = args.target
    ident = {"harness": "-", "session_id": "-", "name": target, "pane": "-"}
    store = peer_obligations.ObligationStore()
    duty_id = getattr(args, "_resume_request_id", None)
    duty = store.get(duty_id) if duty_id else None

    def finish(reason, retired=False, handover=None, detail=None, pending=False):
        tail = f" handover={handover}" if handover else ""
        summary = f"[retire] {target} {reason}{tail}"
        receipt = reason
        if detail:
            summary += f" {detail}"
            receipt += f" {detail}"
            print(detail, file=sys.stderr)
        if duty:
            current = store.get(duty["id"]) or duty
            observation = dict(current.get("observation") or {})
            if retired:
                store.update(duty["id"], state="complete", result="normal-exit",
                             delivery="completed", cleanup="complete",
                             observation={"phase": "complete", "reason": reason})
            elif reason == "self-target":
                store.update(duty["id"], state="cancelled", cleanup="complete",
                             observation={"phase": "cancelled", "reason": reason})
            elif reason == "pane-close-failed" and observation.get("foreground"):
                store.update(duty["id"], state="cleanup-pending",
                             observation={**observation, "phase": "shell-returned",
                                          "reason": reason}, cleanup="pending")
            elif pending:
                store.update(duty["id"], state="unknown" if reason.endswith("unknown") else "pending",
                             expected_phases={observation.get("phase", "waiting")},
                             observation={**observation, "reason": reason})
        _record(to_harness=ident["harness"], to_name=ident["name"], kind="notice",
                to_session_id=ident["session_id"], to_pane=ident["pane"],
                summary_text=summary, receipt=receipt,
                status="sent" if retired else "unknown" if pending else "failed")
        print(f"retired={str(retired).lower()} reason={reason} agent={ident['harness']} "
              f"name={ident['name']} pane={ident['pane']}{tail}"
              + (f" pending=true request_id={duty_id}" if pending and duty_id else ""))
        return 0 if retired or pending else 1

    if _herdr_missing():
        return finish("herdr-not-found")
    state, ident, _, reason = _retire_target(target)
    if not isinstance(ident.get("pane"), str) or ident["pane"] in {"", "-"}:
        # The pane may be gone while the accepted request still proves the
        # lifetime and its successor. Never borrow another requester's duty.
        own_sid, own_harness = _current_session_identity()
        own_pane = _caller_pane()
        matches = [row for row in store.list(states=peer_obligations._PENDING_STATES | {"complete"})
                   if row.get("intent", {}).get("kind") == "retire"
                   and row["intent"].get("target") == target
                   and row["intent"].get("requester") == {
                       "session_id": own_sid, "harness": own_harness,
                       "pane": own_pane, "server": _HERDR_SESSION or "default"}]
        if own_sid and own_pane and len(matches) == 1:
            saved = matches[0]
            if saved.get("state") == "complete" and saved.get("result") in {"normal-exit", "target-already-gone"}:
                handover = _retire_handover(saved)
                print("retired=true reason=already-complete"
                      + (f" handover={handover}" if handover else ""))
                return 0
            if _retire_subject_state(saved) == "gone" and _complete_gone_retire(store, saved):
                return 0
        return finish(reason or "pane-unverified")
    pane, harness = ident["pane"], ident["harness"]
    if duty is not None:
        requester = (duty.get("intent") or {}).get("requester") or {}
        own_sid, own_harness = requester.get("session_id"), requester.get("harness")
        own_pane = requester.get("pane")
        own_server = requester.get("server", (duty.get("intent") or {}).get(
            "identity", {}).get("server", "default"))
    else:
        own_sid, own_harness = _current_session_identity()
        own_pane, own_server = _caller_pane(), _HERDR_SESSION or "default"
    if ((own_pane and pane == own_pane and own_server == (_HERDR_SESSION or "default"))
            or (own_sid and ident["session_id"] == own_sid and harness == own_harness)):
        return finish("self-target")
    if harness not in _RETIRE_ACTIONS:
        return finish("normal-exit-unverified")
    if duty is None:
        if getattr(args, "_resume_request_id", None):
            return finish("retire-obligation-missing", pending=True)
        foreground = _retire_foreground(pane, harness, strict=False)
        exact = _retire_request_identity(ident, foreground)
        store, duty = _retire_request(target, exact)
        duty_id = duty["id"]
    else:
        duty_id = duty["id"]
    if duty.get("state") == "complete":
        _resume_retire_obligation(duty, store)
        print(f"retired=true reason=already-complete agent={harness} "
              f"name={ident['name']} pane={pane}")
        return 0
    if duty.get("state") == "cancelled":
        return finish("retire-cancelled")
    phase = (duty.get("observation") or {}).get("phase", "waiting")
    if phase != "waiting":
        return finish("retire-already-pending", pending=True)
    subject = _retire_subject_state(duty)
    if subject == "gone":
        if _complete_gone_retire(store, duty):
            return 0
        return finish("retire-already-pending", pending=True)
    if subject == "unknown":
        return finish("foreground-unverified", pending=True)
    booked = (duty.get("intent") or {}).get("identity") or {}
    if (booked.get("pane") != pane or booked.get("harness") != harness
            or (booked.get("session_id") != "-"
                and booked.get("session_id") != ident["session_id"])):
        return finish("target-changed", pending=True)
    if reason or state not in {"idle", "done"}:
        return finish(reason or f"agent-{state}", pending=True)
    readiness = _pane_readiness(target, state, expected_harness=harness,
                                expected_sid=ident["session_id"])
    if readiness.state != "ready":
        return finish(readiness.reason, pending=True)
    identity = _retire_foreground(pane, harness)
    if identity is None:
        return finish("foreground-unverified", pending=True)
    if identity != _retire_booked_foreground(duty):
        return finish("foreground-changed", pending=True)
    state2, ident2, _, _ = _retire_target(target)
    if state2 not in ("idle", "done") or ident2 != ident:
        return finish("target-changed", pending=True)
    if _retire_foreground(pane, harness) != identity:
        return finish("foreground-changed", pending=True)
    lines = _read_screen(target)
    screen_reason = (_native_trust_reason(harness, lines) or _screen_ready(harness, lines)) if lines else "screen-unavailable"
    if screen_reason:
        return finish(screen_reason, pending=True)
    claimed = store.claim_phase(
        duty_id, {"waiting"}, "exit-requested", state="pending",
        extra={"foreground": identity, "reason": "normal-exit-requested"})
    if claimed is None:
        return finish("retire-duty-already-claimed", pending=True)
    duty = claimed
    try:
        for operation, value in _RETIRE_ACTIONS[harness]:
            if _retire_foreground(pane, harness) != identity:
                return finish("foreground-changed", pending=True)
            sent = subprocess.run(_herdr_argv("pane", operation, pane, value),
                                  capture_output=True, text=True, timeout=5)
            send_error = False
            if (sent.stdout or sent.stderr).strip():
                try:
                    payload = json.loads(sent.stdout or sent.stderr)
                    send_error = not isinstance(payload, dict) or bool(payload.get("error"))
                except ValueError:
                    send_error = True
            if sent.returncode or send_error:
                return finish("exit-send-unknown", pending=True)
    except (OSError, subprocess.SubprocessError):
        return finish("exit-send-unknown", pending=True)
    wait_seconds = _OPENCODE_RETIRE_SECONDS if harness == "opencode" else _RETIRE_SECONDS
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        info = _retire_pane_info(pane, timeout=max(.1, deadline - time.monotonic()))
        if info is None:
            return finish("shell-return-unverified", pending=True)
        if _retire_shell_returned(info, identity):
            transitioned = store.claim_phase(
                duty_id, {"exit-requested"}, "shell-returned", state="cleanup-pending",
                extra={"foreground": identity, "reason": "shell-returned"})
            if transitioned is None:
                return finish("shell-return-unverified", pending=True)
            duty = transitioned
            cleanup_rc = _finish_retire_cleanup(store, duty, ident, identity)
            if cleanup_rc:
                current = store.get(duty_id) or duty
                cleanup_reason = (current.get("observation") or {}).get("reason", "cleanup-pending")
                return finish(cleanup_reason, pending=True)
            return 0
        time.sleep(min(.1, max(0, deadline - time.monotonic())))
    if harness == "claude":
        return _retire_claude_background_confirm(
            target, pane, harness, ident, identity, finish,
            own_sid, own_harness, duty=duty)
    return finish("agent-still-running", pending=True)


def _retire_background_dialog_lines(lines):
    """Detect Claude's exit-time background-work confirm; return its task lines or None.

    Only the live selection UI at the very bottom of the screen counts: the
    trailing non-empty lines must be the title, the three consecutive option
    lines, then the confirm/cancel footer, with nothing below. A selected
    option carries a leading cursor (`❯ 1. …`). Quoted dialog text up in the
    conversation history -- or an answer quoting it above fresh content --
    never counts. No keys are sent unless the window is certainly showing
    this dialog now.
    """
    if not lines:
        return None
    texts = [_plain(cells).strip() for cells in lines]
    nonempty = [text for text in texts if text]
    while nonempty and nonempty[-1] in ("❯", "›", ">"):
        nonempty.pop()
    if len(nonempty) < 5:
        return None
    foot = "".join(nonempty[-1].split()).lower()
    if ("entertoconfirm" not in foot and "entertoselect" not in foot) or \
            ("esctocancel" not in foot and "esccancel" not in foot):
        return None

    def _opt_number(text):
        stripped = text.lstrip("❯›* ").strip()
        low = "".join(stripped.split()).lower()
        for number, prefixes in ((1, ("1.exitandstoptasks", "1:exitandstoptasks")),
                                 (2, ("2.movetobackground", "2:movetobackground")),
                                 (3, ("3.stay", "3:stay"))):
            if any(low.startswith(prefix) for prefix in prefixes):
                return number
        return None

    options = nonempty[-4:-1]
    if [_opt_number(text) for text in options] != [1, 2, 3]:
        return None
    title_window = " ".join(nonempty[-8:-3])
    if "backgroundworkisrunning" not in "".join(title_window.split()).lower():
        return None
    tasks = [text for text in nonempty[-8:-4]
             if "background work is running" not in text.lower()]
    seen, unique = set(), []
    for task in tasks:
        if task not in seen:
            seen.add(task)
            unique.append(task)
    return unique[:5]


def _retire_claude_background_confirm(target, pane, harness, ident, identity, finish,
                                      own_sid, own_harness, *, duty=None):
    """Finish one normal retire path through Claude's background-work confirm.

    The default picks 1 (exit and stop tasks): retire closes a handed-over
    session, and registered hearting work and compute-hosts runs live apart
    from the session, so only the session's own shell, reservations and
    monitors stop. 2 (move to background) is never used -- it forks the
    session itself into the background, where it keeps running and can
    collide with its successor. The stopped lines stay in the receipt. When
    the window is ambiguous the dialog is closed with a typed 3 (Stay) and
    the reason is returned; the pane is left usable, never stuck open. No
    keys go out unless the live selection UI is certain.
    """
    lines = _read_screen(target)
    tasks = _retire_background_dialog_lines(lines)
    if tasks is None:
        return finish("agent-still-running", pending=True)
    stopped = " stopped-background=" + json.dumps(tasks, ensure_ascii=False) if tasks else ""
    if _retire_foreground(pane, harness) != identity:
        return finish("foreground-changed", pending=True)
    if _retire_background_dialog_lines(_read_screen(target)) is None:
        return finish("agent-still-running", pending=True)
    try:
        subprocess.run(_herdr_argv("pane", "send-text", pane, "1"),
                       capture_output=True, text=True, timeout=5)
        subprocess.run(_herdr_argv("pane", "send-keys", pane, "enter"),
                       capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return finish("exit-send-failed", pending=True)
    deadline = time.monotonic() + _RETIRE_SECONDS
    while time.monotonic() < deadline:
        info = _retire_pane_info(pane, timeout=max(.1, deadline - time.monotonic()))
        if info is None:
            return finish("shell-return-unverified", pending=True)
        if _retire_shell_returned(info, identity):
            if not _retire_shell_returned(_retire_pane_info(pane), identity):
                return finish("shell-changed", pending=True)
            handover = _retire_handover(duty) if duty is not None else _seat_handover(ident, own_sid, own_harness)
            if not _close_pane(pane):
                return finish("pane-close-failed", handover=handover, pending=True)
            return finish("normal-exit", True, handover=handover,
                          detail=(f"background-stopped:{stopped.strip()}") if stopped else None)
        time.sleep(min(.1, max(0, deadline - time.monotonic())))
    try:
        subprocess.run(_herdr_argv("pane", "send-text", pane, "3"),
                       capture_output=True, text=True, timeout=5)
        subprocess.run(_herdr_argv("pane", "send-keys", pane, "enter"),
                       capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return finish("agent-still-running", pending=True)
    return finish("retire-declined-background-work" + (stopped or ""), pending=True)


# ---------------------------------------------------------------------------
# SD-122 (10) detached steward watch: watch / join / status / rearm / ack
# ---------------------------------------------------------------------------

_WATCH_SCHEMA = 1
_WATCH_STATES = _AGENT_STATES + ("timeout", "agent-not-found", "herdr-unavailable")
_JOIN_EXIT = {"timeout": 3, "agent-not-found": 2, "herdr-unavailable": 4}
_WATCH_OBSERVER_TIMEOUT_MS = 30_000
_WATCH_BACKOFF_MAX_SECONDS = 15.0


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
    observation: Path


def _watch_paths(watch_id, root=None):
    root = root or _watch_root()
    return _WatchPaths(
        arm=root / f"{watch_id}.json",
        lock=root / f"{watch_id}.lock",
        log=root / f"{watch_id}.log",
        receipt=root / f"{watch_id}.receipt.json",
        ack=root / f"{watch_id}.ack.json",
        observation=root / f"{watch_id}.observation.json",
    )


def _new_watch_id(steward_sid, target, armed_ts, nonce):
    raw = f"{steward_sid}|{target}|{armed_ts}|{nonce}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def _dedupe_key(steward_sid, target, until, server=None):
    # `until=[]` (herdr's default set) and `until=["idle","done","blocked"]` are
    # the same *behaviour* but stay distinct keys on purpose: (10) dedupes on the
    # "until 집합" as given, and silently normalizing them would suppress a
    # legitimate second watch.
    raw = f"{server or 'default'}|{steward_sid}|{target}|{'|'.join(sorted(until or []))}".encode("utf-8", "replace")
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


def _watch_identity_matches(expected, observed):
    """Exact harness and pane, and the session -- except that a watch armed
    before its target had one (a fresh Codex) takes the first session the pane
    reports; `expected` keeps it, so every later check needs that session."""
    if any(expected.get(key) in {None, "", "-"} or observed.get(key) != expected[key]
           for key in ("harness", "pane")):
        return False
    if expected.get("session_id") == "-" and observed.get("session_id") not in {None, "", "-"}:
        expected["session_id"] = observed["session_id"]
    return observed.get("session_id") == expected.get("session_id")


def _persist_watch_session(args, paths, server, session_id):
    """Write the session a pane-armed watch took into its arm, under the arm
    claim, so dedupe and a restarted observer hold that session from now on."""
    claim = paths.arm.parent / f"{_dedupe_key(args.steward_session_id, args.target, list(args.until or []), server)}.arm"
    try:
        claim_fd = _open_lock(claim)
    except OSError:
        return False
    try:
        if not _acquire_bounded(claim_fd, _CLAIM_TIMEOUT_MS):
            return False
        arm = _read_json(paths.arm)
        agent = arm.get("agent") if isinstance(arm, dict) else None
        if isinstance(agent, dict) and agent.get("session_id") == "-":
            _write_json_atomic(paths.arm, {**arm, "agent": {**agent, "session_id": session_id}})
        return True
    finally:
        os.close(claim_fd)


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
        wake = "hook" if _caller_is_claude() else "none"

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
    server = _HERDR_SESSION or "default"
    claim = root / f"{_dedupe_key(steward_sid, target, until, server)}.arm"
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
        # The session may not exist yet (a fresh Codex before its first input):
        # the watcher then follows the pane and takes the first session it reports.
        if any(agent.get(key) in {None, "", "-"} for key in ("harness", "pane")):
            return _unavailable("watch-identity-unverified")

        if existing_id:
            existing_paths = _watch_paths(existing_id, root)
            existing_arm = _read_json(existing_paths.arm)
            if (not existing_paths.receipt.exists() and isinstance(existing_arm, dict)
                    and (existing_arm.get("server") or "default") == server
                    and _watch_identity_matches(dict(existing_arm.get("agent") or {}), agent)):
                if not _watcher_present(existing_arm):
                    _spawn_watch_observer(existing_id, existing_arm)
                    existing_arm = _read_json(existing_paths.arm) or existing_arm
                print(_already_armed_line(existing_id, existing_arm, existing_paths))
                return 0

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
            "--server", server,
            "--expected-harness", agent["harness"], "--expected-session-id", agent["session_id"],
            "--expected-pane", agent["pane"],
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

        arm = {
            "schema_version": _WATCH_SCHEMA,
            "watch_id": watch_id,
            "target": target,
            "until": until,
            "timeout": args.timeout,
            "refs": list(args.ref or []),
            "server": server,
            "agent": {key: agent.get(key, "-") for key in ("harness", "session_id", "name", "pane")},
            "steward": {
                "harness": steward_harness,
                "session_id": steward_sid,
                "project": steward_project,
            },
            "armed_ts": armed_ts,
            "wake": wake,
            "watcher": {},
            "rearmed_from": getattr(args, "rearmed_from", None) or None,
            "rearm_count": int(args.rearm_count or 0),
            "observer_generation": 1,
        }
        _write_json_atomic(paths.arm, arm)
        # Acceptance and the stable claim precede the observer side effect.
        try:
            os.ftruncate(claim_fd, 0)
            os.lseek(claim_fd, 0, os.SEEK_SET)
            os.write(claim_fd, watch_id.encode("ascii"))
            os.fsync(claim_fd)
        except OSError:
            pass

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
            "server": server,
            "agent": {key: agent.get(key, "-") for key in ("harness", "session_id", "name", "pane")},
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
            "observer_generation": 1,
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

    global _HERDR_SESSION
    server = getattr(args, "server", "default") or "default"
    _HERDR_SESSION = None if server == "default" else server
    expected = {
        "harness": getattr(args, "expected_harness", "-"),
        "session_id": getattr(args, "expected_session_id", "-"),
        "pane": getattr(args, "expected_pane", "-"),
    }
    checkpoint_ms = args.timeout if args.timeout is not None else _WATCH_OBSERVER_TIMEOUT_MS
    checkpoint_ms = min(600_000, max(500, int(checkpoint_ms)))
    backoff = 1.0
    unbound = expected["session_id"] == "-"
    while True:
        resolved, current, _code, reason = _interpret_payload(_run_herdr_get(args.target), args.target)
        exact = (reason is None and resolved not in {"agent-not-found", "herdr-unavailable"}
                 and _watch_identity_matches(expected, current))
        if unbound and expected["session_id"] != "-":
            unbound = not _persist_watch_session(args, paths, server, expected["session_id"])
        if exact:
            payload = _run_herdr_wait(args.target, args.until, checkpoint_ms)
            state, agent, _code, reason = _interpret_payload(payload, args.target)
            if (reason is None and state not in {"timeout", "agent-not-found", "herdr-unavailable"}
                    and _watch_identity_matches(expected, agent)):
                if state in {"idle", "done"}:
                    readiness = _pane_readiness(
                        args.target, state, expected_harness=expected["harness"],
                        expected_sid=expected["session_id"], expected_pane=expected["pane"])
                    if readiness.state == "ready":
                        break
                    reason = readiness.reason
                else:
                    break
            reason = reason or state
        elif reason is None:
            reason = "target-identity-changed"
        # Timeout and transport loss are checkpoints. Keep the same duty and
        # re-resolve the exact server/pane/session before another bounded wait.
        _write_json_atomic(paths.observation, {
            "schema_version": _WATCH_SCHEMA, "watch_id": args.watch_id,
            "state": "pending", "reason": str(reason or "observation-unknown"),
            "server": server, "agent": current if exact else expected,
            "observed_ts": _utc_now(),
            "observer_generation": getattr(args, "rearm_count", 0) + 1,
        })
        time.sleep(backoff)
        backoff = min(_WATCH_BACKOFF_MAX_SECONDS, backoff * 1.7)

    pid = os.getpid()
    receipt = {
        "schema_version": _WATCH_SCHEMA,
        "watch_id": args.watch_id,
        "target": args.target,
        "server": server,
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
        "observer_generation": getattr(args, "rearm_count", 0) + 1,
    }
    _write_json_atomic(paths.receipt, receipt)
    try:
        paths.observation.unlink()
    except FileNotFoundError:
        pass

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
        if (name.endswith(".receipt.json") or name.endswith(".ack.json")
                or name.endswith(".observation.json") or name.startswith(".")):
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


def _spawn_watch_observer(watch_id, arm):
    """Reattach the observer to accepted intent without changing its identity."""
    if not isinstance(arm, dict) or arm.get("watch_id") != watch_id:
        return False
    paths = _watch_paths(watch_id)
    if paths.receipt.exists() or _watcher_present(arm):
        return True
    agent = arm.get("agent") or {}
    if (any(not isinstance(agent.get(key), str) or agent.get(key) in {"", "-"}
            for key in ("harness", "pane"))
            or not isinstance(agent.get("session_id"), str) or not agent["session_id"]):
        _write_json_atomic(paths.observation, {
            "schema_version": _WATCH_SCHEMA, "watch_id": watch_id,
            "state": "unknown", "reason": "legacy-watch-identity-unavailable",
            "observed_ts": _utc_now(),
        })
        return False
    if _herdr_missing():
        # Fail fast without spawning an observer that could only checkpoint
        # forever: the duty stays unknown-pending for the next reconnect.
        _write_json_atomic(paths.observation, {
            "schema_version": _WATCH_SCHEMA, "watch_id": watch_id,
            "state": "unknown", "reason": "herdr-not-found",
            "observed_ts": _utc_now(),
        })
        return False
    lock_fd = _open_lock(paths.lock)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        os.set_inheritable(lock_fd, True)
        log_fd = os.open(str(paths.log), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        server = arm.get("server") or "default"
        argv = [
            sys.executable, str(Path(__file__).resolve()), "__watch-run",
            "--watch-id", watch_id, "--target", arm["target"], "--server", server,
            "--expected-harness", agent["harness"], "--expected-session-id", agent["session_id"],
            "--expected-pane", agent["pane"],
            "--steward-harness", (arm.get("steward") or {}).get("harness", "unknown"),
            "--steward-session-id", (arm.get("steward") or {}).get("session_id", ""),
            "--steward-project", (arm.get("steward") or {}).get("project", ""),
            "--armed-ts", arm.get("armed_ts", ""),
            "--rearm-count", str(int(arm.get("observer_generation", 1))),
            "--lock-fd", str(lock_fd),
        ]
        for state in arm.get("until") or []:
            argv.extend(("--until", state))
        for ref in arm.get("refs") or []:
            argv.extend(("--ref", ref))
        if arm.get("timeout") is not None:
            argv.extend(("--timeout", str(arm["timeout"])))
        env = dict(os.environ)
        if server == "default":
            env.pop("AGENT_HERDR_SESSION", None)
        else:
            env["AGENT_HERDR_SESSION"] = server
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True, pass_fds=(lock_fd,),
                cwd=str(paths.arm.parent), env=env,
            )
        finally:
            os.close(log_fd)
        # Re-read: the observer may have written the session it took meanwhile.
        current_arm = _read_json(paths.arm)
        updated = dict(current_arm if isinstance(current_arm, dict) else arm)
        updated["observer_generation"] = int(arm.get("observer_generation", 1)) + 1
        updated["rearm_count"] = int(arm.get("rearm_count", 0)) + 1
        updated["watcher"] = {"pid": proc.pid, "pid_start": process_start_ticks(proc.pid) or ""}
        _write_json_atomic(paths.arm, updated)
        return True
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        return False
    finally:
        os.close(lock_fd)


def _ensure_watch_observers():
    root = _watch_root()
    for watch_id, arm in _watch_entries(root):
        if not isinstance(arm, dict) or _watch_paths(watch_id, root).receipt.exists():
            continue
        if not _watcher_present(arm):
            _spawn_watch_observer(watch_id, arm)


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
        "watcher": {"pid": watcher.get("pid", "-"), "pid_start": watcher.get("pid_start", "-")},
        "receipt": str(paths.receipt),
        "ack": str(paths.ack),
        "receipt_state": (receipt or {}).get("state"),
        "agent": (receipt or {}).get("agent") or {},
        "server": (arm or {}).get("server") or (receipt or {}).get("server") or "default",
        "observation_state": observation.get("state", "pending" if receipt is None else "settled"),
        "observation_reason": observation.get("reason"),
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
    if _spawn_watch_observer(watch_id, arm):
        print(f"watch_id={watch_id} state=rearmed receipt={paths.receipt}")
        print(steward_fields(
            arm.get("wake"), watch_id, agent_home=Path(__file__).resolve().parents[1],
            arms_hook=False, timeout_ms=arm.get("timeout"),
        ))
        return 0
    print(f"watch_id={watch_id} state=pending reason=observer-restart-unavailable receipt={paths.receipt}")
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


def _bottom_form_tokens(target, window=15):
    """Whether form tokens show in the bottom `window` screen lines.

    A real form always renders at the bottom: even on a narrow pane its
    wrapped footer words land in the trailing lines, and joining them
    reassembles the tokens. Transcript quotes higher up never count. A quote
    sitting in the bottom lines can still misfire, but that only delays the
    send now (the flush redelivers later) instead of stranding it with no
    redelivery (2026-10-07).
    """
    try:
        proc = subprocess.run(_herdr_argv("agent", "read", target, "--source", "visible"),
                              capture_output=True, text=True, timeout=_herdr_get_timeout())
    except (OSError, subprocess.SubprocessError):
        return False
    lines = (proc.stdout or "").splitlines()[-window:]
    flat = "".join("".join(lines).split()).lower()
    return any(token in flat for token in _FORM_TOKENS)


def _prompt_form_open(target, state_before):
    """Whether a prompt send must withhold keystrokes from `target`.

    `blocked` is herdr's own verdict and always counts. A `working` target
    keeps the conservative whole-buffer scan: a mid-turn permission prompt on
    a narrow pane leaves no other trace, and typing into it destroys state.
    Any other state needs the tokens at the very bottom of the screen;
    transcript mentions of forms must not strand messages (2026-10-07).
    """
    if state_before == "blocked":
        return True
    if state_before == "working":
        return _form_open(target)
    return _bottom_form_tokens(target)


def _pane_readiness(target, state, *, expected_harness=None, expected_sid=None, expected_pane=None,
                    purpose="completion"):
    """Use the shared projection with exact native identity and registered bindings.

    Before a harness's first input herdr reports no session. The session the
    pane's foreground process proves is then the pane's own (Codex 0.162 opens
    its thread at start) and keeps the bound-work checks; a pane with neither
    is exact by pane and shell birth: nothing can be bound to it, so its native
    turn decides. A caller holding a session still needs it."""
    try:
        observed_state, ident, _code, _reason = _retire_target(target)
        harness, sid, pane = ident.get("harness"), ident.get("session_id"), ident.get("pane")
        info = _retire_pane_info(pane) if pane and pane != "-" else None
        if sid == "-" and expected_sid in {None, "-"} and isinstance(info, dict):
            proven = _proven_session(info["foreground_processes"], harness)
            if proven:
                sid, expected_sid = proven, None
        unbound = sid == "-" and expected_sid in {None, "-"} and isinstance(info, dict)
        shell_start = (_proc_start_ticks(info.get("shell_pid"))
                       if isinstance(info, dict) else None)
        birth = (f"{info['shell_pid']}:{shell_start}"
                 if isinstance(info, dict) and shell_start else "")
        exact = (
            observed_state not in {"herdr-unavailable", "agent-not-found", "unknown"}
            and harness in {"claude", "codex", "opencode"}
            and (unbound or (isinstance(sid, str) and sid and sid != "-"))
            and isinstance(pane, str) and pane and pane != "-"
            and bool(birth)
            and (expected_harness is None or harness == expected_harness)
            and (expected_sid is None or sid == expected_sid)
            and (expected_pane is None or pane == expected_pane)
        )
        execution_ids = peer_obligations.execution_attempts_for_processes(
            info.get("foreground_processes", ()) if isinstance(info, dict) else ())
        bound, binding_state = (((), "observed") if unbound and not execution_ids else
                                peer_obligations.bound_work_for_pane(
                                    pane or "", harness or "", sid or "",
                                    execution_attempt_ids=execution_ids))
        return peer_obligations.pane_readiness(
            server=_HERDR_SESSION or "default", pane=pane or "",
            harness=harness or "", session_id="" if unbound else sid or "", pid_birth=birth,
            identity_verified=exact, native_turn=observed_state,
            bound_work=bound, bindings_state=binding_state,
            provenance=(("target", target), ("observed_state", observed_state)),
            purpose=purpose,
        )
    except Exception:
        return peer_obligations.pane_readiness(
            server=_HERDR_SESSION or "default", pane="", harness="",
            session_id="", pid_birth="", identity_verified=False,
            native_turn="unknown", bindings_state="unknown",
            provenance=(("target", target),))


def _prompt_input_reason(target, harness, state, *, expected_sid=None):
    """Withhold keyboard input when a form, draft or unreadable box is present."""
    readiness = _pane_readiness(target, state, expected_harness=harness,
                               expected_sid=expected_sid, purpose="input")
    if readiness.state == "unknown":
        return readiness.reason
    if readiness.reason.startswith("bound-registered-work") and readiness.state != "ready":
        return readiness.reason
    if _prompt_form_open(target, state):
        return "target-form-open"
    if harness not in {"claude", "opencode"}:
        return None                 # Codex's existing queue path is unchanged.
    lines = _read_screen(target)
    if lines is None:
        return "target-draft-unknown"
    draft = _draft_state(harness, lines)
    if harness == "opencode" and draft == "unknown" and _opencode_home(lines):
        draft = "empty"              # The native blank home layout is also readable.
    return None if draft == "empty" else "target-draft" if draft == "nonempty" else "target-draft-unknown"


_FLUSH_MAX_ROWS = 4
_FLUSH_ROW_TIMEOUT_S = 12
_FLUSH_STUCK_HOURS = 1.0


def _message_obligation_id(ref):
    return "message-" + ref


def _schedule_message_obligation(row, target, pane=None, *, delay_notice_for=None):
    """Persist exact transport ownership without copying the sealed message body."""
    sender = row.get("from") or {}
    recipient = row.get("to") or {}
    sid = recipient.get("session_id")
    harness = recipient.get("harness")
    identity = {"server": _HERDR_SESSION or "default", "pane": pane or "",
                "harness": harness or "", "session_id": sid or ""}
    intent = {"ref": row.get("ref"), "body_digest": row.get("body_sha256"),
              "target": target, "from": sender, "to": recipient,
              "refs": list(row.get("refs") or []),
              "delay_notice_for": delay_notice_for or ""}
    try:
        duty = peer_obligations.ObligationStore().create(
            _message_obligation_id(row["ref"]), "message", identity, intent)
        peer_obligations.ensure_runner()
        return duty
    except (OSError, ValueError, peer_obligations.ObligationError):
        return None


def _ensure_sender_delay_notice(row):
    """Persist and enqueue one normal peer message for an overdue original ref."""
    original_ref = row.get("ref")
    refs = row.get("refs") or []
    if (not isinstance(original_ref, str) or
            any(isinstance(value, str) and value.startswith("delay-notice:")
                for value in refs)):
        return None
    sender, recipient = row.get("from") or {}, row.get("to") or {}
    notice_id = "delay-" + original_ref
    identity = {"server": _HERDR_SESSION or "default",
                "recipient_harness": sender.get("harness", ""),
                "recipient_session_id": sender.get("session_id", "")}
    try:
        existing = peer_obligations.ObligationStore().get(notice_id)
        if existing:
            peer_obligations.ensure_runner()
            return existing
        created = float(row.get("created") or time.time())
        sent_at = datetime.datetime.fromtimestamp(
            created, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        body = (f"[notice] Your peer message ref {original_ref} is still pending "
                f"delivery; it was sent at {sent_at}.")
        duty = peer_obligations.ObligationStore().create(
            notice_id, "message", identity,
            {"delay_notice_for": original_ref, "target": sender.get("name") or sender.get("session_id"),
             "body_digest": hashlib.sha256(body.encode("utf-8")).hexdigest(),
             "body": body, "from": recipient, "to": sender})
        text, notice_ref = peer_message.prepare_peer_message(
            body, recipient, sender, defer=True,
            refs=["delay-notice:" + original_ref, original_ref],
            receipt="sender-delay-notice")
        observation = dict(duty.get("observation") or {}, transfer_ref=notice_ref)
        peer_obligations.ObligationStore().update(notice_id, observation=observation)
        pending = peer_message._read_pending(notice_ref)
        if pending:
            _schedule_message_obligation(
                pending, sender.get("name") or sender.get("session_id"),
                delay_notice_for=original_ref)
        peer_obligations.ensure_runner()
        return duty
    except (OSError, ValueError, OverflowError, peer_obligations.ObligationError):
        return None


def _flush_pending_for_target(target, t_harness, t_sid, entry_state, skip=None,
                              expected_pane=None):
    """Deliver this recipient's deferred rows before a new send.

    Returns (flushed, stuck): flushed counts observed redeliveries closed
    through the ordinary receive path; stuck lists (age_hours, from_name, ref) rows
    still pending older than _FLUSH_STUCK_HOURS so their senders learn on
    their next prompt. `skip` is (source_sha256, from_harness, from_sid) of
    this send: a row from the same sender with the same content is left to
    the normal path, which reuses its row for its single send -- otherwise
    the content would arrive twice. A same-body row from another sender goes
    out: the normal path mints it a separate row. Only `pending` rows
    without a live delivery claim go out -- `queued` rows were accepted
    somewhere already, `unverified` rows are ambiguous, and claimed rows may
    be moving on another path already (native queue, plugin pull); none of
    those is retried. Codex targets keep their native-queue path only. Each
    row is claimed before input, never deleted, and an observed send closes
    it without waiting for a receiver hook. An ambiguous send stays inflight
    for the hook to acknowledge and is not blindly resubmitted.
    Rows older than _FLUSH_STUCK_HOURS keep their exact sealed body and create
    one separate normal peer message to notify the original sender.
    Never raises; failures print to stderr and leave rows for a later prompt.
    """
    stuck = []
    try:
        rows = [row for row in peer_message._pending_rows()
                if row.get("state") == "pending"
                and (row.get("to") or {}).get("harness") == t_harness
                and (row.get("to") or {}).get("session_id") == t_sid]
    except (OSError, ValueError):
        print("pending-flush-failed target=%s reason=pending-unreadable" % target, file=sys.stderr)
        return 0, stuck
    now = time.time()
    for row in rows:
        try:
            age_h = (now - float(row.get("created") or now)) / 3600
        except (TypeError, ValueError):
            age_h = 0.0
        if age_h >= _FLUSH_STUCK_HOURS:
            sender = ((row.get("from") or {}).get("name")
                      or (row.get("from") or {}).get("session_id") or "-")
            stuck.append((age_h, sender, row.get("ref")))
    for row in rows:
        try:
            age_h = (now - float(row.get("created") or now)) / 3600
        except (TypeError, ValueError):
            age_h = 0.0
        if age_h >= _FLUSH_STUCK_HOURS:
            _ensure_sender_delay_notice(row)
    flushed = 0
    if t_harness == "codex":
        return flushed, stuck
    recipient = {"harness": t_harness, "session_id": t_sid}
    rows.sort(key=lambda row: row.get("created") or 0)
    for row in rows[:_FLUSH_MAX_ROWS]:
        ref = row.get("ref")
        try:
            if row.get("rpc_claim"):
                continue
            if skip and row.get("source_sha256") == skip[0] \
                    and (row.get("from") or {}).get("harness") == skip[1] \
                    and (row.get("from") or {}).get("session_id") == skip[2]:
                continue
            state, _pane = _agent_state(target)
            reason = _prompt_input_reason(target, t_harness, state)
            if reason:
                print("pending-flush-stopped target=%s reason=%s" % (target, reason), file=sys.stderr)
                break
            current_harness, current_sid, _name = _resolve_target(target)
            if (current_harness, current_sid) != (t_harness, t_sid):
                break
            if expected_pane:
                _current_state, current_ident, _code, _reason = _retire_target(target)
                if current_ident.get("pane") != expected_pane:
                    break
            row = peer_message.claim_pending_herdr(ref, recipient)
            if row is None:
                continue
            text = row["text"]
            reason = _prompt_input_reason(target, t_harness, state)
            if reason:
                peer_message.release_unsent_herdr_claim(row, receipt=reason)
                break
            sent_at = time.time()
            rc, payload = _herdr_prompt(target, text, wait=state != "working",
                                        timeout_ms=_PROMPT_VERIFY_TIMEOUT_MS)
            if rc is None:
                continue
            if rc == 0 and state != "working":
                outcome, observed = "true", "state-flip"
            elif rc == 0 or _herdr_error_reason(payload, rc) == "timeout":
                _settle(target)
                outcome, observed, _reason = _verify_after_send(
                    target, text.splitlines()[0], t_harness, t_sid, sent_at)
            else:
                continue
            if outcome == "true":
                ack = peer_message.receive_peer_message(
                    text, recipient, summary_text="herdr redelivery observed: " + observed)
                current = peer_message._read_pending(ref)
                if ack or not current or current.get("state") != "received":
                    continue
                flushed += 1
                try:
                    age_h = (now - float(row.get("created") or now)) / 3600
                except (TypeError, ValueError):
                    age_h = 0.0
                print("pending-flushed target=%s ref=%s age=%.1fh" % (target, ref, age_h), file=sys.stderr)
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    return flushed, stuck


def _resume_message_obligation(duty, store):
    intent = duty.get("intent") or {}
    observation = duty.get("observation") or {}
    target = intent.get("target")
    ref = intent.get("ref") or observation.get("transfer_ref")
    delay_for = intent.get("delay_notice_for") or ""
    if delay_for and not ref:
        body = intent.get("body")
        sender, recipient = intent.get("from") or {}, intent.get("to") or {}
        if not isinstance(body, str) or not body:
            store.update(duty["id"], state="unknown",
                         observation={"reason": "delay-notice-body-unavailable"})
            return
        try:
            _text, ref = peer_message.prepare_peer_message(
                body, sender, recipient, defer=True,
                refs=["delay-notice:" + delay_for, delay_for],
                receipt="sender-delay-notice")
            observation = {**observation, "transfer_ref": ref}
            duty = store.update(duty["id"], observation=observation)
        except (OSError, ValueError):
            store.update(duty["id"], state="unknown",
                         observation={"reason": "delay-notice-payload-unavailable"})
            return
    if not isinstance(ref, str) or not ref:
        store.update(duty["id"], state="unknown",
                     observation={"reason": "message-ref-unavailable"})
        return
    try:
        row = peer_message._read_pending(ref)
    except (OSError, ValueError):
        store.update(duty["id"], state="unknown",
                     observation={"ref": ref, "reason": "pending-row-unreadable"})
        return
    if row is None:
        store.update(duty["id"], state="unknown",
                     observation={"ref": ref, "reason": "pending-row-missing"})
        return
    if row.get("state") == "received":
        store.update(duty["id"], state="complete", result="received",
                     delivery="acknowledged", cleanup="complete",
                     observation={"ref": ref, "reason": "exact-peer-ref"})
        return
    if row.get("state") in {"queued", "unverified"}:
        store.update(duty["id"], state="unknown",
                     observation={"ref": ref, "transport_state": row["state"],
                                  "reason": row.get("receipt", "delivery-unconfirmed")})
        return
    identity = (duty.get("intent") or {}).get("identity") or {}
    # Identity is held outside mutable observations. The accepted target stays
    # nameable only while its exact harness/session/pane still resolves.
    recipient = row.get("to") or {}
    server = identity.get("server") or "default"
    global _HERDR_SESSION
    old_server = _HERDR_SESSION
    _HERDR_SESSION = None if server == "default" else server
    try:
        state, ident, _code, reason = _retire_target(target or recipient.get("name") or "")
        expected_pane = identity.get("pane") or ""
        if (reason or ident.get("harness") != recipient.get("harness")
                or ident.get("session_id") != recipient.get("session_id")
                or (expected_pane and ident.get("pane") != expected_pane)):
            store.update(duty["id"], state="unknown",
                         observation={"ref": ref, "reason": reason or "target-identity-changed",
                                      "observed_pane": ident.get("pane", "")})
            return
        readiness = _pane_readiness(target, state,
                                    expected_harness=recipient.get("harness"),
                                    expected_sid=recipient.get("session_id"), purpose="input")
        if readiness.state == "unknown" or readiness.reason.startswith("bound-registered-work"):
            if readiness.state != "ready":
                store.update(duty["id"], state="unknown" if readiness.state == "unknown" else "pending",
                             observation={"ref": ref, "reason": readiness.reason,
                                          "state": readiness.state})
                return
        if recipient.get("harness") == "codex":
            peer_message.deliver_pending_codex(ref, timeout=0.25)
        elif state in {"idle", "done", "working"}:
            _flush_pending_for_target(target, recipient["harness"], recipient["session_id"],
                                      state, expected_pane=expected_pane or None)
        try:
            current = peer_message._read_pending(ref)
        except (OSError, ValueError):
            current = None
        if current and current.get("state") == "received":
            store.update(duty["id"], state="complete", result="received",
                         delivery="acknowledged", cleanup="complete",
                         observation={"ref": ref, "reason": "exact-peer-ref"})
        else:
            age = max(0.0, time.time() - float((current or row).get("created") or time.time()))
            if age >= _FLUSH_STUCK_HOURS * 3600:
                _ensure_sender_delay_notice(current or row)
            store.update(duty["id"], state="pending",
                         observation={"ref": ref, "state": (current or row).get("state"),
                                      "reason": (current or row).get("receipt", "awaiting-receipt")})

    finally:
        _HERDR_SESSION = old_server


def _resume_registered_obligation(duty, store):
    """Recover a lost Claude native wake through the existing durable peer courier."""
    intent = duty.get("intent") or {}
    if intent.get("carrier") != "claude-parent-runtime":
        return
    identity = intent["identity"]
    jobs = Path(identity["jobs"])
    from dispatch_completion_join import current_attempt_row, materialize_after_terminal_close
    from dispatch_notice_state import keep_claim
    import dispatch_pending_delivery as pending
    from dispatch_seat_handover import effective_parent
    spec = importlib.util.spec_from_file_location(
        "_retained_rewake", _UTILITIES_DIR.parent / "hooks/dispatch-owner-rewake.py")
    rewake = sys.modules.get(spec.name)
    if rewake is None:
        rewake = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = rewake
        spec.loader.exec_module(rewake)
    arm = rewake._read_arm(rewake.arm_path(jobs, identity["attempt_id"]))
    holder = (arm or {}).get("holder") or (duty.get("observation") or {}).get("holder")
    if rewake._holder_alive(holder):
        return
    row = current_attempt_row(jobs, identity["attempt_id"])
    if row is None:
        return
    # The duty follows confirmed handover; the signed queue stays under the
    # registered parent, even when its current recipient is a successor.
    recipient_sid = effective_parent(row.metadata, jobs)
    storage_recipient = row.metadata.get("parent_sid", "")
    if row.status not in {"open", "running", "done"}:
        return
    if row.status == "done":
        if row.metadata.get("delivery_intent") != "1":
            store.update(duty["id"], state="complete", delivery="not-required", cleanup="complete")
            return
        materialize_after_terminal_close(jobs, row.attempt_id)
        delivery_id = row.metadata.get("delivery_id", "")
        record = pending.read(jobs.parent, storage_recipient, delivery_id)
        if record and record.get("state") in {"acked", "rejected", "expired"}:
            store.update(duty["id"], state="complete", delivery=record["state"], cleanup="complete")
            return
    old_server = _HERDR_SESSION
    globals()["_HERDR_SESSION"] = None if identity["server"] == "default" else identity["server"]
    try:
        result = subprocess.run(_herdr_argv("agent", "list"), capture_output=True, text=True, timeout=5)
        agents = (json.loads(result.stdout).get("result") or {}).get("agents") or []
        matches = [a for a in agents if a.get("agent") == "claude"
                   and (a.get("agent_session") or {}).get("value") == recipient_sid]
        if len(matches) != 1:
            return
        pane = matches[0]["pane_id"]
        if _resolve_target(pane)[:2] != ("claude", recipient_sid):
            return
        launch = rewake.Launch(identity["attempt_id"], jobs, recipient_sid)
        win = None
        if row.status in {"open", "running"}:
            claims = []
            notices = rewake._gate_notices(launch, attempt_only=True, settle="sent-ambiguous",
                                           supervision_claims=claims)
            for root, recipient, delivery_id, owner in claims:
                pending.release_claim(root, recipient, delivery_id, claim_owner=owner)
            if not notices:
                return
            message = rewake.gate_wake_message(launch, notices)
        else:
            owner = "retained-rewake:" + duty["id"]
            if record and record.get("state") in {"claimed", "sent-ambiguous"}:
                pending.reclaim(jobs.parent, storage_recipient, delivery_id,
                                now_ns=time.monotonic_ns())
            record = pending.claim(jobs.parent, storage_recipient, delivery_id,
                                   claim_owner=owner, lease_seconds=30, require_generation_proof=False)
            if not keep_claim(jobs.parent, storage_recipient, delivery_id, record, owner, jobs=jobs):
                return
            win = (delivery_id, owner)
            _state, message = rewake.classified_receipt(launch, "ready", "retained-carrier", rewake.agent_home())
        # The normal prompt command seals the body, checks the recipient again,
        # and retains it through forms, busy turns and sender/observer exit (#446).
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as body:
            body.write(message)
            body.flush()
            args = build_parser().parse_args(["prompt", pane, "--body-file", body.name,
                                              "--ref", duty["id"]])
            code = cmd_prompt(args, expected_recipient=("claude", recipient_sid))
        if code in {0, 3}:
            if win:
                pending.mark_sent_ambiguous(jobs.parent, storage_recipient, win[0], claim_owner=win[1])
            store.update(duty["id"], state="complete" if win else "pending",
                         delivery="peer-courier", cleanup="complete" if win else "pending")
        elif win:
            pending.release_claim(jobs.parent, storage_recipient, win[0], claim_owner=win[1])
    finally:
        globals()["_HERDR_SESSION"] = old_server


def cmd_obligation_runner(args):
    """Existing peer steward, scoped to accepted duties and stopped when they settle."""
    store = peer_obligations.ObligationStore(args.state_root)
    try:
        lock_target = Path(os.path.realpath(os.readlink(f"/proc/self/fd/{args.lock_fd}")))
        if lock_target != (store.root / peer_obligations.RUNNER_LOCK_NAME).resolve():
            return 70
        os.fstat(args.lock_fd)
    except (OSError, ValueError, TypeError):
        return 70
    delay = 1.0
    while True:
        duties = store.list()
        if not duties:
            return 0
        for duty in duties:
            try:
                if duty.get("intent", {}).get("kind") == "message" or duty.get("id", "").startswith(
                        ("message-", "delay-")):
                    _resume_message_obligation(duty, store)
                elif duty.get("intent", {}).get("kind") == "retire":
                    with peer_obligations.legacy_runner_lock(store) as acquired:
                        if acquired:
                            _resume_retire_obligation(duty, store)
                elif duty.get("intent", {}).get("kind") == "registered-batch":
                    _resume_registered_obligation(duty, store)
            except Exception:
                try:
                    store.update(duty["id"], observer_error="observer-unavailable")
                except Exception:
                    pass
        time.sleep(delay)
        delay = min(15.0, delay * 1.7)


def cmd_ensure_obligations(_args):
    """Reconnect accepted peer duties from an existing harness lifecycle callback."""
    try:
        _ensure_watch_observers()
        # Reconnect with the activated code, even if an older runner is still
        # holding its lock. Existing transfer claims serialize submission.
        store = peer_obligations.ObligationStore()
        for duty in store.list():
            if duty.get("intent", {}).get("kind") == "message":
                try:
                    _resume_message_obligation(duty, store)
                except Exception:
                    store.update(duty["id"], observer_error="observer-unavailable")
        # Older observers could close a retire without consuming its successor
        # mark. Lifecycle observers are detached, just like the task runner;
        # the accepted request and original mark carry the authority.
        for duty in store.list(states={"complete"}):
            if duty.get("intent", {}).get("kind") == "retire":
                pane = (duty["intent"].get("requester") or {}).get("pane")
                if pane and _seat_successor_path(pane).is_file():
                    _resume_retire_obligation(duty, store)
        peer_obligations.ensure_runner()
        dispatch_batch_obligations.ensure_observers()
    except Exception:
        pass
    return 0


def receiver_idle(recipient, pane, *, peer=True):
    """One existing receiver callback on its exact pane/session; no wait or watcher."""
    import session_tidy as st
    import session_tidy_clear as clear
    harness, sid = recipient["harness"], recipient["session_id"]
    seat = st.resolve_seat(harness, sid=sid)
    req = clear.read_reservation(seat.key)
    booked = (req and req.get("harness") == harness
              and (req.get("seat") or {}).get("pane") == pane
              and req.get("status") == "cleared"
              and (req.get("continued") or {}).get("state") == "pending"
              and (harness != "claude" or req.get("new_session") == sid))
    rows = peer_message.pending_messages(recipient) if peer else []
    if not booked and not any(r.get("state") == "pending" for r in rows):
        return
    current_harness, current_sid, _name = _resolve_target(pane)
    state, _pane = _agent_state(pane)
    if (current_harness, current_sid) != (harness, sid) or state not in {"idle", "done"}:
        return
    readiness = _pane_readiness(pane, state, expected_harness=harness, expected_sid=sid,
                                purpose="input")
    if readiness.state != "ready":
        return
    with contextlib.redirect_stdout(sys.stderr):
        if rows:
            _flush_pending_for_target(pane, harness, sid, state)
        if booked:
            # Booking/card/new-user-input checks still own this ordinary retry.
            clear._continue(seat.key, req["nonce"], clear._run_continue)


def _verify_after_send(target, first, t_harness, t_sid, sent_at):
    """(outcome, verify, reason) once herdr itself could not prove the submission.

    Order: the target transcript (exact, Claude only) → a *read* prompt box
    (`region=prompt_box_body`; residue stays queued, no Enter retry) → nothing
    observed is `unverified`, never `true`."""
    if _transcript_arrival(t_harness, t_sid, first, sent_at):
        return "true", "transcript-arrival", None
    evidence, readable = _prompt_box_evidence(target)
    if not readable:
        return "unverified", "prompt-box-unavailable", "submission-not-observed"
    if not _prompt_box_residue(evidence, first):
        return "true", "prompt-box-clear", None
    return "queued", "prompt-box", "prompt-box-residue"


def cmd_prompt(args, *, expected_recipient=None):
    """F-100c — the harness-neutral steward send: `herdr agent prompt <target> <body +
    trailer>`, recorded with the target's exact session id (herdr `agent get`) and the
    sender's name. The trailer lets the receiving harness write its own `notice`.

    SD-122 (11) v67/v70 — `prompted=true` is printed only after the submission was
    observed, never from herdr's exit code alone:

    * target `blocked` or an open form: no keyboard input. A bounded private
      payload retains the exact transfer/SID. Native queue acceptance is queued;
      unavailable/ambiguous delivery stays pending. Only actual receipt is true.
      Original typing-loss/default-Enter evidence (3/3) remains applicable.
    * target not working (idle/done/unknown): `herdr agent prompt --wait --until
      working` must see the state change; herdr's `agent_prompt_stalled` is
      `prompted=failed reason=agent-prompt-stalled`; a herdr `timeout` falls
      through to `_verify_after_send`.
    * target already working (a state change proves nothing): after a bounded
      herdr wait, `_verify_after_send` -- the target transcript first, then a
      prompt box that was actually read; residue without any Enter retry is
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
        with open(args.body_file, encoding="utf-8", errors="replace") as handle:
            body = handle.read()
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
    if expected_recipient is not None and (t_harness, t_sid) != expected_recipient:
        print("prompted=false reason=target-identity-changed")
        return 5
    transfer_ref = None
    state_before, target_pane = _agent_state(args.target)
    if not args.no_verify and state_before in {"working", "blocked"} and args.wait_idle_ms > 0:
        _run_herdr_wait(args.target, ["idle", "done"], args.wait_idle_ms)
        state_before, target_pane = _agent_state(args.target)
    flushed = 0
    input_options = {"expected_sid": t_sid} if expected_recipient is not None else {}
    input_reason = _prompt_input_reason(args.target, t_harness, state_before, **input_options)
    if not input_reason:
        # Deferred rows go first: a target receivable now takes what an
        # earlier form-open verdict stranded (2026-10-07). Never raises and
        # never re-reads state here: the send path below verifies on its own.
        # The skip carries this send's identity so a same-sender same-body
        # row is left to the normal path's single send.
        flushed, _stuck = _flush_pending_for_target(
            args.target, t_harness, t_sid, state_before,
            skip=(hashlib.sha256(body.rstrip("\n").encode("utf-8")).hexdigest(),
                  from_harness, from_sid))
    if not args.no_trailer:
        try:
            text, transfer_ref = peer_message.prepare_peer_message(
                body, {"harness": from_harness, "session_id": from_sid, "name": from_name},
                {"harness": t_harness, "session_id": t_sid, "name": _t_name},
                defer=bool(input_reason), refs=args.ref, receipt=input_reason or "target-form-open")
        except (OSError, ValueError):
            print("prompted=unverified reason=peer-pending-or-transfer-unavailable")
            return 5
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
    try:
        pending = peer_message._read_pending(transfer_ref) if transfer_ref else None
    except (OSError, ValueError):
        print("prompted=unverified reason=peer-pending-unavailable")
        return 5
    if pending and not t_sid and not input_reason:
        # Private delivery is SID-bound: a receivable target with no session
        # yet (a fresh Codex before its first input) takes the typed send, even
        # when a retry reused a row an earlier deferral left unaddressable.
        pending = None
    surface = "herdr"
    claimed = None
    if pending and t_harness != "codex" and not input_reason:
        try:
            claimed = peer_message.claim_pending_herdr(
                transfer_ref, {"harness": t_harness, "session_id": t_sid})
        except (OSError, ValueError):
            claimed = None
    if t_harness in {"claude", "opencode"} and not input_reason:
        # Preparation/claim may take time: the final read belongs after them.
        input_reason = _prompt_input_reason(args.target, t_harness, state_before, **input_options)
        if input_reason:
            if claimed is not None:
                peer_message.release_unsent_herdr_claim(claimed, receipt=input_reason)
                claimed = None
            if not args.no_trailer and pending is None:
                try:
                    text, transfer_ref = peer_message.prepare_peer_message(
                        body, {"harness": from_harness, "session_id": from_sid, "name": from_name},
                        {"harness": t_harness, "session_id": t_sid, "name": _t_name},
                        defer=True, refs=args.ref, receipt=input_reason)
                    pending = peer_message._read_pending(transfer_ref)
                except (OSError, ValueError):
                    print("prompted=unverified reason=peer-pending-unavailable")
                    return 5
    if pending and pending.get("state") == "pending" and input_reason:
        _schedule_message_obligation(pending, args.target, target_pane)
    if pending and t_harness == "codex":
        surface = "codex-queue"
        try:
            result = peer_message.deliver_pending_codex(transfer_ref)
        except (OSError, ValueError) as exc:
            result = {"status": "unverified", "reason": "peer-pending-unavailable:" + str(exc)}
        outcome = "true" if result["status"] == "received" else (
            "queued" if result["status"] == "queued" else "unverified")
        verify, reason = "native-queue-" + result["status"], result["reason"]
    elif input_reason:
        outcome = ("unverified" if pending and pending["state"] == "unverified" else
                   "queued" if pending else "failed")
        verify = "private-pending" if pending else "none"
        reason = pending["receipt"] if pending and pending["state"] == "unverified" else input_reason
    elif pending and claimed is None:
        outcome, verify, reason = "unverified", "private-pending", "peer-delivery-already-submitted"
    elif args.no_verify:
        state_before = "-"
        rc, _payload = _herdr_prompt(args.target, text, wait=False,
                                     timeout_ms=_PROMPT_VERIFY_TIMEOUT_MS)
        if rc is None:
            return _unavailable("herdr-invocation-failed")
        outcome = "true" if rc == 0 else "failed"
        reason = None if rc == 0 else f"herdr-exit-{rc}"
    else:
        if state_before == "working":
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
    if claimed is not None and outcome == "true" and not args.no_verify:
        peer_message.receive_peer_message(
            text, {"harness": t_harness, "session_id": t_sid},
            summary_text="herdr send observed: " + verify)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    ledger_status = _PROMPT_LEDGER_STATUS[outcome]
    # SD-122 (11): one ledger row per send, whatever happened -- target pane,
    # caller session (from `_record`), time, body digest, and the submission
    # verdict as the receipt. This row is the only caller attribution that
    # exists for a pane prompt.
    receipt = (f"prompted={outcome} state_before={state_before} verify={verify} "
               f"herdr_rc={'-' if rc is None else rc} ms={elapsed_ms}"
               + (f" reason={reason}" if reason else "")
               + (f" flushed={flushed}" if flushed else ""))
    _record(to_harness=t_harness or "unknown", to_name=args.target, kind=kind,
            summary_text=text, to_session_id=t_sid, to_pane=target_pane,
            ref=args.ref, status=ledger_status, receipt=receipt,
            from_identity=from_identity, from_name=from_name, transfer_ref=transfer_ref, surface=surface)
    line = (f"prompted={outcome} target={args.target} "
            f"to_harness={t_harness or '-'} to_alias={peer_message.peer_alias(t_harness, t_sid)} kind={kind} "
            f"state_before={state_before} verify={verify} ms={elapsed_ms}")
    if reason:
        line += f" reason={reason}"
    if flushed:
        line += f" flushed={flushed}"
    print(line)
    return _PROMPT_EXIT[outcome]


# --- clear: the one typed command that starts a fresh conversation (session-tidy auto-clear) ---

_CLEAR_COMMAND = {"claude": "/clear", "codex": "/clear", "opencode": "/new"}
_CLEAR_EXIT = {"true": 0, "skipped": 3, "queued": 3, "failed": 1, "unverified": 5}
_CLEAR_LEDGER_STATUS = {"true": "sent", "failed": "failed", "skipped": "unknown", "queued": "unknown", "unverified": "unknown"}
_CLEAR_OBSERVE_ROUNDS = 8             # bounded waits between looks at the pane after the send (OpenCode repaints its home in ~5 s)
_CLEAR_OBSERVE_SETTLE_MS = 1500
_ANSI_TOKEN = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|(.)", re.S)
_RULE_LINE = re.compile(r"^[\s\u2500-\u257f]*\u2500{8,}[\s\u2500-\u257f]*$")
_OPENCODE_HOME_PLACEHOLDER = "askanything"
# A whole thread id; a cut one (`…` in a narrow pane) or one inside a longer token is not.
_THREAD_ID = re.compile(r"(?<![0-9A-Za-z-])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9A-Za-z-])")


def _screen_lines(raw):
    """The visible screen as lines of `(char, faint)` cells: SGR faint (`2`) tracked,
    every other escape sequence dropped. A dim line is a placeholder or a suggestion."""
    lines, cells, faint = [], [], False
    for match in _ANSI_TOKEN.finditer(raw or ""):
        params, final, char = match.group(1), match.group(2), match.group(3)
        if char is not None:
            if char == "\n":
                lines.append(cells)
                cells = []
            elif char != "\r":
                cells.append((" " if char == "\u00a0" else char, faint))
        elif final == "m":
            for code in (params.split(";") if params else ["0"]):
                if code in ("0", ""):
                    faint = False
                elif code == "2":
                    faint = True
                elif code == "22":
                    faint = False
    lines.append(cells)
    return lines


def _plain(cells):
    return "".join(ch for ch, _ in cells)


def _marker_remainder(cells, marker):
    """`(text, any_bright)` after `marker` on a line that starts with it (leading blanks ok), else None."""
    text = _plain(cells)
    stripped = text.lstrip()
    if not stripped.startswith(marker):
        return None
    offset = len(text) - len(stripped) + len(marker)
    rest = cells[offset:]
    body = _plain(rest).strip()
    return body, any((not faint) and ch.strip() for ch, faint in rest)


def _draft_state(harness, lines):
    """`empty` | `nonempty` | `unknown` for the input box of one harness's visible screen.

    Only a layout read for that harness decides `empty`; a box that cannot be located, or text
    below the marker that could be a second draft line, a popup or a footer, is `unknown` --
    never `empty` (clearing over a draft destroys it)."""
    if harness == "claude":
        idx = next((i for i in range(len(lines) - 1, -1, -1)
                    if _marker_remainder(lines[i], "\u276f") is not None), None)
        if idx is None:
            return "unknown"
        body, bright = _marker_remainder(lines[idx], "\u276f")
        if bright:
            return "nonempty"
        for cells in lines[idx + 1:]:
            if _RULE_LINE.match(_plain(cells)):
                return "empty"          # the faint remainder, if any, is Claude's own suggestion
            if _plain(cells).strip():
                return "nonempty"       # a second draft line before the box closes
        return "unknown"
    if harness == "codex":
        idx = next((i for i in range(len(lines) - 1, -1, -1)
                    if _marker_remainder(lines[i], "\u203a") is not None), None)
        if idx is None:
            return "unknown"
        body, bright = _marker_remainder(lines[idx], "\u203a")
        if bright:
            return "nonempty"
        below = lines[idx + 1] if idx + 1 < len(lines) else []
        return "empty" if not _plain(below).strip() else "unknown"
    if harness == "opencode":
        block, end = [], None
        for i in range(len(lines) - 1, -1, -1):
            text = _plain(lines[i]).lstrip()
            if text.startswith("\u2503"):
                if end is None:
                    end = i
                block.insert(0, text[1:])
            elif end is not None:
                break
        if len(block) < 2:
            return "unknown"
        # The last bar line is the agent/model line. Typed text starts right after the bar's
        # two-space margin; a hint right-aligned in a wide pane (the cwd and branch) sits far
        # to the right behind a wide gap and is not the draft.
        typed = " ".join(filter(None, (_opencode_left_text(body) for body in block[:-1])))
        typed = "".join(typed.split())
        if not typed or typed.lower().startswith(_OPENCODE_HOME_PLACEHOLDER):
            return "empty"
        return "nonempty"
    return "unknown"


def _opencode_left_text(body):
    """The text at the left edge of one OpenCode input-box line (`body` is the line after its bar)."""
    rest = body[2:] if not body[:2].strip() else body
    if not rest[:1].strip():
        return ""
    return re.split(r" {6,}", rest, maxsplit=1)[0].strip()


def _screen_ready(harness, lines):
    """None when the visible screen shows no selection/permission form and an empty input box,
    else the reason (`form-open`, `draft`, `draft-unknown`)."""
    flat = "".join("".join(_plain(c) for c in lines).split()).lower()
    if any(token in flat for token in _FORM_TOKENS):
        return "form-open"
    draft = _draft_state(harness, lines)
    if draft == "nonempty":
        return "draft"
    if draft != "empty":
        return "draft-unknown"
    return None


def _codex_footer_threads(lines):
    """The whole thread ids on Codex's status line (the lines below its input box).

    hearting's Codex status line carries `thread-title`, which shows the thread id until the
    thread has a title -- right after `/clear` the new thread's id (measured on codex 0.160.0:
    the rollout written at the first message carries the same id; herdr kept the old one)."""
    idx = next((i for i in range(len(lines) - 1, -1, -1)
                if _marker_remainder(lines[i], "\u203a") is not None), None)
    if idx is None:
        return set()
    return {m.group(0) for cells in lines[idx + 1:] for m in _THREAD_ID.finditer(_plain(cells))}


def _codex_rollout_exists(thread_id):
    """True when Codex has written a rollout for `thread_id` -- it does so with the thread's
    first message, not at `/clear` (measured on codex 0.160.0)."""
    import glob as _glob
    pattern = os.path.join(_codex_home_dir(), "sessions", "*", "*", "*",
                           f"rollout-*-{_glob.escape(thread_id)}.jsonl")
    return bool(_glob.glob(pattern))


def _read_screen(target):
    """The visible pane (ANSI) as screen lines, or None when it could not be read."""
    try:
        proc = subprocess.run(_herdr_argv("agent", "read", target, "--source", "visible", "--format", "ansi"),
                              capture_output=True, text=True, errors="replace", timeout=_herdr_get_timeout())
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    return _screen_lines(proc.stdout)


def _native_trust_reason(harness, lines):
    """Name an observed native folder trust screen without interacting with it."""
    if not lines:
        return None
    flat = "".join("".join(_plain(line) for line in lines).split()).lower()
    patterns = {
        "codex": ("trustthisfolder", "trustthisworkspace", "doyoutrustthisproject"),
        "claude": ("trustthisfolder", "trustthisproject", "doyoutrustthecontentsofthisfolder"),
        "opencode": ("trustthisfolder", "trustthisproject", "doyoutrustthisproject"),
    }
    return "native-trust-wait" if any(token in flat for token in patterns.get(harness, ())) else None


def _process_session(pane, harness):
    """The session the pane's foreground runtime process is on now, or None.

    Read from the process the way the Fleet board does (`fleet.process_identity`:
    Claude's `sessions/<pid>.json`, rewritten on `/clear`; Codex's open rollout;
    OpenCode's TUI selection record). herdr's own `agent_session`
    follows a `/clear` only when its integration reports it, and has been seen to keep
    the previous session for good (live panes 2026-10-01), so a second tidy in the same
    window would otherwise never clear it."""
    try:
        proc = subprocess.run(_herdr_argv("pane", "process-info", "--pane", pane),
                              capture_output=True, text=True, timeout=_herdr_get_timeout())
        payload = json.loads(proc.stdout or "")
        processes = ((payload.get("result") or {}).get("process_info") or {}).get("foreground_processes") or []
    except Exception:
        return None
    return _proven_session(processes, harness)


def _proven_session(processes, harness):
    """The session one of these foreground processes proves (`fleet.process_identity`), or None."""
    try:
        tools_dir = Path(__file__).resolve().parent.parent / "tools"
        if tools_dir.is_dir() and str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))
        from fleet.process_identity import PROVEN, process_identity
    except Exception:
        return None
    for process in processes:
        try:
            found = process_identity(int(process.get("pid")), harness)
        except Exception:
            continue
        if found.confidence == PROVEN:
            return found.session_id
    return None


def _herdr_lags(req, herdr_sid):
    """True when herdr's session for the booked pane is an OLDER session of the booking seat's own
    ledger than the booked one -- the booked thread is later, herdr just has not followed.

    Codex tools and hooks run in the shared app-server daemon, so the pane's TUI holds no rollout
    the process check could read and herdr's `agent_session` can stay on the cleared session for
    good (measured 2026-10-01).  The seat ledger (written by the hooks of both sessions at this very
    pane) is then the only record that orders the two.  A session the ledger has never seen is not
    a predecessor."""
    if req.get("harness") != "codex" or not herdr_sid or herdr_sid == "-" or herdr_sid == req.get("sid"):
        return False
    try:
        import session_tidy as st
        seat_fields = req.get("seat") or {}
        seat = st.Seat(str(seat_fields.get("kind") or ""), str(seat_fields.get("key") or ""),
                       str(seat_fields.get("pane") or ""), str(seat_fields.get("harness") or ""),
                       str(seat_fields.get("project_key") or ""))
        return st.ledger_precedes(seat, herdr_sid, str(req.get("sid")))
    except Exception:
        return False


def _clear_look(target, req):
    """One judgement of the target: `(None, agent, lines)` when it may be cleared, else
    `(reason, agent, lines)`.

    Identity (pane, harness, session), idle state, then the screen: no form open and an empty
    input box. Every doubt is a reason, never a guess."""
    state, agent, _code, unavailable = _interpret_payload(_run_herdr_get(target), target)
    if unavailable or state in ("agent-not-found", "timeout"):
        return (unavailable or "agent-not-found"), agent, None
    seat_pane = (req.get("seat") or {}).get("pane")
    if seat_pane and agent.get("pane") != seat_pane:
        return "target-changed", agent, None
    if agent.get("harness") != req.get("harness"):
        return "target-changed", agent, None
    if req.get("harness") in ("claude", "codex") and agent.get("session_id") != req.get("sid"):
        # herdr's pane record can lag a /clear; the process itself is the proof, never a guess.
        # An unnamed session (`-`, empty) is not a confirmed match: only the process can vouch for it.
        if _process_session(agent.get("pane") or target, req.get("harness")) != req.get("sid") \
                and not _herdr_lags(req, agent.get("session_id")):
            return "target-changed", agent, None
    if state == "blocked":
        return "form-open", agent, None
    if state not in ("idle", "done"):
        return f"not-idle-{state}", agent, None
    lines = _read_screen(target)
    if lines is None:
        return "screen-unknown", agent, None
    return _screen_ready(req.get("harness"), lines), agent, lines


def _wait_for_home(target, bound_ms):
    """Event-driven pause for OpenCode: its pane is `done` before, during and after `/new`, so
    `_settle` would return at once. Waits (bounded) for the home screen's placeholder to show;
    the answer is not used -- the caller looks at the screen itself."""
    try:
        subprocess.run(_herdr_argv("pane", "wait-output", target, "--match", "Ask anything",
                                   "--source", "visible", "--timeout", str(int(bound_ms))),
                       capture_output=True, text=True, timeout=bound_ms / 1000.0 + 5)
    except (OSError, subprocess.SubprocessError):
        pass


def _opencode_home(lines):
    """OpenCode's home screen: the placeholder box (older builds) or, as measured on 1.18.34,
    nothing at all but the working-directory line. A conversation view always carries the
    `┃` input bars and a model line, so those never read as home."""
    plain = [_plain(line).strip() for line in lines]
    shown = [text for text in plain if text]
    if any(text.startswith("\u2503") for text in shown):
        return any(_OPENCODE_HOME_PLACEHOLDER in "".join(text.split()).lower() for text in shown) \
            and _draft_state("opencode", lines) == "empty"
    return 0 < len(shown) <= 2 and shown[-1][:1] in ("/", "~")


def _seat_knows(req, sid):
    """True when the booking seat's own ledger has seen session `sid` of the booked harness."""
    try:
        import session_tidy as st
        seat_fields = req.get("seat") or {}
        seat = st.Seat(str(seat_fields.get("kind") or ""), str(seat_fields.get("key") or ""),
                       str(seat_fields.get("pane") or ""), str(seat_fields.get("harness") or ""),
                       str(seat_fields.get("project_key") or ""))
        return (str(req.get("harness")), sid) in st.session_summary(seat)
    except Exception:
        return True                     # unreadable: never call an unknown id new


def _codex_new_thread(target, req, before):
    """The thread Codex's status line shows now and did not show at the look before the send:
    exactly one whole id, the cleared one gone, unknown to the seat. Else None."""
    lines = _read_screen(target)
    if lines is None:
        return None
    shown = _codex_footer_threads(lines)
    fresh = shown - set(before or ())
    if len(fresh) != 1 or req.get("sid") in shown:
        return None
    (thread,) = fresh
    return None if _seat_knows(req, thread) else thread


def _clear_observe(target, req, request_path, before_threads=()):
    """The new conversation, seen: its session id (`-` when the harness has none yet), or None.

    Hook-side proof first (the booking's `observed`, written by the new session's start hook),
    then herdr's own session id for the pane (Claude/Codex), then the screen: Codex's status line
    naming a new thread (its start hook only runs with the first message), OpenCode's home."""
    old = req.get("sid")
    for round_no in range(_CLEAR_OBSERVE_ROUNDS):
        held = _read_json(Path(request_path))
        seen = (held or {}).get("observed") or {}
        if seen.get("sid") and seen.get("sid") != old:
            return str(seen["sid"])
        state, agent, _code, unavailable = _interpret_payload(_run_herdr_get(target), target)
        sid = agent.get("session_id")
        if not unavailable and req.get("harness") in ("claude", "codex"):
            # The process's own session decides when it can be read; herdr's pane record can lag
            # a /clear (or keep an older session) and is only the fallback.
            proven = _process_session(agent.get("pane") or target, req.get("harness"))
            if proven and _herdr_lags(req, proven):
                proven = None           # the process check named an older session of this seat
            if proven:
                if proven != old:
                    return str(proven)
            elif sid not in (None, "-", old) and not _herdr_lags(req, sid):
                return str(sid)
        if not unavailable and req.get("harness") == "codex":
            thread = _codex_new_thread(target, req, before_threads)
            if thread:
                return thread
        if not unavailable and req.get("harness") == "opencode" and state in ("idle", "done"):
            lines = _read_screen(target)
            if lines is not None and _opencode_home(lines):
                return "-"
        if round_no + 1 < _CLEAR_OBSERVE_ROUNDS:
            if req.get("harness") == "opencode":
                _wait_for_home(target, _CLEAR_OBSERVE_SETTLE_MS)
            else:
                # The pane stays `done` through /clear, so an idle wait would return at once and every
                # round would pass before the start hook ran or Codex repainted (measured 2026-10-02:
                # 8 rounds in 187 ms). Waiting for a change gives each round its bound.
                _run_herdr_wait(target, ["working", "blocked"], _CLEAR_OBSERVE_SETTLE_MS)
    return None


def _native_tidy_command(args, req, *, continuing):
    """Keep the exact-job transport under the same peer-steward judgement ledger."""
    from session_tidy_native import command
    action, word = ("continue", "continued") if continuing else ("clear", "cleared")
    started = time.monotonic()
    verdict = command(req, args.request, args.nonce, continuing=continuing, screen_ready=_screen_ready)
    outcome = verdict[word]
    harness, old = req.get("harness"), req.get("sid")
    new = verdict.get("new_session") or req.get("new_session")
    target = f"native:{req['seat']['native']['job']}"
    receipt = (f"action={action} {word}={outcome} harness={harness} old_session={old} "
               f"new_session={new or '-'} verify=native ms={int((time.monotonic() - started) * 1000)}"
               + (f" reason={verdict['reason']}" if verdict.get("reason") else ""))
    try:
        _record(to_harness=harness, to_name=target, kind="notice",
                summary_text=f"[notice] action={action} {outcome}",
                to_session_id=new if continuing else old, to_pane=None, ref=[],
                status=_CLEAR_LEDGER_STATUS[outcome], receipt=receipt,
                from_identity=(old, harness, _project_of(req.get("cwd"))), from_name=_from_name(harness, old))
    except Exception:
        pass
    print(" ".join(f"{key}={value}" for key, value in verdict.items() if value))
    return _CLEAR_EXIT[outcome]


def cmd_clear(args):
    """session-tidy auto-clear: type the harness's own new-conversation command into `target`.

    The last judgement and the only pane input of the clear flow. `--request` is the seat's
    booking (`session_tidy_clear.py`); the command is typed only when all of these hold now:

    * the booking is the current one (nonce), unexpired, no prompt was submitted since the card,
      the card is the booked generation;
    * the pane is the booked one, runs the booked harness (and session, where herdr names it),
      and is idle or done (never working, never blocked);
    * the visible screen shows no selection/permission form and the input box is read as empty
      (a Claude suggestion or a Codex placeholder is empty; anything undecidable is not).

    Both looks (`_clear_look`) are taken, the second immediately before the single
    `herdr agent prompt`; herdr offers no send conditional on the pane's revision, so a keystroke
    landing between that look and the send cannot be ruled out -- hence no trailer, no Enter
    retry, and nothing is ever re-sent after a doubtful result.

    `cleared=true` only with evidence the conversation changed (start hook note, new herdr
    session id, a new thread id on Codex's status line, OpenCode home); otherwise `unverified`.
    Exit 0 true / 3 skipped / 1 failed / 5 unverified. One ledger row (`kind=notice`,
    `action=clear` in the receipt) per judgement.
    """
    import session_tidy_clear as clear
    req, why = clear.validate_request(args.request, args.nonce)
    if req and (req.get("seat") or {}).get("kind") == "native":
        return _native_tidy_command(args, req, continuing=False)
    if _herdr_missing():
        print("cleared=failed reason=herdr-not-found")
        return _CLEAR_EXIT["failed"]
    req, why = clear.validate_request(args.request, args.nonce)
    if req is None:
        print(f"cleared=skipped reason={why}")
        return _CLEAR_EXIT["skipped"]
    harness, old_sid, target = req.get("harness"), req.get("sid"), args.target
    command = _CLEAR_COMMAND.get(harness)
    started = time.monotonic()
    new_session, pane = None, (req.get("seat") or {}).get("pane")

    def finish(outcome, reason=None, agent=None):
        nonlocal pane
        if isinstance(agent, dict) and agent.get("pane") not in (None, "-"):
            pane = agent.get("pane")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        receipt = (f"action=clear cleared={outcome} harness={harness} old_session={old_sid} "
                   f"new_session={new_session or '-'} ms={elapsed_ms}" + (f" reason={reason}" if reason else ""))
        try:
            _record(to_harness=harness or "unknown", to_name=target, kind="notice",
                    summary_text=f"[notice] action=clear {outcome} {command or ''}".strip(),
                    to_session_id=old_sid, to_pane=pane, ref=[], status=_CLEAR_LEDGER_STATUS[outcome],
                    receipt=receipt, from_identity=(old_sid, harness, _project_of(req.get("cwd"))),
                    from_name=_from_name(harness, old_sid))
        except Exception:
            pass        # the verdict below is the result; a ledger hiccup must not change it
        line = (f"cleared={outcome} target={target} harness={harness} old_session={old_sid} "
                f"new_session={new_session or '-'}" + (f" reason={reason}" if reason else ""))
        print(line)
        return _CLEAR_EXIT[outcome]

    if not command:
        return finish("skipped", "unsupported-harness")
    reason, agent, _lines = _clear_look(target, req)
    if reason:
        return finish(_look_outcome(reason), reason, agent)
    req, why = clear.validate_request(args.request, args.nonce)
    if req is None:
        return finish("skipped", why, agent)
    reason, agent, lines = _clear_look(target, req)   # the look immediately before the one send
    if reason:
        return finish(_look_outcome(reason), reason, agent)
    before = _codex_footer_threads(lines) if harness == "codex" else set()
    rc, payload = _herdr_prompt(target, command, wait=False, timeout_ms=_PROMPT_VERIFY_TIMEOUT_MS)
    if rc is None:
        return finish("failed", "herdr-invocation-failed", agent)
    if rc != 0:
        return finish("failed", _herdr_error_reason(payload, rc), agent)
    new_session = _clear_observe(target, req, args.request, before)
    if new_session is None:
        return finish("unverified", "new-session-not-observed", agent)
    return finish("true", None, agent)


def _look_outcome(reason):
    return "failed" if reason.startswith(("herdr-", "agent-not-found")) else "skipped"


# --- continue: the one prompt that lets the cleared window's new session carry on (session-tidy) ---

_CONTINUE_LOOK_ROUNDS = 12            # bounded re-looks while the new session settles (idle, card handed over)
_CONTINUE_SETTLE_MS = 1500


def _booked_seat(req):
    import session_tidy as st
    fields = req.get("seat") or {}
    return st, st.Seat(str(fields.get("kind") or ""), str(fields.get("key") or ""), str(fields.get("pane") or ""),
                       str(fields.get("harness") or ""), str(fields.get("project_key") or ""))


def _continue_card_reason(req):
    """Whether the booked card reaches the new session: Claude's start hook hands it over at once
    (its receipt is there), Codex and OpenCode hand it over with the first message (nobody holds
    it yet). `card-pending` is worth another look; `card-taken` is final."""
    st, seat = _booked_seat(req)
    card = st.read_latest_card(seat)
    consumed = st.read_json(st._consumed_path(seat))
    held = isinstance(consumed, dict) and bool(card) and consumed.get("generation") == card.get("generation")
    receipts = (consumed.get("receipts") or []) if held else []
    if req.get("harness") == "claude":
        mine = f"claude:{req.get('new_session')}:"
        return None if any(str(r).startswith(mine) for r in receipts) else "card-pending"
    return "card-taken" if receipts else None


def _continue_look(target, req):
    """One judgement of the cleared window before the continue prompt: None when it may be typed,
    else the reason.

    The pane and harness are the booked ones and the session there is the one the clear started,
    with no message yet: a proven process selection takes precedence over delayed pane labels.
    Claude -- the pane's process (or herdr) names `new_session`; Codex -- no
    rollout exists for `new_session` yet and the status line shows it (or herdr/the process name
    it); OpenCode -- its home screen, which also is its empty box. Then idle, the card on its way,
    no form open, an empty input box."""
    harness, new = req.get("harness"), str(req.get("new_session") or "")
    state, agent, _code, unavailable = _interpret_payload(_run_herdr_get(target), target)
    if unavailable or state in ("agent-not-found", "timeout"):
        return unavailable or "agent-not-found"
    seat_pane = (req.get("seat") or {}).get("pane")
    if (seat_pane and agent.get("pane") != seat_pane) or agent.get("harness") != harness:
        return "target-changed"
    pane = agent.get("pane") or target
    if harness in ("claude", "codex") and new in ("", "-"):
        return "new-session-unknown"
    current = _process_session(pane, harness) if harness in ("claude", "codex") else None
    if current and current != new:
        return "target-changed"
    if harness == "claude" and (current or agent.get("session_id")) != new:
        return "target-changed"
    if harness == "codex" and _codex_rollout_exists(new):
        return "new-input"              # the new thread already had its first message
    if state == "blocked":
        return "form-open"
    if state not in ("idle", "done"):
        return f"not-idle-{state}"
    card = _continue_card_reason(req)
    if card:
        return card
    lines = _read_screen(target)
    if lines is None:
        return "screen-unknown"
    if harness == "opencode":
        flat = "".join("".join(_plain(c) for c in lines).split()).lower()
        if any(token in flat for token in _FORM_TOKENS):
            return "form-open"
        draft = _draft_state(harness, lines)
        if draft == "nonempty":
            return "draft"
        return None if _opencode_home(lines) else "target-changed"
    if harness == "codex" and new not in _codex_footer_threads(lines) \
            and agent.get("session_id") != new and current != new:
        return "target-changed"
    return _screen_ready(harness, lines)


def _continue_arrival(harness, sid, text, since_epoch):
    """The continue prompt as a user row of the new Claude session's own transcript (only that one:
    a short text could also be typed elsewhere)."""
    if harness != "claude" or not sid:
        return None
    import glob as _glob
    for path in _glob.glob(os.path.expanduser(f"~/.claude/projects/*/{_glob.escape(sid)}.jsonl")):
        ts = _transcript_rows_with(path, text, since_epoch)
        if ts:
            return ts
    return None


def cmd_continue(args):
    """session-tidy auto-continue: type the continue prompt once into a window whose clear was confirmed.

    `--request` is the seat's booking after `peer-steward.py clear` reported the new session
    (`session_tidy_clear.py`). The prompt (`session_tidy_clear.CONTINUE_TEXT`, no trailer) is typed
    only when all of these hold now:

    * the booking's continue is pending and in time, no prompt was submitted since the card, the
      card is the booked generation and was not handed to a peer (`validate_continue`);
    * the pane is the booked one, runs the booked harness, and holds the session the clear started,
      which has had no message yet (`_continue_look`);
    * it is idle or done, the card is on its way to it, no form is open and the input box is empty.

    A new session that is still settling (not idle yet, Claude's start hook not through) gets a few
    bounded looks. After the `pending -> sending` claim under the seat lock (`claim_continue`),
    one final look releases an unsent claim if a draft has appeared. An unsent draft/form stays
    pending for the existing idle callback. The single `herdr agent prompt --wait --until working`
    is never re-sent and no Enter is retried. `continued=true` needs the state flip, the seat's prompt count
    moving (the new session's prompt hook) or, for Claude, the text in the new transcript; anything
    else is `unverified`. A keystroke landing between the last look and the send cannot be ruled out
    (herdr has no conditional send), as for `clear`. Exit 0 true / 3 queued or skipped / 1 failed /
    5 unverified. One ledger row (`kind=notice`, `action=continue` in the receipt) per judgement.
    """
    import session_tidy_clear as clear
    req, why = clear.validate_continue(args.request, args.nonce)
    if req and (req.get("seat") or {}).get("kind") == "native":
        return _native_tidy_command(args, req, continuing=True)
    if _herdr_missing():
        print("continued=failed reason=herdr-not-found")
        return _CLEAR_EXIT["failed"]
    req, why = clear.validate_continue(args.request, args.nonce)
    if req is None:
        print(f"continued=skipped reason={why}")
        return _CLEAR_EXIT["skipped"]
    harness, old_sid, new_sid, target = req.get("harness"), req.get("sid"), req.get("new_session"), args.target
    text = clear.CONTINUE_TEXT
    started = time.monotonic()
    verify, pane = "none", (req.get("seat") or {}).get("pane")

    def finish(outcome, reason=None):
        elapsed_ms = int((time.monotonic() - started) * 1000)
        receipt = (f"action=continue continued={outcome} harness={harness} old_session={old_sid} "
                   f"new_session={new_sid or '-'} verify={verify} ms={elapsed_ms}"
                   + (f" reason={reason}" if reason else ""))
        try:
            _record(to_harness=harness or "unknown", to_name=target, kind="notice",
                    summary_text=f"[notice] action=continue {outcome} {text}",
                    to_session_id=new_sid if new_sid not in (None, "", "-") else None, to_pane=pane, ref=[],
                    status=_CLEAR_LEDGER_STATUS[outcome], receipt=receipt,
                    from_identity=(old_sid, harness, _project_of(req.get("cwd"))),
                    from_name=_from_name(harness, old_sid))
        except Exception:
            pass        # the verdict below is the result; a ledger hiccup must not change it
        print(f"continued={outcome} target={target} harness={harness} new_session={new_sid or '-'} "
              f"verify={verify}" + (f" reason={reason}" if reason else ""))
        return _CLEAR_EXIT[outcome]

    reason = None
    for round_no in range(_CONTINUE_LOOK_ROUNDS):
        reason = _continue_look(target, req)
        if reason is None or not reason.startswith(("not-idle-", "card-pending")):
            break
        if round_no + 1 < _CONTINUE_LOOK_ROUNDS:
            # Bounded and event-driven: until the state changes, else the bound passes (no sleep).
            until = ["idle", "done"] if reason.startswith("not-idle-") else ["working", "blocked"]
            _run_herdr_wait(target, until, _CONTINUE_SETTLE_MS)
    if reason == "card-pending":
        reason = "card-not-delivered"
    def withheld(reason):
        return (reason in {"draft", "draft-unknown", "screen-unknown", "form-open"}
                or reason.startswith("not-idle-"))

    if reason:
        return finish("queued" if withheld(reason) else _look_outcome(reason), reason)
    req, why = clear.validate_continue(args.request, args.nonce)
    if req is None:
        return finish("skipped", why)
    reason = _continue_look(target, req)            # the look immediately before the one send
    if reason:
        return finish("queued" if withheld(reason) else _look_outcome(reason), reason)
    req, why = clear.claim_continue(args.request, args.nonce)
    if req is None:
        return finish("skipped", why)
    reason = _continue_look(target, req)            # the claim must not hide a newer draft
    if reason:
        clear.release_unsent_continue(req)
        return finish("queued" if withheld(reason) else _look_outcome(reason), reason)
    st, seat = _booked_seat(req)
    seq_before = int(req.get("prompt_seq", 0) or 0)
    sent_at = time.time()
    rc, payload = _herdr_prompt(target, text, wait=True, timeout_ms=_PROMPT_VERIFY_TIMEOUT_MS)
    if rc is None:
        return finish("failed", "herdr-invocation-failed")
    if rc == 0:
        verify = "state-flip"
        return finish("true")
    reason = _herdr_error_reason(payload, rc)
    if reason != "timeout":
        return finish("failed", reason)
    if st.read_prompt_seq(seat) > seq_before:
        verify = "prompt-hook"
        return finish("true")
    if _continue_arrival(harness, new_sid, text, sent_at):
        verify = "transcript-arrival"
        return finish("true")
    return finish("unverified", "submission-not-observed")


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

    p_obligation = sub.add_parser("__obligation-runner", help=argparse.SUPPRESS)
    p_obligation.add_argument("--lock-fd", type=int, required=True)
    p_obligation.add_argument("--state-root", required=True)
    p_obligation.set_defaults(func=cmd_obligation_runner)

    p_recover = sub.add_parser("__ensure-obligations", help=argparse.SUPPRESS)
    p_recover.set_defaults(func=cmd_ensure_obligations)

    p_wait = sub.add_parser("wait")
    p_wait.add_argument("target")
    p_wait.add_argument("--until", action="append", default=[])
    p_wait.add_argument("--timeout", type=int, default=None, help="milliseconds")
    p_wait.add_argument("--ref", action="append", default=[])
    p_wait.set_defaults(func=cmd_wait)

    p_start = sub.add_parser("start")
    p_start.add_argument("name")
    p_start.add_argument("--kind", required=True, choices=("claude", "codex", "opencode"))
    pane_choice = p_start.add_mutually_exclusive_group()
    pane_choice.add_argument("--pane")
    pane_choice.add_argument("--beside", help="default: the calling pane (HERDR_PANE_ID)")
    p_start.add_argument("--cwd", default=None, help="default for a new pane: the calling cwd")
    p_start.add_argument("--permission-mode", choices=("bypass", "inherit"), default=None)
    p_start.set_defaults(func=cmd_start)

    p_retire = sub.add_parser("retire")
    p_retire.add_argument("target")
    p_retire.set_defaults(func=cmd_retire)

    # --- SD-122 (10) detached steward watch ---
    p_watch = sub.add_parser("watch")
    p_watch.add_argument("target")
    p_watch.add_argument("--until", action="append", default=[])
    p_watch.add_argument("--timeout", type=int, default=None, help="milliseconds")
    p_watch.add_argument("--ref", action="append", default=[])
    p_watch.add_argument("--wake", choices=("auto", "hook", "none"), default="auto")
    p_watch.set_defaults(func=cmd_watch, rearmed_from=None, rearm_count=0)

    # Hidden: the detached watcher's own entry point, reached only by re-exec.
    p_run = sub.add_parser("__watch-run")
    p_run.add_argument("--watch-id", required=True)
    p_run.add_argument("--target", required=True)
    p_run.add_argument("--server", default="default")
    p_run.add_argument("--expected-harness", default="-")
    p_run.add_argument("--expected-session-id", default="-")
    p_run.add_argument("--expected-pane", default="-")
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
    p_join.add_argument("--timeout", type=int, default=None, help="milliseconds")
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

    p_clear = sub.add_parser("clear")
    p_clear.add_argument("target")
    p_clear.add_argument("--request", required=True,
                         help="the seat's auto-clear booking (session_tidy_clear.py)")
    p_clear.add_argument("--nonce", default=None, help="the booking's nonce, when the caller holds one")
    p_clear.set_defaults(func=cmd_clear)

    p_continue = sub.add_parser("continue")
    p_continue.add_argument("target")
    p_continue.add_argument("--request", required=True,
                            help="the seat's auto-clear booking after a confirmed clear (session_tidy_clear.py)")
    p_continue.add_argument("--nonce", default=None, help="the booking's nonce, when the caller holds one")
    p_continue.set_defaults(func=cmd_continue)

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
