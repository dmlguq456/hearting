"""Peer steward: runner duty discovery, reconcile limits, peer indexes and history pruning.

Split out of `peer-steward.py`, which re-exports every name defined here.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

import peer_obligations
import shared_readiness
import dispatch_batch_obligations

from peer_steward_base import (
    _codex_home_dir, _plain, peer_message,
)

from peer_steward_watch import (
    _read_json, _steward_check_module, _watch_entries, _watch_paths, _watch_root,
    _watch_selected_readiness_endpoint, _watcher_present,
)

from peer_steward_status import (
    _blocking_screen,
)

from peer_steward_native import (
    _message_obligation_id,
)


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
    native = int(arm.get("observer_protocol", 0) or 0) >= 3
    pid = agent.get("pid") if native else pane_identity.get("shell_pid")
    start = agent.get("pid_start") if native else pane_identity.get("shell_start")
    identity = shared_readiness.target_identity(
        server=server, endpoint=str(endpoint), pane=agent.get("pane"),
        harness=agent.get("harness"), session_id="" if session_id == "-" else session_id,
        shell_pid=pid, shell_start=start, native_state=native_state,
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


def _settled_handoff_acceptance_duties(store):
    """Recover prior-release accepted handoffs once when the retained runner starts."""
    selected = []
    for duty in store.list(states={"complete"}, kinds={"message"}, cached_terminal=False):
        intent = duty.get("intent") or {}
        if not intent.get("handoff_acceptance"):
            continue
        ref = intent.get("ref")
        if not isinstance(ref, str) or not ref:
            continue
        try:
            row = peer_message._read_pending(ref)
            acceptance = (row or {}).get("acceptance") or {}
            retire_id = acceptance.get("retire_id")
            retire = store.get(retire_id) if isinstance(retire_id, str) else None
            import session_tidy_runner as tidy_runner
            context, predecessor, _successor, _requester, target = \
                tidy_runner._handoff_context(row, peer=peer_message)
            retired_identity = ((retire or {}).get("intent") or {}).get("identity") or {}
            if (acceptance.get("read_and_accepted") is not True
                    or acceptance.get("settled") is not True
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
    return selected


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


def _obligation_retry_fingerprint(duty):
    value = {key: duty.get(key) for key in
             ("id", "state", "intent", "observation", "result", "delivery", "cleanup")}
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _discover_session_notice_duties(store, *, read_context=None):
    """Recover usable old notices; False keeps partial discovery with this runner."""
    import dispatch_pending_delivery as pending
    import session_notice
    from harness_capabilities import HARNESSES, parent_completion
    from session_tidy import _handover_origin
    from peer_read_context import PeerReadContext
    from dispatch_seat_handover import (_registered_parent_harness,
                                        lineage_graph, record_recipient)
    state_root = store.root.parent.parent.resolve()
    jobs = state_root / "jobs.log"
    observed = True
    candidates = set()
    read_context = read_context or PeerReadContext(registry_append_only=True)
    try:
        graph = lineage_graph(read_context=read_context)
        candidates.update(graph.ledger_keys)
        for rows in graph.ledger_rows.values():
            for row in rows:
                if row.get("event") == "handover" and row.get("source") == "retire":
                    origin = _handover_origin(row)
                    if origin is not None:
                        candidates.add(origin)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        # Unavailable authority cannot authorize notices or abandon the runner's
        # other accepted work. Retry discovery on its next ordinary pass.
        observed = False
    try:
        # Discovery and reconciliation consume one generation-checked view.
        # Missing jobs is valid for route-free notices; partial input is not.
        view = read_context.registry_view(jobs)
        observed = (view is None or not view.completeness_reason) and observed
        # The latest row's parent keys, projected once per registry generation.
        for values in (view.handover_parent_sessions() if view is not None else ()):
            meta = dict(zip(view.PARENT_SESSION_KEYS, values))
            sid = str(meta.get("parent_sid") or "")
            harness = _registered_parent_harness(meta)
            if sid and harness:
                candidates.add((harness, sid))
    except FileNotFoundError:
        pass  # Route-free notices do not require a jobs registry.
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        observed = False
    # One lineage observation answers every record's read-only check. Retention
    # is an effect: its recheck runs on its own context, which observes current
    # lineage once for this pass's retentions and never inherits the discovery pin.
    retention_context = PeerReadContext()
    with read_context.pinned_lineage(), retention_context.pinned_lineage():
        for storage_sid in sorted({sid for _harness, sid in candidates}):
            entries = []
            try:
                directory = pending.record_directory(state_root, storage_sid)
                info = directory.stat()
            except FileNotFoundError:
                continue
            except (OSError, pending.PendingDeliveryError):
                observed = False
                continue
            try:
                if not stat.S_ISDIR(info.st_mode):
                    raise OSError("pending-directory-unavailable")
                # glob/is_file can suppress observation errors. Preserve entries
                # already yielded even if enumeration subsequently becomes partial.
                for entry in directory.iterdir():
                    if entry.name.startswith("delivery-") and entry.suffix == ".json":
                        entries.append(entry)
            except OSError:
                observed = False
            for entry in sorted(entries):
                try:
                    try:
                        info = entry.stat()
                    except FileNotFoundError:
                        continue  # A removed record is ordinary absence.
                    if not stat.S_ISREG(info.st_mode):
                        raise OSError("pending-record-unavailable")
                    record = pending.read(state_root, storage_sid, entry.stem)
                    if not record or record.get("state") not in pending.OPEN_STATES:
                        continue
                    receipt = record.get("receipt", {})
                    if not isinstance(receipt, dict):
                        observed = False
                        continue
                    kind = receipt.get("kind")
                    if kind is not None and not isinstance(kind, str):
                        observed = False
                        continue
                    if kind != "notice":
                        continue
                    session_notice.validate_pending_record(record)
                    # A legacy retire names only the predecessor SID; the original
                    # carrier, not the successor's harness, identifies this record.
                    harness = next((name for name in HARNESSES
                                    if parent_completion(name).get("carrier") == record.get("recipient_kind")), None)
                    if harness is None:
                        observed = False
                        continue
                    if record_recipient(record, storage_sid, harness=harness,
                                        read_context=read_context) is None:
                        observed = False
                        continue
                    if not peer_obligations.retain_session_notice(
                            jobs, harness, storage_sid, record, read_context=retention_context):
                        # Its fresh authority/read/write check did not hand this
                        # recoverable notice to a duty. Keep the same waiting owner.
                        observed = False
                except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                    observed = False
    return observed


# ---------------------------------------------------------------------------
# Runner maintenance duties (the existing obligation runner owns these)
# ---------------------------------------------------------------------------
#
# The hooks only read derived observations. This runner pass publishes them and
# performs the global cleanup that stopped running after 2026-10-18. Every duty
# keeps its own cursor/due record (the `prune_terminal_obligations` pattern) and
# a bounded per-pass budget, so no single pass can block notice delivery.

RECONCILE_DUE_SECONDS = 6 * 3600
RECONCILE_MAX_ROWS = 500
RECONCILE_TIME_BUDGET_S = 5.0
PEER_HISTORY_RETENTION_SECONDS = 7 * 86400
RETIRE_ACTIVITY_SECONDS = 14 * 86400
RETIRE_ARCHIVE_SECONDS = 30 * 86400
MAINTENANCE_DUE_SECONDS = 6 * 3600
MAINTENANCE_TIME_BUDGET_S = 3.0
INBOX_PREFILL_DUE_SECONDS = 15 * 60
LINEAGE_ROWS_DUE_SECONDS = 15 * 60
PEER_INDEX_DUE_SECONDS = 15 * 60
PEER_INDEX_TIME_BUDGET_S = 1.0
# The prune's share of one maintenance pass; its cursor carries the rest.
PEER_HISTORY_TIME_BUDGET_S = 1.0
RUN_STATES_DUE_SECONDS = 30.0
STEWARD_RELATIONS_DUE_SECONDS = 60.0
NOTICE_BATCH_LIMIT = 8
RETIRE_ARCHIVE_DIRECTORY = "sessions-retired"
_ROLE_ROW_EVENTS = frozenset({"tidy-claim"})
_ROLE_ROW_KEYS = frozenset({"steward", "role", "watch", "watch_id", "role_id"})


def _open_run_ids(root, notice_duties):
    """Run ids named by open experiment notices, never a global scan by itself."""
    import dispatch_pending_delivery as pending
    import session_notice

    run_ids = set()
    for duty in notice_duties:
        try:
            source = peer_obligations._notice_duty_source(duty, root)
            if source is None:
                continue
            _harness, storage_sid, delivery_id, _source_jobs = source
            record = pending.read(root, storage_sid, delivery_id)
            receipt = (record or {}).get("receipt")
            key = receipt.get("key") if isinstance(receipt, dict) else None
            match = session_notice.EXPERIMENT_FINAL_KEY.match(key or "")
            if match is not None:
                run_ids.add(match.group("run_id"))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            continue
    return run_ids


def _run_jobs_for_row(row, run_id):
    """The jobs log named by a run row, using the row's own recorded value."""
    registry_path = row.get("registry_path")
    if not isinstance(registry_path, str) or not registry_path:
        return None
    path = Path(registry_path)
    if path.name == "jobs.log":
        return path
    try:
        import resource_run_registry as registry
        _source, data = registry.read_registry_source(path)
        payload = registry.strict_json_loads(data) if data is not None else None
        runs = payload.get("runs") if isinstance(payload, dict) else None
        run = runs.get(run_id) if isinstance(runs, dict) else None
        jobs = run.get("jobs") if isinstance(run, dict) else None
        if isinstance(jobs, str) and jobs:
            return Path(jobs)
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return None


