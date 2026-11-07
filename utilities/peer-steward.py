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

Module map. This file keeps the command line and every subcommand, the
herdr session and watch wake state, the readiness API, retire and handoff
bookkeeping, and the obligation runner -- module state (globals rebound with
`global`, mutable caches) and every function that reads it directly or
through calls, plus code that names its own module. Stateless code lives in
sibling modules; each imports only from the ones above it in this list, and
this file re-exports every name they define:

    peer_steward_base     constants, prompt/herdr/tmux/retire helpers
    peer_steward_watch    watch identities, claims, arms, close evidence, join
    peer_steward_status   status rows, watch readiness and transition helpers
    peer_steward_native   native receiving-turn resume, settled native receipts
    peer_steward_duties   runner duty discovery, reconcile limits, peer indexes

A test that replaces a name with `mock.patch` must patch the module whose code
looks the name up; patching it here reaches only the code in this file and
callers that go through this module. `split_module_fixture.patch_split` and
`set_split` reach this file and every part that binds the name.
"""
import argparse
import contextlib
import datetime
import errno
import functools
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
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

_UTILITIES_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(_UTILITIES_DIR))
from dispatch_contract import (pidfd_open, process_start_ticks, resolve_agent_home,
                               resolve_dispatch_state_root)  # noqa: E402
from parent_next_directive import steward_fields  # noqa: E402
import peer_obligations  # noqa: E402
import shared_readiness  # noqa: E402
import dispatch_batch_obligations  # noqa: E402
import herdr_observation  # noqa: E402
import terminal_surface  # noqa: E402


# Re-exported so every name this module defined before the split stays an
# attribute of `peer-steward` (callers, older releases and tests use them).
from peer_steward_base import (  # noqa: F401
    _AGENT_STATES, _ANSI_TOKEN, _BESIDE_READY_SECONDS, _caller_is_claude, _CLAIM_TIMEOUT_MS,
    _codex_home_dir, _CWD_FLAG, _default_permission_mode, _DEFAULTS_SPEC,
    _ensure_steward_runner, _fallback, _from_name, _handoff_lifetime_valid,
    _headless_session_bindings, _herdr_env_for_server, _HERDR_GET_TIMEOUT_SECONDS,
    _herdr_missing, _interpret_payload, _MANAGED_ENTRY_MARK, _MANAGED_ENV_MARK,
    _MANAGED_INGRESS, _managed_ingress_dir, _OPENCODE_RETIRE_SECONDS,
    _opencode_tui_scoped_config, _PATHS_SPEC, _payload_cwd, _PERMISSION_FLAGS, _plain,
    _PM_SPEC, _print_steward_limited, _proc_cwd, _proc_start_ticks, _project_of,
    _PROMPT_BARE_TAG_ADDRESS, _prompt_collapse_rows, _prompt_fleet_tag, _PROMPT_HARNESSES,
    _prompt_inventory_fields, _prompt_inventory_panes, _prompt_listed_session_id,
    _prompt_message_kind, _PROMPT_TAG_ADDRESS, _retained_tui_selection, _RETIRE_ACTIONS,
    _retire_background_dialog_lines, _retire_booked_foreground, _retire_predecessor,
    _retire_process_record, _RETIRE_SECONDS, _retire_shell_returned, _retire_subject_state,
    _retire_zombie_matches, _screen_lines, _seat_successor_lock, _seat_successor_path,
    _session_registry, _source_before_acceptance, _start_request_key, _start_stderr_code,
    _surface_missing, _tmux_agent, _tmux_census, _tmux_input_refusal, _TMUX_INPUT_WAIT,
    _tmux_inventory, _tmux_launch_line, _tmux_named_pane, _tmux_ready_shell,
    _tmux_shell_identity, _TMUX_START_SECONDS, _tmux_surface, _tmux_target_address,
    _typed_line, _unavailable, _UNSET_NAME, _UTILITIES_DIR, DEFAULTS, end_with_test,
    INSTALL_PATHS, peer_message,
)
from peer_steward_watch import (  # noqa: F401
    _acquire_bounded, _alive, _already_armed_line, _armed_line, _boot_id,
    _confirmed_watch_successor, _dedupe_key, _exit_for_state, _herdr_get_timeout, _JOIN_EXIT,
    _JoinTimeout, _lock_held, _logical_watch_claim_key, _new_watch_id, _open_lock,
    _persist_watch_session, _persist_watch_session_under_alias_lock, _pid_identity_ok,
    _prepare_watch_notice, _preserve_watch_native_result_unknown, _read_json, _receipt_line,
    _record_own_watch_identity, _record_watch_native_result, _seat_claim_path,
    _steward_check_module, _steward_seat, _stop_watch_wait, _terminal_watch_request,
    _until_field, _utc_now, _wake_current_watch_observer, _wake_watch_observer,
    _watch_alias_lock_key, _watch_arm_matches, _watch_arm_scope_matches,
    _watch_claim_path_for_arm, _watch_close_reason, _watch_deadline, _watch_entries,
    _watch_monotonic_deadline, _watch_native_binding_evidence, _watch_native_result_identity,
    _watch_observer_duty_argv, _watch_observer_takeover_argvs, _watch_paths,
    _watch_readiness_identity_matches_arm, _WATCH_RECEIPT_CONTENDED, _WATCH_RECEIPT_EXPIRED,
    _WATCH_RECEIPT_PENDING, _watch_registered_work_pending, _watch_root, _WATCH_SCHEMA,
    _watch_selected_readiness_endpoint, _WATCH_SERVER_ENDPOINT_PREFIX, _WATCH_STATES,
    _watch_terminal_from_view, _watch_timeout_remaining_ms, _WATCH_TRANSITION_LOCK_BUDGET_SEC,
    _WatchCloseEvidence, _WatchDeadlineExpired, _watcher_present, _WatchPaths,
    _WatchTransitionLockBusy, _write_json_atomic, _write_watch_claim, _write_watch_observation,
    cmd_join,
)
from peer_steward_status import (  # noqa: F401
    _arm_steward_facts, _blocking_screen, _codex_location_menu, _CODEX_LOCATION_TOKENS,
    _EXEC_NEVER_STARTED_ERRNOS, _FORM_TOKENS, _herdr_prompt_payload, _NATIVE_TRUST_TOKENS,
    _peer_status_codex_observer, _peer_status_line, _PEER_STATUS_MAX_ROWS,
    _PEER_STATUS_MAX_THREADS, _peer_status_rows, _PEER_STATUS_RPC_BUDGET,
    _PEER_STATUS_RPC_TIMEOUT, _PROMPT_EXIT, _PROMPT_LEDGER_STATUS, _PROMPT_VERIFY_TIMEOUT_MS,
    _read_watch_steward_index, _receipt_watch_entries, _status_entry, _tmux_prompt,
    _wake_shared_runner, _watch_may_reach, _watch_note_transition_lock_contention,
    _watch_readiness_fields_match, _watch_selected_jobs_path, _WATCH_STEWARD_INDEX,
    _WATCH_STEWARD_INDEX_MAX_BYTES, _watch_transition_authority_lock,
    _write_watch_steward_index,
)
from peer_steward_native import (  # noqa: F401
    _census_session_presence, _closed_registered_route_outcome, _FLUSH_MAX_ROWS,
    _FLUSH_ROW_TIMEOUT_S, _FLUSH_STUCK_HOURS, _message_obligation_id,
    _native_continuation_message, _NATIVE_QUEUE_MESSAGE_LIMIT, _native_user_message_sources,
    _resume_native_receiving_turn, _settle_claude_native_dispatch_receipt,
    _target_from_readiness_view,
)
from peer_steward_duties import (  # noqa: F401
    _acquire_ensure_obligations_lock, _CLEAR_COMMAND, _CLEAR_EXIT, _CLEAR_LEDGER_STATUS,
    _CLEAR_OBSERVE_ROUNDS, _CLEAR_OBSERVE_SETTLE_MS, _codex_footer_threads,
    _codex_rollout_exists, _component_has_open_binding, _component_has_role_rows,
    _component_last_activity, _discover_session_notice_duties, _draft_state,
    _external_obligation_signature, _external_obligation_snapshot,
    _external_obligation_snapshot_unchanged, _herdr_lags, _lineage_components,
    _live_recipients, _marker_remainder, _native_trust_reason, _obligation_retry_fingerprint,
    _open_run_ids, _OPENCODE_HOME_PLACEHOLDER, _opencode_left_text, _peer_history_order,
    _prefill_live_inboxes, _prefill_recipient_inbox, _prompt_carrier_note, _proven_session,
    _prune_received_peer_history, _publish_lineage_rows_duty, _publish_peer_indexes,
    _publish_run_states, _publish_steward_relations, _purge_retired_archives, _REF_LOCK_NAME,
    _ROLE_ROW_EVENTS, _ROLE_ROW_KEYS, _RULE_LINE, _run_jobs_for_row, _runner_duty_signature,
    _runner_lock_current, _screen_ready, _settled_handoff_acceptance_duties,
    _sid_has_open_notice, _watch_readiness_identity, _watch_readiness_pending,
    _watch_reconciliation_needed, cmd_ensure_obligations, INBOX_PREFILL_DUE_SECONDS,
    LINEAGE_ROWS_DUE_SECONDS, MAINTENANCE_DUE_SECONDS, MAINTENANCE_TIME_BUDGET_S,
    NOTICE_BATCH_LIMIT, PEER_HISTORY_RETENTION_SECONDS, PEER_HISTORY_TIME_BUDGET_S,
    PEER_INDEX_DUE_SECONDS, PEER_INDEX_TIME_BUDGET_S, RECONCILE_DUE_SECONDS,
    RECONCILE_MAX_ROWS, RECONCILE_TIME_BUDGET_S, RETIRE_ACTIVITY_SECONDS,
    RETIRE_ARCHIVE_DIRECTORY, RETIRE_ARCHIVE_SECONDS, RUN_STATES_DUE_SECONDS,
    STEWARD_RELATIONS_DUE_SECONDS,
)

# Moved classes report the module they were defined in before the split (for
# this path-loaded file, the first copy loaded in the process), so tracebacks
# and reprs read as before.
for _moved_class in (
    _JoinTimeout, _WatchCloseEvidence, _WatchDeadlineExpired, _WatchPaths,
    _WatchTransitionLockBusy,
):
    if _moved_class.__module__.startswith("peer_steward_"):
        _moved_class.__module__ = __name__
del _moved_class

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
_HANDOFF_ACCEPTANCE_REPLAYED = set()


def _herdr_argv(*args):
    """`herdr [--session <name>] <args...>` for the selected herdr session."""
    argv = ["herdr"]
    if _HERDR_SESSION:
        argv += ["--session", _HERDR_SESSION]
    return argv + list(args)

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


def _caller_pane():
    """The common live ownership judgment, never the inherited pane label."""
    sid, harness = _current_session_identity()
    return _verified_caller_pane(harness, sid)


def _verified_caller_pane(harness, sid):
    """The caller's own Herdr pane, else (no Herdr pane hint) its own tmux pane, proven live."""
    from pane_ownership import verified_pane
    pane = verified_pane(os.environ.get("HERDR_PANE_ID", ""), harness, sid,
                         server=_HERDR_SESSION)
    if pane or os.environ.get("HERDR_PANE_ID"):
        return pane
    tmux_pane, socket = terminal_surface.caller_tmux_pane()
    return verified_pane(tmux_pane, harness, sid, tmux=socket) if tmux_pane else ""


def _resolve_target(target):
    """`herdr agent get <target>` → (harness, session_id, name); every miss is None.
    herdr reports an id for Claude (UUID) and Codex (thread id), none for OpenCode
    (measured 2026-09-03) — so an OpenCode target keeps session_id=None and the pane
    pid probe in Fleet's herdr collector is what joins it."""
    tmux = _tmux_surface(target)
    if tmux is not None:
        payload = tmux.agent(target)
    elif _surface_missing(target):
        return None, None, None
    else:
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


def _prompt_agent_inventory():
    """Read the selected herdr endpoint's complete agent inventory, or explain why not.

    Without herdr, the tmux server this process runs in answers the same shape."""
    tmux = _tmux_inventory()
    if tmux is not None:
        payload = tmux.agents()
        if payload is None:
            return None, "tmux-agent-list-unavailable"
        return payload["result"]["agents"], None
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
                              target_observation=None, pane_info=None, native_address=None):
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
    if info is None and native_address != (harness, expected_sid, pane):
        return None, "target-observation-unavailable"
    process_sid, process_error = (_prompt_process_session(pane, harness, info)
                                  if info is not None else (None, None))
    if process_error:
        return None, process_error
    expected_sid = peer_message.usable_session_id(expected_sid)
    if (expected_sid and process_sid and expected_sid != process_sid
            or not expected_sid and herdr_sid and process_sid and herdr_sid != process_sid):
        return None, "target-identity-changed"
    verified_address = (native_address == (harness, expected_sid, pane) and expected_sid is not None)
    if (expected_sid or herdr_sid or require_process_session) and not process_sid and not verified_address:
        return None, "target-identity-unverified"
    session_id = process_sid or (expected_sid if verified_address else None)
    if require_process_session and not session_id:
        return None, "target-identity-unverified"
    return {"state": state, "harness": harness, "session_id": session_id,
            "name": name if isinstance(name, str) and name and name != "-" else None,
            "pane": pane, "_identity": dict(ident, session_id=session_id or "-"), "_pane_info": info,
            "_native_session_id": process_sid}, None


