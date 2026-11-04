"""Peer steward: constants and stateless helpers for prompts, herdr, tmux and retire.

Split out of `peer-steward.py`, which re-exports every name defined here.
"""
import hashlib
import importlib.util
import json
import math
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import peer_obligations
import terminal_surface


_UTILITIES_DIR = Path(__file__).resolve().parent
_UNSET_NAME = object()

_PM_SPEC = importlib.util.spec_from_file_location(
    "peer_message", str(_UTILITIES_DIR / "peer-message.py")
)
peer_message = importlib.util.module_from_spec(_PM_SPEC)
_PM_SPEC.loader.exec_module(peer_message)
try:  # ends a detached child with the test that started it; no-op outside tests
    from detached_lifetime import bind as end_with_test  # noqa: E402
except ImportError:  # a partial tree without it (e.g. a fixture release): no record
    def end_with_test(child):
        return child

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


def _herdr_env_for_server(server, environ=None):
    """Carry an inherited socket only when its HERDR_SESSION owner still matches."""
    selected = server or "default"
    env = dict(os.environ if environ is None else environ)
    socket_owner = env.get("HERDR_SESSION") or "default"
    if socket_owner != selected:
        env.pop("HERDR_SOCKET_PATH", None)
    if selected == "default":
        env.pop("AGENT_HERDR_SESSION", None)
        if env.get("HERDR_SESSION") not in {None, "", "default"}:
            env.pop("HERDR_SESSION", None)
    else:
        env["AGENT_HERDR_SESSION"] = selected
        if env.get("HERDR_SESSION") not in {None, "", selected}:
            env.pop("HERDR_SESSION", None)
    return env


_PERMISSION_FLAGS = {
    "claude": ["--permission-mode", "bypassPermissions"],
    "codex": ["--dangerously-bypass-approvals-and-sandbox"],
    "opencode": ["--auto"],
}


def _project_of(cwd):
    if not cwd:
        return ""
    return os.path.basename(str(cwd).rstrip("/"))


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


_PROMPT_TAG_ADDRESS = re.compile(r"^\[([0-9a-f]{2})\]$", re.IGNORECASE)
_PROMPT_BARE_TAG_ADDRESS = re.compile(r"^[0-9a-f]{2}$", re.IGNORECASE)
_PROMPT_HARNESSES = {"claude", "codex", "opencode"}


def _prompt_listed_session_id(agent):
    identity = agent.get("agent_session")
    if identity is None:
        return None
    if not isinstance(identity, dict) or identity.get("kind") not in (None, "id"):
        return None
    return peer_message.usable_session_id(identity.get("value"))


def _prompt_fleet_tag(harness, session_id, *, pane=None, cwd=None):
    """Use Fleet's tag rule with the explicit recipient seat context."""
    if harness not in _PROMPT_HARNESSES or not peer_message.usable_session_id(session_id):
        return None, "target-identity-unverified"
    try:
        tools_dir = Path(__file__).resolve().parent.parent / "tools"
        if tools_dir.is_dir() and str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))
        from fleet.session_handle import resolve_recipient_tag
        tag = resolve_recipient_tag(harness, session_id, pane=pane, cwd=cwd)
    except Exception:
        return None, "fleet-tag-observation-unavailable"
    if tag is None:
        return None, None
    if not isinstance(tag, str) or not re.fullmatch(r"[0-9a-f]{2}", tag, re.IGNORECASE):
        return None, "fleet-tag-observation-unavailable"
    return tag.lower(), None


def _prompt_inventory_fields(agent):
    harness, pane, name = agent.get("agent"), agent.get("pane_id"), agent.get("name")
    if (not isinstance(harness, str) or not harness
            or not isinstance(pane, str) or not pane or pane == "-"):
        return None
    identity = agent.get("agent_session")
    if identity is not None:
        if not isinstance(identity, dict) or identity.get("kind") not in (None, "id"):
            return None
        value = identity.get("value")
        if value not in (None, "", "-") and not peer_message.usable_session_id(value):
            return None
    if not isinstance(name, str) or not name or name == "-":
        name = None
    return {"harness": harness.lower(), "pane": pane, "name": name,
            "session_id": _prompt_listed_session_id(agent)}