def _publish_run_states(store, notice_duties):
    """Publish the public run status/exit the notice path may refresh from.

    Owner note N1: one ``scan()`` for the run ids named by open notice keys,
    each public record's status merged with the registry row's exit code. A
    short due interval keeps the runner pass responsive; the reader treats a
    file older than its notice as unknown and keeps the original text.
    """
    import dispatch_recipient_inbox as inbox

    root = store.root.parent.parent.resolve()
    identity = store._state_root_identity
    state = peer_obligations.read_runner_duty_state(root, "run-states")
    if not peer_obligations.runner_duty_due(state, RUN_STATES_DUE_SECONDS):
        return {"runs": 0, "published": False, "due": False}
    run_ids = _open_run_ids(root, notice_duties)
    if not run_ids:
        peer_obligations.write_runner_duty_state(
            root, "run-states", {"last_pass_ns": time.time_ns(), "runs": 0},
            expected_root_identity=identity)
        return {"runs": 0, "published": False, "due": True}
    import resource_run_registry as registry
    from resource_progress import read_public_run_record

    rows, _diagnostics = registry.scan()
    runs = {}
    for row in rows:
        run_id = row.get("run_id")
        if run_id not in run_ids:
            continue
        artifact_root = row.get("artifact_root")
        public = None
        if isinstance(artifact_root, str) and artifact_root:
            public = read_public_run_record(artifact_root, run_id)
        status = public.get("status") if isinstance(public, dict) else None
        if not isinstance(status, str) or not status:
            status = row.get("registry_status") or row.get("liveness") or "unknown"
        ended_at = row.get("ended_at")
        if isinstance(public, dict) and isinstance(public.get("ended_at"), (int, float)):
            ended_at = public["ended_at"]
        jobs = _run_jobs_for_row(row, run_id)
        runs[run_id] = {"status": status, "exit_code": row.get("exit_code"),
                        "ended_at": ended_at, "artifact_root": artifact_root,
                        "parent_attempt_id": row.get("parent_attempt_id"),
                        "jobs": str(jobs) if jobs is not None else ""}
    inbox.write_run_states(
        root, {"runs": runs}, expected_root_identity=identity)
    peer_obligations.write_runner_duty_state(
        root, "run-states", {"last_pass_ns": time.time_ns(), "runs": len(runs)},
        expected_root_identity=identity)
    return {"runs": len(runs), "published": True, "due": True}