def _prompt_candidate_snapshot(target, *, wanted_tag=None):
    """Discover address candidates before proving their exact native identities."""
    agents, inventory_error = _prompt_agent_inventory()
    endpoint = herdr_observation.endpoint_name(_HERDR_SESSION)
    census = (_tmux_census(agents, endpoint) if _tmux_inventory() is not None
              else herdr_observation.pane_census(session=_HERDR_SESSION))
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
        census_sid, census_error = None, None
        if census_session is not None:
            if (not isinstance(census_session, dict)
                    or census_session.get("kind") not in (None, "id")):
                census_error = "pane-census-malformed"
            else:
                census_sid = peer_message.usable_session_id(census_session.get("value"))
                if census_session.get("value") not in (None, "", "-") and not census_sid:
                    census_error = "pane-census-malformed"

        # Names, selected-pane metadata, and shared tags locate candidates. They
        # do not establish identity; only matching panes reach native proof.
        observed = _retire_target(pane)
        observed_ident = (observed[1] if isinstance(observed, tuple)
                          and len(observed) > 1 and isinstance(observed[1], dict) else {})
        observed_sid = peer_message.usable_session_id(observed_ident.get("session_id"))
        sid_hints = list(dict.fromkeys(
            sid for sid in (census_sid, row.get("session_id"), observed_sid) if sid))
        name_values = [row.get("name"), observed_ident.get("name"),
                       item.get("display_agent"), item.get("title"),
                       item.get("terminal_title_stripped")]
        names = {value for value in name_values
                 if isinstance(value, str) and value and value != "-"}
        for sid_hint in sid_hints:
            try:
                registry_name = _from_name(harness, sid_hint)
            except Exception:
                registry_name = None
            if isinstance(registry_name, str) and registry_name and registry_name != "-":
                names.add(registry_name)

        if target == pane:
            names.add(pane)
        name_match = target in names
        name_unresolved = not names and not sid_hints
        tag_match, tag_unresolved = False, False
        if wanted_tag is not None:
            if not sid_hints:
                tag_unresolved = True
            for sid_hint in sid_hints:
                current_tag, tag_error = _prompt_fleet_tag(
                    harness, sid_hint, pane=pane,
                    cwd=item.get("cwd") or item.get("foreground_cwd"))
                if tag_error:
                    tag_unresolved = True
                elif current_tag == wanted_tag:
                    tag_match = True
        if (not name_match and not name_unresolved
                and not tag_match and not tag_unresolved):
            continue

        if census_error:
            unknown_panes.add(pane)
            unknown_reason = unknown_reason or census_error
            continue
        if row["harness"] != harness:
            unknown_panes.add(pane)
            unknown_reason = unknown_reason or "target-identity-changed"
            continue
        expected_sid = census_sid or row.get("session_id") or observed_sid
        if (observed_ident.get("harness") not in (None, harness)
                or observed_ident.get("pane") not in (None, pane)):
            unknown_panes.add(pane)
            unknown_reason = unknown_reason or "target-identity-changed"
            continue
        candidate, reason = _prompt_current_candidate(
            pane, expected_harness=harness, expected_sid=expected_sid,
            require_process_session=True, target_observation=observed,
            native_address=(harness, expected_sid, pane)
            if census_sid and census_sid == row.get("session_id") else None)
        if candidate is None:
            unknown_panes.add(pane)
            unknown_reason = unknown_reason or reason or "target-identity-unverified"
            continue
        if isinstance(candidate.get("name"), str) and candidate["name"] != "-":
            names.add(candidate["name"])
        try:
            registry_name = _from_name(harness, candidate["session_id"])
        except Exception:
            registry_name = None
        if isinstance(registry_name, str) and registry_name and registry_name != "-":
            names.add(registry_name)
        name_match = name_match or target in names
        if not names:
            unknown_panes.add(pane)
            unknown_reason = unknown_reason or "target-name-observation-unavailable"
            continue
        if wanted_tag is not None and (tag_match or tag_unresolved):
            current_tag, tag_error = _prompt_fleet_tag(
                candidate["harness"], candidate["session_id"], pane=candidate["pane"],
                cwd=item.get("cwd") or item.get("foreground_cwd"))
            if tag_error or current_tag != wanted_tag:
                unknown_panes.add(pane)
                unknown_reason = unknown_reason or tag_error or "target-identity-changed"
                continue
        elif not name_match:
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
    A bare two-hex Fleet number (``e8``) is a name first; only when the name stage
    settles on no match does it resolve as the bracketed tag ``[e8]``.
    """
    result = _resolve_prompt_address(target)
    if (result[1] == "target-name-not-found" and isinstance(target, str)
            and _PROMPT_BARE_TAG_ADDRESS.fullmatch(target)):
        return _resolve_prompt_address("[" + target + "]")
    return result


def _resolve_prompt_address(target):
    if not isinstance(target, str) or not target:
        return None, "target-not-found", []
    tag_match = _PROMPT_TAG_ADDRESS.fullmatch(target)
    is_tag = tag_match is not None
    is_pane = ((not is_tag and ":" in target and not any(char.isspace() for char in target))
               or terminal_surface.is_tmux_pane(target))

    if is_pane:
        candidate, reason = _prompt_current_candidate(target)
        if candidate:
            return candidate, None, [target]
        if reason != "target-identity-unverified":
            return None, reason, [target]

    wanted_tag = tag_match.group(1).lower() if is_tag else None
    rows, snapshot_error, unknown_panes = _prompt_candidate_snapshot(
        target, wanted_tag=wanted_tag)
    if rows is None:
        return None, snapshot_error or "target-observation-unavailable", unknown_panes
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
        require_process_session=True,
        native_address=(row["harness"], row["session_id"], row["pane"])
        if not row.get("_native_session_id") else None)
    if not candidate:
        return None, reason, candidate_panes
    if wanted_tag is not None:
        current_tag, tag_error = _prompt_fleet_tag(
            candidate["harness"], candidate["session_id"], pane=candidate["pane"], cwd=row.get("cwd"))
        if tag_error or current_tag != wanted_tag:
            return None, tag_error or "target-identity-changed", candidate_panes
    return candidate, None, candidate_panes


def _prompt_exact_candidate(pane, *, expected_harness, expected_sid, target_observation=None):
    """Revalidate a pinned address for prompt and its existing continuation."""
    current, error = _prompt_current_candidate(
        pane, expected_harness=expected_harness, expected_sid=expected_sid,
        require_process_session=True, target_observation=target_observation)
    if current is None and error == "target-identity-unverified" and expected_harness == "codex":
        current, error, _panes = _resolve_prompt_target(pane)
        if current is not None and (current["harness"], current["session_id"], current["pane"]) != (
                expected_harness, expected_sid, pane):
            return None, "target-identity-changed"
    return current, error


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


def _run_herdr_wait(target, until, timeout_ms):
    global _LAST_HERDR_EXIT
    if terminal_surface.is_tmux_pane(target):
        _LAST_HERDR_EXIT = None
        tmux = _tmux_surface(target)
        payload = tmux.wait_agent(target, tuple(until or ()), timeout_ms) if tmux else None
        if isinstance(payload, dict):
            _LAST_HERDR_EXIT = 1 if payload.get("error") else 0
        return payload
    cmd = _herdr_argv("agent", "wait", target)
    for state in until or []:
        cmd += ["--until", state]
    if timeout_ms is not None:
        cmd += ["--timeout", str(timeout_ms)]
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
_LAST_HERDR_EXIT = None          # real `herdr agent wait` return code of the last call (m3)


def _mark_observed(to_harness, to_session_id, to_name, from_identity=None, *, pane=None,
                   target_cwd=None):
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
    result = peer_message.grant_steward(from_harness, from_sid, to, "watch", _utc_now(),
                                        source="watch", target_cwd=target_cwd)
    _print_steward_limited(result)
    return result


def cmd_wait(args):
    target = args.target = _tmux_target_address(args.target)
    # S6: one record at wait start, append-only, never updated on return. F-100c: the
    # target is resolved through herdr first so the record carries an exact session id
    # (Fleet joins sent/recv and the `←` subtitle on it), the name being only a label.
    t_harness, t_sid, _t_name = _resolve_target(target)
    _record(to_harness=t_harness or "unknown", to_name=target, kind="watch", ref=args.ref,
            to_session_id=t_sid)

    if _surface_missing(target):
        return _unavailable("tmux-not-found" if terminal_surface.is_tmux_pane(target)
                            else "herdr-not-found")

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
                       resolved_name, pane=agent["pane"], target_cwd=_payload_cwd(payload))
    print(_typed_line(state, agent["harness"], agent["session_id"], agent["name"], agent["pane"]))
    return code


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
        if terminal_surface.is_tmux_pane(pane):
            processes = (_retire_pane_info(pane) or {}).get("foreground_processes") or []
        else:
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


def _pane_send(pane, operation, value):
    """One retire keystroke (`send-text` text or `send-keys` key) to ``pane``; True when accepted.

    A tmux pane takes it through its surface, which refuses a pane in a tmux mode,
    with input off or with synchronized neighbours; Herdr keeps its own call.
    """
    if terminal_surface.is_tmux_pane(pane):
        tmux = _tmux_surface(pane)
        if tmux is None:
            return False
        if operation == "send-text":
            return tmux.send_text(pane, value)
        key = {"enter": "Enter", "ctrl+d": "C-d"}.get(str(value).lower())
        return bool(key) and tmux.send_keys(pane, key)
    sent = subprocess.run(_herdr_argv("pane", operation, pane, value),
                          capture_output=True, text=True, timeout=5)
    if (sent.stdout or sent.stderr).strip():
        try:
            payload = json.loads(sent.stdout or sent.stderr)
            if not isinstance(payload, dict) or payload.get("error"):
                return False
        except ValueError:
            return False
    return sent.returncode == 0


def _dialog_send(pane, operation, value):
    """One answer to Claude's exit-time dialog. Herdr's call keeps its old shape (its
    answer was never read); a refused tmux keystroke stops the dialog as a send error."""
    if terminal_surface.is_tmux_pane(pane):
        if not _pane_send(pane, operation, value):
            raise OSError("tmux input refused")
        return
    subprocess.run(_herdr_argv("pane", operation, pane, value),
                   capture_output=True, text=True, timeout=5)


def _close_pane(pane):
    if terminal_surface.is_tmux_pane(pane):
        tmux = _tmux_surface(pane)
        return bool(tmux is not None and tmux.close(pane))
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


def _seat_handover(ident, own_sid, own_harness, *, requester=None, accepted_at=None, recheck=None, wait=None,
                   retain_mark=False):
    """Transfer to the exact successor; physical retirement settles separately.
    The pane started beside the predecessor inherits its routes across harnesses
    (`dispatch_seat_handover.record_retire_handover`). Returns the receipt value, or None when
    this is not a seat change (the receipt then stays as it was); it never fails the retire."""
    pane = requester.get("pane") if requester is not None else _caller_pane()
    if not pane:
        return None
    try:
        with _seat_successor_lock(pane, wait=wait):
            return _seat_handover_locked(ident, own_sid, own_harness, pane,
                                         requester=requester, accepted_at=accepted_at, recheck=recheck,
                                         retain_mark=retain_mark)
    except OSError:
        return "error"


def _legacy_requester_projection(duty, *, expected_pane=None):
    """Resolve an old empty requester pane from one exact pre-acceptance mark."""
    global _HERDR_SESSION
    import opencode_session_runtime as native
    intent = duty.get("intent") or {}
    requester = intent.get("requester")
    identity = intent.get("identity") or {}
    if not isinstance(requester, dict) or not requester:
        return None
    pane = requester.get("pane")
    if isinstance(pane, str) and pane not in {"", "-"}:
        if expected_pane is None or pane == expected_pane:
            return requester
        return None
    sid, harness = requester.get("session_id"), requester.get("harness")
    server = requester.get("server")
    accepted_at = duty.get("accepted_at")
    predecessor_pane = identity.get("pane")
    if (not isinstance(sid, str) or not sid or sid == "-"
            or harness not in _RETIRE_ACTIONS
            or not isinstance(server, str) or not server
            or not isinstance(predecessor_pane, str) or not predecessor_pane
            or not isinstance(accepted_at, (int, float)) or isinstance(accepted_at, bool)):
        return None
    root = peer_message.peer_state_root() / "seat-successors"
    candidates = []
    try:
        paths = sorted(root.glob("*.json"))
    except OSError:
        return None
    for path in paths:
        try:
            record = native._read_native_record(path)
            if record is None:
                continue
            mark = json.loads(record["raw"])
        except (OSError, ValueError, TypeError, native.OpenCodeTransportError):
            continue
        candidate_pane = mark.get("pane") if isinstance(mark, dict) else None
        successor = mark.get("successor") if isinstance(mark, dict) else None
        if (not isinstance(candidate_pane, str) or not candidate_pane
                or _seat_successor_path(candidate_pane) != path
                or type(mark.get("schema")) is not int or mark.get("schema") != 1
                or mark.get("beside") != predecessor_pane
                or mark.get("server", "default") != server
                or not isinstance(successor, dict)
                or successor.get("harness") != harness
                or successor.get("session_id") not in (None, "", sid)):
            continue
        candidates.append((candidate_pane, record, mark))
    if len(candidates) != 1:
        return None
    candidate_pane, record, mark = candidates[0]
    if (expected_pane is not None and candidate_pane != expected_pane
            or not _source_before_acceptance(record["source"], accepted_at, mark.get("at"))):
        return None

    old_server = _HERDR_SESSION
    _HERDR_SESSION = None if server == "default" else server
    try:
        observed = _retire_target(candidate_pane, server=server)
        pane_info = _retire_pane_info(candidate_pane, server=server)
        current, reason = _prompt_current_candidate(
            candidate_pane, expected_harness=harness, expected_sid=sid,
            require_process_session=True, target_observation=observed,
            pane_info=pane_info)
        if (current is None or reason
                or current.get("pane") != candidate_pane
                or current.get("harness") != harness
                or current.get("session_id") != sid
                or (current.get("_identity") or {}).get("server", server) != server):
            return None
    finally:
        _HERDR_SESSION = old_server
    projected = dict(requester)
    projected.update(pane=candidate_pane, server=server)
    return projected


def _seat_handover_locked(ident, own_sid, own_harness, pane, *, requester=None, accepted_at=None,
                          recheck=None, retain_mark=False):
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
    if retain_mark:
        current, reason = _prompt_current_candidate(
            pane, expected_harness=own_harness, expected_sid=own_sid,
            require_process_session=True)
        if (reason or current is None or current.get("pane") != pane
                or current.get("harness") != own_harness
                or current.get("session_id") != own_sid):
            return "skipped:successor-unverified"
    try:
        import dispatch_seat_handover as handover
        booked = {}
        if requester is not None:
            import session_tidy
            seat_env = _herdr_env_for_server(server)
            booked["successor_seat"] = session_tidy._pane_seat_of(
                pane, endpoint=session_tidy._endpoint_scope(seat_env),
                socket_path=seat_env.get("HERDR_SOCKET_PATH", ""))
        if _read_native_record(path) != record or (recheck is not None and not recheck()):
            return "skipped:predecessor-unverified"
        row = handover.record_retire_handover(
            ident["session_id"], ident["harness"], own_sid, own_harness, env=os.environ, **booked)
    except Exception as exc:  # noqa: BLE001 - the retire itself already succeeded
        # Nothing was recorded because the registry could not be read, so this
        # is not the verified-empty "none": keep the receipt unresolved.
        if getattr(exc, "reason", "") == "registry-unreadable":
            return "skipped:registry-unreadable"
        return "error"
    try:
        if row is not None and not retain_mark and _read_native_record(path) == record:
            path.unlink()
    except (OSError, OpenCodeTransportError):
        pass
    return str(len(row["bindings"])) if row else "none"


def _retire_handover(duty, *, wait=None):
    """Consume only the accepted retirement's original succession, on any observer."""
    intent = duty.get("intent") or {}
    requester = intent.get("requester")
    projected_legacy = False
    legacy_blank_pane = (isinstance(requester, dict) and bool(requester)
                         and requester.get("pane") in (None, "", "-"))
    try:
        if legacy_blank_pane:
            own_sid, own_harness = requester.get("session_id"), requester.get("harness")
        elif not isinstance(requester, dict) or not requester:
            requester = None
            own_sid, own_harness = _current_session_identity()
        else:
            own_sid, own_harness = requester.get("session_id"), requester.get("harness")
        resolved = _retire_predecessor(duty)
    except Exception:  # noqa: BLE001 - the retire itself already succeeded
        return "error"
    if resolved is None:
        return None
    identity, evidence = resolved
    if legacy_blank_pane:
        projected = _legacy_requester_projection(duty)
        if projected is None:
            # No successor mark is a normal no-handover case when the
            # predecessor currently owns no registered route. Keep the
            # retained duty unresolved only when that same shared authority
            # reader still finds work it cannot safely bind to a pane.
            try:
                import dispatch_seat_handover as handover
                jobs = handover._default_jobs()
                if not jobs or not Path(jobs).is_file():
                    return "skipped:successor-unverified"
                from dispatch_registry_cache import registry_view
                registry_view(Path(jobs))
                if handover._handover_candidates(
                        identity.get("harness", ""), identity.get("session_id", ""), jobs):
                    return "skipped:successor-unverified"
                return None
            except (OSError, ValueError, TypeError, RuntimeError):
                return "skipped:successor-unverified"
        requester = projected
        own_sid, own_harness = requester.get("session_id"), requester.get("harness")
        projected_legacy = True
    def recheck():
        if evidence and _retire_predecessor(duty) != resolved:
            return False
        if projected_legacy:
            return _legacy_requester_projection(
                duty, expected_pane=requester.get("pane")) == requester
        return True
    return _seat_handover(identity, own_sid, own_harness, requester=requester,
                          accepted_at=duty.get("accepted_at"),
                          recheck=recheck if evidence or projected_legacy else None,
                          wait=wait, retain_mark=(duty.get("state") != "complete"
                              and (duty.get("observation") or {}).get("phase", "waiting") == "waiting"
                              and _retire_subject_state(duty) != "gone"))


def _retire_cutover(duty):
    """Move a proven successor's parent duties before deciding whether to exit.

    A valid mark with no eligible route is still a confirmed succession. Its
    past notification uncertainty is not an executor in the retiring pane.
    The normal input projection continues to protect native turns and actual
    registered executors; delivery records and their ACKs remain untouched.
    """
    try:
        result = _retire_handover(duty, wait=0)
    except Exception:
        return False
    return isinstance(result, str) and (result == "none" or result.isdecimal())


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
    result = peer_message.grant_steward(
        from_harness, from_sid,
        {"harness": args.kind, "session_id": sid, "name": args.name, "pane": args.pane},
        "start", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), source="start",
        target_cwd=getattr(args, "cwd", None))
    _print_steward_limited(result)


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


def _retain_start_failure(requester, requested, actual, result):
    """One message to the original native controller; the normal start remains the retry."""
    sid, harness = requester
    if not peer_message.usable_session_id(sid) or harness not in _PROMPT_HARNESSES:
        return
    sender = {"harness": harness, "session_id": sid}
    request_key = _start_request_key(sender, requested)
    ref_key = "peer-start:" + request_key
    # Include received rows: this failed request never wakes its controller twice.
    for path in peer_message._pending_dir().glob("*.json"):
        try:
            row = peer_message._read_pending(path.stem)
        except (OSError, ValueError):
            continue
        if (row and (row.get("refs") == [ref_key] or row.get("start_retry_key") == request_key)
                and row.get("from") == sender and row.get("to") == sender):
            _schedule_message_obligation(row, "")
            return
    command = ["hearting", "run", "peer-steward", "--herdr-session", requested["server"],
               "start", requested["name"], "--kind", requested["kind"]]
    # A split that cleanup closed is no longer a retry destination. A retained
    # pane still enters the normal observe/adopt and occupied-pane checks.
    pane = requested.get("pane")
    if actual.get("pane_cleanup") in {"retained", "close-failed"}:
        pane = actual.get("pane") or pane
    if pane:
        command += ["--pane", pane]
    elif requested.get("beside"):
        command += ["--beside", requested["beside"]]
    if requested.get("cwd"):
        command += ["--cwd", requested["cwd"]]
    if requested.get("permission_mode"):
        command += ["--permission-mode", requested["permission_mode"]]
    if requested.get("agent_args"):
        command += ["--", *requested["agent_args"]]
    if requested.get("project_workspace") and not pane:
        # Recreate the original project lookup using its existing cwd/pane hint,
        # without turning that hint into physical caller ownership.
        command = ["env", "-C", requested["launch_cwd"],
                   "HERDR_PANE_ID=" + requested["project_hint"], *command]
    body = ("[notice] Your requested peer start did not complete. "
        "Reobserve the same endpoint and original request on this native turn. "
        "If the requested agent already started, adopt that exact observation; otherwise continue the same start. "
        "Keep the requested workspace/tab/pane and do not select another window.\n"
        + "Request: " + json.dumps(requested, sort_keys=True, ensure_ascii=False)
        + "\nObserved result: " + result.strip() + "\nSame start: " + shlex.join(command))
    _text, ref = peer_message.prepare_peer_message(body, sender, sender, defer=True,
                                                  refs=[ref_key], receipt="peer-start-failed")
    retry = dict(requested)
    if pane:
        retry.update(pane=pane, beside=None, project_workspace=None)
    with peer_message.pending_lock(ref):
        row = peer_message._read_pending(ref)
        if row is None:
            return
        row.update(start_request=requested, start_retry_key=_start_request_key(sender, retry))
        peer_message._save_pending(row)
    _schedule_message_obligation(row, "")


def cmd_start(args, *, retain_failure=True):
    import io
    requester = _current_session_identity()
    requested = {key: getattr(args, key, None) for key in
                 ("name", "kind", "pane", "beside", "cwd", "permission_mode", "agent_args")}
    requested.update(server=_HERDR_SESSION or os.environ.get("HERDR_SESSION") or "default",
                     launch_cwd=os.getcwd())
    if not requested["cwd"]:
        requested["cwd"] = os.getcwd()
    requested["cwd"] = os.path.realpath(os.path.expanduser(str(requested["cwd"])))
    args.start_request = requested
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = _start_request(args)
    result = output.getvalue()
    if result:
        print(result, end="")
    # No herdr is not a failed attempt: a retry cannot succeed either, so no retained
    # notice asks this session to repeat it. The one printed line is the whole answer.
    if (code != 0 and retain_failure and "reason=herdr-not-found" not in result
            and "reason=tmux-not-found" not in result):
        try:
            _retain_start_failure(requester, requested, vars(args), result)
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Preserve the actual start failure when no native carrier is available.
    return code


def _start_request(args):
    if len(args.name) > 32:
        print(f"started=false reason=agent-name-too-long agent={args.kind} name={args.name} "
              "max_length=32 detail=이름은 최대 32자까지 사용할 수 있습니다")
        return 1
    if not args.pane and not args.beside:
        args.beside = _caller_pane() or None
        if not args.beside:
            args.project_workspace = _project_start_workspace()
    args.start_request.update(beside=args.beside,
                              project_workspace=getattr(args, "project_workspace", None),
                              project_hint=os.environ.get("HERDR_PANE_ID", ""))
    if terminal_surface.is_tmux_pane(args.beside or args.pane or ""):
        return _start_tmux_request(args)
    workspace = (args.beside.split(":")[0] if args.beside else
                 getattr(args, "project_workspace", None))
    if workspace and not args.cwd:
        try:
            args.cwd = str(INSTALL_PATHS.primary_checkout(os.getcwd()))
        except OSError:
            args.cwd = None
    if args.cwd:
        args.start_request["cwd"] = os.path.realpath(os.path.expanduser(str(args.cwd)))
    if not workspace:
        return _start_in_pane(args)
    # Hold the workspace claim until native start settles, including shell reuse.
    # A second start then reads the new layout instead of splitting the old cell.
    with terminal_surface.workspace_lock(_herdr_surface(), workspace):
        return _start_in_pane(args)


def _start_tmux_request(args):
    """`start` beside a tmux pane, or in a given empty tmux shell, under the session claim."""
    surface = _tmux_surface(args.beside or args.pane)
    if surface is None or not surface.answers():
        print(f"started=false reason=tmux-not-found agent={args.kind} name={args.name} pane=- "
              "detail=tmux 서버에 닿지 못했습니다. 이 일은 지금 세션에서 이어서 하세요")
        return 4
    if args.beside and not args.cwd:
        try:
            args.cwd = str(INSTALL_PATHS.primary_checkout(os.getcwd()))
        except OSError:
            args.cwd = None
    if args.cwd:
        args.start_request["cwd"] = os.path.realpath(os.path.expanduser(str(args.cwd)))
    args.start_request["server"] = "tmux:" + (surface.socket or "default")
    workspace = surface.workspace_of(args.beside or args.pane)
    if not workspace:
        print(f"started=false reason=pane-layout-unavailable agent={args.kind} name={args.name} "
              f"pane={args.pane or '-'}")
        return 1
    with terminal_surface.workspace_lock(surface, workspace):
        return _start_in_tmux(args, surface)


def _start_in_tmux(args, surface):
    """Open the pane, type the launch line once into its proven shell, observe the runtime.

    Placement is the shared `open_pane` (inside the reference pane's window). The one
    typed line goes only into a shell this start opened or the caller named, after
    the same shell shows the same empty prompt twice and the pane takes keys alone
    (no copy mode, no synchronize-panes). A pane this start opened is closed only
    while that shell still shows that screen; after the line was typed it is kept.
    """
    pane_cwd = (os.path.realpath(os.path.expanduser(str(args.cwd))) if args.cwd else None)
    cwd_note = f" cwd={pane_cwd}" if pane_cwd else ""
    if pane_cwd and not os.path.isdir(pane_cwd):
        print(f"started=false reason=cwd-not-a-directory agent={args.kind} name={args.name} "
              f"pane={args.pane or '-'}{cwd_note}")
        return 1
    created = False
    shell = screen = None

    def cleanup():
        if not created:
            return ""
        result = "retained"
        if (shell is not None and screen is not None and _tmux_agent(surface, args.pane) is None
                and _tmux_shell_identity(surface, args.pane) == shell
                and surface.read_visible(args.pane) == screen):
            result = "closed" if surface.close(args.pane) else "close-failed"
        args.pane_cleanup = result
        return f" pane_cleanup={result}"

    def refuse(reason, extra=""):
        print(f"started=false reason={reason} agent={args.kind} name={args.name} "
              f"pane={args.pane or '-'}{cwd_note}{extra}")
        return 1

    if args.pane:
        args.pane = surface.address(args.pane) or args.pane
        existing = _tmux_agent(surface, args.pane)
        if existing is not None:
            if (existing.get("agent") != args.kind or existing.get("name") not in (None, args.name)
                    or (pane_cwd and os.path.realpath(str(existing.get("cwd") or "")) != pane_cwd)):
                return refuse("pane-occupied")
            # The retained-pane reuse command after a late start: adopt, never type.
            return _tmux_started(args, surface, existing, "observed-pane", cwd_note)
    else:
        opened = terminal_surface.open_pane(
            near=terminal_surface.Near(surface, pane=args.beside, same_tab=True), cwd=pane_cwd)
        if opened.pane is None:
            return refuse(opened.notes[0] if opened.notes else "pane-layout-unavailable",
                          f" beside={args.beside}")
        args.pane, created = surface.address(opened.pane) or opened.pane, opened.placement != "reuse"
    shell, screen, reason = _tmux_ready_shell(
        surface, args.pane, pane_cwd if created else None,
        time.monotonic() + _BESIDE_READY_SECONDS)
    if reason:
        return refuse(reason, cleanup())
    line, _mode = _tmux_launch_line(args, None if created else pane_cwd)
    if (_tmux_shell_identity(surface, args.pane) != shell
            or surface.read_visible(args.pane) != screen):
        return refuse("beside-shell-changed", cleanup())
    # The surface's one typed input: refused in a tmux mode, with input off or with
    # synchronized panes (it would reach the neighbours), rechecked before Enter.
    rc, payload = surface.prompt(args.pane, line)
    if rc is None:
        reason = ((payload or {}).get("error") or {}).get("reason") or "tmux-launch-not-started"
        return refuse(reason, cleanup())
    # The runtime process proves the start; its own session record usually follows
    # within seconds (an OpenCode or a not-yet-prompted Codex may have none to show).
    deadline = time.monotonic() + _TMUX_START_SECONDS
    agent, seen = None, None
    while time.monotonic() < deadline:
        agent = _tmux_agent(surface, args.pane)
        if agent is not None and agent.get("agent") == args.kind and seen is None:
            seen = time.monotonic()
        if agent is not None and (agent.get("agent") != args.kind or agent.get("agent_session")
                                  or (seen is not None and time.monotonic() - seen >= 5)):
            break
        time.sleep(.2)
    if agent is None or agent.get("agent") != args.kind:
        retry = ["hearting", "run", "peer-steward", "start", args.name, "--kind", args.kind,
                 "--pane", args.pane]
        if pane_cwd:
            retry += ["--cwd", pane_cwd]
        if args.permission_mode:
            retry += ["--permission-mode", args.permission_mode]
        if getattr(args, "agent_args", None):
            retry += ["--"] + list(args.agent_args)
        reason = "tmux-start-failed" if rc else "tmux-start-unobserved"
        return refuse(reason, " pane_cleanup=retained reuse_command=" + shlex.quote(shlex.join(retry)))
    return _tmux_started(args, surface, agent, None, cwd_note)


