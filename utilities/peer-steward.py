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
import contextlib
import datetime
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

_UTILITIES_DIR = Path(__file__).resolve().parent
_UNSET_NAME = object()

_PM_SPEC = importlib.util.spec_from_file_location(
    "peer_message", str(_UTILITIES_DIR / "peer-message.py")
)
peer_message = importlib.util.module_from_spec(_PM_SPEC)
_PM_SPEC.loader.exec_module(peer_message)

sys.path.insert(0, str(_UTILITIES_DIR))
from dispatch_contract import (process_start_ticks, resolve_agent_home,
                               resolve_dispatch_state_root)  # noqa: E402
from parent_next_directive import steward_fields  # noqa: E402
import peer_obligations  # noqa: E402
import shared_readiness  # noqa: E402
import dispatch_batch_obligations  # noqa: E402
import herdr_observation  # noqa: E402
import terminal_surface  # noqa: E402

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
_RETIRE_FOLLOWUP_COMPLETE = set()
_RETIRE_FOLLOWUP_RETRIES = {}


def _herdr_argv(*args):
    """`herdr [--session <name>] <args...>` for the selected herdr session."""
    argv = ["herdr"]
    if _HERDR_SESSION:
        argv += ["--session", _HERDR_SESSION]
    return argv + list(args)


def _herdr_env_for_server(server, environ=None):
    """Carry a socket path only when its inherited session still matches."""
    selected = server or "default"
    env = dict(os.environ if environ is None else environ)
    configured = [env.get("AGENT_HERDR_SESSION"), env.get("HERDR_SESSION")]
    if any(value and value != selected for value in configured):
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
        if harness == "codex":
            from pane_ownership import current_codex_thread_for_caller
            current = current_codex_thread_for_caller(
                os.environ.get("HERDR_PANE_ID", ""), server=_HERDR_SESSION)
            if current is not None:
                return current, "codex"
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


_PROMPT_TAG_ADDRESS = re.compile(r"^\[([0-9a-f]{2})\]$", re.IGNORECASE)
_PROMPT_HARNESSES = {"claude", "codex", "opencode"}


def _prompt_agent_inventory():
    """Read the selected herdr endpoint's complete agent inventory, or explain why not."""
    if _herdr_missing():
        return None, "herdr-not-found"
    try:
        proc = subprocess.run(_herdr_argv("agent", "list"), capture_output=True,
                              text=True, timeout=_herdr_get_timeout())
    except (OSError, subprocess.SubprocessError):
        return None, "herdr-agent-list-unavailable"
    if proc.returncode != 0:
        return None, "herdr-agent-list-unavailable"
    try:
        payload = json.loads(proc.stdout or "")
    except (json.JSONDecodeError, TypeError):
        return None, "herdr-agent-list-malformed"
    result = payload.get("result") if isinstance(payload, dict) else None
    agents = result.get("agents") if isinstance(result, dict) else None
    if not isinstance(agents, list) or any(not isinstance(agent, dict) for agent in agents):
        return None, "herdr-agent-list-malformed"
    return agents, None


def _prompt_listed_session_id(agent):
    identity = agent.get("agent_session")
    if identity is None:
        return None
    if not isinstance(identity, dict) or identity.get("kind") not in (None, "id"):
        return None
    return peer_message.usable_session_id(identity.get("value"))


def _prompt_process_session(pane, harness, info):
    """Return one current native session claim across all foreground processes."""
    if (not isinstance(info, dict) or info.get("pane_id") != pane
            or not isinstance(info.get("foreground_processes"), list)):
        return None, "target-observation-unavailable"
    claims = set()
    try:
        if harness == "codex":
            from pane_ownership import current_codex_thread
            executable = shutil.which("herdr")
            for process in info["foreground_processes"]:
                pid = process.get("pid") if isinstance(process, dict) else None
                if type(pid) is not int or pid <= 0:
                    continue
                session_id = current_codex_thread(
                    pane, pid, executable=executable, server=_HERDR_SESSION)
                if peer_message.usable_session_id(session_id):
                    claims.add(session_id)
        else:
            tools_dir = Path(__file__).resolve().parent.parent / "tools"
            if tools_dir.is_dir() and str(tools_dir) not in sys.path:
                sys.path.insert(0, str(tools_dir))
            from fleet.process_identity import PROVEN, process_identity
            for process in info["foreground_processes"]:
                pid = process.get("pid") if isinstance(process, dict) else None
                if type(pid) is not int or pid <= 0:
                    continue
                found = process_identity(pid, harness)
                if (found.confidence == PROVEN
                        and peer_message.usable_session_id(found.session_id)):
                    claims.add(found.session_id)
    except Exception:
        return None, "target-observation-unavailable"
    if len(claims) > 1:
        return None, "target-identity-changed"
    return (next(iter(claims)), None) if claims else (None, None)


def _prompt_current_candidate(pane, *, expected_harness=None, expected_sid=None,
                              expected_name=None, require_process_session=False,
                              target_observation=None, pane_info=None):
    """Bind one pane to a single current native identity before prompt admission."""
    if target_observation is None:
        state, ident, _code, unavailable = _retire_target(pane)
    else:
        if len(target_observation) == 2:
            state, ident = target_observation
            unavailable = None
        else:
            state, ident, _code, unavailable = target_observation
    if state == "agent-not-found":
        return None, "target-not-found"
    if unavailable or state in {"herdr-unavailable", "timeout"}:
        return None, "target-observation-unavailable"
    harness = ident.get("harness")
    current_pane = ident.get("pane")
    if (harness not in _PROMPT_HARNESSES or current_pane != pane
            or not isinstance(pane, str) or not pane or pane == "-"):
        return None, "target-identity-changed"
    if expected_harness and harness != expected_harness:
        return None, "target-identity-changed"
    name = ident.get("name")
    if expected_name is not None and name != expected_name:
        return None, "target-identity-changed"
    herdr_sid = peer_message.usable_session_id(ident.get("session_id"))
    info = pane_info if pane_info is not None else _retire_pane_info(pane)
    if info is None:
        return None, "target-observation-unavailable"
    process_sid, process_error = _prompt_process_session(pane, harness, info)
    if process_error:
        return None, process_error
    expected_sid = peer_message.usable_session_id(expected_sid)
    if (expected_sid and process_sid and expected_sid != process_sid
            or herdr_sid and process_sid and herdr_sid != process_sid):
        return None, "target-identity-changed"
    if expected_sid and not process_sid or herdr_sid and not process_sid:
        return None, "target-identity-unverified"
    if require_process_session and not process_sid:
        return None, "target-identity-unverified"
    session_id = process_sid
    return {"state": state, "harness": harness, "session_id": session_id,
            "name": name if isinstance(name, str) and name and name != "-" else None,
            "pane": pane, "_identity": dict(ident), "_pane_info": info,
            "_native_session_id": process_sid}, None


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


def _prompt_candidate_snapshot():
    """Join the complete selected-endpoint pane census to herdr's agent list."""
    agents, inventory_error = _prompt_agent_inventory()
    census = herdr_observation.pane_census(session=_HERDR_SESSION)
    endpoint = herdr_observation.endpoint_name(_HERDR_SESSION)
    if census is None:
        return None, "pane-census-unavailable", sorted(_prompt_inventory_panes(agents))
    panes = getattr(census, "panes", None)
    if not isinstance(panes, (tuple, list)):
        return None, "pane-census-malformed", sorted(_prompt_inventory_panes(agents))
    census_ids = sorted({item.get("pane_id") for item in panes
                         if isinstance(item, dict)
                         and isinstance(item.get("pane_id"), str)
                         and item.get("pane_id")})
    if (getattr(census, "complete", False) is not True
            or getattr(census, "endpoint", None) != endpoint):
        return None, "pane-census-unavailable", sorted(
            set(census_ids) | _prompt_inventory_panes(agents))
    census_by_pane = {}
    known_panes = set()
    for item in panes:
        pane = item.get("pane_id") if isinstance(item, dict) else None
        if not isinstance(pane, str) or not pane or pane == "-" or pane in census_by_pane:
            return None, "pane-census-malformed", sorted(known_panes)
        known_panes.add(pane)
        census_by_pane[pane] = item

    if agents is None:
        return None, inventory_error or "target-observation-unavailable", sorted(known_panes)

    listed_by_pane = {}
    malformed_panes = set()
    for agent in agents:
        fields = _prompt_inventory_fields(agent)
        if fields is None:
            pane = agent.get("pane_id") if isinstance(agent, dict) else None
            if isinstance(pane, str) and pane:
                malformed_panes.add(pane)
            else:
                malformed_panes.update(known_panes)
            continue
        listed_by_pane.setdefault(fields["pane"], []).append(fields)
    if malformed_panes:
        return None, "herdr-agent-list-malformed", sorted(malformed_panes | known_panes)

    rows, unknown_panes, unknown_reason = [], set(), None
    for pane, item in census_by_pane.items():
        raw_harness = item.get("agent")
        if raw_harness in (None, ""):
            continue
        if not isinstance(raw_harness, str):
            unknown_panes.add(pane)
            unknown_reason = unknown_reason or "pane-census-malformed"
            continue
        harness = raw_harness.lower()
        if harness not in _PROMPT_HARNESSES:
            continue
        listed = _prompt_collapse_rows(listed_by_pane.get(pane, []))
        if listed is None or len(listed or []) != 1:
            unknown_panes.add(pane)
            unknown_reason = unknown_reason or "herdr-agent-list-incomplete"
            continue
        row = listed[0]
        census_session = item.get("agent_session")
        if census_session is not None:
            if (not isinstance(census_session, dict)
                    or census_session.get("kind") not in (None, "id")):
                unknown_panes.add(pane)
                unknown_reason = unknown_reason or "pane-census-malformed"
                continue
            census_sid = peer_message.usable_session_id(census_session.get("value"))
            if census_session.get("value") not in (None, "", "-") and not census_sid:
                unknown_panes.add(pane)
                unknown_reason = unknown_reason or "pane-census-malformed"
                continue
            if census_sid and row.get("session_id") and census_sid != row["session_id"]:
                unknown_panes.add(pane)
                unknown_reason = unknown_reason or "target-identity-changed"
                continue
        else:
            census_sid = None
        if row["harness"] != harness:
            unknown_panes.add(pane)
            unknown_reason = unknown_reason or "target-identity-changed"
            continue
        if row.get("session_id") and census_sid and row["session_id"] != census_sid:
            unknown_panes.add(pane)
            unknown_reason = unknown_reason or "target-identity-changed"
            continue
        expected_sid = census_sid or row.get("session_id")
        observed = _retire_target(pane)
        candidate, reason = _prompt_current_candidate(
            pane, expected_harness=harness, expected_sid=expected_sid,
            require_process_session=True, target_observation=observed)
        if candidate is None:
            unknown_panes.add(pane)
            unknown_reason = unknown_reason or reason or "target-identity-unverified"
            continue
        names = {value for value in (row.get("name"), candidate.get("name"),
                 item.get("display_agent"), item.get("title"),
                 item.get("terminal_title_stripped"))
                 if isinstance(value, str) and value and value != "-"}
        try:
            registry_name = _from_name(harness, candidate["session_id"])
        except Exception:
            registry_name = None
        if isinstance(registry_name, str) and registry_name and registry_name != "-":
            names.add(registry_name)
        if not names:
            unknown_panes.add(pane)
            unknown_reason = unknown_reason or "target-name-observation-unavailable"
            continue
        row_cwd = item.get("cwd") or item.get("foreground_cwd")
        rows.append({**candidate, "names": names, "cwd": row_cwd,
                     "endpoint": endpoint, "census": item})

    for pane, listed in listed_by_pane.items():
        if pane not in census_by_pane:
            unknown_panes.add(pane)
            unknown_reason = unknown_reason or "pane-census-inconsistent"
            continue
        if census_by_pane[pane].get("agent") in (None, ""):
            unknown_panes.add(pane)
            unknown_reason = unknown_reason or "pane-census-inconsistent"

    rows_by_pane = {row["pane"]: row for row in rows}
    for pane in unknown_panes:
        if pane not in rows_by_pane:
            rows_by_pane[pane] = {"pane": pane, "unknown": True}
    return list(rows_by_pane.values()), unknown_reason, sorted(unknown_panes)


def _resolve_prompt_target(target):
    """Resolve a prompt address once to a pane and exact current session identity.

    Names are matched against the complete selected-endpoint inventory, Fleet tags use
    the shared tag definition against current process identities, and pane IDs retain
    their direct meaning. Every success is pinned to a pane for all later guards/carriers.
    """
    if not isinstance(target, str) or not target:
        return None, "target-not-found", []
    tag_match = _PROMPT_TAG_ADDRESS.fullmatch(target)
    is_tag = tag_match is not None
    is_pane = not is_tag and ":" in target and not any(char.isspace() for char in target)

    if is_pane:
        candidate, reason = _prompt_current_candidate(target)
        return (candidate, None, [target]) if candidate else (None, reason, [target])

    rows, snapshot_error, unknown_panes = _prompt_candidate_snapshot()
    if rows is None:
        return None, snapshot_error or "target-observation-unavailable", unknown_panes
    wanted_tag = tag_match.group(1).lower() if is_tag else None
    matches = {}
    uncertain = set(unknown_panes)
    uncertainty_reason = snapshot_error
    for row in rows:
        if row.get("unknown"):
            continue
        if target in row["names"]:
            matches[row["pane"]] = row
        if wanted_tag is not None:
            current_tag, tag_error = _prompt_fleet_tag(
                row["harness"], row["session_id"], pane=row["pane"], cwd=row.get("cwd"))
            if tag_error or current_tag is None:
                uncertain.add(row["pane"])
                uncertainty_reason = uncertainty_reason or tag_error or "fleet-tag-observation-unavailable"
            elif current_tag == wanted_tag:
                matches[row["pane"]] = row

    candidate_panes = sorted(set(matches) | uncertain)
    if uncertain:
        return None, uncertainty_reason or "target-observation-unavailable", candidate_panes
    if not matches:
        return None, "fleet-tag-not-found" if is_tag else "target-name-not-found", []
    if len(matches) > 1:
        return None, "fleet-tag-ambiguous" if is_tag else "target-name-ambiguous", candidate_panes
    row = next(iter(matches.values()))
    # Re-read the pinned pane immediately before admission. Historical names and
    # tags select a pane but never stand in for its current native identity.
    candidate, reason = _prompt_current_candidate(
        row["pane"], expected_harness=row["harness"], expected_sid=row["session_id"],
        require_process_session=True)
    if not candidate:
        return None, reason, candidate_panes
    if wanted_tag is not None:
        current_tag, tag_error = _prompt_fleet_tag(
            candidate["harness"], candidate["session_id"], pane=candidate["pane"], cwd=row.get("cwd"))
        if tag_error or current_tag != wanted_tag:
            return None, tag_error or "target-identity-changed", candidate_panes
    return candidate, None, candidate_panes


def _prompt_failure_ledger(target, reason, candidate_panes, *, kind="steer", ref=(),
                           from_identity=None, from_name=_UNSET_NAME):
    """Record a bounded resolution failure without persisting or publishing its body."""
    target_text = target if isinstance(target, str) else str(target or "")
    candidates = sorted({pane for pane in candidate_panes
                         if isinstance(pane, str) and pane})
    shown, omitted = [], 0
    for pane in candidates:
        value = pane[:96]
        preview = shown + [value]
        receipt = json.dumps({"reason": str(reason)[:96], "target": target_text[:256],
                              "candidate_panes": preview,
                              "candidate_panes_omitted": omitted + len(candidates) - len(preview)},
                             ensure_ascii=False, separators=(",", ":"))
        if len(receipt.encode("utf-8")) > 1400 or len(shown) >= 24:
            omitted += len(candidates) - len(shown)
            break
        shown.append(value)
    receipt = json.dumps({"reason": str(reason)[:96], "target": target_text[:256],
                          "candidate_panes": shown,
                          "candidate_panes_omitted": omitted},
                         ensure_ascii=False, separators=(",", ":"))
    if from_identity is None:
        sid, harness = _current_session_identity()
        from_identity = (sid, harness, _project_of(os.getcwd()))
    if from_name is _UNSET_NAME:
        from_name = _from_name(from_identity[1], from_identity[0])
    _record(to_harness="unknown", to_name=target_text[:256], kind=kind,
            receipt="prompt-resolution:" + receipt, status="unknown",
            from_identity=from_identity, from_name=from_name, ref=ref, surface="herdr")


def _prompt_message_kind(body):
    first = body.strip().splitlines()[0] if body.strip() else ""
    for prefix, value in (("[steer]", "steer"), ("[handoff]", "handoff"),
                          ("[gate]", "gate-relay")):
        if first.startswith(prefix):
            return value
    return "steer"


def _record(*, to_harness, to_name, kind, ref=None, summary_text=None,
            receipt=None, status="sent", from_identity=None, to_session_id=None,
            to_pane=None, transfer_ref=None, from_name=_UNSET_NAME, surface="herdr",
            producer_binding=None):
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
        producer_binding=producer_binding,
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


def _mark_observed(to_harness, to_session_id, to_name, from_identity=None, *, pane=None):
    """F-100c-2 — the ONE place `source=watch` steward evidence is written: after
    herdr answered about a real target (`wait` returned a typed state other than
    agent-not-found, or `watch` armed its watcher). The `kind=watch` ledger row itself
    is only the message; `record` never marks (review round 1, #1/#2)."""
    if from_identity is not None:
        from_sid, from_harness = from_identity[0], from_identity[1]
    else:
        from_sid, from_harness = _current_session_identity()
    sid = peer_message.usable_session_id(to_session_id)
    to = {"harness": to_harness if to_harness and to_harness != "-" else "unknown",
          "session_id": sid,
          "name": to_name if isinstance(to_name, str) and to_name and to_name != "-" else None,
          "pane": pane if isinstance(pane, str) and pane and pane != "-" else None}
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
        resolved_name = (agent["name"] if agent["name"] != "-" else
                         _t_name if _t_name and _t_name != "-" else target)
        _mark_observed(agent["harness"] if agent["harness"] != "-" else t_harness,
                       agent["session_id"] if agent["session_id"] != "-" else t_sid,
                       resolved_name, pane=agent["pane"])
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
        block = _blocking_screen(kind, _read_screen(pane))
        if block:
            return block
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
        block = _blocking_screen("opencode", _read_screen(pane))
        if block:
            return block
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


def _seat_successor_lock(pane, *, wait=None):
    import session_tidy
    return session_tidy.seat_lock("peer-successor-" + _seat_successor_path(pane).stem, wait=wait)


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
                                       "server": _HERDR_SESSION or "default",
                                       "successor": {"harness": kind, "session_id": session_id or ""},
                                       "at": time.time()}, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
    except OSError:
        pass


def _seat_handover(ident, own_sid, own_harness, *, requester=None, accepted_at=None, recheck=None, wait=None):
    """After `retire` proved the predecessor exited: when this session runs in the pane started
    beside the predecessor, its routes become this session's, across harnesses
    (`dispatch_seat_handover.record_retire_handover`). Returns the receipt value, or None when
    this is not a seat change (the receipt then stays as it was); it never fails the retire."""
    pane = requester.get("pane") if requester is not None else _caller_pane()
    if not pane:
        return None
    try:
        with _seat_successor_lock(pane, wait=wait):
            return _seat_handover_locked(ident, own_sid, own_harness, pane,
                                         requester=requester, accepted_at=accepted_at, recheck=recheck)
    except OSError:
        return "error"


def _source_before_acceptance(source, accepted_at, *times):
    values = (accepted_at, source.get("mtime"), source.get("ctime"), *times)
    return (all(isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v) and v > 0 for v in values)
            and all(v <= accepted_at for v in values[1:]))


def _seat_handover_locked(ident, own_sid, own_harness, pane, *, requester=None, accepted_at=None,
                          recheck=None):
    from opencode_session_runtime import _read_native_record, OpenCodeTransportError
    path = _seat_successor_path(pane)
    try:
        record = _read_native_record(path)
        if record is None:
            return None
        mark = json.loads(record["raw"])
    except (OSError, ValueError, OpenCodeTransportError):
        return "skipped:successor-unverified"
    if (not isinstance(mark, dict) or type(mark.get("schema")) is not int or mark["schema"] != 1
            or mark.get("pane") != pane or mark.get("beside") != ident["pane"]):
        return None
    server = requester.get("server", "default") if requester is not None else _HERDR_SESSION or "default"
    if mark.get("server", "default") != server:
        return "skipped:successor-unverified"
    successor = mark.get("successor")
    if not isinstance(successor, dict):
        return "skipped:successor-unverified"
    recorded = successor.get("session_id")
    if (not isinstance(own_sid, str) or not own_sid or own_sid == "-"
            or not isinstance(recorded, str) or ident.get("session_id") in {None, "", "-"}
            or own_harness not in {"claude", "codex", "opencode"}
            or successor.get("harness") != own_harness or (recorded and recorded != own_sid)):
        return "skipped:successor-unverified"
    if requester is not None:
        if (requester.get("pane") != pane or requester.get("server") != server
                or requester.get("harness") != own_harness or requester.get("session_id") != own_sid
                or ident.get("server", "default") != server
                or not _source_before_acceptance(record["source"], accepted_at, mark.get("at"))):
            return "skipped:successor-unverified"
    try:
        import dispatch_seat_handover as handover
        booked = {}
        if requester is not None:
            import session_tidy
            booked["successor_seat"] = session_tidy._pane_seat_of(pane)
        if _read_native_record(path) != record or (recheck is not None and not recheck()):
            return "skipped:predecessor-unverified"
        row = handover.record_retire_handover(
            ident["session_id"], ident["harness"], own_sid, own_harness, env=os.environ, **booked)
    except Exception:  # noqa: BLE001 - the retire itself already succeeded
        return "error"
    try:
        if row is not None and _read_native_record(path) == record:
            path.unlink()
    except (OSError, OpenCodeTransportError):
        pass
    return str(len(row["bindings"])) if row else "none"


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


def _retire_handover(duty, *, wait=None):
    """Consume only the accepted retirement's original succession, on any observer."""
    intent = duty.get("intent") or {}
    requester = intent.get("requester") or {}
    try:
        resolved = _retire_predecessor(duty)
    except Exception:  # noqa: BLE001 - the retire itself already succeeded
        return "error"
    if resolved is None:
        return None
    identity, evidence = resolved
    return _seat_handover(identity, requester.get("session_id"),
                          requester.get("harness"), requester=requester,
                          accepted_at=duty.get("accepted_at"),
                          recheck=(lambda: _retire_predecessor(duty) == resolved) if evidence else None,
                          wait=wait)


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
                # Not this start. Name the miss so a refused start receipt explains
                # why the pane's agent was not adopted (herdr may report no cwd).
                args.observed_miss = "cwd-mismatch" if agent.get("cwd") else "cwd-empty"
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


def _placement_result(*argv):
    proc = subprocess.run(_herdr_argv(*argv), capture_output=True, text=True,
                          timeout=_herdr_get_timeout())
    payload = json.loads(proc.stdout or "")
    result = payload.get("result") if isinstance(payload, dict) else None
    if proc.returncode or not isinstance(result, dict) or payload.get("error"):
        return {}
    return result


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


def _herdr_surface():
    """The selected Herdr session as the shared pane surface."""
    return terminal_surface.HerdrSurface(
        lambda argv: subprocess.run(argv, capture_output=True, text=True,
                                    timeout=_herdr_get_timeout()),
        _HERDR_SESSION)


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
    with terminal_surface.workspace_lock(_herdr_surface(), workspace):
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
        # The shared pane opener places and creates the pane; the agent start below
        # still runs inside the workspace claim cmd_start holds.
        opened = terminal_surface.open_pane(
            near=terminal_surface.Near(_herdr_surface(), pane=None if workspace else args.beside,
                                       workspace=workspace),
            cwd=pane_cwd)
        if opened.pane is None:
            reason = opened.notes[0] if opened.notes else "pane-layout-unavailable"
            print(f"started=false reason={reason} agent={args.kind} "
                  f"name={args.name} pane=- beside={args.beside}")
            return 1
        args.pane = opened.pane
        if opened.placement != "reuse":
            created_pane = True
            created_shell = _start_shell_identity(args.pane)
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
    project_arg = [pane_cwd] if args.kind == "opencode" and pane_cwd else []
    # Shared-daemon defaults and caller-supplied connection flags pass through unchanged.
    # Identity is resolved once from the started foreground process below.
    full_agent_args = prefix + cwd_flag + project_arg + agent_args

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
        if isinstance(agent_block, dict) and args.kind != "codex":
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
            if args.kind != "codex":
                started_sid = peer_message.usable_session_id(
                    (observed.get("agent_session") or {}).get("value"))
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
    # Herdr's start receipt is not process-identity proof for Codex. A single live
    # resolver read may supply the SID; unknown leaves the successful launch intact.
    if started and args.kind == "codex":
        try:
            started_sid = peer_message.usable_session_id(_process_session(args.pane, "codex"))
        except Exception:
            started_sid = None
    # A successful agent block or this pane's observed matching runtime proves the
    # start. An unresolved refusal or unparsable answer grants no role marker.
    if started and isinstance(agent_block, dict):
        _mark_started(args, started_sid, mode)
    else:
        _record(to_harness=args.kind, to_name=args.name, kind="steer",
                summary_text=f"[start] {args.name} kind={args.kind} mode={mode}",
                to_session_id=started_sid)
    # `session_id=-` means the shared process-identity resolver could not prove the
    # started Codex TUI's current thread; it does not change the successful start result.
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
        + (f" reason={failure_reason} herdr_rc={proc.returncode}" if failure_reason else "")
        + (f" observed={args.observed_miss}" if not started and getattr(args, "observed_miss", None) else "")
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


def _retire_pane_info(pane, timeout=5, *, server=None):
    try:
        argv = (_herdr_argv("pane", "process-info", "--pane", pane) if server is None else
                ["herdr"] + ([] if server == "default" else ["--session", server])
                + ["pane", "process-info", "--pane", pane])
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env=_herdr_env_for_server(server) if server is not None else None)
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


def _retire_foreground(pane, harness, *, strict=True, info=None):
    info = _retire_pane_info(pane) if info is None else info
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


def _retire_target(target, *, server=None):
    try:
        payload = (_run_herdr_get(target) if server is None
                   else _run_herdr_get(target, server=server))
        state, ident, rc, reason = _interpret_payload(payload, target)
        if any(not isinstance(value, str) or not value for value in ident.values()):
            raise ValueError("malformed target")
        return state, ident, rc, reason
    except (AttributeError, TypeError, ValueError):
        return ("herdr-unavailable", {"harness": "-", "session_id": "-",
                "name": target, "pane": "-"}, 4, "herdr-protocol-error")


def _native_opencode_turn(ident, *, foreground=None):
    """Read a fresh OpenCode status event bound to this exact foreground birth."""
    if (not isinstance(ident, dict) or ident.get("harness") != "opencode"
            or any(not isinstance(ident.get(key), str) or ident[key] in {"", "-"}
                   for key in ("pane", "session_id"))):
        return None
    foreground = foreground or _retire_foreground(ident["pane"], "opencode", strict=False)
    if not isinstance(foreground, dict):
        return None
    return herdr_observation.read_native_status(
        ident.get("server") or _HERDR_SESSION or "default", ident["pane"],
        "opencode", ident["session_id"], foreground["pid"], foreground["start"],
    )


def _effective_retire_state(state, ident):
    native = _native_opencode_turn(ident)
    if isinstance(native, dict) and native.get("status") in {"idle", "working", "blocked"}:
        return native["status"], "opencode-session-status"
    return state, "herdr-corroboration"


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


def _retire_followup_fingerprint(duty, identity):
    """Observe the cheap source generations that can wake a held retire retry."""
    foreground = _retire_booked_foreground(duty) or {}
    pid = foreground.get("pid")
    process = _retire_process_record(pid) if isinstance(pid, int) else None
    process_fingerprint = (process.get("start"), process.get("group"),
                           tuple(process.get("argv") or ())) if isinstance(process, dict) else None
    process_start = _proc_start_ticks(pid) if isinstance(pid, int) else None
    pane = identity.get("pane")
    successor_stamp = None
    if isinstance(pane, str) and pane:
        try:
            info = _seat_successor_path(pane).lstat()
            successor_stamp = (info.st_dev, info.st_ino, info.st_size,
                               info.st_mtime_ns, info.st_ctime_ns)
        except OSError:
            pass
    route_stamp = None
    native = None
    if identity.get("harness") == "opencode":
        try:
            import dispatch_seat_handover as handover
            jobs = Path(handover._default_jobs())
            route_stamp = dispatch_batch_obligations._registry_stamp(jobs)
        except (OSError, ValueError, RuntimeError):
            pass
        if _handoff_lifetime_valid(foreground):
            try:
                record = _retained_tui_selection(foreground)
                if record is not None:
                    value = record.get("value") or {}
                    source = record.get("source") or {}
                    native = (value.get("sessionID"), value.get("writtenAt"),
                              tuple(source.get("generation") or ()))
            except Exception:
                pass
    return (_retire_subject_state(duty), process_fingerprint, process_start,
            successor_stamp, route_stamp, native)


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


def _complete_gone_retire(store, duty):
    handover = _retire_handover(duty)
    phase = (duty.get("observation") or {}).get("phase", "waiting")
    settled = store.update(duty["id"], state="complete", result="target-already-gone",
                           delivery="completed", cleanup="complete", expected_phases={phase},
                           observation={"phase": "complete", "reason": "target-already-gone"})
    if settled.get("state") == "complete":
        ident = (duty.get("intent") or {}).get("identity") or {}
        _mark_completed_retire_followup(store, settled, ident,
                                        "target-already-gone", handover,
                                        defer_if_missing=True)
        print(f"retired=true reason=target-already-gone agent={ident.get('harness', '-')} "
              f"name={ident.get('name', '-')} pane={ident.get('pane', '-')}"
              + (f" handover={handover}" if handover else ""))
        return True
    return False


def _mark_completed_retire_followup(store, duty, identity, reason, handover, *,
                                    defer_if_missing=False):
    unresolved = (handover == "error" or (isinstance(handover, str)
                  and handover.startswith("skipped:")))
    if handover is None:
        pane = (identity or {}).get("pane")
        if not pane:
            pane = ((duty.get("intent") or {}).get("requester") or {}).get("pane")
        try:
            if defer_if_missing or (pane and _seat_successor_path(pane).exists()):
                unresolved = True
        except OSError:
            unresolved = True
    try:
        _request_watch_terminal(identity, reason)
    except Exception:
        unresolved = True
    observation = dict(duty.get("observation") or {})
    observation.update(terminal_followup="pending" if unresolved else "complete",
                       terminal_followup_result=str(handover or "none"))
    try:
        return store.update(duty["id"], observation=observation)
    except (OSError, ValueError, RuntimeError):
        return duty


def _handoff_lifetime_valid(foreground):
    if not isinstance(foreground, dict):
        return False
    pid, start = foreground.get("pid"), foreground.get("start")
    return (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            and not isinstance(start, bool) and str(start).isdigit() and int(start) > 0)


def reserve_handoff_retire(target, exact, requester):
    """Reserve in the existing store; never execute exit from acceptance."""
    store = peer_obligations.ObligationStore()
    matches = [d for d in store.list(states=peer_obligations._PENDING_STATES | {"complete"},
                                    kinds={"retire"})
        if all((d["intent"].get("identity") or {}).get(k) == exact.get(k)
                for k in ("server", "pane", "harness", "session_id"))]
    if matches:
        if len(matches) != 1 or matches[0]["intent"].get("requester") != requester:
            raise ValueError("handoff-retire-requester-conflict")
        duty = matches[0]
    else:
        if not _handoff_lifetime_valid(exact.get("foreground")):
            raise ValueError("handoff-predecessor-lifetime-unverified")
        store, duty = _retire_request(target, exact, requester=requester)
    peer_obligations.ensure_normal_runner()
    return duty["id"]


def _retire_request(target, exact, *, requester=None):
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
        if requester is None:
            own_sid, own_harness = _current_session_identity()
            requester = {"session_id": own_sid, "harness": own_harness,
                         "pane": _caller_pane(), "server": _HERDR_SESSION or "default"}
        intent = {"target": target, "requester": requester}
        if retry_of:
            intent["retry_of"] = retry_of
        duty = store.create(duty_id, "retire", exact, intent)
    peer_obligations.ensure_normal_runner()
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
        _mark_completed_retire_followup(
            store, store.get(duty["id"]) or duty, {**ident, "server": server},
            "completed-retire", handover)
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


