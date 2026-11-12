#!/usr/bin/env python3
"""Hermetic same-process Fleet tick benchmark.

The versioned fixture freezes every collector input. Only the two benchmark clocks
remain live. The JSON evidence is intentionally verbose: timings are descriptive,
while normalized payload equality and raw-operation counts are the correctness gate.
"""
import argparse
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fleet import model, titles  # noqa: E402
from fleet.collectors import collect_all  # noqa: E402
from fleet.collectors import codex, dispatch, liveness, procscan, usage_api  # noqa: E402
from fleet.model import Session  # noqa: E402


BENCHMARK_SCHEMA = "fleet-tick-benchmark-v3"
FIXTURE_SCHEMA = "fleet-tick-fixture-v1"
# Same passthrough set as tools/run-tests.py's isolated child environment. Every
# other collector input is either a fixture path or absent.
HERMETIC_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ")
SESSION_FIELDS = (
    "harness", "pid", "proc_start", "cwd", "elapsed_min", "session_id", "title",
    "summary", "model", "effort", "ctx_pct", "active_context_tokens",
    "context_window_tokens", "session_input_tokens", "session_cached_input_tokens",
    "session_output_tokens", "rl_5h", "rl_7d", "rl_windows", "task_lifecycle",
    "liveness", "is_child", "orphan",
)
JOB_FIELDS = (
    "key", "slug", "source", "status", "harness", "profile", "cwd",
    "elapsed_min", "parent_sid", "parent_cwd", "parent_slug", "pid", "proc_start",
    "attempt_id", "route_id", "route_node", "dispatch_depth", "is_child",
    "liveness", "state_evidence",
)


def _canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _expand(value, fixture_root):
    if isinstance(value, str):
        return value.replace("$FIXTURE_ROOT", fixture_root)
    if isinstance(value, list):
        return [_expand(item, fixture_root) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, fixture_root) for key, item in value.items()}
    return value


def _normalize(value, fixture_root):
    if isinstance(value, str):
        return (
            value.replace(fixture_root, "$FIXTURE_ROOT")
            if fixture_root else value
        )
    if isinstance(value, list):
        return [_normalize(item, fixture_root) for item in value]
    if isinstance(value, tuple):
        return [_normalize(item, fixture_root) for item in value]
    if isinstance(value, dict):
        return {
            key: _normalize(value[key], fixture_root)
            for key in sorted(value)
        }
    return value


def _load_fixture(path):
    with open(path, encoding="utf-8") as handle:
        fixture = json.load(handle)
    required = {
        "fixture_schema", "fixed_now", "runtime_homes", "sessions",
        "dispatch_process_rows", "proc_rollouts", "state_dbs", "rollouts",
        "jobs", "usage", "expected",
    }
    missing = sorted(required - set(fixture))
    if missing:
        raise ValueError("fixture missing fields: %s" % ", ".join(missing))
    if fixture.get("fixture_schema") != FIXTURE_SCHEMA:
        raise ValueError("unsupported fixture schema: %r" % fixture.get("fixture_schema"))
    canonical = _canonical_bytes(fixture)
    return fixture, hashlib.sha256(canonical).hexdigest()


def _write_state_db(entry):
    home = entry["home"]
    os.makedirs(home, exist_ok=True)
    path = os.path.join(home, "state_5.sqlite")
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, agent_role TEXT, "
        "agent_path TEXT, agent_nickname TEXT, thread_source TEXT, source TEXT, "
        "created_at INTEGER, created_at_ms INTEGER, updated_at INTEGER, "
        "updated_at_ms INTEGER, rollout_path TEXT)"
    )
    connection.execute(
        "CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, "
        "child_thread_id TEXT, status TEXT)"
    )
    for thread in entry["threads"]:
        connection.execute(
            "INSERT INTO threads (id, title, agent_role, agent_path, agent_nickname, "
            "thread_source, source, created_at, created_at_ms, updated_at, "
            "updated_at_ms, rollout_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thread["id"], thread.get("title"), thread.get("agent_role"),
                thread.get("agent_path"), thread.get("agent_nickname"),
                thread.get("thread_source"), thread.get("source"),
                thread.get("created_at"), thread.get("created_at_ms"),
                thread.get("updated_at"), thread.get("updated_at_ms"),
                thread.get("rollout_path"),
            ),
        )
    connection.executemany(
        "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)", entry["edges"]
    )
    connection.commit()
    connection.close()