def _tmux_started(args, surface, agent, start_verify, cwd_note):
    sid = peer_message.usable_session_id((agent.get("agent_session") or {}).get("value"))
    mode = args.permission_mode or _default_permission_mode()
    if type(agent.get("pid")) is int:
        surface.name_started(args.pane, agent["pid"], args.name)
    if sid:
        try:
            peer_message._session_handle()  # Make the shared tools package importable.
            from fleet.session_tags import assign_observed
            assign_observed(args.kind, sid, pane_id=args.pane)
        except Exception:
            pass
    _mark_started(args, sid, mode)
    if getattr(args, "beside", None) and args.pane != args.beside:
        _mark_seat_successor(args.pane, args.beside, args.kind, sid)
    trust_wait = _native_trust_reason(args.kind, _read_screen(args.pane))
    managed = "-"
    if _MANAGED_INGRESS.get(args.kind):
        verdict = _pane_is_managed(args.pane)
        managed = "unknown" if verdict is None else str(verdict).lower()
    print(f"started=true agent={args.kind} name={args.name} pane={args.pane} "
          f"permission_mode={mode} session_id={sid or '-'} managed={managed}"
          + (f" ready=false reason={trust_wait}" if trust_wait else "")
          + cwd_note + " surface=tmux"
          + (f" start_verify={start_verify}" if start_verify else ""))
    return 0


def _start_in_pane(args):
    if _herdr_missing():
        print(f"started=false reason=herdr-not-found agent={args.kind} name={args.name} pane=- "
              "detail=새 세션 창은 herdr가 있어야 열립니다. 이 일은 지금 세션에서 이어서 하세요")
        return 4
    workspace = getattr(args, "project_workspace", None)
    if not args.pane and not args.beside and not workspace:
        print(f"started=false reason=pane-unknown agent={args.kind} name={args.name} "
              "pane=- detail=호출자의 창 또는 같은 프로젝트 작업공간을 확인하지 못했습니다")
        return 1
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
        args.pane_cleanup = result
        if result in {"retained", "close-failed"}:
            retry = ["hearting", "run", "peer-steward", "--herdr-session",
                     args.start_request["server"], "start", args.name,
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
                                       workspace=workspace, same_tab=not workspace),
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
    if started and started_sid:
        try:
            peer_message._session_handle()  # Make the shared tools package importable.
            from fleet.session_tags import assign_observed
            assign_observed(args.kind, started_sid, pane_id=args.pane)
        except Exception:
            pass  # Numberless pending does not change a successful start.
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
    return 0 if started else 1


def _retire_pane_info(pane, timeout=5, *, server=None):
    if terminal_surface.is_tmux_pane(pane):
        tmux = _tmux_surface(pane)
        info = tmux.process_info(pane) if tmux else None
        return info if (isinstance(info, dict) and info.get("pane_id") == pane
                        and info.get("foreground_processes")) else None
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


def _retire_followup_successor_pane(duty):
    intent = duty.get("intent") or {}
    requester = intent.get("requester")
    if isinstance(requester, dict) and requester:
        return requester.get("pane")
    return _caller_pane()


def _release_retired_session(identity, summary=None):
    """Let go of the roles and supervision watches a retired session still held.

    Returns False only when a transient failure should be retried by the existing
    follow-up; any other outcome is recorded in ``summary`` and never blocks it."""
    harness, sid = identity.get("harness"), identity.get("session_id")
    if not all(isinstance(value, str) and value and value != "-" for value in (harness, sid)):
        return True
    try:
        result = peer_message.release_closed_session(
            harness, sid, reason="retired", settle_watch=settle_steward_exited_watch)
    except OSError:
        return False
    except Exception as exc:  # noqa: BLE001 - best effort; the retire itself already ended
        result = {"error": type(exc).__name__}
    if summary is not None:
        summary["steward_release"] = json.dumps(result, sort_keys=True, separators=(",", ":"))
    return True


def _retire_followup_ready(duty, identity, reason, handover, *,
                           defer_if_missing=False, summary=None):
    unresolved = (handover == "error" or (isinstance(handover, str)
                  and handover.startswith("skipped:")))
    if handover is None and defer_if_missing:
        unresolved = True
    pane = _retire_followup_successor_pane(duty)
    if pane:
        try:
            _seat_successor_path(pane).lstat()
        except FileNotFoundError:
            pass
        except OSError:
            unresolved = True
        else:
            unresolved = True
    try:
        if _request_watch_terminal(identity, reason) is None:
            unresolved = True
    except Exception:
        unresolved = True
    if not _release_retired_session(identity, summary):
        unresolved = True
    return not unresolved


def _mark_completed_retire_followup(store, duty, identity, reason, handover, *,
                                    defer_if_missing=False):
    release = {}
    complete = _retire_followup_ready(
        duty, identity, reason, handover, defer_if_missing=defer_if_missing,
        summary=release)
    observation = dict(duty.get("observation") or {})
    observation.update(terminal_followup="complete" if complete else "pending",
                       terminal_followup_result=str(handover or "none"), **release)
    try:
        return store.update(duty["id"], observation=observation)
    except (OSError, ValueError, RuntimeError):
        return duty


def _retire_requester_matches(duty, requester):
    """Match only the same requester, projecting an old blank pane from its mark."""
    saved = (duty.get("intent") or {}).get("requester")
    if not isinstance(saved, dict) or not isinstance(requester, dict):
        return False
    if saved == requester:
        return True
    if ((saved.get("session_id"), saved.get("harness"), saved.get("server"))
            != (requester.get("session_id"), requester.get("harness"), requester.get("server"))
            or saved.get("pane") not in (None, "", "-")):
        return False
    projected = _legacy_requester_projection(duty, expected_pane=requester.get("pane"))
    return projected == requester


def reserve_handoff_retire(target, exact, requester):
    """Reserve in the existing store; never execute exit from acceptance."""
    store = peer_obligations.ObligationStore()
    matches = [d for d in store.list(states=peer_obligations._PENDING_STATES | {"complete"},
                                    kinds={"retire"})
        if all((d["intent"].get("identity") or {}).get(k) == exact.get(k)
                for k in ("server", "pane", "harness", "session_id"))]
    if matches:
        if len(matches) != 1 or not _retire_requester_matches(matches[0], requester):
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
                              persist_followup=False, runner_lock_held=False):
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
            # Older versions could finish a blank-pane requester's terminal
            # follow-up without reaching its successor mark. Re-read that same
            # retained duty so an installed runner can repair the missed ledger
            # row; an absent, changed, or ambiguous mark still grants nothing.
            try:
                handover = _retire_handover(duty, wait=0)
            except Exception:
                return
            if handover in (None, "", "none", "error") or (
                    isinstance(handover, str) and handover.startswith("skipped:")):
                _RETIRE_FOLLOWUP_COMPLETE.add(followup_key)
                return
            if not _retire_followup_ready(duty, ident, "completed-retire", handover):
                return
            latest = store.get(duty["id"]) or duty
            latest_observation = dict(latest.get("observation") or {})
            if latest_observation.get("terminal_followup_result") != str(handover):
                latest_observation.update(terminal_followup="complete",
                                          terminal_followup_result=str(handover))
                try:
                    store.update(duty["id"], observation=latest_observation)
                except (OSError, ValueError, RuntimeError):
                    return
            _RETIRE_FOLLOWUP_COMPLETE.add(followup_key)
            return
        try:
            handover = _retire_handover(duty, wait=0)
            if not _retire_followup_ready(
                    duty, ident, "completed-retire", handover):
                return
            if persist_followup:
                current = store.get(duty["id"]) or duty
                current_observation = dict(current.get("observation") or {})
                if current_observation.get("terminal_followup") != "complete":
                    current_observation.update(
                        terminal_followup="complete",
                        terminal_followup_result=str(handover or "none"))
                    store.update(duty["id"], observation=current_observation)
            _RETIRE_FOLLOWUP_COMPLETE.add(followup_key)
            if len(_RETIRE_FOLLOWUP_COMPLETE) > 4096:
                _RETIRE_FOLLOWUP_COMPLETE.clear()
        except Exception:
            # The active duty is revisited on the next runner pass. Keeping
            # that lineage is the retry; a fingerprint scan of other inputs
            # would add a second owner for the same decision.
            return
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
            elif (ident.get("harness") == "claude" and foreground
                    and _retire_foreground(ident["pane"], "claude") == foreground
                    and _retire_background_dialog_lines(_read_screen(ident["pane"])) is not None):
                requester = intent.get("requester") or {}

                def finish(reason, retired=False, **kwargs):
                    return _finish_retire(store, duty, target, ident, reason,
                                          retired=retired, **kwargs)

                # Finish the exit already sent, on its booked pane/lifetime.
                # A late dialog needs its answer, never another /exit.
                _retire_claude_background_confirm(
                    ident["pane"], ident["pane"], "claude", ident, foreground, finish,
                    requester.get("session_id"), requester.get("harness"), duty=duty)
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
        cutover = _retire_cutover(duty)
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
                                    purpose="input" if cutover else "completion",
                                    read_context=read_context)
        if readiness.state != "ready":
            store.update(duty["id"], state="unknown" if readiness.state == "unknown" else "pending",
                         expected_phases={"waiting"},
                         observation={"phase": "waiting", "reason": readiness.reason,
                                      "state": readiness.state})
            return
        cmd_retire(argparse.Namespace(target=target, _resume_request_id=duty["id"],
                                     _runner_lock_held=runner_lock_held))
    finally:
        _HERDR_SESSION = old_server


def _finish_retire(store, duty, target, ident, reason, retired=False, handover=None,
                   detail=None, pending=False, *, duty_id=None):
    """Record the same retire result from the command or its retained continuation."""
    duty_id = duty["id"] if duty else duty_id
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


def cmd_retire(args):
    target = args.target
    ident = {"harness": "-", "session_id": "-", "name": target, "pane": "-"}
    store = peer_obligations.ObligationStore()
    duty_id = getattr(args, "_resume_request_id", None)
    duty = store.get(duty_id) if duty_id else None

    def finish(reason, retired=False, handover=None, detail=None, pending=False):
        return _finish_retire(store, duty, target, ident, reason, retired, handover,
                              detail, pending, duty_id=duty_id)

    if _herdr_missing():
        # Without Herdr a tmux pane (or the name `start` gave its runtime) is the target.
        target = _tmux_named_pane(target)
        if not terminal_surface.is_tmux_pane(target):
            return finish("herdr-not-found")
        if _surface_missing(target):
            return finish("tmux-not-found")
    state, ident, _, reason = _retire_target(target)
    if not isinstance(ident.get("pane"), str) or ident["pane"] in {"", "-"}:
        # The pane may be gone while the accepted request still proves the
        # lifetime and its successor. Never borrow another requester's duty.
        own_sid, own_harness = _current_session_identity()
        own_pane = _caller_pane()
        current_requester = {"session_id": own_sid, "harness": own_harness,
                             "pane": own_pane, "server": _HERDR_SESSION or "default"}
        matches = [row for row in store.list(states=peer_obligations._PENDING_STATES | {"complete"},
                                             kinds={"retire"})
                   if row["intent"].get("target") == target
                   and _retire_requester_matches(row, current_requester)]
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
    cutover = _retire_cutover(duty)
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
                                expected_sid=ident["session_id"],
                                purpose="input" if cutover else "completion")
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
    # A tmux pane in a mode, with input off or synchronized takes no keys: nothing is
    # sent, so the duty stays waiting and a later retire or its runner tries again.
    refusal = _tmux_input_refusal(pane)
    if refusal:
        return finish(refusal + "-wait", pending=True)
    claimed = store.claim_phase(
        duty_id, {"waiting"}, "exit-requested", state="pending",
        extra={"foreground": identity, "reason": "normal-exit-requested"})
    if claimed is None:
        return finish("retire-duty-already-claimed", pending=True)
    duty = claimed
    try:
        for index, (operation, value) in enumerate(_RETIRE_ACTIONS[harness]):
            if _retire_foreground(pane, harness) != identity:
                return finish("foreground-changed", pending=True)
            if not _pane_send(pane, operation, value):
                # tmux refused the first keystroke itself: nothing reached the pane,
                # so the exit was not requested after all. Anything later stays unknown.
                refusal = _tmux_input_refusal(pane) if index == 0 else None
                if refusal and store.claim_phase(
                        duty_id, {"exit-requested"}, "waiting", state="pending",
                        extra={"reason": refusal + "-wait"}) is not None:
                    return finish(refusal + "-wait", pending=True)
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
        socket_path = (None if terminal_surface.is_tmux_pane(pane) else
                       herdr_observation.socket_path_for_endpoint(_HERDR_SESSION or "default"))
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
        # The retained runner uses this same lock; only one observer answers
        # the exit dialog while the foreground command is still returning.
        # A runner already holding it must not contend with its own second fd.
        lock = (contextlib.nullcontext(True) if getattr(args, "_runner_lock_held", False)
                else peer_obligations.legacy_runner_lock(store))
        with lock as acquired:
            if acquired:
                return _retire_claude_background_confirm(
                    target, pane, harness, ident, identity, finish,
                    own_sid, own_harness, duty=duty)
        return finish("retire-already-pending", pending=True)
    return finish("agent-still-running", pending=True)


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
        if _retire_foreground(pane, harness) != identity:
            return None, "foreground-changed"
        current_tasks = _retire_background_dialog_lines(current)
        if current_tasks is not None:
            return current_tasks, None
        return None, _blocking_screen(harness, current)

    current_tasks, screen_block = observed_dialog()
    if current_tasks is None:
        return finish(screen_block or "agent-still-running", pending=True)
    try:
        _dialog_send(pane, "send-text", "1")
        current_tasks, screen_block = observed_dialog()
        if current_tasks is None:
            return finish(screen_block or "agent-still-running", pending=True)
        _dialog_send(pane, "send-keys", "enter")
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
        _dialog_send(pane, "send-text", "3")
        current_tasks, screen_block = observed_dialog()
        if current_tasks is None:
            return finish(screen_block or "agent-still-running", pending=True)
        _dialog_send(pane, "send-keys", "enter")
    except (OSError, subprocess.SubprocessError):
        return finish("agent-still-running", pending=True)
    return finish("retire-declined-background-work" + (stopped or ""), pending=True)
_WATCH_WAKE_FD = None


def _watch_wakeup_signal(_signum, _frame):
    fd = _WATCH_WAKE_FD
    if fd is not None:
        with contextlib.suppress(OSError):
            os.write(fd, b"w")


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


def settle_steward_exited_watch(watch_id, *, reason="steward-exited"):
    """End one exact watch whose supervisor session is closed (idempotent).

    The same ``terminal_pending`` request a retire uses: a live observer is woken to
    settle it, otherwise the receipt is written here. ``True`` once the watch has
    (or is being given) its receipt; ``False`` when the arm is not readable."""
    paths = _watch_paths(watch_id)
    if paths.receipt.exists():
        return True
    arm = _read_json(paths.arm)
    if not isinstance(arm, dict) or arm.get("watch_id") != watch_id:
        return False
    expected = dict(arm.get("agent") or {})
    _write_json_atomic(paths.observation, {
        "schema_version": _WATCH_SCHEMA, "watch_id": watch_id,
        "state": "terminal_pending", "reason": reason,
        "server": arm.get("server") or "default",
        "agent": {key: expected.get(key, "-") for key in ("harness", "session_id", "pane")},
    })
    return _settle_terminal_watch_if_unobserved(watch_id, arm, reason_override=reason)


def _settle_closed_watch(watch_id, arm, root=None, *, evidence=None, lock_held=False):
    """End a watch whose supervisor closed or whose target was replaced, before it is
    re-armed. Never needs the herdr socket; unknown evidence leaves the watch alone."""
    if not isinstance(evidence, _WatchCloseEvidence):
        evidence = _WatchCloseEvidence(evidence)
    if _settle_ended_seatless_recipient(
            watch_id, arm, root, census=evidence.recipient_census, lock_held=lock_held):
        return True
    paths = _watch_paths(watch_id, root)
    if paths.receipt.exists():
        return True
    if not isinstance(arm, dict) or arm.get("watch_id") != watch_id:
        return False
    try:
        reason = _watch_close_reason(watch_id, arm, evidence)
    except Exception:
        reason = None
    if reason is None:
        return False
    current = _read_json(paths.arm)
    if (not isinstance(current, dict) or current.get("watch_id") != watch_id
            or current.get("agent") != arm.get("agent")
            or current.get("steward") != arm.get("steward")
            or (current.get("server") or "default") != (arm.get("server") or "default")):
        return False
    expected = dict(current.get("agent") or {})
    _write_json_atomic(paths.observation, {
        "schema_version": _WATCH_SCHEMA, "watch_id": watch_id,
        "state": "terminal_pending", "reason": reason,
        "server": current.get("server") or "default",
        "agent": {key: expected.get(key, "-") for key in ("harness", "session_id", "pane")},
    })
    if lock_held:
        _write_terminal_watch_receipt(watch_id, current, reason)
        return True
    return _settle_terminal_watch_if_unobserved(
        watch_id, current, root, reason_override=reason)


def _request_watch_terminal(identity, reason):
    """Ask matching observers to settle on their next bounded wake."""
    if not isinstance(identity, dict):
        return None
    server = identity.get("server") or "default"
    pane, harness, sid = identity.get("pane"), identity.get("harness"), identity.get("session_id")
    if not all(isinstance(value, str) and value and value != "-"
               for value in (pane, harness, sid)):
        return None
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


def _run_herdr_get(target, *, server=None):
    if terminal_surface.is_tmux_pane(target):
        tmux = _tmux_surface(target)
        return tmux.agent(target) if tmux else None
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
    watch_payload = _run_herdr_get(target)
    state, agent, _code, reason = _interpret_payload(watch_payload, target)
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
                _spawn_watch_observer(existing_id, existing_arm, claim_held=(
                    _watch_claim_path_for_arm(existing_paths, existing_arm, server)
                    in {logical_claim, claim}))
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
            "steward_server": getattr(args, "steward_server", None) or server,
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
                end_with_test(proc)
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
                       from_identity=(steward_sid, steward_harness), pane=agent["pane"],
                       target_cwd=_payload_cwd(watch_payload))
    finally:
        if claim_fd is not None:
            os.close(claim_fd)
        os.close(logical_fd)
        os.close(alias_fd)
        if seat_fd is not None:
            os.close(seat_fd)

    print(_armed_line(arm, paths))
    return 0


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
        # Only after the wake handler exists: naming ourselves lets the runner
        # signal this process.
        arm = _record_own_watch_identity(args.watch_id, paths, server) or arm

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
            steward_server = arm.get("steward_server", server) or "default"
            if steward_seat.get("kind") == "pane" and steward_server == server:
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
                    steward_pidfd = pidfd_open(native_pid, 0)
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
                if wait_identity_live:
                    try:
                        wait_pidfd = pidfd_open(int(wait_pid), 0)
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
                    if wait_start:
                        try:
                            wait_pidfd = pidfd_open(int(wait_pid), 0)
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
                            if readiness.reason != "shared-readiness-revalidating":
                                # No refresh was requested (a pending or unknown
                                # cached projection, or a local check failed).
                                # Without a wake the runner would read this native
                                # result only at its next backoff pass.
                                _wake_shared_runner(peer_obligations.ObligationStore().root)
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