def _resume_retire_obligation(duty, store, *, read_context=None,
                              persist_followup=False):
    global _HERDR_SESSION
    intent = duty.get("intent") or {}
    ident = dict((duty.get("intent") or {}).get("identity") or {})
    target = intent.get("target") or ""
    observation = duty.get("observation") or {}
    phase = observation.get("phase", "waiting")
    if duty.get("state") == "complete":
        if duty.get("result") not in {"normal-exit", "target-already-gone"}:
            return
        followup_key = (str(store.root.resolve(strict=False)), duty.get("id", ""))
        if observation.get("terminal_followup") == "complete":
            _RETIRE_FOLLOWUP_COMPLETE.add(followup_key)
            return
        source_fingerprint = _retire_followup_fingerprint(duty, ident)
        prior = _RETIRE_FOLLOWUP_RETRIES.get(followup_key)
        if prior is not None and prior[2] != source_fingerprint:
            _RETIRE_FOLLOWUP_RETRIES.pop(followup_key, None)
            prior = None
        if prior is not None and time.monotonic() < prior[0]:
            return
        attempts = prior[1] if prior is not None else 0
        try:
            # A busy successor belongs to its existing owner. Keep unresolved
            # handover evidence retryable, with an item-local bounded backoff.
            handover = _retire_handover(duty, wait=0)
            if handover == "error" or (isinstance(handover, str)
                                        and handover.startswith("skipped:")):
                raise RuntimeError("retire-handover-unresolved")
            if handover is None:
                pane = ident.get("pane") or ((intent.get("requester") or {}).get("pane"))
                if pane:
                    try:
                        _seat_successor_path(pane).lstat()
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        raise RuntimeError("retire-successor-unavailable") from exc
                    else:
                        raise RuntimeError("retire-successor-unresolved")
            _request_watch_terminal(ident, "completed-retire")
            _RETIRE_FOLLOWUP_COMPLETE.add(followup_key)
            _RETIRE_FOLLOWUP_RETRIES.pop(followup_key, None)
            if persist_followup:
                current = store.get(duty["id"]) or duty
                current_observation = dict(current.get("observation") or {})
                current_observation.update(terminal_followup="complete",
                                          terminal_followup_result=str(handover or "none"))
                store.update(duty["id"], observation=current_observation)
            if len(_RETIRE_FOLLOWUP_COMPLETE) > 4096:
                _RETIRE_FOLLOWUP_COMPLETE.clear()
        except Exception:
            attempts += 1
            delay = min(60, 2 ** min(attempts - 1, 6))
            _RETIRE_FOLLOWUP_RETRIES[followup_key] = (
                time.monotonic() + delay, attempts, source_fingerprint)
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
                                    expected_sid=observed["session_id"],
                                    read_context=read_context)
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
        if retired and duty:
            _mark_completed_retire_followup(
                store, store.get(duty["id"]) or duty,
                {"server": _HERDR_SESSION or "default", "pane": ident.get("pane"),
                 "harness": ident.get("harness"), "session_id": ident.get("session_id"),
                 "foreground": duty.get("intent", {}).get("identity", {}).get("foreground")},
                "completed-retire", handover)
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
        matches = [row for row in store.list(states=peer_obligations._PENDING_STATES | {"complete"},
                                             kinds={"retire"})
                   if row["intent"].get("target") == target
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
    retire_state, state_source = _effective_retire_state(state, ident)
    if reason or retire_state not in {"idle", "done"}:
        return finish(reason or f"agent-{retire_state}-{state_source}", pending=True)
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
    state2, _state2_source = _effective_retire_state(state2, ident2)
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
    event_transport = None

    def settle_shell_returned(info):
        nonlocal duty
        if info is None or not _retire_shell_returned(info, identity):
            return None
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

    try:
        socket_path = herdr_observation.socket_path_for_endpoint(_HERDR_SESSION or "default")
        if socket_path:
            event_transport = herdr_observation.ObservationTransport(
                _HERDR_SESSION or "default", socket_path,
                timeout=min(0.15, _HERDR_GET_TIMEOUT_SECONDS))
        initial = event_transport.observe() if event_transport is not None else None
        if initial is not None:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                event = event_transport.wait_for_event(
                    pane, remaining,
                    event_types={"pane.closed", "pane.exited", "pane.updated",
                                 "pane.agent_detected", "pane.agent_status_changed",
                                 "pane_closed", "pane_exited", "pane_updated",
                                 "pane_agent_detected", "pane_agent_status_changed"},
                )
                if event is None:
                    if not event_transport._state.complete:
                        event_transport.observe()
                    break
                envelope_data = event.get("data") or {}
                event_name = event.get("event") or envelope_data.get("type")
                if event_name in {"pane.closed", "pane.exited", "pane_closed", "pane_exited"}:
                    current = event_transport.resnapshot()
                    if (current is None or not current.complete
                            or any(row.get("pane_id") == pane for row in current.panes)):
                        continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                info = _retire_pane_info(pane, timeout=max(0.1, min(2.0, remaining)))
                settled = settle_shell_returned(info)
                if settled is not None:
                    return settled
        else:
            # Keep the existing event-driven agent wait as the bounded
            # fallback; its status is only a wake signal, never shell-return
            # proof. One final pane/kernel observation decides the result.
            # For OpenCode, the exact native idle state that authorized this
            # exit can satisfy `idle` before the foreground process has left.
            # Wait for terminal `done`; the exact PID/birth check below still
            # decides whether the normal exit completed.
            wait_states = ["done"] if harness == "opencode" else ["idle", "done"]
            _run_herdr_wait(target, wait_states, int(wait_seconds * 1000))
    finally:
        if event_transport is not None:
            event_transport.close()

    final_info = _retire_pane_info(pane, timeout=2.0)
    settled = settle_shell_returned(final_info)
    if settled is not None:
        return settled
    if _retire_subject_state(duty) == "gone" and _complete_gone_retire(store, duty):
        return 0
    if final_info is None:
        return finish("shell-return-unverified", pending=True)
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
    def observed_dialog():
        current = _read_screen(target)
        current_tasks = _retire_background_dialog_lines(current)
        if current_tasks is not None:
            return current_tasks, None
        return None, _blocking_screen(harness, current)

    current_tasks, screen_block = observed_dialog()
    if current_tasks is None:
        return finish(screen_block or "agent-still-running", pending=True)
    try:
        subprocess.run(_herdr_argv("pane", "send-text", pane, "1"),
                       capture_output=True, text=True, timeout=5)
        current_tasks, screen_block = observed_dialog()
        if current_tasks is None:
            return finish(screen_block or "agent-still-running", pending=True)
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
    current_tasks, screen_block = observed_dialog()
    if current_tasks is None:
        return finish(screen_block or "agent-still-running", pending=True)
    try:
        subprocess.run(_herdr_argv("pane", "send-text", pane, "3"),
                       capture_output=True, text=True, timeout=5)
        current_tasks, screen_block = observed_dialog()
        if current_tasks is None:
            return finish(screen_block or "agent-still-running", pending=True)
        subprocess.run(_herdr_argv("pane", "send-keys", pane, "enter"),
                       capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return finish("agent-still-running", pending=True)
    return finish("retire-declined-background-work" + (stopped or ""), pending=True)


# ---------------------------------------------------------------------------
# SD-122 (10) detached steward watch: watch / join / status / rearm / ack
# ---------------------------------------------------------------------------

_WATCH_SCHEMA = 1
_WATCH_STATES = _AGENT_STATES + (
    "timeout", "agent-not-found", "herdr-unavailable", "pane-exited",
    "steward-exited", "superseded",
)
_JOIN_EXIT = {"timeout": 3, "agent-not-found": 2, "herdr-unavailable": 4,
              "pane-exited": 2, "steward-exited": 0, "superseded": 0}
_WATCH_WAKE_FD = None
_WATCH_TRANSITION_LOCK_BUDGET_SEC = 0.2
_WATCH_RECEIPT_CONTENDED = "contended"
_WATCH_RECEIPT_PENDING = "pending"
_WATCH_RECEIPT_EXPIRED = "watch-deadline"


class _WatchTransitionLockBusy(RuntimeError):
    """The shared receipt lock budget expired before both locks were held."""


class _WatchDeadlineExpired(RuntimeError):
    """The watch's original monotonic deadline expired at the receipt edge."""


def _watch_wakeup_signal(_signum, _frame):
    fd = _WATCH_WAKE_FD
    if fd is not None:
        with contextlib.suppress(OSError):
            os.write(fd, b"w")


def _wake_watch_observer(arm):
    if not isinstance(arm, dict):
        return False
    try:
        protocol = int(arm.get("observer_protocol", 0) or 0)
    except (TypeError, ValueError):
        return False
    if protocol < 2:
        return False
    watcher = (arm or {}).get("watcher") or {}
    pid, start = watcher.get("pid"), watcher.get("pid_start")
    if not _pid_identity_ok(pid, start):
        return False
    try:
        os.kill(int(pid), signal.SIGUSR1)
        return True
    except (OSError, ValueError):
        return False


def _wake_current_watch_observer(watch_root, watch_id, identity, *, transition=None,
                                 readiness_root=None):
    """Wake only the observer still named by this watch's current arm."""
    if not isinstance(identity, dict):
        return False
    paths = _watch_paths(watch_id, watch_root)
    if paths.receipt.exists():
        return False
    arm = _read_json(paths.arm)
    if (not isinstance(arm, dict) or arm.get("watch_id") != watch_id
            or not _watch_readiness_identity_matches_arm(
                identity, arm, identity.get("native_state"))):
        return False
    watcher = arm.get("watcher") or {}
    if not _pid_identity_ok(watcher.get("pid"), watcher.get("pid_start")):
        return False
    if transition is not None:
        if readiness_root is None:
            return False
        observation = _read_json(paths.observation) or {}
        current = observation.get("readiness_transition")
        consumer = transition.get("consumer") if isinstance(transition, dict) else None
        token = transition.get("token") if isinstance(transition, dict) else None
        if (not isinstance(current, dict) or not isinstance(consumer, dict)
                or transition.get("identity") != identity
                or current.get("token") != token
                or current.get("identity") != identity
                or current.get("consumer") != consumer
                or consumer != watcher
                or not shared_readiness.consumer_is_current({"consumer": consumer})
                or shared_readiness.watch_transition_result(
                    readiness_root, watch_id, identity, token) is None):
            return False
    return _wake_watch_observer(arm)


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


def _prepare_watch_notice(watch_id, arm, receipt):
    """Queue one stable exact-ref notice under the original steward identity."""
    steward = (arm or {}).get("steward") or {}
    steward_seat = (arm or {}).get("steward_seat") or {}
    harness, sid = steward.get("harness"), steward.get("session_id")
    if (harness not in {"claude", "codex", "opencode"}
            or not peer_message.usable_session_id(sid)
            or steward_seat.get("kind") not in {"pane", "native"}
            or not steward_seat.get("key")):
        return None
    agent = (receipt or {}).get("agent") or (arm or {}).get("agent") or {}
    target = agent.get("name") or agent.get("session_id") or (arm or {}).get("target") or "unknown"
    body = (f"[peer-steward] watch_id={watch_id} state={(receipt or {}).get('state', 'unknown')} "
            f"target={target}; exact watch receipt is available")
    sender = {"harness": harness, "session_id": sid}
    if steward.get("project"):
        sender["project"] = steward["project"]
    recipient = {"harness": harness, "session_id": sid}
    try:
        _text, transfer_ref = peer_message.prepare_peer_message(
            body, sender, recipient, defer=True, refs=[f"peer-watch:{watch_id}"],
            receipt=f"peer-steward-watch:{watch_id}")
        current = _read_json(_watch_paths(watch_id).arm) or dict(arm or {})
        if current.get("notice_transfer_ref") != transfer_ref:
            _write_json_atomic(_watch_paths(watch_id).arm,
                               {**current, "notice_transfer_ref": transfer_ref})
        return transfer_ref
    except Exception:  # noqa: BLE001 - status/reconnect remains the ordinary fallback
        return None


def _write_terminal_watch_receipt(watch_id, arm, reason, *, watcher=None):
    """Settle one exact watch after its observer has the watch lock."""
    paths = _watch_paths(watch_id)
    if paths.receipt.exists():
        with contextlib.suppress(OSError, ValueError):
            shared_readiness.clear_watch_transition(
                peer_obligations.ObligationStore().root, watch_id)
        return _read_json(paths.receipt)
    agent = dict((arm or {}).get("agent") or {})
    terminal_state = {
        "watch-deadline": "timeout", "wait-timeout": "timeout",
        "steward-exited": "steward-exited", "steward-replaced": "superseded",
        "herdr-unavailable": "herdr-unavailable",
    }.get(reason, "pane-exited")
    receipt = {
        "schema_version": _WATCH_SCHEMA, "watch_id": watch_id,
        "target": (arm or {}).get("target", "-"),
        "server": (arm or {}).get("server") or "default",
        "steward": (arm or {}).get("steward") or {},
        "armed_ts": (arm or {}).get("armed_ts", ""), "done_ts": _utc_now(),
        "state": terminal_state, "agent": agent,
        "herdr_exit": _LAST_HERDR_EXIT if _LAST_HERDR_EXIT is not None else -1,
        "watcher": watcher or {"pid": os.getpid(), "pid_start": process_start_ticks(os.getpid()) or ""},
        "rearmed_from": (arm or {}).get("rearmed_from"),
        "refs": list((arm or {}).get("refs") or []),
        "observer_generation": (arm or {}).get("observer_generation", 1),
        "reason": reason,
    }
    _write_json_atomic(paths.receipt, receipt)
    with contextlib.suppress(OSError, ValueError):
        shared_readiness.clear_watch_transition(
            peer_obligations.ObligationStore().root, watch_id)
    try:
        paths.observation.unlink()
    except FileNotFoundError:
        pass
    _prepare_watch_notice(watch_id, arm, receipt)
    return receipt


def _terminal_watch_request(paths, expected, server):
    observation = _read_json(paths.observation)
    if (not isinstance(observation, dict) or observation.get("state") != "terminal_pending"
            or (observation.get("server") or "default") != server):
        return None
    requested = observation.get("agent") or {}
    if any(requested.get(key) != expected.get(key)
           for key in ("harness", "session_id", "pane")):
        return None
    return str(observation.get("reason") or "completed-retire")


def _watch_terminal_from_view(view, expected, server, transport=None):
    """Return terminal evidence only for this endpoint and armed pane lifetime."""
    if (view is None or view.complete is not True
            or herdr_observation.endpoint_name(view.endpoint) !=
               herdr_observation.endpoint_name(server)):
        return None
    pane_id = expected.get("pane")
    if not isinstance(pane_id, str) or not pane_id or pane_id == "-":
        return None
    terminal = view.terminal.get(pane_id)
    if terminal in {"pane-closed", "pane-exited"}:
        if transport is None:
            return terminal
        # Event envelopes carry pane IDs but no public lifetime sequence. A
        # fresh snapshot on the same subscription prevents a delayed terminal
        # event from retiring a new occupant that reused the public pane ID.
        view = transport.resnapshot()
        if (view is None or view.complete is not True
                or herdr_observation.endpoint_name(view.endpoint) !=
                   herdr_observation.endpoint_name(server)):
            return None
        if not any(row.get("pane_id") == pane_id for row in view.panes):
            return terminal
    rows = [row for row in view.panes if row.get("pane_id") == pane_id]
    if not rows:
        return "pane-absent"
    if len(rows) != 1:
        return None
    pane = rows[0]
    harness = expected.get("harness")
    session_id = expected.get("session_id")
    current_harness = pane.get("agent")
    current_session = pane.get("agent_session") or {}
    if isinstance(current_session, dict):
        current_session = current_session.get("value")
    if (harness not in {None, "", "-"}
            and current_harness not in {None, "", harness}):
        return "session-replaced"
    if (session_id not in {None, "", "-"}
            and isinstance(current_session, str) and current_session
            and current_session != session_id):
        return "session-replaced"
    return None


def _watch_registered_work_pending(view, expected, server):
    """A fresh blocking fact can defer readiness, never authorize completion."""
    if (view is None or getattr(view, "complete", False) is not True
            or herdr_observation.endpoint_name(view.endpoint) !=
               herdr_observation.endpoint_name(server)):
        return False
    pane, harness, sid = (expected.get(key) for key in ("pane", "harness", "session_id"))
    if not pane or harness not in {"claude", "codex", "opencode"} or not sid or sid == "-":
        return False
    matches = [row for row in view.panes if row.get("pane_id") == pane]
    if len(matches) != 1:
        return False
    current_sid = matches[0].get("agent_session")
    if isinstance(current_sid, dict):
        current_sid = current_sid.get("value")
    if matches[0].get("agent") != harness or current_sid != sid:
        return False
    try:
        work, state = peer_obligations.bound_work_for_pane(
            pane, harness, sid, registry_append_only=True)
        if state != "observed" or not work:
            return False
        states = [peer_obligations.completion_readiness(item).state for item in work]
    except (OSError, ValueError, TypeError):
        # Uncertainty takes the ordinary complete readiness path below.
        return False
    return "unknown" not in states and "pending" in states


def _settle_terminal_watch_if_unobserved(watch_id, arm, root=None,
                                        reason_override=None):
    paths = _watch_paths(watch_id, root)
    if paths.receipt.exists():
        return True
    expected = dict((arm or {}).get("agent") or {})
    server = (arm or {}).get("server") or "default"
    reason = reason_override or _terminal_watch_request(paths, expected, server)
    if reason is None:
        return False
    fd = _open_lock(paths.lock)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _wake_watch_observer(arm)
            return True
        if paths.receipt.exists():
            return True
        _write_terminal_watch_receipt(watch_id, arm, reason)
        return True
    finally:
        os.close(fd)


def _settle_ended_seatless_recipient(watch_id, arm, root=None, *, census=None,
                                      lock_held=False):
    """Settle a seat-less Claude watch with native existence and no-holder proof."""
    paths = _watch_paths(watch_id, root)
    if paths.receipt.exists():
        return True
    if not isinstance(arm, dict) or arm.get("watch_id") != watch_id:
        return False
    seat = arm.get("steward_seat")
    if ("steward_seat" in arm and seat is not None):
        return False
    steward = arm.get("steward") or {}
    harness, sid = steward.get("harness"), steward.get("session_id")
    if harness != "claude" or not peer_message.usable_session_id(sid):
        return False
    try:
        from claude_process_identity import is_local_path
        if not is_local_path(paths.arm.parent):
            return False
    except (OSError, TypeError, ValueError):
        return False
    if census is None:
        from recipient_lifetime import RecipientCensus, recipient_state
        census = RecipientCensus()
    else:
        from recipient_lifetime import recipient_state
    if recipient_state(harness=harness, session_id=sid, census=census) != "ended":
        return False

    current = _read_json(paths.arm)
    current_steward = (current or {}).get("steward") or {}
    if (not isinstance(current, dict) or current.get("watch_id") != watch_id
            or ("steward_seat" in current and current.get("steward_seat") is not None)
            or current_steward.get("harness") != harness
            or current_steward.get("session_id") != sid
            or (current.get("server") or "default") != (arm.get("server") or "default")):
        return False
    expected = dict(current.get("agent") or {})
    server = current.get("server") or "default"
    _write_json_atomic(paths.observation, {
        "schema_version": _WATCH_SCHEMA, "watch_id": watch_id,
        "state": "terminal_pending", "reason": "steward-exited",
        "server": server,
        "agent": {key: expected.get(key, "-") for key in ("harness", "session_id", "pane")},
    })
    if lock_held:
        _write_terminal_watch_receipt(watch_id, current, "steward-exited")
        return True
    return _settle_terminal_watch_if_unobserved(
        watch_id, current, root, reason_override="steward-exited")


def _request_watch_terminal(identity, reason):
    """Ask matching observers to settle on their next bounded wake."""
    if not isinstance(identity, dict):
        return 0
    server = identity.get("server") or "default"
    pane, harness, sid = identity.get("pane"), identity.get("harness"), identity.get("session_id")
    if not all(isinstance(value, str) and value and value != "-"
               for value in (pane, harness, sid)):
        return 0
    root = _watch_root()
    count = 0
    for watch_id, arm in _watch_entries(root):
        if not isinstance(arm, dict) or _watch_paths(watch_id, root).receipt.exists():
            continue
        agent = arm.get("agent") or {}
        if ((arm.get("server") or "default") != server
                or any(agent.get(key) != value for key, value in (
                    ("pane", pane), ("harness", harness), ("session_id", sid)))):
            continue
        saved = arm.get("foreground_identity")
        foreground = identity.get("foreground")
        if (isinstance(saved, dict) and isinstance(foreground, dict)
                and (saved.get("pid"), saved.get("start")) !=
                    (foreground.get("pid"), foreground.get("start"))):
            continue
        paths = _watch_paths(watch_id, root)
        _write_json_atomic(paths.observation, {
            "schema_version": _WATCH_SCHEMA, "watch_id": watch_id,
            "state": "terminal_pending", "reason": reason, "server": server,
            "agent": {key: agent.get(key, "-") for key in ("harness", "session_id", "pane")},
            "observed_ts": _utc_now(),
        })
        _wake_watch_observer(arm)
        _settle_terminal_watch_if_unobserved(watch_id, arm, root)
        count += 1
    return count


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


def _logical_watch_claim_key(steward_sid, agent, until, server=None):
    """Key a watch by its resolved target while retaining the caller's raw claim.

    Pane identity is stable before a new native session exists. Once Herdr knows
    the session, include it so a reused pane cannot inherit the prior session's
    watch. The separate pane lock serializes that transition.
    """
    session_id = agent.get("session_id")
    bound = isinstance(session_id, str) and session_id not in {"", "-"}
    identity = [agent.get("harness"), agent.get("pane"),
                "session" if bound else "pane", session_id if bound else ""]
    raw = json.dumps([
        server or "default", steward_sid, identity, sorted(until or []),
    ], ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")
    return "logical-" + hashlib.sha256(raw).hexdigest()[:16]


def _watch_alias_lock_key(steward_sid, agent, until, server=None):
    """Serialize registrations that resolve to one pane, including SID binding."""
    raw = json.dumps([
        server or "default", steward_sid, agent.get("harness"), agent.get("pane"),
        sorted(until or []),
    ], ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")
    return "alias-" + hashlib.sha256(raw).hexdigest()[:16]


def _write_watch_claim(lock_fd, watch_id):
    try:
        os.ftruncate(lock_fd, 0)
        os.lseek(lock_fd, 0, os.SEEK_SET)
        os.write(lock_fd, watch_id.encode("ascii"))
        os.fsync(lock_fd)
    except OSError:
        pass


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _boot_id():
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        return value or None
    except OSError:
        return None


def _watch_deadline(timeout_ms, *, wall=None, monotonic=None, boot_id=None):
    """Capture one absolute budget; observer restarts never receive a fresh timeout."""
    if timeout_ms is None:
        return None
    requested = int(timeout_ms)
    duration = max(0, requested) / 1000.0
    now_wall = time.time() if wall is None else float(wall)
    now_mono = time.monotonic() if monotonic is None else float(monotonic)
    return {
        "requested_ms": requested,
        "wall_deadline": now_wall + duration,
        "monotonic_deadline": now_mono + duration,
        "boot_id": _boot_id() if boot_id is None else boot_id,
    }


def _watch_timeout_remaining_ms(arm, *, wall=None, monotonic=None, boot_id=None):
    """Return (remaining-ms, state); legacy incomplete deadlines stay unknown."""
    timeout = (arm or {}).get("timeout")
    if timeout is None:
        return None, "unbounded"
    try:
        requested = int(timeout)
    except (TypeError, ValueError):
        return None, "unknown"
    if requested <= 0:
        return 0, "expired"
    now_wall = time.time() if wall is None else float(wall)
    now_mono = time.monotonic() if monotonic is None else float(monotonic)
    current_boot = _boot_id() if boot_id is None else boot_id
    deadline = (arm or {}).get("deadline")
    if not isinstance(deadline, dict):
        armed_ts = (arm or {}).get("armed_ts")
        try:
            armed_wall = datetime.datetime.strptime(
                armed_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc).timestamp()
        except (TypeError, ValueError):
            return None, "unknown"
        deadline = {"wall_deadline": armed_wall + requested / 1000.0,
                    "requested_ms": requested, "boot_id": None}
    try:
        original = min(requested, max(0, int(deadline.get("requested_ms", requested))))
        if (deadline.get("boot_id") and current_boot
                and deadline.get("boot_id") == current_boot
                and isinstance(deadline.get("monotonic_deadline"), (int, float))):
            remaining = (float(deadline["monotonic_deadline"]) - now_mono) * 1000
        else:
            remaining = (float(deadline["wall_deadline"]) - now_wall) * 1000
        value = min(original, max(0, math.ceil(remaining)))
    except (TypeError, ValueError, OverflowError):
        return None, "unknown"
    return value, "expired" if value <= 0 else "active"


def _watch_monotonic_deadline(arm, *, wall=None, monotonic=None, boot_id=None):
    """Resolve the arm's one absolute deadline into this process's monotonic clock."""
    timeout = (arm or {}).get("timeout")
    if timeout is None:
        return None
    try:
        requested = int(timeout)
    except (TypeError, ValueError):
        return None
    if requested <= 0:
        return time.monotonic() if monotonic is None else float(monotonic)
    now_wall = time.time() if wall is None else float(wall)
    now_mono = time.monotonic() if monotonic is None else float(monotonic)
    current_boot = _boot_id() if boot_id is None else boot_id
    deadline = (arm or {}).get("deadline")
    if not isinstance(deadline, dict):
        armed_ts = (arm or {}).get("armed_ts")
        try:
            armed_wall = datetime.datetime.strptime(
                armed_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc).timestamp()
        except (TypeError, ValueError):
            return None
        deadline = {"wall_deadline": armed_wall + requested / 1000.0,
                    "requested_ms": requested}
    try:
        original = min(requested, max(0, int(deadline.get("requested_ms", requested))))
        cap = now_mono + original / 1000.0
        if (deadline.get("boot_id") and current_boot
                and deadline.get("boot_id") == current_boot
                and isinstance(deadline.get("monotonic_deadline"), (int, float))):
            return min(cap, float(deadline["monotonic_deadline"]))
        return min(cap, now_mono + float(deadline["wall_deadline"]) - now_wall)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _steward_seat(harness, sid):
    """Return a pane/native seat only when the shared seat resolver proves one."""
    if not harness or harness == "unknown" or not sid:
        return None
    try:
        import dispatch_seat_handover
        seat = dispatch_seat_handover.pane_seat(harness=harness, sid=sid)
        return seat.as_dict() if seat is not None and seat.kind in {"pane", "native"} else None
    except Exception:
        return None


def _seat_claim_path(root, server, seat):
    raw = f"{server or 'default'}|{seat.get('key', '')}".encode("utf-8", "replace")
    return Path(root) / (hashlib.sha256(raw).hexdigest()[:16] + ".seat")


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


def _run_herdr_get(target, *, server=None):
    if server is None:
        argv = _herdr_argv("agent", "get", target)
        env = None
    else:
        selected = server or "default"
        argv = (["herdr"] + ([] if selected == "default"
                              else ["--session", selected])
                + ["agent", "get", target])
        env = _herdr_env_for_server(selected)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, env=env,
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


def _watch_arm_scope_matches(arm, server, steward_sid, until, agent):
    if not isinstance(arm, dict) or (arm.get("server") or "default") != server:
        return False
    steward = arm.get("steward") or {}
    if steward.get("session_id") != steward_sid:
        if not _confirmed_watch_successor(arm.get("watch_id"), arm, steward_sid):
            return False
    if tuple(sorted(arm.get("until") or [])) != tuple(sorted(until or [])):
        return False
    expected = arm.get("agent") or {}
    return all(expected.get(key) not in {None, "", "-"}
               and expected.get(key) == agent.get(key)
               for key in ("harness", "pane"))


def _confirmed_watch_successor(watch_id, arm, requested_sid=None, *, include_acked=False):
    """A watch follows only the exact successor stored by the common seat resolver."""
    steward = (arm or {}).get("steward") or {}
    seat_key = ((arm or {}).get("steward_seat") or {}).get("key")
    if not watch_id or not seat_key:
        return None
    try:
        import dispatch_seat_handover as handover
        relation = handover.resolve_watch(str(watch_id), seat_key=seat_key,
                                          include_acked=include_acked)
    except Exception:
        return None
    if not isinstance(relation, dict):
        return None
    if any(relation.get(field) != (arm or {}).get(field) for field in
           ("server", "target", "agent", "until", "deadline", "origin")):
        # Arms predating the optional persisted `origin` compare against their
        # original steward tuple below; no name or pane alias fills the gap.
        if any(relation.get(field) != (arm or {}).get(field) for field in
               ("server", "target", "agent", "until", "deadline")):
            return None
    holder = relation.get("holder") or {}
    if holder.get("harness") != steward.get("harness"):
        return None
    if requested_sid is not None and holder.get("sid") != requested_sid:
        return None
    return relation


def _watch_arm_matches(arm, server, steward_sid, until, agent):
    if not _watch_arm_scope_matches(arm, server, steward_sid, until, agent):
        return False
    expected_sid = (arm.get("agent") or {}).get("session_id")
    observed_sid = agent.get("session_id")
    return expected_sid == observed_sid


def _watch_claim_path_for_arm(paths, arm, server):
    claim_key = arm.get("claim_key") if isinstance(arm, dict) else None
    if isinstance(claim_key, str) and re.fullmatch(r"logical-[0-9a-f]{16}", claim_key):
        return paths.arm.parent / f"{claim_key}.arm"
    raw_target = arm.get("target") if isinstance(arm, dict) else None
    if not isinstance(raw_target, str) or not raw_target:
        return None
    steward_sid = (arm.get("steward") or {}).get("session_id")
    if not isinstance(steward_sid, str) or not steward_sid:
        return None
    return paths.arm.parent / (
        f"{_dedupe_key(steward_sid, raw_target, list(arm.get('until') or []), server)}.arm"
    )


def _persist_watch_session_under_alias_lock(paths, server, session_id):
    """Bind an arm while its pane alias lock is held, then its claim lock.

    Lock order for watch identity is steward-seat (registration only), pane
    alias, then the arm's logical or legacy raw claim. Registration admission
    uses the same order. The claim lock protects durable legacy/new claim
    publication; the pane lock serializes the SID transition itself.
    """
    if not isinstance(session_id, str) or session_id in {"", "-"}:
        return False, _read_json(paths.arm)
    arm = _read_json(paths.arm)
    agent = arm.get("agent") if isinstance(arm, dict) else None
    if (not isinstance(agent, dict) or (arm.get("server") or "default") != server
            or any(not isinstance(agent.get(key), str) or agent.get(key) in {"", "-"}
                   for key in ("harness", "pane"))):
        return False, arm
    claim = _watch_claim_path_for_arm(paths, arm, server)
    if claim is None:
        return False, arm
    try:
        claim_fd = _open_lock(claim)
    except OSError:
        return False, arm
    try:
        if not _acquire_bounded(claim_fd, _CLAIM_TIMEOUT_MS):
            return False, _read_json(paths.arm)
        current = _read_json(paths.arm)
        current_agent = current.get("agent") if isinstance(current, dict) else None
        if (not isinstance(current_agent, dict)
                or (current.get("server") or "default") != server
                or (current.get("steward") or {}).get("session_id") !=
                    (arm.get("steward") or {}).get("session_id")
                or tuple(sorted(current.get("until") or [])) !=
                    tuple(sorted(arm.get("until") or []))
                or any(current_agent.get(key) != agent.get(key) for key in ("harness", "pane"))):
            return False, current
        current_sid = current_agent.get("session_id")
        if current_sid in {None, "", "-"}:
            updated = {**current, "agent": {**current_agent, "session_id": session_id}}
            _write_json_atomic(paths.arm, updated)
            return True, updated
        if current_sid == session_id:
            return True, current
        # A native binding is immutable. A conflicting later result stays
        # unknown to its observer and never replaces the established SID.
        return False, current
    finally:
        os.close(claim_fd)


def _watch_native_binding_evidence(paths, arm):
    """Return durable binding evidence without inventing a second identity source."""
    observation = _read_json(paths.observation) or {}
    wait_record = observation.get("native_wait") if isinstance(observation, dict) else None
    payload = None
    try:
        raw = paths.log.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    candidates = [raw, *reversed(raw.splitlines())] if raw else []
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
            payload = parsed
            break

    if payload is not None:
        state, result_agent, _code, reason = _interpret_payload(
            payload, arm.get("target", "-"))
        if state in {"timeout", "agent-not-found", "herdr-unavailable"}:
            return "terminal", None, None
        result = {"agent_state": state, "agent": result_agent,
                  "reason": reason or state}
        identity, sid, _identity_reason = _watch_native_result_identity(arm, result)
        if identity == "conflict":
            return "conflict", sid, result
        if reason is not None or state == "unknown":
            return "unknown", None, None
        if identity == "unbound" and sid is None:
            return "sidless", None, result
        if identity in {"match", "unbound"}:
            return "bound", sid, result
        if identity == "sidless":
            return "sidless", sid, result
        return "unknown", None, None

    if isinstance(wait_record, dict) and wait_record.get("state") == "result":
        result_agent = wait_record.get("agent")
        if isinstance(result_agent, dict):
            result = {
                "agent_state": wait_record.get("agent_state", "unknown"),
                "agent": result_agent,
                "reason": wait_record.get("reason", "native-wait-result"),
            }
            identity, sid, _identity_reason = _watch_native_result_identity(arm, result)
            if identity == "unbound" and sid is None:
                return "sidless", None, result
            if identity in {"match", "unbound"}:
                return "bound", sid, result
            if identity == "sidless":
                return "sidless", sid, result
            if identity == "conflict":
                return "conflict", sid, result
            return "unknown", None, None
    if isinstance(wait_record, dict) and wait_record.get("state") == "unknown":
        return "unknown", None, None
    if observation.get("state") == "unknown" or raw.strip():
        return "unknown", None, None
    return "none", None, None


def _watch_native_result_identity(arm, result):
    """Classify a parsed wait result against the exact durable arm identity."""
    if not isinstance(arm, dict) or not isinstance(result, dict):
        return "unknown", None, "target-identity-unverified"
    if (result.get("terminal_reason")
            or result.get("agent_state") in {"timeout", "agent-not-found", "herdr-unavailable"}):
        return "terminal", None, None
    expected = arm.get("agent")
    observed = result.get("agent")
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        return "unknown", None, "target-identity-unverified"
    if any(not isinstance(expected.get(key), str) or expected.get(key) in {"", "-"}
           or observed.get(key) != expected.get(key)
           for key in ("harness", "pane")):
        return "unknown", None, "target-identity-unverified"
    sid = observed.get("session_id")
    if not isinstance(sid, str):
        return "unknown", None, "target-identity-unverified"
    expected_sid = expected.get("session_id")
    if expected_sid in {None, "", "-"}:
        if sid in {"", "-"}:
            return "unbound", None, None
        return "unbound", sid, None
    if sid in {"", "-"}:
        return "sidless", expected_sid, None
    if expected_sid != sid:
        return "conflict", sid, "target-identity-conflict"
    return "match", sid, None


def _preserve_watch_native_result_unknown(paths, arm, result, reason):
    """Keep a result unresolved and retain the exact prior wait process identity."""
    observation = _read_json(paths.observation) or {}
    previous = observation.get("native_wait")
    unknown_wait = dict(previous) if isinstance(previous, dict) else {}
    if isinstance(result, dict):
        if isinstance(result.get("agent"), dict):
            unknown_wait["agent"] = result["agent"]
        if result.get("agent_state") is not None:
            unknown_wait["agent_state"] = result["agent_state"]
        if result.get("reason") is not None:
            unknown_wait["result_reason"] = result["reason"]
        unknown_wait["unresolved_result"] = dict(result)
    unknown_wait.update({"state": "unknown", "reason": reason})
    observation.update({
        "schema_version": _WATCH_SCHEMA,
        "watch_id": paths.arm.stem,
        "server": arm.get("server") or "default",
        "state": "unknown",
        "reason": reason,
        "native_wait": unknown_wait,
        "observed_ts": _utc_now(),
    })
    _write_json_atomic(paths.observation, observation)
    return unknown_wait, observation


def _record_watch_native_result(paths, arm, result):
    """Publish only a result attributed to this arm's exact native identity."""
    if not isinstance(result, dict):
        return False
    identity, _sid, identity_reason = _watch_native_result_identity(arm, result)
    if identity not in {"match", "unbound", "sidless"}:
        _preserve_watch_native_result_unknown(
            paths, arm, result, identity_reason or "target-identity-unverified")
        return False
    observation = _read_json(paths.observation) or {}
    previous = observation.get("native_wait")
    native_wait = dict(previous) if isinstance(previous, dict) else {}
    native_wait.update(result)
    native_wait["state"] = "result"
    observation.update({
        "schema_version": _WATCH_SCHEMA,
        "watch_id": paths.arm.stem,
        "server": arm.get("server") or "default",
        "native_wait": native_wait,
        "observed_ts": _utc_now(),
    })
    _write_json_atomic(paths.observation, observation)
    return True


def _write_watch_observation(paths, observation, *, control_updates=()):
    """Keep a transition request beside its exact durable native result."""
    current = _read_json(paths.observation) or {}
    current_native = current.get("native_wait") or {}
    next_native = observation.get("native_wait") or {}
    if (current_native.get("state") == "result"
            and current_native.get("agent_state") in {"idle", "done"}
            and next_native.get("state") == "result"
            and next_native.get("agent_state") == current_native.get("agent_state")
            and next_native.get("agent") == current_native.get("agent")):
        control_fields = {"readiness_transition", "readiness_contention"}
        updates = set(control_updates)
        if not updates.issubset(control_fields):
            raise ValueError("unknown watch observation control update")
        observation = dict(observation)
        for field in control_fields - updates:
            if field in current:
                observation[field] = current[field]
            else:
                observation.pop(field, None)
    _write_json_atomic(paths.observation, observation)
    return observation


def _persist_watch_session(args, paths, server, session_id):
    """Bind an unbound arm under the same pane admission lock as registration."""
    arm = _read_json(paths.arm)
    agent = arm.get("agent") if isinstance(arm, dict) else None
    if (not isinstance(agent, dict) or (arm.get("server") or "default") != server
            or (arm.get("steward") or {}).get("session_id") != args.steward_session_id
            or tuple(sorted(arm.get("until") or [])) != tuple(sorted(args.until or []))
            or agent.get("harness") != getattr(args, "expected_harness", None)
            or agent.get("pane") != getattr(args, "expected_pane", None)):
        return False
    alias = paths.arm.parent / (
        f"{_watch_alias_lock_key(args.steward_session_id, agent, list(arm.get('until') or []), server)}.lock"
    )
    try:
        alias_fd = _open_lock(alias)
    except OSError:
        return False
    try:
        if not _acquire_bounded(alias_fd, _CLAIM_TIMEOUT_MS):
            return False
        current = _read_json(paths.arm)
        current_agent = current.get("agent") if isinstance(current, dict) else None
        if (not isinstance(current_agent, dict)
                or (current.get("steward") or {}).get("session_id") != args.steward_session_id
                or any(current_agent.get(key) != agent.get(key) for key in ("harness", "pane"))):
            return False
        ok, _updated = _persist_watch_session_under_alias_lock(paths, server, session_id)
        return ok
    finally:
        os.close(alias_fd)


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
    steward_seat = _steward_seat(steward_harness, steward_sid)
    target = args.target
    until = list(args.until or [])
    wake = args.wake
    if wake == "auto":
        wake = "hook" if _caller_is_claude() else "none"

    server = _HERDR_SESSION or "default"
    # Resolve before choosing a logical claim: names, pane IDs and agent IDs can
    # all refer to the same native target. Keep the raw target for the arm,
    # receipt, ledger and native wait; it remains the operator's accepted text.
    if _herdr_missing():
        return _unavailable("herdr-not-found")
    state, agent, _code, reason = _interpret_payload(_run_herdr_get(target), target)
    if state == "agent-not-found":
        print(_typed_line("agent-not-found", "-", "-", target, "-"))
        return 2
    if reason is not None:
        return _unavailable(reason)
    # An unbound fresh pane is still a valid target. Its pane lock also serializes
    # the later transition from pane identity to the first native session.
    if any(agent.get(key) in {None, "", "-"} for key in ("harness", "pane")):
        return _unavailable("watch-identity-unverified")

    seat_fd = None
    if steward_seat is not None:
        seat_claim = _seat_claim_path(root, server, steward_seat)
        try:
            seat_fd = _open_lock(seat_claim)
        except OSError as exc:
            return _unavailable(f"watch-seat-claim-failed-{exc.errno}")
        if not _acquire_bounded(seat_fd, _CLAIM_TIMEOUT_MS):
            os.close(seat_fd)
            return _unavailable("watch-seat-claim-contended")

    # The pane-scoped lock covers aliases, authoritative target revalidation,
    # and the unbound-pane -> bound-session transition. Lock order is steward
    # seat -> pane alias -> logical claim -> raw claim. Observer binding starts
    # at the same pane alias lock and then takes its arm's logical or legacy raw
    # claim, so old arms follow the same order.
    alias_claim = root / f"{_watch_alias_lock_key(steward_sid, agent, until, server)}.lock"
    try:
        alias_fd = _open_lock(alias_claim)
    except OSError as exc:
        if seat_fd is not None:
            os.close(seat_fd)
        return _unavailable(f"watch-alias-claim-failed-{exc.errno}")
    if not _acquire_bounded(alias_fd, _CLAIM_TIMEOUT_MS):
        os.close(alias_fd)
        if seat_fd is not None:
            os.close(seat_fd)
        return _unavailable("watch-alias-claim-contended")

    # The first get only selects a pane lock. It cannot authorize admission:
    # the pane may have been reused while this call waited for that lock.
    if (_HERDR_SESSION or "default") != server:
        os.close(alias_fd)
        if seat_fd is not None:
            os.close(seat_fd)
        return _unavailable("watch-endpoint-changed")
    state, current_agent, _code, reason = _interpret_payload(_run_herdr_get(target), target)
    if state == "agent-not-found":
        os.close(alias_fd)
        if seat_fd is not None:
            os.close(seat_fd)
        print(_typed_line("agent-not-found", "-", "-", target, "-"))
        return 2
    if reason is not None:
        os.close(alias_fd)
        if seat_fd is not None:
            os.close(seat_fd)
        return _unavailable(reason)
    if (_HERDR_SESSION or "default") != server:
        os.close(alias_fd)
        if seat_fd is not None:
            os.close(seat_fd)
        return _unavailable("watch-endpoint-changed")
    if any(current_agent.get(key) in {None, "", "-"} for key in ("harness", "pane")):
        os.close(alias_fd)
        if seat_fd is not None:
            os.close(seat_fd)
        return _unavailable("watch-identity-unverified")
    if any(current_agent.get(key) != agent.get(key) for key in ("harness", "pane")):
        os.close(alias_fd)
        if seat_fd is not None:
            os.close(seat_fd)
        return _unavailable("watch-target-pane-changed")
    agent = current_agent

    logical_key = _logical_watch_claim_key(steward_sid, agent, until, server)
    logical_claim = root / f"{logical_key}.arm"

    # Inspect active obligations before taking this request's logical/raw
    # claims. An unbound candidate may own a legacy raw claim; reconciling it
    # here preserves the global alias -> arm-claim order without holding a
    # second, unrelated claim lock.
    claim = root / f"{_dedupe_key(steward_sid, target, until, server)}.arm"
    candidate_ids = []
    for candidate_claim in (logical_claim, claim):
        try:
            candidate_id = candidate_claim.read_text(encoding="utf-8").strip()
        except OSError:
            candidate_id = ""
        if candidate_id and candidate_id not in candidate_ids:
            candidate_ids.append(candidate_id)
    entries = _watch_entries(root)
    by_id = {watch_id: arm for watch_id, arm in entries}
    ordered_ids = candidate_ids + [watch_id for watch_id, _arm in entries
                                   if watch_id not in candidate_ids]
    existing_match = None
    requested_sid = agent.get("session_id")
    for existing_id in ordered_ids:
        existing_arm = by_id.get(existing_id)
        existing_paths = _watch_paths(existing_id, root)
        if (existing_paths.receipt.exists()
                or not _watch_arm_scope_matches(
                    existing_arm, server, steward_sid, until, agent)):
            continue
        existing_agent = existing_arm.get("agent") or {}
        existing_sid = existing_agent.get("session_id")
        evidence, evidence_sid, wait_result = _watch_native_binding_evidence(
            existing_paths, existing_arm)
        if evidence == "bound":
            if existing_sid not in {None, "", "-"} and existing_sid != evidence_sid:
                _preserve_watch_native_result_unknown(
                    existing_paths, existing_arm, wait_result,
                    "target-identity-conflict")
                continue
            if existing_sid in {None, "", "-"}:
                _bound, updated_arm = _persist_watch_session_under_alias_lock(
                    existing_paths, server, evidence_sid)
                if not _bound:
                    _preserve_watch_native_result_unknown(
                        existing_paths, existing_arm, wait_result,
                        "target-identity-unverified")
                    continue
                existing_arm = updated_arm or _read_json(existing_paths.arm)
                by_id[existing_id] = existing_arm
            if not _record_watch_native_result(existing_paths, existing_arm, wait_result):
                continue
        elif evidence == "sidless":
            if (existing_sid not in {None, "", "-"}
                    and evidence_sid != existing_sid):
                _preserve_watch_native_result_unknown(
                    existing_paths, existing_arm, wait_result,
                    "target-identity-conflict")
                continue
            if not _record_watch_native_result(existing_paths, existing_arm, wait_result):
                continue
        elif evidence == "conflict":
            _preserve_watch_native_result_unknown(
                existing_paths, existing_arm, wait_result,
                "target-identity-conflict")
            continue
        elif (existing_sid in {None, "", "-"}
              and requested_sid not in {None, "", "-"} and evidence == "none"):
            _bound, updated_arm = _persist_watch_session_under_alias_lock(
                existing_paths, server, requested_sid)
            existing_arm = updated_arm or _read_json(existing_paths.arm)
            by_id[existing_id] = existing_arm
        if _watch_arm_matches(existing_arm, server, steward_sid, until, agent):
            existing_match = (existing_id, existing_arm, existing_paths)
            break

    try:
        logical_fd = _open_lock(logical_claim)
    except OSError as exc:
        os.close(alias_fd)
        if seat_fd is not None:
            os.close(seat_fd)
        return _unavailable(f"watch-claim-failed-{exc.errno}")
    if not _acquire_bounded(logical_fd, _CLAIM_TIMEOUT_MS):
        os.close(logical_fd)
        os.close(alias_fd)
        if seat_fd is not None:
            os.close(seat_fd)
        return _unavailable("watch-claim-contended")

    claim_fd = None
    try:
        claim_fd = _open_lock(claim)
    except OSError as exc:
        os.close(logical_fd)
        os.close(alias_fd)
        if seat_fd is not None:
            os.close(seat_fd)
        return _unavailable(f"watch-claim-failed-{exc.errno}")
    try:
        if not _acquire_bounded(claim_fd, _CLAIM_TIMEOUT_MS):
            return _unavailable("watch-claim-contended")

        if existing_match is not None:
            existing_id, existing_arm, existing_paths = existing_match
            if not _watcher_present(existing_arm):
                _spawn_watch_observer(existing_id, existing_arm)
                existing_arm = _read_json(existing_paths.arm) or existing_arm
            _write_watch_claim(logical_fd, existing_id)
            _write_watch_claim(claim_fd, existing_id)
            print(_already_armed_line(existing_id, existing_arm, existing_paths))
            return 0

        # A new native session in the same proven seat replaces its previous
        # watch only after the new observer is durably armed. Until-set identity
        # remains exact, and a different seat is never folded into this one.
        superseded = []
        retired_targets = []
        for old_id, old_arm in _watch_entries(root):
            if (not isinstance(old_arm, dict)
                    or _watch_paths(old_id, root).receipt.exists()
                    or (old_arm.get("server") or "default") != server):
                continue
            old_agent = old_arm.get("agent") or {}
            old_session = old_agent.get("session_id")
            if (old_agent.get("pane") == agent.get("pane")
                    and isinstance(old_session, str) and old_session not in {"", "-"}
                    and old_session != agent.get("session_id")):
                retired_targets.append((old_id, old_arm))
        if isinstance(steward_seat, dict):
            wanted_until = tuple(sorted(until))
            for old_id, old_arm in _watch_entries(root):
                if (not isinstance(old_arm, dict)
                        or _watch_paths(old_id, root).receipt.exists()
                        or (old_arm.get("server") or "default") != server
                        or (old_arm.get("steward") or {}).get("session_id") == steward_sid
                        or (old_arm.get("steward_seat") or {}).get("key") != steward_seat.get("key")
                        or tuple(sorted(old_arm.get("until") or [])) != wanted_until):
                    continue
                old_agent = old_arm.get("agent") or {}
                if all(old_agent.get(key) == agent.get(key)
                       for key in ("pane", "harness", "session_id")):
                    # A role successor or an unconfirmed callback never retires
                    # the predecessor's accepted duty. Same-seat transfer is
                    # settled by the original watch and its shared resolver.
                    continue

        pane_info = _retire_pane_info(agent["pane"])
        foreground = _retire_foreground(agent["pane"], agent["harness"], strict=False,
                                        info=pane_info)
        foreground_identity = ({key: foreground.get(key) for key in
                                ("pid", "start", "shell_pid", "shell_start")}
                               if isinstance(foreground, dict) else None)
        shell_pid = (pane_info.get("shell_pid") if isinstance(pane_info, dict) else
                     foreground.get("shell_pid") if isinstance(foreground, dict) else None)
        shell_start = (_proc_start_ticks(shell_pid) if shell_pid else None)
        if not shell_start and isinstance(foreground, dict):
            shell_start = foreground.get("shell_start")
        pane_identity = ({"shell_pid": shell_pid, "shell_start": str(shell_start)}
                         if shell_pid and shell_start else None)
        armed_ts = _utc_now()
        deadline = _watch_deadline(args.timeout)
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

        arm = {
            "schema_version": _WATCH_SCHEMA,
            "watch_id": watch_id,
            "target": target,
            "claim_key": logical_key,
            "until": until,
            "timeout": args.timeout,
            "refs": list(args.ref or []),
            "server": server,
            "readiness_endpoint": (
                herdr_observation.socket_path_for_endpoint(server)
                or _WATCH_SERVER_ENDPOINT_PREFIX + herdr_observation.endpoint_name(server)),
            "agent": {key: agent.get(key, "-") for key in ("harness", "session_id", "name", "pane")},
            "steward": {
                "harness": steward_harness,
                "session_id": steward_sid,
                "project": steward_project,
            },
            "steward_seat": steward_seat,
            "armed_ts": armed_ts,
            "deadline": deadline,
            "observer_protocol": 2,
            "wake": wake,
            "watcher": {},
            "rearmed_from": getattr(args, "rearmed_from", None) or None,
            "rearm_count": int(args.rearm_count or 0),
            "observer_generation": 1,
            "foreground_identity": foreground_identity,
            "pane_identity": pane_identity,
        }
        argv = [sys.executable, str(Path(__file__).resolve()), *_watch_observer_duty_argv(
            watch_id, arm, rearm_generation=args.rearm_count or 0,
            lock_fd=lock_fd, timeout_before_refs=True)]
        _write_json_atomic(paths.arm, arm)
        # Acceptance and the stable claim precede the observer side effect.
        _write_watch_claim(logical_fd, watch_id)
        _write_watch_claim(claim_fd, watch_id)

        try:
            watcher_signal_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
            # Re-exec, never fork: a fork inherits the caller's process group,
            # file descriptors and interpreter state, which is precisely the
            # coupling this contract removes.
            try:
                proc = subprocess.Popen(
                    argv, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True, pass_fds=(lock_fd,), cwd=str(root),
                    env=_herdr_env_for_server(server),
                )
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, watcher_signal_mask)
        except (OSError, subprocess.SubprocessError) as exc:
            os.close(log_fd)
            os.close(lock_fd)
            return _unavailable(f"watcher-spawn-failed-{getattr(exc, 'errno', 'unknown')}")
        os.close(log_fd)
        # Closing this copy does not release the flock: the child still holds a
        # descriptor for the same open file description.
        os.close(lock_fd)

        arm["watcher"] = {"pid": proc.pid, "pid_start": process_start_ticks(proc.pid) or ""}
        _write_json_atomic(paths.arm, arm)

        for old_id, old_arm in superseded:
            old_paths = _watch_paths(old_id, root)
            old_observation = _read_json(old_paths.observation) or {}
            _write_json_atomic(old_paths.observation, {
                **old_observation,
                "schema_version": _WATCH_SCHEMA, "watch_id": old_id,
                "state": "terminal_pending", "reason": "steward-replaced",
                "server": server,
                "agent": {key: (old_arm.get("agent") or {}).get(key, "-")
                          for key in ("harness", "session_id", "pane")},
                "observed_ts": _utc_now(),
            })
            _wake_watch_observer(old_arm)
            _settle_terminal_watch_if_unobserved(old_id, old_arm, root)

        for old_id, old_arm in retired_targets:
            old_paths = _watch_paths(old_id, root)
            old_observation = _read_json(old_paths.observation) or {}
            _write_json_atomic(old_paths.observation, {
                **old_observation,
                "schema_version": _WATCH_SCHEMA, "watch_id": old_id,
                "state": "terminal_pending", "reason": "session-replaced",
                "server": server,
                "agent": {key: (old_arm.get("agent") or {}).get(key, "-")
                          for key in ("harness", "session_id", "pane")},
                "observed_ts": _utc_now(),
            })
            _wake_watch_observer(old_arm)
            _settle_terminal_watch_if_unobserved(old_id, old_arm, root)

        # Publish the winning watch_id into the claim while still holding it.
        _write_watch_claim(logical_fd, watch_id)
        _write_watch_claim(claim_fd, watch_id)

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
        resolved_name = agent["name"] if agent["name"] != "-" else target
        _mark_observed(agent["harness"], agent["session_id"], resolved_name,
                       from_identity=(steward_sid, steward_harness), pane=agent["pane"])
    finally:
        if claim_fd is not None:
            os.close(claim_fd)
        os.close(logical_fd)
        os.close(alias_fd)
        if seat_fd is not None:
            os.close(seat_fd)

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
    arm = _read_json(paths.arm) or {}
    terminal_reason = None
    expected_agent = dict(arm.get("agent") or {
        "harness": expected["harness"], "session_id": expected["session_id"],
        "name": args.target, "pane": expected["pane"],
    })
    observation = _read_json(paths.observation) or {}
    wait_record = observation.get("native_wait") if isinstance(observation, dict) else None
    remaining_ms, deadline_state = _watch_timeout_remaining_ms(arm)
    deadline = _watch_monotonic_deadline(arm)
    if deadline_state == "unknown":
        wait_record = {"state": "unknown", "reason": "watch-timeout-deadline-unverifiable"}
        _write_watch_observation(paths, {
            **observation, "schema_version": _WATCH_SCHEMA, "watch_id": args.watch_id,
            "state": "unknown", "reason": "watch-timeout-deadline-unverifiable",
            "native_wait": wait_record,
            "observed_ts": _utc_now(),
        })
    if deadline_state == "expired":
        if isinstance(wait_record, dict) and wait_record.get("state") == "started":
            _stop_watch_wait(None, wait_record.get("pid"), wait_record.get("pid_start"))
        terminal_reason = "watch-deadline"
        state, agent = "timeout", expected_agent
        _write_terminal_watch_receipt(args.watch_id, arm, terminal_reason)
        os._exit(0)

    event_transport = None
    wait_pidfd = None
    steward_pidfd = None
    wake_read = wake_write = None
    previous_usr1 = None
    wait_proc = None
    wait_pid = wait_start = None
    wait_log = paths.log
    try:
        requested_terminal = _terminal_watch_request(paths, expected, server)
        if requested_terminal:
            _write_terminal_watch_receipt(args.watch_id, arm, requested_terminal)
            os._exit(0)
        # The private wake pipe makes a confirmed handover/retire request an
        # event to this controller without polling the watch directory.
        wake_read, wake_write = os.pipe()
        os.set_blocking(wake_read, False)
        os.set_blocking(wake_write, False)
        global _WATCH_WAKE_FD
        _WATCH_WAKE_FD = wake_write
        previous_usr1 = signal.signal(signal.SIGUSR1, _watch_wakeup_signal)
        # The parent blocks SIGUSR1 across spawn, so a retirement arriving
        # before this handler exists stays pending and wakes us after install.
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGUSR1})

        steward_seat = arm.get("steward_seat") or {}
        socket_path = herdr_observation.socket_path_for_endpoint(server)
        if socket_path:
            event_transport = herdr_observation.ObservationTransport(
                server, socket_path, timeout=min(0.5, _HERDR_GET_TIMEOUT_SECONDS))
            initial_view = event_transport.observe()  # subscribe before the one native wait
            target_reason = _watch_terminal_from_view(
                initial_view, expected, server, event_transport)
            if target_reason:
                terminal_reason = target_reason
                state, agent = "pane-exited", expected_agent
            if steward_seat.get("kind") == "pane":
                steward_agent = {**(arm.get("steward") or {}),
                                 "pane": steward_seat.get("pane", "-")}
                steward_reason = _watch_terminal_from_view(
                    initial_view, steward_agent, server, event_transport)
                if steward_reason == "session-replaced":
                    # Session replacement can precede the ordinary confirmed
                    # start callback. Keep this accepted duty pending until the
                    # shared seat lineage resolves; unknown is not terminal.
                    _confirmed_watch_successor(args.watch_id, arm)
                elif steward_reason in {"pane-absent", "pane-closed", "pane-exited"}:
                    terminal_reason = "steward-exited"

        native = steward_seat.get("native") if isinstance(steward_seat, dict) else None
        if isinstance(native, dict):
            try:
                from session_tidy_native import live as native_session_live
                native_record = native_session_live(native)
                native_pid = int(native.get("pid"))
                observed_start = process_start_ticks(native_pid)
                if (observed_start is None or str(observed_start) != str(native.get("start"))):
                    terminal_reason = "steward-exited"
                elif native_record and native_record.get("sessionId") == (arm.get("steward") or {}).get("session_id"):
                    steward_pidfd = os.pidfd_open(native_pid, 0)
                elif native_record and native_record.get("sessionId"):
                    _confirmed_watch_successor(args.watch_id, arm)
            except (AttributeError, OSError, TypeError, ValueError):
                # Missing native proof stays unknown. A PID number or missing
                # session record alone is never used as a death vote.
                steward_pidfd = None

        if terminal_reason:
            _write_terminal_watch_receipt(args.watch_id, arm, terminal_reason)
            os._exit(0)

        # Older admission code could persist a conflicting SID as a normal
        # result. Reattachment must check every such record against the arm
        # before allowing it into the settlement path.
        if (isinstance(wait_record, dict) and wait_record.get("state") == "result"
                and not terminal_reason):
            identity, result_sid, identity_reason = _watch_native_result_identity(
                arm, wait_record)
            if identity == "unbound" and result_sid is not None:
                bound = _persist_watch_session(args, paths, server, result_sid)
                arm = _read_json(paths.arm) or arm
                expected_agent = dict(arm.get("agent") or expected_agent)
                expected.update({key: expected_agent.get(key, "-")
                                 for key in ("harness", "session_id", "pane")})
                identity, _result_sid, identity_reason = _watch_native_result_identity(
                    arm, wait_record)
                if identity == "unbound" and not bound:
                    identity_reason = "target-identity-unverified"
                    identity = "unknown"
            if identity not in {"match", "terminal", "unbound", "sidless"}:
                _preserve_watch_native_result_unknown(
                    paths, arm, wait_record,
                    identity_reason or "target-identity-unverified")
                os._exit(0)

        if ((wait_record or {}).get("state") == "result"
                and (wait_record or {}).get("agent_state") in {"idle", "done"}
                and _watch_readiness_identity(
                    arm, (wait_record or {}).get("agent_state")) is not None):
            try:
                readiness_store = peer_obligations.ObligationStore()
                shared_readiness.wake_runner(readiness_store.root)
                peer_obligations.ensure_normal_runner(reconciliation_required=True)
            except Exception:
                pass

        if isinstance(wait_record, dict):
            if wait_record.get("state") == "result":
                state = wait_record.get("agent_state", "unknown")
                agent = wait_record.get("agent") or expected_agent
                terminal_reason = wait_record.get("terminal_reason")
            elif wait_record.get("state") == "started":
                wait_pid = wait_record.get("pid")
                wait_start = wait_record.get("pid_start")
                wait_identity_live = (
                    isinstance(wait_start, str) and bool(wait_start)
                    and _pid_identity_ok(wait_pid, wait_start)
                )
                if wait_identity_live and hasattr(os, "pidfd_open"):
                    try:
                        wait_pidfd = os.pidfd_open(int(wait_pid), 0)
                    except OSError:
                        wait_pidfd = None
            elif wait_record.get("state") == "launching":
                _write_watch_observation(paths, {
                    **observation, "schema_version": _WATCH_SCHEMA,
                    "watch_id": args.watch_id, "state": "unknown",
                    "reason": "native-wait-launch-uncertain",
                    "native_wait": wait_record, "observed_ts": _utc_now(),
                })
                wait_record = {"state": "unknown", "reason": "native-wait-launch-uncertain"}
        else:
            if int(arm.get("observer_protocol", 0) or 0) < 2:
                # A legacy observer may already have called herdr wait. Keep
                # watching exact lifecycle/deadline evidence without repeating
                # a native wait whose launch/result was not recorded.
                wait_record = {"state": "unknown", "reason": "legacy-native-wait-unknown"}
                observation = {**observation, "schema_version": _WATCH_SCHEMA,
                               "watch_id": args.watch_id, "state": "unknown",
                               "reason": wait_record["reason"],
                               "native_wait": wait_record, "observed_ts": _utc_now()}
                _write_watch_observation(paths, observation)
            else:
                if deadline_state == "expired":
                    terminal_reason = "watch-deadline"
                    state, agent = "timeout", expected_agent
            if terminal_reason is None and int(arm.get("observer_protocol", 0) or 0) >= 2:
                # Publish the single-call intent before creating the child. A
                # crash in this tiny window remains explicitly ambiguous and
                # cannot cause a second native wait on reattachment.
                wait_record = {"state": "launching", "started_ts": _utc_now()}
                observation = {
                    "schema_version": _WATCH_SCHEMA, "watch_id": args.watch_id,
                    "state": "waiting", "reason": "native-wait-starting",
                    "server": server, "agent": expected_agent,
                    "native_wait": wait_record, "observed_ts": _utc_now(),
                }
                _write_watch_observation(paths, observation)
                command = _herdr_argv("agent", "wait", args.target)
                for until_state in args.until or []:
                    command.extend(("--until", until_state))
                if remaining_ms is not None:
                    if remaining_ms <= 0:
                        terminal_reason = "watch-deadline"
                        state, agent = "timeout", expected_agent
                    else:
                        command.extend(("--timeout", str(remaining_ms)))
                if terminal_reason is None:
                    log_fd = os.open(str(wait_log), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
                    try:
                        wait_proc = subprocess.Popen(
                            command, stdin=subprocess.DEVNULL, stdout=log_fd,
                            stderr=subprocess.STDOUT, close_fds=True,
                            cwd=str(paths.arm.parent), env=_herdr_env_for_server(server),
                        )
                    finally:
                        os.close(log_fd)
                    wait_pid = wait_proc.pid
                    wait_start = process_start_ticks(wait_pid) or ""
                    wait_record = {"state": "started", "pid": wait_pid,
                                   "pid_start": wait_start, "started_ts": _utc_now()}
                    observation = {**observation, "state": "waiting",
                                   "reason": "native-wait-running", "native_wait": wait_record,
                                   "observed_ts": _utc_now()}
                    _write_watch_observation(paths, observation)
                    if hasattr(os, "pidfd_open") and wait_start:
                        try:
                            wait_pidfd = os.pidfd_open(int(wait_pid), 0)
                        except OSError:
                            wait_pidfd = None

        if terminal_reason:
            _write_terminal_watch_receipt(args.watch_id, arm, terminal_reason)
            os._exit(0)

        def read_wait_result():
            global _LAST_HERDR_EXIT
            nonlocal arm, expected_agent
            if wait_proc is not None:
                try:
                    _LAST_HERDR_EXIT = wait_proc.wait(timeout=0)
                except subprocess.TimeoutExpired:
                    _LAST_HERDR_EXIT = None
            payload = None
            try:
                raw = wait_log.read_text(encoding="utf-8")
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    for line in reversed(raw.splitlines()):
                        try:
                            candidate = json.loads(line)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if isinstance(candidate, dict):
                            payload = candidate
                            break
            except OSError:
                pass
            if not isinstance(payload, dict):
                return None
            result_state, result_agent, _code, result_reason = _interpret_payload(payload, args.target)
            if result_state not in {"timeout", "agent-not-found", "herdr-unavailable"}:
                result = {"agent_state": result_state, "agent": result_agent,
                          "reason": result_reason}
                identity, observed_sid, identity_reason = _watch_native_result_identity(
                    arm, result)
                if identity == "unbound" and observed_sid is not None:
                    bound = _persist_watch_session(args, paths, server, observed_sid)
                    arm = _read_json(paths.arm) or arm
                    expected_agent = dict(arm.get("agent") or expected_agent)
                    expected.update({key: expected_agent.get(key, "-")
                                     for key in ("harness", "session_id", "pane")})
                    identity, _observed_sid, identity_reason = _watch_native_result_identity(
                        arm, result)
                    if identity == "unbound" and not bound:
                        identity_reason = "target-identity-unverified"
                        identity = "unknown"
                if identity not in {"match", "unbound", "sidless"}:
                    return {"unverified": identity_reason or "target-identity-unverified",
                            "agent": result_agent, "agent_state": result_state,
                            "reason": result_reason}
            return {"agent_state": result_state, "agent": result_agent,
                    "reason": result_reason}

        def preserve_unknown_wait(reason, wait_result=None):
            """Mark identity unresolved while retaining durable native evidence."""
            return _preserve_watch_native_result_unknown(
                paths, arm, wait_result, reason)

        # Reattached observers may start after the single native waiter has
        # already exited. Its exact stdout log is the durable result; settle it
        # before classifying the vanished PID as an unknown obligation.
        if ((wait_record or {}).get("state") == "started"
                and not _pid_identity_ok(wait_pid, wait_start)):
            wait_result = read_wait_result()
            if wait_result is None:
                wait_record, observation = preserve_unknown_wait(
                    "native-wait-result-unavailable")
            elif wait_result.get("terminal_reason"):
                terminal_reason = wait_result["terminal_reason"]
                state, agent = "pane-exited", expected_agent
            elif wait_result.get("unverified"):
                wait_record, observation = preserve_unknown_wait(
                    wait_result["unverified"], wait_result)
            else:
                state, agent = wait_result["agent_state"], wait_result["agent"]
                reason = wait_result["reason"] or state
                wait_record = {"state": "result", "agent_state": state,
                               "agent": agent, "reason": reason,
                               "terminal_reason": None, "done_ts": _utc_now()}
                observation = {**(_read_json(paths.observation) or observation),
                               "state": "waiting", "reason": reason,
                               "native_wait": wait_record, "observed_ts": _utc_now()}
                _write_watch_observation(paths, observation)

        if terminal_reason:
            _write_terminal_watch_receipt(args.watch_id, arm, terminal_reason,
                                          watcher={"pid": os.getpid(),
                                                   "pid_start": process_start_ticks(os.getpid()) or ""})
            os._exit(0)

        if not terminal_reason and (wait_record or {}).get("state") == "started":
            if (wait_pidfd is None and event_transport is None and remaining_ms is None
                    and steward_pidfd is None):
                # The only non-pidfd fallback remains event driven where a
                # Herdr subscription is available; no short sleep loop is used.
                if event_transport is None and remaining_ms is None and steward_pidfd is None:
                    _write_watch_observation(paths, {
                        **observation, "state": "unknown",
                        "reason": "native-wait-exit-event-unavailable",
                        "native_wait": wait_record, "observed_ts": _utc_now(),
                    })
                    os._exit(0)

        if not terminal_reason:
            awaiting_readiness = False
            receipt = None
            receipt_published = False
            receipt_rejected = False
            readiness_transition_pending = False

            def publish_ready_result(result_state, result_agent):
                nonlocal state, agent, receipt, receipt_published
                nonlocal receipt_rejected, readiness_transition_pending, terminal_reason
                nonlocal awaiting_readiness
                state, agent = result_state, result_agent
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
                    "watcher": {"pid": pid, "pid_start": process_start_ticks(pid) or ""},
                    "rearmed_from": args.rearmed_from or None,
                    "refs": list(args.ref or []),
                    "observer_generation": getattr(args, "rearm_count", 0) + 1,
                }
                result = _watch_publish_ready_receipt(
                    args.target, state, arm, expected, server, args.watch_id,
                    paths, receipt, deadline=deadline)
                if result is True:
                    receipt_published = True
                    return "published"
                if result == _WATCH_RECEIPT_EXPIRED:
                    terminal_reason = "watch-deadline"
                    state, agent = "timeout", expected_agent
                    return "expired"
                readiness_transition_pending = True
                if result in {_WATCH_RECEIPT_CONTENDED, _WATCH_RECEIPT_PENDING}:
                    awaiting_readiness = True
                    return "retry"
                receipt_rejected = True
                return "rejected"

            while True:
                terminal_reason = _terminal_watch_request(paths, expected, server)
                if terminal_reason:
                    break
                if steward_pidfd is not None:
                    ready, _, _ = select.select([steward_pidfd], [], [], 0)
                    if ready:
                        terminal_reason = "steward-exited"
                        break
                if deadline is not None and time.monotonic() >= deadline:
                    terminal_reason = "watch-deadline"
                    state, agent = "timeout", expected_agent
                    break
                if (wait_record or {}).get("state") == "result":
                    pass
                elif wait_pidfd is None and wait_proc is not None and wait_proc.poll() is not None:
                    wait_result = read_wait_result()
                    if wait_result is None:
                        terminal_reason = "herdr-unavailable"
                        state, agent = "herdr-unavailable", expected_agent
                    elif wait_result.get("terminal_reason"):
                        terminal_reason = wait_result["terminal_reason"]
                        state, agent = "pane-exited", expected_agent
                    elif wait_result.get("unverified"):
                        preserve_unknown_wait(wait_result["unverified"], wait_result)
                        os._exit(0)
                    else:
                        state, agent = wait_result["agent_state"], wait_result["agent"]
                        reason = wait_result["reason"] or state
                        wait_record = {"state": "result", "agent_state": state,
                                       "agent": agent, "reason": reason,
                                       "terminal_reason": None, "done_ts": _utc_now()}
                        observation = {**observation, "state": "waiting",
                                       "reason": reason, "native_wait": wait_record,
                                       "observed_ts": _utc_now()}
                        _write_watch_observation(paths, observation)
                if (wait_pidfd is not None and (wait_record or {}).get("state") == "started"):
                    ready, _, _ = select.select([wait_pidfd], [], [], 0)
                    if ready:
                        wait_result = read_wait_result()
                        if wait_result is None:
                            terminal_reason = "herdr-unavailable"
                            state, agent = "herdr-unavailable", expected_agent
                        elif wait_result.get("terminal_reason"):
                            terminal_reason = wait_result["terminal_reason"]
                            state, agent = "pane-exited", expected_agent
                        elif wait_result.get("unverified"):
                            preserve_unknown_wait(wait_result["unverified"], wait_result)
                            os._exit(0)
                        else:
                            state, agent = wait_result["agent_state"], wait_result["agent"]
                            reason = wait_result["reason"] or state
                            wait_record = {"state": "result", "agent_state": state,
                                           "agent": agent, "reason": reason,
                                           "terminal_reason": None, "done_ts": _utc_now()}
                            observation = {**observation, "state": "waiting",
                                           "reason": reason, "native_wait": wait_record,
                                           "observed_ts": _utc_now()}
                            _write_watch_observation(paths, observation)
                if terminal_reason:
                    break

                if (wait_record or {}).get("state") == "result":
                    result_state = wait_record.get("agent_state", "unknown")
                    result_reason = wait_record.get("reason")
                    if result_state == "timeout" or result_reason == "timeout":
                        terminal_reason = "wait-timeout"
                        state, agent = "timeout", wait_record.get("agent") or expected_agent
                        break
                    if result_state in {"herdr-unavailable", "agent-not-found"}:
                        census = herdr_observation.pane_census(session=server)
                        if (result_state == "agent-not-found" and herdr_observation.confirms_absence(
                                census, expected.get("pane"), endpoint=server)):
                            terminal_reason = "pane-absent"
                            state, agent = "pane-exited", expected_agent
                        elif result_state == "herdr-unavailable":
                            terminal_reason = "herdr-unavailable"
                            state, agent = "herdr-unavailable", expected_agent
                        else:
                            observation = {**observation, "state": "unknown",
                                           "reason": "target-absence-unconfirmed",
                                           "native_wait": wait_record, "observed_ts": _utc_now()}
                            _write_watch_observation(paths, observation)
                            os._exit(0)
                        break
                    if result_state in {"idle", "done"}:
                        if not awaiting_readiness:
                            readiness = _watch_transition_readiness(
                                args.target, result_state, arm, expected, server,
                                args.watch_id, paths, deadline=deadline)
                            if readiness.state == _WATCH_RECEIPT_EXPIRED:
                                terminal_reason = "watch-deadline"
                                state, agent = "timeout", expected_agent
                                break
                            if readiness.state == "ready":
                                action = publish_ready_result(
                                    result_state, wait_record.get("agent") or expected_agent)
                                if action == "retry":
                                    continue
                                break
                            observation = {**observation, "state": "pending",
                                           "reason": readiness.reason or result_state,
                                           "native_wait": wait_record, "observed_ts": _utc_now()}
                            _write_watch_observation(paths, observation)
                            awaiting_readiness = True
                    else:
                        state, agent = result_state, wait_record.get("agent") or expected_agent
                        break

                fds = [wake_read]
                event_fd = event_transport.fileno() if event_transport is not None else None
                if event_fd is not None:
                    fds.append(event_fd)
                if wait_pidfd is not None and (wait_record or {}).get("state") == "started":
                    fds.append(wait_pidfd)
                if steward_pidfd is not None:
                    fds.append(steward_pidfd)
                timeout = None if deadline is None else max(0, deadline - time.monotonic())
                ready, _, _ = select.select(fds, [], [], timeout)
                if wake_read in ready:
                    with contextlib.suppress(OSError):
                        os.read(wake_read, 4096)
                    terminal_reason = _terminal_watch_request(paths, expected, server)
                    if terminal_reason:
                        break
                    if (wait_record or {}).get("state") == "result" and (
                            wait_record.get("agent_state") in {"idle", "done"}):
                        readiness = _watch_transition_readiness(
                            args.target, wait_record.get("agent_state"), arm, expected, server,
                            args.watch_id, paths, deadline=deadline)
                        if readiness.state == _WATCH_RECEIPT_EXPIRED:
                            terminal_reason = "watch-deadline"
                            state, agent = "timeout", expected_agent
                            break
                        if readiness.state == "ready":
                            action = publish_ready_result(
                                wait_record.get("agent_state"),
                                wait_record.get("agent") or expected_agent)
                            if action == "retry":
                                continue
                            break
                        observation = {**observation, "state": "pending",
                                       "reason": readiness.reason or "shared-readiness-pending",
                                       "native_wait": wait_record, "observed_ts": _utc_now()}
                        _write_watch_observation(paths, observation)
                        awaiting_readiness = True
                if steward_pidfd is not None and steward_pidfd in ready:
                    terminal_reason = "steward-exited"
                    break
                if event_fd is not None and event_fd in ready:
                    view = event_transport.observe()
                    target_reason = _watch_terminal_from_view(view, expected, server, event_transport)
                    if target_reason:
                        terminal_reason = target_reason
                        state = "pane-exited"
                        agent = expected_agent
                        break
                    steward_agent = dict((arm.get("steward") or {}))
                    steward_agent["pane"] = steward_seat.get("pane", "-")
                    steward_agent["harness"] = steward_agent.get("harness", "-")
                    steward_agent["session_id"] = steward_agent.get("session_id", "-")
                    if steward_seat.get("kind") == "pane":
                        steward_reason = _watch_terminal_from_view(
                            view, steward_agent, server, event_transport)
                        if steward_reason == "session-replaced":
                            _confirmed_watch_successor(args.watch_id, arm)
                        if steward_reason in {"pane-absent", "pane-closed", "pane-exited"}:
                            terminal_reason = "steward-exited"
                            break
                    if (wait_record or {}).get("state") == "result":
                        result_state = wait_record.get("agent_state")
                        if result_state in {"idle", "done"}:
                            readiness = _watch_transition_readiness(
                                args.target, result_state, arm, expected, server,
                                args.watch_id, paths, deadline=deadline)
                            if readiness.state == _WATCH_RECEIPT_EXPIRED:
                                terminal_reason = "watch-deadline"
                                state, agent = "timeout", expected_agent
                                break
                            if readiness.state == "ready":
                                action = publish_ready_result(
                                    result_state, wait_record.get("agent") or expected_agent)
                                if action != "retry":
                                    break

        if terminal_reason:
            _stop_watch_wait(wait_proc, wait_pid, wait_start)
            _write_terminal_watch_receipt(args.watch_id, arm, terminal_reason,
                                          watcher={"pid": os.getpid(),
                                                   "pid_start": process_start_ticks(os.getpid()) or ""})
            os._exit(0)

        if ((wait_record or {}).get("state") == "result"
                and not receipt_published and not readiness_transition_pending):
            _write_watch_observation(paths, {
                **observation, "state": "settled", "reason": "native-wait-result-consumed",
                "native_wait": wait_record, "observed_ts": _utc_now(),
            })
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        if wait_proc is not None:
            _stop_watch_wait(wait_proc, wait_proc.pid, process_start_ticks(wait_proc.pid))
        latest = _read_json(paths.observation) or observation
        _write_watch_observation(paths, {
            **latest,
            "schema_version": _WATCH_SCHEMA, "watch_id": args.watch_id,
            "state": "unknown", "reason": "watch-observer-unavailable",
            **({"native_wait": wait_record} if isinstance(wait_record, dict) else {}),
            "observed_ts": _utc_now(),
        })
        os._exit(0)
    finally:
        if event_transport is not None:
            event_transport.close()
        for fd in (wait_pidfd, steward_pidfd, wake_read, wake_write):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        _WATCH_WAKE_FD = None
        if previous_usr1 is not None:
            with contextlib.suppress(ValueError):
                signal.signal(signal.SIGUSR1, previous_usr1)

    if receipt_rejected:
        os._exit(0)

    if receipt is None:
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
            "watcher": {"pid": pid, "pid_start": process_start_ticks(pid) or ""},
            "rearmed_from": args.rearmed_from or None,
            "refs": list(args.ref or []),
            "observer_generation": getattr(args, "rearm_count", 0) + 1,
        }
    if state in {"idle", "done"}:
        if not receipt_published:
            os._exit(0)
    else:
        _write_json_atomic(paths.receipt, receipt)
    with contextlib.suppress(OSError, ValueError):
        shared_readiness.clear_watch_transition(
            peer_obligations.ObligationStore().root, args.watch_id)
    try:
        paths.observation.unlink()
    except FileNotFoundError:
        pass

    _prepare_watch_notice(args.watch_id, arm, receipt)

    _record(
        to_harness=agent.get("harness", "unknown"),
        to_name=args.target, kind="notice", ref=args.ref, receipt=args.watch_id,
        status="received",
        summary_text=f"[notice] watch {args.watch_id} state={state} target={args.target}",
        from_identity=(args.steward_session_id, args.steward_harness, args.steward_project),
    )
    os._exit(0)