def _live_recipients(root):
    """Sessions that may need a derived inbox: live native sessions and open handovers.

    Live sessions come from Hearting's own records (``peer_address.native_sessions``:
    shell attachments and exact native session records), never a terminal census."""
    from harness_capabilities import HARNESSES, parent_completion
    from dispatch_seat_handover import _registered_parent_harness
    import peer_address

    found = set()
    try:
        sessions = peer_address.native_sessions()
    except Exception:  # noqa: BLE001 - an unreadable table names no live session
        sessions = []
    for row in sessions:
        harness, value = row.get("harness"), row.get("sid")
        if (harness in HARNESSES and isinstance(value, str) and value
                and parent_completion(harness).get("carrier")):
            found.add((harness, value))
    jobs = root / "jobs.log"
    from dispatch_supervision import _rows
    try:
        for status, meta in _rows(jobs).values():
            if status not in {"open", "running"}:
                continue
            sid = str(meta.get("parent_sid") or "")
            harness = _registered_parent_harness(meta)
            if sid and harness in HARNESSES and parent_completion(harness).get("carrier"):
                found.add((harness, sid))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        pass
    return found


def _prefill_recipient_inbox(root, harness, session_id, *, deadline_ns,
                             expected_root_identity=None):
    """Derive and publish one recipient inbox from the real recipient scopes.

    Only open records are named: ``delivery_ids`` is the open-delivery list, so
    a terminal record under a granted scope must not make a carrier believe it
    still owes a delivery. The per-record read also honours the shared pass
    deadline; a pass cut here publishes nothing and retries next pass.
    """
    import dispatch_pending_delivery as pending
    import dispatch_recipient_inbox as inbox
    import dispatch_session_sweep as sweep
    from dispatch_seat_handover import storage_recipients
    from peer_read_context import PeerReadContext

    read_context = PeerReadContext()
    # Capture the generation before the scope enumeration; an edge writer that
    # completes afterwards makes the payload recheck refuse this publication.
    edge_generation = inbox.read_edge_generation(root)
    with sweep._registry_read_scope():
        scopes = storage_recipients(
            session_id, harness=harness, read_context=read_context,
            deadline_ns=deadline_ns)
        complete = bool(getattr(scopes, "complete", True))
        open_entries = []
        for storage_key, _allowed in scopes:
            try:
                directory = pending.record_directory(root, storage_key)
                entries = list(directory.iterdir())
            except FileNotFoundError:
                continue
            except (OSError, pending.PendingDeliveryError):
                complete = False
                continue
            for entry in entries:
                if not entry.name.startswith("delivery-") or entry.suffix != ".json":
                    continue
                if time.monotonic_ns() >= deadline_ns:
                    complete = False
                    break
                try:
                    info = entry.stat()
                except OSError:
                    complete = False
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                try:
                    record = pending.read(root, storage_key, entry.stem)
                except (OSError, pending.PendingDeliveryError):
                    complete = False
                    continue
                if not record or record.get("state") not in pending.OPEN_STATES:
                    # A terminal record is never owed; an unreadable one leaves
                    # the pass incomplete so no partial list is published.
                    continue
                open_entries.append((storage_key, entry.stem, info.st_mtime_ns))
            if not complete:
                break
        if not complete:
            return None
        payload = sweep._inbox_payload(
            root, session_id, harness, scopes, open_entries,
            edge_generation=edge_generation,
            folded_edge_digest=sweep.scopes_edge_digest(scopes))
    inbox.write_inbox(root, harness, session_id, payload,
                      expected_root_identity=expected_root_identity)
    return payload