def _prompt_collapse_rows(rows):
    """Collapse duplicate observations of the same pane identity, rejecting conflicts."""
    by_pane = {}
    for row in rows:
        prior = by_pane.get(row["pane"])
        if prior is None:
            by_pane[row["pane"]] = row
            continue
        if (prior["harness"] != row["harness"]
                or (prior["name"] and row["name"] and prior["name"] != row["name"])
                or (prior["session_id"] and row["session_id"]
                    and prior["session_id"] != row["session_id"])):
            return None
        if not prior["name"] and row["name"]:
            prior["name"] = row["name"]
        if not prior["session_id"] and row["session_id"]:
            prior["session_id"] = row["session_id"]
    return list(by_pane.values())


def _prompt_inventory_panes(agents):
    if not isinstance(agents, list):
        return set()
    return {agent["pane_id"] for agent in agents if isinstance(agent, dict)
            and isinstance(agent.get("pane_id"), str) and agent["pane_id"]}


def _prompt_message_kind(body):
    first = body.strip().splitlines()[0] if body.strip() else ""
    for prefix, value in (("[steer]", "steer"), ("[handoff]", "handoff"),
                          ("[gate]", "gate-relay")):
        if first.startswith(prefix):
            return value
    return "steer"


def _herdr_missing():
    return shutil.which("herdr") is None


def _unavailable(reason):
    print(f"herdr-unavailable reason={reason} fallback={_fallback()}")
    return 4


def _tmux_surface(target):
    """The tmux server answering for a tmux pane id (``%N``), else None.

    Herdr targets keep their own calls unchanged; only the pane id selects tmux, so
    a tmux pane is never addressed through Herdr or the reverse."""
    if not terminal_surface.is_tmux_pane(target):
        return None
    return terminal_surface.pane_surface(target)


def _tmux_input_refusal(target):
    """For a tmux pane: why typed input must not reach it now (a tmux mode, synchronized
    panes, input off), else None. Herdr targets are unaffected."""
    tmux = _tmux_surface(target)
    return tmux.input_refusal(target) if tmux is not None else None


_TMUX_INPUT_WAIT = frozenset({"pane-in-mode", "pane-synchronized", "pane-input-off", "pane-state-unknown"})


def _tmux_target_address(target):
    """A bare tmux pane id names a pane on this caller's server: carry that server with it."""
    if not terminal_surface.is_tmux_pane(target) or terminal_surface.tmux_address(target)[1]:
        return target
    tmux = _tmux_surface(target)
    return (tmux.address(target) if tmux is not None else None) or target


def _surface_missing(target):
    """The terminal that would answer for ``target`` is not installed."""
    if terminal_surface.is_tmux_pane(target):
        return shutil.which("tmux") is None
    return _herdr_missing()


def _tmux_inventory():
    """Without herdr: the tmux server this process runs in (else tmux's default), when it answers."""
    if not _herdr_missing() or not shutil.which("tmux"):
        return None
    surface = terminal_surface.TmuxSurface(socket=terminal_surface.tmux_socket())
    return surface if surface.answers() else None


def _tmux_census(agents, endpoint):
    """A complete pane census from one tmux agent inventory, in Herdr's census shape."""
    from types import SimpleNamespace
    if not isinstance(agents, list):
        return None
    panes = tuple({"pane_id": agent.get("pane_id"), "agent": agent.get("agent"),
                   "agent_session": agent.get("agent_session"), "cwd": agent.get("cwd")}
                  for agent in agents if isinstance(agent, dict))
    return SimpleNamespace(panes=panes, complete=True, endpoint=endpoint)


_AGENT_STATES = ("idle", "done", "blocked", "working", "finalizing", "unknown")
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


def _payload_cwd(payload):
    """The target agent's cwd from a herdr payload, or None."""
    try:
        cwd = payload["result"]["agent"].get("cwd")
    except (KeyError, TypeError, AttributeError):
        return None
    return cwd if isinstance(cwd, str) and cwd else None


def _print_steward_limited(result):
    """A refused steward grant never fails the command that asked for it; it only says so."""
    if isinstance(result, dict) and result.get("status") == "limited":
        print(peer_message.format_steward_limited(result), file=sys.stderr)


