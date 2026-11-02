"""Bounded retention for exact Hearting dispatch logs under the jobs state root.

This pass shares terminal and consumer protection with worker-home retention.
Its ownership boundary is narrower: only a regular, single-link file in the
canonical state-root ``logs`` directory with an exact registry binding is ever
eligible for unlink.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import time

from dispatch_contract import (
    DispatchContractError,
    dispatch_state_root,
    validate_dispatch_log_dir,
)
from dispatch_retention import (
    QUARANTINE_PREFIX,
    recover_quarantines,
    unlink_same_file,
)

RETENTION_NS = 14 * 86400 * 1_000_000_000
MAX_LOG_BYTES = 2 * 1024 * 1024 * 1024
DUE_SECONDS = 300
# While the last pass left removable files behind, the next pass may start sooner.
BACKLOG_DUE_SECONDS = 60
EXAMINE_LIMIT = 64
MAX_DIRECTORY_ENTRIES = 50000
MAX_PASS_SECONDS = 10
REMOVE_LIMIT = 16
STATE_NAME = "dispatch-log-retention.json"
LOCK_NAME = "dispatch-log-retention.lock"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_uid)


def _terminal_ns(value):
    from datetime import datetime, timezone

    try:
        ended = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        delta = ended.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return ((delta.days * 86400 + delta.seconds) * 1_000_000_000
                + delta.microseconds * 1000)
    except (AttributeError, OverflowError, TypeError, ValueError):
        return None


class _LogLayout:
    def __init__(self, root: Path):
        self.fds = []
        self.root_fd = os.open(root, _DIR_FLAGS)
        self.fds.append(self.root_fd)
        if os.fstat(self.root_fd).st_uid != os.geteuid():
            self.close()
            raise PermissionError("dispatch-state-root-owner-mismatch")
        self.logs_fd = os.open("logs", _DIR_FLAGS, dir_fd=self.root_fd)
        self.fds.append(self.logs_fd)
        if os.fstat(self.logs_fd).st_uid != os.geteuid():
            self.close()
            raise PermissionError("dispatch-log-dir-owner-mismatch")

    def close(self):
        while self.fds:
            with contextlib.suppress(OSError):
                os.close(self.fds.pop())

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


@contextlib.contextmanager
def _advisory_lock(layout):
    fd = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=layout.root_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_uid != os.geteuid():
            yield False
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_state(layout):
    try:
        fd = os.open(STATE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=layout.root_fd)
    except FileNotFoundError:
        return {"last_pass_ns": 0, "cursor": ""}
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(16385)
    if len(raw) > 16384:
        return {"last_pass_ns": 0, "cursor": ""}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return {"last_pass_ns": 0, "cursor": ""}
    if (not isinstance(value, dict) or not isinstance(value.get("last_pass_ns"), int)
            or not isinstance(value.get("cursor", ""), str)):
        return {"last_pass_ns": 0, "cursor": ""}
    return value


def _write_state(layout, state):
    temp = ".dispatch-log-retention.%s.tmp" % secrets.token_hex(8)
    data = json.dumps(state, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=layout.root_fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, STATE_NAME, src_dir_fd=layout.root_fd, dst_dir_fd=layout.root_fd)
        os.fsync(layout.root_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp, dir_fd=layout.root_fd)
        raise


def _bound_name(jobs: Path, root: Path, log_file: str):
    if not isinstance(log_file, str) or not os.path.isabs(log_file):
        return None
    if os.path.normpath(log_file) != log_file:
        return None
    path = Path(log_file)
    logs = root / "logs"
    try:
        validated = validate_dispatch_log_dir(jobs, logs)
    except (DispatchContractError, OSError, ValueError):
        return None
    if (validated != logs or path.parent != logs or path.name in ("", ".", "..",
            "dispatch-session-sweep.log", "dispatch-pending-delivery.log")
            or ".segment." in path.name or path.name.endswith(".lock")):
        return None
    return path.name


def _prompt_name_for_log_name(log_name, harness):
    """Derive only the prompt sibling written by a registered adapter."""
    if harness not in {"claude", "codex", "opencode"}:
        return None
    suffix = f".{harness}.jsonl"
    if not isinstance(log_name, str) or not log_name.endswith(suffix):
        return None
    return log_name[:-len(".jsonl")] + ".prompt.txt"


def _prompt_name_for_row(jobs, root, attempt, row):
    if row.meta.get("attempt_id") != attempt:
        return None
    harness = row.meta.get("harness")
    log_name = _bound_name(jobs, root, row.meta.get("log_file"))
    if log_name is None or harness not in {"claude", "codex", "opencode"}:
        return None
    slug = row.fields[4] if len(row.fields) > 4 else ""
    if not slug or slug in (".", "..") or os.path.basename(slug) != slug:
        return None
    writer_names = {
        f"{slug}.{attempt}.{harness}.jsonl",
        f"{slug}.{harness}.jsonl",
    }
    if log_name not in writer_names:
        return None
    return _prompt_name_for_log_name(log_name, harness)


def _unknown_prompt_names(jobs, root, unknown_log_refs):
    names = set()
    for log_file in unknown_log_refs:
        log_name = _bound_name(jobs, root, log_file)
        if log_name is None:
            continue
        for harness in ("claude", "codex", "opencode"):
            prompt_name = _prompt_name_for_log_name(log_name, harness)
            if prompt_name is not None:
                names.add(prompt_name)
                break
    return names


def _log_protection(ctx, attempt, *, probe_processes):
    """Shared attempt decision plus the log-only failing-review consumer."""
    from dispatch_retention import Protection
    from worker_home_retention import _protections, _route_state

    # Attempt logs remain route evidence for a live workflow even when the
    # attempt's separate worker HOME has no remaining consumer.
    verdict = _protections(ctx, "", attempt, probe_processes=probe_processes,
                           protect_open_route=True)
    row = ctx.snapshot.rows.get(attempt)
    if (verdict.kind == "eligible" and row is not None
            and row.meta.get("worker_type") == "review"
            and row.meta.get("failure_class") == "fail"):
        state, _activity = _route_state(ctx.jobs, row.meta)
        if state != "closed":
            return Protection("keep", "review-consumer-open")
    return verdict


def _unlink_if_same(layout, name, expected):
    """Remove through a unique quarantine, preserving replacements at name."""
    return unlink_same_file(layout.logs_fd, name, expected, _stamp)


@contextlib.contextmanager
def _fresh_eligible(jobs, root, layout, candidate_name, expected_rows,
                    expected_stamp, now_ns, candidate_kind, prepared, deadline, expected_settled):
    from dispatch_contract import process_table_scan_scope
    from worker_home_retention import _Context, _Snapshot, _declared_index

    body_entered = False
    try:
        snapshot = _Snapshot(jobs)
        if candidate_kind == "log":
            log_file = str(root / "logs" / candidate_name)
            if log_file in snapshot.unknown_log_refs:
                yield False
                return
            current_refs = snapshot.log_refs.get(log_file, {})
            if set(current_refs) != set(expected_rows):
                yield False
                return
        else:
            if candidate_name in _unknown_prompt_names(jobs, root, snapshot.unknown_log_refs):
                yield False
                return
            current_refs = {}
            for attempt, row in snapshot.rows.items():
                if _prompt_name_for_row(jobs, root, attempt, row) == candidate_name:
                    current_refs[attempt] = row
            if set(current_refs) != set(expected_rows):
                yield False
                return
        for attempt, old in expected_rows.items():
            current = snapshot.rows.get(attempt)
            current_ref = current_refs.get(attempt)
            if (current is None or current_ref is None or current.raw != old.raw
                    or current_ref.raw != old.raw):
                yield False
                return
            if candidate_kind == "log" and current.meta.get("log_file") != log_file:
                yield False
                return
            if (candidate_kind == "prompt"
                    and _prompt_name_for_row(jobs, root, attempt, current) != candidate_name):
                yield False
                return
        ctx = _Context(jobs, root, layout, snapshot, now_ns)
        ctx.by_key = _declared_index(snapshot, root, ctx.workers_real)
        ctx.delivery_subject_id = candidate_name
        ctx.observation = prepared.observation
        ctx.delivery = prepared.delivery
        ctx.delivery_records = prepared.delivery_records
        ctx.discovery_stamps = prepared.discovery_stamps
        ctx.group_stamps = prepared.group_stamps
        ctx.delivery_groups = prepared.delivery_groups
        from worker_home_retention import (_DEPENDENCIES, _fresh, _registry_lock_until, _admission_scan,
                                           _late_delivery_change, _late_candidate_delivery_change)
        watched = {}
        with process_table_scan_scope():
            for attempt in expected_rows:
                evidence = _DEPENDENCIES.set(watched)
                try:
                    verdict = _log_protection(ctx, attempt, probe_processes=True)
                finally:
                    _DEPENDENCIES.reset(evidence)
                if verdict.kind != "eligible":
                    yield False
                    return
                row = snapshot.rows[attempt]
                terminal = _terminal_ns(row.fields[0])
                if (terminal is None or max(terminal, verdict.activity_ns) > now_ns
                        or max(terminal, verdict.activity_ns) > expected_settled):
                    yield False
                    return
        current_stat = os.stat(candidate_name, dir_fd=layout.logs_fd, follow_symlinks=False)
        if not (_stamp(current_stat) == expected_stamp and stat.S_ISREG(current_stat.st_mode)
                and current_stat.st_nlink == 1 and current_stat.st_uid == os.geteuid()):
            yield False
            return
        import dispatch_attempt_reference as reference
        from worker_home_retention import _candidate_related_attempts
        related = set()
        for attempt in expected_rows:
            related.update(_candidate_related_attempts(root, snapshot, attempt))
        representative = next(iter(expected_rows))
        early = _late_delivery_change(ctx, representative, deadline)
        if early is None:
            early = _late_candidate_delivery_change(ctx, representative, deadline)
        if early is not None:
            if early == "deferred":
                prepared.admission_reason = "delivery-refresh-budget"
            yield False
            return
        baseline = ctx.candidate_delivery_baseline
        registry_stamp = os.stat(jobs, follow_symlinks=False)
        registry = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if registry(registry_stamp) != snapshot.registry_snapshot:
            yield False
            return
        # The actual quarantine/unlink executes inside this yield. Process
        # census and broad discovery above never run while jobs.log.lock is held.
        with _registry_lock_until(jobs, deadline) as admitted:
            if admitted is False:
                yield False
                return
            with reference.admission(root, related, blocking=False) as admitted:
                if not admitted or not _fresh(watched) or registry(os.stat(jobs)) != snapshot.registry_snapshot:
                    yield False
                    return
                from worker_home_retention import _lease_state
                from dispatch_summary import home_consumer_evidence
                for row in expected_rows.values():
                    if (_lease_state(jobs, row.meta) in ("held", "unknown")
                            or home_consumer_evidence(row.meta)[0] in ("live", "unknown")):
                        yield False
                        return
                ctx.candidate_delivery_baseline = baseline
                # The reference admission is held through the unlink: look once
                # for what was published since the refresh above.
                ctx.admission_scan = _admission_scan(ctx, deadline)
                late = (ctx.admission_scan if ctx.admission_scan is not None
                        else _late_candidate_delivery_change(ctx, representative, deadline))
                if late is not None:
                    if late == "deferred":
                        prepared.admission_reason = "delivery-reread-budget"
                    yield False
                    return
                current_stat = os.stat(candidate_name, dir_fd=layout.logs_fd, follow_symlinks=False)
                body_entered = True
                yield _stamp(current_stat) == expected_stamp
    except Exception:  # noqa: BLE001 -- unreadable evidence keeps the bytes
        if body_entered:
            raise
        yield False
        return


def sweep(jobs, *, now_ns=None, force=False):
    """Run one best-effort pass; unknown evidence and failures always keep files."""
    started = time.monotonic()
    now_ns = time.time_ns() if now_ns is None else int(now_ns)
    try:
        jobs = Path(jobs).resolve(strict=False)
        root = dispatch_state_root(jobs)
        log_dir = validate_dispatch_log_dir(jobs, root / "logs")
        if log_dir != root / "logs":
            return {"skipped": "log-dir-noncanonical"}
        log_info = os.stat(log_dir, follow_symlinks=False)
        if not stat.S_ISDIR(log_info.st_mode):
            return {"skipped": "log-dir-not-directory"}
        with _LogLayout(root) as layout:
            with _advisory_lock(layout) as locked:
                if not locked:
                    return {"skipped": "busy"}
                state = _read_state(layout)
                last = state.get("last_result")
                gap = (BACKLOG_DUE_SECONDS if isinstance(last, dict) and last.get("more")
                       else DUE_SECONDS)
                if (not force and state["last_pass_ns"] <= now_ns
                        and now_ns - state["last_pass_ns"] < gap * 1_000_000_000):
                    return {"skipped": "not-due"}
                return _pass(jobs, root, layout, state, now_ns, started)
    except FileNotFoundError:
        return {"skipped": "logs-missing"}
    except (OSError, DispatchContractError) as exc:
        return {"skipped": "io-error", "error": type(exc).__name__}
    except Exception as exc:  # noqa: BLE001
        return {"skipped": "error", "error": type(exc).__name__}


def _pass(jobs, root, layout, state, now_ns, started):
    from dispatch_contract import process_table_scan_scope
    from worker_home_retention import _Context, _Snapshot

    recovery = recover_quarantines(layout.logs_fd)
    names = []
    with os.scandir(layout.logs_fd) as listing:
        for entry in listing:
            if (entry.name.startswith(QUARANTINE_PREFIX)
                    and len(entry.name) == len(QUARANTINE_PREFIX) + 32
                    and all(ch in "0123456789abcdef"
                            for ch in entry.name[len(QUARANTINE_PREFIX):])):
                continue
            names.append(entry.name)
            if len(names) > MAX_DIRECTORY_ENTRIES:
                result = {"skipped": "directory-limit", "observed_entries": len(names)}
                state.update(last_pass_ns=now_ns, last_result=result)
                _write_state(layout, state)
                return result
    names.sort()
    total_bytes = 0
    stamps = {}
    for name in names:
        try:
            info = os.stat(name, dir_fd=layout.logs_fd, follow_symlinks=False)
        except OSError:
            continue
        stamps[name] = _stamp(info)
        total_bytes += max(0, info.st_size)
    total_bytes += recovery["pending_bytes"]

    try:
        snapshot = _Snapshot(jobs)
    except Exception as exc:  # noqa: BLE001
        result = {"skipped": "registry-unreadable", "error": type(exc).__name__,
                  "total_bytes": total_bytes, "removed": 0}
        state.update(last_pass_ns=now_ns, last_result=result)
        _write_state(layout, state)
        return result

    # A path with any malformed or unidentifiable registry reference remains
    # protected even if another row happens to bind it correctly.
    rows_by_name = {}
    for log_file, rows in snapshot.log_refs.items():
        name = _bound_name(jobs, root, log_file)
        if name is None:
            continue
        rows_by_name.setdefault(name, {}).update(rows)
    prompt_rows_by_name = {}
    for attempt, row in snapshot.rows.items():
        name = _prompt_name_for_row(jobs, root, attempt, row)
        if name is not None:
            prompt_rows_by_name.setdefault(name, {})[attempt] = row
    unknown_names = {
        name for path in snapshot.unknown_log_refs
        if (name := _bound_name(jobs, root, path)) is not None
    }
    unknown_prompt_names = _unknown_prompt_names(jobs, root, snapshot.unknown_log_refs)

    candidate_rows = dict(rows_by_name)
    candidate_kinds = {name: "log" for name in rows_by_name}
    for name, rows in prompt_rows_by_name.items():
        if name in candidate_rows:
            unknown_names.add(name)
            continue
        candidate_rows[name] = rows
        candidate_kinds[name] = "prompt"
    unknown_names.update(unknown_prompt_names)
    referenced_names = set(candidate_rows)
    ordered = sorted(referenced_names)
    cursor = state.get("cursor", "")
    after = [name for name in ordered if name > cursor]
    selected_names = (after + [name for name in ordered if name <= cursor])[:EXAMINE_LIMIT]

    protected_names = set()
    eligible = []
    unknown_bytes = 0
    reasons = {}
    examined_names = []
    removable = 0
    over_limit = total_bytes > MAX_LOG_BYTES
    ctx = _Context(jobs, root, layout, snapshot, now_ns)
    from worker_home_retention import _candidate_related_attempts
    saved = state.get("delivery_observation")
    if isinstance(saved, dict) and saved.get("started") and not saved.get("complete"):
        pending = {s for s in saved.get("subjects", []) if isinstance(s, str)}
        selected_names = list(dict.fromkeys(
            [name for name in ordered if name in pending] + selected_names))[:EXAMINE_LIMIT]
    subjects = []
    for name in selected_names:
        related = set()
        for attempt in candidate_rows[name]:
            related.update(_candidate_related_attempts(root, snapshot, attempt))
        subjects.append({"id": name, "related": sorted(related), "binding": None})
    ctx.configure_delivery(state, subjects, started + MAX_PASS_SECONDS, state_budget=12288)
    if ctx.observation is not None:
        cohort = {s["id"] for s in ctx.observation.subjects}
        selected_names = [name for name in selected_names if name in cohort]
    discovery_deferred = 0
    with process_table_scan_scope():
        for name in selected_names:
            if examined_names and time.monotonic() - started > MAX_PASS_SECONDS:
                break
            examined_names.append(name)
            expected_stamp = stamps.get(name)
            if expected_stamp is None:
                protected_names.add(name)
                unknown_names.add(name)
                continue
            mode, links = expected_stamp[2], expected_stamp[3]
            if (not stat.S_ISREG(mode) or links != 1 or expected_stamp[7] != os.geteuid()
                    or name in unknown_names):
                protected_names.add(name)
                unknown_names.add(name)
                continue
            rows = candidate_rows[name]
            ctx.delivery_subject_id = name
            if len(rows) == 0:
                protected_names.add(name)
                continue
            terminal_values = []
            all_eligible = True
            for attempt, row in rows.items():
                if row.meta.get("attempt_id") != attempt or row.meta.get("harness") not in {"claude", "codex", "opencode"}:
                    all_eligible = False
                    unknown_names.add(name)
                    break
                verdict = _log_protection(ctx, attempt, probe_processes=True)
                if verdict.kind != "eligible":
                    reasons[verdict.reason or verdict.kind] = reasons.get(verdict.reason or verdict.kind, 0) + 1
                    all_eligible = False
                    if verdict.kind == "deferred":
                        discovery_deferred += 1
                    if verdict.kind == "unknown":
                        unknown_names.add(name)
                    break
                terminal = _terminal_ns(row.fields[0])
                if terminal is None:
                    all_eligible = False
                    unknown_names.add(name)
                    break
                settled = max(terminal, verdict.activity_ns)
                if settled > now_ns:
                    all_eligible = False
                    unknown_names.add(name)
                    break
                terminal_values.append(settled)
            if all_eligible and terminal_values:
                settled = max(terminal_values)
                eligible.append((name, settled, expected_stamp, rows,
                                 candidate_kinds[name]))
                # Removal re-reads the registry per file, so it is the slow
                # step. Once enough removable files are in hand, stop
                # examining and spend the pass on removing them; the cursor
                # stays on the last examined name.
                if over_limit or now_ns - settled >= RETENTION_NS:
                    removable += 1
                    if removable >= REMOVE_LIMIT:
                        break
            else:
                protected_names.add(name)

    if examined_names:
        state["cursor"] = examined_names[-1]
    selected_set = set(examined_names)
    known_names = set(candidate_rows)
    unknown_names.update(set(stamps) - known_names)
    unknown_names.update(known_names - selected_set)
    unknown_bytes = sum(max(0, stamps[name][4]) for name in unknown_names if name in stamps)
    unknown_bytes += recovery["pending_bytes"]
    protected_bytes = sum(max(0, stamps[name][4]) for name in protected_names - unknown_names if name in stamps)
    eligible_bytes = sum(max(0, item[2][4]) for item in eligible)
    expired = [item for item in eligible if now_ns - item[1] >= RETENTION_NS]
    expired.sort(key=lambda item: (item[1], item[0]))
    selected = list(expired)
    projected = total_bytes - sum(max(0, item[2][4]) for item in selected)
    if projected > MAX_LOG_BYTES:
        for item in sorted((item for item in eligible if item not in selected), key=lambda x: (x[1], x[0])):
            if projected <= MAX_LOG_BYTES:
                break
            selected.append(item)
            projected -= max(0, item[2][4])
    removed = 0
    removed_bytes = 0
    deferred = 0
    # Examining and removing each get their own time budget, so a long
    # examination cannot leave eligible files undeleted.
    removal_started = time.monotonic()
    for name, settled, expected_stamp, rows, candidate_kind in selected:
        if removed >= REMOVE_LIMIT or time.monotonic() - removal_started > MAX_PASS_SECONDS:
            deferred += 1
            continue
        with _fresh_eligible(jobs, root, layout, name, rows, expected_stamp,
                             now_ns, candidate_kind, ctx, started + MAX_PASS_SECONDS, settled) as admitted:
            if not admitted:
                protected_names.add(name)
                unknown_names.add(name)
                deferred += 1
                if ctx.admission_reason:
                    reasons[ctx.admission_reason] = reasons.get(ctx.admission_reason, 0) + 1
                    ctx.admission_reason = None
                continue
            if _unlink_if_same(layout, name, expected_stamp):
                removed += 1
                removed_bytes += expected_stamp[4]
            else:
                protected_names.add(name)
                unknown_names.add(name)
                deferred += 1
    result = {
        "examined": len(examined_names), "eligible": len(eligible), "eligible_bytes": eligible_bytes,
        "removed": removed, "removed_bytes": removed_bytes, "deferred": deferred + discovery_deferred,
        "total_bytes": total_bytes, "protected_bytes": protected_bytes,
        "unknown_bytes": unknown_bytes,
        "quarantine_pending_files": recovery["pending_files"],
        "quarantine_pending_bytes": recovery["pending_bytes"],
        "over_limit_bytes": max(0, total_bytes - removed_bytes - MAX_LOG_BYTES),
        "kept_reasons": reasons,
        "more": int(removed >= REMOVE_LIMIT or deferred > 0),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    if ctx.observation is not None:
        ctx.observation.checkpoint()
        result.update(delivery_reads=ctx.observation.reads, delivery_entries=ctx.observation.visits,
                      delivery_metadata=ctx.observation.metadata)
        if len(json.dumps(ctx.observation.data).encode()) > 12288:
            state.pop("delivery_observation", None)
    state.update(last_pass_ns=now_ns, last_result=result)
    _write_state(layout, state)
    return result


def sweep_best_effort(jobs):
    try:
        return sweep(jobs)
    except Exception:  # noqa: BLE001 -- never change the caller's result
        return None