def _prefill_live_inboxes(root, *, deadline_ns, expected_root_identity=None):
    """Refresh a missing/stale inbox for live sessions only, bounded per pass.

    The periodic reconciliation compares the published ``folded_edge_digest``
    with a fresh graph observation, so a confirmed edge writer whose best-effort
    generation bump failed is still repaired within this prefill window. The
    hook's current path stays stat-only and never reads the graph.
    """
    import dispatch_recipient_inbox as inbox

    result = {"checked": 0, "written": 0, "skipped": 0}
    live = sorted(_live_recipients(root))
    if not live:
        return result
    folded_edge_digest = None
    if time.monotonic_ns() < deadline_ns:
        try:
            from dispatch_seat_handover import lineage_graph
            from peer_read_context import PeerReadContext
            # The runner keeps full observations by contract; only the sweep's
            # effect groups opt into the compact read.
            graph = lineage_graph(read_context=PeerReadContext())
            folded_edge_digest = inbox.edge_digest(getattr(graph, "edges", ()) or ())
        except (OSError, ValueError, TypeError):
            folded_edge_digest = None  # unknown edges keep the generation key
        if time.monotonic_ns() >= deadline_ns:
            # The full-graph read is not deadline-bounded; a pass that spent its
            # budget on it leaves every session to the next pass.
            result["skipped"] += len(live)
            return result
    for harness, session_id in live:
        if time.monotonic_ns() >= deadline_ns:
            result["skipped"] += 1
            continue
        try:
            import session_resume
            session_resume.register(harness, session_id, source="resume", jobs=Path(root) / "jobs.log")
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            pass  # Existing pending state and the Fleet attention view remain.
        recorded = inbox.read_inbox(root, harness, session_id)
        current = False
        if recorded is not None:
            try:
                current = inbox.is_current(recorded, root,
                                           folded_edge_digest=folded_edge_digest)
            except (OSError, ValueError, TypeError):
                current = False
        if current:
            continue
        result["checked"] += 1
        try:
            written = _prefill_recipient_inbox(
                root, harness, session_id, deadline_ns=deadline_ns,
                expected_root_identity=expected_root_identity)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            written = None
        if written is None:
            result["skipped"] += 1
        else:
            result["written"] += 1
    return result