def _ensure_steward_runner(result):
    """A written grant carries a steward-check duty; the shared runner advances it with
    no later input, so start it now instead of waiting for the next lifecycle event."""
    if isinstance(result, dict) and result.get("written"):
        try:
            peer_obligations.ensure_normal_runner()
        except Exception:
            pass


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


_MANAGED_ENTRY_MARK = "codex-managed-entry"
_MANAGED_ENV_MARK = "AGENT_CODEX_MANAGED_GATEWAY"


# A Codex TUI attached to the shared app-server daemon holds no rollout file of its own, and
# the daemon creates the thread's rollout about a second after the TUI starts. Fleet can then
# only match a TUI to its thread by start time, which several same-cwd starts a few seconds
# apart defeat on purpose (no guessing), so the row stays anonymous and herdr learns nothing.
# `start` is the one party that saw the launch: it notes which rollouts exist before the
# launch and afterwards takes exactly ONE new root rollout for the target cwd as the session.
# Zero or several is no proof, so nothing is bound and the receipt says why. Bookkeeping, not
# a gate: it adds no flag or required input and never fails the start; the bound is the wait.


def _codex_home_dir():
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")








def _proc_cwd(pid):
    try:
        return os.path.realpath(os.readlink("/proc/%d/cwd" % int(pid)))
    except (OSError, ValueError):
        return None








def _proc_start_ticks(pid):
    """/proc/<pid>/stat field 22 as a str (the registry's PID-reuse guard), else None."""
    try:
        raw = Path("/proc/%d/stat" % int(pid)).read_text()
        rest = raw[raw.rindex(")") + 1:].split()
        return None if rest[0] in ("Z", "X") else rest[19]
    except (OSError, ValueError, IndexError):
        return None




# The harness flag that puts a launched session in a chosen directory. `herdr agent
# start` has none of its own — the agent it starts inherits the PANE's shell cwd — so
# `--cwd` used to move nothing but this CLI process: a session started with
# `--cwd <hearting>` came up in `SR_ModelA`, the pane's own directory (measured
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


def _tmux_named_pane(target):
    """Without Herdr: the one tmux pane whose started runtime carries ``target`` as its name."""
    if terminal_surface.is_tmux_pane(target):
        return target
    tmux = _tmux_inventory()
    payload = tmux.agents() if tmux is not None else None
    found = [agent.get("pane_id") for agent in ((payload or {}).get("result") or {}).get("agents") or ()
             if isinstance(agent, dict) and agent.get("name") == target]
    return found[0] if len(found) == 1 else target


# NAS-backed prompt initialization can spend minutes in a git child. Keep one
# finite deadline and the same shell/prompt checks through bootstrap and start.
_BESIDE_READY_SECONDS = 300


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


def _seat_successor_path(pane):
    digest = hashlib.sha256(str(pane).encode("utf-8")).hexdigest()[:32]
    return peer_message.peer_state_root() / "seat-successors" / f"{digest}.json"


def _seat_successor_lock(pane, *, wait=None):
    import session_tidy
    return session_tidy.seat_lock("peer-successor-" + _seat_successor_path(pane).stem, wait=wait)


def _source_before_acceptance(source, accepted_at, *times):
    values = (accepted_at, source.get("mtime"), source.get("ctime"), *times)
    return (all(isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v) and v > 0 for v in values)
            and all(v <= accepted_at for v in values[1:]))