def _materialize(fixture, fixture_root):
    expanded = _expand(fixture, fixture_root)
    for path in (
        expanded["runtime_homes"]["default"],
        expanded["runtime_homes"]["nested"],
        expanded["runtime_homes"]["profile_alpha"],
        expanded["runtime_homes"]["profile_beta"],
        os.path.join(fixture_root, "worktree-a"),
        os.path.join(fixture_root, "worktree-b"),
        os.path.join(fixture_root, "home"),
        os.path.join(fixture_root, "xdg-state"),
        os.path.join(fixture_root, "xdg-data"),
        os.path.join(fixture_root, "xdg-cache"),
        os.path.join(fixture_root, "tmp"),
    ):
        os.makedirs(path, exist_ok=True)
    for rollout in expanded["rollouts"]:
        path = rollout["path"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            for record in rollout["records"]:
                handle.write(_canonical_bytes(record) + b"\n")
        os.utime(path, (rollout["mtime"], rollout["mtime"]))
    for entry in expanded["state_dbs"]:
        _write_state_db(entry)
    jobs_path = os.path.join(fixture_root, "registry", ".dispatch", "jobs.log")
    os.makedirs(os.path.dirname(jobs_path), exist_ok=True)
    with open(jobs_path, "w", encoding="utf-8") as handle:
        for job in expanded["jobs"]:
            handle.write("\t".join((
                "2033-05-18T03:28:00+00:00", "open", "fixture",
                job["cwd"], job["slug"], job["metadata"],
            )) + "\n")
    return expanded, jobs_path


def _fresh_sessions(fixture):
    sessions = []
    for item in fixture["sessions"]:
        session = Session(
            harness=item["harness"], pid=item["pid"], cwd=item["cwd"],
            elapsed_min=item["elapsed_min"],
        )
        session.proc_start = item.get("proc_start")
        sessions.append(session)
    return sessions


def _entity_value(entity, field):
    if field == "dispatch_depth":
        return getattr(entity, "dispatch_depth", getattr(entity, "depth", None))
    return getattr(entity, field, None)


def _semantic_payload(sessions, jobs, fixture_root):
    normalized_sessions = []
    for session in sessions:
        row = {field: _entity_value(session, field) for field in SESSION_FIELDS}
        subagents = getattr(session, "subagents", None)
        row["subagents"] = (
            None if subagents is None else [
                {
                    "agent_type": item.agent_type,
                    "active": item.active,
                    "started_at": item.started_at,
                    "source": item.source,
                }
                for item in subagents
            ]
        )
        normalized_sessions.append(_normalize(row, fixture_root))
    normalized_sessions.sort(key=lambda row: (
        row.get("harness") or "", row.get("pid") or -1,
        row.get("session_id") or "", row.get("cwd") or "",
    ))

    normalized_jobs = []
    for job in jobs:
        row = {field: _entity_value(job, field) for field in JOB_FIELDS}
        normalized_jobs.append(_normalize(row, fixture_root))
    normalized_jobs.sort(key=lambda row: (
        row.get("source") or "", row.get("slug") or "", row.get("key") or "",
        row.get("pid") or -1, row.get("cwd") or "",
    ))
    return {"sessions": normalized_sessions, "jobs": normalized_jobs}


def _source_state():
    root = Path(__file__).resolve().parents[3]
    revision = "unknown"
    dirty = None
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(root), text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain", "--", "tools/fleet"],
            cwd=str(root), text=True, stderr=subprocess.DEVNULL,
        ).strip())
    except (OSError, subprocess.CalledProcessError):
        pass
    return {"revision": revision, "dirty": dirty}


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, int(math.ceil(percentile * len(ordered))) - 1)]


def _summary(samples):
    wall = [sample["wall_ns"] for sample in samples]
    cpu = [sample["cpu_ns"] for sample in samples]
    return {
        "wall_median_ns": statistics.median(wall) if wall else None,
        "wall_p95_ns": _percentile(wall, 0.95),
        "cpu_median_ns": statistics.median(cpu) if cpu else None,
        "cpu_p95_ns": _percentile(cpu, 0.95),
    }