_REF_LOCK_NAME = re.compile(r"\.lock-[0-9a-f]{32}")


def _peer_history_order(name):
    # Rows first, then ref lock files: one cursor walks both, and an older
    # row-only cursor keeps its place.
    return (name.startswith(".lock-"), name)


def _prune_received_peer_history(*, now=None, deadline_ns=None, start_after=None):
    """Drop received queue rows past the 7-day history window; transfers stay.

    The same walk then removes ``.lock-<ref>`` files whose row no longer exists
    and that were created before the window (`remove_rowless_ref_lock`); no
    other code deletes them. Preparation locks are left alone: a ``prepare-``
    key is a hash of sender, recipient and body that any later identical send
    takes again, so no row or state says it is finished. Current releases
    remove their own preparation lock directories on release.

    ``start_after`` resumes the sorted walk at the next file name. A pass cut
    off by the shared deadline returns ``complete=False`` and the last examined
    name as ``cursor`` so the next pass continues instead of restarting the
    same prefix; a completed walk returns ``cursor=None``.
    """
    now = time.time() if now is None else float(now)
    cutoff = now - PEER_HISTORY_RETENTION_SECONDS
    result = {"removed": 0, "locks_removed": 0, "examined": 0, "cursor": None,
              "complete": True}
    try:
        pending_dir = peer_message._pending_dir()
        with os.scandir(pending_dir) as entries:
            listed = [entry.name for entry in entries]
    except OSError:
        return result
    rows = {name for name in listed if name.endswith(".json")}
    names = sorted((name for name in listed
                    if name in rows or _REF_LOCK_NAME.fullmatch(name)),
                   key=_peer_history_order)
    start_after = start_after if isinstance(start_after, str) and start_after else None
    last = start_after
    for name in names:
        if (start_after is not None
                and _peer_history_order(name) <= _peer_history_order(start_after)):
            continue
        if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
            result["complete"] = False
            result["cursor"] = last
            return result
        result["examined"] += 1
        last = name
        if name.startswith(".lock-"):
            ref = name[len(".lock-"):]
            if ref + ".json" not in rows and peer_message.remove_rowless_ref_lock(
                    pending_dir, ref, created_before=cutoff):
                result["locks_removed"] += 1
            continue
        path = pending_dir / name
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(row, dict) or row.get("state") != "received":
            continue
        received = row.get("received_at")
        if (not isinstance(received, (int, float)) or isinstance(received, bool)
                or received > cutoff):
            continue
        try:
            os.unlink(path)
        except OSError:
            continue
        rows.discard(name)
        result["removed"] += 1
    if result["removed"]:
        try:
            import dispatch_recipient_inbox as inbox
            peer_message.rebuild_recipient_index(
                inbox.dispatch_inbox_root(), time_budget_s=0.5)
        except (OSError, ValueError, TypeError, RuntimeError):
            pass
    return result