def _headless_session_bindings(session_id, jobs):
    """Verified headless OpenCode owner sidecars whose native session is ``session_id``.

    Only exact matches count: an unrelated owner without a readable sidecar is
    no evidence either way, while matching foreign or closed owners still count."""
    import dispatch_seat_handover as handover
    import opencode_session_runtime as native
    from dispatch_completion_join import current_attempt_row
    matches = []
    if not jobs:
        return matches
    try:
        from dispatch_registry_cache import registry_view
        candidate_rows = registry_view(Path(jobs)).headless_owner_candidates()
    except OSError:
        return matches
    for _status, registry_record in candidate_rows:
        meta = registry_record.handover_metadata()
        aid = meta.get("attempt_id", "")
        if (not handover.eligible_row(meta) or meta.get("worker_type") != "owner"
                or meta.get("harness") != "opencode" or meta.get("transport") != "headless"
                or meta.get("registered_worker") != "1"
                or meta.get("execution_surface") != "registered-headless"):
            continue
        try:
            row = current_attempt_row(Path(jobs), aid)
            if row is None or row.metadata != meta:
                continue
            # The expected worktree is the registry row's, never the sidecar's own.
            binding = native.read_binding_record(SimpleNamespace(
                jobs=jobs, parent_attempt_id=aid, worktree=row.raw.split("\t")[3]))
        except Exception:  # noqa: BLE001 - an unreadable sidecar proves no relation
            continue
        if binding is not None and binding["value"]["session_id"] == session_id:
            matches.append((row, binding))
    return matches