class _Counters:
    def __init__(self, fixture_root):
        self.fixture_root = fixture_root
        self.current = {}
        self._unique = {}
        self._seen_registry_objects = set()
        self.tcp_scan_phases = {}
        self.tcp_phase = "uncached"
        self.reset("unspecified")

    def reset(self, phase="unspecified"):
        self.phase = phase
        self.tcp_scan_phases.clear()
        self.current = {
            "lifecycle_calls": 0,
            "lifecycle_cursor_initializations": 0,
            "lifecycle_exact_scans": 0,
            "raw_edge_builds_by_home": {},
            "cwd_parses": 0,
            "roots_visited": 0,
            "files_visited": 0,
            "cache_evictions": 0,
            "phase": phase,
            "five_paths": {
                name: {"helper_calls": 0, "costly_evaluations": 0,
                      "physical_reads": 0, "rows_parsed": 0,
                      "file_touches": 0, "by_phase": {}}
                for name in ("registry", "tcp", "seat-ledger", "route-gate",
                             "owner-lifecycle")
            },
        }
        self._unique = {name: set() for name in self.current["five_paths"]}

    def record(self, name, field, *, key=None, amount=1):
        row = self.current["five_paths"][name]
        row[field] += amount
        if key is not None:
            self._unique[name].add(json.dumps(key, sort_keys=True, default=str))
            if name == "tcp" and isinstance(key, (tuple, list)) and key:
                phase_row = row["by_phase"].setdefault(
                    str(key[0]), {"helper_calls": 0, "costly_evaluations": 0,
                                  "physical_reads": 0, "ports": set()})
                if field in phase_row:
                    phase_row[field] += amount
                if field == "helper_calls" and len(key) > 1:
                    phase_row["ports"].add(str(key[1]))

    def snapshot(self):
        result = json.loads(json.dumps(self.current, sort_keys=True, default=list))
        for name, keys in self._unique.items():
            result["five_paths"][name]["unique_keys"] = len(keys)
        for phase_row in result["five_paths"]["tcp"]["by_phase"].values():
            phase_row["distinct_ports"] = len(phase_row.pop("ports"))
        return result