def _publish_lineage_rows_duty(root, *, deadline_ns=None,
                               expected_root_identity=None):
    """Keep the compact lineage observation published for the effect path.

    Without this first/periodic publication a fresh install has no
    ``lineage-rows.json`` and every prompt falls back to reading all ledger
    bytes; the published file is what makes the compact effect path real. A
    changed ledger still re-reads that ledger, so a stale file is never
    authority.
    """
    import dispatch_seat_handover as handover
    import dispatch_recipient_inbox as inbox

    state = peer_obligations.read_runner_duty_state(root, "lineage-rows")
    if (inbox.read_lineage_rows(root) is not None
            and not peer_obligations.runner_duty_due(state, LINEAGE_ROWS_DUE_SECONDS)):
        return {"published": False, "due": False}
    if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
        return {"published": False, "due": True, "deferred": True}
    try:
        counts = handover.publish_lineage_rows(
            root, expected_root_identity=expected_root_identity)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return {"published": False, "due": True, "deferred": True}
    peer_obligations.write_runner_duty_state(
        root, "lineage-rows", {
            "last_pass_ns": time.time_ns(),
            "ledgers": counts.get("ledgers", 0),
            "rows": counts.get("rows", 0),
            "skipped": counts.get("skipped", 0)},
        expected_root_identity=expected_root_identity)
    return {"published": True, "due": True, **counts}


def _publish_peer_indexes(root, *, deadline_ns=None, expected_root_identity=None):
    """Publish each session's pending-row index (rows it receives and rows it
    sent) so the Stop and context hooks read no full history.

    The Stop header retry is bounded by its own 3 s budget and may skip, so the
    runner is the intended steady-state publisher. A pass cut off by its budget
    publishes nothing (the builder refuses partial indexes) and retries next
    pass instead of waiting for the 15-minute due window.
    """
    state = peer_obligations.read_runner_duty_state(root, "peer-index")
    if not peer_obligations.runner_duty_due(state, PEER_INDEX_DUE_SECONDS):
        return {"published": False, "due": False}
    if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
        return {"published": False, "due": True, "deferred": True}
    peer_root = peer_message.peer_state_root()
    if (expected_root_identity is not None
            and Path(peer_root).resolve(strict=False)
                == Path(root).resolve(strict=False)
            and peer_obligations.state_root_identity(peer_root)
                != tuple(expected_root_identity)):
        # The runner's own root also carries recipient indexes in this layout;
        # a vanished root must not be recreated by the publisher either.
        return {"published": False, "due": True, "deferred": True}
    remaining = PEER_INDEX_TIME_BUDGET_S
    if deadline_ns is not None:
        remaining = min(remaining, max(
            0.0, (deadline_ns - time.monotonic_ns()) / 1_000_000_000))
    counts = peer_message.rebuild_recipient_index(
        peer_root, time_budget_s=remaining)
    complete = bool(counts.get("complete"))
    peer_obligations.write_runner_duty_state(
        root, "peer-index", {
            "last_pass_ns": time.time_ns() if complete else 0,
            "scanned": counts.get("scanned", 0),
            "recipients": counts.get("recipients", 0),
            "rows": counts.get("rows", 0),
            "complete": complete},
        expected_root_identity=expected_root_identity)
    return {"published": complete, "due": True, "complete": complete,
            "scanned": counts.get("scanned", 0),
            "recipients": counts.get("recipients", 0)}


def _lineage_components(graph):
    """Connected components over the confirmed same-harness lineage edges."""
    adjacency = {}
    for harness, origin, target in graph.edges:
        left, right = (harness, origin), (harness, target)
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)
    seen, components = set(), []
    for vertex in sorted(adjacency):
        if vertex in seen:
            continue
        stack, component = [vertex], set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            seen.add(current)
            stack.extend(adjacency.get(current, ()))
        components.append(frozenset(component))
    return sorted(components, key=lambda item: tuple(sorted(item)))


def _sid_has_open_notice(root, session_id):
    import dispatch_pending_delivery as pending
    try:
        directory = pending.record_directory(root, session_id)
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return False
    except (OSError, pending.PendingDeliveryError):
        return True  # unknown never authorizes retirement
    for entry in entries:
        if not entry.name.startswith("delivery-") or entry.suffix != ".json":
            continue
        try:
            record = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        if isinstance(record, dict) and record.get("state") in pending.OPEN_STATES:
            return True
    return False


def _component_has_open_binding(graph, component, jobs):
    open_attempts = set()
    from dispatch_supervision import _rows
    try:
        for status, meta in _rows(jobs).values():
            if status in {"open", "running"}:
                open_attempts.add(meta.get("attempt_id"))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return True  # unknown never authorizes retirement
    for ledger_key in {key for vertex in component
                       for key in graph.ledger_keys.get(vertex, ())}:
        for row in graph.ledger_rows.get(ledger_key, ()):
            for binding in row.get("bindings") or ():
                attempt = binding.get("attempt") if isinstance(binding, dict) else None
                if attempt and attempt in open_attempts:
                    return True
    return False