def _spawn_watch_observer(watch_id, arm, *, service_current=None,
                          recipient_census=None, claim_held=False):
    """Reattach and publish under the same short-lived claim as admission."""
    if not isinstance(arm, dict) or arm.get("watch_id") != watch_id:
        return False
    paths = _watch_paths(watch_id)
    if paths.receipt.exists():
        return True
    if not claim_held:
        claim = _watch_claim_path_for_arm(paths, arm, arm.get("server") or "default")
        if claim is not None:
            claim_fd = None
            try:
                claim_fd = _open_lock(claim)
                if not _acquire_bounded(claim_fd, _CLAIM_TIMEOUT_MS):
                    return False
                return _spawn_watch_observer(
                    watch_id, arm, service_current=service_current,
                    recipient_census=recipient_census, claim_held=True)
            except (OSError, TypeError, ValueError):
                return False
            finally:
                if claim_fd is not None:
                    os.close(claim_fd)
        # Older accepted arms may have no steward/claim identity. Retain their
        # lifetime-lock recovery without inventing a claim or reporting a
        # contended lock as proof of a published observer.
    current_arm = _read_json(paths.arm)
    if (not isinstance(current_arm, dict) or current_arm.get("watch_id") != watch_id
            or (current_arm.get("steward") or {}).get("session_id") !=
                (arm.get("steward") or {}).get("session_id")):
        return False
    arm = current_arm
    if not isinstance(recipient_census, _WatchCloseEvidence):
        recipient_census = _WatchCloseEvidence(recipient_census)
    if _settle_closed_watch(watch_id, arm, evidence=recipient_census):
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
            # A held lifetime lock can precede publication of a replacement.
            # It proves attachment only with a receipt or a recorded live PID.
            return paths.receipt.exists() or _watcher_present(_read_json(paths.arm))
        current_arm = _read_json(paths.arm)
        if (not isinstance(current_arm, dict) or current_arm.get("watch_id") != watch_id
                or (current_arm.get("steward") or {}).get("session_id") !=
                    (arm.get("steward") or {}).get("session_id")):
            return False
        if _settle_closed_watch(
                watch_id, current_arm, evidence=recipient_census, lock_held=True):
            return True
        arm = current_arm
        os.set_inheritable(lock_fd, True)
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
        # Record the generation this observer will run under before it exists.
        # A spawner killed right after the spawn then leaves only the watcher
        # identity unwritten, and the observer names itself; the arm still
        # matches the argv a later release takeover expects.
        ledger = dict(arm)
        ledger["observer_generation"] = int(arm.get("observer_generation", 1)) + 1
        ledger["rearm_count"] = int(arm.get("rearm_count", 0)) + 1
        ledger["observer_protocol"] = 2
        _write_json_atomic(paths.arm, ledger)
        # Opened last: nothing that can fail before the spawn holds it open.
        log_fd = os.open(str(paths.log), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            # As in `cmd_watch`: a wake sent before the observer installs its
            # handler stays pending instead of killing it.
            watcher_signal_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
            try:
                proc = subprocess.Popen(
                    argv, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True, pass_fds=(lock_fd,),
                    cwd=str(paths.arm.parent), env=env,
                )
                end_with_test(proc)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, watcher_signal_mask)
        finally:
            os.close(log_fd)
        # Re-read: the observer may have written the session it took meanwhile.
        current_arm = _read_json(paths.arm)
        updated = dict(current_arm if isinstance(current_arm, dict) else ledger)
        updated["watcher"] = {"pid": proc.pid, "pid_start": process_start_ticks(proc.pid) or ""}
        _write_json_atomic(paths.arm, updated)
        return True
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        return False
    finally:
        os.close(lock_fd)


def _ensure_watch_observers(*, service_current=None, entries=None):
    root = _watch_root()
    transports = {}
    recipient_census = _WatchCloseEvidence()
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
            if _settle_closed_watch(watch_id, arm, root, evidence=recipient_census):
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


def undelivered_watch_rows(entries=None, root=None, *, deadline=None):
    """This session's watch receipts that nobody acknowledged and no queued
    exact notice carries: `status --undelivered` and the Claude prompt sweep.

    Only watches with a receipt whose recipient can be this session reach the
    per-watch status read (`_receipt_watch_entries`), and each seat ledger is
    parsed once per call, so the cost follows this session's watches instead
    of every session's.

    ``deadline`` (``time.monotonic()``) bounds a hook. Watches it did not reach
    stay un-acked for the next prompt, the Stop path and the asyncRewake hook;
    when it cuts the queued-notice read short nothing is listed, because an
    unproven notice could show the same receipt twice.
    """
    root = root or _watch_root()
    sid, harness = _current_session_identity()
    rows, seat_holders = [], {}
    try:
        import session_tidy
        one_ledger_read = session_tidy.seat_ledger_read_scope()
    except Exception:  # noqa: BLE001 - without the shared cache each read happens as before
        one_ledger_read = contextlib.nullcontext()
    with one_ledger_read:
        if entries is None:
            entries = _receipt_watch_entries(root, sid, harness, seat_holders)
        for watch_id, arm in entries:  # newest arm first
            if deadline is not None and time.monotonic() >= deadline:
                break
            if not _watch_may_reach(watch_id, arm, sid, harness, seat_holders):
                continue
            row = _status_entry(watch_id, arm, root)
            recipient = row.get("recipient") or {}
            if (row["state"] == "receipt" and recipient.get("session_id") == sid
                    and recipient.get("harness") == harness):
                rows.append(row)
    if not rows:
        return rows
    # A queued exact native notice owns the display/ACK path for its receipt.
    # One read of this recipient's queued rows instead of one per receipt.
    try:
        queued = {ref for pending in peer_message._pending_rows(
                      recipient={"harness": harness, "session_id": sid}, deadline=deadline)
                  for ref in pending.get("refs") or () if isinstance(ref, str)}
    except Exception:  # noqa: BLE001 - as before: an unreadable queue never hides a receipt
        queued = set()
    if deadline is not None and time.monotonic() >= deadline:
        return []
    return [row for row in rows if f"peer-watch:{row['watch_id']}" not in queued]


def cmd_status(args):
    root = _watch_root()
    if args.undelivered and not args.watch:
        # The same bounded read as the prompt sweep; not every arm.
        entries = None
    else:
        entries = _watch_entries(root)
    if args.watch:
        entries = [(w, a) for w, a in entries if w == args.watch]
    if args.undelivered:
        rows = undelivered_watch_rows(entries, root)
    else:
        rows = [_status_entry(w, a, root) for w, a in entries]
    peer_rows, peer_omitted, peer_error = [], 0, None
    if not args.watch:
        try:
            sid, harness = _current_session_identity()
            peer_rows, peer_omitted = _peer_status_rows(sid, harness)
        except Exception as exc:  # noqa: BLE001 - peer facts never break watch output
            peer_rows, peer_omitted, peer_error = [], 0, type(exc).__name__
    if args.json:
        payload = {"watch_root": str(root), "watches": rows}
        if not args.watch:
            payload["peer_messages"] = peer_rows
            payload["peer_messages_omitted"] = peer_omitted
            if peer_error is not None:
                payload["peer_messages_error"] = peer_error
        print(json.dumps(payload, ensure_ascii=False))
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
    for entry in peer_rows:
        print(_peer_status_line(entry))
    if peer_omitted:
        print(f"peer-message omitted={peer_omitted}")
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


def _herdr_prompt(target, text, *, wait, timeout_ms, start_aware=False):
    """One external herdr prompt write.

    With `start_aware`, an `execve` failure is reported as `herdr-not-started`
    (nothing reached the pane, so its carrier may retry); every later failure,
    including a timeout, may already have typed and stays ambiguous. Only the
    ordinary native-submission call sites ask for that distinction; the plain
    call keeps the existing single-shot contract for every other caller.
    """
    if terminal_surface.is_tmux_pane(target):
        return _tmux_prompt(target, text, wait=wait, timeout_ms=timeout_ms, start_aware=start_aware)
    cmd = _herdr_argv("agent", "prompt", target, text)
    if wait:
        cmd += ["--wait", "--until", "working", "--timeout", str(timeout_ms)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_ms / 1000 + 15)
    except subprocess.TimeoutExpired:
        return None, None
    except OSError as exc:
        if start_aware and exc.errno in _EXEC_NEVER_STARTED_ERRNOS:
            return None, {"error": {"code": "herdr-not-started"}}
        return None, None
    except subprocess.SubprocessError:
        return None, None
    return proc.returncode, _herdr_prompt_payload(proc.stdout, proc.stderr)


def _prompt_foreground(pane, harness, *, info=None):
    """Prompt-only foreground lifetime, including a native child of a group wrapper.

    The agent may be the pane's root process (tmux ``new-session "claude"``), so it
    can be the same pid as ``shell_pid``; the argv basename already rules out a shell."""
    info = info if info is not None else _retire_pane_info(pane)
    if not isinstance(info, dict):
        return None
    found = []
    for process in info.get("foreground_processes", ()):
        pid = process.get("pid") if isinstance(process, dict) else None
        actual = _retire_process_record(pid) if type(pid) is int and pid > 0 else None
        argv = (actual or {}).get("argv") or []
        if (actual and argv and os.path.basename(argv[0]) == harness
                and actual.get("group") == info.get("foreground_process_group_id")):
            found.append({"pid": pid, **actual, "shell_pid": info.get("shell_pid"),
                          "shell_start": _proc_start_ticks(info.get("shell_pid"))})
    return found[0] if len(found) == 1 else None


def _prompt_foreground_proof(pane, harness, expected_sid):
    info = _retire_pane_info(pane)
    foreground = _prompt_foreground(pane, harness, info=info)
    if not foreground or _prompt_process_session(pane, harness, info) != (expected_sid, None):
        return None
    pid, start = foreground["pid"], foreground["start"]
    if _proc_start_ticks(pid) != start:
        return None
    try:
        namespace = os.readlink(f"/proc/{pid}/ns/pid")
        if (namespace != os.readlink("/proc/self/ns/pid")
                or _proc_start_ticks(pid) != start
                or _prompt_process_session(pane, harness, info) != (expected_sid, None)):
            return None
    except OSError:
        return None
    return {"pid": pid, "start": start, "pid_namespace": namespace, "session_id": expected_sid}


def _native_idle_prompt_guard(target, harness, expected_sid, expected_pane,
                              expected_foreground=None, fresh_ok=False):
    """Prove the exact idle Claude/OpenCode TUI and empty composer before one prompt send."""
    state, ident, _code, reason = _retire_target(target)
    if (reason or ident.get("harness") != harness
            or ident.get("pane") != expected_pane):
        return reason or "target-identity-changed", None
    current, identity_error = _prompt_current_candidate(
        expected_pane, expected_harness=harness, expected_sid=expected_sid,
        require_process_session=True, target_observation=(state, ident))
    if current is None:
        return identity_error or "target-identity-unverified", None
    from session_tidy_clear import peer_input_state
    state, _source = _effective_retire_state(state, ident)
    state = peer_input_state(harness, expected_sid, expected_pane, state)
    # A Codex thread with no first turn has no state yet; its empty composer,
    # checked below, is the evidence that it is waiting for input.
    settled = ("idle", "done", "unknown") if fresh_ok else ("idle", "done")
    if state not in settled:
        return "native-state-" + str(state), None
    current_state, current_pane = _agent_state(target)
    current_state, _source = _effective_retire_state(current_state, ident)
    current_state = peer_input_state(harness, expected_sid, expected_pane, current_state)
    if current_state not in settled or current_pane != expected_pane:
        return "target-state-changed", None
    if current_state == "unknown":
        current_state, state = "idle", "idle"
    readiness = _pane_readiness(target, current_state, expected_harness=harness,
                                expected_sid=expected_sid, expected_pane=expected_pane,
                                purpose="input", target_observation=(current_state, ident),
                                native_session_id=current["_native_session_id"])
    if readiness.state != "ready":
        return readiness.reason or "target-identity-unverified", None
    proof = _prompt_foreground_proof(expected_pane, harness, expected_sid)
    if proof is None:
        return "foreground-unverified", None
    if expected_foreground is not None and proof != expected_foreground:
        return "target-process-changed", None
    refusal = _tmux_input_refusal(expected_pane)
    if refusal:
        return refusal, None
    lines = _read_screen(expected_pane)
    if lines is None:
        return "screen-unavailable", None
    screen_reason = _screen_ready(harness, lines)
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
    refusal = _tmux_input_refusal(expected_pane)
    if refusal:
        return refusal, None
    lines = _read_screen(expected_pane)
    if lines is None:
        return "screen-unavailable", None
    screen_reason = _screen_ready(expected_harness, lines)
    if screen_reason:
        return screen_reason, None
    return None, proof


def _form_open(target):
    """Compatibility wrapper for the shared observed-harness screen classifier."""
    harness, _sid, _name = _resolve_target(target)
    return _blocking_screen_for_target(target, harness) is not None