def _retained_tui_selection(foreground):
    tools = str(_UTILITIES_DIR.parent / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from fleet.collectors.opencode import _tui_selection_record
    return _tui_selection_record(foreground["pid"], foreground["start"])


def _retire_predecessor(duty):
    """Return a handover-only identity copy and the source generations it relied on.

    A booked OpenCode label that is a verified headless owner session is stale.
    Only a successful completed replay replaces it, with the original lifetime's
    retained TUI selection, and only when that selection is the registered
    parent of the one matching owner binding. Unknown never falls back to the
    stale label; every other booking keeps the ordinary path."""
    import dispatch_seat_handover as handover
    intent = duty.get("intent") or {}
    identity = intent.get("identity") or {}
    booked = identity.get("session_id")
    foreground = _retire_booked_foreground(duty)
    if (not _handoff_lifetime_valid(foreground) or not isinstance(booked, str)
            or booked in {"", "-"} or duty.get("state") in {"cancelled", "failed"}):
        return None
    if identity.get("harness") != "opencode":
        return dict(identity), None
    jobs = handover._default_jobs()
    matches = _headless_session_bindings(booked, jobs)
    complete = duty.get("state") == "complete"
    # Read the foreground independently; the census never supplies its SID.
    record = _retained_tui_selection(foreground) if complete else None
    if not matches:
        if record is not None and record["value"]["sessionID"] != booked:
            return None  # the selection contradicts the label without a proven relation
        return dict(identity), None
    if (not complete or duty.get("result") not in {"normal-exit", "target-already-gone"}
            or _retire_subject_state(duty) != "gone" or record is None or len(matches) != 1):
        return None
    sid = record["value"]["sessionID"]
    argv = foreground.get("argv")
    row, binding = matches[0]
    accepted = duty.get("accepted_at")
    if (sid == booked or not isinstance(argv, list) or not argv
            or any(not isinstance(a, str) for a in argv)
            or os.path.basename(argv[0]) != "opencode" or "run" in argv[1:]
            or row.metadata.get("parent_sid") != sid
            or not _source_before_acceptance(record["source"], accepted, record["source"]["written_at"])
            or not _source_before_acceptance(binding["source"], accepted)
            or handover.binding_of(jobs, row.metadata) not in handover.current_bindings("opencode", sid, jobs)):
        return None
    return {**identity, "session_id": sid}, (record, row.raw, binding)


def _start_request_key(sender, requested):
    fields = ("server", "name", "kind", "pane", "beside", "cwd", "permission_mode", "agent_args",
              "project_workspace")
    identity = {key: requested.get(key) for key in fields}
    if identity["project_workspace"]:
        identity.update(launch_cwd=requested.get("launch_cwd"), project_hint=requested.get("project_hint"))
    return hashlib.sha256(json.dumps([sender, identity], sort_keys=True).encode()).hexdigest()


# A typed tmux launch has no native start receipt: the runtime must show itself in the
# pane (its foreground process, then its own session record) within this bound.
_TMUX_START_SECONDS = 30


def _tmux_shell_identity(surface, pane):
    """``(shell pid, start)`` when the pane's shell alone holds its terminal, else None."""
    info = surface.process_info(pane)
    if (not info or info["foreground_process_group_id"] != info["shell_pid"]
            or [item.get("pid") for item in info["foreground_processes"]] != [info["shell_pid"]]):
        return None
    start = _proc_start_ticks(info["shell_pid"])
    return (info["shell_pid"], start) if start else None


def _tmux_ready_shell(surface, pane, cwd, deadline):
    """``(shell, screen, None)`` once the pane's own shell sits at an empty prompt.

    The same shell must hold the terminal, show a prompt and show the same screen
    twice in a row (an initializing prompt may still redraw); ``cwd`` must match when
    given. Anything else until ``deadline`` is ``(…, reason)``: nothing is typed.
    """
    shell, previous = None, None
    while time.monotonic() < deadline:
        if _tmux_agent(surface, pane) is not None:
            return shell, None, "pane-occupied"
        current = _tmux_shell_identity(surface, pane)
        if shell is None:
            shell = current
        elif current is not None and current != shell:
            return shell, None, "beside-shell-changed"
        screen = surface.read_visible(pane) if current is not None else None
        refusal = surface.input_refusal(pane) if current is not None else None
        if refusal:
            # A mode, input-off or synchronized window: nothing may be typed, so say so now.
            return shell, (screen if current == shell else None), refusal
        ready = (current is not None and surface.empty_shell(pane)
                 and (cwd is None or _proc_cwd(current[0]) == cwd))
        if ready and screen and screen == previous:
            return shell, screen, None
        previous = screen if ready else None
        time.sleep(min(.1, max(0, deadline - time.monotonic())))
    return shell, None, "beside-shell-readiness-timeout"


def _tmux_agent(surface, pane):
    payload = surface.agent(pane)
    agent = ((payload or {}).get("result") or {}).get("agent")
    return agent if isinstance(agent, dict) else None


def _tmux_launch_line(args, cd=None):
    """One shell line: the managed launcher first on PATH, then the runtime itself."""
    mode = args.permission_mode or _default_permission_mode()
    words = list(_PERMISSION_FLAGS.get(args.kind, [])) if mode == "bypass" else []
    if args.cwd and _CWD_FLAG.get(args.kind):
        words += [_CWD_FLAG[args.kind], os.path.realpath(os.path.expanduser(str(args.cwd)))]
    if args.kind == "opencode" and args.cwd:
        words.append(os.path.realpath(os.path.expanduser(str(args.cwd))))
    words += list(getattr(args, "agent_args", None) or [])
    parts = []
    if cd:
        parts.append("cd -- " + shlex.quote(cd))
    directory = _managed_ingress_dir(args.kind)
    if directory:
        parts.append('export PATH=%s:"$PATH"' % shlex.quote(directory))
    scoped = _opencode_tui_scoped_config() if args.kind == "opencode" else None
    if scoped:
        parts.append("export OPENCODE_TUI_CONFIG=${OPENCODE_TUI_CONFIG:-%s}" % shlex.quote(scoped))
    parts.append(shlex.join([args.kind, *words]))
    return " && ".join(parts), mode


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
    if current is None and _retire_zombie_matches(original["pid"], original["start"]):
        return "gone"
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


def _retire_zombie_matches(pid, expected_start):
    """A same-birth zombie has exited, but /proc still exists until its parent reaps it."""
    if (type(pid) is not int or pid <= 1 or not isinstance(expected_start, str)
            or not expected_start.isdigit()):
        return False
    try:
        root = Path(f"/proc/{pid}")
        raw = (root / "stat").read_text()
        split = raw.rfind(")")
        if split < 0 or int(raw[:raw.find(" (")]) != pid:
            return False
        fields = raw[split + 1:].split()
        if (len(fields) <= 19 or fields[0] not in {"Z", "X"}
                or fields[19] != expected_start):
            return False
        return os.readlink(root / "ns/pid") == os.readlink("/proc/self/ns/pid")
    except (OSError, ValueError, IndexError):
        return False


def _handoff_lifetime_valid(foreground):
    if not isinstance(foreground, dict):
        return False
    pid, start = foreground.get("pid"), foreground.get("start")
    return (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            and not isinstance(start, bool) and str(start).isdigit() and int(start) > 0)


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
_ANSI_TOKEN = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|(.)", re.S)
# A whole thread id; a cut one (`…` in a narrow pane) or one inside a longer token is not.


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