def _component_has_role_rows(graph, component, store):
    ledgers = {key for vertex in component for key in graph.ledger_keys.get(vertex, ())}
    for ledger_key in ledgers:
        for row in graph.ledger_rows.get(ledger_key, ()):
            if row.get("event") in _ROLE_ROW_EVENTS:
                return True
            if _ROLE_ROW_KEYS.intersection(row):
                return True
    sids = {sid for _harness, sid in component}
    try:
        duties = store.list(kinds={"watch", "steward-check"})
    except (OSError, ValueError, RuntimeError, peer_obligations.ObligationError):
        return True
    for duty in duties:
        encoded = json.dumps((duty.get("intent") or {}).get("identity") or {}, sort_keys=True)
        if any(sid in encoded for sid in sids):
            return True
    return False


def _component_last_activity(graph, component):
    """Newest ledger row timestamp, or ``None`` when any row is unreadable."""
    latest = None
    for ledger_key in {key for vertex in component
                       for key in graph.ledger_keys.get(vertex, ())}:
        for row in graph.ledger_rows.get(ledger_key, ()):
            stamp = row.get("ts")
            if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
                return None
            latest = stamp if latest is None else max(latest, stamp)
    return latest


def _purge_retired_archives(*, now=None):
    """Remove archive ledgers after their 30-day retention."""
    import session_tidy as tidy

    now = time.time() if now is None else float(now)
    archive = tidy.state_root() / RETIRE_ARCHIVE_DIRECTORY
    result = {"removed": 0}
    if not archive.is_dir():
        return result
    try:
        entries = sorted(archive.glob("*.jsonl"))
    except OSError:
        return result
    for path in entries:
        try:
            info = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        if now - info.st_mtime < RETIRE_ARCHIVE_SECONDS:
            continue
        try:
            os.unlink(path)
        except OSError:
            continue
        result["removed"] += 1
    return result


def _publish_steward_relations(root, *, force=False, expected_root_identity=None):
    """Publish the role relation the Stop header may reuse instead of a live walk.

    Display-only: the file feeds `fleet.herdr_projection.is_steward` on the Stop
    path, which falls back to the live read when it is missing. Authority paths
    and Fleet never read it. A 60 s due keeps header evidence fresh, and an
    edge-generation change (including every retirement) republishes at once.
    """
    import dispatch_recipient_inbox as inbox
    from dispatch_seat_handover import resolved_steward_relations

    state = peer_obligations.read_runner_duty_state(root, "steward-relations")
    generation = inbox.read_edge_generation(root)
    due = (force or state.get("edge_generation") != generation
           or peer_obligations.runner_duty_due(state, STEWARD_RELATIONS_DUE_SECONDS))
    if not due:
        return {"published": False, "due": False, "relations": 0}
    relations = resolved_steward_relations(include_released=True)
    inbox.write_steward_relations(
        root, relations, expected_root_identity=expected_root_identity)
    peer_obligations.write_runner_duty_state(
        root, "steward-relations", {
            "last_pass_ns": time.time_ns(), "edge_generation": generation,
            "relations": len(relations)},
        expected_root_identity=expected_root_identity)
    return {"published": True, "due": True, "relations": len(relations)}


def _runner_lock_current(store, lock_fd):
    """The lock this runner holds is still the state root's runner lock file."""
    try:
        held = os.fstat(lock_fd)
        current = os.stat(store.root / peer_obligations.RUNNER_LOCK_NAME)
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)