def _blocking_screen_for_target(target, harness, *, window=15):
    if terminal_surface.is_tmux_pane(target):
        tmux = _tmux_surface(target)
        return _blocking_screen(harness, _screen_lines((tmux.read_visible(target) if tmux else "") or ""),
                                window=window)
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
        foreground = (_prompt_foreground(pane, harness, info=info)
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
        # A missing response stays pending and reuses this exact request.
        _wake_shared_runner(root)
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


def _message_census(server, pane):
    """The pane census of ``server``; for a tmux pane whose server is gone, an empty
    complete one (nothing listens on its socket, so it holds no pane or session)."""
    _bare, socket_path = terminal_surface.tmux_address(pane)
    if socket_path:
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.settimeout(2.0)
            probe.connect(socket_path)
        except (FileNotFoundError, ConnectionRefusedError):
            return herdr_observation.PaneCensus(
                herdr_observation.endpoint_name(server), (), time.monotonic())
        except OSError:
            pass
        finally:
            probe.close()
    return herdr_observation.pane_census(session=server)


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
            census = _message_census(server, original.get("pane_id"))
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
    if expected_pane and observed.get("pane") == expected_pane and reason is None:
        current, _error = _prompt_current_candidate(
            expected_pane, expected_harness=recipient.get("harness"),
            expected_sid=recipient.get("session_id"), require_process_session=True,
            target_observation=("observed", observed))
        if current is not None:
            return None  # Native process proof outranks an outdated census/terminal label.
    census = _message_census(server, expected_pane)
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
    recipient = peer_message.pending_delivery_recipient(row) or row.get("to") or {}
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
    if not (peer_message._valid_transfer_endpoint(sender)
            or peer_message._valid_pane_bound_endpoint(sender)):
        # A sender with no address (runtime, unknown) cannot receive a notice.
        # The original duty stays with the runner; its settlement is the result.
        return None
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
            restart_gate = (_codex_interrupt_start_gate(expected_pane)
                            if expected_pane else False)
            result = _deliver_codex_pending(
                row["ref"], expected_pane, timeout=0.25, start_interrupted=restart_gate)
            if result.get("status") == "received":
                flushed += 1
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    return flushed, stuck

def _resume_message_obligation(duty, store, *, read_context=None):
    """Observe and deliver on the duty's endpoint, including unresolved addresses."""
    global _HERDR_SESSION
    old_server = _HERDR_SESSION
    server = ((duty.get("intent") or {}).get("identity") or {}).get("server") or "default"
    _HERDR_SESSION = None if server == "default" else server
    try:
        return _resume_message_obligation_scoped(duty, store, read_context=read_context)
    finally:
        _HERDR_SESSION = old_server


def _resume_message_obligation_scoped(duty, store, *, read_context=None):
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
    if delay_for:
        # An additional notice follows its original duty: once that duty is
        # settled (complete or cancelled) nothing more is owed. An open,
        # unreadable, or missing original settles nothing.
        original = store.get(_message_obligation_id(delay_for))
        if original and original.get("state") in {"complete", "cancelled"}:
            checkpoint(state="complete", delivery="not-required", cleanup="complete",
                       observation={"ref": ref or "", "reason": "delay-original-settled",
                                    "original_state": original["state"]})
            return
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
    if peer_message._unresolved_target(row.get("to")) and not row.get("bound_to"):
        if row.get("receipt") == "target-identity-changed" or "ambiguous" in row.get("receipt", ""):
            checkpoint(state="unknown", observation={"ref": ref, "reason": row["receipt"]})
            return
        current, error, _panes = _resolve_prompt_target((row.get("to") or {}).get("name"))
        if not current or not peer_message.usable_session_id(current.get("session_id")):
            checkpoint(state="unknown", observation={"ref": ref, "reason": error or "target-identity-unverified"})
            return
        row = peer_message.bind_unresolved_pending_recipient(ref, {
            "harness": current["harness"], "session_id": current["session_id"],
            "pane_id": current.get("pane")})
        if row is None:
            checkpoint(state="unknown", observation={"ref": ref, "reason": "target-binding-changed"})
            return
    if intent.get("handoff_acceptance"):
        accepted = row.get("acceptance") or {}
        if not accepted.get("read_and_accepted"):
            checkpoint(state="pending", observation={"reason": "awaiting-agent-acceptance"})
            return
        import session_tidy_runner as runner
        context = row.get("handoff") or {}
        successor = context.get("successor") or {}
        global _HERDR_SESSION
        old_server = _HERDR_SESSION
        _HERDR_SESSION = None if successor.get("server") == "default" else successor.get("server")
        try:
            # Even old settled=true rows are replayed through the existing
            # ledger writer; this repairs acceptance-before-ledger crashes
            # without asking the current pane to reassert the same identity.
            settlement = runner.resume_handoff_acceptance(ref, steward=sys.modules[__name__])
        except (OSError, ValueError, KeyError, TypeError):
            if duty.get("state") == "complete":
                return
            checkpoint(state="pending", observation={"reason": "handoff-acceptance-unsettled"})
            return
        finally:
            _HERDR_SESSION = old_server
        if not settlement.get("settled") or not settlement.get("retire_id"):
            checkpoint(state="pending", cleanup="pending", observation={"ref": ref,
                "reason": settlement.get("retire_reason", "handoff-retire-unsettled")})
            return
        _HANDOFF_ACCEPTANCE_REPLAYED.add((str(store.root.resolve(strict=False)), duty_id))
        if len(_HANDOFF_ACCEPTANCE_REPLAYED) > 4096:
            _HANDOFF_ACCEPTANCE_REPLAYED.clear()
        if duty.get("state") == "complete":
            return
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
    native_recipient = peer_message.pending_delivery_recipient(row)
    if (native_recipient and row.get("state") == "pending"
            and native_recipient.get("harness") in {"claude", "opencode"}):
        import session_shell
        attachment, _attachment_reason = session_shell.exact_binding(
            native_recipient["harness"], native_recipient["session_id"])
        if attachment is not None:
            result = session_shell.deliver_pending(ref, native_recipient["harness"],
                                                   native_recipient["session_id"])
            checkpoint(state="pending", observation={"ref": ref,
                       "reason": result.get("reason") or "awaiting-exact-receiver-ref"})
            return
    if (native_recipient and not native_recipient.get("pane_id")
            and not ((duty.get("intent") or {}).get("identity") or {}).get("pane")):
        if row.get("state") == "pending" and native_recipient.get("harness") == "codex":
            peer_message.deliver_pending_codex(ref, timeout=0.25)
        # Claude/OpenCode consume their exact native pending rows at their existing callback.
        checkpoint(state="pending", observation={"ref": ref, "reason": "awaiting-native-receiver"})
        return
    identity = (duty.get("intent") or {}).get("identity") or {}
    # Identity is held outside mutable observations. The accepted target stays
    # nameable only while its exact harness/session/pane still resolves.
    recipient = peer_message.pending_delivery_recipient(row) or row.get("to") or {}
    server = identity.get("server") or "default"
    old_server = _HERDR_SESSION
    _HERDR_SESSION = None if server == "default" else server
    try:
        state, ident, _code, reason = _retire_target(
            recipient.get("pane_id") or target or recipient.get("name") or "")
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
        held_codex_restart = (
            recipient.get("harness") == "codex"
            and row.get("state") == "queued"
            and str(row.get("receipt") or "").startswith("native-queue-held-interrupted"))
        if row.get("state") in {"queued", "unverified"} and not held_codex_restart:
            # Keep the acceptance receipt intact when a fresh target check is
            # still inconclusive. It is not a receive, and another pass will
            # reobserve the target without rewriting this unchanged evidence.
            # The sender notice is the existing one-hour rule; the Claude hook
            # already sweeps these states, and this covers every other sender.
            _ensure_sender_delay_notice_if_due(row)
            checkpoint(state="unknown", observation={"ref": ref,
                       "transport_state": row["state"],
                       "reason": row.get("receipt", "delivery-unconfirmed")})
            return
        if (reason or ident.get("harness") != recipient.get("harness")
                or (expected_pane and ident.get("pane") != expected_pane)):
            checkpoint(state="unknown", observation={"ref": ref,
                       "reason": reason or "target-identity-changed",
                       "observed_pane": ident.get("pane", "")})
            return
        pinned_pane = expected_pane or ident.get("pane") or ""
        if pane_bound:
            current, current_error = _prompt_current_candidate(
                pinned_pane, expected_harness=recipient.get("harness"),
                target_observation=(state, ident))
        else:
            current, current_error = _prompt_exact_candidate(
                pinned_pane, expected_harness=recipient.get("harness"),
                expected_sid=recipient.get("session_id"), target_observation=(state, ident))
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
        if (recipient.get("harness") != "codex" or pane_bound) and (
                readiness.state == "unknown" or readiness.reason.startswith("bound-registered-work")):
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
            latest, latest_error = _prompt_exact_candidate(
                pinned_pane, expected_harness=recipient.get("harness"),
                expected_sid=recipient.get("session_id"))
            if latest is None:
                _ensure_sender_delay_notice_if_due(row)
                checkpoint(state="unknown", observation={"ref": ref,
                           "reason": latest_error or "target-identity-unverified",
                           "observed_pane": pinned_pane})
                return
        producer = row.get("producer_binding") or {}
        if (producer.get("schema") in {peer_message._RUNTIME_NOTICE_SCHEMA,
                                        peer_message._SESSION_NOTICE_PRODUCER_SCHEMA}
                and recipient.get("harness") == "claude"
                and state in {"idle", "done"}
                and row.get("state") == "pending"):
            # Re-enter the same prompt/courier with the exact stored body and
            # refs once the native recipient is idle. This reuses its existing
            # claim and async-capable Claude send; it does not wait for a user
            # prompt or create a second transfer.
            body_text = row.get("text") or ""
            source, separator, _trailer = body_text.rpartition("\n\n(runtime-from:")
            if row.get("body_ref"):
                try:
                    source = peer_message.read_peer_payload(ref)["body"]
                    separator = True
                except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                    separator = False
            if separator and peer_obligations.session_notice_delivery_current(row):
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as body:
                    body.write(source)
                    body.flush()
                    argv = ["prompt", pinned_pane, "--body-file", body.name]
                    for ref_value in row.get("refs") or []:
                        argv.extend(("--ref", ref_value))
                    try:
                        cmd_prompt(build_parser().parse_args(argv),
                                   expected_recipient=(recipient.get("harness"),
                                                       recipient.get("session_id")),
                                   producer_binding=producer)
                    except (OSError, ValueError, RuntimeError):
                        pass
                current = read_pending()
                if current and current.get("state") == "received":
                    peer_message._ack_registered_delivery(current)
                checkpoint(state="pending", observation={"ref": ref,
                    "reason": "runtime-notice-native-retry",
                    "transport_state": (current or row).get("state")})
                return
        direct_reason = None
        if recipient.get("harness") in {"claude", "opencode"} and row.get("state") == "pending":
            # Same guarded idle input for both harnesses. The OpenCode idle plugin
            # may still take the row first: rpc_claim lets exactly one side send.
            direct_reason, _o, _v, _r = _submit_pending_native(
                row, pinned_pane, recipient.get("session_id"), pinned_pane, state,
                harness=recipient["harness"], no_trailer=row.get("no_trailer", False))
        if recipient.get("harness") == "codex":
            # Same sender-side screen gate as `cmd_prompt`: an interrupted
            # thread starts only from the rechecked pane's empty, form-free
            # composer. Without a rechecked pane there is no screen evidence.
            restart_gate = (_codex_interrupt_start_gate(expected_pane)
                            if expected_pane else False)
            _deliver_codex_pending(
                ref, expected_pane, timeout=0.25, start_interrupted=restart_gate)
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
            latest = current or row
            wait_reason = latest.get("receipt", "awaiting-receipt")
            if direct_reason and latest.get("state") == "pending":
                # Name what the guard saw instead of the stale "native-pending".
                wait_reason = direct_reason
            checkpoint(state="pending", observation={
                "ref": ref, "state": latest.get("state"), "reason": wait_reason})

    finally:
        _HERDR_SESSION = old_server


def _resume_ordinary_notice_obligation(duty, store):
    """Compatibility entry for one canonical duty; the normal courier owns notices."""
    _resume_session_notice_obligations([duty], store)


def _resume_registered_obligation(duty, store, *, read_context=None,
                                  settlement_context=None):
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
    from dispatch_seat_handover import _effective, lineage_effect_lock
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
        elif transferred and transferred.get("state") == "received":
            # The exact recipient receipt is durable even when this ACK write
            # failed. Keep the existing duty/ref for the next natural retry;
            # preparing another transfer would duplicate a known receipt.
            store.update(duty["id"], observation={**observation,
                "reason": ("received-delivery-ack-pending" if bound_received else
                           "received-delivery-identity-unresolved")})
            return
    parent = [row.metadata.get(key, "") for key in
              ("parent_runtime_pid", "parent_runtime_pid_start", "parent_runtime_ns")]
    # A native hook belongs to its original runtime. A live holder cannot wake
    # a replacement process after that runtime died. Unknown identity stays protected.
    # The duty follows confirmed handover; the signed queue stays under the
    # registered parent, even when its current recipient is a successor.
    recipient_sid, recipient_harness = _effective(row.metadata, jobs, read_context=read_context)
    recipient_harness = recipient_harness or harness
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
        fresh_pair = _effective(fresh.metadata, jobs) if fresh is not None else ("", "")
        fresh_pair = (fresh_pair[0], fresh_pair[1] or harness)
        if (fresh is None or fresh.status != row.status
                or fresh_pair != (recipient_sid, recipient_harness)
                or any(fresh.metadata.get(key) != row.metadata.get(key)
                       for key in identity_fields)):
            return None
        return fresh

    if row.status not in {"open", "running", "done"}:
        return
    notice_id = intent.get("notice_delivery_id")
    record = None
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
        redrive_performed = False
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
            materialize = lambda: materialize_after_terminal_close(jobs, row.attempt_id)
            owner_meta = row.metadata
            if (settlement_context is not None and row.status == "done"
                    and owner_meta.get("worker_type") == "owner"
                    and owner_meta.get("workflow_completion") == "runtime-v1"):
                from dispatch_contract import supervisor_lease_is_held
                lease_held = supervisor_lease_is_held(jobs, owner_meta)
                if not lease_held:
                    from dispatch_registry_cache import registry_view
                    view = registry_view(jobs, append_only=True)
                    related = view.completion_related(
                        row.attempt_id,
                        owner_meta.get("owner_route_id") or owner_meta.get("route_id", ""),
                        owner_meta.get("owner_route_hash") or owner_meta.get("route_hash", ""))
                    def refresh_owner_observation():
                        fresh = current_attempt_row(jobs, row.attempt_id)
                        if fresh is None:
                            return None
                        fresh_view = registry_view(jobs, append_only=True)
                        fresh_related = fresh_view.completion_related(
                            fresh.attempt_id,
                            fresh.metadata.get("owner_route_id") or
                            fresh.metadata.get("route_id", ""),
                            fresh.metadata.get("owner_route_hash") or
                            fresh.metadata.get("route_hash", ""))
                        return fresh.status, fresh.raw, fresh.metadata, fresh_related
                    redrive_deferred = settlement_context.registered_owner_redrive(
                        jobs, row.status, row.raw, owner_meta, related,
                        lease_held=False, materialize=materialize,
                        refresh=refresh_owner_observation)
                    redrive_performed = not redrive_deferred
                # The helper either performed this materialization or deferred
                # it; this duty must not perform a second copy.
                materialize = None
            if materialize is not None:
                materialize()
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
                                          "delivery_id": delivery_id},
                             skip_unchanged=True)
                return
            if redrive_performed and settlement_context is not None:
                # The exact record is now present and has passed recovery validation.
                settlement_context.forget_unsettled_owner(jobs, row.attempt_id)
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
            fresh = fresh_recipient_row()
            if fresh is None:
                return
            row = fresh
            original_sid = str(row.metadata.get("parent_sid") or "")
            if (len(matches) == 0 and (recipient_harness, recipient_sid) == (harness, original_sid)
                    and row.status == "done"):
                with lineage_effect_lock(row.metadata, jobs, recipient_sid) as current_owner:
                    final_row = current_attempt_row(jobs, identity["attempt_id"])
                    if (not current_owner or final_row is None or final_row.status != "done"
                            or any(final_row.metadata.get(key) != row.metadata.get(key)
                                   for key in identity_fields)):
                        return
                    closed = _closed_registered_route_outcome(final_row, jobs)
                    if closed is not None:
                        store.update(duty["id"], state="complete", delivery="not-required",
                            cleanup="complete", observation={**observation, "reason": "route-closed",
                                **closed, "recipient_sid": recipient_sid}, skip_unchanged=True)
                        return
                if isinstance(record, dict):
                    from dispatch_completion_join import expire_pending_for_dead_recipient
                    expiry = expire_pending_for_dead_recipient(
                        jobs, row.metadata, record, attempt_id=row.attempt_id)
                    if expiry == "expired":
                        store.update(duty["id"], observation={**observation,
                            "reason": "recipient-session-gone", "session_id": recipient_sid,
                            "recipient_pid": row.metadata.get("parent_runtime_pid", ""),
                            "recipient_pid_start": row.metadata.get("parent_runtime_pid_start", "")},
                            skip_unchanged=True)
                        return
            store.update(duty["id"], observation={"reason": "recipient-unavailable",
                                                  "session_id": recipient_sid},
                         skip_unchanged=True)
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
        claim_owner = owner
        try:
            claimed_members = pending.claim_ready_batch(
                jobs.parent, storage_recipient, delivery_id, claim_owner=claim_owner,
                lease_seconds=30, jobs=jobs, require_generation_proof=False,
                retry_context=f"{recipient_harness}:{recipient_sid}:{pane}:{duty['id']}")
        except pending.PendingDeliveryError:
            # Positive holds and claims owned by another carrier leave every
            # obligation open for the runner's existing retry/backoff.
            return
        from dispatch_recipient_receipt import release_unsubmitted_claim
        def release_members():
            for acquired in claimed_members:
                release_unsubmitted_claim(jobs.parent, storage_recipient, acquired,
                                          claim_owner, recipient_sid)

        member_context = []
        transfer_members = []
        for claimed in claimed_members:
            member_id = claimed["delivery_id"]
            stored_member = pending.read(jobs.parent, storage_recipient, member_id)
            expected_identity = pending.received_ack_identity(claimed, storage_recipient, member_id)
            # keep_claim may project the outward receipt in memory. First-write
            # guards and safe release still bind the original stored identity.
            claimed[IDENTITY_KEY] = expected_identity
            if (stored_member is None or pending.received_ack_identity(
                    stored_member, storage_recipient, member_id) != expected_identity or not keep_claim(
                    jobs.parent, storage_recipient, member_id, claimed, claim_owner, jobs=jobs)):
                release_members()
                return
            member_receipt = (stored_member.get("receipt") or {})
            stall_notice = (member_receipt.get("kind") == "supervision"
                            and member_receipt.get("reason") == "attempt-stalled")
            if stall_notice:
                from dispatch_supervision import notice_is_current
                if not notice_is_current(stored_member):
                    for acquired in claimed_members:
                        pending.release_claim(jobs.parent, storage_recipient,
                            acquired["delivery_id"], claim_owner=claim_owner)
                    return
            attempt_ids = list(stored_member.get("attempt_ids") or [])
            if not attempt_ids and member_id == delivery_id:
                attempt_ids = [row.attempt_id]
            matching_rows = ([row] if len(claimed_members) == 1 and member_id == delivery_id
                             else [current_attempt_row(jobs, aid, read_context=read_context)
                                   for aid in attempt_ids])
            seed_notice = bool(notice_id and len(claimed_members) == 1
                               and member_id == delivery_id)
            if stall_notice:
                matching_rows = [candidate for candidate in matching_rows
                    if candidate is not None and candidate.status in {"open", "running"}
                    and candidate.attempt_id == member_receipt.get("owner_attempt_id")]
            else:
                matching_rows = [candidate for candidate in matching_rows
                    if candidate is not None and candidate.status == "done"
                    and (seed_notice or candidate.metadata.get("delivery_id") == member_id)]
            if len(matching_rows) != 1:
                release_members()
                return
            member_row = matching_rows[0]
            if len(claimed_members) == 1 and member_id == delivery_id:
                member_duty_id, member_duty = duty["id"], duty
            else:
                member_identity = {"jobs": str(jobs.resolve()),
                    "attempt_id": member_row.attempt_id, "session_id": recipient_sid,
                    "harness": harness, "server": identity.get("server") or "default"}
                member_duty_id = peer_obligations.stable_duty_id(
                    "registered-batch", member_identity)
                member_duty = store.get(member_duty_id)
            if (member_duty is None or (member_duty.get("intent") or {}).get("carrier")
                    != intent["carrier"]):
                release_members()
                return
            context_item = {**claimed, STORAGE_KEY: storage_recipient,
                            IDENTITY_KEY: expected_identity}
            member_context.append(context_item)
            transfer_members.append({"duty_id": member_duty_id, "jobs": str(jobs),
                "attempt_id": member_row.attempt_id,
                "carrier": member_duty["intent"]["carrier"],
                "recipient_sid": recipient_sid, "delivery_id": member_id,
                "storage_recipient": storage_recipient, "claim_owner": claim_owner,
                "receipt_digest": stored_member.get("receipt_digest"),
                "row": member_row, "duty": member_duty, "record": stored_member})
        if not transfer_members:
            release_members()
            return
        if (recipient_harness == "claude" and len(transfer_members) == 1
                and _settle_claude_native_dispatch_receipt(
                    jobs.parent, storage_recipient, transfer_members[0]["row"],
                    recipient_sid, transfer_members[0]["record"])):
            return
        from dispatch_supervision import CONTINUED_KEY, continue_for_parent
        for index, member in enumerate(transfer_members):
            continued = continue_for_parent(member["record"], session_id=recipient_sid,
                                            recipient_kind=member["carrier"])
            if continued:
                member_context[index][CONTINUED_KEY] = continued
        message = delivery_context([(jobs.parent, member_context)])
        from dispatch_recipient_receipt import claimed_send_current
        if not all(claimed_send_current(jobs.parent, storage_recipient, member,
                claim_owner, recipient_harness, recipient_sid, jobs=jobs)
                for member in claimed_members):
            release_members()
            return
        first_member = transfer_members[0]
        producer_binding = {
            "schema": peer_message._RUNTIME_PRODUCER_SCHEMA,
            "duty_id": first_member["duty_id"], "jobs": str(jobs),
            "attempt_id": first_member["attempt_id"],
            "carrier": first_member["carrier"],
            "delivery_id": first_member["delivery_id"],
            "receipt_digest": first_member["receipt_digest"],
            "recipient": {"harness": recipient_harness, "session_id": recipient_sid},
            "route_id": str(first_member["row"].metadata.get("route_id") or ""),
            "route_node": str(first_member["row"].metadata.get("route_node") or ""),
        }
        if len(transfer_members) > 1:
            producer_binding["dispatch_notices"] = [
                {key: member[key] for key in ("duty_id", "attempt_id", "delivery_id",
                    "storage_recipient", "receipt_digest", "claim_owner")}
                for member in transfer_members]
        runtime_sender = {"harness": "runtime", "session_id": None, "name": "Hearting runtime"}
        transfer_refs = [value for member in transfer_members for value in (
            member["duty_id"], member["attempt_id"], member["delivery_id"])]
        if not peer_message._valid_runtime_producer_binding(
                producer_binding, runtime_sender, producer_binding["recipient"], transfer_refs):
            release_members()
            return
        # The normal prompt command seals the body, checks the recipient again,
        # and retains it through forms, busy turns and sender/observer exit (#446).
        prepared = []
        def bind_transfer(ref):
            bindings = [{key: member[key] for key in (
                "duty_id", "jobs", "attempt_id", "carrier", "recipient_sid",
                "delivery_id", "storage_recipient", "claim_owner", "receipt_digest")}
                for member in transfer_members]
            peer_message.bind_dispatch_notice(ref, bindings if len(bindings) > 1 else bindings[0])
            for member in transfer_members:
                member_observation = member["duty"].get("observation") or {}
                store.update(member["duty_id"], observation={**member_observation,
                    "transfer_ref": ref, "delivery_id": member["delivery_id"],
                    "storage_recipient": storage_recipient, "recipient_sid": recipient_sid,
                    "claim_owner": claim_owner, "producer_binding": producer_binding})
            prepared.append(ref)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as body:
            body.write(message)
            body.flush()
            args = build_parser().parse_args(["prompt", pane, "--body-file", body.name,
                *[item for member in transfer_members for item in (
                    "--ref", member["duty_id"], "--ref", member["attempt_id"],
                    "--ref", member["delivery_id"]) ]])
            from dispatch_supervision import notice_is_current
            from dispatch_seat_handover import effective_parent
            for member in transfer_members:
                receipt = (member["record"].get("receipt") or {})
                if receipt.get("kind") == "supervision" and receipt.get("reason") == "attempt-stalled":
                    latest_row = current_attempt_row(jobs, member["attempt_id"],
                                                     read_context=read_context)
                    if (not notice_is_current(member["record"]) or latest_row is None
                            or latest_row.status not in {"open", "running"}
                            or effective_parent(latest_row.metadata, jobs,
                                                read_context=read_context) != recipient_sid):
                        for pending_member in transfer_members:
                            pending.release_claim(jobs.parent, storage_recipient,
                                pending_member["delivery_id"], claim_owner=claim_owner)
                        return
            code = cmd_prompt(args, expected_recipient=(recipient_harness, recipient_sid),
                              on_prepared=bind_transfer, producer_binding=producer_binding)
        current = [pending.read(jobs.parent, storage_recipient, member["delivery_id"])
                   for member in transfer_members]
        if current and all(item and item.get("state") == "acked" for item in current):
            return
        if prepared or code in {0, 3}:
            for member in transfer_members:
                pending.mark_sent_ambiguous(jobs.parent, storage_recipient,
                    member["delivery_id"], claim_owner=claim_owner,
                    expected_identity=pending.received_ack_identity(
                        member["record"], storage_recipient, member["delivery_id"]))
                store.update(member["duty_id"], state="pending",
                             delivery="peer-courier", cleanup="pending")
        else:
            release_members()
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
    selected = [duty for duty in duties
                if (duty.get("intent", {}).get("kind") in {"message", "retire", "registered-batch",
                                                             "session-notice", "run-notice", "steward-check",
                                                             shared_readiness.DUTY_KIND}
                    or duty.get("id", "").startswith(("message-", "delay-")))]
    seen = {duty.get("id") for duty in selected}
    root = str(store.root.resolve(strict=False))
    for duty in store.list(states={"complete"}, kinds={"message"}, cached_terminal=True):
        intent = duty.get("intent") or {}
        key = (root, duty.get("id", ""))
        if (duty.get("id") in seen or key in _HANDOFF_ACCEPTANCE_REPLAYED
                or not intent.get("handoff_acceptance")):
            continue
        ref = intent.get("ref")
        if not isinstance(ref, str) or not ref:
            continue
        try:
            row = peer_message._read_pending(ref)
            acceptance = (row or {}).get("acceptance") or {}
            import session_tidy_runner as tidy_runner
            context, predecessor, _successor, _requester, target = \
                tidy_runner._handoff_context(row, peer=peer_message)
            retire_id = acceptance.get("retire_id")
            retire = store.get(retire_id) if isinstance(retire_id, str) else None
            retired_identity = ((retire or {}).get("intent") or {}).get("identity") or {}
            if (acceptance.get("read_and_accepted") is not True
                    or not isinstance(retire, dict)
                    or (retire.get("intent") or {}).get("kind") != "retire"
                    or retire.get("state") in {"cancelled", "failed"}
                    or (retired_identity.get("server"), retired_identity.get("pane"),
                        retired_identity.get("harness"), retired_identity.get("session_id"))
                       != (predecessor.get("server"), predecessor.get("pane"),
                           predecessor.get("harness"), predecessor.get("session_id"))
                    or (retire.get("intent") or {}).get("target") != target
                    or context.get("target") != target):
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        selected.append(duty)
        seen.add(duty.get("id"))
    return selected


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


