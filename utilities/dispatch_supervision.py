"""Shared wait ownership and durable, non-terminal parent handback.

The canonical registry owns outcomes. This controller schedules bounded recovery
and keeps waiting; its timeout never authorizes killing a worker or retrying it.
The existing pending-delivery queue owns a notice until a carrier accepts it.
"""
from __future__ import annotations

import hashlib
import glob
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import time
from typing import Callable, Iterable

from dispatch_attempt_policy import decide_attempt, terminal_conflict_digest
from dispatch_receipt_identity import receipt_digest
from dispatch_registry_cache import registry_view
import dispatch_pending_delivery as pending_delivery

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

KIND = "supervision"
REASONS = frozenset({"owner-input-undelivered", "no-progress", "attempt-stalled", "process-unverifiable", "join-deadline", "supervisor-exited", "join-observer-failed", "terminal-evidence-conflict", "workflow-completion-pending", "closure-blocked", "watch-deadline", "receiver-unavailable", "answer-awaiting-parent", "parent-session-ended"})
# Another session answered a BLOCKED owner; only the route's parent launches the continuation.
ANSWER_AWAITING_PARENT = "answer-awaiting-parent"


class SupervisionError(ValueError):
    pass


class StopWaiting(RuntimeError):
    """Abort a managed wait without recording a delivery notice or row change."""


def scoped_file_signature(metadata, worktree=""):
    """Bounded signature of declared write-scope paths; never reads file prose."""
    entries = []
    roots = []
    for raw_root in (worktree, metadata.get("artifact_root", "")):
        root = Path(raw_root)
        if root.is_absolute() and root not in roots:
            roots.append(root)
    declared = metadata.get("write_scope", "").split(";")
    route_file = Path(metadata.get("route_file", ""))
    if route_file.is_absolute() and route_file.is_file():
        cycle_root = route_file.parent.parent if route_file.parent.name == "_internal" else route_file.parent
        if cycle_root not in roots:
            roots.append(cycle_root)
        try:
            route = json.loads(route_file.read_text(encoding="utf-8"))
            node = next((item for item in route.get("nodes", [])
                         if item.get("id") == metadata.get("route_node")), {})
            declared.extend(str(item) for item in node.get("outputs", []))
        except (OSError, TypeError, ValueError):
            pass
    for raw in declared:
        raw = raw.strip()
        if not raw:
            continue
        relative = not Path(raw).is_absolute()
        if relative and ".." in Path(raw).parts:
            continue
        patterns = [raw] if not relative else [str(root / raw) for root in roots]
        for pattern in patterns:
            candidates = glob.iglob(pattern, recursive=True) if glob.has_magic(pattern) else iter((pattern,))
            for candidate in candidates:
                item = Path(candidate)
                nested = item.rglob("*") if item.is_dir() and not glob.has_magic(pattern) else (item,)
                for item in nested:
                    if not item.is_file():
                        continue
                    if relative:
                        try:
                            resolved = item.resolve(strict=False)
                            if not any(_inside_root(resolved, root) for root in roots):
                                continue
                        except OSError:
                            continue
                    try:
                        stat = item.stat()
                    except OSError:
                        continue
                    entries.append((str(item), stat.st_size, stat.st_mtime_ns))
                    if len(entries) >= 256:
                        break
                if len(entries) >= 256:
                    break
            if len(entries) >= 256:
                break
        if len(entries) >= 256:
            break
    if not entries:
        return ""
    return hashlib.sha256(json.dumps(sorted(entries), separators=(",", ":")).encode()).hexdigest()


def _inside_root(path, root):
    try:
        path.relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _tool_event_source(metadata, log_path):
    """The file that carries the owner's tool rows.

    A supervised Claude owner's attempt log is a receipt log with control rows
    only; it announces the child session whose own transcript has the tool
    rows. The summary follower already resolves that transcript the same way.
    """
    try:
        from dispatch_summary import announced_session, session_transcript
        decided, announced = announced_session(log_path)
        if (not decided or not isinstance(announced, dict) or announced.get("invalid")
                or announced.get("runtime")):
            return log_path
        runtime_home = metadata.get("runtime_home") or None
        return session_transcript(announced, runtime_home=runtime_home) or log_path
    except (ImportError, OSError, ValueError, TypeError):
        return log_path


def runtime_tool_progress(metadata, previous=None):
    """Read only the latest bounded native tool identity, never transcript prose."""
    latest = previous or None
    raw_path = metadata.get("log_file", "")
    if not raw_path or not Path(raw_path).is_absolute():
        return latest
    try:
        with _tool_event_source(metadata, Path(raw_path)).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            start = max(0, handle.tell() - 262144)
            handle.seek(start)
            if start:
                handle.readline()
            lines = handle.read(262144).splitlines()
    except OSError:
        return latest
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        kind, item = event.get("type"), event.get("item")
        if kind in {"item.started", "item.completed"} and isinstance(item, dict):
            if item.get("type") in {"command_execution", "file_change", "mcp_tool_call", "web_search"} and item.get("id"):
                latest = ["codex", str(item["id"])[:256], kind]
        if kind in {"assistant", "user"} and isinstance(event.get("message"), dict):
            content = event["message"].get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                identity = block.get("id") if block_type == "tool_use" else block.get("tool_use_id")
                if block_type in {"tool_use", "tool_result"} and identity:
                    latest = ["claude", str(identity)[:256], block_type]
        part = event.get("part")
        if kind == "tool_use" and isinstance(part, dict) and part.get("type") == "tool":
            state = part.get("state")
            status = state.get("status") if isinstance(state, dict) else None
            identity = part.get("callID") or part.get("id")
            if identity and status in {"pending", "running", "completed", "error"}:
                latest = ["opencode", str(identity)[:256], status]
    return latest


def verification_process_digest(pid):
    """Hash the exact live argv without trusting the lease's copied command."""
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes().rstrip(b"\0")
    except OSError:
        return ""
    if not raw:
        return ""
    return hashlib.sha256(raw).hexdigest()


def verification_lease(jobs, attempt_id, route_id, route_node, now):
    """Return one exact live runtime verification lease, else ``None``."""
    from dispatch_contract import dispatch_state_root, process_start_ticks, process_state
    path = (dispatch_state_root(jobs) / "verification-leases"
            / f"{attempt_id.replace('/', '_')}.json")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    digest, pid, pgid, deadline = (value.get("command_digest"), value.get("pid"),
                                   value.get("pgid"), value.get("deadline"))
    if (value.get("schema_version") != 1 or value.get("attempt_id") != attempt_id
            or value.get("route_id") != route_id or value.get("route_node") != route_node
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(pid, int) or not isinstance(pgid, int) or pid <= 0 or pgid != pid
            or not isinstance(deadline, (int, float)) or deadline < now
            or process_start_ticks(pid) != str(value.get("pid_start") or "")
            or process_state(pid) == "Z" or verification_process_digest(pid) != digest):
        return None
    try:
        if os.getpgid(pid) != pgid:
            return None
    except OSError:
        return None
    return value


def _observation_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _observation_read(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _observation_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _attempt_human_wait(metadata, jobs):
    route_file = metadata.get("owner_route_file") or metadata.get("route_file") or ""
    if not route_file:
        return None
    path = Path(route_file)
    if not path.is_absolute() or path.is_symlink():
        return None
    try:
        route = json.loads(path.read_text(encoding="utf-8"))
        expected = metadata.get("owner_route_id") or metadata.get("route_id")
        expected_hash = metadata.get("owner_route_hash") or metadata.get("route_hash")
        if (route.get("route_id") != expected
                or (expected_hash and route.get("route_hash") != expected_hash)):
            return None
        from human_gate_observation import pending_human_waits
        return bool(pending_human_waits(route, jobs=jobs))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def _safe_receipt_text(value, limit=2048):
    if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 for char in value):
        return ""
    return value