def _acquire_ensure_obligations_lock(store, *, retain_directory=False):
    """Try the shared recovery lock without waiting or unlinking its inode."""
    if not store.ensure_directory():
        return None
    directory_fd = store._obligations_fd(create=False)
    if directory_fd is None:
        return None
    fd = None
    acquired = False
    try:
        try:
            entry = os.stat("ensure-obligations.lock", dir_fd=directory_fd,
                            follow_symlinks=False)
        except FileNotFoundError:
            entry = None
        if entry is not None and stat.S_ISLNK(entry.st_mode):
            return None
        fd = os.open("ensure-obligations.lock",
                     os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                     0o600, dir_fd=directory_fd)
        if not store._file_binding_is_current(
                directory_fd, fd, "ensure-obligations.lock"):
            return None
        # This descriptor belongs only to this short call. A detached observer
        # or runner must never keep the shared exclusion alive after we exit.
        os.set_inheritable(fd, False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        if not store._file_binding_is_current(
                directory_fd, fd, "ensure-obligations.lock"):
            return None
        acquired = True
        if retain_directory:
            store._ensure_recovery_directory_fd = directory_fd
            directory_fd = None
        return fd
    except Exception:
        raise
    finally:
        if fd is not None and not acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        if directory_fd is not None:
            os.close(directory_fd)


def cmd_ensure_obligations(_args):
    """Request the shared runner and return without repeating its global work."""
    store = peer_obligations.ObligationStore(allow_state_root_creation=True)
    observed = peer_obligations.state_root_identity_from_environ()
    if observed is not None and store._state_root_identity != observed:
        # The producer observed this root, but it was removed or replaced
        # before this detached writer started. Stop instead of recreating it;
        # only a genuinely absent root (fresh install) takes the create path.
        return 0
    if not store._initialize_state_root_for_producer(create_parents=False):
        return 0
    # The explicit lifecycle producer has completed the sole root-creation
    # step. Its Store is now observation-only through the runner request.
    store._allow_state_root_creation = False
    try:
        lock_fd = _acquire_ensure_obligations_lock(store, retain_directory=True)
    except Exception:
        return 0
    if lock_fd is None:
        return 0
    try:
        try:
            _steward_check_module().reconcile_self(store)
        except Exception:
            pass
        try:
            lifecycle_root = getattr(_args, "lifecycle_root", None)
            peer_obligations.ensure_normal_runner(
                lifecycle_root=lifecycle_root, reconciliation_required=True,
                _store=store, _recovery_lock_fd=lock_fd)
        except Exception:
            pass
        return 0
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)
        directory_fd = store._ensure_recovery_directory_fd
        store._ensure_recovery_directory_fd = None
        if directory_fd is not None:
            os.close(directory_fd)


def _prompt_carrier_note(ref, target, outcome):
    """Say on stderr, in one plain line, that a message not received yet is still carried.

    A queued or unverified send with a live message obligation is delivered by the
    obligation runner once the target can take input, and an identical repeat reuses the
    same ref; agents still re-sent such messages by hand. Nothing is said when no
    obligation holds the ref. stderr only: stdout keeps the one `prompted=` line its
    readers parse, and in-process callers keep their return codes.
    """
    if outcome == "true" or not ref:
        return
    try:
        duty = peer_obligations.ObligationStore().get(_message_obligation_id(ref))
    except Exception:  # noqa: BLE001 -- an unreadable store adds no line
        return
    if not isinstance(duty, dict) or duty.get("state") in {"complete", "cancelled"}:
        return
    try:
        sys.stderr.write(f"next: nothing to resend - the runtime delivers ref={ref} to {target} "
                         "once it can take input; the same body sent again keeps this ref\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass

# --- clear: the one typed /compact of a session-tidy booking (name kept for callers) ---

_CLEAR_EXIT = {"true": 0, "skipped": 3, "queued": 3, "failed": 1, "unverified": 5}
_CLEAR_LEDGER_STATUS = {"true": "sent", "failed": "failed", "skipped": "unknown", "queued": "unknown", "unverified": "unknown"}
_RULE_LINE = re.compile(r"^[\s\u2500-\u257f]*\u2500{8,}[\s\u2500-\u257f]*$")
_OPENCODE_HOME_PLACEHOLDER = "askanything"


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


def _codex_rollout_exists(thread_id):
    """True when Codex has written a rollout for `thread_id` -- it does so with the thread's
    first message, not at `/clear` (measured on codex 0.160.0)."""
    import glob as _glob
    pattern = os.path.join(_codex_home_dir(), "sessions", "*", "*", "*",
                           f"rollout-*-{_glob.escape(thread_id)}.jsonl")
    return bool(_glob.glob(pattern))


def _native_trust_reason(harness, lines):
    """Name an observed native folder trust screen without interacting with it."""
    return "native-trust-wait" if _blocking_screen(harness, lines) == "native-trust-wait" else None


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