def _reconcile_obligations(store, duties=None, *, read_context=None, retry_state=None,
                           notice_selection=None, runner_incarnation=None,
                           settlement_context=None):
    """Advance only the accepted duties owned by this runner."""
    if duties is None:
        duties = _runner_duties(store, store.list())
    retry_state = {} if retry_state is None else retry_state
    notice_kinds = {"session-notice", "run-notice"}
    notice_duties = [duty for duty in duties
                     if (duty.get("intent") or {}).get("kind") in notice_kinds]
    # Accepted delivery and receipt settlement must not wait for an unrelated
    # message, batch, retirement or role check to finish. The existing consumer
    # still owns all currentness/claim checks and its bounded recipient cursor.
    if notice_duties:
        try:
            _publish_run_states(store, notice_duties)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            pass
        try:
            _resume_session_notice_obligations(
                notice_duties, store, read_context=read_context,
                selection=notice_selection, runner_incarnation=runner_incarnation)
        except Exception as exc:
            # Class name only: a message carries paths and ids that change per pass.
            cause = "observer-unavailable:" + type(exc).__name__
            for duty in notice_duties:
                try:
                    store.update(duty["id"], observer_error=cause)
                except Exception:
                    pass
    for duty in duties:
        duty_id = duty.get("id", "")
        kind = (duty.get("intent") or {}).get("kind")
        if kind in notice_kinds:
            retry_state.pop(duty_id, None)
            continue
        fingerprint = _obligation_retry_fingerprint(duty)
        # These duties observe authority outside their own bytes: registry rows
        # for batches, and confirmed lineage/current recipients for notices.
        # Their bounded consumers must see that authority on every runner pass.
        retryable = (duty.get("state") == "unknown" and fingerprint is not None
                     and kind not in {"registered-batch", "session-notice", "run-notice"})
        if retryable:
            prior = retry_state.get(duty_id)
            if (prior is not None and prior[0] == fingerprint
                    and time.monotonic() < prior[1]):
                continue
        else:
            retry_state.pop(duty_id, None)
        try:
            if kind == "message" or duty.get("id", "").startswith(("message-", "delay-")):
                _resume_message_obligation(duty, store, read_context=read_context)
            elif kind == "retire":
                with peer_obligations.legacy_runner_lock(store) as acquired:
                    if acquired:
                        _resume_retire_obligation(
                            duty, store, read_context=read_context,
                            persist_followup=True, runner_lock_held=True)
            elif kind == "registered-batch":
                _resume_registered_obligation(
                    duty, store, read_context=read_context,
                    settlement_context=settlement_context)
            elif kind == "steward-check":
                _steward_check_module().advance(duty, store)
        except Exception as exc:
            try:
                store.update(duty["id"],
                             observer_error="observer-unavailable:" + type(exc).__name__)
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


def _resume_session_notice_obligations(duties, store, *, read_context=None,
                                       selection=None, runner_incarnation=None):
    try:
        return _resume_session_notice_pass(duties, store, read_context=read_context,
            selection=selection, runner_incarnation=runner_incarnation)
    finally:
        # Source completion may have recovered from an earlier failed write.
        # Queue its decision after carrier and source/selected locks are gone.
        import human_wait_state
        human_wait_state.flush_message_decisions()


def _resume_session_notice_pass(duties, store, *, read_context=None,
                               selection=None, runner_incarnation=None):
    """Deliver notices through one verified attachment and the existing peer courier."""
    import dispatch_pending_delivery as pending
    from dispatch_notice_state import keep_claim
    from dispatch_seat_handover import record_recipient_binding
    from dispatch_session_sweep import STORAGE_KEY, IDENTITY_KEY, delivery_context
    from dispatch_recipient_receipt import claimed_send_current, release_unsubmitted_claim
    from peer_read_context import PeerReadContext
    import session_notice

    root = store.root.parent.parent.resolve()
    jobs = root / "jobs.log"
    candidates = []
    read_context = read_context or PeerReadContext(registry_append_only=True)
    with read_context.pinned_lineage():
        for duty in duties:
            intent = duty.get("intent") or {}
            identity = intent.get("identity") or {}
            source = peer_obligations._notice_duty_source(duty, root)
            if source is None:
                continue
            source_harness, storage_sid, delivery_id, source_jobs = source
            if not source_harness or not storage_sid or not delivery_id:
                continue
            try:
                record = pending.read(root, storage_sid, delivery_id)
                if (not record or not peer_obligations._notice_duty_matches(
                        duty, record, root, storage_sid, delivery_id, source_jobs)):
                    continue
                if record.get("state") in {"acked", "rejected", "expired"}:
                    if record.get("state") == "acked" and "answer_binding" in (record.get("receipt") or {}):
                        ref = (duty.get("observation") or {}).get("transfer_ref")
                        message = peer_message._read_pending(ref, roots=[root], state_root=root) if ref else None
                        if message and message.get("state") == "received":
                            peer_obligations.acknowledge_session_notice_delivery(message, roots=[root])
                        continue
                    store.update(duty["id"], state="complete", delivery=record["state"], cleanup="complete",
                                 expected_phases={(duty.get("observation") or {}).get("phase", "waiting")})
                    continue
                if record.get("state") not in pending.OPEN_STATES:
                    continue
                session_notice.validate_pending_record(record)
                pair = record_recipient_binding(record, storage_sid, harness=source_harness,
                                                jobs=source_jobs, read_context=read_context)
                if pair is None:
                    ref = (duty.get("observation") or {}).get("transfer_ref")
                    if ref:
                        message = peer_message._read_pending(ref, roots=[root], state_root=root)
                        if message and message.get("state") == "received":
                            peer_obligations.acknowledge_session_notice_delivery(message, roots=[root])
                            continue
                    store.update(duty["id"], state="pending", observation={
                        **(duty.get("observation") or {}), "reason": "recipient-lineage-unavailable"},
                        expected_phases={(duty.get("observation") or {}).get("phase", "waiting")},
                        skip_unchanged=True)
                    continue
                harness, recipient_sid = pair
                import session_resume
                resume_notice = session_resume.is_resume(record)
                if resume_notice:
                    try:
                        session_resume.current(record)
                    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                        store.update(duty["id"], state="pending", observation={
                            **(duty.get("observation") or {}), "reason": "resume-source-pending"},
                            skip_unchanged=True)
                        continue
                if (intent.get("kind") == "run-notice" and pair == ("opencode", storage_sid)
                        and not resume_notice
                        and "answer_binding" not in (record.get("receipt") or {})):
                    store.update(duty["id"], state="pending", observation={
                        **(duty.get("observation") or {}), "reason": "native-opencode-carrier-owns-notice"},
                        expected_phases={(duty.get("observation") or {}).get("phase", "waiting")},
                        skip_unchanged=True)
                    continue
                candidates.append((duty, harness, storage_sid, recipient_sid, record))
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                continue
    if not candidates:
        return
    # A pass sends one bounded body to one exact recipient. Keep only a
    # runner-local selection cursor so an open first group cannot starve later
    # recipients. It carries no delivery or authorization evidence; candidates
    # and every record/target are re-read through the normal path each pass.
    # d6: terminal (failure/completion) notices go first, both in group order
    # and inside the batch; progress notices share the batch or wait one pass.
    def _terminal_rank(item):
        try:
            return 0 if session_notice.classify(item[4].get("receipt")) == session_notice.TERMINAL else 1
        except (TypeError, ValueError):
            return 1

    import session_resume
    candidates.sort(key=lambda item: (item[1], item[3], _terminal_rank(item),
                                      session_resume.is_resume(item[4]), item[4]["delivery_id"]))
    groups = {}
    for item in candidates:
        groups.setdefault((item[1], item[3]), []).append(item)
    group_keys = sorted(groups)
    terminal_groups = {key for key in group_keys
                       if any(_terminal_rank(item) == 0 for item in groups[key])}
    state_root = str(root.resolve())
    if selection is not None:
        if (selection.get("state_root") != state_root
                or selection.get("runner_incarnation") != runner_incarnation):
            selection.clear()
            selection.update(state_root=state_root,
                              runner_incarnation=runner_incarnation,
                              last_group=None)
        last_group = selection.get("last_group")
        if not (isinstance(last_group, tuple) and len(last_group) == 2
                and all(isinstance(value, str) for value in last_group)):
            last_group = None
    else:
        last_group = None

    def _pick(keys):
        return next((key for key in keys if last_group is not None and key > last_group), keys[0])

    terminal_keys = sorted(key for key in group_keys if key in terminal_groups)
    progress_keys = sorted(key for key in group_keys if key not in terminal_groups)
    group_key = _pick(terminal_keys) if terminal_keys else _pick(progress_keys)
    if selection is not None:
        selection["last_group"] = group_key
    harness, recipient_sid = group_key
    batch = groups[group_key][:NOTICE_BATCH_LIMIT]
    # Resume needs idle/empty; ordinary notices keep their existing steering
    # policy. An obsolete resume must not hold a useful completion behind it.
    resume_only = session_resume.is_resume(batch[0][4])
    batch = [item for item in batch if session_resume.is_resume(item[4]) == resume_only]
    pending_transfers = []
    retargeted = {}
    folded = []
    for item in batch:
        obs = item[0].get("observation") or {}
        ref = obs.get("transfer_ref")
        if ref:
            message = peer_message._read_pending(ref, roots=[root], state_root=root)
            if message and message.get("state") == "received":
                matched = peer_obligations.acknowledge_session_notice_delivery(message, roots=[root])
                if not matched:
                    store.update(item[0]["id"], state="unknown", observation={**obs,
                        "reason": "received-notice-history-unbound"},
                        expected_phases={obs.get("phase", "waiting")}, skip_unchanged=True)
                return
            # Accepted and ambiguous transfers are never redirected or resent.
            if message and message.get("state") in {"queued", "unverified"}:
                return
            if message and message.get("state") == "pending":
                target = message.get("to") or {}
                if ((target.get("harness"), target.get("session_id")) == (harness, recipient_sid)
                        and peer_obligations._current_session_notice_bindings(message)):
                    pending_transfers.append(message)
                else:
                    # An exact unsent transfer for an earlier holder stays
                    # immutable. Its original identity can recover at today's
                    # holder after the existing same-owner lease expires.
                    originals = list(peer_obligations._session_notice_delivery_bindings(message, roots=[root]))
                    if (not originals or len(originals) != len(
                            (message.get("producer_binding") or {}).get("records") or [None])):
                        return
                    for _store, _duty, record, _root, storage, delivery_id in originals:
                        retargeted[(storage, delivery_id)] = (ref, record)
            elif not message:
                # An unavailable prepared transfer is not proof of no send.
                return
    old_server = _HERDR_SESSION
    _server = os.environ.get("AGENT_HERDR_SESSION") or "default"
    globals()["_HERDR_SESSION"] = None if _server == "default" else _server
    claimed, prepared = [], []
    submitted = False
    try:
        import session_shell
        attachment, _attachment_reason = session_shell.exact_binding(harness, recipient_sid)
        # Resume keeps its existing stricter native-start policy. Ordinary
        # notices (including question answers) use the same exact shell path as
        # peer messages; no fabricated pane, second queue or alternate resend.
        if resume_only:
            attachment = None
        if attachment is not None:
            target = harness + ":" + recipient_sid
        else:
            result = subprocess.run(_herdr_argv("agent", "list"), capture_output=True,
                                    text=True, timeout=5)
            if result.returncode != 0:
                raise OSError("notice-census-unavailable")
            census = json.loads(result.stdout)
            observed = census.get("result") if isinstance(census, dict) else None
            agents = observed.get("agents") if isinstance(observed, dict) else None
            if (not isinstance(agents, list) or any(not isinstance(agent, dict)
                    or (agent.get("agent_session") is not None
                        and not isinstance(agent["agent_session"], dict)) for agent in agents)):
                raise ValueError("notice-census-invalid")
            matches = [agent for agent in agents if agent.get("agent") == harness
                       and (agent.get("agent_session") or {}).get("value") == recipient_sid]
            if len(matches) != 1:
                for item in batch:
                    store.update(item[0]["id"], observer_error="recipient-unavailable",
                                 observation={**(item[0].get("observation") or {}),
                                              "recipient_sid": recipient_sid, "reason": "recipient-unavailable"})
                return
            target = matches[0].get("pane_id")
            if not isinstance(target, str) or not target or target == "-":
                for item in batch:
                    store.update(item[0]["id"], observer_error="recipient-unavailable",
                                 observation={**(item[0].get("observation") or {}),
                                              "recipient_sid": recipient_sid, "reason": "recipient-unavailable"})
                return
            if _resolve_target(target)[:2] != (harness, recipient_sid):
                return

        def recipient_current():
            if attachment is not None:
                current, _reason = session_shell.exact_binding(harness, recipient_sid)
                return current == attachment
            return _resolve_target(target)[:2] == (harness, recipient_sid)

        import session_resume
        if any(session_resume.is_resume(item[4]) for item in batch):
            refusal, _proof = _native_idle_prompt_guard(target, harness, recipient_sid, target)
            if refusal:
                for item in batch:
                    store.update(item[0]["id"], state="pending", observation={
                        **(item[0].get("observation") or {}), "reason": "resume-" + refusal},
                        skip_unchanged=True)
                return
        if pending_transfers:
            for message in pending_transfers:
                _schedule_message_obligation(message, target, None if attachment is not None else target)
            return
        producer_records = []
        refs = []
        # A claim is an effect boundary: the group observes current lineage once,
        # on its own context, never the read-only loop's pin. The transport check
        # below still reads fresh authority for every member.
        effect_context = PeerReadContext()
        with effect_context.pinned_lineage():
            for item_duty, item_harness, storage_sid, item_sid, original in batch:
                source_jobs = peer_obligations._notice_duty_source(item_duty, root)[3]
                fresh = pending.read(root, storage_sid, original["delivery_id"])
                if (not fresh or fresh.get("receipt_digest") != original.get("receipt_digest")
                        or record_recipient_binding(fresh, storage_sid, harness=item_harness,
                                                    jobs=source_jobs, read_context=effect_context)
                        != (harness, item_sid)
                        or not recipient_current()):
                    return
                owner = "retained-rewake:" + item_duty["id"]
                prior = retargeted.get((storage_sid, fresh["delivery_id"]))
                try:
                    with peer_message.pending_lock(prior[0]) if prior else contextlib.nullcontext():
                        if prior:
                            transfer = peer_message._read_pending(prior[0], roots=[root], state_root=root)
                            if transfer and transfer.get("state") == "received":
                                peer_obligations.acknowledge_session_notice_delivery(transfer, roots=[root])
                                return
                            if not transfer or transfer.get("state") != "pending":
                                return
                            originals = list(peer_obligations._session_notice_delivery_bindings(transfer, roots=[root]))
                            if len(originals) != len(transfer["producer_binding"].get("records") or [None]):
                                return
                            fresh = pending.read(root, storage_sid, original["delivery_id"])
                            if (not fresh or (fresh.get("claim_owner") != owner
                                    and not (fresh.get("state") == "pending" and fresh.get("claim_owner") is None))
                                    or pending.received_ack_identity(fresh, storage_sid, fresh["delivery_id"])
                                       != prior[1][IDENTITY_KEY]):
                                return
                        if pending.read_submission_fence(root, storage_sid, fresh["delivery_id"],
                                expected_identity=pending.submission_fence_identity(fresh, item_sid)) is not None:
                            return
                        # Only a verified still-pending transfer explains a legacy
                        # sent-ambiguous preparation; native ambiguity stays held.
                        if fresh.get("state") == "claimed" or prior and fresh.get("state") == "sent-ambiguous":
                            deadline = fresh.get("claim_deadline_ns")
                            if type(deadline) is not int or time.monotonic_ns() < deadline:
                                return
                            fresh = pending.reclaim(root, storage_sid, fresh["delivery_id"],
                                                    now_ns=time.monotonic_ns())
                        claim = pending.claim(root, storage_sid, fresh["delivery_id"], claim_owner=owner,
                            lease_seconds=30, require_generation_proof=False, jobs=source_jobs,
                            retry_context=f"{harness}:{recipient_sid}:{target}:{item_duty['id']}")
                        original_identity = (prior[1][IDENTITY_KEY] if prior else
                            pending.received_ack_identity(original, storage_sid, original["delivery_id"]))
                        if pending.received_ack_identity(claim, storage_sid, fresh["delivery_id"]) != original_identity:
                            release_unsubmitted_claim(root, storage_sid,
                                {**original, IDENTITY_KEY: original_identity}, owner, item_sid)
                            return
                    if not keep_claim(root, storage_sid, fresh["delivery_id"], claim, owner, jobs=source_jobs):
                        # d7: a run notice whose exact parent completion is already
                        # recorded is folded, not sent alone. It stays rejected (its
                        # replacement is that completion receipt); one summary line
                        # rides the body of the notices this pass does send.
                        # A rejected notice that cannot fold has no replacement, but
                        # it must not abandon members already claimed in this batch:
                        # keep them moving and leave this one rejected.
                        summary = session_notice.fold_summary(fresh)
                        if summary is None:
                            continue
                        folded.append(summary)
                        continue
                    stored = dict(claim)
                    identity = pending.received_ack_identity(stored, storage_sid, fresh["delivery_id"])
                    view = {**claim, STORAGE_KEY: storage_sid, IDENTITY_KEY: identity}
                    claimed.append((item_duty, storage_sid, fresh["delivery_id"], owner, stored, view))
                    producer_records.append({"duty_id": item_duty["id"], "storage_sid": storage_sid,
                        "delivery_id": fresh["delivery_id"], "receipt_digest": fresh["receipt_digest"]})
                    refs.extend((item_duty["id"], fresh["delivery_id"]))
                except (OSError, ValueError, RuntimeError):
                    return
        if not claimed:
            return
        body = delivery_context([(root, [entry[5] for entry in claimed])])
        if folded:
            body += ("\n\nfolded run notices (parent completion already received):\n"
                     + "\n".join(f"- {line}" for line in folded))
        producer = {"schema": peer_message._SESSION_NOTICE_PRODUCER_SCHEMA,
            "duty_id": claimed[0][0]["id"], "jobs": str(jobs.resolve()),
            "records": producer_records,
            "recipient": {"harness": harness, "session_id": recipient_sid}}
        sender = {"harness": "runtime", "session_id": None, "name": "Hearting runtime"}
        if not peer_message._valid_session_notice_producer_binding(producer, sender,
                producer["recipient"], refs):
            return
        def bind_transfer(ref):
            prepared.append(ref)
            for item_duty, storage_sid, delivery_id, _owner, _stored, _view in claimed:
                previous = item_duty.get("observation") or {}
                bindings = {**(previous.get("transfer_bindings") or {}), ref: producer}
                store.update(item_duty["id"], observation={**previous, "transfer_ref": ref,
                    "transfer_bindings": bindings,
                    "storage_sid": storage_sid, "delivery_id": delivery_id,
                    "recipient_sid": recipient_sid, "producer_binding": producer},
                    expected_phases={previous.get("phase", "waiting")})
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as body_file:
            body_file.write(body)
            body_file.flush()
            ref_args = [arg for ref in dict.fromkeys(refs) for arg in ("--ref", ref)]
            args = build_parser().parse_args(["prompt", target, "--body-file", body_file.name,
                                               *ref_args])
            if any(not recipient_current() for _entry in claimed):
                return
            for _duty, storage_sid, delivery_id, owner, stored, _view in claimed:
                source_jobs = peer_obligations._notice_duty_source(_duty, root)[3]
                if not claimed_send_current(root, storage_sid, stored, owner, harness, recipient_sid,
                                            jobs=source_jobs):
                    return
            if any(session_resume.is_resume(entry[4]) for entry in claimed):
                refusal, _proof = _native_idle_prompt_guard(target, harness, recipient_sid, target)
                if refusal:
                    return
                # As for idle tidy, re-read input authority after the slow
                # physical observation, not just before it. The courier also
                # rechecks this at its existing first-write boundary.
                try:
                    if any(not session_resume.current(entry[4], for_submission=True)
                           for entry in claimed if session_resume.is_resume(entry[4])):
                        return
                except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                    return
            code = cmd_prompt(args, expected_recipient=(harness, recipient_sid),
                              on_prepared=bind_transfer, producer_binding=producer)
            submitted = code in {0, 3}
    finally:
        globals()["_HERDR_SESSION"] = old_server
        for item_duty, storage_sid, delivery_id, owner, stored, _view in claimed:
            try:
                current = pending.read(root, storage_sid, delivery_id)
                if current and current.get("state") == "acked":
                    continue
                if prepared or submitted:
                    pending.mark_sent_ambiguous(root, storage_sid, delivery_id, claim_owner=owner,
                        expected_identity=pending.received_ack_identity(stored, storage_sid, delivery_id))
                    store.update(item_duty["id"], state="pending", delivery="peer-courier", cleanup="pending",
                        expected_phases={(item_duty.get("observation") or {}).get("phase", "waiting")})
                else:
                    release_unsubmitted_claim(root, storage_sid, stored, owner, recipient_sid)
            except (OSError, ValueError, RuntimeError):
                pass  # The original lease and prepared transfer remain recoverable.