def _stop_watch_wait(wait_proc, wait_pid, wait_start):
    """Stop only this watch's exact native wait child; never signal its target."""
    try:
        pid = int(wait_pid)
    except (TypeError, ValueError):
        return
    if not isinstance(wait_start, str) or not wait_start:
        return
    if str(process_start_ticks(pid) or "") != wait_start:
        return
    try:
        if wait_proc is not None and wait_proc.pid == pid:
            if wait_proc.poll() is not None:
                return
            wait_proc.terminate()
            try:
                wait_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if str(process_start_ticks(pid) or "") == wait_start:
                    wait_proc.kill()
                    wait_proc.wait(timeout=2)
            return
        os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        return
    except Exception:
        return

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


def _watch_observer_duty_argv(watch_id, arm, *, rearm_generation, lock_fd=None,
                              service_current=None, timeout_before_refs=False,
                              include_rearmed_from=True):
    """Build the accepted watch contract, with service controls kept separate."""
    agent = arm.get("agent") or {}
    steward = arm.get("steward") or {}
    argv = [
        "__watch-run", "--watch-id", str(watch_id),
        "--target", str(arm.get("target", "")),
        "--server", str(arm.get("server") or "default"),
        "--expected-harness", str(agent.get("harness", "-")),
        "--expected-session-id", str(agent.get("session_id", "-")),
        "--expected-pane", str(agent.get("pane", "-")),
        "--steward-harness", str(steward.get("harness", "unknown")),
        "--steward-session-id", str(steward.get("session_id", "")),
        "--steward-project", str(steward.get("project", "")),
        "--armed-ts", str(arm.get("armed_ts", "")),
        "--rearm-count", str(int(rearm_generation)),
    ]
    if lock_fd is not None:
        argv.extend(("--lock-fd", str(lock_fd)))
    until = list(arm.get("until") or [])
    refs = list(arm.get("refs") or [])
    timeout = arm.get("timeout")
    for state in until:
        argv.extend(("--until", str(state)))
    if timeout_before_refs and timeout is not None:
        argv.extend(("--timeout", str(timeout)))
    for ref in refs:
        argv.extend(("--ref", str(ref)))
    if not timeout_before_refs and timeout is not None:
        argv.extend(("--timeout", str(timeout)))
    rearmed_from = arm.get("rearmed_from")
    if include_rearmed_from and rearmed_from:
        argv.extend(("--rearmed-from", str(rearmed_from)))
    if service_current:
        argv.extend(("--service-current", str(service_current)))
    return argv