@contextlib.contextmanager
def _instrument(counters):
    cursor_initialization = codex._initialize_lifecycle_cursor
    exact_scan = codex._parse_latest_task_lifecycle
    lifecycle = codex._latest_task_lifecycle
    raw_edges = codex._build_thread_subagents
    transcript_cwd = dispatch._codex_transcript_cwd
    real_walk = os.walk
    import builtins
    from fleet import projection, route

    utility_root = Path(__file__).resolve().parents[3] / "utilities"
    if str(utility_root) not in sys.path:
        sys.path.insert(0, str(utility_root))
    import session_tidy
    owner_binding = projection._owner_route_binding_module()
    parsed_registry = dispatch.parsed_registry_rows
    tcp_snapshot = codex._tcp_listener_snapshot
    resolve_gate_marks = route.resolve_gate_marks
    gate_mark = route.gate_mark
    owner_binding_call = projection._owner_route_advance_binding
    resolve_owner_lifecycle = owner_binding.resolve_owner_route_lifecycle
    read_ledger = session_tidy._read_ledger_lines
    read_bytes = session_tidy.read_bytes
    real_open = builtins.open

    exact_depth = 0

    def counted_cursor_initialization(*args, **kwargs):
        if exact_depth == 0:
            counters.current["lifecycle_cursor_initializations"] += 1
        return cursor_initialization(*args, **kwargs)

    def counted_exact_scan(*args, **kwargs):
        nonlocal exact_depth
        counters.current["lifecycle_exact_scans"] += 1
        exact_depth += 1
        try:
            return exact_scan(*args, **kwargs)
        finally:
            exact_depth -= 1

    def counted_lifecycle(*args, **kwargs):
        counters.current["lifecycle_calls"] += 1
        return lifecycle(*args, **kwargs)

    def counted_edges(db_path, parent_ids=None):
        home = _normalize(os.path.dirname(db_path), counters.fixture_root)
        homes = counters.current["raw_edge_builds_by_home"]
        homes[home] = homes.get(home, 0) + 1
        return raw_edges(db_path, parent_ids=parent_ids)

    def counted_cwd(path):
        counters.current["cwd_parses"] += 1
        return transcript_cwd(path)

    def counted_walk(path, *args, **kwargs):
        for root, dirs, names in real_walk(path, *args, **kwargs):
            counters.current["roots_visited"] += 1
            counters.current["files_visited"] += len(names)
            yield root, dirs, names

    def counted_registry(path):
        normalized = os.path.abspath(os.fspath(path))
        key = (normalized,)
        counters.record("registry", "helper_calls", key=key)
        value = parsed_registry(path)
        if value and id(value) not in counters._seen_registry_objects:
            counters._seen_registry_objects.add(id(value))
            counters.record("registry", "costly_evaluations", key=key)
            counters.record("registry", "rows_parsed", key=key, amount=len(value))
        return value

    def counted_tcp(port, scan=None):
        phase = counters.tcp_scan_phases.get(id(scan), "uncached") if scan is not None else "uncached"
        counters.tcp_phase = phase
        counters.record("tcp", "helper_calls", key=(phase, str(port)))
        cached = scan is not None and port in scan
        if not cached:
            counters.record("tcp", "costly_evaluations", key=(phase, str(port)))
        try:
            return tcp_snapshot(port, scan)
        finally:
            counters.tcp_phase = "uncached"

    def counted_open(file, *args, **kwargs):
        path = os.fspath(file) if isinstance(file, (str, bytes, os.PathLike)) else ""
        if path in ("/proc/net/tcp", "/proc/net/tcp6"):
            counters.record("tcp", "physical_reads",
                            key=(counters.tcp_phase, path.rsplit("/", 1)[-1]))
        if owner_depth:
            counters.record("owner-lifecycle", "physical_reads", key=(path,))
        return real_open(file, *args, **kwargs)

    def counted_observation_init(observation, *args, **kwargs):
        original_observation_init(observation, *args, **kwargs)
        counters.tcp_scan_phases[id(observation.initial_scan)] = "initial"
        counters.tcp_scan_phases[id(observation.recheck_scan)] = "recheck"

    original_observation_init = codex.DaemonIdentityObservation.__init__

    def counted_resolve_gate_marks(*args, **kwargs):
        records = args[0] if args else kwargs.get("records")
        counters.record("route-gate", "helper_calls",
                        key=tuple(sorted((records or {}).keys())))
        return resolve_gate_marks(*args, **kwargs)

    def counted_gate_mark(record, node_id, home=None, state_roots=None):
        rid = record.get("route_id") if isinstance(record, dict) else None
        roots = tuple(os.fspath(item) for item in (state_roots or ()))
        counters.record("route-gate", "costly_evaluations",
                        key=(rid, str(node_id), home, roots))
        return gate_mark(record, node_id, home=home, state_roots=state_roots)

    def counted_owner_binding(entity, owner_binding_value=None):
        key = (getattr(entity, "owner_attempt_id", None),
               getattr(entity, "owner_route_file", None),
               getattr(entity, "route_id", None))
        counters.record("owner-lifecycle", "helper_calls", key=key)
        return owner_binding_call(entity, owner_binding_value)

    owner_depth = 0

    def counted_owner_lifecycle(*args, **kwargs):
        nonlocal owner_depth
        key = (os.fspath(args[0]) if args else os.fspath(kwargs.get("jobs", "")),
               kwargs.get("owner_attempt_id", ""))
        counters.record("owner-lifecycle", "costly_evaluations", key=key)
        owner_depth += 1
        try:
            return resolve_owner_lifecycle(*args, **kwargs)
        finally:
            owner_depth -= 1

    def counted_read_ledger(seat):
        path = os.fspath(session_tidy._ledger_path(seat))
        counters.record("seat-ledger", "helper_calls", key=(path,))
        return read_ledger(seat)

    def counted_read_bytes(path):
        normalized = os.fspath(path)
        if "/sessions/" in normalized and normalized.endswith(".jsonl"):
            counters.record("seat-ledger", "physical_reads", key=(normalized,))
        return read_bytes(path)

    def counted_owner_file_read(kind, path):
        # Owner lifecycle's file observer reports the exact files it evaluates.
        if kind == "file":
            counters.record("owner-lifecycle", "file_touches",
                            key=(os.fspath(path),))

    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes

    def counted_read_text(path, *args, **kwargs):
        value = os.fspath(path)
        if "/.dispatch/jobs.log" in value or value.endswith("/jobs.log"):
            counters.record("registry", "physical_reads", key=(value,))
        if owner_depth:
            counters.record("owner-lifecycle", "physical_reads", key=(value,))
        return original_read_text(path, *args, **kwargs)

    def counted_path_read_bytes(path, *args, **kwargs):
        value = os.fspath(path)
        if owner_depth:
            counters.record("owner-lifecycle", "physical_reads", key=(value,))
        return original_read_bytes(path, *args, **kwargs)

    def counted_owner_lifecycle_with_observer(*args, **kwargs):
        observer = kwargs.get("observer")
        if observer is None:
            kwargs["observer"] = counted_owner_file_read
        else:
            def observe(kind, path):
                counted_owner_file_read(path)
                return observer(kind, path)
            kwargs["observer"] = observe
        return counted_owner_lifecycle(*args, **kwargs)

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(
            codex, "_initialize_lifecycle_cursor",
            side_effect=counted_cursor_initialization,
        ))
        stack.enter_context(mock.patch.object(
            codex, "_parse_latest_task_lifecycle", side_effect=counted_exact_scan
        ))
        stack.enter_context(mock.patch.object(
            codex, "_latest_task_lifecycle", side_effect=counted_lifecycle
        ))
        stack.enter_context(mock.patch.object(
            codex, "_build_thread_subagents", side_effect=counted_edges
        ))
        stack.enter_context(mock.patch.object(
            dispatch, "_codex_transcript_cwd", side_effect=counted_cwd
        ))
        stack.enter_context(mock.patch.object(
            dispatch, "parsed_registry_rows", side_effect=counted_registry
        ))
        stack.enter_context(mock.patch.object(
            codex, "_tcp_listener_snapshot", side_effect=counted_tcp
        ))
        stack.enter_context(mock.patch.object(
            builtins, "open", side_effect=counted_open
        ))
        stack.enter_context(mock.patch.object(
            codex.DaemonIdentityObservation, "__init__", new=counted_observation_init
        ))
        stack.enter_context(mock.patch.object(
            route, "resolve_gate_marks", side_effect=counted_resolve_gate_marks
        ))
        stack.enter_context(mock.patch.object(
            route, "gate_mark", side_effect=counted_gate_mark
        ))
        stack.enter_context(mock.patch.object(
            projection, "_owner_route_advance_binding", side_effect=counted_owner_binding
        ))
        stack.enter_context(mock.patch.object(
            owner_binding, "resolve_owner_route_lifecycle",
            side_effect=counted_owner_lifecycle_with_observer
        ))
        stack.enter_context(mock.patch.object(
            session_tidy, "_read_ledger_lines", side_effect=counted_read_ledger
        ))
        stack.enter_context(mock.patch.object(
            session_tidy, "read_bytes", side_effect=counted_read_bytes
        ))
        stack.enter_context(mock.patch.object(Path, "read_text", new=counted_read_text))
        stack.enter_context(mock.patch.object(Path, "read_bytes", new=counted_path_read_bytes))
        stack.enter_context(mock.patch.object(dispatch.os, "walk", side_effect=counted_walk))
        yield