def _component_proven_ended(component, census, server, process_cache=None):
    """Absence in a complete census and no live pane still on a member session."""
    if census is None or census.complete is not True:
        return False
    for harness, session_id in component:
        if _census_session_presence(census, session_id, server) is not False:
            return False
    harnesses = {harness for harness, _sid in component}
    for pane in census.panes:
        harness = pane.get("agent")
        if harness not in harnesses:
            continue
        pane_id = pane.get("pane_id")
        if not isinstance(pane_id, str) or not pane_id:
            return False
        cache_key = (harness, pane_id)
        if process_cache is not None and cache_key in process_cache:
            observed = process_cache[cache_key]
        else:
            observed = _process_session(pane_id, harness)  # pane id string, never the dict
            if process_cache is not None:
                process_cache[cache_key] = observed
        if observed is None:
            return False  # unknown never authorizes retirement
        if (harness, observed) in component:
            return False
    return True


def _retire_finished_lineage_bundles(store, *, now=None, deadline_ns=None):
    """Archive a finished lineage bundle after all five conditions hold."""
    import dispatch_recipient_inbox as inbox
    import session_tidy as tidy
    from dispatch_seat_handover import lineage_graph

    now = time.time() if now is None else float(now)
    root = store.root.parent.parent.resolve()
    jobs = root / "jobs.log"
    result = {"components": 0, "retired": 0, "ledgers": 0, "skipped": 0}
    try:
        graph = lineage_graph()
    except (OSError, ValueError, TypeError):
        return {**result, "skipped": 1}
    components = _lineage_components(graph)
    if not components:
        # No lineage bundle can be retired; do not ask herdr for a census.
        return result
    try:
        census = herdr_observation.pane_census(session=_HERDR_SESSION)
    except (OSError, ValueError, TypeError, RuntimeError):
        census = None
    server = herdr_observation.endpoint_name(_HERDR_SESSION)
    sessions = tidy.state_root() / "sessions"
    archive = tidy.state_root() / RETIRE_ARCHIVE_DIRECTORY
    process_cache = {}
    for component in components:
        if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
            result["skipped"] += 1
            break
        result["components"] += 1
        if any(vertex in graph.unresolved for vertex in component):
            result["skipped"] += 1
            continue
        ledgers = sorted({key for vertex in component
                          for key in graph.ledger_keys.get(vertex, ())})
        if not ledgers:
            result["skipped"] += 1
            continue
        if any(_sid_has_open_notice(root, sid) for _harness, sid in component):
            result["skipped"] += 1
            continue
        if _component_has_open_binding(graph, component, jobs):
            result["skipped"] += 1
            continue
        if _component_has_role_rows(graph, component, store):
            result["skipped"] += 1
            continue
        last = _component_last_activity(graph, component)
        if last is None or now - last < RETIRE_ACTIVITY_SECONDS:
            result["skipped"] += 1
            continue
        if not _component_proven_ended(component, census, server, process_cache):
            result["skipped"] += 1
            continue
        try:
            archive.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            result["skipped"] += 1
            continue
        moved = 0
        for ledger_key in ledgers:
            source = sessions / (ledger_key + ".jsonl")
            if not source.is_file() or source.is_symlink():
                continue
            destination = archive / source.name
            placeholder = None
            if os.path.lexists(destination):
                # Never overwrite an archived ledger for a re-used key: keep
                # the earlier retirement under its own name and give this one
                # a unique sibling.
                try:
                    fd, unique = tempfile.mkstemp(prefix=f"{source.stem}.retired-",
                                                  suffix=".jsonl", dir=archive)
                except OSError:
                    continue
                os.close(fd)
                destination = Path(unique)
                placeholder = destination
            try:
                os.replace(source, destination)
            except OSError:
                if placeholder is not None:
                    with contextlib.suppress(OSError):
                        os.unlink(placeholder)
                continue
            # The source keeps its last-write mtime; retention counts from the
            # retirement, so stamp the moved ledger explicitly.
            with contextlib.suppress(OSError):
                os.utime(destination, None)
            moved += 1
        if moved:
            result["retired"] += 1
            result["ledgers"] += moved
            try:
                from dispatch_seat_handover import publish_lineage_rows
                publish_lineage_rows(inbox.dispatch_inbox_root())
            except (OSError, ValueError, TypeError):
                pass
            try:
                inbox.bump_edge_generation(inbox.dispatch_inbox_root())
            except (OSError, ValueError, TypeError):
                pass
        else:
            result["skipped"] += 1
    return result


def _runner_maintenance_pass(store):
    """Publish derived observations and run the bounded global cleanup duties."""
    import dispatch_recipient_inbox as inbox

    identity = store._state_root_identity
    if not store._state_root_identity_is_current():
        # The root that carries every duty below was removed or replaced (a
        # finished test's temporary home). Stop writing; the runner loop's
        # next lock check exits.
        return {}
    root = store.root.parent.parent.resolve()
    deadline_ns = time.monotonic_ns() + int(MAINTENANCE_TIME_BUDGET_S * 1_000_000_000)
    result = {}
    # The Stop header reads this file; publish before the cleanup duties so a
    # long reconcile backlog cannot starve it for hours.
    try:
        result["steward_relations"] = _publish_steward_relations(
            root, expected_root_identity=identity)
    except (OSError, ValueError, TypeError, RuntimeError):
        pass
    try:
        result["lineage_rows"] = _publish_lineage_rows_duty(
            root, deadline_ns=deadline_ns, expected_root_identity=identity)
    except (OSError, ValueError, TypeError, RuntimeError):
        pass
    # The peer history prune and the recipient index are what every prompt
    # and Stop hook reads; they run before the registry reconcile, each with
    # its own share of the budget, so a reconcile backlog cannot keep them
    # from ever running (2026-11-03: neither had run once and the pending
    # history held 4,138 received rows past the 7-day window).
    try:
        state = peer_obligations.read_runner_duty_state(root, "peer-history")
        if (peer_obligations.runner_duty_due(state, RECONCILE_DUE_SECONDS)
                and time.monotonic_ns() < deadline_ns):
            cursor = state.get("cursor")
            counts = _prune_received_peer_history(
                deadline_ns=min(deadline_ns, time.monotonic_ns()
                                + int(PEER_HISTORY_TIME_BUDGET_S * 1_000_000_000)),
                start_after=cursor if isinstance(cursor, str) else None)
            result["peer_history"] = counts
            peer_obligations.write_runner_duty_state(
                root, "peer-history", {
                    "last_pass_ns": time.time_ns() if counts.get("complete", True) else 0,
                    "cursor": counts.get("cursor"),
                    "removed": counts["removed"],
                    "locks_removed": counts.get("locks_removed", 0)},
                expected_root_identity=identity)
    except (OSError, ValueError, RuntimeError):
        pass
    try:
        result["peer_index"] = _publish_peer_indexes(
            root, deadline_ns=deadline_ns, expected_root_identity=identity)
    except (OSError, ValueError, TypeError, RuntimeError):
        pass
    try:
        state = peer_obligations.read_runner_duty_state(root, "reconcile")
        if (peer_obligations.runner_duty_due(state, RECONCILE_DUE_SECONDS)
                and time.monotonic_ns() < deadline_ns):
            import dispatch_completion_join as join
            jobs = root / "jobs.log"
            remaining = max(0.0, (deadline_ns - time.monotonic_ns()) / 1_000_000_000)
            cursor = state.get("cursor")
            registry_cursor = state.get("registry_cursor")
            # At most half of what is left, so the duties after it (retire,
            # inbox prefill) also run while a reconcile cycle spans many passes.
            counts = join.reconcile_pending_delivery(
                jobs, max_rows=RECONCILE_MAX_ROWS,
                time_budget_s=min(RECONCILE_TIME_BUDGET_S, remaining / 2),
                start_after=cursor if isinstance(cursor, str) else None,
                registry_start=(registry_cursor if isinstance(registry_cursor, int)
                                and not isinstance(registry_cursor, bool) else None))
            result["reconcile"] = counts
            complete = (not counts.get("budget_exhausted") and counts.get("cursor") is None
                        and counts.get("registry_cursor") is None)
            peer_obligations.write_runner_duty_state(
                root, "reconcile", {
                    # An unfinished walk is due on the next pass, not in six hours,
                    # and resumes where it stopped: the first 2,575-record backlog
                    # then clears within hours.
                    "last_pass_ns": time.time_ns() if complete else 0,
                    "cursor": counts.get("cursor"),
                    "registry_cursor": counts.get("registry_cursor"),
                    "rows_read": counts.get("rows_read", 0),
                    "records_read": counts.get("records_read", 0),
                    "pruned_records": counts.get("pruned_records", 0),
                    "expired": counts.get("expired", 0),
                    "budget_exhausted": counts.get("budget_exhausted", 0)},
                expected_root_identity=identity)
    except (OSError, ValueError, RuntimeError):
        pass
    try:
        state = peer_obligations.read_runner_duty_state(root, "retire")
        if (peer_obligations.runner_duty_due(state, MAINTENANCE_DUE_SECONDS)
                and time.monotonic_ns() < deadline_ns):
            counts = _retire_finished_lineage_bundles(store, deadline_ns=deadline_ns)
            counts.update(_purge_retired_archives())
            result["retire"] = counts
            peer_obligations.write_runner_duty_state(
                root, "retire", {
                    "last_pass_ns": time.time_ns(), "retired": counts.get("retired", 0),
                    "ledgers": counts.get("ledgers", 0),
                    "purged": counts.get("removed", 0)},
                expected_root_identity=identity)
            if counts.get("retired"):
                # A retirement bumps the edge generation; republish in this
                # same pass rather than waiting for the next due check.
                try:
                    result["steward_relations"] = _publish_steward_relations(
                        root, expected_root_identity=identity)
                except (OSError, ValueError, TypeError, RuntimeError):
                    pass
    except (OSError, ValueError, RuntimeError):
        pass
    try:
        state = peer_obligations.read_runner_duty_state(root, "inbox-prefill")
        generation = inbox.read_edge_generation(root)
        # A fresh install has no confirmed edge writer yet (generation 0), but
        # a live OpenCode/carrier session still needs its first derived inbox;
        # the hooks of a session that never prompts own nothing. The first pass
        # publishes it and every later edge change or due window refreshes it.
        due = (state.get("edge_generation") != generation
               or peer_obligations.runner_duty_due(state, INBOX_PREFILL_DUE_SECONDS))
        if due and time.monotonic_ns() < deadline_ns:
            counts = _prefill_live_inboxes(
                root, deadline_ns=deadline_ns, expected_root_identity=identity)
            result["inbox"] = counts
            peer_obligations.write_runner_duty_state(
                root, "inbox-prefill", {
                    "last_pass_ns": time.time_ns(), "edge_generation": generation,
                    "written": counts["written"], "skipped": counts["skipped"]},
                expected_root_identity=identity)
    except (OSError, ValueError, RuntimeError):
        pass
    try:
        # The post-exit retention passes (worker homes, attempt logs, reusable runtimes,
        # the registry archive) otherwise run only when a dispatch's reap watch ends; with
        # nothing dispatched they never would. Each keeps its own due gate; they may take
        # longer than this pass's budget, so they run in their own short-lived process.
        state = peer_obligations.read_runner_duty_state(root, "retention")
        jobs = root / "jobs.log"
        from dispatch_contract import stable_state_root
        canonical = root == stable_state_root(os.environ).resolve()   # never a fixture's own root
        if (canonical and jobs.is_file()
                and peer_obligations.runner_duty_due(state, MAINTENANCE_DUE_SECONDS)):
            # Recorded first: a start that fails is not retried before the next due window.
            peer_obligations.write_runner_duty_state(
                root, "retention", {"last_pass_ns": time.time_ns()},
                expected_root_identity=identity)
            child = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve().parent / "dispatch_registry_archive.py"),
                 "retention", "--jobs", str(jobs)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True, cwd=str(root))
            # It forks the passes off and exits at once, so this wait is short and leaves no zombie.
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=10)
            result["retention"] = {"started": child.pid}
    except (OSError, ValueError, RuntimeError):
        pass
    return result


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

    notice_selection = {"state_root": str(store.root.parents[1].resolve()),
                        "runner_incarnation": runner_incarnation, "last_group": None}
    settlement_context = dispatch_batch_obligations.OwnerSettlementReadContext()
    retired_for_exec = []

    def retire_for_service_exec():
        # A service replacement is about to exec this process. Join the
        # readiness worker first so no old-code thread survives the process
        # image, and release the settlement watcher with its context.
        retired_for_exec.append(True)
        stop_readiness_worker()
        settlement_context.close()

    duty_retry_state = {}
    terminal_handoff_scan = True
    terminal_handoff_duties = []
    recover_steward_roles = True
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
        from peer_read_context import PeerReadContext
        from runtime_observation import ObservationContext
        # A retained notice already has an active marker. Give that accepted
        # work its own read pass before any cold/legacy discovery or role scan;
        # a fast-only snapshot must never suppress the later full recovery read.
        store.begin_read_pass()
        try:
            accepted_notices = store.list(
                kinds={"session-notice", "run-notice"}, fast_active_only=True)
            if accepted_notices:
                _reconcile_obligations(
                    store, accepted_notices,
                    read_context=PeerReadContext(registry_append_only=True),
                    retry_state=duty_retry_state, notice_selection=notice_selection,
                    runner_incarnation=runner_incarnation)
        finally:
            store.end_read_pass()
        accepted_notice_ids = {duty["id"] for duty in accepted_notices}
        if recover_steward_roles:
            try:
                _steward_check_module().reconcile_all(store)
            except Exception:
                pass
            recover_steward_roles = False
        store.begin_read_pass()
        read_context = PeerReadContext(registry_append_only=True)
        notice_discovery_observed = _discover_session_notice_duties(store, read_context=read_context)
        duties = [duty for duty in store.list() if duty["id"] not in accepted_notice_ids]
        if (terminal_handoff_scan
                and any((duty.get("intent") or {}).get("kind") == "retire"
                         for duty in duties)):
            terminal_handoff_duties = _settled_handoff_acceptance_duties(store)
        terminal_handoff_scan = False
        for duty in terminal_handoff_duties:
            replay_key = (str(store.root.resolve(strict=False)), duty.get("id", ""))
            if replay_key not in _HANDOFF_ACCEPTANCE_REPLAYED:
                duties.append(duty)
        duties = _runner_duties(store, duties)
        observation_context = ObservationContext()
        _reconcile_obligations(store, duties, read_context=read_context,
                               retry_state=duty_retry_state,
                               notice_selection=notice_selection,
                               runner_incarnation=runner_incarnation,
                               settlement_context=settlement_context)
        if service_current:
            retired_for_exec.clear()
            peer_obligations.exec_service_runner(
                args.lock_fd, store.root.parents[1], service_current,
                herdr_session=_HERDR_SESSION, before_exec=retire_for_service_exec,
            )
            if retired_for_exec:
                # The exec failed after the old context retired: continue on a
                # fresh context under the same lock.
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
        # Global cleanup and derived-observation publishing run on every pass
        # after its delivery work. Each duty keeps its own due check and the
        # shared maintenance budget, so a quiet pass costs only a small state
        # read per duty. A failure here never stops delivery work.
        try:
            _runner_maintenance_pass(store)
        except (OSError, ValueError, RuntimeError, peer_obligations.ObligationError):
            pass
        current_duties = store.list()
        external_signature = _external_obligation_signature(external_snapshot)
        if (notice_discovery_observed and not _runner_duties(store, current_duties)
                and not any(external_signature)):
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
        # Unknown discovery is an observation, not proof of an empty backlog.
        # Keep the same owner and bounded wait even before legacy duty recovery.
        signature = (notice_discovery_observed,
                     _runner_duty_signature(current_duties), external_signature)
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
            # This pane/session was just rechecked above; the flush uses the same
            # start gate as the runner so an interrupted thread can resume.
            _flush_pending_for_target(pane, harness, sid, state, expected_pane=pane)
        if booked:
            # Booking/card/new-user-input checks still own this ordinary retry.
            clear._continue(seat.key, req["nonce"], clear._run_continue)


def _submit_pending_native(pending, target, sid, pane, state, *, harness, no_trailer=False):
    transfer_ref = pending.get("ref")
    if not transfer_ref:
        return "peer-ref-unavailable", "unverified", "private-pending", "peer-ref-unavailable"
    resolved_target, t_sid, target_pane, state_before = target, sid, pane, state
    from session_tidy_clear import peer_input_state
    state_before, _source = _effective_retire_state(state_before,
        {"harness": harness, "session_id": t_sid, "pane": target_pane})
    state_before = peer_input_state(harness, t_sid, target_pane, state_before)
    outcome, verify, reason = "queued", "native-callback", None
    direct_reason = None
    if no_trailer:
        direct_reason = "no-trailer-callback-only"
    elif pending["state"] != "pending":
        direct_reason = "peer-delivery-already-submitted"
    elif state_before not in ("idle", "done"):
        direct_reason = "native-state-" + str(state_before)
    elif not target_pane or not peer_message.usable_session_id(t_sid):
        direct_reason = "target-identity-unverified"
    else:
        direct_reason, proof = _native_idle_prompt_guard(
            resolved_target, harness, t_sid, target_pane)
        if direct_reason is None:
            try:
                claimed = peer_message.reserve_pending_herdr(
                    transfer_ref, {"harness": harness, "session_id": t_sid,
                                   "pane_id": target_pane},
                    receipt=f"{harness}-pty-prompt-inflight")
            except (OSError, ValueError):
                claimed = None
            if claimed is None:
                direct_reason = "peer-delivery-already-claimed"
            else:
                direct_reason, _proof = _native_idle_prompt_guard(
                    target_pane, harness, t_sid, target_pane, expected_foreground=proof)
                if direct_reason:
                    peer_message.release_unsent_herdr_claim(
                        claimed, receipt=direct_reason)
                    claimed = None
                else:
                    reserved = claimed
                    claimed = peer_message.begin_pending_write(
                        claimed, recipient={"harness": harness, "session_id": t_sid,
                                            "pane_id": target_pane},
                        receipt=f"{harness}-pty-prompt-inflight")
                    if claimed is None:
                        peer_message.release_pending_herdr_claim(reserved)
                        direct_reason = "peer-delivery-already-claimed"
                    else:
                        rc, payload = _herdr_prompt(
                            target_pane, pending["text"], wait=False, start_aware=True,
                            timeout_ms=_PROMPT_VERIFY_TIMEOUT_MS)
                        if peer_message.write_never_started(payload, rc):
                            # exec failed: nothing reached the pane, so the same
                            # row may be retried by its existing carrier.
                            peer_message.release_unstarted_pending_write(
                                claimed, receipt="herdr-not-started")
                            direct_reason = "herdr-not-started"
                        elif rc != 0:
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
    return direct_reason, outcome, verify, reason



def _cmd_prompt_shell(args, body, binding, *, from_identity, from_name,
                      expected_recipient=None, on_prepared=None, producer_binding=None, handoff=None):
    """Native-session address using the existing sealed message and carrier."""
    import session_shell
    harness, sid = binding["harness"], binding["sid"]
    if expected_recipient is not None and (harness, sid) != expected_recipient:
        print("prompted=false reason=target-identity-changed")
        return 5
    sender_sid, sender_harness, _project = from_identity
    try:
        text, ref = peer_message.prepare_peer_message(body,
            {"harness": sender_harness, "session_id": sender_sid, "name": from_name},
            {"harness": harness, "session_id": sid, "name": args.target},
            defer=True, refs=args.ref, receipt="native-pending", with_trailer=not args.no_trailer,
            producer_binding=producer_binding, handoff=handoff,
            original_target=args.target, server=_HERDR_SESSION or "default")
        if on_prepared is not None:
            on_prepared(ref)
        row = peer_message._read_pending(ref)
        if row.get("state") == "received":
            result = {"ok": True, "state": "received"}
        elif row.get("state") != "pending" or args.no_trailer:
            result = {"ok": False, "reason": "awaiting-native-receiver"}
        elif harness == "codex":
            # Same exact native queue; shell display grants no start permission.
            result = peer_message.deliver_pending_codex(ref, start_interrupted=False)
        else:
            result = session_shell.deliver_pending(ref, harness, sid)
        current = peer_message._read_pending(ref) or row
    except (OSError, ValueError):
        print("prompted=unverified reason=peer-pending-unavailable")
        return 5
    outcome = "true" if current.get("state") == "received" else (
        "queued" if current.get("state") in {"pending", "queued"} else "unverified")
    reason = result.get("reason") or "awaiting-exact-receiver-ref"
    if producer_binding is None and outcome != "true":
        _schedule_message_obligation(current, f"{harness}:{sid}")
    _record(to_harness=harness, to_name=args.target, kind=_prompt_message_kind(body),
            summary_text=text, to_session_id=sid, to_pane=None, ref=args.ref,
            status=_PROMPT_LEDGER_STATUS[outcome], receipt="session-shell:" + reason,
            from_identity=from_identity, from_name=from_name, transfer_ref=ref,
            surface="codex-queue" if harness == "codex" else harness + "-native",
            producer_binding=producer_binding)
    print(f"prompted={outcome} target={args.target} to_harness={harness} ref={ref} reason={reason}")
    _prompt_carrier_note(ref, args.target, outcome)
    return _PROMPT_EXIT[outcome]