def _spawn_watch_observer(watch_id, arm, *, service_current=None,
                          recipient_census=None):
    """Reattach the observer to accepted intent without changing its identity."""
    if not isinstance(arm, dict) or arm.get("watch_id") != watch_id:
        return False
    paths = _watch_paths(watch_id)
    if paths.receipt.exists():
        return True
    if recipient_census is None:
        from recipient_lifetime import RecipientCensus
        recipient_census = RecipientCensus()
    if _settle_ended_seatless_recipient(
            watch_id, arm, census=recipient_census):
        return True
    if _watcher_present(arm):
        return True
    prior_observation = _read_json(paths.observation) or {}
    if _settle_terminal_watch_if_unobserved(watch_id, arm):
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
        current_arm = _read_json(paths.arm)
        if (not isinstance(current_arm, dict) or current_arm.get("watch_id") != watch_id
                or (current_arm.get("steward") or {}).get("session_id") !=
                    (arm.get("steward") or {}).get("session_id")):
            return False
        if _settle_ended_seatless_recipient(
                watch_id, current_arm, census=recipient_census, lock_held=True):
            return True
        arm = current_arm
        os.set_inheritable(lock_fd, True)
        log_fd = os.open(str(paths.log), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        server = arm.get("server") or "default"
        import peer_obligations
        env = _herdr_env_for_server(server)
        script, child_env, binding = peer_obligations.service_script(
            "utilities/peer-steward.py", service_current, environ=env)
        argv = [sys.executable, str(script), *_watch_observer_duty_argv(
            watch_id, arm, rearm_generation=int(arm.get("observer_generation", 1)),
            lock_fd=lock_fd, service_current=binding)]
        if child_env is not None:
            env = child_env
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
        updated["observer_protocol"] = 2
        updated["watcher"] = {"pid": proc.pid, "pid_start": process_start_ticks(proc.pid) or ""}
        _write_json_atomic(paths.arm, updated)
        return True
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        return False
    finally:
        os.close(lock_fd)


def _watch_observer_takeover_argvs(watch_id, arm):
    """Return exact argv layouts supported by the recorded watch generation."""
    generation = arm.get("observer_generation") if isinstance(arm, dict) else None
    if (not isinstance(generation, int) or isinstance(generation, bool)
            or generation < 1):
        return None
    try:
        if generation == 1:
            accepted_generation = arm.get("rearm_count")
            if (not isinstance(accepted_generation, int)
                    or isinstance(accepted_generation, bool) or accepted_generation < 0):
                return None
            return [_watch_observer_duty_argv(
                watch_id, arm, rearm_generation=accepted_generation,
                timeout_before_refs=True)]

        # A successful restart records the next observer_generation only after
        # spawning. The live process therefore carries the preceding value.
        accepted_generation = generation - 1
        current = _watch_observer_duty_argv(
            watch_id, arm, rearm_generation=accepted_generation)
        variants = [current]
        # v3.35.28/.33 restart builders omitted this lineage-only option even
        # when the arm retained it. Keep that exact historical argv readable.
        legacy = _watch_observer_duty_argv(
            watch_id, arm, rearm_generation=accepted_generation,
            include_rearmed_from=False)
        if legacy != current:
            variants.append(legacy)
        return variants
    except (TypeError, ValueError, OverflowError):
        return None


def _ensure_watch_observers(*, service_current=None, entries=None):
    root = _watch_root()
    transports = {}
    from recipient_lifetime import RecipientCensus
    recipient_census = RecipientCensus()
    try:
        for watch_id, _snapshot_arm in (_watch_entries(root) if entries is None else entries):
            paths = _watch_paths(watch_id, root)
            if paths.receipt.exists():
                continue
            # The pass snapshot selects work only. Re-read the current arm so
            # replacement and reattachment use the latest sealed watch intent.
            arm = _read_json(paths.arm)
            if not isinstance(arm, dict) or arm.get("watch_id") != watch_id:
                continue
            if paths.receipt.exists():
                continue
            if _settle_ended_seatless_recipient(
                    watch_id, arm, root, census=recipient_census):
                continue
            if _settle_terminal_watch_if_unobserved(watch_id, arm, root):
                continue
            if _watcher_present(arm):
                binding = peer_obligations.validate_service_binding(service_current)
                watcher = arm.get("watcher") or {}
                agent = arm.get("agent") or {}
                if not binding or not watcher.get("pid_start"):
                    continue
                accepted_argvs = _watch_observer_takeover_argvs(watch_id, arm)
                if not accepted_argvs:
                    continue
                replaced = peer_obligations.replace_managed_observer(
                    watcher.get("pid"), watcher.get("pid_start"),
                    _watch_paths(watch_id, root).lock,
                    "utilities/peer-steward.py",
                    accepted_argvs[0],
                    binding, lock_fd_option="--lock-fd",
                    exact_arguments=True, argument_variants=accepted_argvs[1:],
                )
                if replaced != "replaced":
                    continue
            server = (arm.get("server") or "default")
            agent = arm.get("agent") or {}
            socket_path = herdr_observation.socket_path_for_endpoint(server)
            key = (server, socket_path)
            if socket_path and key not in transports:
                transports[key] = herdr_observation.ObservationTransport(
                    server, socket_path, timeout=min(0.5, _HERDR_GET_TIMEOUT_SECONDS))
            transport = transports.get(key)
            view = transport.observe() if transport is not None else None
            terminal_reason = _watch_terminal_from_view(view, agent, server, transport)
            if (terminal_reason and _settle_terminal_watch_if_unobserved(
                    watch_id, arm, root, terminal_reason)):
                continue
            if not _watcher_present(arm):
                _spawn_watch_observer(watch_id, arm, service_current=service_current,
                                      recipient_census=recipient_census)
    finally:
        for transport in transports.values():
            transport.close()


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


def _watch_notice_pending(watch_id, recipient):
    """A queued exact native notice owns the display/ACK path for this receipt."""
    try:
        ref = f"peer-watch:{watch_id}"
        return any(ref in (row.get("refs") or ())
                   for row in peer_message._pending_rows(recipient=recipient))
    except Exception:
        return False


def cmd_status(args):
    root = _watch_root()
    entries = _watch_entries(root)
    if args.watch:
        entries = [(w, a) for w, a in entries if w == args.watch]
    rows = [_status_entry(w, a, root) for w, a in entries]
    if args.undelivered:
        sid, harness = _current_session_identity()
        rows = [
            row for row in rows
            if row["state"] == "receipt"
            and (row.get("recipient") or {}).get("session_id") == sid
            and (row.get("recipient") or {}).get("harness") == harness
            and not _watch_notice_pending(row["watch_id"], row.get("recipient") or {})
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
        if paths.receipt.exists():
            print(f"watch_id={watch_id} state=already-done receipt={paths.receipt}")
            return 0
        observation = _read_json(paths.observation) or {}
        if (observation.get("state") == "terminal_pending"
                and observation.get("reason") == "steward-exited"):
            print(f"watch_id={watch_id} state=pending reason=steward-exited receipt={paths.receipt}")
            return 0
        print(f"watch_id={watch_id} state=rearmed receipt={paths.receipt}")
        print(steward_fields(
            arm.get("wake"), watch_id, agent_home=Path(__file__).resolve().parents[1],
            arms_hook=False, timeout_ms=arm.get("timeout"),
        ))
        return 0
    print(f"watch_id={watch_id} state=pending reason=observer-restart-unavailable receipt={paths.receipt}")
    return 0


def cmd_ack(args):
    """Publish or validate the shared exact-recipient ACK before settling its ref."""
    session_id, harness = _current_session_identity()
    import dispatch_seat_handover as handover
    outcome = handover.acknowledge_watch(args.watch_id, harness, session_id, args.carrier)
    if outcome == "created":
        peer_message.settle_watch_transfers(args.watch_id, harness, session_id)
        print(f"watch_id={args.watch_id} ack=created carrier={args.carrier}")
    elif outcome == "already":
        peer_message.settle_watch_transfers(args.watch_id, harness, session_id)
        print(f"watch_id={args.watch_id} ack=already")
    elif outcome == "not-recipient":
        print(f"watch_id={args.watch_id} ack=not-recipient")
    else:
        print(f"watch_id={args.watch_id} ack=failed reason=unresolved-recipient", file=sys.stderr)
    return 0

_PROMPT_VERIFY_TIMEOUT_MS = 8000      # bounded native /clear command invocation
_PROMPT_EXIT = {"true": 0, "failed": 1, "queued": 3, "unverified": 5}
_PROMPT_LEDGER_STATUS = {"true": "sent", "failed": "failed", "queued": "unknown", "unverified": "unknown"}


def _agent_state(target):
    """(9) state word and pane id for `target` via one `herdr agent get`
    (never raises). The pane is what the ledger keeps: herdr's own server log
    records `cli:agent:prompt` with no target and no caller (measured
    2026-09-06, 324 such rows), so the wrapper's row is the only attribution."""
    state, agent, _code, _reason = _interpret_payload(_run_herdr_get(target), target)
    pane = agent.get("pane") if isinstance(agent, dict) else None
    return state, (pane if pane and pane != "-" else None)


def _peer_reference_access_evidence(target, harness, session_id, pane):
    """Observe the one supported model-read path: this host's exact Codex session.

    Herdr supplies the verified runtime/session binding; its foreground process
    must also match a live local PID and namespace. The Codex adapter declaration
    supplies the read-everywhere sandbox contract. Other runtimes and incomplete
    observations keep the complete body on the inline delivery path.
    """
    recipient = {"harness": harness, "session_id": session_id}
    if (harness != "codex" or not peer_message.usable_session_id(session_id)
            or not isinstance(pane, str) or not pane
            or not peer_message._codex_model_read_access(recipient)):
        return None
    try:
        from harness_capabilities import capabilities
        if capabilities("codex")["session_identity"]["herdr_session_id"] != "verified":
            return None
        _state, first, _code, _reason = _retire_target(target)
        if (first.get("harness") != harness or first.get("session_id") != session_id
                or first.get("pane") != pane):
            return None
        foreground = _retire_foreground(pane, harness)
        if not isinstance(foreground, dict):
            return None
        namespace = os.readlink(f"/proc/{foreground['pid']}/ns/pid")
        _state, second, _code, _reason = _retire_target(target)
        if (second.get("harness") != harness or second.get("session_id") != session_id
                or second.get("pane") != pane):
            return None
        evidence = {
            "schema": "peer-read-access-observation-v1",
            "observed_at": time.monotonic(),
            "recipient": {"harness": harness, "session_id": session_id, "pane_id": pane},
            "local_process": {"pid": foreground["pid"], "start": foreground["start"],
                              "argv": foreground["argv"], "pid_namespace": namespace},
        }
        return evidence if peer_message._reference_access_evidence_valid(evidence, recipient) else None
    except Exception:
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


def _claude_prompt_foreground_proof(pane, expected_sid):
    """Bind the pane leader to Claude's exact registered SID, PID birth and namespace."""
    info = _retire_pane_info(pane)
    if not isinstance(info, dict) or info.get("pane_id") != pane:
        return None
    group = info.get("foreground_process_group_id")
    leaders = [item for item in info.get("foreground_processes", ())
               if isinstance(item, dict) and item.get("pid") == group]
    if len(leaders) != 1:
        return None
    item = leaders[0]
    pid, argv = item.get("pid"), item.get("argv")
    if (type(pid) is not int or pid <= 0 or pid == info.get("shell_pid")
            or not isinstance(argv, list) or not argv
            or any(not isinstance(arg, str) for arg in argv)
            or os.path.basename(argv[0]) != "claude"):
        return None
    try:
        proc = Path(f"/proc/{pid}")
        stat_fields = (proc / "stat").read_text().rsplit(") ", 1)[1].split()
        start = stat_fields[19]
        process_group = int(stat_fields[2])
        comm = (proc / "comm").read_text().strip()
        namespace = os.readlink(proc / "ns/pid")
        current_namespace = os.readlink("/proc/self/ns/pid")
    except (OSError, ValueError, IndexError):
        return None
    if (comm != "claude" or process_group != group or not start.isdigit()
            or namespace != current_namespace or _proc_start_ticks(pid) != start):
        return None
    session_id = _proven_session([{"pid": pid}], "claude")
    if session_id != expected_sid:
        return None
    proof = {"pid": pid, "start": start, "pid_namespace": namespace,
             "session_id": session_id}
    try:
        if (_proc_start_ticks(pid) != start
                or os.readlink(proc / "ns/pid") != namespace
                or _proven_session([{"pid": pid}], "claude") != expected_sid):
            return None
    except OSError:
        return None
    return proof


def _claude_idle_prompt_guard(target, expected_sid, expected_pane, expected_foreground=None):
    """Prove the exact idle Claude TUI and empty composer before one prompt send."""
    state, ident, _code, reason = _retire_target(target)
    if (reason or ident.get("harness") != "claude"
            or ident.get("session_id") != expected_sid
            or ident.get("pane") != expected_pane):
        return reason or "target-identity-changed", None
    current, identity_error = _prompt_current_candidate(
        expected_pane, expected_harness="claude", expected_sid=expected_sid,
        require_process_session=True, target_observation=(state, ident))
    if current is None:
        return identity_error or "target-identity-unverified", None
    if state not in ("idle", "done"):
        return "native-state-" + str(state), None
    current_state, current_pane = _agent_state(target)
    if current_state not in ("idle", "done") or current_pane != expected_pane:
        return "target-state-changed", None
    readiness = _pane_readiness(target, current_state, expected_harness="claude",
                                expected_sid=expected_sid, expected_pane=expected_pane,
                                purpose="input", target_observation=(current_state, ident),
                                native_session_id=current["_native_session_id"])
    if readiness.state != "ready":
        return readiness.reason or "target-identity-unverified", None
    proof = _claude_prompt_foreground_proof(expected_pane, expected_sid)
    if proof is None:
        return "foreground-unverified", None
    if expected_foreground is not None and proof != expected_foreground:
        return "target-process-changed", None
    lines = _read_screen(expected_pane)
    if lines is None:
        return "screen-unavailable", None
    screen_reason = _screen_ready("claude", lines)
    if screen_reason:
        return screen_reason, None
    return None, proof


def _foreground_process_proof(expected_harness, expected_pane):
    """Capture the exact current foreground TUI process identity for one pane."""
    foreground = _retire_foreground(expected_pane, expected_harness, strict=True)
    if foreground is None:
        return None
    try:
        namespace = os.readlink(f"/proc/{foreground['pid']}/ns/pid")
        if namespace != os.readlink("/proc/self/ns/pid"):
            return None
    except OSError:
        return None
    return {"schema": "peer-first-input-process-v1", "harness": expected_harness,
            "pane_id": expected_pane, "pid": foreground["pid"],
            "start": str(foreground["start"]), "pid_namespace": namespace}


def _first_input_foreground_proof(target, expected_harness, expected_pane):
    """Capture one SID-less TUI's exact foreground process at admission time."""
    state, ident, _code, reason = _retire_target(target)
    if (reason or ident.get("harness") != expected_harness
            or ident.get("session_id") not in (None, "", "-")
            or ident.get("pane") != expected_pane
            or state in {"herdr-unavailable", "agent-not-found", "unknown"}):
        return None
    return _foreground_process_proof(expected_harness, expected_pane)


def _sidless_idle_prompt_guard(target, expected_harness, expected_pane,
                               expected_foreground=None):
    """Prove an empty, idle SID-less TUI and its stable foreground process."""
    state, ident, _code, reason = _retire_target(target)
    if (reason or ident.get("harness") != expected_harness
            or ident.get("session_id") not in (None, "", "-")
            or ident.get("pane") != expected_pane):
        return reason or "target-identity-changed", None
    if state not in ("idle", "done"):
        return "native-state-" + str(state), None
    current_state, current_pane = _agent_state(target)
    if current_state not in ("idle", "done") or current_pane != expected_pane:
        return "target-state-changed", None
    readiness = _pane_readiness(target, current_state, expected_harness=expected_harness,
                                expected_sid=None, expected_pane=expected_pane,
                                purpose="input")
    if readiness.state != "ready":
        return readiness.reason or "target-identity-unverified", None
    proof = _first_input_foreground_proof(target, expected_harness, expected_pane)
    if proof is None:
        return "foreground-unverified", None
    if expected_foreground is not None and not peer_message.first_input_process_matches(
            expected_foreground, proof):
        return "target-process-changed", None
    lines = _read_screen(expected_pane)
    if lines is None:
        return "screen-unavailable", None
    screen_reason = _screen_ready(expected_harness, lines)
    if screen_reason:
        return screen_reason, None
    return None, proof


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


def _form_open(target):
    """Compatibility wrapper for the shared observed-harness screen classifier."""
    harness, _sid, _name = _resolve_target(target)
    return _blocking_screen_for_target(target, harness) is not None


def _blocking_screen_for_target(target, harness, *, window=15):
    try:
        proc = subprocess.run(_herdr_argv("agent", "read", target, "--source", "visible"),
                              capture_output=True, text=True, timeout=_herdr_get_timeout())
    except (OSError, subprocess.SubprocessError):
        return None
    return _blocking_screen(harness, _screen_lines(proc.stdout or ""), window=window)


def _bottom_form_tokens(target, window=15):
    """Compatibility wrapper for the shared classifier's trailing-row window."""
    harness, _sid, _name = _resolve_target(target)
    return _blocking_screen_for_target(target, harness, window=window) is not None


def _pane_readiness(target, state, *, expected_harness=None, expected_sid=None, expected_pane=None,
                    purpose="completion", target_observation=None, pane_info=None,
                    native_session_id=None, registry_append_only=False, read_context=None,
                    server=None, authority_out=None, selected_authority=None):
    """Use the shared projection with exact native identity and registered bindings.

    Before a harness's first input herdr reports no session, and for a Codex TUI
    on the shared app-server it may never report one. The session the pane's
    foreground process proves is then the pane's own (Codex 0.162 opens its
    thread at start) and keeps the bound-work checks; a pane with neither is
    exact by pane and shell birth: nothing can be bound to it, so its native
    turn decides. A caller holding a session needs that proof to name it."""
    try:
        if authority_out is not None:
            authority_out.update(rows=[], handover_keys=[])
        if target_observation is None:
            observed_state, ident, _code, _reason = _retire_target(target)
        else:
            observed_state, ident = target_observation
        harness, sid, pane = ident.get("harness"), ident.get("session_id"), ident.get("pane")
        info = (pane_info if pane_info is not None else
                _retire_pane_info(pane) if pane and pane != "-" else None)
        if sid == "-" and native_session_id is None and isinstance(info, dict):
            proven = _proven_session(info["foreground_processes"], harness)
            if proven and expected_sid in {None, "-", proven}:
                sid, expected_sid = proven, None
        if isinstance(native_session_id, str) and peer_message.usable_session_id(native_session_id):
            sid = native_session_id
        foreground = (_retire_foreground(pane, harness, strict=False, info=info)
                      if native_session_id is not None and isinstance(info, dict) else None)
        native_source = "herdr-corroboration"
        native_turn = observed_state
        if native_session_id is not None:
            native_source = "native-foreground-identity"
        native_ident = {"server": server or _HERDR_SESSION or "default",
                        "pane": pane, "harness": harness, "session_id": sid}
        native = (_native_opencode_turn(native_ident) if native_session_id is None else
                  _native_opencode_turn(native_ident, foreground=foreground)
                  if foreground is not None else None)
        if isinstance(native, dict) and native.get("status") in {"idle", "working", "blocked"}:
            native_turn = native["status"]
            native_source = "opencode-session-status"
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
            and (native_session_id is None or foreground is not None)
            and (expected_harness is None or harness == expected_harness)
            and (expected_sid is None or sid == expected_sid)
            and (expected_pane is None or pane == expected_pane)
        )
        execution_ids = peer_obligations.execution_attempts_for_processes(
            info.get("foreground_processes", ()) if isinstance(info, dict) else ())
        bound, binding_state = (((), "observed") if unbound and not execution_ids else
                                peer_obligations.bound_work_for_pane(
                                    pane or "", harness or "", sid or "",
                                    execution_attempt_ids=execution_ids,
                                    include_parent=purpose != "input",
                                    read_context=read_context,
                                    registry_append_only=(registry_append_only or bool(
                                        read_context and read_context.registry_append_only)),
                                    authority_out=authority_out,
                                    selected_rows=(selected_authority.get("rows")
                                                   if isinstance(selected_authority, dict) else None),
                                    expected_handover_keys=(selected_authority.get("handover_keys")
                                                           if isinstance(selected_authority, dict)
                                                           else None)))
        return peer_obligations.pane_readiness(
            server=server or _HERDR_SESSION or "default", pane=pane or "",
            harness=harness or "", session_id="" if unbound else sid or "", pid_birth=birth,
            identity_verified=exact, native_turn=native_turn,
            bound_work=bound, bindings_state=binding_state,
            provenance=(("target", target), ("observed_state", observed_state),
                        ("native_status_source", native_source)),
            purpose=purpose,
        )
    except Exception:
        return peer_obligations.pane_readiness(
            server=server or _HERDR_SESSION or "default", pane="", harness="",
            session_id="", pid_birth="", identity_verified=False,
            native_turn="unknown", bindings_state="unknown",
            provenance=(("target", target),))


def _watch_shared_readiness(target, native_state, arm, expected, server):
    """Consume a cached runner result and recheck only current target lineage."""
    identity = _watch_readiness_identity(arm, native_state)
    if identity is None:
        return SimpleNamespace(state="unknown", reason="shared-readiness-identity-unavailable")
    try:
        result = shared_readiness.target_result(
            peer_obligations.ObligationStore().root, identity)
    except (OSError, ValueError, RuntimeError):
        result = None
    if result is None:
        return SimpleNamespace(state="unknown", reason="shared-readiness-unavailable")
    return _watch_readiness_result(target, native_state, identity, result, server)


def _watch_selected_jobs_path():
    """Return the selected dispatch registry used by all readiness authority."""
    return resolve_dispatch_state_root(resolve_agent_home()) / "jobs.log"


_WATCH_SERVER_ENDPOINT_PREFIX = "server:"


def _watch_selected_readiness_endpoint(arm, server=None):
    """Return this watch's selected endpoint, independent of the runner's env."""
    selected_server = server or (arm.get("server") if isinstance(arm, dict) else None) or "default"
    if isinstance(arm, dict) and "readiness_endpoint" in arm:
        endpoint = arm.get("readiness_endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            return None
        if endpoint.startswith(_WATCH_SERVER_ENDPOINT_PREFIX):
            expected = (_WATCH_SERVER_ENDPOINT_PREFIX
                        + herdr_observation.endpoint_name(selected_server))
            return endpoint if endpoint == expected else None
        return endpoint
    endpoint = herdr_observation.socket_path_for_endpoint(selected_server)
    if endpoint:
        return endpoint
    return (_WATCH_SERVER_ENDPOINT_PREFIX
            + herdr_observation.endpoint_name(selected_server))


def _watch_readiness_identity_matches_arm(identity, arm, native_state):
    """Validate a watch-carried endpoint identity against its immutable target."""
    if not isinstance(identity, dict) or not isinstance(arm, dict):
        return False
    agent = arm.get("agent") or {}
    server = arm.get("server") or "default"
    session_id = agent.get("session_id") or ""
    if session_id == "-":
        session_id = ""
    endpoint = identity.get("endpoint")
    if (not isinstance(endpoint, str) or not endpoint
            or identity.get("server") != server
            or identity.get("pane") != agent.get("pane")
            or identity.get("harness") != agent.get("harness")
            or identity.get("session_id") != session_id
            or identity.get("native_state") != native_state
            or identity.get("purpose") != "completion"):
        return False
    if endpoint.startswith(_WATCH_SERVER_ENDPOINT_PREFIX):
        if endpoint != (_WATCH_SERVER_ENDPOINT_PREFIX
                        + herdr_observation.endpoint_name(server)):
            return False
    if ("readiness_endpoint" in arm
            and endpoint != _watch_selected_readiness_endpoint(arm, server)):
        return False
    pane_identity = arm.get("pane_identity") or arm.get("foreground_identity") or {}
    shell_pid = pane_identity.get("shell_pid")
    shell_start = str(pane_identity.get("shell_start") or "")
    if shell_pid is not None:
        try:
            if (int(identity.get("shell_pid")) != int(shell_pid)
                    or str(identity.get("shell_start") or "") != shell_start):
                return False
        except (TypeError, ValueError):
            return False
    elif identity.get("shell_pid") is not None or identity.get("shell_start") not in {None, ""}:
        return False
    return True


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
                       and endpoint.startswith(_WATCH_SERVER_ENDPOINT_PREFIX)
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


def _watch_readiness_result(target, native_state, identity, result, server, *,
                            require_authority=False, authority_lock_held=False,
                            deadline=None):
    """Bind any runner result to the exact live pane and its native result."""
    state = result.get("state", "unknown")
    reason = result.get("reason", "shared-readiness-pending")
    if state != "ready":
        return SimpleNamespace(state=state, reason=reason)
    lineage = result.get("lineage")
    if not isinstance(lineage, dict):
        lineage = {"shell_pid": identity.get("shell_pid"),
                   "shell_start": identity.get("shell_start")}
    try:
        observed_state, current, _code, _detail = _retire_target(target, server=server)
        pane_info = _retire_pane_info(identity["pane"], server=server)
        shell_pid = int(lineage.get("shell_pid"))
        shell_start = str(lineage.get("shell_start") or "")
        current_pid = int(pane_info.get("shell_pid")) if isinstance(pane_info, dict) else 0
        current_start = str(_proc_start_ticks(current_pid) or "") if current_pid else ""
    except (OSError, TypeError, ValueError):
        return SimpleNamespace(state="unknown", reason="shared-readiness-lineage-unavailable")
    current_sid = (current.get("session_id") or "") if isinstance(current, dict) else ""
    if current_sid == "-":
        current_sid = ""
    expected_sid = identity.get("session_id", "")
    sidless_current = not current_sid and bool(expected_sid)
    if (observed_state not in {"idle", "done"}
            or not isinstance(current, dict)
            or current.get("pane") != identity.get("pane")
            or current.get("harness") != identity.get("harness")
            or (current_sid != expected_sid and not sidless_current)
            or shell_pid != current_pid or shell_start != current_start):
        return SimpleNamespace(state="unknown", reason="shared-readiness-identity-changed")
    current_identity = {
        "server": server, "pane": identity["pane"],
        "harness": identity["harness"],
        "session_id": expected_sid if sidless_current else current_sid,
        "pid_birth": f"{current_pid}:{current_start}",
    }
    if sidless_current and not require_authority:
        from peer_read_context import PeerReadContext
        readiness = _pane_readiness(
            target, native_state,
            expected_harness=identity["harness"], expected_sid=expected_sid,
            expected_pane=identity["pane"],
            target_observation=(observed_state, current), pane_info=pane_info,
            registry_append_only=True,
            read_context=PeerReadContext(registry_append_only=True), server=server)
        readiness_identity = dict(getattr(readiness, "identity", ()) or ())
        if not _watch_readiness_fields_match(current_identity, readiness_identity):
            return SimpleNamespace(state="unknown",
                                   reason="shared-readiness-identity-unverified")
        return SimpleNamespace(state=readiness.state, reason=readiness.reason,
                               identity=tuple(current_identity.items()))
    if require_authority:
        authority = result.get("authority")
        if not shared_readiness.valid_watch_authority(authority):
            return SimpleNamespace(state="unknown",
                                   reason="shared-readiness-authority-unavailable")

        def fresh_authority():
            from peer_read_context import PeerReadContext
            readiness = _pane_readiness(
                target, native_state,
                expected_harness=identity["harness"],
                expected_sid=identity["session_id"] or "-",
                expected_pane=identity["pane"],
                target_observation=(observed_state, current),
                pane_info=pane_info,
                registry_append_only=True,
                read_context=PeerReadContext(registry_append_only=True),
                selected_authority=authority,
                server=server)
            readiness_identity = dict(getattr(readiness, "identity", ()) or ())
            if not _watch_readiness_fields_match(current_identity, readiness_identity):
                return SimpleNamespace(state="unknown",
                                       reason="shared-readiness-identity-changed")
            return SimpleNamespace(state=readiness.state, reason=readiness.reason,
                                   identity=tuple(current_identity.items()))

        if authority_lock_held:
            return fresh_authority()
        try:
            with _watch_transition_authority_lock(identity, server, deadline=deadline):
                return fresh_authority()
        except (_WatchDeadlineExpired, _WatchTransitionLockBusy):
            raise
        except (OSError, ValueError, RuntimeError):
            return SimpleNamespace(state="unknown",
                                   reason="shared-readiness-authority-lock-unavailable")
    return SimpleNamespace(state="ready", reason=reason,
                           identity=tuple(current_identity.items()))


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
            shared_readiness.wake_runner(root)
            peer_obligations.ensure_normal_runner(reconciliation_required=True)
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


def _watch_transition_readiness(target, native_state, arm, expected, server,
                                watch_id, paths, *, authority_lock_held=False,
                                deadline=None):
    """Require a fresh runner authority read before this watch can publish ready.

    The long-lived target projection is only a trigger. The observer writes a
    token into its existing observation, while still owning the watch lock;
    the existing runner returns a separate small response after re-reading
    bound work and its mutable completion evidence in the shared pass.
    """
    identity = _watch_readiness_identity(arm, native_state)
    if identity is None:
        return SimpleNamespace(state="unknown", reason="shared-readiness-identity-unavailable")
    root = peer_obligations.ObligationStore().root
    observation = _read_json(paths.observation) or {}
    native = observation.get("native_wait") or {}
    if (native.get("state") != "result"
            or native.get("agent_state") != native_state):
        return SimpleNamespace(state="unknown", reason="shared-readiness-native-result-changed")
    request = observation.get("readiness_transition")
    consumer_start = process_start_ticks(os.getpid())
    consumer = {"pid": os.getpid(), "pid_start": str(consumer_start or "")}
    if not consumer["pid_start"]:
        return SimpleNamespace(state="unknown", reason="shared-readiness-consumer-unverified")

    def request_refresh():
        token = os.urandom(16).hex()
        request = {"token": token, "identity": identity, "consumer": consumer,
                   "requested_at": time.time()}
        updated = {**observation, "readiness_transition": request}
        updated.pop("readiness_contention", None)
        try:
            shared_readiness.clear_watch_transition(root, watch_id)
        except (OSError, ValueError):
            return SimpleNamespace(state="unknown",
                                   reason="shared-readiness-refresh-unavailable")
        _write_json_atomic(paths.observation, updated)
        try:
            shared_readiness.wake_runner(root)
            peer_obligations.ensure_normal_runner(reconciliation_required=True)
        except Exception:
            # A missing response stays pending and reuses this exact request.
            pass
        return SimpleNamespace(state="pending", reason="shared-readiness-revalidating")

    if (isinstance(request, dict) and request.get("identity") == identity
            and request.get("consumer") == consumer
            and isinstance(request.get("token"), str) and request.get("token")):
        fresh = shared_readiness.watch_transition_result(
            root, watch_id, identity, request["token"])
        if fresh is None:
            return SimpleNamespace(state="pending", reason="shared-readiness-revalidating")
        try:
            result = _watch_readiness_result(
                target, native_state, identity, fresh, server,
                require_authority=True, authority_lock_held=authority_lock_held,
                deadline=deadline)
        except _WatchDeadlineExpired:
            return SimpleNamespace(state=_WATCH_RECEIPT_EXPIRED,
                                   reason="watch-deadline")
        except _WatchTransitionLockBusy:
            if deadline is not None and time.monotonic() >= deadline:
                return SimpleNamespace(state=_WATCH_RECEIPT_EXPIRED,
                                       reason="watch-deadline")
            _watch_note_transition_lock_contention(root, watch_id, paths)
            return SimpleNamespace(state="pending",
                                   reason="shared-readiness-authority-lock-busy")
        if deadline is not None and time.monotonic() >= deadline:
            return SimpleNamespace(state=_WATCH_RECEIPT_EXPIRED,
                                   reason="watch-deadline")
        return result

    cached = _watch_shared_readiness(target, native_state, arm, expected, server)
    if cached.state != "ready":
        if cached.reason == "shared-readiness-unavailable":
            # A fresh native result can be the first accepted readiness duty
            # in this state root. Carry this watcher's selected endpoint and
            # identity into the existing runner's first classification pass.
            return request_refresh()
        return cached
    return request_refresh()


def _watch_publish_ready_receipt(target, native_state, arm, expected, server,
                                 watch_id, paths, receipt, *, deadline=None):
    """Refresh mutable authority and publish the receipt under its writer locks."""
    reason = "shared-readiness-authority-lock-unavailable"
    readiness_pending = False
    if deadline is not None and time.monotonic() >= deadline:
        return _WATCH_RECEIPT_EXPIRED
    try:
        identity = _watch_readiness_identity(arm, native_state)
        if identity is None:
            reason = "shared-readiness-identity-unavailable"
            raise ValueError(reason)
        with _watch_transition_authority_lock(identity, server, deadline=deadline):
            if deadline is not None and time.monotonic() >= deadline:
                return _WATCH_RECEIPT_EXPIRED
            readiness = _watch_transition_readiness(
                target, native_state, arm, expected, server,
                watch_id, paths, authority_lock_held=True, deadline=deadline)
            if deadline is not None and time.monotonic() >= deadline:
                return _WATCH_RECEIPT_EXPIRED
            if readiness.state != "ready":
                reason = readiness.reason or "shared-readiness-pending"
                readiness_pending = readiness.state == "pending"
            else:
                if deadline is not None and time.monotonic() >= deadline:
                    return _WATCH_RECEIPT_EXPIRED
                _write_json_atomic(paths.receipt, receipt)
                return True
    except _WatchDeadlineExpired:
        return _WATCH_RECEIPT_EXPIRED
    except _WatchTransitionLockBusy:
        if deadline is not None and time.monotonic() >= deadline:
            return _WATCH_RECEIPT_EXPIRED
        _watch_note_transition_lock_contention(
            peer_obligations.ObligationStore().root, watch_id, paths)
        return _WATCH_RECEIPT_CONTENDED
    except (OSError, ValueError, RuntimeError):
        if deadline is not None and time.monotonic() >= deadline:
            return _WATCH_RECEIPT_EXPIRED
    _write_watch_observation(paths, {
        **(_read_json(paths.observation) or {}),
        "state": "pending", "reason": reason, "observed_ts": _utc_now(),
    })
    return _WATCH_RECEIPT_PENDING if readiness_pending else False


_FLUSH_MAX_ROWS = 4
_FLUSH_ROW_TIMEOUT_S = 12
_FLUSH_STUCK_HOURS = 1.0


def _message_obligation_id(ref):
    return "message-" + ref


def _census_session_presence(census, session_id, server):
    """Return True/False only for a complete, well-shaped exact-endpoint census."""
    if (census is None or census.complete is not True
            or census.endpoint != herdr_observation.endpoint_name(server)
            or not isinstance(session_id, str) or not session_id):
        return None
    for pane in census.panes:
        session = pane.get("agent_session")
        if session is None:
            continue
        if not isinstance(session, dict):
            return None
        value = session.get("value")
        if value is None:
            continue
        if not isinstance(value, str):
            return None
        if value == session_id:
            return True
    return False


def _message_terminal_evidence(duty, row, observed, reason):
    """Prove only an exact recipient loss or death of its pane-bound process."""
    intent = duty.get("intent") or {}
    identity = intent.get("identity") or {}
    original = (row or {}).get("to") or intent.get("to") or {}
    server = identity.get("server") or "default"
    pane_bound = peer_message._valid_pane_bound_endpoint(original)
    recipient = peer_message.pending_delivery_recipient(row) if row else None
    recipient_binding = (peer_message.pending_delivery_binding(row, include_acked=True)
                         if row else None)
    if (recipient_binding is None and row
            and not any(isinstance(ref, str) and ref.startswith("peer-watch:")
                        for ref in row.get("refs") or ())
            and pane_bound):
        recipient_binding = {"recipient": dict(original), "watches": []}
    if recipient_binding is None:
        return None
    if pane_bound:
        proof = peer_message.pending_first_input_process(row)
        if proof is None:
            return None
        try:
            namespace = os.readlink("/proc/self/ns/pid")
            if proof.get("pid_namespace") != namespace:
                return None
            from dispatch_contract import process_identity_disposition
            disposition = process_identity_disposition(proof["pid"], proof["start"])
        except (OSError, ValueError, TypeError, KeyError):
            return None
        if disposition != "dead":
            return None
        evidence = {"kind": "first-input-process-dead", "server": server,
                    "process": proof, "process_disposition": disposition,
                    "recipient_binding": recipient_binding}
        if recipient is not None:
            census = herdr_observation.pane_census(session=server)
            presence = _census_session_presence(census, recipient.get("session_id"), server)
            if presence is not False:
                return None
            evidence.update({"harness": recipient.get("harness"),
                             "recipient_session_id": recipient.get("session_id"),
                             "recipient_session_absent": True,
                             "census_complete": True, "session_absent": True,
                             "census_endpoint": census.endpoint,
                             "census_observed_at": census.observed_at})
        return evidence

    if recipient is None:
        return None
    expected_pane = identity.get("pane") or original.get("pane_id")
    if (reason is None and observed.get("harness") == recipient.get("harness")
            and observed.get("session_id") == recipient.get("session_id")
            and (not expected_pane or observed.get("pane") == expected_pane)):
        return None
    census = herdr_observation.pane_census(session=server)
    presence = _census_session_presence(census, recipient.get("session_id"), server)
    if presence is not False:
        return None
    return {"kind": "recipient-session-absent", "server": server,
            "harness": recipient.get("harness"),
            "session_id": recipient.get("session_id"),
            "census_complete": True, "session_absent": True,
            "census_endpoint": census.endpoint,
            "census_observed_at": census.observed_at,
            "recipient_binding": recipient_binding}


def _schedule_message_obligation(row, target, pane=None, *, delay_notice_for=None, handoff_acceptance=False):
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
    if row.get("producer_binding") is not None:
        intent["producer_binding"] = dict(row["producer_binding"])
    if handoff_acceptance:
        intent["handoff_acceptance"] = True
    try:
        duty = peer_obligations.ObligationStore().create(
            _message_obligation_id(row["ref"] + ("-accept" if handoff_acceptance else "")), "message", identity, intent)
        peer_obligations.ensure_normal_runner()
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
            peer_obligations.ensure_normal_runner()
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
        peer_obligations.ensure_normal_runner()
        return duty
    except (OSError, ValueError, OverflowError, peer_obligations.ObligationError):
        return None


def _ensure_sender_delay_notice_if_due(row):
    """Retain the existing one-hour sender notice for an unresolved pending row."""
    try:
        age = max(0.0, time.time() - float(row.get("created") or time.time()))
    except (TypeError, ValueError):
        age = 0.0
    if age >= _FLUSH_STUCK_HOURS * 3600:
        return _ensure_sender_delay_notice(row)
    return None


def _flush_pending_for_target(target, t_harness, t_sid, entry_state, skip=None,
                              expected_pane=None):
    """Drain only the exact Codex native queue; other carriers own their rows."""
    stuck = []
    if not t_sid or t_harness != "codex":
        return 0, stuck
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
            age_h = max(0.0, (now - float(row.get("created") or now)) / 3600)
        except (TypeError, ValueError):
            age_h = 0.0
        if age_h >= _FLUSH_STUCK_HOURS:
            sender = ((row.get("from") or {}).get("name")
                      or (row.get("from") or {}).get("session_id") or "-")
            stuck.append((age_h, sender, row.get("ref")))
            _ensure_sender_delay_notice(row)
    flushed = 0
    for row in sorted(rows, key=lambda item: item.get("created") or 0)[:_FLUSH_MAX_ROWS]:
        try:
            current_harness, current_sid, _name = _resolve_target(target)
            if (current_harness, current_sid) != (t_harness, t_sid):
                break
            if expected_pane:
                _state, current_ident, _code, _reason = _retire_target(target)
                if current_ident.get("pane") != expected_pane:
                    break
            result = peer_message.deliver_pending_codex(row["ref"], timeout=0.25)
            if result.get("status") == "received":
                flushed += 1
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    return flushed, stuck

def _resume_message_obligation(duty, store, *, read_context=None):
    intent = duty.get("intent") or {}
    observation = duty.get("observation") or {}
    duty_id = duty["id"]

    def checkpoint(**changes):
        nonlocal duty
        # This duty came from the current read pass. If the requested checkpoint
        # is already present, skip the lock and record reread as well as the write.
        # The message-specific CAS preserves concurrent receipts and terminal rows.
        if all(duty.get(key) == value for key, value in changes.items()):
            return duty
        duty = store.update_message_checkpoint(
            duty_id, expected_revision=duty.get("revision"),
            skip_unchanged=True, **changes)
        return duty

    target = intent.get("target")
    # Later checkpoints replace the observation but carry the minted delay
    # notice ref as "ref"; reading it keeps one notice per overdue message.
    ref = intent.get("ref") or observation.get("transfer_ref") or observation.get("ref")
    delay_for = intent.get("delay_notice_for") or ""
    if delay_for and not ref:
        body = intent.get("body")
        sender, recipient = intent.get("from") or {}, intent.get("to") or {}
        if not isinstance(body, str) or not body:
            checkpoint(state="unknown", observation={"reason": "delay-notice-body-unavailable"})
            return
        try:
            _text, ref = peer_message.prepare_peer_message(
                body, sender, recipient, defer=True,
                refs=["delay-notice:" + delay_for, delay_for],
                receipt="sender-delay-notice")
            observation = {**observation, "transfer_ref": ref}
            duty = checkpoint(observation=observation)
        except (OSError, ValueError):
            checkpoint(state="unknown", observation={"reason": "delay-notice-payload-unavailable"})
            return
    if not isinstance(ref, str) or not ref:
        checkpoint(state="unknown", observation={"reason": "message-ref-unavailable"})
        return

    roots = (read_context.transfer_roots(peer_message.steward_marker_roots)
             if read_context is not None else None)

    def read_pending():
        return (peer_message._read_pending(ref, roots=roots) if roots is not None
                else peer_message._read_pending(ref))

    try:
        row = read_pending()
    except (OSError, ValueError):
        checkpoint(state="unknown", observation={"ref": ref, "reason": "pending-row-unreadable"})
        return
    if row is None:
        if not intent.get("handoff_acceptance"):
            recovered = peer_message.recover_missing_pending_receipt(
                ref, intent, roots=roots, read_context=read_context)
            if recovered is not None:
                received, recipient = recovered
                if store.acknowledge_message_receipt(duty_id, received, recipient):
                    return
        checkpoint(state="unknown", observation={"ref": ref, "reason": "pending-row-missing"})
        return
    if intent.get("handoff_acceptance"):
        accepted = row.get("acceptance") or {}
        if not accepted.get("read_and_accepted"):
            checkpoint(state="pending", observation={"reason": "awaiting-agent-acceptance"})
            return
        if not accepted.get("settled"):
            import session_tidy as tidy
            import session_tidy_runner as runner
            context = row.get("handoff") or {}
            successor = context.get("successor") or {}
            global _HERDR_SESSION
            old_server = _HERDR_SESSION
            _HERDR_SESSION = None if successor.get("server") == "default" else successor.get("server")
            try:
                runner.accept_handoff(ref, (tidy.Seat("pane", "accepted-transfer", successor["pane"]),
                    successor["harness"], successor["session_id"]), steward=sys.modules[__name__])
            except (OSError, ValueError, KeyError):
                checkpoint(state="pending", observation={"reason": "handoff-acceptance-unsettled"})
                return
            finally:
                _HERDR_SESSION = old_server
        checkpoint(state="complete", result="accepted", delivery="scheduled", cleanup="complete",
                   observation={"ref": ref, "reason": "handoff-acceptance-settled"})
        return
    if peer_obligations.registered_delivery_settled(row):
        checkpoint(state="complete", delivery="not-required", cleanup="complete",
                   observation={"ref": ref, "reason": "dispatch-notice-consumed"})
        return
    if row.get("state") == "received":
        peer_message.acknowledge_message_obligations(row)
        current = store.get(duty_id)
        if current and current.get("state") in {"complete", "cancelled"}:
            return
        checkpoint(state="unknown", observation={
            "ref": ref, "reason": "received-binding-unverified"})
        return
    identity = (duty.get("intent") or {}).get("identity") or {}
    # Identity is held outside mutable observations. The accepted target stays
    # nameable only while its exact harness/session/pane still resolves.
    recipient = row.get("to") or {}
    server = identity.get("server") or "default"
    old_server = _HERDR_SESSION
    _HERDR_SESSION = None if server == "default" else server
    try:
        state, ident, _code, reason = _retire_target(target or recipient.get("name") or "")
        expected_pane = identity.get("pane") or ""
        pane_bound = peer_message._valid_pane_bound_endpoint(recipient)
        terminal_evidence = _message_terminal_evidence(duty, row, ident, reason)
        if terminal_evidence is not None:
            outcome = peer_message.cancel_message_obligation(
                ref, duty_id=duty_id, expected_revision=duty.get("revision"),
                identity=identity, message=row, evidence=terminal_evidence)
            if outcome == "cancelled":
                return
            if outcome == "received":
                return
            if outcome in {"changed", "missing"}:
                return
            if outcome == "inflight":
                checkpoint(state="unknown", observation={"ref": ref,
                           "reason": "delivery-inflight"})
                return
        if row.get("state") in {"queued", "unverified"}:
            # Keep the acceptance receipt intact when a fresh target check is
            # still inconclusive. It is not a receive, and another pass will
            # reobserve the target without rewriting this unchanged evidence.
            checkpoint(state="unknown", observation={"ref": ref,
                       "transport_state": row["state"],
                       "reason": row.get("receipt", "delivery-unconfirmed")})
            return
        if (reason or ident.get("harness") != recipient.get("harness")
                or (expected_pane and ident.get("pane") != expected_pane)
                or (not pane_bound and ident.get("session_id") != recipient.get("session_id"))):
            checkpoint(state="unknown", observation={"ref": ref,
                       "reason": reason or "target-identity-changed",
                       "observed_pane": ident.get("pane", "")})
            return
        pinned_pane = expected_pane or ident.get("pane") or ""
        current, current_error = _prompt_current_candidate(
            pinned_pane, expected_harness=recipient.get("harness"),
            expected_sid=None if pane_bound else recipient.get("session_id"),
            require_process_session=not pane_bound,
            target_observation=(state, ident))
        if current is None:
            _ensure_sender_delay_notice_if_due(row)
            checkpoint(state="unknown", observation={"ref": ref,
                       "reason": current_error or "target-identity-unverified",
                       "observed_pane": pinned_pane})
            return
        ident = current["_identity"]
        state = current["state"]
        if pane_bound:
            observed_sid = current.get("session_id")
            if observed_sid in (None, "", "-"):
                checkpoint(state="pending", observation={"ref": ref, "reason": "target-session-unresolved",
                                                          "observed_pane": ident.get("pane", "")})
                return
            original_process = peer_message.pending_first_input_process(row)
            if original_process is None:
                _ensure_sender_delay_notice_if_due(row)
                checkpoint(state="unknown", observation={"ref": ref, "reason": "target-process-proof-unknown",
                                                           "observed_pane": ident.get("pane", "")})
                return
            current_process = _foreground_process_proof(
                recipient.get("harness"), expected_pane or recipient.get("pane_id"))
            if current_process is None:
                _ensure_sender_delay_notice_if_due(row)
                checkpoint(state="unknown", observation={"ref": ref, "reason": "target-process-proof-unknown",
                                                           "observed_pane": ident.get("pane", "")})
                return
            if not peer_message.first_input_process_matches(original_process, current_process):
                _ensure_sender_delay_notice_if_due(row)
                checkpoint(state="unknown", observation={"ref": ref, "reason": "target-process-changed",
                                                           "observed_pane": ident.get("pane", "")})
                return
            bound = peer_message.bind_pending_recipient(
                ref, {"harness": recipient["harness"], "session_id": observed_sid,
                      "pane_id": expected_pane or recipient.get("pane_id")},
                process_proof=current_process)
            if bound is None:
                _ensure_sender_delay_notice_if_due(row)
                checkpoint(state="unknown", observation={"ref": ref, "reason": "target-process-changed",
                                                           "observed_pane": ident.get("pane", "")})
                return
            recipient = peer_message.pending_delivery_recipient(bound)
        readiness = _pane_readiness(pinned_pane, state,
                                    expected_harness=recipient.get("harness"),
                                    expected_sid=recipient.get("session_id"), purpose="input",
                                    expected_pane=pinned_pane,
                                    target_observation=(state, ident),
                                    native_session_id=current.get("_native_session_id") or "",
                                    read_context=read_context)
        if readiness.state == "unknown" or readiness.reason.startswith("bound-registered-work"):
            if readiness.state != "ready":
                checkpoint(state="unknown" if readiness.state == "unknown" else "pending",
                           observation={"ref": ref, "reason": readiness.reason,
                                        "state": readiness.state})
                return
        if pane_bound:
            # Recheck the original PID/start/namespace at the external-send
            # boundary, after the later SID binding and readiness observation.
            latest = _foreground_process_proof(
                recipient.get("harness"), expected_pane or recipient.get("pane_id"))
            original_process = peer_message.pending_first_input_process(row)
            if (latest is None or original_process is None
                    or not peer_message.first_input_process_matches(original_process, latest)):
                _ensure_sender_delay_notice_if_due(row)
                checkpoint(state="unknown", observation={"ref": ref,
                           "reason": "target-process-changed" if latest is not None
                           else "target-process-proof-unknown", "observed_pane": expected_pane})
                return
        else:
            latest, latest_error = _prompt_current_candidate(
                pinned_pane, expected_harness=recipient.get("harness"),
                expected_sid=recipient.get("session_id"), require_process_session=True)
            if latest is None:
                _ensure_sender_delay_notice_if_due(row)
                checkpoint(state="unknown", observation={"ref": ref,
                           "reason": latest_error or "target-identity-unverified",
                           "observed_pane": pinned_pane})
                return
        if recipient.get("harness") == "codex":
            peer_message.deliver_pending_codex(ref, timeout=0.25)
        # Claude hook context and OpenCode's idle plugin own native delivery.
        # This obligation runner never types or invents a second wake process.
        try:
            current = read_pending()
        except (OSError, ValueError):
            current = None
        if current and current.get("state") == "received":
            checkpoint(state="complete", result="received", delivery="acknowledged", cleanup="complete",
                       observation={"ref": ref, "reason": "exact-peer-ref"})
        else:
            age = max(0.0, time.time() - float((current or row).get("created") or time.time()))
            if age >= _FLUSH_STUCK_HOURS * 3600:
                _ensure_sender_delay_notice(current or row)
            checkpoint(state="pending", observation={
                "ref": ref, "state": (current or row).get("state"),
                "reason": (current or row).get("receipt", "awaiting-receipt")})

    finally:
        _HERDR_SESSION = old_server


def _resume_native_receiving_turn(duty, store, row, record, recipient_sid, storage_recipient):
    """Settle or resume one ACKed Codex hook turn through the existing queue."""
    from dispatch_completion_join import current_attempt_row
    acked_by = record.get("acked_by")
    if not (isinstance(acked_by, str) and acked_by.startswith((
            "codex-native-prompt:", "codex-native-queue:delivery-"))):
        return False
    observation = duty.get("observation") or {}
    jobs_path = Path((duty.get("intent") or {}).get("identity", {}).get("jobs", ""))
    binding = observation.get("native_receiving_turn")
    if not isinstance(binding, dict):
        store.update(duty["id"], state="unknown", delivery="pending",
                     observation={**observation, "reason": "native-receiving-turn-binding-unknown"})
        return True
    delivery_id = row.metadata.get("delivery_id", "")
    if (binding.get("thread_id") != recipient_sid
            or binding.get("recipient_sid") != recipient_sid
            or binding.get("storage_recipient") != storage_recipient
            or binding.get("delivery_id") != delivery_id
            or binding.get("receipt_digest") != row.metadata.get("delivery_receipt_digest")
            or binding.get("row_revision") != row.metadata.get("delivery_row_revision")):
        store.update(duty["id"], state="unknown", delivery="pending",
                     observation={**observation, "reason": "native-receiving-turn-identity-changed"})
        return True
    try:
        import dispatch_pending_delivery as pending
        from dispatch_completion_join import pending_record_identity
        root = jobs_path.resolve(strict=True).parent
        exact = pending.read(root, storage_recipient, delivery_id)
        if (exact is None or exact.get("state") != "acked"
                or exact.get("receipt_digest") != row.metadata.get("delivery_receipt_digest")
                or exact.get("row_revisions", {}).get(row.attempt_id)
                    != row.metadata.get("delivery_row_revision")):
            store.update(duty["id"], state="unknown", delivery="pending",
                         observation={**observation, "reason": "native-receipt-identity-unavailable"})
            return True
        route_id, route_node, parent_attempt_id = pending_record_identity(row.metadata, jobs_path)
        ack_identity = pending.received_ack_identity(exact, storage_recipient, delivery_id)
        if (ack_identity.get("route_id") != route_id
                or ack_identity.get("route_node") != route_node
                or ack_identity.get("parent_attempt_id") != parent_attempt_id):
            store.update(duty["id"], state="unknown", delivery="pending",
                         observation={**observation, "reason": "native-receipt-binding-conflict"})
            return True
        from codex_queue_dispatch import resolve_queue_socket
        import codex_queue_delivery
        endpoint = resolve_queue_socket()
        source_turn_id = observation.get("continuation_source_turn") or binding.get("turn_id")
        turn_id = source_turn_id
        try:
            source_turn = codex_queue_delivery.read_exact_turn(
                endpoint, recipient_sid, source_turn_id, timeout=5.0)
        except codex_queue_delivery.QueueDeliveryError:
            source_turn = None
        current_turn = source_turn
        continuation_id = observation.get("continuation_id")
        if continuation_id:
            continuation = codex_queue_delivery.find_turn_by_client_message_id(
                endpoint, recipient_sid, continuation_id, timeout=5.0)
            if continuation is not None:
                current_turn = continuation
                turn_id = continuation.get("id")
            elif any(item.get("clientUserMessageId") == continuation_id for item in
                     codex_queue_delivery.list_queue(endpoint, recipient_sid, timeout=5.0)):
                store.update(duty["id"], state="pending", delivery="native-continuation-accepted",
                             observation={**observation, "reason": "native-continuation-still-queued"})
                return True
        if current_turn is None:
            store.update(duty["id"], state="unknown", delivery="pending",
                         observation={**observation, "reason": "native-receiving-turn-unavailable"})
            return True
        status = current_turn["status"]
        if status in {"completed", "complete"}:
            store.update(duty["id"], state="complete", delivery="accepted",
                         observation={**observation, "receiving_turn_status": status})
            return True
        if status in {"inProgress", "in_progress", "running"}:
            store.update(duty["id"], state="pending", delivery="pending",
                         observation={**observation, "reason": "native-receiving-turn-active",
                                      "receiving_turn_status": status})
            return True
        if status != "interrupted":
            store.update(duty["id"], state="unknown", delivery="pending",
                         observation={**observation, "reason": "native-receiving-turn-status-unknown",
                                      "receiving_turn_status": status})
            return True
        latest = codex_queue_delivery._latest_turn(endpoint, recipient_sid, timeout=5.0)
        if not isinstance(latest, dict) or latest.get("id") != turn_id:
            store.update(duty["id"], state="unknown", delivery="pending",
                         observation={**observation, "reason": "native-receiving-turn-no-longer-current"})
            return True
        try:
            source_messages = _native_user_message_sources(source_turn)
        except codex_queue_delivery.QueueDeliveryError:
            source_messages = []
        receipt = exact.get("receipt")
        if not isinstance(receipt, dict):
            store.update(duty["id"], state="unknown", delivery="pending",
                         observation={**observation, "reason": "native-receipt-body-unavailable"})
            return True
        from dispatch_completion_join import completion_followup_text
        encoded = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        followup = completion_followup_text(
            receipt, jobs=str(jobs_path),
            surface=str(_UTILITIES_DIR.parent / "adapters/codex/bin/preflight.sh"))
        request_context = observation.get("native_request_context")
        original_request = None
        request_reference = None
        source_reason = "native-original-user-message-unavailable"
        source_record = None
        receiving_binding = observation.get("native_receiving_turn") or binding
        if (isinstance(request_context, dict)
                and request_context.get("source") == "codex-userprompt-submit"
                and request_context.get("status") == "available"
                and isinstance(request_context.get("text"), str)
                and request_context.get("text")
                and request_context.get("thread_id") == recipient_sid
                and request_context.get("turn_id") == receiving_binding.get("turn_id")
                and request_context.get("delivery_id") == delivery_id
                and request_context.get("receipt_digest")
                    == row.metadata.get("delivery_receipt_digest")
                and request_context.get("row_revision")
                    == row.metadata.get("delivery_row_revision")):
            original_request = request_context["text"]
            source_record = {"source": "codex-userprompt-submit", "status": "available",
                             "thread_id": recipient_sid,
                             "turn_id": receiving_binding.get("turn_id")}
            request_reference = (
                "\nOriginal user request source: read the exact JSON field "
                "observation.native_request_context.text in the existing durable obligation "
                f"file {store._record_path(duty['id']).resolve()} (duty {duty['id']}; "
                f"Codex thread {recipient_sid}, turn {receiving_binding.get('turn_id')}; "
                f"delivery {delivery_id}; receipt revision "
                f"{row.metadata.get('delivery_row_revision')}). Fulfill it verbatim, including "
                "all requested content and output-format requirements."
            )
        elif source_messages:
            source_record = {"source": "codex-native-userMessage", "status": "available",
                             "item_ids": [item["item_id"] for item in source_messages]}
        else:
            if (isinstance(request_context, dict)
                    and request_context.get("status") == "unavailable"
                    and request_context.get("thread_id") == recipient_sid
                    and request_context.get("turn_id") == receiving_binding.get("turn_id")
                    and request_context.get("delivery_id") == delivery_id
                    and request_context.get("receipt_digest")
                        == row.metadata.get("delivery_receipt_digest")
                    and request_context.get("row_revision")
                        == row.metadata.get("delivery_row_revision")):
                source_reason = request_context.get("reason", source_reason)
            elif isinstance(request_context, dict):
                source_reason = "hook-request-context-binding-unavailable"
            source_record = {"source": "unavailable", "status": "unavailable",
                             "reason": source_reason}
        message = _native_continuation_message(
            thread_id=recipient_sid, turn_id=turn_id, source_turn_id=source_turn_id,
            receipt_text=encoded,
            followup=followup, source_messages=source_messages,
            original_request=original_request,
            request_reference=request_reference,
            source_unavailable_reason=(source_reason if source_record["status"] == "unavailable"
                                       else None))
        stable = "delivery-" + hashlib.sha256((
            "codex-native-recovery-v1\0" + delivery_id + "\0" + turn_id + "\0"
            + str(row.metadata.get("delivery_receipt_digest", ""))
        ).encode("utf-8")).hexdigest()[:32]
        prepared_record, prepared = store.prepare_native_continuation(
            duty["id"], continuation_id=stable, source_turn_id=turn_id,
            source=source_record, message=message)
        observation = dict(prepared_record.get("observation") or {})
        stable = prepared["id"]
        message = prepared["message"]

        def before_add():
            fresh = current_attempt_row(jobs_path, row.attempt_id)
            from dispatch_seat_handover import effective_parent
            from dispatch_contract import terminal_conflict_pending
            fresh_receiving_turn = codex_queue_delivery.read_exact_turn(
                endpoint, recipient_sid, turn_id, timeout=5.0)
            newest = codex_queue_delivery._latest_turn(endpoint, recipient_sid, timeout=5.0)
            latest_record = pending.read(root, storage_recipient, delivery_id)
            if (fresh is None or fresh.status != row.status
                    or fresh.metadata.get("delivery_id") != delivery_id
                    or fresh.metadata.get("delivery_receipt_digest")
                        != row.metadata.get("delivery_receipt_digest")
                    or fresh.metadata.get("delivery_row_revision") != row.metadata.get("delivery_row_revision")
                    or effective_parent(fresh.metadata, jobs_path) != recipient_sid
                    or terminal_conflict_pending(fresh.metadata)
                    or pending_record_identity(fresh.metadata, jobs_path)
                        != (route_id, route_node, parent_attempt_id)
                    or not fresh_receiving_turn
                    or fresh_receiving_turn.get("status") != "interrupted"
                    or fresh_receiving_turn.get("id") != turn_id
                    or not isinstance(newest, dict) or newest.get("id") != turn_id
                    or not latest_record or latest_record.get("state") != "acked"
                    or pending.received_ack_identity(latest_record, storage_recipient, delivery_id)
                        != ack_identity):
                raise codex_queue_delivery.QueueDeliveryError("native-continuation-identity-changed")

        sent = codex_queue_delivery.send_at_least_once(
            endpoint, thread_id=recipient_sid, client_message_id=stable, message=message,
            timeout=5.0, require_message_match=True, before_add=before_add)
        if sent.get("status") in {"queued", "consumed"}:
            # Queue acceptance transfers transport, not the still-unfinished
            # receiving turn. Keep observing its exact native turn on this duty.
            store.update(duty["id"], state="pending", delivery="native-continuation-accepted",
                         observation={**observation, "reason": "native-continuation-awaiting-turn",
                                      "continuation_status": sent["status"]})
        else:
            store.update(duty["id"], state="unknown", delivery="pending",
                         observation={**observation, "reason": "native-continuation-unconfirmed"})
        return True
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        store.update(duty["id"], state="unknown", delivery="pending",
                     observation={**observation, "reason": "native-continuation-observation-unavailable",
                                  "detail": str(exc)[:160]})
        return True


_NATIVE_QUEUE_MESSAGE_LIMIT = 16 * 1024


def _native_user_message_sources(turn):
    """Return exact text userMessage items from one exact native turn."""
    import codex_queue_delivery

    items = turn.get("items") if isinstance(turn, dict) else None
    if not isinstance(items, list):
        raise codex_queue_delivery.QueueDeliveryError(
            "native-original-user-message-unavailable")
    sources = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "userMessage":
            continue
        item_id = item.get("id")
        content = item.get("content")
        if (not isinstance(item_id, str) or not item_id
                or not isinstance(content, list) or len(content) != 1
                or not isinstance(content[0], dict)
                or content[0].get("type") != "text"
                or not isinstance(content[0].get("text"), str)):
            raise codex_queue_delivery.QueueDeliveryError(
                "native-original-user-message-unavailable")
        sources.append({"item_id": item_id, "text": content[0]["text"]})
    if len({item["item_id"] for item in sources}) != len(sources):
        raise codex_queue_delivery.QueueDeliveryError(
            "native-original-user-message-unavailable")
    return sources


def _native_continuation_message(*, thread_id, turn_id, source_turn_id,
                                 receipt_text, followup, source_messages,
                                 original_request=None, request_reference=None,
                                 source_unavailable_reason=None):
    import codex_queue_delivery

    prefix = ("AGENT_HARNESS_COMPLETION_V1\n" + receipt_text + "\n" + followup
              + f"\nThe exact receiving turn {turn_id} was interrupted. Continue its approved follow-through "
                "and report once; check existing exact artifact and next-leg lineage before repeating "
                "any action. Preserve new failures, conflicts, and unfinished work.")
    if original_request is not None:
        source_header = (
            "\nOriginal user request received by Codex UserPromptSubmit, verbatim. "
            "Continue and fulfill that original task, preserving all requested content and output "
            "formats; this receipt is context and does not replace or narrow the task:\n")
        source_body = original_request
    elif source_messages:
        source_header = (
            "\nOriginal user request from the exact interrupted turn, verbatim. "
            "Continue and fulfill that original task, preserving all requested content and output formats; "
            "this receipt is context and does not replace or narrow the task:\n")
        source_body = "".join(
            f"\n[original userMessage item {item['item_id']}]\n{item['text']}"
            for item in source_messages)
    else:
        source_header = (
            "\nThe original user request could not be recovered from the Codex hook or exact turn "
            f"({source_unavailable_reason or 'source-unavailable'}). Continue the existing approved "
            "follow-through in this receipt and record any missing original-task context honestly.\n")
        source_body = ""
    full = prefix + source_header + source_body
    if len(full.encode("utf-8")) <= _NATIVE_QUEUE_MESSAGE_LIMIT:
        return full
    if original_request is not None and request_reference:
        bounded = prefix + request_reference
        if len(bounded.encode("utf-8")) <= _NATIVE_QUEUE_MESSAGE_LIMIT:
            return bounded
    references = ", ".join(item["item_id"] for item in source_messages)
    if not references:
        raise codex_queue_delivery.QueueDeliveryError(
            "native-continuation-message-over-limit")
    bounded = (prefix + f"\nOriginal task source: read the exact userMessage item(s) {references} "
               f"in Codex thread {thread_id}, turn {source_turn_id}. Fulfill the entire original request, "
               "including every requested content and output-format requirement. This receipt is only "
               "continuation context; it does not replace, narrow, or summarize the original task.")
    if len(bounded.encode("utf-8")) > _NATIVE_QUEUE_MESSAGE_LIMIT:
        raise codex_queue_delivery.QueueDeliveryError(
            "native-continuation-message-over-limit")
    return bounded


def _settle_claude_native_dispatch_receipt(root, storage_recipient, row,
                                          recipient_sid, record) -> bool:
    """Reuse only the exact Claude transcript bound to the registered cwd."""
    cwd = row.metadata.get("parent_cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return False
    runtime_home = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    try:
        from claude_project_dir import resolve_session_transcript
        from dispatch_recipient_receipt import (
            claude_transcript_evidence, legacy_claude_receipt_bindings,
            settle_native_receipt,
        )
        transcript, _reason = resolve_session_transcript(
            recipient_sid, cwd, expected_cwd=cwd, runtime_home=runtime_home)
        if transcript is None:
            return False
        evidence = claude_transcript_evidence(
            transcript_path=transcript, session_id=recipient_sid, cwd=cwd,
            expected_cwd=cwd, runtime_home=runtime_home)
        if evidence is not None:
            from dispatch_session_sweep import addressed_records
            candidates, _count = addressed_records(
                Path(root), record.get("recipient_kind", ""), recipient_sid)
            legacy = legacy_claude_receipt_bindings(
                root, record.get("recipient_kind", ""), recipient_sid,
                candidates, evidence)
            binding = legacy.get((storage_recipient, record.get("delivery_id")))
            if binding is not None:
                evidence = {**evidence,
                            "bindings": [*(evidence.get("bindings") or []), binding]}
        outcome = settle_native_receipt(
            root, storage_recipient, "claude", recipient_sid, record, evidence,
            acked_by=f"native-receipt:claude:{recipient_sid}")
        return bool(outcome.get("received"))
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def _resume_registered_obligation(duty, store, *, read_context=None):
    """Wake an exact restored recipient through the existing durable peer courier."""
    intent = duty.get("intent") or {}
    from harness_capabilities import parent_completion
    identity = intent["identity"]
    harness = identity.get("harness")
    if intent.get("carrier") != parent_completion(harness)["carrier"]:
        return
    jobs = Path(identity["jobs"])
    from dispatch_completion_join import current_attempt_row
    from dispatch_notice_state import keep_claim
    import dispatch_pending_delivery as pending
    from dispatch_seat_handover import effective_parent, effective_parent_harness
    from dispatch_session_sweep import (
        IDENTITY_KEY, STORAGE_KEY, addressed_records, delivery_context, is_human_gate_record,
    )
    spec = importlib.util.spec_from_file_location(
        "_retained_rewake", _UTILITIES_DIR.parent / "hooks/dispatch-owner-rewake.py")
    rewake = sys.modules.get(spec.name)
    if rewake is None:
        rewake = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = rewake
        spec.loader.exec_module(rewake)
    arm = rewake._read_arm(rewake.arm_path(jobs, identity["attempt_id"]))
    holder = (arm or {}).get("holder") or (duty.get("observation") or {}).get("holder")
    row = current_attempt_row(jobs, identity["attempt_id"], read_context=read_context)
    if row is None:
        return
    observation = duty.get("observation") or {}
    transfer_ref = observation.get("transfer_ref")
    transferred = None
    if transfer_ref:
        transferred = peer_message._read_pending(transfer_ref)
        if transferred and transferred.get("state") == "received":
            bound_received = peer_obligations.acknowledge_registered_delivery(
                transferred, roots=[jobs.parent])
        if transferred and peer_obligations.registered_delivery_settled(
                transferred, roots=[jobs.parent], read_context=read_context):
            transferred = None
        elif transferred and transferred.get("state") == "received" and bound_received:
            # The exact recipient receipt is durable even when this ACK write
            # failed. Keep the existing duty/ref for the next natural retry;
            # preparing another transfer would duplicate a known receipt.
            store.update(duty["id"], observation={**observation,
                "reason": "received-delivery-ack-pending"})
            return
    parent = [row.metadata.get(key, "") for key in
              ("parent_runtime_pid", "parent_runtime_pid_start", "parent_runtime_ns")]
    # A native hook belongs to its original runtime. A live holder cannot wake
    # a replacement process after that runtime died. Unknown identity stays protected.
    # The duty follows confirmed handover; the signed queue stays under the
    # registered parent, even when its current recipient is a successor.
    recipient_sid = effective_parent(row.metadata, jobs, read_context=read_context)
    recipient_harness = (harness if recipient_sid == str(row.metadata.get("parent_sid") or "")
                         else effective_parent_harness(row.metadata, jobs, read_context=read_context)
                         or harness)
    storage_recipient = row.metadata.get("parent_sid", "")
    # Transfers are immutable. A transfer reserved for an earlier recipient
    # cannot hold the current duty open or be redirected; the current recipient
    # receives a fresh transfer through the normal path below.
    transferred_is_current = bool(
        transferred
        and (transferred.get("to") or {}).get("session_id") == recipient_sid
        and (transferred.get("to") or {}).get("harness") == recipient_harness)
    if transferred and not transferred_is_current:
        transferred = None
    identity_fields = ("parent_sid", "parent_harness", "parent_completion_delivery",
                       "delivery_id", "delivery_receipt_digest", "delivery_row_revision",
                       "delivery_intent", "route_id", "route_hash", "route_node",
                       "owner_route_id", "owner_route_hash", "owner_route_node")

    def fresh_recipient_row():
        fresh = current_attempt_row(jobs, identity["attempt_id"])
        fresh_harness = (effective_parent_harness(fresh.metadata, jobs)
                         if fresh is not None else "")
        if (fresh is None or fresh.status != row.status
                or effective_parent(fresh.metadata, jobs) != recipient_sid
                or (fresh_harness and fresh_harness != recipient_harness)
                or any(fresh.metadata.get(key) != row.metadata.get(key)
                       for key in identity_fields)):
            return None
        return fresh

    if row.status not in {"open", "running", "done"}:
        return
    notice_id = intent.get("notice_delivery_id")
    if notice_id:
        # A fresh handover is independent of the original completion ACK. Its
        # exact pending record already carries revision, lineage and authority.
        record = pending.read(jobs.parent, storage_recipient, notice_id)
        if not record or record.get("receipt_digest") != intent.get("notice_digest"):
            return
        from dispatch_supervision import validate as validate_notice
        notice = validate_notice(record.get("receipt"), expected_thread=storage_recipient)
        pending.received_ack_identity(record, storage_recipient, notice_id)
        if (notice["owner_attempt_id"] != row.attempt_id
                or notice["job_registry"] != str(jobs)
                or notice["sealed_batch_id"] != row.metadata.get("managed_sealed_batch_id", "")
                or record.get("recipient_kind") != intent["carrier"]):
            return
        delivery_id = notice_id
        if record.get("state") in {"acked", "rejected", "expired"}:
            store.update(duty["id"], state="complete", delivery=record["state"], cleanup="complete")
            return
        from dispatch_supervision import notice_is_current
        if not notice_is_current(record):
            owner = "retained-rewake:" + duty["id"]
            claimed = pending.claim(jobs.parent, storage_recipient, notice_id,
                claim_owner=owner, lease_seconds=30, require_generation_proof=False, jobs=jobs)
            keep_claim(jobs.parent, storage_recipient, notice_id, claimed, owner, jobs=jobs)
            latest = pending.read(jobs.parent, storage_recipient, notice_id)
            if latest and latest.get("state") == "rejected":
                store.update(duty["id"], state="complete", delivery="not-required", cleanup="complete")
            return
    elif row.status == "done":
        if row.metadata.get("delivery_intent") != "1":
            store.update(duty["id"], state="complete", delivery="not-required", cleanup="complete")
            return
        delivery_id = row.metadata.get("delivery_id", "")
        record = pending.read(jobs.parent, storage_recipient, delivery_id)
        if record is None:
            terminal_state = peer_obligations._registered_terminal_state(
                jobs.parent, storage_recipient, delivery_id, row.attempt_id,
                row.metadata, intent["carrier"])
            if terminal_state is not None:
                store.update(duty["id"], state="complete", delivery=terminal_state,
                             cleanup="complete")
                return
            # Terminal-close materialization is idempotent. Use it only for a
            # missing exact record; _reconcile_obligations backs off an unknown
            # duty if this bounded recovery attempt still cannot restore source.
            from dispatch_completion_join import materialize_after_terminal_close
            materialize_after_terminal_close(jobs, row.attempt_id)
            record = pending.read(jobs.parent, storage_recipient, delivery_id)
            expected_digest = row.metadata.get("delivery_receipt_digest")
            expected_revision = row.metadata.get("delivery_row_revision")
            try:
                if record is not None:
                    pending.received_ack_identity(record, storage_recipient, delivery_id)
            except (OSError, ValueError, RuntimeError, TypeError, KeyError):
                record = None
            if (record is None
                    or record.get("recipient_kind") != intent["carrier"]
                    or row.attempt_id not in (record.get("attempt_ids") or [])
                    or not expected_digest
                    or record.get("receipt_digest") != expected_digest
                    or not expected_revision
                    or (record.get("row_revisions") or {}).get(row.attempt_id)
                        != expected_revision):
                store.update(duty["id"], state="unknown",
                             observation={**observation,
                                          "reason": "completion-delivery-record-unavailable",
                                          "delivery_id": delivery_id})
                return
        if record and record.get("state") in {"acked", "rejected", "expired"}:
            if record.get("state") == "acked" and _resume_native_receiving_turn(
                    duty, store, row, record, recipient_sid, storage_recipient):
                return
            store.update(duty["id"], state="complete", delivery=record["state"], cleanup="complete")
            return
    if transferred_is_current and transferred and transferred.get("state") in {"queued", "unverified"}:
        return  # The existing message courier owns this accepted/ambiguous send.
    if not notice_id and holder and rewake._holder_alive(holder) and rewake._holder_alive(parent):
        return
    old_server = _HERDR_SESSION
    globals()["_HERDR_SESSION"] = None if identity["server"] == "default" else identity["server"]
    try:
        def list_agents():
            result = subprocess.run(_herdr_argv("agent", "list"), capture_output=True,
                                   text=True, timeout=5)
            return (json.loads(result.stdout).get("result") or {}).get("agents") or []
        agents = (read_context.recipient_census(identity.get("server"), list_agents)
                  if read_context is not None else list_agents())
        matches = [a for a in agents if a.get("agent") == recipient_harness
                   and (a.get("agent_session") or {}).get("value") == recipient_sid]
        if len(matches) != 1:
            if fresh_recipient_row() is None:
                return
            store.update(duty["id"], observation={"reason": "recipient-unavailable",
                                                  "session_id": recipient_sid})
            return
        pane = matches[0]["pane_id"]
        if _resolve_target(pane)[:2] != (recipient_harness, recipient_sid):
            return
        if transferred_is_current and transferred and transferred.get("state") == "pending":
            if fresh_recipient_row() is None:
                return
            _schedule_message_obligation(transferred, pane, pane)
            return
        if not notice_id and row.status in {"open", "running"}:
            records, _ = addressed_records(jobs.parent, intent["carrier"], recipient_sid)
            record = next((r for r in records if row.attempt_id in (r.get("attempt_ids") or [])
                           and (is_human_gate_record(r) or
                                r.get("receipt", {}).get("kind") == "supervision")), None)
            if record is None:
                return
            storage_recipient = record.get("_storage_key") or storage_recipient
            delivery_id = record["delivery_id"]
        if (record and recipient_harness == "claude"
                and _settle_claude_native_dispatch_receipt(
                    jobs.parent, storage_recipient, row, recipient_sid, record)):
            return
        # Read-pass snapshots save parsing only. The exact identity and
        # recipient are read again immediately before claiming the delivery.
        fresh_row = fresh_recipient_row()
        if fresh_row is None:
            return
        row = fresh_row
        owner = "retained-rewake:" + duty["id"]
        if record and record.get("state") in {"claimed", "sent-ambiguous"}:
            pending.reclaim(jobs.parent, storage_recipient, delivery_id,
                            now_ns=time.monotonic_ns())
        record = pending.claim(jobs.parent, storage_recipient, delivery_id,
                               claim_owner=owner, lease_seconds=30, require_generation_proof=False,
                               jobs=jobs,
                               retry_context=f"{recipient_harness}:{recipient_sid}:{pane}:{duty['id']}")
        # keep_claim may project the in-memory receipt; receipt evidence and
        # the rendered marker stay bound to the stored record.
        stored = dict(record)
        if not keep_claim(jobs.parent, storage_recipient, delivery_id, record, owner, jobs=jobs):
            return
        if (recipient_harness == "claude"
                and _settle_claude_native_dispatch_receipt(
                    jobs.parent, storage_recipient, row, recipient_sid, stored)):
            return
        from dispatch_supervision import CONTINUED_KEY, continue_for_parent
        continued = continue_for_parent(record, session_id=recipient_sid,
                                        recipient_kind=intent["carrier"])
        try:
            bound = {IDENTITY_KEY: pending.received_ack_identity(
                stored, storage_recipient, delivery_id)}
        except pending.PendingDeliveryError:
            bound = {}  # Sent without a marker; its receipt stays on the fallback path.
        context_record = {**record, STORAGE_KEY: storage_recipient, **bound,
                          **({CONTINUED_KEY: continued} if continued else {})}
        message = delivery_context([(jobs.parent, [context_record])])
        producer_binding = {
            "schema": peer_message._RUNTIME_PRODUCER_SCHEMA,
            "duty_id": duty["id"], "jobs": str(jobs), "attempt_id": row.attempt_id,
            "carrier": intent["carrier"], "delivery_id": delivery_id,
            "receipt_digest": record.get("receipt_digest"),
            "recipient": {"harness": recipient_harness, "session_id": recipient_sid},
            "route_id": str(row.metadata.get("route_id") or ""),
            "route_node": str(row.metadata.get("route_node") or ""),
        }
        runtime_sender = {"harness": "runtime", "session_id": None, "name": "Hearting runtime"}
        transfer_refs = [duty["id"], row.attempt_id, delivery_id]
        if not peer_message._valid_runtime_producer_binding(
                producer_binding, runtime_sender, producer_binding["recipient"], transfer_refs):
            pending.release_claim(jobs.parent, storage_recipient, delivery_id, claim_owner=owner)
            return
        # The normal prompt command seals the body, checks the recipient again,
        # and retains it through forms, busy turns and sender/observer exit (#446).
        prepared = []
        def bind_transfer(ref):
            binding = {"duty_id": duty["id"], "jobs": str(jobs), "attempt_id": row.attempt_id,
                "carrier": intent["carrier"], "recipient_sid": recipient_sid,
                "delivery_id": delivery_id, "storage_recipient": storage_recipient, "claim_owner": owner}
            peer_message.bind_dispatch_notice(ref, binding)
            store.update(duty["id"], observation={**observation, "transfer_ref": ref,
                "delivery_id": delivery_id, "storage_recipient": storage_recipient,
                "recipient_sid": recipient_sid, "claim_owner": owner,
                "producer_binding": producer_binding})
            prepared.append(ref)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as body:
            body.write(message)
            body.flush()
            args = build_parser().parse_args(["prompt", pane, "--body-file", body.name,
                                              "--ref", duty["id"], "--ref", row.attempt_id,
                                              "--ref", delivery_id])
            code = cmd_prompt(args, expected_recipient=(recipient_harness, recipient_sid),
                              on_prepared=bind_transfer, producer_binding=producer_binding)
        current = pending.read(jobs.parent, storage_recipient, delivery_id)
        if current and current.get("state") == "acked":
            return
        if prepared or code in {0, 3}:
            pending.mark_sent_ambiguous(jobs.parent, storage_recipient, delivery_id, claim_owner=owner)
            store.update(duty["id"], state="pending", delivery="peer-courier", cleanup="pending")
        else:
            pending.release_claim(jobs.parent, storage_recipient, delivery_id, claim_owner=owner)
    finally:
        globals()["_HERDR_SESSION"] = old_server


_DISPATCH_READY_API = None


def _dispatch_ready_api():
    global _DISPATCH_READY_API
    if _DISPATCH_READY_API is None:
        path = _UTILITIES_DIR / "dispatch-attempt-ready.py"
        spec = importlib.util.spec_from_file_location("_dispatch_attempt_ready", path)
        if spec is None or spec.loader is None:
            raise ImportError("dispatch-attempt-readiness-unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _DISPATCH_READY_API = module
    return _DISPATCH_READY_API


def _reconcile_readiness_observations(store, duties, *, read_context,
                                      observation_context, incarnation):
    """Publish one bounded classification per exact selection for live consumers."""
    groups = {}
    for duty in duties:
        intent = duty.get("intent") or {}
        if intent.get("kind") != shared_readiness.DUTY_KIND:
            continue
        identity = intent.get("identity")
        if not isinstance(identity, dict) or not shared_readiness.consumer_is_current(identity):
            try:
                store.update(duty["id"], state="complete", result="consumer-exited",
                             cleanup="complete")
            except (OSError, ValueError, RuntimeError):
                pass
            continue
        selection = identity.get("selection")
        jobs = identity.get("jobs")
        if (not isinstance(selection, dict) or not isinstance(jobs, str)
                or not selection.get("attempt_id")):
            try:
                store.update(duty["id"], observation={"readiness_state": "unknown",
                             "reason": "readiness-request-invalid"})
            except (OSError, ValueError, RuntimeError):
                pass
            continue
        key = (jobs, selection.get("attempt_id", ""), selection.get("parent", ""),
               selection.get("slug", ""))
        groups.setdefault(key, []).append((duty, identity))

    if not groups:
        return
    api = _dispatch_ready_api()
    producer_start = process_start_ticks(os.getpid()) or ""
    for (jobs_text, attempt_id, parent, slug), requests in groups.items():
        jobs = Path(jobs_text)
        try:
            if not jobs.is_file():
                raise FileNotFoundError(str(jobs))
            rows = api.selected_rows(jobs, parent=parent, slug=slug,
                                    attempt_id=attempt_id, read_context=read_context,
                                    append_only=True)
            result = api.classify_selection(
                jobs, rows, settle=False, observation_context=observation_context,
                registry_append_only=True)
            view = read_context.registry_view(jobs, append_only=True)
            generation = list(view.signature) if view is not None else None
        except FileNotFoundError:
            result, generation = {"state": "unknown", "reason": "registry-missing", "children": []}, None
        except OSError:
            result, generation = {"state": "unknown", "reason": "registry-unreadable", "children": []}, None
        except Exception as exc:
            result, generation = {"state": "contract-error", "reason": type(exc).__name__,
                                  "children": []}, None
        for duty, identity in requests:
            projection = shared_readiness.make_projection(
                identity, result, producer_pid=os.getpid(), producer_start=producer_start,
                incarnation=incarnation, generation=generation)
            observation = dict(duty.get("observation") or {})
            observation.update(shared_readiness=projection,
                               readiness_state=result.get("state", "unknown"),
                               reason=result.get("reason", ""))
            try:
                store.update(duty["id"], observation=observation)
            except (OSError, ValueError, RuntimeError):
                continue


_RUNNER_READINESS_TRANSPORTS = {}


def _runner_readiness_transport(server, endpoint=None):
    selected_endpoint = (herdr_observation.socket_path_for_endpoint(server)
                         if endpoint is None else endpoint)
    if (not isinstance(selected_endpoint, str) or not selected_endpoint
            or selected_endpoint.startswith(_WATCH_SERVER_ENDPOINT_PREFIX)):
        return None
    key = (server or "default", selected_endpoint)
    transport = _RUNNER_READINESS_TRANSPORTS.get(key)
    if transport is None:
        transport = herdr_observation.ObservationTransport(
            key[0], selected_endpoint, timeout=min(0.5, _HERDR_GET_TIMEOUT_SECONDS))
        _RUNNER_READINESS_TRANSPORTS[key] = transport
    return transport


def _target_from_readiness_view(view, target):
    if view is None or not isinstance(target, str) or not target:
        return "unknown", {"harness": "-", "session_id": "-", "name": target or "-",
                            "pane": "-"}, 4, "target-census-unavailable"
    matches = []
    for agent in view.agents:
        sid = (agent.get("agent_session") or {}).get("value")
        if target in {agent.get("name"), sid, agent.get("pane_id")}:
            matches.append(agent)
    if len(matches) != 1:
        return "unknown", {"harness": "-", "session_id": "-", "name": target,
                            "pane": "-"}, 4, "target-census-ambiguous"
    return _interpret_payload({"result": {"agent": matches[0]}}, target)


def _runner_readiness_fds():
    fds = []
    for transport in tuple(_RUNNER_READINESS_TRANSPORTS.values()):
        try:
            fd = transport.fileno()
        except OSError:
            fd = None
        if isinstance(fd, int) and fd >= 0:
            fds.append(fd)
    return sorted(set(fds))


def _watch_readiness_identity(arm, native_state):
    if not isinstance(arm, dict):
        return None
    agent = arm.get("agent") or {}
    pane_identity = arm.get("pane_identity") or arm.get("foreground_identity") or {}
    server = arm.get("server") or "default"
    session_id = agent.get("session_id") or ""
    endpoint = _watch_selected_readiness_endpoint(arm, server)
    if not endpoint:
        return None
    identity = shared_readiness.target_identity(
        server=server, endpoint=str(endpoint), pane=agent.get("pane"),
        harness=agent.get("harness"), session_id="" if session_id == "-" else session_id,
        shell_pid=pane_identity.get("shell_pid"),
        shell_start=pane_identity.get("shell_start"), native_state=native_state,
        purpose="completion")
    if identity is not None:
        return identity
    return shared_readiness.legacy_target_identity(
        server=server, endpoint=str(endpoint), pane=agent.get("pane"),
        harness=agent.get("harness"), session_id="" if session_id == "-" else session_id,
        native_state=native_state, purpose="completion")


def _watch_readiness_pending(snapshot):
    root = snapshot.get("watch_root") if isinstance(snapshot, dict) else None
    entries = snapshot.get("watches") if isinstance(snapshot, dict) else None
    for watch_id, arm in entries or ():
        if not isinstance(arm, dict) or root is None:
            continue
        paths = _watch_paths(watch_id, root)
        if paths.receipt.exists():
            continue
        observation = _read_json(paths.observation) or {}
        native = observation.get("native_wait") or {}
        if native.get("state") == "result" and native.get("agent_state") in {"idle", "done"}:
            return True
    return False


def _publish_watch_readiness(store, snapshot, *, read_context, incarnation):
    """Share one post-native-result pane read across matching detached watches."""
    root = snapshot.get("watch_root") if isinstance(snapshot, dict) else None
    entries = snapshot.get("watches") if isinstance(snapshot, dict) else None
    if root is None or entries is None:
        return False
    groups = {}
    for watch_id, arm in entries:
        if not isinstance(arm, dict):
            continue
        paths = _watch_paths(watch_id, root)
        if paths.receipt.exists():
            continue
        observation = _read_json(paths.observation) or {}
        native = observation.get("native_wait") or {}
        if (native.get("state") != "result"
                or native.get("agent_state") not in {"idle", "done"}):
            continue
        native_state = native.get("agent_state")
        transition = observation.get("readiness_transition")
        transition_is_current = (
            isinstance(transition, dict)
            and isinstance(transition.get("token"), str) and bool(transition.get("token"))
            and shared_readiness.consumer_is_current(
                {"consumer": transition.get("consumer")})
            and _watch_readiness_identity_matches_arm(
                transition.get("identity"), arm, native_state))
        identity = (transition.get("identity") if transition_is_current else
                    _watch_readiness_identity(arm, native_state))
        if identity is None:
            continue
        if not transition_is_current:
            transition = None
        target_key = {key: value for key, value in identity.items()
                      if key not in {"shell_pid", "shell_start"}}
        key = json.dumps(target_key, sort_keys=True, separators=(",", ":"))
        if (not isinstance(transition, dict)
                or transition.get("identity") != identity
                or not shared_readiness.consumer_is_current(
                    {"consumer": transition.get("consumer")})
                or not isinstance(transition.get("token"), str)
                or not transition.get("token")):
            transition = None
        groups.setdefault(key, []).append((watch_id, arm, identity,
                                            native.get("agent_state"), transition))

    if not groups:
        for transport in _RUNNER_READINESS_TRANSPORTS.values():
            with contextlib.suppress(Exception):
                transport.close()
        _RUNNER_READINESS_TRANSPORTS.clear()
        return False
    active_endpoints = {
        (requests[0][2]["server"], requests[0][2]["endpoint"])
        for requests in groups.values()
        if not requests[0][2]["endpoint"].startswith(_WATCH_SERVER_ENDPOINT_PREFIX)
    }
    for key, transport in tuple(_RUNNER_READINESS_TRANSPORTS.items()):
        if key not in active_endpoints:
            with contextlib.suppress(Exception):
                transport.close()
            _RUNNER_READINESS_TRANSPORTS.pop(key, None)
    producer_start = process_start_ticks(os.getpid()) or ""
    try:
        jobs = _watch_selected_jobs_path()
    except (OSError, ValueError, RuntimeError):
        jobs = None
    views_by_endpoint = {}
    for requests in groups.values():
        _watch_id, arm, identity, native_state, _transition = requests[0]
        server = identity["server"]
        agent = arm.get("agent") or {}
        target = arm.get("target") or agent.get("name") or ""
        reason = "shared-readiness-unavailable"
        readiness_state = "unknown"
        generation = None
        lineage = None
        transition_read_context = None
        transition_authority = None
        try:
            if jobs is None:
                raise ValueError("selected-dispatch-registry-unavailable")
            endpoint_key = (server, identity["endpoint"])
            if endpoint_key not in views_by_endpoint:
                transport = _runner_readiness_transport(server, identity["endpoint"])
                views_by_endpoint[endpoint_key] = (
                    transport.observe() if transport is not None else None)
            view = views_by_endpoint[endpoint_key]
            if view is None or getattr(view, "complete", True) is not True:
                # Preserve the legacy positive CLI observation when the optional
                # transport has no usable census. The group key already limits
                # this to one read per endpoint/target/pass.
                observed_state, observed_ident, _code, _reason = _retire_target(
                    target, server=server)
            else:
                observed_state, observed_ident, _code, _reason = _target_from_readiness_view(
                    view, target)
            observed_session_id = observed_ident.get("session_id")
            if observed_session_id in {None, "-"}:
                observed_session_id = ""
            session_candidate = (observed_session_id == identity["session_id"]
                                 or (not observed_session_id
                                     and bool(identity["session_id"])))
            pane_info = _retire_pane_info(identity["pane"], server=server)
            shell_pid = (int(pane_info.get("shell_pid", 0))
                         if isinstance(pane_info, dict) else 0)
            shell_start = _proc_start_ticks(shell_pid) if shell_pid > 0 else None
            lineage = ({"shell_pid": shell_pid, "shell_start": str(shell_start)}
                       if shell_pid > 0 and shell_start else None)
            if (observed_state not in {"unknown", "herdr-unavailable", "agent-not-found"}
                    and observed_ident.get("pane") == identity["pane"]
                    and observed_ident.get("harness") == identity["harness"]
                    and session_candidate
                    and isinstance(pane_info, dict) and lineage is not None):
                readiness = _pane_readiness(
                    target, native_state,
                    expected_harness=identity["harness"],
                    expected_sid=identity["session_id"] or "-",
                    expected_pane=identity["pane"],
                    target_observation=(observed_state, observed_ident),
                    pane_info=pane_info, registry_append_only=True,
                    read_context=read_context, server=server)
                readiness_identity = dict(readiness.identity)
                expected_readiness_identity = {
                    "server": identity["server"], "pane": identity["pane"],
                    "harness": identity["harness"],
                    "session_id": identity["session_id"],
                    "pid_birth": f"{shell_pid}:{shell_start}",
                }
                if _watch_readiness_fields_match(
                        expected_readiness_identity, readiness_identity):
                    readiness_state, reason = readiness.state, readiness.reason
                else:
                    reason = "shared-readiness-identity-changed"
            else:
                reason = "shared-readiness-identity-unverified"

            # A cached ready projection only starts a per-watch transition.
            # Re-read the mutable authority at that boundary in this shared
            # runner pass. This is one extra bounded classification per exact
            # target group, never one RegistryView or pane observation per
            # waiting watch.
            transition_requests = [
                (watch_id, watch_arm, transition)
                for watch_id, watch_arm, _watch_identity, _state, transition in requests
                if (isinstance(transition, dict)
                    and transition.get("identity") == _watch_identity
                    and shared_readiness.consumer_is_current(
                        {"consumer": transition.get("consumer")}))
            ]
            if transition_requests:
                from peer_read_context import PeerReadContext
                transition_read_context = PeerReadContext(registry_append_only=True)
                transition_authority = {"rows": [], "handover_keys": []}
                readiness = _pane_readiness(
                    target, native_state,
                    expected_harness=identity["harness"],
                    expected_sid=identity["session_id"] or "-",
                    expected_pane=identity["pane"],
                    target_observation=(observed_state, observed_ident),
                    pane_info=pane_info, registry_append_only=True,
                    read_context=transition_read_context, server=server,
                    authority_out=transition_authority)
                readiness_identity = dict(readiness.identity)
                expected_readiness_identity = {
                    "server": identity["server"], "pane": identity["pane"],
                    "harness": identity["harness"],
                    "session_id": identity["session_id"],
                    "pid_birth": f"{shell_pid}:{shell_start}",
                }
                if _watch_readiness_fields_match(
                        expected_readiness_identity, readiness_identity):
                    readiness_state, reason = readiness.state, readiness.reason
                else:
                    readiness_state, reason = "unknown", "shared-readiness-identity-changed"
            generation_context = transition_read_context or read_context
            view = generation_context.registry_view(jobs, append_only=True)
            generation = shared_readiness.registry_generation(
                jobs, view, selected=True)
            if generation is None:
                readiness_state, reason = "unknown", "registry-generation-unavailable"
        except (OSError, TypeError, ValueError):
            readiness_state, reason = "unknown", "shared-readiness-observation-failed"

        by_identity = {}
        for watch_id, watch_arm, watch_identity, _watch_state, transition in requests:
            key = json.dumps(watch_identity, sort_keys=True, separators=(",", ":"))
            by_identity.setdefault(key, (watch_identity, []))[1].append(
                (watch_id, watch_arm, transition))
        for watch_identity, consumers in by_identity.values():
            projection_state, projection_reason = readiness_state, reason
            expected_pid = watch_identity.get("shell_pid")
            if (expected_pid is not None
                    and (lineage is None or int(expected_pid) != lineage["shell_pid"]
                         or str(watch_identity.get("shell_start")) != lineage["shell_start"])):
                projection_state, projection_reason = "unknown", "shared-readiness-identity-changed"
            transition_state, transition_reason = projection_state, projection_reason
            transition_generation = generation
            transition_consumers = [
                item for item in consumers
                if (isinstance(item[2], dict)
                    and item[2].get("identity") == watch_identity
                    and shared_readiness.consumer_is_current(
                        {"consumer": item[2].get("consumer")}))
            ]
            if transition_consumers and transition_state == "ready":
                if not shared_readiness.valid_watch_authority(transition_authority):
                    transition_state = "unknown"
                    transition_reason = "shared-readiness-authority-unbounded"
            changed = shared_readiness.publish_target_projection(
                store.root, watch_identity,
                {"state": projection_state, "reason": projection_reason},
                producer_pid=os.getpid(), producer_start=producer_start,
                incarnation=incarnation, generation=generation, lineage=lineage)
            if changed:
                for _watch_id, _watch_arm, _transition in consumers:
                    _wake_current_watch_observer(
                        root, _watch_id, watch_identity)
            for watch_id, watch_arm, transition in transition_consumers:
                if (not isinstance(transition, dict)
                        or transition.get("identity") != watch_identity):
                    continue
                shared_readiness.publish_watch_transition(
                    store.root, watch_id, watch_identity, transition.get("token"),
                    {"state": transition_state, "reason": transition_reason},
                    producer_pid=os.getpid(), producer_start=producer_start,
                    incarnation=incarnation, generation=transition_generation, lineage=lineage,
                    authority=transition_authority)
                _wake_current_watch_observer(
                    root, watch_id, watch_identity, transition=transition,
                    readiness_root=store.root)
    return True


def _runner_duties(store, duties=None):
    if duties is None:
        duties = store.list()
    return [duty for duty in duties
            if (duty.get("intent", {}).get("kind") in {"message", "retire", "registered-batch",
                                                         shared_readiness.DUTY_KIND}
                or duty.get("id", "").startswith(("message-", "delay-")))]


def _runner_duty_signature(duties):
    """Track meaningful duty changes while ignoring repeated observation timestamps."""
    fields = ("reason", "phase", "ref", "session_id", "transfer_ref", "delivery_id",
              "recipient_sid", "holder", "readiness_state")
    values = []
    for duty in duties:
        observation = duty.get("observation") or {}
        values.append((
            duty.get("id"), duty.get("state"), duty.get("delivery"),
            duty.get("cleanup"), duty.get("result"), duty.get("reason"),
            duty.get("observer_error"),
            tuple((key, json.dumps(observation[key], sort_keys=True, default=str))
                  for key in fields if key in observation),
        ))
    return tuple(sorted(values))


def _watch_reconciliation_needed(*, service_current=None, entries=None):
    root = _watch_root()
    for watch_id, arm in (_watch_entries(root) if entries is None else entries):
        if not isinstance(arm, dict) or _watch_paths(watch_id, root).receipt.exists():
            continue
        if ((arm.get("steward") or {}).get("harness") == "claude"
                and ("steward_seat" not in arm or arm.get("steward_seat") is None)):
            return True
        if not _watcher_present(arm) or service_current:
            return True
    return False


def _external_obligation_snapshot():
    """Read external duty files once for one runner pass.

    The contained arm and record values are scheduling inputs only. Watch and
    batch mutation paths reread their exact current records before acting.
    """
    try:
        root = _watch_root()
        watch_before = dispatch_batch_obligations._directory_stamp(root)
        watches = tuple(_watch_entries(root))
        watch_stamp = dispatch_batch_obligations._directory_stamp(root)
        watch_cacheable = (watch_before is not None and watch_before == watch_stamp)
    except Exception:
        root, watches, watch_stamp, watch_cacheable = None, None, None, False
    try:
        batches = dispatch_batch_obligations.pending_observer_snapshot()
    except Exception:
        batches = {"signature": (("batch-state-unavailable",),), "paths_by_jobs": {}}
    jobs_stamps = {key: dispatch_batch_obligations._registry_stamp(Path(key))
                   for key in batches.get("roots_by_jobs", {})}
    return {"watch_root": root, "watches": watches, "watch_stamp": watch_stamp,
            "jobs_stamps": jobs_stamps,
            "watch_cacheable": watch_cacheable, "batches": batches}


def _external_obligation_snapshot_unchanged(snapshot):
    """Cheaply validate the post-unlock read boundary before reusing a scan."""
    if (not snapshot.get("watch_cacheable") or snapshot.get("watches") is None
            or not snapshot.get("batches", {}).get("cacheable")):
        return False
    root = snapshot.get("watch_root")
    if dispatch_batch_obligations._directory_stamp(root) != snapshot.get("watch_stamp"):
        return False
    batches = snapshot["batches"]
    for key, path in batches.get("roots_by_jobs", {}).items():
        if dispatch_batch_obligations._directory_stamp(path) != batches.get(
                "root_stamps", {}).get(key):
            return False
    return True


def _external_obligation_signature(snapshot=None):
    """Read pending watch/batch identities for the existing outer runner.

    The signature exists only to keep that runner alive and reset its bounded
    backoff when external work changes. It is not delivery or mutation authority.
    """
    snapshot = _external_obligation_snapshot() if snapshot is None else snapshot
    watches = []
    try:
        root = snapshot["watch_root"]
        entries = snapshot["watches"]
        if entries is None:
            raise OSError("watch-state-unavailable")
        for watch_id, arm in entries:
            if _watch_paths(watch_id, root).receipt.exists():
                continue
            watcher = (arm or {}).get("watcher") or {}
            watches.append((watch_id, watcher.get("pid"), watcher.get("pid_start")))
    except Exception:
        watches.append(("watch-state-unavailable",))
    batches = snapshot["batches"]["signature"]
    return tuple(sorted(watches)), batches


def _reconcile_external_obligations(store, *, service_current=None, snapshot=None,
                                    settlement_context=None):
    """Reconnect independent recovery owners from the existing lifecycle callback."""
    watches = None if snapshot is None else snapshot.get("watches")
    try:
        if snapshot is None:
            needs_watch = _watch_reconciliation_needed(service_current=service_current)
        else:
            needs_watch = _watch_reconciliation_needed(
                service_current=service_current, entries=watches)
    except Exception:
        needs_watch = True
    if needs_watch:
        try:
            if snapshot is None:
                _ensure_watch_observers(service_current=service_current)
            else:
                _ensure_watch_observers(service_current=service_current, entries=watches)
        except Exception:
            pass
    for duty in store.list(states={"complete"}, kinds={"retire"},
                           cached_terminal=True):
        try:
            _resume_retire_obligation(duty, store)
        except Exception:
            pass
    try:
        needs_batch = snapshot is not None or bool(service_current) or dispatch_batch_obligations.has_unowned_work()
    except Exception:
        needs_batch = True
    if needs_batch:
        try:
            if snapshot is None:
                dispatch_batch_obligations.ensure_observers(service_current=service_current)
            else:
                dispatch_batch_obligations.ensure_observers(
                    service_current=service_current,
                    observer_snapshot=snapshot.get("batches"),
                    settlement_context=settlement_context)
        except Exception:
            pass


def _obligation_retry_fingerprint(duty):
    value = {key: duty.get(key) for key in
             ("id", "state", "intent", "observation", "result", "delivery", "cleanup")}
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _reconcile_obligations(store, duties=None, *, read_context=None, retry_state=None):
    """Advance only the accepted duties owned by this runner."""
    if duties is None:
        duties = store.list()
    retry_state = {} if retry_state is None else retry_state
    for duty in duties:
        duty_id = duty.get("id", "")
        fingerprint = _obligation_retry_fingerprint(duty)
        retryable = duty.get("state") == "unknown" and fingerprint is not None
        if retryable:
            prior = retry_state.get(duty_id)
            if (prior is not None and prior[0] == fingerprint
                    and time.monotonic() < prior[1]):
                continue
        else:
            retry_state.pop(duty_id, None)
        try:
            kind = duty.get("intent", {}).get("kind")
            if kind == "message" or duty.get("id", "").startswith(("message-", "delay-")):
                _resume_message_obligation(duty, store, read_context=read_context)
            elif kind == "retire":
                with peer_obligations.legacy_runner_lock(store) as acquired:
                    if acquired:
                        _resume_retire_obligation(
                            duty, store, read_context=read_context,
                            persist_followup=True)
            elif kind == "registered-batch":
                _resume_registered_obligation(duty, store, read_context=read_context)
        except Exception:
            try:
                store.update(duty["id"], observer_error="observer-unavailable")
            except Exception:
                pass
        if retryable:
            try:
                latest = store.get(duty_id)
            except Exception:
                latest = None
            if latest and latest.get("state") == "unknown":
                latest_fingerprint = _obligation_retry_fingerprint(latest)
                prior = retry_state.get(duty_id)
                attempts = (prior[2] if prior is not None
                            and prior[0] == latest_fingerprint else 0)
                retry_state[duty_id] = (
                    latest_fingerprint,
                    time.monotonic() + min(60.0, 2.0 ** min(attempts, 6)),
                    attempts + 1)
            else:
                retry_state.pop(duty_id, None)
def _runner_lock_current(store, lock_fd):
    """The lock this runner holds is still the state root's runner lock file."""
    try:
        held = os.fstat(lock_fd)
        current = os.stat(store.root / peer_obligations.RUNNER_LOCK_NAME)
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)


def _runner_readiness_loop(state_root, lock_fd, incarnation, stop_event):
    """Keep attempt projections independent of slow obligation reconciliation."""
    from peer_read_context import PeerReadContext
    from runtime_observation import ObservationContext

    store = peer_obligations.ObligationStore(
        state_root, allow_state_root_creation=False)
    while not stop_event.is_set():
        if not _runner_lock_current(store, lock_fd):
            return
        store.begin_read_pass()
        try:
            duties = store.list(kinds={shared_readiness.DUTY_KIND},
                                fast_active_only=True)
            if duties and incarnation:
                read_context = PeerReadContext(registry_append_only=True)
                observation_context = ObservationContext()
                try:
                    _reconcile_readiness_observations(
                        store, duties, read_context=read_context,
                        observation_context=observation_context, incarnation=incarnation)
                except Exception:
                    pass
        except (OSError, ValueError, RuntimeError, peer_obligations.ObligationError):
            duties = ()
        finally:
            store.end_read_pass()
        stop_event.wait(1.0 if duties else 2.0)


def cmd_obligation_runner(args):
    """Existing peer steward, scoped to accepted duties and stopped when they settle."""
    store = peer_obligations.ObligationStore(
        args.state_root, allow_state_root_creation=False)
    try:
        lock_target = Path(os.path.realpath(os.readlink(f"/proc/self/fd/{args.lock_fd}")))
        if lock_target != (store.root / peer_obligations.RUNNER_LOCK_NAME).resolve():
            return 70
        os.fstat(args.lock_fd)
    except (OSError, ValueError, TypeError):
        return 70
    service_current = getattr(args, "service_current", None)
    delay = 1.0
    previous_signature = None
    runner_incarnation = f"{os.getpid()}:{process_start_ticks(os.getpid()) or ''}:{time.monotonic_ns()}"
    wake_fd = None
    if __name__ == "__main__":
        try:
            wake_fd = shared_readiness.open_runner_wake(store.root)
        except OSError:
            pass
    try:
        shared_readiness.publish_runner_incarnation(
            store.root, os.getpid(), process_start_ticks(os.getpid()) or "", runner_incarnation)
    except OSError:
        runner_incarnation = ""
    readiness_stop = None
    readiness_thread = None

    def start_readiness_worker():
        nonlocal readiness_stop, readiness_thread
        if (not runner_incarnation or readiness_thread is not None
                and readiness_thread.is_alive()):
            return
        readiness_stop = threading.Event()
        readiness_thread = threading.Thread(
            target=_runner_readiness_loop,
            args=(args.state_root, args.lock_fd, runner_incarnation, readiness_stop),
            name="hearting-obligation-readiness", daemon=True)
        readiness_thread.start()

    def stop_readiness_worker():
        nonlocal readiness_stop, readiness_thread
        if readiness_stop is not None:
            readiness_stop.set()
        if readiness_thread is not None and readiness_thread.is_alive():
            readiness_thread.join()
        readiness_stop = None
        readiness_thread = None

    settlement_context = dispatch_batch_obligations.OwnerSettlementReadContext()
    duty_retry_state = {}
    while True:
        start_readiness_worker()
        if not _runner_lock_current(store, args.lock_fd):
            # Its state root was removed or replaced (e.g. a finished test's
            # temporary home): no producer can reach this runner any more.
            stop_readiness_worker()
            settlement_context.close()
            if wake_fd is not None:
                os.close(wake_fd)
            return 0
        store.begin_read_pass()
        duties = store.list()
        from peer_read_context import PeerReadContext
        from runtime_observation import ObservationContext
        read_context = PeerReadContext(registry_append_only=True)
        observation_context = ObservationContext()
        _reconcile_obligations(store, duties, read_context=read_context,
                               retry_state=duty_retry_state)
        if service_current:
            # A service replacement may exec this process. Join the readiness
            # worker first so no old-code thread survives the process image.
            stop_readiness_worker()
            settlement_context.close()
            peer_obligations.exec_service_runner(
                args.lock_fd, store.root.parents[1], service_current,
                herdr_session=_HERDR_SESSION,
            )
            settlement_context = dispatch_batch_obligations.OwnerSettlementReadContext()
            start_readiness_worker()
        external_snapshot = _external_obligation_snapshot()
        _reconcile_external_obligations(
            store, service_current=service_current, snapshot=external_snapshot,
            settlement_context=settlement_context)
        try:
            store.prune_terminal_obligations(time_budget=0.1)
        except (OSError, ValueError, RuntimeError, peer_obligations.ObligationError):
            pass
        if runner_incarnation:
            try:
                # This is one complete readiness pass after service work may
                # have changed jobs.log. Pin its own generation before selecting
                # rows so every owner gate in the pass sees the same full view.
                readiness_context = PeerReadContext(registry_append_only=True)
                from dispatch_registry_cache import registry_read_scope
                with registry_read_scope():
                    _publish_watch_readiness(
                        store, external_snapshot, read_context=readiness_context,
                        incarnation=runner_incarnation)
            except Exception:
                pass
        current_duties = store.list()
        external_signature = _external_obligation_signature(external_snapshot)
        if not _runner_duties(store, current_duties) and not any(external_signature):
            # A producer that saw our held lock may already have accepted work.
            # Release before the final read: later producers can start a runner,
            # and work accepted before release is either reclaimed here or owned
            # by the new lock holder.
            stop_readiness_worker()
            fcntl.flock(args.lock_fd, fcntl.LOCK_UN)
            store.end_read_pass()
            latest_duties = store.list()
            latest_snapshot = external_snapshot
            if not _external_obligation_snapshot_unchanged(external_snapshot):
                latest_snapshot = _external_obligation_snapshot()
            latest_external = _external_obligation_signature(latest_snapshot)
            jobs_changed = any(dispatch_batch_obligations._registry_stamp(Path(key)) != stamp
                               for key, stamp in external_snapshot.get("jobs_stamps", {}).items())
            if (not _runner_duties(store, latest_duties) and not any(latest_external)
                    and not jobs_changed):
                settlement_context.close()
                if wake_fd is not None:
                    os.close(wake_fd)
                return 0
            try:
                fcntl.flock(args.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                settlement_context.close()
                if wake_fd is not None:
                    os.close(wake_fd)
                return 0
            # A newly accepted owner row can owe work without a sidecar. Keep
            # this runner for its next bounded pass. A settlement writer may
            # also have changed jobs itself; that must not create a busy loop.
            if _runner_duties(store, latest_duties):
                # The lock-free final read found newly accepted local work.
                # Process that work immediately; no-progress and external-only
                # passes fall through to the common bounded wait below.
                continue
            current_duties = latest_duties
            external_signature = latest_external
        signature = (_runner_duty_signature(current_duties), external_signature)
        if previous_signature is None or signature != previous_signature:
            delay = 1.0
        else:
            active_readiness = any(
                (duty.get("intent") or {}).get("kind") == shared_readiness.DUTY_KIND
                for duty in current_duties)
            active_readiness = active_readiness or _watch_readiness_pending(external_snapshot)
            delay = min(15.0, delay * 1.7)
            if active_readiness:
                delay = min(5.0, delay)
        previous_signature = signature
        readiness_fds = _runner_readiness_fds()
        wait_fds = ([wake_fd] if wake_fd is not None else []) + readiness_fds
        if wait_fds:
            ready_fds, _, _ = select.select(wait_fds, [], [], delay)
            if wake_fd is not None and wake_fd in ready_fds:
                with contextlib.suppress(OSError):
                    while os.read(wake_fd, 4096):
                        pass
        else:
            time.sleep(delay)


def _acquire_ensure_obligations_lock(store):
    """Try the shared recovery lock without waiting or unlinking its inode."""
    if not store.ensure_directory():
        return None
    path = store.root / "ensure-obligations.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        # This descriptor belongs only to this short call. A detached observer
        # or runner must never keep the shared exclusion alive after we exit.
        os.set_inheritable(fd, False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd
    except Exception:
        os.close(fd)
        raise


def cmd_ensure_obligations(_args):
    """Request the shared runner and return without repeating its global work."""
    store = peer_obligations.ObligationStore(allow_state_root_creation=False)
    try:
        lock_fd = _acquire_ensure_obligations_lock(store)
    except Exception:
        return 0
    if lock_fd is None:
        return 0
    try:
        try:
            lifecycle_root = getattr(_args, "lifecycle_root", None)
            peer_obligations.ensure_normal_runner(
                lifecycle_root=lifecycle_root, reconciliation_required=True)
        except Exception:
            pass
        return 0
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


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


def cmd_prompt(args, *, expected_recipient=None, on_prepared=None, producer_binding=None):
    """Seal a peer message and route it through the recipient's native carrier.

    Codex uses its exact thread queue. Claude's hook context and OpenCode's
    native idle callback consume their own pending rows. Queue acceptance or
    context emission remains queued; only an exact native receive is true.
    """
    if _herdr_missing():
        _prompt_failure_ledger(args.target, "herdr-not-found", [],
                               ref=getattr(args, "ref", ()))
        return _unavailable("herdr-not-found")
    if args.body_file:
        with open(args.body_file, encoding="utf-8", errors="replace", newline="") as handle:
            body = handle.read()
    elif args.body_stdin:
        body = sys.stdin.read()
    else:
        body = args.text or ""
    if not body.strip():
        print("prompted=false reason=empty-body")
        return 1

    body = body.rstrip("\n")
    if producer_binding is not None:
        if args.no_trailer:
            print("prompted=false reason=runtime-completion-requires-receipt-envelope")
            return 5
        from_sid, from_harness, from_name = None, "runtime", "Hearting runtime"
        from_identity = (None, "runtime", "")
    else:
        from_sid, from_harness = _current_session_identity()
        from_identity = (from_sid, from_harness, _project_of(os.getcwd()))
        from_name = _from_name(from_harness, from_sid)
    resolved, resolve_error, candidate_panes = _resolve_prompt_target(args.target)
    if resolved is None:
        reason = resolve_error or "target-observation-unavailable"
        _prompt_failure_ledger(args.target, reason, candidate_panes,
                               kind=_prompt_message_kind(body), ref=args.ref,
                               from_identity=from_identity, from_name=from_name)
        print("prompted=false reason=%s target=%s candidate_panes=%s" % (
            reason, json.dumps(args.target, ensure_ascii=False),
            json.dumps(candidate_panes, ensure_ascii=False, separators=(",", ":"))))
        return 1 if reason.endswith("-not-found") else 5
    resolved_target = resolved["pane"]
    t_harness, t_sid, t_name = (resolved["harness"], resolved["session_id"],
                                resolved["name"] or args.target)
    state_before, target_pane = resolved["state"], resolved["pane"]
    if expected_recipient is not None and (t_harness, t_sid) != expected_recipient:
        _prompt_failure_ledger(args.target, "target-identity-changed", [target_pane],
                               kind=_prompt_message_kind(body), ref=args.ref,
                               from_identity=from_identity, from_name=from_name)
        print("prompted=false reason=target-identity-changed")
        return 5
    recipient = {"harness": t_harness, "session_id": peer_message.usable_session_id(t_sid),
                 "name": t_name}
    if target_pane:
        recipient["pane_id"] = target_pane
    first_input_process = None
    if (t_harness in {"claude", "codex", "opencode"}
            and recipient["session_id"] is None and target_pane):
        first_input_process = _first_input_foreground_proof(
            resolved_target, t_harness, target_pane)
    readiness = _pane_readiness(resolved_target, state_before, expected_harness=t_harness,
                                expected_sid=recipient["session_id"], expected_pane=target_pane,
                                purpose="input",
                                target_observation=(state_before, resolved.get("_identity")),
                                native_session_id=resolved.get("_native_session_id") or "")
    input_reason = None if readiness.state == "ready" else (
        readiness.reason or "target-identity-unverified")

    reference_evidence = None
    if (not args.no_trailer
            and len(body.encode("utf-8")) > peer_message._BODY_REFERENCE_THRESHOLD):
        reference_evidence = _peer_reference_access_evidence(
            resolved_target, t_harness, t_sid, target_pane)
    try:
        text, transfer_ref = peer_message.prepare_peer_message(
            body, {"harness": from_harness, "session_id": from_sid, "name": from_name},
            recipient, defer=True, refs=args.ref, receipt=input_reason or "native-pending",
            with_trailer=not args.no_trailer, reference_evidence=reference_evidence,
            first_input_process=first_input_process, producer_binding=producer_binding)
        if on_prepared is not None:
            on_prepared(transfer_ref)
        pending = peer_message._read_pending(transfer_ref)
    except (OSError, ValueError):
        print("prompted=unverified reason=peer-pending-or-transfer-unavailable")
        return 5

    kind = _prompt_message_kind(body)
    started = time.monotonic()
    verify = "private-pending"
    reason = None
    surface = {"claude": "claude-native", "codex": "codex-queue",
               "opencode": "opencode-native"}.get(t_harness, "herdr")

    if pending["state"] == "received":
        outcome, verify = "true", pending.get("receipt") or "exact-native-history"
    elif input_reason:
        if pending["state"] == "pending":
            _schedule_message_obligation(pending, resolved_target, target_pane)
        outcome = "queued" if pending["state"] in {"pending", "queued"} else "unverified"
        if t_harness == "claude" and state_before == "working":
            verify = "native-callback"
        reason = input_reason
    elif t_harness == "codex" and peer_message.usable_session_id(t_sid):
        current, current_error = _prompt_current_candidate(
            resolved_target, expected_harness=t_harness, expected_sid=t_sid,
            require_process_session=True)
        if current is None:
            outcome, reason = "unverified", current_error or "target-identity-unverified"
            _schedule_message_obligation(pending, resolved_target, target_pane)
        else:
            try:
                result = peer_message.deliver_pending_codex(transfer_ref)
                outcome = "true" if result["status"] == "received" else (
                    "queued" if result["status"] == "queued" else "unverified")
                verify, reason = "native-queue-" + result["status"], result.get("reason")
            except (OSError, ValueError) as exc:
                outcome, reason = "unverified", "peer-pending-unavailable:" + str(exc)
    elif (t_harness in {"claude", "codex", "opencode"}
          and not peer_message.usable_session_id(t_sid)):
        direct_reason = None
        admission_proof = peer_message.pending_first_input_process(pending)
        if args.no_trailer:
            direct_reason = "no-trailer-callback-only"
        elif pending["state"] != "pending":
            direct_reason = "peer-delivery-already-submitted"
        elif admission_proof is None:
            direct_reason = "target-process-proof-unknown"
        else:
            direct_reason, proof = _sidless_idle_prompt_guard(
                resolved_target, t_harness, target_pane, expected_foreground=admission_proof)
            if direct_reason is None:
                try:
                    claimed = peer_message.reserve_pending_first_input(
                        transfer_ref, {"harness": t_harness, "pane_id": target_pane}, proof)
                except (OSError, ValueError):
                    claimed = None
                if claimed is None:
                    direct_reason = "peer-delivery-already-claimed"
                else:
                    direct_reason, _proof = _sidless_idle_prompt_guard(
                        target_pane, t_harness, target_pane,
                        expected_foreground=admission_proof)
                    if direct_reason:
                        peer_message.release_unsent_herdr_claim(
                            claimed, receipt=direct_reason)
                    else:
                        reserved = claimed
                        claimed = peer_message.begin_pending_write(
                            claimed, recipient={"harness": t_harness, "pane_id": target_pane},
                            receipt="native-first-input-inflight")
                        if claimed is None:
                            peer_message.release_pending_herdr_claim(reserved)
                            direct_reason = "peer-delivery-already-claimed"
                    if direct_reason is None:
                        # This is the one external submission attempt. A lost or
                        # malformed reply stays unverified; it is never retried.
                        rc, payload = _herdr_prompt(
                            target_pane, pending["text"], wait=False,
                            timeout_ms=_PROMPT_VERIFY_TIMEOUT_MS)
                        if rc != 0:
                            outcome = "unverified"
                            verify = "native-first-input-unverified"
                            reason = (_herdr_error_reason(payload, rc)
                                      if rc is not None else "herdr-invocation-failed")
                        else:
                            peer_message.mark_pending_prompt_queued(
                                transfer_ref, claimed["rpc_claim"],
                                receipt="native-first-input-submitted")
                            current = peer_message._read_pending(transfer_ref)
                            if current and current.get("state") == "received":
                                outcome, verify = "true", "exact-peer-ref"
                            elif current and current.get("state") == "queued":
                                outcome, verify = "queued", "native-first-input-submitted"
                                reason = "awaiting-exact-receiver-ref"
                            else:
                                outcome, verify = "unverified", "native-first-input-unverified"
                                reason = "peer-state-unavailable-after-submit"
        if direct_reason is not None:
            if pending["state"] == "pending":
                _schedule_message_obligation(pending, resolved_target, target_pane)
            outcome = "queued" if pending["state"] in {"pending", "queued"} else "unverified"
            verify = "first-input-deferred"
            reason = direct_reason
        elif outcome == "unverified":
            _schedule_message_obligation(pending, resolved_target, target_pane)
    elif t_harness == "claude":
        direct_reason = None
        if args.no_trailer:
            direct_reason = "no-trailer-callback-only"
        elif pending["state"] != "pending":
            direct_reason = "peer-delivery-already-submitted"
        elif state_before not in ("idle", "done"):
            direct_reason = "native-state-" + str(state_before)
        elif not target_pane or not peer_message.usable_session_id(t_sid):
            direct_reason = "target-identity-unverified"
        else:
            direct_reason, proof = _claude_idle_prompt_guard(
                resolved_target, t_sid, target_pane)
            if direct_reason is None:
                try:
                    claimed = peer_message.reserve_pending_herdr(
                        transfer_ref, {"harness": "claude", "session_id": t_sid,
                                       "pane_id": target_pane},
                        receipt="claude-pty-prompt-inflight")
                except (OSError, ValueError):
                    claimed = None
                if claimed is None:
                    direct_reason = "peer-delivery-already-claimed"
                else:
                    direct_reason, _proof = _claude_idle_prompt_guard(
                        target_pane, t_sid, target_pane, expected_foreground=proof)
                    if direct_reason:
                        peer_message.release_unsent_herdr_claim(
                            claimed, receipt=direct_reason)
                        claimed = None
                    else:
                        reserved = claimed
                        claimed = peer_message.begin_pending_write(
                            claimed, recipient={"harness": "claude", "session_id": t_sid,
                                                "pane_id": target_pane},
                            receipt="claude-pty-prompt-inflight")
                        if claimed is None:
                            peer_message.release_pending_herdr_claim(reserved)
                            direct_reason = "peer-delivery-already-claimed"
                        else:
                            rc, payload = _herdr_prompt(
                                target_pane, pending["text"], wait=False,
                                timeout_ms=_PROMPT_VERIFY_TIMEOUT_MS)
                            if rc != 0:
                                outcome = "unverified"
                                verify = "native-prompt-unverified"
                                reason = (_herdr_error_reason(payload, rc)
                                          if rc is not None else "herdr-invocation-failed")
                            else:
                                peer_message.mark_pending_prompt_queued(
                                    transfer_ref, claimed["rpc_claim"])
                                current = peer_message._read_pending(transfer_ref)
                                if current and current.get("state") == "received":
                                    outcome, verify = "true", "exact-peer-ref"
                                elif current and current.get("state") == "queued":
                                    outcome, verify = "queued", "native-prompt-submitted"
                                    reason = "awaiting-exact-receiver-ref"
                                else:
                                    outcome, verify = "unverified", "native-prompt-unverified"
                                    reason = "peer-state-unavailable-after-submit"
        if direct_reason is not None:
            if pending["state"] == "pending":
                _schedule_message_obligation(pending, resolved_target, target_pane)
            outcome = "queued" if pending["state"] in {"pending", "queued"} else "unverified"
            verify = "native-callback"
            reason = direct_reason
        elif outcome == "unverified":
            _schedule_message_obligation(pending, resolved_target, target_pane)
    else:
        if pending["state"] == "pending":
            _schedule_message_obligation(pending, resolved_target, target_pane)
        outcome = "queued" if pending["state"] in {"pending", "queued"} else "unverified"
        reason = ("target-session-unresolved" if not peer_message.usable_session_id(t_sid)
                  else "awaiting-native-receiver")

    elapsed_ms = int((time.monotonic() - started) * 1000)
    receipt = (f"prompted={outcome} state_before={state_before} verify={verify} "
               f"native_id={pending.get('actual_message_id') or '-'} ms={elapsed_ms}"
               + (f" reason={reason}" if reason else ""))
    _record(to_harness=t_harness or "unknown", to_name=args.target, kind=kind,
            summary_text=text, to_session_id=t_sid, to_pane=target_pane,
            ref=args.ref, status=_PROMPT_LEDGER_STATUS[outcome], receipt=receipt,
            from_identity=from_identity, from_name=from_name, transfer_ref=transfer_ref,
            surface=surface, producer_binding=producer_binding)
    line = (f"prompted={outcome} target={args.target} "
            f"to_harness={t_harness or '-'} to_alias={peer_message.peer_alias(t_harness, t_sid)} kind={kind} "
            f"state_before={state_before} verify={verify} ms={elapsed_ms}")
    if reason:
        line += f" reason={reason}"
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
    if _blocking_screen(harness, lines):
        return "form-open"
    draft = _draft_state(harness, lines)
    if draft == "nonempty":
        return "draft"
    if draft != "empty":
        return "draft-unknown"
    return None


def _codex_footer_threads(lines):
    """Strict current-footer identity shared by clear observation and pane ownership."""
    from pane_ownership import codex_footer_threads
    return codex_footer_threads([_plain(cells) for cells in lines], current_id_only=True)


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
    return "native-trust-wait" if _blocking_screen(harness, lines) == "native-trust-wait" else None


def _process_session(pane, harness):
    """The session the pane's foreground runtime process is on now, or None.

    Claude uses `sessions/<pid>.json`, rewritten on `/clear`; Codex uses
    process_identity for the foreground TUI, with a visible footer as an optional
    mismatch check; OpenCode uses its TUI selection record. Herdr's own `agent_session`
    follows a `/clear` only when its integration reports it, and has been seen to keep
    the previous session for good (live panes 2026-10-01), so a second tidy in the same
    window would otherwise never clear it. Shared Codex callers use CODEX_THREAD_ID;
    pane_ownership resolves that thread to its foreground TUI."""
    try:
        proc = subprocess.run(_herdr_argv("pane", "process-info", "--pane", pane),
                              capture_output=True, text=True, timeout=_herdr_get_timeout())
        payload = json.loads(proc.stdout or "")
        processes = ((payload.get("result") or {}).get("process_info") or {}).get("foreground_processes") or []
    except Exception:
        return None
    if harness == "codex":
        from pane_ownership import current_codex_thread
        executable = shutil.which("herdr")
        for process in processes:
            try:
                current = current_codex_thread(
                    pane, int(process.get("pid")), executable=executable,
                    server=_HERDR_SESSION)
            except Exception:
                current = ""
            if current:
                return current
        return None
    return _proven_session(processes, harness)


def _continue_native_session(pane, harness, *, process_info):
    """Resolve the exact foreground TUI session for the continue action boundary."""
    if not isinstance(process_info, dict) or process_info.get("pane_id") != pane:
        return None
    foreground = _retire_foreground(pane, harness, strict=False, info=process_info)
    if not foreground:
        return None
    if harness == "codex":
        try:
            from pane_ownership import current_codex_thread
            session_id = current_codex_thread(
                pane, foreground["pid"], executable=shutil.which("herdr"),
                server=_HERDR_SESSION)
        except Exception:
            return None
    else:
        session_id = _proven_session([{"pid": foreground["pid"]}], harness)
    if not peer_message.usable_session_id(session_id):
        return None
    latest = _retire_process_record(foreground["pid"])
    if not latest or latest.get("start") != foreground.get("start"):
        return None
    if harness != "codex" and _proven_session([{"pid": foreground["pid"]}], harness) != session_id:
        return None
    return session_id


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
        seat = st.seat_from_dict(seat_fields)
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
    if req.get("harness") == "opencode":
        # Herdr may retain an old pane session after the TUI selects another one.
        # The PID/birth-bound TUI record is the only authority for this seat.
        current = _process_session(agent.get("pane") or target, "opencode")
        if current != req.get("sid"):
            return "target-changed" if current else "session-identity-unknown", agent, None
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
        seat = st.seat_from_dict(seat_fields)
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
        if req.get("harness") == "codex":
            menu_lines = _read_screen(target)
            if menu_lines is not None and _blocking_screen(req.get("harness"), menu_lines):
                if round_no + 1 < _CLEAR_OBSERVE_ROUNDS:
                    _run_herdr_wait(target, ["working", "blocked"], _CLEAR_OBSERVE_SETTLE_MS)
                continue
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
        opencode_native = None
        if not unavailable and req.get("harness") == "opencode":
            opencode_native = _process_session(agent.get("pane") or target, "opencode")
            if opencode_native and opencode_native != old:
                return str(opencode_native)
        if not unavailable and req.get("harness") == "codex":
            thread = _codex_new_thread(target, req, before_threads)
            if thread:
                return thread
        if (not unavailable and req.get("harness") == "opencode" and not opencode_native
                and state in ("idle", "done")):
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
    if req.get("snapshot_state") in ("pending", "deferred"):
        print("cleared=skipped reason=snapshot-pending")
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
    req, why = clear.claim_clear(args.request, args.nonce)
    if req is None:
        return finish("skipped", why, agent)
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


def cmd_idle_card_request(args):
    """Send one claim-bound request to the exact idle main session to author its own card."""
    import session_tidy_clear as clear
    path = Path(args.request)
    req, why = clear.validate_idle_request(path, args.nonce)

    def finish(outcome, reason=""):
        harness = str((req or {}).get("harness") or "unknown")
        sid = str((req or {}).get("sid") or "")
        pane = (req or {}).get("seat", {}).get("pane")
        receipt = (f"action=idle-card-request accepted={outcome} harness={harness} session={sid}"
                   + (f" reason={reason}" if reason else ""))
        try:
            _record(to_harness=harness, to_name=args.target, kind="notice",
                    summary_text="[notice] session-tidy idle card request", to_session_id=sid,
                    to_pane=pane, ref=[], status="sent" if outcome == "true" else "unknown",
                    receipt=receipt, from_identity=(sid, harness, _project_of((req or {}).get("cwd"))),
                    from_name=_from_name(harness, sid))
        except Exception:
            pass
        print(f"accepted={outcome} target={args.target} harness={harness} session={sid}"
              + (f" reason={reason}" if reason else ""))
        return 0 if outcome == "true" else (3 if outcome in ("queued", "skipped") else 5)

    if req is None:
        return finish("skipped", why)
    seat = req.get("seat") or {}
    if seat.get("kind") == "native":
        from session_tidy_native import idle_card_request
        verdict = idle_card_request(req, str(path), args.nonce, screen_ready=_screen_ready)
        return finish(verdict.get("accepted", "failed"), verdict.get("reason", ""))
    if _herdr_missing():
        return finish("failed", "herdr-not-found")
    if seat.get("kind") != "pane" or seat.get("pane") != args.target:
        return finish("skipped", "target-changed")

    def exact_session(agent):
        if req.get("harness") != "opencode":
            return True
        if isinstance(agent, dict) and agent.get("session_id") not in (None, "", req.get("sid")):
            return False
        try:
            import session_tidy as st
            seat = st.seat_from_dict(req["seat"])
            latest = st.latest_session(seat, "opencode")
            return bool(latest and latest.get("sid") == req.get("sid"))
        except Exception:
            return False

    reason, agent, _lines = _clear_look(args.target, req)
    if not reason and not exact_session(agent):
        reason = "target-changed"
    if reason:
        queued = reason in {"draft", "draft-unknown", "form-open", "screen-unknown"} \
            or reason.startswith("not-idle-")
        return finish("queued" if queued else _look_outcome(reason), reason)
    req, why = clear.validate_idle_request(path, args.nonce)
    if req is None:
        return finish("skipped", why)
    reason, agent, _lines = _clear_look(args.target, req)  # immediately before the one claimed send
    if not reason and not exact_session(agent):
        reason = "target-changed"
    if reason:
        queued = reason in {"draft", "draft-unknown", "form-open", "screen-unknown"} \
            or reason.startswith("not-idle-")
        return finish("queued" if queued else _look_outcome(reason), reason)
    claim, why = clear.claim_idle_card_request(path, args.nonce)
    if claim is None:
        return finish("skipped", why)
    reason, agent, _lines = _clear_look(args.target, claim)
    if not reason and not exact_session(agent):
        reason = "target-changed"
    if reason:
        clear.release_unsent_idle_request(claim)
        queued = reason in {"draft", "draft-unknown", "form-open", "screen-unknown"} \
            or reason.startswith("not-idle-")
        return finish("queued" if queued else _look_outcome(reason), reason)
    checked, why = clear.validate_idle_request(path, args.nonce, _sending_claim=claim.get("idle_request"))
    if checked is None:
        clear.release_unsent_idle_request(claim)
        return finish("skipped", why)
    text = clear.idle_card_request_text(claim)
    rc, payload = _herdr_prompt(args.target, text, wait=True, timeout_ms=_PROMPT_VERIFY_TIMEOUT_MS)
    if rc != 0:
        reason = "herdr-invocation-failed" if rc is None else _herdr_error_reason(payload, rc)
        clear.finish_idle_card_request(claim, accepted=False, reason=reason)
        return finish("unverified", reason)
    if clear.finish_idle_card_request(claim, accepted=True):
        return finish("true")
    return finish("unverified", "arrival-not-observed")


def _look_outcome(reason):
    return "failed" if reason.startswith(("herdr-", "agent-not-found")) else "skipped"


# --- continue: the one prompt that lets the cleared window's new session carry on (session-tidy) ---

_CONTINUE_LOOK_ROUNDS = 12            # bounded re-looks while the new session settles (idle, card handed over)
_CONTINUE_SETTLE_MS = 1500


def _booked_seat(req):
    import session_tidy as st
    fields = req.get("seat") or {}
    return st, st.seat_from_dict(fields)


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
    """Check the exact booked session and native idle state without reading its composer."""
    harness, new = req.get("harness"), str(req.get("new_session") or "")
    state, ident, _code, reason = _retire_target(target)
    if reason or state in ("agent-not-found", "timeout", "unknown"):
        return reason or state
    pane = (req.get("seat") or {}).get("pane")
    if (ident.get("harness") != harness or not pane or ident.get("pane") != pane):
        return "target-changed"
    if not new or new == "-":
        return "new-session-unknown"
    if state not in ("idle", "done"):
        return f"not-idle-{state}"
    card = _continue_card_reason(req)
    if card:
        return card
    info = _retire_pane_info(pane)
    if not isinstance(info, dict):
        return "target-identity-unknown"
    native_sid = _continue_native_session(pane, harness, process_info=info)
    if not peer_message.usable_session_id(native_sid):
        return "new-session-unknown"
    if native_sid != new:
        return "target-changed"
    # The continue is input to an idle session: work it dispatched (a supervisor's
    # owners) keeps running and must not hold the continue back.
    readiness = _pane_readiness(target, state, expected_harness=harness,
                                expected_sid=new, expected_pane=pane, purpose="input",
                                target_observation=(state, ident), pane_info=info,
                                native_session_id=native_sid)
    if readiness.state != "ready":
        return readiness.reason or "target-identity-unknown"
    return None


def cmd_continue(args):
    """Deliver the booked no-trailer continue to the exact native receiver."""
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

    harness, old_sid = req.get("harness"), req.get("sid")
    new_sid, target = req.get("new_session"), args.target
    text = clear.followup_text(req)
    started = time.monotonic()
    verify = "private-pending"
    pane = (req.get("seat") or {}).get("pane")
    claim_id = None
    transfer_ref = (req.get("continued") or {}).get("transfer_ref")

    def finish(outcome, reason=None, *, native_ref=transfer_ref):
        elapsed_ms = int((time.monotonic() - started) * 1000)
        receipt = (f"action=continue continued={outcome} harness={harness} old_session={old_sid} "
                   f"new_session={new_sid or '-'} verify={verify} ms={elapsed_ms}"
                   + (f" reason={reason}" if reason else "")
                   + (f" claim_id={claim_id}" if claim_id else "")
                   + (f" transfer_ref={native_ref}" if native_ref else ""))
        try:
            _record(to_harness=harness or "unknown", to_name=target, kind="notice",
                    summary_text=f"[notice] action=continue {outcome} {text}",
                    to_session_id=new_sid if new_sid not in (None, "", "-") else None,
                    to_pane=pane, ref=[], status=_CLEAR_LEDGER_STATUS[outcome], receipt=receipt,
                    from_identity=(old_sid, harness, _project_of(req.get("cwd"))),
                    from_name=_from_name(harness, old_sid))
        except Exception:
            pass
        line = (f"continued={outcome} target={target} harness={harness} "
                f"new_session={new_sid or '-'} verify={verify}"
                + (f" reason={reason}" if reason else "")
                + (f" claim_id={claim_id}" if claim_id else "")
                + (f" transfer_ref={native_ref}" if native_ref else ""))
        print(line)
        return _CLEAR_EXIT[outcome]

    if transfer_ref:
        try:
            existing = peer_message._read_pending(transfer_ref)
        except (OSError, ValueError):
            return finish("unverified", "continue-transfer-unreadable")
        if (not existing or existing.get("no_trailer") is not True
                or existing.get("text") not in (None, text)
                or (existing.get("to") or {}).get("harness") != harness
                or (existing.get("to") or {}).get("session_id") != new_sid):
            return finish("unverified", "continue-transfer-mismatch")
        if existing.get("state") == "received":
            verify = existing.get("receipt") or "exact-native-history"
            return finish("true", native_ref=transfer_ref)

    reason = None
    for round_no in range(_CONTINUE_LOOK_ROUNDS):
        reason = _continue_look(target, req)
        if reason is None or not reason.startswith(("not-idle-", "card-pending")):
            break
        if round_no + 1 < _CONTINUE_LOOK_ROUNDS:
            until = ["idle", "done"] if reason.startswith("not-idle-") else ["working", "blocked"]
            _run_herdr_wait(target, until, _CONTINUE_SETTLE_MS)
    if reason == "card-pending":
        reason = "card-not-delivered"
    if reason:
        return finish("queued" if reason.startswith(("not-idle-", "new-session-unknown"))
                      or reason == "card-not-delivered" else _look_outcome(reason), reason)

    req, why = clear.validate_continue(args.request, args.nonce)
    if req is None:
        return finish("skipped", why)
    reason = _continue_look(target, req)
    if reason:
        return finish("queued" if reason.startswith(("not-idle-", "new-session-unknown"))
                      else _look_outcome(reason), reason)
    req, why = clear.claim_continue(args.request, args.nonce)
    if req is None:
        return finish("skipped", why)
    claim_id = (req.get("continued") or {}).get("claim_id")
    reason = _continue_look(target, req)
    if reason:
        clear.release_unsent_continue(req)
        return finish("queued" if reason.startswith(("not-idle-", "new-session-unknown"))
                      else _look_outcome(reason), reason)

    seat = _booked_seat(req)[1]
    seq_before = int(req.get("prompt_seq", 0) or 0)
    if transfer_ref:
        row = peer_message._read_pending(transfer_ref)
    else:
        sender = {"harness": harness, "session_id": old_sid,
                  "name": _from_name(harness, old_sid)}
        recipient = {"harness": harness, "session_id": new_sid, "name": None,
                     "pane_id": pane}
        try:
            _body, transfer_ref = peer_message.prepare_peer_message(
                text, sender, recipient, defer=True, with_trailer=False,
                receipt="session-tidy-continue")
            row = peer_message._read_pending(transfer_ref)
        except (OSError, ValueError):
            clear.release_unsent_continue(req)
            return finish("unverified", "continue-transfer-unavailable", native_ref=None)
    if not clear.bind_continue_transfer(req, transfer_ref):
        return finish("skipped", "continue-claim-changed", native_ref=transfer_ref)

    if harness == "codex":
        try:
            result = peer_message.deliver_pending_codex(transfer_ref)
        except (OSError, ValueError) as exc:
            result = {"status": "unverified", "reason": str(exc)}
        if result.get("status") == "received":
            verify, outcome, reason = "exact-native-history", "true", None
        elif result.get("status") == "queued":
            verify, outcome, reason = "native-queue-accepted", "queued", result.get("reason")
        else:
            verify, outcome, reason = "native-queue-unverified", "queued", result.get("reason")
    elif harness == "claude":
        verify, outcome, reason = _continue_claude_prompt(target, pane, new_sid, transfer_ref, text)
    else:
        # The OpenCode idle plugin owns the next native delivery.
        # It will retain this exact no-trailer ref until the booked SID consumes it.
        verify, outcome, reason = "native-pending", "queued", "awaiting-native-receiver"

    if (outcome in ("queued", "unverified")
            and _booked_seat(req)[0].read_prompt_seq(seat) > seq_before):
        verify, outcome, reason = "prompt-hook", "true", None
    return finish(outcome, reason, native_ref=transfer_ref)


def _continue_claude_prompt(target, pane, sid, ref, text):
    """Type the booked continue once into the exact idle Claude TUI.

    A fresh Claude session after /clear reaches no hook boundary until something is
    typed, so leaving the ref for the hook leaves the window empty. The guards are the
    idle Claude prompt path of `cmd_prompt`; any refusal keeps the ref pending for the
    hook. A sent ref is never released, so the hook cannot deliver it a second time.
    """
    reason, proof = _claude_idle_prompt_guard(target, sid, pane)
    if reason:
        return "native-pending", "queued", reason
    try:
        claimed = peer_message.reserve_pending_herdr(
            ref, {"harness": "claude", "session_id": sid, "pane_id": pane},
            receipt="claude-pty-prompt-inflight")
    except (OSError, ValueError):
        claimed = None
    if claimed is None:
        return "native-pending", "queued", "peer-delivery-already-claimed"
    reason, _proof = _claude_idle_prompt_guard(pane, sid, pane, expected_foreground=proof)
    if reason:
        peer_message.release_unsent_herdr_claim(claimed, receipt=reason)
        return "native-pending", "queued", reason
    reserved = claimed
    claimed = peer_message.begin_pending_write(
        claimed, recipient={"harness": "claude", "session_id": sid, "pane_id": pane},
        receipt="claude-pty-prompt-inflight")
    if claimed is None:
        peer_message.release_pending_herdr_claim(reserved)
        return "native-pending", "queued", "peer-delivery-already-claimed"
    rc, payload = _herdr_prompt(pane, text, wait=True, timeout_ms=_PROMPT_VERIFY_TIMEOUT_MS)
    if rc == 0:
        peer_message.mark_pending_queued(ref, receipt="claude-pty-continue-submitted")
        return "state-flip", "true", None
    reason = _herdr_error_reason(payload, rc) if rc is not None else "herdr-invocation-failed"
    return "native-prompt-unverified", "unverified", reason

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
    p_obligation.add_argument("--service-current", default=None, help=argparse.SUPPRESS)
    p_obligation.set_defaults(func=cmd_obligation_runner)

    p_recover = sub.add_parser("__ensure-obligations", help=argparse.SUPPRESS)
    p_recover.add_argument("--lifecycle-root", default=None, help=argparse.SUPPRESS)
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
    p_run.add_argument("--service-current", default=None, help=argparse.SUPPRESS)
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
    p_prompt.set_defaults(func=cmd_prompt)

    p_clear = sub.add_parser("clear")
    p_clear.add_argument("target")
    p_clear.add_argument("--request", required=True,
                         help="the seat's auto-clear booking (session_tidy_clear.py)")
    p_clear.add_argument("--nonce", default=None, help="the booking's nonce, when the caller holds one")
    p_clear.set_defaults(func=cmd_clear)

    p_idle_card = sub.add_parser("idle-card-request")
    p_idle_card.add_argument("target")
    p_idle_card.add_argument("--request", required=True)
    p_idle_card.add_argument("--nonce", default=None)
    p_idle_card.set_defaults(func=cmd_idle_card_request)

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
        selected_env = _herdr_env_for_server(args.herdr_session)
        for key in ("AGENT_HERDR_SESSION", "HERDR_SESSION", "HERDR_SOCKET_PATH"):
            if key in selected_env:
                os.environ[key] = selected_env[key]
            else:
                os.environ.pop(key, None)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