def _run_tick(jobs_path, fixture_root, counters, phase="unchanged-warm"):
    counters.reset(phase)
    evictions_before = getattr(codex, "_LIFECYCLE_CACHE_EVICTIONS", 0)
    wall_start = time.perf_counter_ns()
    cpu_start = time.process_time_ns()
    sessions, jobs = collect_all(jobs_path=jobs_path)
    cpu_ns = time.process_time_ns() - cpu_start
    wall_ns = time.perf_counter_ns() - wall_start
    counters.current["cache_evictions"] = max(
        0,
        getattr(codex, "_LIFECYCLE_CACHE_EVICTIONS", 0) - evictions_before,
    )
    return {
        "wall_ns": wall_ns,
        "cpu_ns": cpu_ns,
        "counters": counters.snapshot(),
    }, _semantic_payload(sessions, jobs, fixture_root)


def _hermetic_result(raw_fixture, fixture_hash, fixture_root, iterations):
    fixture, jobs_path = _materialize(raw_fixture, fixture_root)
    proc_rollouts = {
        int(pid): path for pid, path in fixture["proc_rollouts"].items()
    }
    proc_starts = {
        item["pid"]: item.get("proc_start") for item in fixture["sessions"]
    }
    default_home = fixture["runtime_homes"]["default"]
    registry_home = os.path.join(fixture_root, "registry")
    counters = _Counters(fixture_root)

    codex._SUBAGENT_INDEX.clear()
    codex._TITLE_INDEX.update(stamp=None, map={})
    codex._PROC_PATHS.clear()
    codex._INDEX.update(ts=0.0, map=None)
    lifecycle_cache = getattr(codex, "_LIFECYCLE_CACHE", None)
    if lifecycle_cache is not None:
        lifecycle_cache.clear()

    def fake_proc_rollout(pid, _cwd, _home):
        return proc_rollouts.get(pid)

    fixture_env = {
        "HOME": os.path.join(fixture_root, "home"),
        "XDG_STATE_HOME": os.path.join(fixture_root, "xdg-state"),
        "XDG_DATA_HOME": os.path.join(fixture_root, "xdg-data"),
        "XDG_CACHE_HOME": os.path.join(fixture_root, "xdg-cache"),
        "TMPDIR": os.path.join(fixture_root, "tmp"),
        # tmux finds its server through TMUX_TMPDIR, not TMPDIR: without it a pane
        # query here reaches the caller's own default tmux server.
        "TMUX_TMPDIR": os.path.join(fixture_root, "tmp"),
        "AGENT_DISPATCH_JOBS": jobs_path,
        "AGENT_RESOURCE_RUN_INDEX": os.path.join(
            fixture_root, "resource-runs.index.json"),
        "AGENT_HOME": str(Path(__file__).resolve().parents[3]),
    }
    for key in HERMETIC_ENV_KEYS:
        if key in os.environ:
            fixture_env[key] = os.environ[key]

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, fixture_env, clear=True))
        stack.enter_context(_instrument(counters))
        stack.enter_context(mock.patch.object(
            procscan, "scan", side_effect=lambda **_kwargs: _fresh_sessions(fixture)
        ))
        stack.enter_context(mock.patch.object(dispatch, "_scan_processes", return_value=[]))
        stack.enter_context(mock.patch.object(
            dispatch, "_candidate_jobs_paths", return_value=[jobs_path]
        ))
        stack.enter_context(mock.patch.object(
            dispatch, "_pid_namespace_identity", return_value="pid:[fixture]"
        ))
        stack.enter_context(mock.patch.object(
            dispatch, "_iso_elapsed_min", return_value=fixture["job_elapsed_min"]
        ))
        stack.enter_context(mock.patch.object(
            procscan, "read_proc_start",
            side_effect=lambda pid: proc_starts.get(int(pid)),
        ))
        stack.enter_context(mock.patch.object(codex, "_proc_rollout", side_effect=fake_proc_rollout))
        stack.enter_context(mock.patch.object(codex, "_home", return_value=default_home))
        stack.enter_context(mock.patch.object(
            codex, "_config_model_effort", return_value=("gpt-fixture", "medium")
        ))
        stack.enter_context(mock.patch.object(
            codex, "account_usage", return_value=fixture["usage"]["codex"]
        ))
        stack.enter_context(mock.patch.object(
            usage_api, "account_usage", return_value=fixture["usage"]["claude"]
        ))
        stack.enter_context(mock.patch.object(dispatch, "_codex_home", return_value=default_home))
        stack.enter_context(mock.patch.object(
            dispatch, "_registry_home", return_value=registry_home
        ))
        stack.enter_context(mock.patch.object(liveness, "_alive", return_value=True))
        stack.enter_context(mock.patch.object(titles, "fresh_title", return_value=None))
        stack.enter_context(mock.patch.object(titles, "fresh_summary", return_value=None))
        stack.enter_context(mock.patch("time.time", return_value=fixture["fixed_now"]))

        cold, cold_payload = _run_tick(jobs_path, fixture_root, counters, phase="cold")
        warm = []
        payload = cold_payload
        for _index in range(iterations):
            sample, candidate = _run_tick(
                jobs_path, fixture_root, counters, phase="unchanged-warm")
            if candidate != payload:
                raise RuntimeError("semantic payload changed between hermetic ticks")
            warm.append(sample)
        registry_path = Path(jobs_path)
        original_registry = registry_path.read_bytes()
        last_row = original_registry.splitlines(keepends=True)[-1]
        try:
            registry_path.write_bytes(original_registry + last_row)
            append_warm, _append_payload = _run_tick(
                jobs_path, fixture_root, counters, phase="append-warm")
        finally:
            registry_path.write_bytes(original_registry)

    payload_bytes = _canonical_bytes(payload)
    return {
        "benchmark_schema": BENCHMARK_SCHEMA,
        "fixture_schema": FIXTURE_SCHEMA,
        "fixture_sha256": fixture_hash,
        "source": _source_state(),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
        "iterations": iterations,
        "cold_sample": cold,
        "warm_samples": warm,
        "append_warm_sample": append_warm,
        "warm_summary": _summary(warm),
        "normalized_payload": payload,
        "semantic_digest": hashlib.sha256(payload_bytes).hexdigest(),
        "expected": raw_fixture["expected"],
    }