def _route_needs_attention(metadata, jobs, now):
    """Read a recent needs-attention receipt from its exact effective parent."""
    try:
        route_file = metadata.get("owner_route_file") or metadata.get("route_file") or ""
        route_path = Path(route_file)
        expected_route_id = metadata.get("owner_route_id") or metadata.get("route_id")
        expected_route_hash = metadata.get("owner_route_hash") or metadata.get("route_hash")
        if (not route_path.is_absolute() or route_path.is_symlink() or not route_path.is_file()
                or not expected_route_id or not expected_route_hash):
            return None
        route = json.loads(route_path.read_text(encoding="utf-8"))
        from route_identity import route_hash
        if (route.get("route_id") != expected_route_id
                or route.get("route_hash") != expected_route_hash
                or route_hash(route) != expected_route_hash):
            return None
        route_node = metadata.get("route_node")
        if (route_node and route_node != "_owner"
                and not any(item.get("id") == route_node
                            for item in route.get("nodes", []) if isinstance(item, dict))):
            return None
        from dispatch_seat_handover import effective_parent, effective_parent_harness
        parent = effective_parent(metadata, jobs)
        harness = effective_parent_harness(metadata, jobs)
        if not parent or harness not in {"claude", "codex", "opencode"}:
            return None
        key = hashlib.sha256(f"{harness}\0{parent}".encode()).hexdigest()
        path = Path(jobs).resolve(strict=False).parent / "start-receipts" / f"{key}.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4_000_000:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if (value.get("schema") != "start_receipt_v1"
                or value.get("harness") != harness or value.get("session_id") != parent):
            return None
        saved_at = value.get("saved_at")
        if type(saved_at) not in {int, float} or not math.isfinite(saved_at) or not 0 <= now - saved_at <= 3600:
            return None
        receipt = value.get("receipt")
        if (not isinstance(receipt, dict) or receipt.get("state") != "needs-attention"
                or receipt.get("route_id") != expected_route_id
                or (receipt.get("route_hash") and receipt["route_hash"] != expected_route_hash)):
            return None
        bound_attempt = receipt.get("owner_attempt_id") or receipt.get("attempt_id")
        allowed_attempts = {metadata.get("attempt_id"), metadata.get("parent_attempt_id"),
                            metadata.get("owner_attempt_id")}
        allowed_attempts.discard(None)
        if bound_attempt and bound_attempt not in allowed_attempts:
            return None
        reason = _safe_receipt_text(receipt.get("reason"), 128)
        action = _safe_receipt_text(receipt.get("required_action"), 128)
        if not reason:
            return None
        command_kind = next((key.removesuffix("_command")
                             for key in ("recovery_command", "resume_command", "parent_next_command")
                             if _safe_receipt_text(receipt.get(key), 2048)), "")
        command = _safe_receipt_text(receipt.get(command_kind + "_command"), 2048) if command_kind else ""
        return {"reason": reason, "required_action": action,
                "command": command, "command_kind": command_kind,
                "next_step": _safe_receipt_text(receipt.get("next_step"), 512),
                "gate": _safe_receipt_text(receipt.get("gate"), 128),
                "parent_harness": harness, "parent_session_id": parent}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError):
        return None


def _runtime_tool_active(event):
    if not isinstance(event, list) or len(event) != 3:
        return False
    harness, _identity, state = event
    return ((harness == "codex" and state == "item.started")
            or (harness == "claude" and state == "tool_use")
            or (harness == "opencode" and state in {"pending", "running"}))