def cmd_prompt(args, *, expected_recipient=None, on_prepared=None, producer_binding=None,
               handoff=None, resolved_recipient=None, sender=None):
    """Seal a peer message and route it through the recipient's native carrier.

    ``sender`` is the observed predecessor identity for a supervisor-issued
    handoff: it supplies the sealed sender and the pane binding instead of
    ``_current_session_identity``/``verified_pane``. It is internal to the
    typed handoff path and is never a CLI flag.

    Codex steers ordinary input into an active turn (`turn/steer`) and
    otherwise uses its exact thread queue; an interrupted thread is started
    only after this sender sees an empty composer. Claude's hook context and
    OpenCode's native idle callback consume their own pending rows; the
    obligation runner continues a still-pending Claude or OpenCode row through
    the same guarded idle input. Queue acceptance or context emission remains
    queued; only an exact native receive is true.
    """
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
    args.target = _tmux_target_address(args.target)

    body = body.rstrip("\n")
    if producer_binding is not None and sender is not None:
        raise ValueError("producer-binding-with-sender")
    if producer_binding is not None:
        if args.no_trailer:
            print("prompted=false reason=runtime-completion-requires-receipt-envelope")
            return 5
        from_sid, from_harness, from_name = None, "runtime", "Hearting runtime"
        from_identity = (None, "runtime", "")
    elif sender is not None:
        from_sid, from_harness = sender.get("session_id"), sender.get("harness")
        from_identity = (from_sid, from_harness, _project_of(sender.get("cwd") or os.getcwd()))
        from_name = _from_name(from_harness, from_sid)
    else:
        from_sid, from_harness = _current_session_identity()
        from_identity = (from_sid, from_harness, _project_of(os.getcwd()))
        from_name = _from_name(from_harness, from_sid)
    relay_pane = (sender or {}).get("pane") or ""
    shell_binding = None
    # Full native addresses are explicit. Existing window/name resolution keeps
    # precedence otherwise, and an uncertain send never switches transports.
    if (str(args.target).split(":", 1)[0] in {"claude", "codex", "opencode"}
            or _surface_missing(args.target) and _tmux_inventory() is None):
        import session_shell
        shell_binding, _shell_reason = session_shell.resolve_target(args.target)
    if shell_binding is not None:
        return _cmd_prompt_shell(args, body, shell_binding, from_identity=from_identity,
            from_name=from_name, expected_recipient=expected_recipient, on_prepared=on_prepared,
            producer_binding=producer_binding, handoff=handoff)
    if _surface_missing(args.target) and _tmux_inventory() is None:
        resolved, resolve_error, candidate_panes = None, "herdr-not-found", []
    elif resolved_recipient is None:
        resolved, resolve_error, candidate_panes = _resolve_prompt_target(args.target)
    else:
        resolved, resolve_error, candidate_panes = resolved_recipient, None, [resolved_recipient.get("pane")]
    if resolved is None:
        reason = resolve_error or "target-observation-unavailable"
        _prompt_failure_ledger(args.target, reason, candidate_panes,
                               kind=_prompt_message_kind(body), ref=args.ref,
                               from_identity=from_identity, from_name=from_name)
        try:
            _text, ref = peer_message.prepare_peer_message(
                body, {"harness": from_harness, "session_id": from_sid, "name": from_name},
                {"harness": "unknown", "session_id": None, "name": args.target},
                defer=True, refs=args.ref, receipt=reason, with_trailer=not args.no_trailer,
                producer_binding=producer_binding, handoff=handoff,
                original_target=args.target, server=_HERDR_SESSION or "default")
            if on_prepared is not None:
                on_prepared(ref)
            row = peer_message._read_pending(ref)
            if producer_binding is None:
                _schedule_message_obligation(row, args.target)
            print(f"prompted=unverified reason={reason} ref={ref}")
            _prompt_carrier_note(ref, args.target, "unverified")
        except (OSError, ValueError):
            print("prompted=unverified reason=peer-pending-unavailable")
        return 5
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
    sender_row = {"harness": from_harness, "session_id": from_sid, "name": from_name}
    if sender is not None:
        # The supervisor's shell pane is not the sender; the observed predecessor
        # pane is what the typed context and the retire endpoint require.
        if relay_pane:
            sender_row["pane_id"] = relay_pane
    elif from_sid and from_harness in {"claude", "codex", "opencode"}:
        try:
            sender_pane = _verified_caller_pane(from_harness, from_sid)
            if sender_pane:
                sender_row["pane_id"] = sender_pane
        except Exception:
            pass
    try:
        text, transfer_ref = peer_message.prepare_peer_message(
            body, sender_row,
            recipient, defer=True, refs=args.ref, receipt=input_reason or "native-pending",
            with_trailer=not args.no_trailer, reference_evidence=reference_evidence,
            first_input_process=first_input_process, producer_binding=producer_binding, handoff=handoff,
            original_target=args.target, server=_HERDR_SESSION or "default")
        if on_prepared is not None:
            on_prepared(transfer_ref)
        pending = peer_message._read_pending(transfer_ref)
    except (OSError, ValueError):
        print("prompted=unverified reason=peer-pending-or-transfer-unavailable")
        return 5

    pinned = peer_message.pending_delivery_recipient(pending)
    if pinned is not None and not peer_message._same_transfer_session(pinned, recipient):
        # The same retained request may outlive its original name association.
        # Keep its exact recipient and controller; a current namesake is no send
        # authority for that message, including when it was already submitted.
        if producer_binding is None:
            _schedule_message_obligation(pending, pending.get("original_target") or args.target,
                                         pinned.get("pane_id"))
        print(f"prompted=unverified reason=target-identity-changed ref={transfer_ref}")
        _prompt_carrier_note(transfer_ref, pending.get("original_target") or args.target, "unverified")
        return 5

    kind = _prompt_message_kind(body)
    started = time.monotonic()
    verify = "private-pending"
    reason = None
    surface = {"claude": "claude-native", "codex": "codex-queue",
               "opencode": "opencode-native"}.get(t_harness, "herdr")

    if pending["state"] == "received":
        outcome, verify = "true", pending.get("receipt") or "exact-native-history"
    elif input_reason and not (t_harness == "codex" and peer_message.usable_session_id(t_sid)):
        if pending["state"] == "pending":
            _schedule_message_obligation(pending, resolved_target, target_pane)
        outcome = "queued" if pending["state"] in {"pending", "queued"} else "unverified"
        if t_harness == "claude" and state_before == "working":
            verify = "native-callback"
        reason = input_reason
    elif t_harness == "codex" and peer_message.usable_session_id(t_sid):
        if resolved.get("_native_session_id"):
            current, current_error = _prompt_current_candidate(
                resolved_target, expected_harness=t_harness, expected_sid=t_sid,
                require_process_session=True)
        else:
            current, current_error = _prompt_exact_candidate(
                resolved_target, expected_harness=t_harness, expected_sid=t_sid)
        if current is None:
            outcome, reason = "unverified", current_error or "target-identity-unverified"
            _schedule_message_obligation(pending, resolved_target, target_pane)
        else:
            try:
                gate = _codex_interrupt_start_gate(target_pane or resolved_target)
                result = _deliver_codex_pending(transfer_ref, target_pane, timeout=None,
                                                start_interrupted=gate)
                outcome = "true" if result["status"] == "received" else (
                    "queued" if result["status"] == "queued" else "unverified")
                reason = result.get("reason")
                verify = ("native-first-input-submitted" if reason == "native-first-input-submitted"
                          else ("native-steer-" if reason == "native-steer-accepted"
                                else "native-queue-") + result["status"])
                if outcome == "unverified" and str(reason).startswith("queue-rpc-refused:"):
                    # A refused RPC sent nothing and the pane first prompt could
                    # not submit either (busy or unreadable composer): the carrier
                    # still holds the row and retries it, so the receipt says queued.
                    held = peer_message._read_pending(transfer_ref) or {}
                    if held.get("state") in {"pending", "queued"} and not held.get("rpc_claim"):
                        outcome, verify = "queued", "native-queue-held"
            except (OSError, ValueError) as exc:
                outcome, reason = "unverified", "peer-pending-unavailable:" + str(exc)
            if outcome != "true" and producer_binding is None:
                # Runtime producers already have their bound retained controller.
                _schedule_message_obligation(pending, resolved_target, target_pane)
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
                            target_pane, pending["text"], wait=False, start_aware=True,
                            timeout_ms=_PROMPT_VERIFY_TIMEOUT_MS)
                        if peer_message.write_never_started(payload, rc):
                            peer_message.release_unstarted_pending_write(
                                claimed, receipt="herdr-not-started")
                            direct_reason = "herdr-not-started"
                        elif rc != 0:
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
    elif t_harness in {"claude", "opencode"}:
        direct_reason, outcome, verify, reason = _submit_pending_native(
            pending, resolved_target, t_sid, target_pane, state_before,
            harness=t_harness, no_trailer=args.no_trailer)
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
    _prompt_carrier_note(transfer_ref, args.target, outcome)
    return _PROMPT_EXIT[outcome]


def _read_screen(target):
    """The visible pane (ANSI) as screen lines, or None when it could not be read."""
    if terminal_surface.is_tmux_pane(target):
        tmux = _tmux_surface(target)
        text = tmux.read_visible(target, ansi=True) if tmux else None
        return _screen_lines(text) if text and text.strip() else None
    try:
        proc = subprocess.run(_herdr_argv("agent", "read", target, "--source", "visible", "--format", "ansi"),
                              capture_output=True, text=True, errors="replace", timeout=_herdr_get_timeout())
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    return _screen_lines(proc.stdout)


_CODEX_UNMATERIALIZED = "not materialized yet"
_CODEX_FIRST_PROMPT_POLL_SEC = 4.0
_CODEX_FIRST_ENTER_RETRIES = 3
_CODEX_ENTER_GAP_SEC = 2.0


def _codex_composer_holds_text(pane):
    """True when the pane's Codex input box is readable and still holds typed text;
    False when it is empty or a turn took it; None when the screen cannot be read."""
    lines = _read_screen(pane)
    if not lines:
        return None
    return _screen_ready("codex", lines) == "draft"


def _codex_first_prompt_submitted(pane, sid):
    """The first prompt was taken: the receiving Codex's composer emptied (its own screen,
    valid for any CODEX_HOME), or a rollout for the thread exists in this steward's home."""
    return _codex_composer_holds_text(pane) is False or _codex_rollout_exists(sid)


def _codex_wait_first_submitted(pane, sid, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _codex_first_prompt_submitted(pane, sid):
            return True
        time.sleep(0.25)
    return _codex_first_prompt_submitted(pane, sid)


def _codex_settle(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.1)


def _codex_first_prompt(ref, pane, sid):
    """Submit one pending row through the pane for a Codex thread with no first turn yet.

    The native queue cannot reach a thread until its first turn materializes it, so
    that first message goes in as pane input through the host's own prompt call (herdr
    `agent prompt`; tmux bracketed paste plus a separate Enter), not as raw typed text
    followed by Enter, which Codex can take as a paste burst and keep as a newline. The
    submission signal is the composer emptying; Enter is repeated only while the
    composer still holds the text. Returns ``True`` once submitted, else ``None``.
    """
    if _codex_rollout_exists(sid):
        return None
    with peer_message.pending_lock(ref):
        row = peer_message._read_pending(ref)
        if not row or row.get("state") != "pending" or row.get("rpc_claim"):
            return None
        reason, _proof = _native_idle_prompt_guard(pane, "codex", sid, pane, fresh_ok=True)
        if reason:
            return None
        from dispatch_contract import process_start_ticks
        claim = {"token": secrets.token_hex(16), "pid": os.getpid(),
                 "start": process_start_ticks(os.getpid())}
        peer_message._save_pending(dict(row, state="unverified", rpc_claim=claim,
                                        receipt="native-first-input-inflight"))
    submitted = False
    try:
        code, payload = _herdr_prompt(pane, row["text"], wait=False, timeout_ms=5000)
        typed = code == 0 and not (isinstance(payload, dict) and payload.get("error"))
        for enters in range(_CODEX_FIRST_ENTER_RETRIES + 1 if typed else 0):
            submitted = _codex_wait_first_submitted(pane, sid, _CODEX_FIRST_PROMPT_POLL_SEC)
            # An unreadable screen proves nothing: never press Enter blind.
            if (submitted or enters >= _CODEX_FIRST_ENTER_RETRIES
                    or _codex_composer_holds_text(pane) is not True):
                break
            _codex_settle(_CODEX_ENTER_GAP_SEC)
            if not _pane_send(pane, "send-keys", "Enter"):
                break
    except (OSError, subprocess.SubprocessError):
        pass
    with peer_message.pending_lock(ref):
        current = peer_message._read_pending(ref)
        if current and (current.get("rpc_claim") or {}).get("token") == claim["token"]:
            peer_message._save_pending(
                dict(current, state="queued", rpc_claim=None,
                     receipt="native-first-input-submitted") if submitted else
                dict(current, state="unverified", rpc_claim=None,
                     receipt="native-first-input-unverified"))
    return True if submitted else None


def _deliver_codex_pending(ref, pane, *, timeout, start_interrupted):
    """``deliver_pending_codex``, plus the pane first prompt for an unmaterialized thread.

    The oldest pending row of that recipient goes first, so one submission
    materializes the thread and every later row (this one included) keeps using
    the native queue. A submitted row is ``queued`` and never delivered again.
    """
    deliver = functools.partial(peer_message.deliver_pending_codex,
                                start_interrupted=start_interrupted,
                                **({} if timeout is None else {"timeout": timeout}))
    result = deliver(ref)
    if (result.get("status") != "unverified" or not pane
            or _CODEX_UNMATERIALIZED not in str(result.get("reason") or "")):
        return result
    row = peer_message._read_pending(ref) or {}
    to = row.get("to") or {}
    sid = to.get("session_id")
    if row.get("state") != "pending" or to.get("harness") != "codex" or not sid:
        return result
    older = [item for item in peer_message._pending_rows()
             if item.get("state") == "pending" and not item.get("rpc_claim")
             and (item.get("to") or {}).get("session_id") == sid]
    first = min(older, key=lambda item: (item.get("created") or 0, item["ref"]),
                default=row)["ref"]
    if _codex_first_prompt(first, pane, sid) is None:
        return result
    if first == ref:
        return {"status": "queued", "reason": "native-first-input-submitted"}
    return deliver(ref)


def _codex_interrupt_start_gate(target):
    """Sender-side screen evidence for starting an interrupted Codex thread.

    True only for a readable screen with no form and an empty input box; else the
    observed reason (`draft`, `form`, `screen-unknown`).
    """
    lines = _read_screen(target) if target else None
    if not lines:
        return "screen-unknown"
    reason = _native_trust_reason("codex", lines) or _screen_ready("codex", lines)
    if reason is None:
        return True
    return {"form-open": "form", "native-trust-wait": "form", "draft": "draft"}.get(
        reason, "screen-unknown")


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
        if terminal_surface.is_tmux_pane(pane):
            processes = (_retire_pane_info(pane) or {}).get("foreground_processes") or []
        else:
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
    refusal = _tmux_input_refusal(target)
    if refusal:
        return refusal, agent, None
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
    if _surface_missing(args.target):
        print("cleared=failed reason=" + ("tmux-not-found" if terminal_surface.is_tmux_pane(args.target)
                                          else "herdr-not-found"))
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
    lines = _lines

    def ready(current):
        nonlocal agent, lines
        reason, agent, lines = _clear_look(target, current)
        return reason

    # The final look and compact claim share the existing booking lock with
    # peer admission. No native I/O or receipt waits hold that lock.
    req, why = clear.claim_clear(args.request, args.nonce, ready=ready)
    if req is None:
        return finish(_look_outcome(why), why, agent)
    compacting = (req.get("clear_claim") or {}).get("command") == "compact"
    if compacting:
        command = clear.COMPACT_COMMAND     # background agents keep running in the same session
    before = _codex_footer_threads(lines) if harness == "codex" else set()
    req, why = clear.recheck_idle_input(args.request, req)
    if req is None:
        return finish("skipped", why, agent)
    rc, payload = _herdr_prompt(target, command, wait=False, timeout_ms=_PROMPT_VERIFY_TIMEOUT_MS)
    if rc is None:
        return finish("failed", "herdr-invocation-failed", agent)
    if rc != 0:
        return finish("failed", _herdr_error_reason(payload, rc), agent)
    if compacting:
        # The session id stays; the helper waits (bounded) for the compaction evidence.
        if clear.compact_confirmed(_read_json(Path(args.request)) or {}):
            new_session = old_sid
            return finish("true", None, agent)
        return finish("unverified", "compact-pending", agent)
    new_session = _clear_observe(target, req, args.request, before)
    if new_session is None:
        return finish("unverified", "new-session-not-observed", agent)
    return finish("true", None, agent)


def cmd_idle_card_request(args):
    """Prepare an idle card off-screen; retain old compact-origin prompt transport."""
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
            if req and req.get("origin") != "idle":
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
    if _surface_missing(args.target):
        return finish("failed", "tmux-not-found" if terminal_surface.is_tmux_pane(args.target)
                      else "herdr-not-found")
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
        queued = reason in {"draft", "draft-unknown", "form-open", "screen-unknown", *_TMUX_INPUT_WAIT} \
            or reason.startswith("not-idle-")
        return finish("queued" if queued else _look_outcome(reason), reason)
    req, why = clear.validate_idle_request(path, args.nonce)
    if req is None:
        return finish("skipped", why)
    reason, agent, _lines = _clear_look(args.target, req)  # immediately before the one claimed send
    if not reason and not exact_session(agent):
        reason = "target-changed"
    if reason:
        queued = reason in {"draft", "draft-unknown", "form-open", "screen-unknown", *_TMUX_INPUT_WAIT} \
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
        queued = reason in {"draft", "draft-unknown", "form-open", "screen-unknown", *_TMUX_INPUT_WAIT} \
            or reason.startswith("not-idle-")
        return finish("queued" if queued else _look_outcome(reason), reason)
    checked, why = clear.validate_idle_request(path, args.nonce, _sending_claim=claim.get("idle_request"))
    if checked is None:
        clear.release_unsent_idle_request(claim)
        return finish("skipped", why)
    if (claim.get("idle_request") or {}).get("source") == "memory-helper":
        import session_tidy_idle_card
        accepted, why = session_tidy_idle_card.enqueue(claim)
        return finish("true" if accepted else "skipped", why)
    text = clear.idle_card_request_text(claim)
    checked, why = clear.recheck_idle_input(path, checked)
    if checked is None:
        clear.release_unsent_idle_request(claim)
        return finish("skipped", why)
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
    if _surface_missing(args.target):
        print("continued=failed reason=" + ("tmux-not-found" if terminal_surface.is_tmux_pane(args.target)
                                            else "herdr-not-found"))
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
    reason, proof = _native_idle_prompt_guard(target, "claude", sid, pane)
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
    reason, _proof = _native_idle_prompt_guard(
        pane, "claude", sid, pane, expected_foreground=proof)
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
        result = peer_message.grant_steward(harness, sid, {"harness": "unknown", "name": "-"},
                                            "explicit", ts, source="explicit")
        if result["status"] == "limited":
            print(peer_message.format_steward_limited(result))
            return 1
        ok = result["written"]
        _ensure_steward_runner(result)
        print(f"steward={'on' if ok else 'unchanged'} harness={harness} session_id={sid}")
        return 0 if ok else 1
    if args.state == "check":
        try:
            check_module = _steward_check_module()
        except ImportError:
            print("steward-check=unavailable reason=module-missing")
            return 1
        return check_module.cmd_check(harness, sid)
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
    p_mode.add_argument("state", choices=("on", "off", "check"))
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
    args.steward_server = herdr_observation.endpoint_name(os.environ.get("HERDR_SESSION"))
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