def _validate_result(result):
    if result.get("benchmark_schema") != BENCHMARK_SCHEMA:
        raise ValueError("invalid benchmark schema")
    if result.get("fixture_schema") != FIXTURE_SCHEMA:
        raise ValueError("invalid fixture schema")
    if result.get("iterations", 0) < 20:
        raise ValueError("hermetic evidence requires at least 20 warm iterations")
    if len(result.get("warm_samples", [])) != result["iterations"]:
        raise ValueError("warm sample count mismatch")
    required_counters = {
        "lifecycle_cursor_initializations", "lifecycle_exact_scans",
        "raw_edge_builds_by_home", "cwd_parses", "cache_evictions",
        "roots_visited", "files_visited",
    }
    samples = ([result.get("cold_sample")] + result.get("warm_samples", [])
               + [result.get("append_warm_sample")])
    required_paths = {"registry", "tcp", "seat-ledger", "route-gate", "owner-lifecycle"}
    for sample in samples:
        if not isinstance(sample, dict) or not required_counters.issubset(
            (sample.get("counters") or {}).keys()
        ):
            raise ValueError("sample missing required counters")
        counters = sample["counters"]
        if counters.get("phase") not in {"cold", "unchanged-warm", "append-warm"}:
            raise ValueError("sample missing phase label")
        paths = counters.get("five_paths")
        if not isinstance(paths, dict) or set(paths) != required_paths:
            raise ValueError("sample missing five-path counters")
        for path_counts in paths.values():
            for field in ("helper_calls", "costly_evaluations", "physical_reads", "unique_keys"):
                if not isinstance(path_counts.get(field), int) or path_counts[field] < 0:
                    raise ValueError("invalid five-path counter")
    if not isinstance(result.get("normalized_payload"), dict):
        raise ValueError("normalized payload missing")
    digest = hashlib.sha256(
        _canonical_bytes(result["normalized_payload"])
    ).hexdigest()
    if digest != result.get("semantic_digest"):
        raise ValueError("semantic digest does not match payload")