def update_attempt_stall_observation(jobs, attempt_id, *, now=None,
                                     progress_window_seconds=300.0,
                                     max_quiet_windows=2, persist=True, worktree=None,
                                     row=None):
    """Update one advisory observation without invoking watchdog terminal policy."""
    from dispatch_contract import (
        attempt_execution_observation, dispatch_state_root,
    )
    from tools.fleet.model import ATTEMPT_CLASSIFIER_SOURCE, deterministic_progress_fingerprint
    jobs = Path(jobs).resolve(strict=True)
    now = time.time() if now is None else float(now)
    if row is None:
        rows = _rows(jobs, {attempt_id})
        row = rows.get(attempt_id)
    if not row:
        return None
    status, metadata = row
    if status not in {"open", "running"}:
        return None
    route_id = metadata.get("owner_route_id") or metadata.get("route_id") or ""
    route_node = metadata.get("route_node") or ("_owner" if metadata.get("worker_type") == "owner" else "")
    if not route_id or not route_node:
        return {"state": "unknown", "reason": "route-binding-unavailable",
                "attempt_id": attempt_id, "route_id": route_id, "route_node": route_node}
    state_root = dispatch_state_root(jobs)
    name = attempt_id.replace("/", "_")
    heartbeat_path = state_root / "heartbeats" / f"{name}.json"
    watchdog_path = state_root / "watchdog" / f"{name}.json"
    lock_path = state_root / "watchdog" / f"{name}.lock"
    hb = _observation_read(heartbeat_path)
    fd = _observation_lock(lock_path) if persist else None
    try:
        state = _observation_read(watchdog_path)
        tool_event = runtime_tool_progress(metadata, state.get("runtime_tool_event"))
        lease = verification_lease(jobs, attempt_id, route_id, route_node, now)
        fingerprint = deterministic_progress_fingerprint({
            "attempt_id": attempt_id, "route_id": route_id, "route_node": route_node,
            "heartbeat": hb or None, "tool": tool_event,
            "registry_transition": {"status": status, "note": metadata.get("note", "")},
            "file_signature": scoped_file_signature(
                metadata, worktree if worktree is not None else metadata.get("_stall_worktree", "")),
            "artifact_signature": metadata.get("artifact_sha256") or None,
            "background_verification": ({
                "command_digest": lease.get("command_digest"), "pid": lease.get("pid"),
                "pid_start": lease.get("pid_start"), "deadline": lease.get("deadline"),
            } if lease else None),
        })
        same_binding = (state.get("schema_version") == 1
                        and state.get("attempt_id") == attempt_id
                        and state.get("route_id") == route_id
                        and state.get("route_node") == route_node)
        try:
            last_progress = float(state.get("last_progress_at")) if same_binding else now
            last_window = float(state.get("last_window_at", last_progress)) if same_binding else now
            quiet = max(0, int(state.get("quiet_windows", 0))) if same_binding else 0
        except (TypeError, ValueError, OverflowError):
            last_progress, last_window, quiet = now, now, 0
            same_binding = False
        if not same_binding:
            try:
                baseline = float(hb.get("updated_at"))
                if not math.isfinite(baseline):
                    baseline = now
            except (TypeError, ValueError, OverflowError):
                baseline = now
            last_progress = last_window = baseline
            quiet = 0
        elif fingerprint != state.get("fingerprint"):
            last_progress, last_window, quiet = now, now, 0
        else:
            elapsed_windows = max(0, int((now - last_window) // max(1.0, progress_window_seconds)))
            if elapsed_windows:
                quiet += elapsed_windows
                last_window += elapsed_windows * max(1.0, progress_window_seconds)
        if lease:
            last_progress = last_window = now
            quiet = 0

        execution = attempt_execution_observation(metadata)
        human_wait = _attempt_human_wait(metadata, jobs)
        route_attention = _route_needs_attention(metadata, jobs, now)
        resource_wait = (metadata.get("worker_type") == "resource"
                         or metadata.get("resource_run_id")
                         or metadata.get("resource_id"))
        tool_active = _runtime_tool_active(tool_event)
        if human_wait is None:
            observation_state, reason = "unknown", "human-gate-observation-unverifiable"
        elif human_wait:
            observation_state, reason = "waiting", "raised-human-gate"
        elif route_attention:
            observation_state, reason = "stalled", "route-needs-attention"
        elif resource_wait:
            observation_state, reason = "waiting", "resource-watch-owns-progress"
        elif lease:
            observation_state, reason = "active", "exact-verification-lease"
        elif execution.state == "absent":
            if tool_active:
                observation_state, reason = "unknown", "runtime-tool-active-process-absent"
            else:
                from codex_dispatch_terminal import terminal_envelope_observed
                if terminal_envelope_observed(metadata.get("log_file")):
                    observation_state, reason = "stalled", "settlement-unresolved"
                else:
                    observation_state, reason = "stalled", "execution-absent"
        elif (execution.state == "unknown" and tool_active
              and any(len(identity) >= 3 and str(identity[2]).startswith("unclassified:")
                      for identity in execution.identities)):
            observation_state, reason = "active", "native-tool-process-live"
        elif execution.state == "unknown":
            observation_state, reason = "unknown", execution.reason or "execution-unverifiable"
        elif tool_active:
            observation_state, reason = "active", "native-tool-active"
        elif quiet >= max_quiet_windows:
            observation_state, reason = "stalled", "no-comparable-progress"
        else:
            observation_state, reason = "active", "within-progress-window"

        prior = state.get("stall_observation") if isinstance(state.get("stall_observation"), dict) else {}
        unknown_since = (prior.get("unknown_since") if prior.get("state") in {"observing", "unknown"}
                         and prior.get("reason") == reason else now)
        if observation_state == "unknown":
            try:
                persisted_unknown_since = float(unknown_since)
            except (TypeError, ValueError):
                persisted_unknown_since = now
            if now - persisted_unknown_since < progress_window_seconds * max_quiet_windows:
                unresolved_state = "observing"
            else:
                unresolved_state = "unknown"
        else:
            persisted_unknown_since = None
            unresolved_state = observation_state
        if unresolved_state in {"stalled", "unknown"}:
            if prior.get("state") in {"stalled", "unknown"} and prior.get("episode_id"):
                episode_id = prior["episode_id"]
                first_observed_at = prior.get("first_observed_at", now)
            else:
                episode_seed = json.dumps([attempt_id, route_id, route_node, now,
                                           last_progress, unresolved_state], separators=(",", ":"))
                episode_id = "stall-" + hashlib.sha256(episode_seed.encode()).hexdigest()[:32]
                first_observed_at = now
        else:
            episode_id = prior.get("episode_id", "")
            first_observed_at = prior.get("first_observed_at", now)
        observation = {
            "schema_version": 1, "attempt_id": attempt_id,
            "route_id": route_id, "route_node": route_node,
            "state": unresolved_state, "reason": reason,
            "execution_state": execution.state, "execution_reason": execution.reason,
            "execution_identities": [list(item) for item in execution.identities],
            "route_attention": route_attention,
            "progress_fingerprint": fingerprint,
            "last_progress_at": last_progress, "quiet_windows": quiet,
            "observed_at": now, "first_observed_at": first_observed_at,
            "unknown_since": persisted_unknown_since, "episode_id": episode_id,
        }
        state.update({"schema_version": 1, "attempt_id": attempt_id,
                      "route_id": route_id, "route_node": route_node,
                      "classifier_source": ATTEMPT_CLASSIFIER_SOURCE,
                      "fingerprint": fingerprint, "runtime_tool_event": tool_event,
                      "last_progress_at": last_progress, "last_window_at": last_window,
                      "quiet_windows": quiet, "warning": int(quiet >= 1),
                      "observed_at": now, "stall_observation": observation})
        if persist and _observation_changed(_observation_read(watchdog_path), state):
            _observation_write(watchdog_path, state)
        if unresolved_state not in {"stalled", "unknown"}:
            return observation
        return observation
    finally:
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _observation_changed(before, after):
    """Ignore observation timestamps when deciding whether durable state changed."""
    old = dict(before)
    new = dict(after)
    old.pop("observed_at", None)
    new.pop("observed_at", None)
    old_stall = dict(old.get("stall_observation") or {})
    new_stall = dict(new.get("stall_observation") or {})
    old_stall.pop("observed_at", None)
    new_stall.pop("observed_at", None)
    if old_stall:
        old["stall_observation"] = old_stall
    if new_stall:
        new["stall_observation"] = new_stall
    return old != new


def _open_attempt_rows(jobs):
    jobs = Path(jobs).resolve(strict=False)
    if not jobs.is_file():
        return []
    view = registry_view(jobs)
    result = []
    seen = set()
    for record in view.records:
        if record.status not in {"open", "running"}:
            continue
        metadata = record.completion_metadata()
        attempt_id = metadata.get("attempt_id", "")
        if (not attempt_id or attempt_id in seen
                or not metadata.get("parent_sid")
                or not metadata.get("parent_completion_delivery")
                or not (metadata.get("owner_route_id") or metadata.get("route_id"))):
            continue
        if len(record.fields) > 3:
            metadata["_stall_worktree"] = record.fields[3]
        seen.add(attempt_id)
        result.append((record.status, metadata))
    return result


def has_unowned_stall_observation(jobs):
    """Read-only runner wake predicate for unresolved open attempt observations."""
    try:
        return bool(stall_observer_signature(jobs))
    except (OSError, ValueError, RuntimeError):
        return True


def stall_observer_signature(jobs):
    """Return exact unresolved observation identities without writing state."""
    from dispatch_contract import process_table_scan_scope
    values = []
    rows = _open_attempt_rows(jobs)
    with process_table_scan_scope():
        for status, metadata in rows:
            attempt_id = metadata.get("attempt_id", "")
            observation = update_attempt_stall_observation(
                jobs, attempt_id, persist=False, row=(status, metadata))
            if observation and observation.get("state") in {"stalled", "unknown"}:
                values.append((attempt_id, observation.get("state"),
                               observation.get("episode_id", "")))
    return tuple(sorted(values))


def update_open_attempt_stall_observations(jobs):
    """Advance the existing bounded observer pass and retain exact stall notices."""
    from dispatch_contract import process_table_scan_scope
    jobs = Path(jobs).resolve(strict=True)
    records = []
    with process_table_scan_scope():
        for status, metadata in _open_attempt_rows(jobs):
            attempt_id = metadata.get("attempt_id", "")
            observation = update_attempt_stall_observation(
                jobs, attempt_id, persist=True, row=(status, metadata))
            if not observation or observation.get("state") not in {"stalled", "unknown"}:
                continue
            try:
                records.extend(materialize(jobs, {attempt_id}, reason="attempt-stalled"))
            except (OSError, ValueError, RuntimeError, SupervisionError):
                # The row and advisory observation stay open for the next normal pass.
                continue
    return records


def _attach_stall_delivery(jobs, attempt_id, record):
    """Bind the read-only Fleet projection to the exact retained delivery."""
    from dispatch_contract import dispatch_state_root
    receipt = record.get("receipt") if isinstance(record, dict) else None
    recipient = receipt.get("recipient_thread_id") if isinstance(receipt, dict) else None
    monitored = receipt.get("monitored_attempt_ids") if isinstance(receipt, dict) else None
    delivery_id = record.get("delivery_id") if isinstance(record, dict) else None
    digest = record.get("receipt_digest") if isinstance(record, dict) else None
    recipient_digest = record.get("recipient_digest") if isinstance(record, dict) else None
    if (not isinstance(receipt, dict)
            or not isinstance(monitored, list) or attempt_id not in monitored
            or not receipt.get("owner_attempt_id") or not recipient
            or delivery_id != receipt.get("pending_delivery_id")
            or digest != receipt_digest(receipt)
            or recipient_digest != pending_delivery.recipient_digest(recipient)):
        return
    name = attempt_id.replace("/", "_")
    path = dispatch_state_root(jobs) / "watchdog" / f"{name}.json"
    lock_path = path.with_suffix(".lock")
    fd = _observation_lock(lock_path)
    try:
        state = _observation_read(path)
        observation = state.get("stall_observation")
        if (not isinstance(observation, dict)
                or observation.get("attempt_id") != attempt_id):
            return
        observation.update({
            "delivery_id": delivery_id,
            "notice_digest": digest,
            "delivery_owner_attempt_id": receipt["owner_attempt_id"],
            "delivery_recipient_thread_id": recipient,
            "delivery_recipient_digest": recipient_digest,
            "delivery_recipient_kind": record.get("recipient_kind"),
            "obligation_revision": receipt.get("obligation_revision"),
        })
        state["stall_observation"] = observation
        _observation_write(path, state)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def join_process_error(returncode: int, receipt: object, parent_attempt_id: str) -> str:
    """Preserve the join protocol's typed failure, never arbitrary child output."""
    reason = "unverified-error-receipt"
    if (isinstance(receipt, dict) and receipt.get("schema_version") == 2
            and receipt.get("state") == "contract-error"
            and receipt.get("parent_attempt_id") == parent_attempt_id
            and receipt.get("children") == []):
        candidate = receipt.get("reason")
        if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,127}", candidate):
            reason = candidate
    return f"join-process-contract-failed:exit={returncode}:reason={reason}"


def _rows(jobs: Path, attempts: Iterable[str] | None = None, *,
          require_unique: bool = False) -> dict:
    view = registry_view(Path(jobs))
    if require_unique and view.has_duplicate_attempts:
        # The registry-wide duplicate refusal of an unfiltered read, kept for
        # callers that now look up only the attempts they own.
        raise SupervisionError("supervision-attempt-ambiguous")
    rows = {}
    if attempts is None:
        selected = ((record, record.completion_metadata()) for record in view.records)
    else:
        pending = list(dict.fromkeys(attempts))
        seen = set()
        selected_rows = []
        while pending:
            aid = pending.pop()
            if aid in seen:
                continue
            seen.add(aid)
            candidates = view.completion_attempt_records(aid)
            if len(candidates) > 1:
                raise SupervisionError("supervision-attempt-ambiguous")
            if not candidates:
                continue
            record = candidates[0]
            meta = record.completion_metadata()
            selected_rows.append((record, meta))
            parent = meta.get("parent_attempt_id")
            if parent not in (None, "", "-"):
                pending.append(parent)
        selected = iter(selected_rows)
    for record, meta in selected:
        aid = meta.get("attempt_id")
        if aid:
            if len(record.fields) > 3:
                meta["_stall_worktree"] = record.fields[3]
            if aid in rows:
                raise SupervisionError("supervision-attempt-ambiguous")
            rows[aid] = (record.status, meta)
    return rows


def _owned_rows(jobs: Path, attempt_id: str) -> dict:
    """Rows of one owner and its direct children, without parsing the registry."""
    view = registry_view(Path(jobs))
    ids = {attempt_id} | {meta.get("attempt_id") for meta in
                          (record.completion_metadata() for record in view.completion_children(attempt_id))
                          if meta.get("attempt_id")}
    return _rows(jobs, ids, require_unique=True)


def _root(rows: dict, attempt: str) -> str:
    seen = set()
    while attempt in rows and attempt not in seen:
        seen.add(attempt)
        meta = rows[attempt][1]
        parent = meta.get("parent_attempt_id")
        if parent in (None, "", "-"):
            return attempt
        attempt = parent
    raise SupervisionError("supervision-lineage-unresolved")


def _awaiting_answers(jobs: Path, aid: str) -> list[str]:
    """The kept answers of a BLOCKED owner whose continuation has not launched yet."""
    from dispatch_owner_input import blocked_owner_answers
    from dispatch_replacement import _replacement_in_flight, registry_rows
    answers = blocked_owner_answers(jobs, aid)
    if not answers:
        return []
    rows = registry_rows(jobs)
    if aid in rows and _replacement_in_flight(jobs, rows, rows[aid][1]):
        return []
    return [item["id"] for item in answers]


def _attempt_stall_superseded(jobs: Path, aid: str, metadata: dict) -> bool:
    """A claimed replacement or linked retry owns the route's live attempt now."""
    from dispatch_replacement import (
        _plain_retry_successor, _replacement_in_flight, registry_rows,
    )
    try:
        rows = registry_rows(jobs)
    except (OSError, UnicodeError, ValueError):
        return True
    if aid not in rows:
        return True
    if _replacement_in_flight(jobs, rows, metadata) or _plain_retry_successor(rows, aid):
        return True
    if any(candidate.get("replacement_original_attempt_id") == aid
           and fields[1] in {"open", "running", "done"}
           for fields, candidate in rows.values()):
        return True
    marker_path = metadata.get("completion_marker", "")
    route_file = metadata.get("owner_route_file") or metadata.get("route_file") or ""
    if marker_path and route_file:
        marker_file, route_path = Path(marker_path), Path(route_file)
        if (not marker_file.is_absolute() or not route_path.is_absolute()
                or marker_file.is_symlink() or route_path.is_symlink()):
            return False
        try:
            route = json.loads(route_path.read_text(encoding="utf-8"))
            node_id = metadata.get("route_node") or "_owner"
            node = next(item for item in route.get("nodes", []) if item.get("id") == node_id)
            marker = json.loads(marker_file.read_text(encoding="utf-8"))
            from dispatch_contract import completion_marker_is_current
            if (marker.get("attempt_id") == aid
                    and completion_marker_is_current(route, node, marker_file, marker, jobs=jobs)):
                return True
        except (OSError, UnicodeError, ValueError, TypeError, StopIteration):
            pass
    return False


def _settlement_decision(status: str, meta: dict):
    from dispatch_contract import observed_attempt_liveness
    from codex_dispatch_terminal import terminal_envelope_observed
    proof = observed_attempt_liveness(status, meta,
        terminal_envelope=terminal_envelope_observed(meta.get("log_file")),
        terminal_receipt_gate=True)
    decision = decide_attempt(status, meta, process_state=proof.process_state,
                              process_reason=proof.process_reason)
    return proof, decision


def _pending(rows: dict, attempts: list[str], jobs: Path, reason=None, *, notice_context=None) -> bool:
    import route_parent_close
    attempts = [aid for aid in attempts if aid not in rows or not route_parent_close.row_requested(rows[aid][1], jobs)]
    if not attempts:
        return False
    if reason == "owner-input-undelivered":
        from dispatch_owner_input import unresolved
        return any(unresolved(jobs, aid) for aid in attempts)
    if reason == ANSWER_AWAITING_PARENT:
        return any(_awaiting_answers(jobs, aid) for aid in attempts)
    if reason == "attempt-stalled":
        for aid in attempts:
            metadata = (rows.get(aid) or (None, {}))[1]
            if _attempt_stall_superseded(jobs, aid, metadata):
                continue
            status = update_attempt_stall_observation(jobs, aid, persist=False,
                                                       row=rows.get(aid))
            if status and status.get("state") in {"stalled", "unknown"}:
                return True
        return False
    if reason == "no-progress":
        from dispatch_replacement import parent_resume_obligation
        for aid in attempts:
            meta = (rows.get(aid) or (None, {}))[1]
            route_id = meta.get("owner_route_id") or meta.get("route_id")
            if parent_resume_obligation(jobs, aid, route_id=route_id) is not None:
                return True
        return False
    for aid in attempts:
        if aid not in rows:
            raise SupervisionError("supervision-attempt-missing")
        status, meta = rows[aid]
        # Route settlement can retire its completion wait while process cleanup
        # remains an independent obligation. Never let route close hide a live,
        # unknown, or conflicting execution.
        from dispatch_notice_state import route_obligation_closed
        route_closed = reason == "supervisor-exited" and route_obligation_closed(meta, jobs)
        _proof, decision = _settlement_decision(status, meta)
        if decision.action in {"wait", "recover", "reconcile", "inspect-conflict"}:
            return True
        # A supervisor-exited notice may be only a lagging route-settlement
        # signal. Once that exact route is closed, do not keep it alive solely
        # because the workflow ledger is still flushing; closure-blocked remains
        # a distinct reason and does not take this branch.
        if meta.get("workflow_completion") == "runtime-v1":
            if not route_closed:
                if notice_context is not None:
                    notice_context.owner_completion(jobs, status, meta)
                    completion = {"job_registry": str(jobs), "recipient_thread_id": meta.get("parent_sid"),
                                  "monitored_attempt_ids": attempts}
                    if _recorded_completion_result(completion, require_ack=False,
                            notice_context=notice_context) is None:
                        return True
                else:
                    from dispatch_terminal_commit import owner_completion_pending
                    if owner_completion_pending(jobs, status, meta):
                        return True
    return False


def _obligation_revision(rows: dict, attempts: list[str], reason: str, jobs=None) -> str:
    if reason == "owner-input-undelivered":
        from dispatch_owner_input import unresolved_revision
        return hashlib.sha256(json.dumps([(aid, unresolved_revision(jobs, aid))
                                          for aid in attempts]).encode()).hexdigest()
    if reason == ANSWER_AWAITING_PARENT:
        return hashlib.sha256(json.dumps([(aid, _awaiting_answers(jobs, aid))
                                          for aid in attempts]).encode()).hexdigest()
    if reason == "attempt-stalled":
        values = []
        for aid in attempts:
            observation = update_attempt_stall_observation(
                jobs, aid, persist=False, row=rows.get(aid))
            if observation is None:
                values.append((aid, "missing", "", ""))
            else:
                state = observation.get("state", "unknown")
                values.append((aid, state, observation.get("episode_id", ""),
                               "" if state in {"stalled", "unknown"}
                               else observation.get("progress_fingerprint", "")))
        return hashlib.sha256(json.dumps(values, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
    if reason != "terminal-evidence-conflict":
        return ""
    value = [(aid, terminal_conflict_digest(rows[aid][1])) for aid in attempts]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def materialize(jobs: Path, attempts: set[str], *, reason: str) -> list[dict]:
    """Idempotent handback per exact root/batch/reason, with no row mutation."""
    if reason not in REASONS:
        raise SupervisionError("supervision-reason-invalid")
    jobs = jobs.resolve()
    rows = _rows(jobs, attempts)
    groups: dict[str, list[str]] = {}
    for aid in sorted(attempts):
        groups.setdefault(_root(rows, aid), []).append(aid)
    results = []
    for owner, monitored in groups.items():
        meta = rows[owner][1]
        recipient = meta.get("parent_sid", "")
        kind = meta.get("parent_completion_delivery", "")
        if not recipient or kind not in pending_delivery.RECIPIENT_KINDS:
            raise SupervisionError("supervision-parent-carrier-unbound")
        receipt = {
            "schema_version": 1, "kind": KIND, "state": "attention",
            "owner_attempt_id": owner, "monitored_attempt_ids": monitored,
            "recipient_thread_id": recipient,
            "sealed_batch_id": meta.get("managed_sealed_batch_id", ""),
            "job_registry": str(jobs), "reason": reason,
            "obligation_revision": _obligation_revision(rows, monitored, reason, jobs),
            "responsible": "supervision-controller", "required_action": "inspect-recovery",
        }
        key = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
        delivery = "delivery-" + key
        receipt["pending_delivery_id"] = delivery
        record = pending_delivery.create(jobs.parent, delivery_id=delivery,
            recipient_kind=kind, recipient_key=recipient,
            # The work obligation outlives gateway connections. The courier
            # proves the current recipient generation when claiming it.
            session_generation="", session_generation_supported="0",
            attempt_ids=[owner], parent_attempt_id=owner,
            route_id=meta.get("owner_route_id") or meta.get("route_id") or "route-free",
            route_node=meta.get("route_node") or "supervision",
            receipt=receipt, receipt_digest=receipt_digest(receipt),
            row_revisions={owner: "supervision:" + key})
        if reason == "attempt-stalled":
            for monitored_attempt in receipt["monitored_attempt_ids"]:
                _attach_stall_delivery(jobs, monitored_attempt, record)
            try:
                from peer_obligations import retain_registered_notice
                retain_registered_notice(jobs, record)
            except (OSError, ValueError, RuntimeError):
                # The canonical pending record remains discoverable on the next
                # normal observation pass if retention is temporarily unavailable.
                pass
        results.append(record)
    return results


def handover_identity_refusal(jobs: Path, owner_attempt_id: str, start_receipt: dict) -> dict:
    """Durably hand an identity-only replacement refusal to its route parent."""
    if start_receipt.get("reason") != "replacement-parent-identity-unproven":
        return {}
    from dispatch_owner_input import retained
    reason = ANSWER_AWAITING_PARENT if retained(jobs, owner_attempt_id) else "no-progress"
    try:
        records = materialize(Path(jobs), {owner_attempt_id}, reason=reason)
        if records:
            try:
                from peer_obligations import retain_registered_notice
                for record in records:
                    retain_registered_notice(jobs, record)
            except (OSError, ValueError, RuntimeError):
                # A runner outage does not erase the durable parent notice.
                # Its normal carrier/lifecycle recovery can still deliver it.
                pass
            return {"parent_notified": True,
                    "next_step": ("The route parent has a durable notice to continue this exact replacement. "
                                  "Its runtime will recheck the existing route start and parent authority when "
                                  "the notice is consumed; this receipt does not claim that a worker launched.")}
    except (OSError, ValueError, SupervisionError):
        pass
    return {"parent_notified": False,
            "next_step": ("The route parent remains the only session allowed to start this replacement, but "
                          "no durable parent notice was recorded. Continue with the existing route start from "
                          "that parent; no launch-settlement wait is required.")}


def validate(receipt: dict, *, expected_thread: str | None = None,
             expected_epoch: int | None = None) -> dict:
    keys = {"schema_version", "kind", "state", "owner_attempt_id", "monitored_attempt_ids",
            "recipient_thread_id", "sealed_batch_id", "job_registry",
            "reason", "responsible", "required_action", "pending_delivery_id", "obligation_revision"}
    if (not isinstance(receipt, dict) or set(receipt) != keys
            or receipt.get("kind") != KIND or receipt.get("schema_version") != 1
            or receipt.get("state") != "attention" or receipt.get("reason") not in REASONS
            or receipt.get("required_action") != "inspect-recovery"
            or receipt.get("responsible") != "supervision-controller"):
        raise SupervisionError("supervision-shape-invalid")
    text_keys = keys - {"schema_version", "monitored_attempt_ids"}
    if (any(not isinstance(receipt[key], str) for key in text_keys)
            or not Path(receipt["job_registry"]).is_absolute()
            or any(ord(char) < 32 for key in text_keys for char in receipt[key])):
        raise SupervisionError("supervision-field-invalid")
    attempts = receipt["monitored_attempt_ids"]
    if (not isinstance(attempts, list) or not attempts
            or any(not isinstance(a, str) or not re.fullmatch(r"att-[A-Za-z0-9._-]+", a)
                   for a in [*attempts, receipt["owner_attempt_id"]])
            or attempts != sorted(set(attempts))):
        raise SupervisionError("supervision-attempts-invalid")
    if expected_thread is not None and receipt["recipient_thread_id"] != expected_thread:
        raise SupervisionError("supervision-thread-mismatch")
    # Unlike an approval gate, recovery is attempt-scoped, not tied to a
    # particular connection. The common transport fences its live epoch.
    return dict(receipt)


def validate_digest(receipt: dict, supplied: str) -> None:
    if receipt_digest(receipt) != supplied:
        raise SupervisionError("supervision-digest-mismatch")


def validate_receipt(receipt: dict, *, jobs: Path, expected_thread_id: str,
                     expected_epoch: int, expected_attempts: set[str],
                     expected_sealed_batch_id: str, validate_live: bool = True) -> dict:
    receipt = validate(receipt, expected_thread=expected_thread_id, expected_epoch=expected_epoch)
    if (not jobs.is_absolute() or jobs.is_symlink() or str(jobs) != receipt["job_registry"]
            or receipt["owner_attempt_id"] not in expected_attempts
            or receipt["sealed_batch_id"] != expected_sealed_batch_id):
        raise SupervisionError("supervision-binding-mismatch")
    rows = _rows(jobs, set(receipt["monitored_attempt_ids"]) | {receipt["owner_attempt_id"]})
    owner = receipt["owner_attempt_id"]
    if owner not in rows:
        raise SupervisionError("supervision-owner-missing")
    meta = rows[owner][1]
    if (meta.get("parent_sid") != expected_thread_id
            or meta.get("managed_sealed_batch_id", "") != expected_sealed_batch_id
            or any(_root(rows, aid) != owner for aid in receipt["monitored_attempt_ids"])):
        raise SupervisionError("supervision-lineage-mismatch")
    # A recovered batch must not receive an obsolete intervention request.
    if _obligation_revision(rows, receipt["monitored_attempt_ids"], receipt["reason"], Path(receipt["job_registry"])) != receipt["obligation_revision"]:
        raise SupervisionError("supervision-resolved")
    if validate_live and (not _pending(rows, receipt["monitored_attempt_ids"], jobs, receipt["reason"])
                          or _cleanup_only_result(receipt) is not None):
        raise SupervisionError("supervision-resolved")
    record = pending_delivery.read(jobs.parent, expected_thread_id, receipt["pending_delivery_id"])
    if record is None or record.get("receipt") != receipt:
        raise SupervisionError("supervision-authority-missing")
    return receipt


def validate_pending_record(record: dict, **kwargs) -> dict:
    receipt = validate_receipt(record.get("receipt"), **kwargs)
    if (record.get("delivery_id") != receipt["pending_delivery_id"]
            or record.get("attempt_ids") != [receipt["owner_attempt_id"]]
            or record.get("parent_attempt_id") != receipt["owner_attempt_id"]
            or record.get("recipient_digest") != pending_delivery.recipient_digest(receipt["recipient_thread_id"])
            or record.get("receipt_digest") != receipt_digest(receipt)):
        raise SupervisionError("supervision-pending-mismatch")
    return dict(record)


def gateway_delivery_id(receipt: dict) -> str:
    return "sn-dlv-" + receipt_digest(validate(receipt)).removeprefix("sha256:")


def notice_is_current(record: dict, *, notice_context=None) -> bool:
    """Shared pre-delivery check for native prompt and async carriers."""
    receipt = validate(record.get("receipt"))
    from dispatch_notice_state import NoticeReadContext
    notice_context = notice_context or NoticeReadContext()
    if record.get("receipt_digest") != receipt_digest(receipt):
        raise SupervisionError("supervision-digest-mismatch")
    rows = _rows(Path(receipt["job_registry"]),
                 set(receipt["monitored_attempt_ids"]) | {receipt["owner_attempt_id"]})
    owner = receipt["owner_attempt_id"]
    if (owner not in rows or rows[owner][1].get("parent_sid") != receipt["recipient_thread_id"]
            or any(_root(rows, aid) != owner for aid in receipt["monitored_attempt_ids"])):
        raise SupervisionError("supervision-lineage-mismatch")
    if _obligation_revision(rows, receipt["monitored_attempt_ids"], receipt["reason"], Path(receipt["job_registry"])) != receipt["obligation_revision"]:
        return False
    if _cleanup_only_result(receipt) is not None:
        return False  # Retire the model wake; the cleanup controller still owns its observation.
    return _pending(rows, receipt["monitored_attempt_ids"], Path(receipt["job_registry"]),
                    receipt["reason"], notice_context=notice_context)


def _residue_text(receipt: dict) -> str:
    """Name survivors a finished worker left running; display only, empty when none."""
    try:
        from dispatch_contract import residue_live_pids
        rows = _rows(Path(receipt["job_registry"]),
                     set(receipt["monitored_attempt_ids"]) | {receipt["owner_attempt_id"]})
    except (OSError, ValueError, SupervisionError):
        return ""
    parts = []
    for aid in receipt["monitored_attempt_ids"]:
        pids = residue_live_pids(rows[aid][1]) if aid in rows else ()
        if pids:
            parts.append(f"{aid} finished, but the worker left process pid "
                         f"{', '.join(map(str, pids[:4]))} running")
    if not parts:
        return ""
    return (" " + "; ".join(parts) + ". The row closes by itself once that process exits; "
            "stop it if it is no longer needed. Nothing is signalled automatically.")


def _printed(name: str) -> str:
    from parent_next_directive import entrypoint
    return entrypoint(Path(__file__).resolve().parents[1], f"utilities/{name}")


def _resume_text(receipt: dict) -> str:
    """The route's start command for the notice's owner, or "" when its route cannot be read."""
    try:
        from dispatch_replacement import _route
        from parent_next_directive import resume_command
        rows = _rows(Path(receipt["job_registry"]), {receipt["owner_attempt_id"]})
        path, _route_doc = _route(Path(receipt["job_registry"]), receipt["owner_attempt_id"],
                                  rows[receipt["owner_attempt_id"]][1])
        return resume_command(path, receipt["job_registry"], agent_home=Path(__file__).resolve().parents[1])
    except Exception:  # noqa: BLE001 -- the notice still names the owner
        return ""


def _acknowledged_completion_result(receipt: dict) -> dict | None:
    """Return the exact all-child receipt when its current recipient ACKed it."""
    return _recorded_completion_result(receipt, require_ack=True)


def _recorded_completion_result(receipt: dict, *, require_ack: bool, notice_context=None) -> dict | None:
    """Cover all monitored attempts using existing exact receipt projections."""
    try:
        import dispatch_pending_delivery as pending
        from dispatch_completion_join import (current_attempt_row, recover_acknowledged_batch_outcome,
                                               current_batch_receipt_projection)
        from dispatch_seat_handover import effective_parent
        jobs = Path(receipt["job_registry"])
        monitored = set(receipt["monitored_attempt_ids"])
        covered = {}
        for attempt in sorted(monitored):
            row = current_attempt_row(jobs, attempt)
            if row is None:
                return None
            meta = row.metadata
            target = effective_parent(meta, jobs)
            if (meta.get("parent_sid") != receipt["recipient_thread_id"]
                    or not meta.get("delivery_id") or not target):
                return None
            record = pending.read(jobs.resolve(strict=True).parent,
                                  meta["parent_sid"], meta["delivery_id"])
            body = (record or {}).get("receipt")
            if not isinstance(body, dict):
                continue
            # Per-child terminal receipts keep scope in the pending identity,
            # leaving parent_attempt_id empty in the immutable body. The shared
            # projection verifies that identity before projecting legacy scope.
            parent_attempt = body.get("parent_attempt_id") or record.get("parent_attempt_id", "")
            attempt_ids = {child.get("attempt_id") for child in body.get("children", [])
                           if isinstance(child, dict)}
            if not attempt_ids:
                continue
            # A receipt can include siblings outside the notice's monitored
            # subset. Their effective recipients remain fresh too, even when
            # the unchanged receipt bytes below are reused.
            if notice_context is not None:
                for aid in sorted(attempt_ids):
                    member = current_attempt_row(jobs, aid)
                    if member is None or effective_parent(member.metadata, jobs) != target:
                        return None
            def project(observer):
                observer("file", jobs)
                # Include every batch member, not only this notice's subset.
                from dispatch_notice_state import bound_route
                for aid in sorted(attempt_ids):
                    member = current_attempt_row(jobs, aid)
                    if member is None:
                        return None
                    member_meta = member.metadata
                    if member_meta.get("parent_sid") and member_meta.get("delivery_id"):
                        observer("file", pending.record_path(jobs.resolve(strict=True).parent,
                            member_meta["parent_sid"], member_meta["delivery_id"]))
                    bound_route(member_meta, jobs, observer=observer)
                projection = recover_acknowledged_batch_outcome if require_ack else current_batch_receipt_projection
                return projection(jobs, attempt_ids, recipient_sid=target,
                    parent_attempt_id=parent_attempt,
                    parent_completion_delivery=meta.get("parent_completion_delivery", ""))
            # Effective recipient and current record are always read above.
            # Only the stored receipt projection is reused, never that authority.
            if notice_context is not None:
                recovered = notice_context.read(("completion", str(jobs.resolve()),
                    tuple(sorted(attempt_ids)), target, parent_attempt,
                    meta.get("parent_completion_delivery"), require_ack), project)
            else:
                projection = recover_acknowledged_batch_outcome if require_ack else current_batch_receipt_projection
                recovered = projection(jobs, attempt_ids, recipient_sid=target,
                    parent_attempt_id=parent_attempt,
                    parent_completion_delivery=meta.get("parent_completion_delivery", ""))
            if recovered is None:
                continue
            children = recovered.get("children")
            if (isinstance(children, list) and children
                    and all(isinstance(child, dict) and child.get("status") == "done"
                            and child.get("readiness") == "ready"
                            and child.get("required_action") == "advance-completed"
                            for child in children)):
                covered.update({child["attempt_id"]: child for child in children})
        return {"children": list(covered.values())} if monitored and monitored <= covered.keys() else None
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None


def _completion_refusal_notice(receipt: dict, *, notice_context=None) -> str:
    """Render late-bound refusal details from this receipt's exact owner row."""
    try:
        utility = _printed("dispatch_terminal_commit.py")
    except Exception:  # noqa: BLE001 -- a read-only path fallback keeps the pending notice useful
        utility = str(Path(__file__).resolve().parents[1] / "utilities" / "dispatch_terminal_commit.py")
    clean = lambda value: re.sub(r"[\x00-\x1f\x7f]", "", str(value))[:2048]
    command = shlex.join(["python3", str(utility), "inspect",
                          "--jobs", clean(receipt["job_registry"]),
                          "--attempt", clean(receipt["owner_attempt_id"])])
    try:
        from dispatch_completion_join import exact_attempt_row
        from dispatch_notice_state import NoticeReadContext
        row = exact_attempt_row(Path(receipt["job_registry"]), receipt["owner_attempt_id"])
        diagnosis = (notice_context or NoticeReadContext()).owner_completion(
            Path(receipt["job_registry"]), row.status, row.metadata)
    except Exception:  # noqa: BLE001 -- closure remains controller-owned if the exact row is unavailable
        diagnosis = {}
    refusal = diagnosis.get("last_refusal") if isinstance(diagnosis, dict) else None
    if not isinstance(refusal, dict):
        checkpoint = diagnosis.get("checkpoint") if isinstance(diagnosis, dict) else None
        detail = f" The last recorded checkpoint is {checkpoint}." if isinstance(checkpoint, str) else ""
        return ("No recorded refusal is available." + detail
                + " The existing controller retries closure automatically; read-only diagnosis: " + command)
    reason = refusal.get("reason")
    raw_detail = refusal.get("detail", "")
    nested_output_invalid = reason == "producer-finalize-failed" and isinstance(raw_detail, str) \
        and raw_detail.startswith("output-invalid:")
    if reason != "output-invalid" and not nested_output_invalid:
        safe_reason = reason if isinstance(reason, str) and re.fullmatch(r"[a-z0-9-]{1,80}", reason) else "closure-refused"
        return (f"The last recorded closure refusal is {safe_reason}. The existing controller retries when possible; "
                "read-only diagnosis: " + command)
    # Producer validation records a semicolon-separated list of code:locator
    # values. Keep only that known shape and strip control characters.
    details = []
    if isinstance(raw_detail, str):
        for item in raw_detail.split(";"):
            match = re.match(r"[a-z0-9-]{1,80}:[^;\x00-\x1f\x7f]{1,500}", item)
            if match:
                details.append(match.group(0))
    safe_detail = "; ".join(details) if details else "invalid payload locator"
    path = refusal.get("record_path")
    safe_path = clean(path) if isinstance(path, str) else "record path unavailable"
    remediation = refusal.get("remediation")
    safe_remediation = (remediation if isinstance(remediation, str) and "\n" not in remediation and "\r" not in remediation
                        else "Correct the locator, mark its exact payload directory for self-exclusion, or move it outside the cycle.")
    cause = "producer-finalize-failed (output-invalid)" if nested_output_invalid else "output-invalid"
    return (f"The last recorded closure refusal is {cause}: {safe_detail}. Record: {safe_path}. "
            f"{safe_remediation} The existing controller retries closure automatically; read-only diagnosis: {command}")


def _cleanup_only_result(receipt: dict) -> dict | None:
    if receipt["reason"] in {"process-unverifiable", "join-deadline", "join-observer-failed",
                              "supervisor-exited", "watch-deadline", "receiver-unavailable"}:
        return _acknowledged_completion_result(receipt)
    return None


def _cleanup_only_text(receipt: dict, completed: dict) -> str:
    attempts = ", ".join(receipt["monitored_attempt_ids"])
    text = (f"The exact settled completion for {attempts} was already received by the current parent. "
            "This notice retains cleanup observation only; the process cleanup is still live or unknown "
            "under the existing controller. Do not restart the goal, repeat completed follow-through, "
            "or ask the user whether to wait or cancel. This notice grants no process signal, registry "
            "rewrite, retry, or cancellation. Preserve any new failure, conflict, unfinished work, or "
            "direct user question. The parent result remains the exact received result.")
    try:
        import dispatch_pending_delivery as pending
        from dispatch_completion_join import current_attempt_row
        completion_times = []
        for child in completed.get("children", []):
            if not isinstance(child, dict) or not isinstance(child.get("attempt_id"), str):
                continue
            row = current_attempt_row(Path(receipt["job_registry"]), child["attempt_id"])
            if row is None or not row.metadata.get("parent_sid") or not row.metadata.get("delivery_id"):
                continue
            record = pending.read(Path(receipt["job_registry"]).resolve(strict=True).parent,
                                  row.metadata["parent_sid"], row.metadata["delivery_id"])
            timing = (record or {}).get("timestamp_provenance") or {}
            first_send = timing.get("first_send_at_utc") or "unknown"
            received = timing.get("receive_at_utc") or "unknown"
            first_source = timing.get("first_send_source") or "unknown"
            receive_source = timing.get("receive_source") or "unknown"
            completion_times.append(f"{child['attempt_id']} first observed send={first_send} ({first_source}), "
                                    f"exact receipt={received} ({receive_source})")
        if completion_times:
            text += " Original completion delivery timing (UTC wall clock): " + "; ".join(completion_times) + "."

        rows = _rows(Path(receipt["job_registry"]), set(receipt["monitored_attempt_ids"]))
        from dispatch_contract import residue_live_pids
        residues = []
        for attempt in receipt["monitored_attempt_ids"]:
            row = rows.get(attempt)
            pids = residue_live_pids(row[1]) if row else ()
            if pids:
                residues.append(f"{attempt} residue pid(s) {', '.join(map(str, pids[:4]))}")
        if residues:
            text += " Observed cleanup residue: " + "; ".join(residues) + "."
    except (OSError, ValueError, SupervisionError):
        pass
    return text


# Set on a delivered record (not its receipt) when its carrier started the continuation.
CONTINUED_KEY = "parent_continuation"


def continue_for_parent(record: dict, *, session_id: str, recipient_kind: str,
                        environ=None, arm=None) -> dict | None:
    """Let the route parent resume an answer or exact resultless replacement notice.

    Only the route's parent launches it (`route_authority.require_replacement_parent`). The carrier
    that kept its claim for `session_id` runs under that session's own identity and arms the route's
    existing `start` path. Exact replacement records use their source edge; ordinary answers keep
    their existing answer-resume behavior."""
    receipt = record.get("receipt") if isinstance(record, dict) else None
    if (not session_id or not isinstance(receipt, dict) or receipt.get("kind") != KIND
            or receipt.get("reason") not in {ANSWER_AWAITING_PARENT, "no-progress"}):
        return None
    try:
        from dispatch_replacement import _route, source_reservation
        from session_identity import session_env
        harness = recipient_kind.split("-", 1)[0]
        jobs = Path(receipt["job_registry"])
        owner = receipt["owner_attempt_id"]
        route_file, route = _route(jobs, owner, _rows(jobs, {owner})[owner][1])
        env = {**(os.environ if environ is None else environ),
               session_env()[harness][0]: session_id, "AGENT_DISPATCH_CALLER_HARNESS": harness}
        if arm is None:
            from capacity_auto_resume import arm
        result = {"reason": receipt["reason"], "route_id": route.get("route_id")}
        if receipt["reason"] == "no-progress" or source_reservation(jobs, owner):
            result["source_attempt_id"] = owner
        return arm(result, route_file, jobs, environ=env)
    except Exception:  # noqa: BLE001 -- the notice still carries the start command
        return None


def render_text(receipt: dict, *, continued: dict | None = None, notice_context=None) -> str:
    receipt = validate(receipt)
    completed = _cleanup_only_result(receipt)
    if completed is not None:
        return _cleanup_only_text(receipt, completed)
    if receipt["reason"] == "attempt-stalled":
        rows = _rows(Path(receipt["job_registry"]),
                     set(receipt["monitored_attempt_ids"]) | {receipt["owner_attempt_id"]})
        messages = []
        route_commands = []
        for aid in receipt["monitored_attempt_ids"]:
            status, metadata = rows.get(aid, ("unknown", {}))
            observation = update_attempt_stall_observation(
                Path(receipt["job_registry"]), aid, persist=False,
                row=(status, metadata) if metadata else None)
            route_id = metadata.get("owner_route_id") or metadata.get("route_id") or "unknown-route"
            route_node = metadata.get("route_node") or ("_owner" if metadata.get("worker_type") == "owner" else "unknown-node")
            route_attention = observation.get("route_attention") if observation else None
            if isinstance(route_attention, dict):
                reason = route_attention.get("reason") or "needs-attention"
                action = route_attention.get("required_action") or "inspect-recovery"
                command = route_attention.get("command") or ""
                gate = route_attention.get("gate")
                detail = f"; gate={gate}" if gate else ""
                if command:
                    route_commands.append((route_attention.get("command_kind") or "recovery", command))
                if reason == "work-parent-recovery-required":
                    authority = " This is a current-parent authority handoff, not human approval."
                elif reason.startswith("human-gate-"):
                    authority = " This gate state is distinct from parent authority and an active human wait."
                else:
                    authority = ""
                messages.append(f"{route_id}/{route_node} attempt {aid}: route needs attention; "
                                f"reason={reason}; required_action={action}{detail}.{authority}")
                continue
            if not observation or observation.get("state") == "unknown":
                messages.append(f"{route_id}/{route_node} attempt {aid}: execution or progress is unverified; "
                                "do not infer that it ended")
            elif observation.get("reason") == "execution-absent":
                messages.append(f"{route_id}/{route_node} attempt {aid}: no exact runtime/command process "
                                "is present; a carrier may still be alive")
            elif observation.get("reason") == "settlement-unresolved":
                messages.append(f"{route_id}/{route_node} attempt {aid}: the exact runtime result is present, "
                                "but route settlement or its completion marker remains pending")
            else:
                try:
                    quiet = max(0, int(observation.get("quiet_windows", 0)))
                except (TypeError, ValueError, OverflowError):
                    quiet = 0
                last = observation.get("last_progress_at")
                try:
                    age = max(0, int(time.time() - float(last))) if last is not None else None
                except (TypeError, ValueError, OverflowError):
                    age = None
                duration = f"; last comparable progress {age}s ago" if age is not None else ""
                messages.append(f"{route_id}/{route_node} attempt {aid}: no comparable progress for "
                                f"{quiet} watchdog windows{duration}")
        if route_commands:
            kind, command = route_commands[0]
            next_command = f"Existing next-command for the current parent ({kind}): {command}. "
            next_command += "The route's existing authority and gate checks still apply."
        else:
            aid = receipt["monitored_attempt_ids"][0]
            command = shlex.join(["python3", str(Path(__file__).resolve().parents[1]
                                                    / "utilities/dispatch-registry.py"),
                                  "reconcile", "--jobs", receipt["job_registry"],
                                  "--attempt", aid])
            next_command = "Read-only next command: " + command + "."
        return ("Needs attention: " + "; ".join(messages) + ". This notice did not start, retry, "
                "signal, approve, release a gate, or close the attempt. " + next_command)
    if receipt["reason"] == ANSWER_AWAITING_PARENT:
        if continued:
            return ("Another session answered owner " + receipt["owner_attempt_id"] + ", which ended BLOCKED; "
                    "the answer is kept. This session's runtime scheduled the route's start under the parent's "
                    "identity; the existing launch checks still decide whether a new owner starts. The result "
                    "will arrive through the existing notice path. This is a handoff, not completion; do not "
                    "ask for the answer again.")
        command = _resume_text(receipt)
        return ("Another session answered owner " + receipt["owner_attempt_id"] + ", which ended BLOCKED; "
                "the answer is kept. Only this route's parent session launches its continuation, so "
                "continue the work with the route's start command: the new owner receives the answer "
                "first. Do not ask for the answer again." + (" " + command if command else ""))
    if receipt["reason"] == "no-progress":
        if continued:
            return ("The route parent has an armed start for the remaining replacement work. Its runtime "
                    "will recheck the exact process, claim, and parent authority before launching; this is "
                    "handoff evidence, not worker completion. The result will arrive through the existing "
                    "notice path.")
        command = _resume_text(receipt)
        return ("A resultless replacement still needs its route parent to continue it. No parent start "
                "was observed; the exact process, claim, and launch checks remain in force. "
                + ("Use the route start command: " + command if command else "Inspect the exact route and owner."))
    if receipt["reason"] == "owner-input-undelivered":
        command = shlex.join(["python3", _printed("capability-route.py"),
                              "correct", "--jobs", receipt["job_registry"],
                              "--attempt-id", receipt["owner_attempt_id"]])
        return ("A user correction was not delivered, or its delivery is unknown. "
                "The execution result is unchanged. Read the exact input receipts, tell the user "
                "what remains unconfirmed, and do not resend an unknown input automatically. " + command)
    if receipt["reason"] == "workflow-completion-pending":
        return ("The owner result remains PASS, but workflow/route/report closure is pending. "
                "The completion controller owns closure; this is not a running model. "
                "The notice authorizes no new execution. " + _completion_refusal_notice(receipt, notice_context=notice_context))
    if receipt["reason"] == "closure-blocked":
        utility = _printed("dispatch_terminal_commit.py")
        command = (f"python3 {shlex.quote(str(utility))} inspect --jobs {shlex.quote(receipt['job_registry'])} "
                   f"--attempt {shlex.quote(receipt['owner_attempt_id'])}")
        return ("The owner result remains PASS, but its own workflow/route closure can never complete from "
                "this exact attempt -- a proven permanent reason (a later attempt already claimed this route "
                "node, or the route's completion marker no longer matches its recorded evidence), not an "
                "ordinary in-flight wait. Retrying this attempt's closure will not help. Explain this to the "
                "user and inspect the exact gate reason before deciding a recovery path (a fresh dispatch of "
                "the same route node, or manual repair of the marker/evidence). Read-only diagnosis: " + command)
    if receipt["reason"] == "parent-session-ended":
        return ("The exact Codex parent session ended. The recorded child attempts continue unchanged, "
                "and their completion and delivery obligations remain in the durable batch record. "
                "The existing controller resumes them automatically when that parent or a confirmed "
                "same-seat successor is active; nothing was cancelled or retried.")
    if receipt["reason"] in {"watch-deadline", "receiver-unavailable"}:
        utility = _printed("dispatch_terminal_commit.py")
        command = (f"python3 {shlex.quote(str(utility))} finish --jobs {shlex.quote(receipt['job_registry'])} "
                   f"--attempt {shlex.quote(receipt['owner_attempt_id'])}")
        detail = ("The completion watch reached its own deadline (about a day) without the batch settling"
                  if receipt["reason"] == "watch-deadline" else
                  "The completion watch could not reach its delivery gateway, and every monitored attempt "
                  "is already terminal")
        return (f"{detail}, so it recorded this notice and stopped instead of running forever. The owner "
                "result and workflow/route closure state are unchanged -- nothing was retried or discarded. "
                "The same observation duty stays retained: the existing controller, orphan recovery, or session "
                "sweep resumes it automatically without a manual retry command; this notice alone does not. "
                "Existing transaction recovery: " + command)
    utility = _printed("dispatch-registry.py")
    operation = "resolve-terminal-conflict" if receipt["reason"] == "terminal-evidence-conflict" else "reconcile"
    commands = [f"python3 {shlex.quote(str(utility))} {operation} --jobs "
                f"{shlex.quote(receipt['job_registry'])} --attempt {shlex.quote(aid)}"
                for aid in receipt["monitored_attempt_ids"]]
    disposition = (
        "Compare the committed result with the conflicting evidence and explain the discrepancy. "
        "The read-only command previews the exact conflict. After reviewing and recording the evidence, "
        "use the same command with --review-evidence <report> --expected-row-sha256 <preview-hash> --apply "
        "to retain the committed result and release consumption. A new conflict requires a fresh review. "
        if receipt["reason"] == "terminal-evidence-conflict" else
        "Keep unresolved observation with the existing recovery controller. An inspection notice alone "
        "does not require a user decision. Ask about cancellation only when actual unfinished work "
        "requires that decision under its existing authority; "
    )
    return ("Hearting supervision needs attention. This is not workflow completion. "
            f"reason={receipt['reason']} owner={receipt['owner_attempt_id']}. "
            "The controller retains waiting/recovery responsibility. Explain the blockage to the user "
            "and inspect these exact attempts. Do not infer death, erase rows, or retry from this notice. "
            + disposition +
            "an accepted notification does not close the work. Read-only diagnosis: " + " ; ".join(commands)
            + _residue_text(receipt))


def context(receipt: dict, delivery_id: str) -> dict:
    return {"threadId": receipt["recipient_thread_id"], "input": [],
            "clientUserMessageId": delivery_id,
            "additionalContext": {"hearting-supervision": {"kind": "application", "value": render_text(receipt)}}}


def _project_replacement_lineage(edges: list[dict], roots: set[str]) -> list[dict]:
    """Collapse observed exact edges to one bounded original-to-final row."""
    outgoing = {}
    for edge in edges:
        original = edge.get("original_attempt_id")
        target = edge.get("replacement_attempt_id")
        if not isinstance(original, str) or not isinstance(target, str):
            raise SupervisionError("replacement-lineage-invalid")
        previous = outgoing.get(original)
        if previous is not None and previous.get("replacement_attempt_id") != target:
            raise SupervisionError("replacement-lineage-conflict")
        outgoing[original] = edge
    projected = []
    for root in sorted(roots):
        first = outgoing.get(root)
        if first is None:
            continue
        target = first["replacement_attempt_id"]
        seen = {root}
        while target in outgoing:
            if target in seen:
                raise SupervisionError("replacement-lineage-cycle")
            seen.add(target)
            target = outgoing[target]["replacement_attempt_id"]
        projected.append({**first, "replacement_attempt_id": target})
    return projected


def wait_for_batch(*, join: Callable[[set[str]], dict], attempts: set[str],
                   jobs: Path, parent_attempt_id: str = "", emit: Callable[[dict], None] | None = None,
                   on_timeout: Callable[[set[str]], None] | None = None,
                   deadline: float | None = None,
                   stop_check: Callable[[], str] | None = None,
                   replacement_checkpoint: Callable | None = None) -> dict:
    """One shared wait loop. Join deadlines are checkpoints, never death votes.

    Execution boundaries retain their finite budgets. The parent receives one
    durable notice for this exact batch while this controller retains the wait.
    A failed queue write is retried at the next bounded checkpoint, not discarded.

    `deadline` and `stop_check` are both optional and, left `None`, leave this
    loop's behavior exactly as before (every existing caller). A caller that
    itself owns an unfinishable-watch budget -- one process that must not run
    forever -- supplies one or both: `deadline` (a `time.monotonic()` value;
    once reached after a join timeout, this records one `watch-deadline`
    notice and returns `{"state": "watch-expired", ...}` instead of looping
    again) and/or `stop_check` (called once per join timeout; a non-empty
    REASONS token it returns halts the same way, under that reason). Neither
    ever fires mid-join -- only at the same checkpoint an ordinary
    `join-deadline` notice already used.
    """
    ordinal = 0
    lineage_edges = []
    lineage = []
    roots = set(attempts)
    attention = []
    while True:
        observer_error, deferred = "", None
        try:
            receipt = join(set(attempts))
            if replacement_checkpoint is not None:
                effective, edges, attention = replacement_checkpoint(set(attempts))
                lineage_edges.extend(edge for edge in edges if edge not in lineage_edges)
                lineage = _project_replacement_lineage(lineage_edges, roots)
                if set(effective) != set(attempts):
                    attempts = set(effective)
                    continue
            if receipt.get("state") != "timeout":
                if lineage:
                    receipt = {**receipt, "replacement_lineage": sorted(lineage, key=lambda edge: edge["original_attempt_id"])}
                if attention:
                    receipt = {**receipt, "replacement_attention": attention}
                return receipt
        except StopWaiting:
            raise
        except Exception as exc:
            from dispatch_completion_join import CompletionDeferred
            if isinstance(exc, CompletionDeferred):
                deferred = exc
            else:
                # Observation failure retains the exact batch and diagnostic.
                observer_error = str(exc)
        ordinal += 1
        halt_reason = ""
        if not observer_error:
            if stop_check is not None:
                try:
                    halt_reason = stop_check() or ""
                except StopWaiting:
                    raise
                except Exception as exc:
                    observer_error = str(exc)
            if not halt_reason and deadline is not None and time.monotonic() >= deadline:
                halt_reason = "watch-deadline"
        if halt_reason:
            try:
                records = materialize(jobs, attempts, reason=halt_reason)
            except (OSError, ValueError, pending_delivery.PendingDeliveryError):
                records = []
            result = {"state": "watch-expired", "reason": halt_reason, "delivery_records": records}
            if lineage:
                result["replacement_lineage"] = sorted(lineage, key=lambda edge: edge["original_attempt_id"])
                result["children"] = [{"attempt_id": aid} for aid in sorted(attempts)]
            if attention:
                result["replacement_attention"] = attention
            return result
        notice_error = ""
        try:
            materialize(jobs, attempts,
                        reason="join-observer-failed" if observer_error else "join-deadline")
        except (OSError, ValueError, pending_delivery.PendingDeliveryError) as exc:
            notice_error = str(exc)
        if on_timeout:
            try:
                on_timeout(set(attempts))
            except Exception as exc:
                observer_error = str(exc)
        if emit:
            emit({"type": "dispatch.supervisor.reparked", "parent_attempt_id": parent_attempt_id,
                  "attempt_count": len(attempts), "repark_ordinal": ordinal,
                  "responsible": "supervision-controller", "notice_error": notice_error,
                  "observer_error": observer_error,
                  **({"reason": str(deferred), "retry_when": getattr(deferred, "retry_when", "")} if deferred else {})})
        # A failed observer receives bounded backoff; execution budgets remain
        # the execution boundary's responsibility, never this observer's vote.
        pause = 30.0 if observer_error or deferred else 0.05
        if deadline is not None:
            pause = min(pause, max(0.0, deadline - time.monotonic()))
        time.sleep(pause)


def wait_for_child_settlement(*, jobs: Path, attempts: set[str],
                              parent_attempt_id: str,
                              join: Callable[[set[str]], dict],
                              reconcile: Callable[[set[str]], object],
                              emit: Callable[[dict], None]) -> None:
    """Retain an unresolved child after notification without another model turn.

    Exact recovery and the shared attempt policy decide when work is settled.
    A ready join receipt alone does not prove cleanup or registry closure.
    The ordinary wait controller retains the obligation and parent handback.
    """
    def observe(monitored: set[str]) -> dict:
        if not _pending(_rows(jobs, monitored), sorted(monitored), jobs):
            return {"state": "settled"}
        reconcile(monitored)
        if not _pending(_rows(jobs, monitored), sorted(monitored), jobs):
            return {"state": "settled"}
        receipt = join(monitored)
        reconcile(monitored)
        if not _pending(_rows(jobs, monitored), sorted(monitored), jobs):
            return {"state": "settled"}
        if receipt.get("state") != "timeout":
            # A terminal-but-unclosed observation may return immediately.
            # Retry evidence collection at a bounded cadence, without tokens.
            time.sleep(1.0)
        return {"state": "timeout"}

    def held_by_conflict() -> str:
        """End the wait only when every remaining child is a settled, unreviewed conflict.

        Open, live, or recoverable work keeps the wait; a closed and quiescent
        child that only a person's review can change would otherwise hold this
        supervisor, and its watchers, forever.
        """
        from dispatch_contract import PARENT_EXTINCTION_TERMINAL_STATUSES
        rows = _rows(jobs, attempts)
        held = False
        for aid in sorted(attempts):
            if aid not in rows:
                return ""
            if not _pending({aid: rows[aid]}, [aid], jobs):
                continue
            status, meta = rows[aid]
            proof, decision = _settlement_decision(status, meta)
            if (decision.action != "inspect-conflict"
                    or status not in PARENT_EXTINCTION_TERMINAL_STATUSES
                    or proof.process_state != "quiescent"):
                return ""
            held = True
        return "terminal-evidence-conflict" if held else ""

    wait_for_batch(join=observe, attempts=attempts, jobs=jobs,
                   parent_attempt_id=parent_attempt_id, emit=emit,
                   stop_check=held_by_conflict)