def _comparison(baseline, current):
    _validate_result(baseline)
    _validate_result(current)
    checks = {
        "benchmark_schema_equal": baseline["benchmark_schema"] == current["benchmark_schema"],
        "fixture_schema_equal": baseline["fixture_schema"] == current["fixture_schema"],
        "fixture_sha256_equal": baseline["fixture_sha256"] == current["fixture_sha256"],
        "normalized_payload_equal": baseline["normalized_payload"] == current["normalized_payload"],
        "semantic_digest_equal": baseline["semantic_digest"] == current["semantic_digest"],
    }
    warm = current["warm_samples"]
    all_samples = [current["cold_sample"]] + warm
    edge_bound = all(
        all(count <= 1 for count in sample["counters"]["raw_edge_builds_by_home"].values())
        for sample in all_samples
    )
    zero_warm_cursor_initializations = all(
        sample["counters"]["lifecycle_cursor_initializations"] == 0
        for sample in warm
    )
    expected_cwd = current["expected"]["unique_rollouts"]
    cwd_scaled = all(
        sample["counters"]["cwd_parses"] == expected_cwd for sample in all_samples
    )
    five_path_costs_bounded = all(
        path["costly_evaluations"] <= path["unique_keys"]
        for sample in ([current["cold_sample"]] + warm
                       + [current["append_warm_sample"]])
        for path in sample["counters"]["five_paths"].values()
    )
    phases_recorded = (
        current["cold_sample"]["counters"]["phase"] == "cold"
        and all(sample["counters"]["phase"] == "unchanged-warm" for sample in warm)
        and current["append_warm_sample"]["counters"]["phase"] == "append-warm"
    )
    checks.update({
        "edge_builds_at_most_one_per_home": edge_bound,
        "unchanged_warm_lifecycle_cursor_initializations_zero": (
            zero_warm_cursor_initializations
        ),
        "cwd_parses_equal_unique_rollouts": cwd_scaled,
        "five_path_costs_do_not_exceed_unique_keys": five_path_costs_bounded,
        "cold_unchanged_append_phases_recorded": phases_recorded,
    })
    baseline_lifecycle_cursor_initializations = [
        item["counters"]["lifecycle_cursor_initializations"]
        for item in baseline["warm_samples"]
    ]
    current_lifecycle_cursor_initializations = [
        item["counters"]["lifecycle_cursor_initializations"]
        for item in current["warm_samples"]
    ]
    baseline_cwd = [item["counters"]["cwd_parses"] for item in baseline["warm_samples"]]
    current_cwd = [item["counters"]["cwd_parses"] for item in current["warm_samples"]]
    deltas = {
        "warm_wall_median_ns": (
            current["warm_summary"]["wall_median_ns"]
            - baseline["warm_summary"]["wall_median_ns"]
        ),
        "warm_cpu_median_ns": (
            current["warm_summary"]["cpu_median_ns"]
            - baseline["warm_summary"]["cpu_median_ns"]
        ),
        "warm_lifecycle_cursor_initializations_median": (
            statistics.median(current_lifecycle_cursor_initializations)
            - statistics.median(baseline_lifecycle_cursor_initializations)
        ),
        "warm_cwd_parses_median": (
            statistics.median(current_cwd) - statistics.median(baseline_cwd)
        ),
    }
    return {
        "benchmark_schema": BENCHMARK_SCHEMA,
        "fixture_schema": FIXTURE_SCHEMA,
        "fixture_sha256": current["fixture_sha256"],
        "checks": checks,
        "deltas": deltas,
        "pass": all(checks.values()),
    }


def _write_json(path, value, exclusive=False):
    if path is None:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "x" if exclusive else "w"
    with target.open(mode, encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")


def _resolved_path(path):
    """Canonical CLI path, including existing and prospective symlink aliases."""
    return Path(path).expanduser().resolve(strict=False)


def _paths_alias(left, right):
    if left == right:
        return True
    try:
        return left.samefile(right)
    except OSError:
        return False


def _resolve_compare_paths(parser, args):
    """Resolve and reject aliased compare outputs before benchmark work begins."""
    resolved = {
        "baseline": _resolved_path(args.compare_baseline),
        "result": _resolved_path(args.result_out),
        "comparison": _resolved_path(args.comparison_out),
    }
    pairs = (
        ("baseline", "result"),
        ("baseline", "comparison"),
        ("result", "comparison"),
    )
    for left, right in pairs:
        if _paths_alias(resolved[left], resolved[right]):
            parser.error(
                "--compare-baseline, --result-out, and --comparison-out "
                "must resolve to distinct paths (%s aliases %s)" % (left, right)
            )
    args.compare_baseline = str(resolved["baseline"])
    args.result_out = str(resolved["result"])
    args.comparison_out = str(resolved["comparison"])


def _live_result(iterations):
    samples = []
    payload = None
    for _index in range(iterations + 1):
        wall_start = time.perf_counter_ns()
        cpu_start = time.process_time_ns()
        sessions, jobs = collect_all()
        sample = {
            "wall_ns": time.perf_counter_ns() - wall_start,
            "cpu_ns": time.process_time_ns() - cpu_start,
        }
        if payload is None:
            cold = sample
        else:
            samples.append(sample)
        payload = _semantic_payload(sessions, jobs, "")
    fingerprint = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    return {
        "benchmark_schema": BENCHMARK_SCHEMA,
        "mode": "live-descriptive-only",
        "source": _source_state(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "iterations": iterations,
        "cold_sample": cold,
        "warm_samples": samples,
        "warm_summary": _summary(samples),
        "state_fingerprint": fingerprint,
        "semantic_correctness_gate": False,
    }


def _live_budget(result, max_warm_wall_seconds):
    """Machine-check one saved live sample without pretending it is semantic proof."""
    if result.get("benchmark_schema") != BENCHMARK_SCHEMA:
        raise ValueError("invalid benchmark schema")
    if result.get("mode") != "live-descriptive-only":
        raise ValueError("live budget input must come from --live")
    samples = result.get("warm_samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("live budget input has no warm samples")
    wall_ns = []
    for sample in samples:
        value = sample.get("wall_ns") if isinstance(sample, dict) else None
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("live budget input has an invalid wall_ns sample")
        wall_ns.append(value)
    value = max(wall_ns) / 1_000_000_000.0
    return {
        "metric": "max_warm_wall_seconds",
        "value": value,
        "limit": max_warm_wall_seconds,
        "pass": value <= max_warm_wall_seconds,
        "iterations": len(wall_ns),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--baseline-out")
    parser.add_argument("--compare-baseline")
    parser.add_argument("--result-out")
    parser.add_argument("--comparison-out")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--check-live-budget")
    parser.add_argument("--max-warm-wall-seconds", type=float)
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    if args.check_live_budget:
        if (args.fixture or args.baseline_out or args.compare_baseline or args.result_out
                or args.comparison_out or args.live):
            parser.error("--check-live-budget does not accept benchmark/output options")
        if (args.max_warm_wall_seconds is None
                or not math.isfinite(args.max_warm_wall_seconds)
                or args.max_warm_wall_seconds <= 0):
            parser.error("--check-live-budget requires a positive --max-warm-wall-seconds")
        with open(args.check_live_budget, encoding="utf-8") as handle:
            result = json.load(handle)
        budget = _live_budget(result, args.max_warm_wall_seconds)
        print(json.dumps(budget, sort_keys=True))
        return 0 if budget["pass"] else 1
    if args.max_warm_wall_seconds is not None:
        parser.error("--max-warm-wall-seconds requires --check-live-budget")
    if args.live:
        if args.baseline_out or args.compare_baseline or args.comparison_out:
            parser.error("--live accepts only --result-out")
        result = _live_result(args.iterations)
        _write_json(args.result_out, result)
        print(json.dumps(result["warm_summary"], sort_keys=True))
        return 0
    if not args.fixture:
        parser.error("--fixture is required outside --live")
    if args.iterations < 20:
        parser.error("hermetic mode requires at least 20 warm iterations")
    if bool(args.baseline_out) == bool(args.compare_baseline):
        parser.error("choose exactly one of --baseline-out or --compare-baseline")
    if args.baseline_out:
        if args.result_out or args.comparison_out:
            parser.error("--baseline-out does not accept result/comparison outputs")
        args.baseline_out = str(_resolved_path(args.baseline_out))
    else:
        if not args.result_out or not args.comparison_out:
            parser.error("--compare-baseline requires --result-out and --comparison-out")
        _resolve_compare_paths(parser, args)

    fixture, fixture_hash = _load_fixture(args.fixture)
    with tempfile.TemporaryDirectory(prefix="fleet-tick-v1-") as fixture_root:
        result = _hermetic_result(
            fixture, fixture_hash, os.path.abspath(fixture_root), args.iterations
        )
    _validate_result(result)
    if args.baseline_out:
        _write_json(args.baseline_out, result, exclusive=True)
        print(json.dumps({
            "baseline": args.baseline_out,
            "semantic_digest": result["semantic_digest"],
            "warm_summary": result["warm_summary"],
        }, sort_keys=True))
        return 0

    with open(args.compare_baseline, encoding="utf-8") as handle:
        baseline = json.load(handle)
    comparison = _comparison(baseline, result)
    _write_json(args.result_out, result)
    _write_json(args.comparison_out, comparison)
    print(json.dumps(comparison, sort_keys=True))
    return 0 if comparison["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
