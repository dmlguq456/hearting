#!/usr/bin/env python3
"""Matched production-entry carrier and peer-watch readiness measurements.

This module is the measurement harness imported by
``shared_readiness_cpu_fixture.test.py``. It uses the supplied baseline source
and a content-bound candidate copy, real registered pending work, real carrier
and ``__watch-run`` entry points, and the existing obligation runner.  Only the
Herdr CLI and its local observation endpoint are faked.
"""

from __future__ import annotations

import contextlib
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import statistics
from pathlib import PurePosixPath


SOURCE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WINDOW = 12.0
COLD_SAMPLE_INTERVAL = 0.05
SAMPLE_INTERVAL = 0.05
TARGET_BYTES = 28 * 1024 * 1024
CARRIER_DEADLINE = 36.0
PENDING_ATTEMPTS = 15
PRODUCTION_CLASSES = {"carrier", "readiness-helper", "peer-watch", "shared-runner"}


def _proc_stat(pid):
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii")
        tail = raw[raw.rfind(")") + 2:].split()
        if not tail or tail[0] in {"Z", "X"}:
            return None
        return {
            "pid": int(pid), "ppid": int(tail[1]), "pgrp": int(tail[2]),
            "utime": int(tail[11]), "stime": int(tail[12]),
            "cutime": int(tail[13]), "cstime": int(tail[14]),
            "start": tail[19], "rss_pages": int(tail[21]),
        }
    except (OSError, ValueError, IndexError, TypeError):
        return None


def _cmdline(pid):
    try:
        return [part.decode("utf-8", "replace") for part in
                Path(f"/proc/{int(pid)}/cmdline").read_bytes().split(b"\0") if part]
    except OSError:
        return []


def _role(pid, argv):
    joined = " ".join(argv)
    base = os.path.basename(argv[0]) if argv else ""
    if pid == os.getpid():
        return "fixture-driver"
    if "--carrier-child" in argv:
        return "carrier"
    if "dispatch-attempt-ready.py" in joined:
        return "readiness-helper"
    if "peer-steward.py" in joined and "__watch-run" in argv:
        return "peer-watch"
    if "peer-steward.py" in joined and "__obligation-runner" in argv:
        return "shared-runner"
    if base == "herdr" or "/fakebin/herdr" in joined:
        return "fixture-cli"
    if "fixture-bound-worker" in joined:
        return "bound-worker"
    return "fixture-process"


def _snapshot(root_pid, *, state_root=None):
    rows = {}
    unreadable = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return {}, ["/proc"]
    for name in names:
        if not name.isdigit():
            continue
        item = _proc_stat(int(name))
        if item is None:
            continue
        argv = _cmdline(item["pid"])
        item["argv"] = argv
        item["role"] = _role(item["pid"], argv)
        rows[item["pid"]] = item
    seed_pids = {int(root_pid)}
    if state_root is not None:
        runner_path = (Path(state_root) / "peer-steward" / "obligations"
                       / "readiness-v1" / "runner.json")
        try:
            runner = json.loads(runner_path.read_text(encoding="utf-8"))
            runner_pid = int(runner.get("pid"))
            runner_start = str(runner.get("pid_start") or "")
            runner_row = rows.get(runner_pid)
            if runner_row is not None and runner_row["start"] == runner_start:
                # The production fixture forks the existing runner in-process;
                # its inherited command line is the fixture's. Its durable
                # PID/start marker is the exact identity used by the runtime.
                runner_row["role"] = "shared-runner"
                seed_pids.add(runner_pid)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        marker = ("AGENT_PEER_LEDGER_ROOT=" + str(Path(state_root).resolve())).encode()
        for pid, item in rows.items():
            argv = item["argv"]
            if "__obligation-runner" not in argv or not any(
                    "peer-steward.py" in part for part in argv):
                continue
            try:
                environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            except OSError:
                continue
            if marker in environ:
                seed_pids.add(pid)
    selected = seed_pids.intersection(rows)
    unreadable.extend(str(pid) for pid in seed_pids - selected)
    changed = True
    while changed:
        changed = False
        for pid, item in rows.items():
            if item["ppid"] in selected and pid not in selected:
                selected.add(pid)
                changed = True
    page_size = os.sysconf("SC_PAGE_SIZE")
    result = {}
    for pid in selected:
        item = rows.get(pid)
        if item is None:
            unreadable.append(str(pid))
            continue
        identity = (pid, item["start"])
        result[identity] = {
            **item,
            "own_ticks": item["utime"] + item["stime"],
            "reaped_ticks": item["cutime"] + item["cstime"],
            "rss_bytes": item["rss_pages"] * page_size,
            "argv_text": " ".join(item["argv"])[:320],
        }
    return result, unreadable


def _snapshot_totals(snapshot):
    grouped = {}
    all_rss = 0
    production_rss = 0
    for item in snapshot.values():
        role = item["role"]
        row = grouped.setdefault(role, {"processes": 0, "rss_bytes": 0,
                                        "own_ticks": 0, "reaped_ticks": 0})
        row["processes"] += 1
        row["rss_bytes"] += item["rss_bytes"]
        row["own_ticks"] += item["own_ticks"]
        row["reaped_ticks"] += item["reaped_ticks"]
        all_rss += item["rss_bytes"]
        if role in PRODUCTION_CLASSES:
            production_rss += item["rss_bytes"]
    return grouped, all_rss, production_rss


def _inclusive_ticks(snapshot):
    # Linux transfers a waited child's cumulative self CPU into its parent's
    # cutime/cstime.  Summing these counters at each boundary counts that
    # transfer once; own and reaped deltas below remain separate views.
    return sum(item["own_ticks"] + item["reaped_ticks"] for item in snapshot.values())


def _raw_boundary(snapshot):
    return [
        {"pid": pid, "start": start, "role": item["role"],
         "own_cpu_ticks": item["own_ticks"],
         "reaped_child_cpu_ticks": item["reaped_ticks"],
         "rss_bytes": item["rss_bytes"], "argv": item["argv_text"]}
        for (pid, start), item in sorted(snapshot.items())
    ]


def _measure(seed_pid, *, seconds=None, ready=None, prepare=None, timeout=None,
             state_root=None):
    """Sample the process lineage; roots stay alive until end counters are read."""
    hz = os.sysconf("SC_CLK_TCK")
    started = time.monotonic()
    previous, unknown_start = _snapshot(seed_pid, state_root=state_root)
    start_snapshot = previous
    start_totals, start_rss, start_production_rss = _snapshot_totals(start_snapshot)
    samples = [previous]
    unreadable = list(unknown_start)
    if prepare is not None:
        prepare()
    deadline = started + (timeout if timeout is not None else (seconds or 0))
    while True:
        if seconds is not None and time.monotonic() - started >= seconds:
            break
        if timeout is not None and time.monotonic() >= deadline:
            raise TimeoutError("fixture branch did not reach its required readiness checkpoint")
        time.sleep(COLD_SAMPLE_INTERVAL if ready is not None else SAMPLE_INTERVAL)
        current, missed = _snapshot(seed_pid, state_root=state_root)
        samples.append(current)
        unreadable.extend(missed)
        previous = current
        if ready is not None and ready():
            break
    ended = time.monotonic()
    final_snapshot, missed = _snapshot(seed_pid, state_root=state_root)
    samples.append(final_snapshot)
    unreadable.extend(missed)
    final_totals, end_rss, end_production_rss = _snapshot_totals(final_snapshot)
    elapsed = max(0.0, ended - started)

    role_peaks = {}
    total_peak_rss = 0
    production_peak_rss = 0
    total_peak_processes = 0
    seen = {}
    own_deltas = {}
    reaped_deltas = {}
    transfers = []
    missing_between_samples = []
    for index, snapshot in enumerate(samples):
        totals, total_rss, production_rss = _snapshot_totals(snapshot)
        total_peak_rss = max(total_peak_rss, total_rss)
        production_peak_rss = max(production_peak_rss, production_rss)
        total_peak_processes = max(total_peak_processes, len(snapshot))
        for identity, item in snapshot.items():
            role = item["role"]
            bucket = role_peaks.setdefault(role, {"processes": 0, "rss_bytes": 0})
            current_role = totals[role]
            bucket["processes"] = max(bucket["processes"], current_role["processes"])
            bucket["rss_bytes"] = max(bucket["rss_bytes"], current_role["rss_bytes"])
            seen.setdefault(role, set()).add(identity)
        if index == 0:
            continue
        before = samples[index - 1]
        gone = set(before) - set(snapshot)
        missing_between_samples.extend(
            {"pid": pid, "start": start,
             "role": before[(pid, start)]["role"],
             "reason": "process-exited-before-next-boundary"}
            for pid, start in sorted(gone))
        for identity, item in snapshot.items():
            old = before.get(identity)
            if old is None:
                continue
            role = item["role"]
            own_delta = max(0, item["own_ticks"] - old["own_ticks"])
            reaped_delta = max(0, item["reaped_ticks"] - old["reaped_ticks"])
            own_deltas[role] = own_deltas.get(role, 0) + own_delta
            reaped_deltas[role] = reaped_deltas.get(role, 0) + reaped_delta
            if reaped_delta:
                child_pids_before = {child_pid for (child_pid, _start), child in before.items()
                                     if child["ppid"] == identity[0]}
                child_pids_after = {child_pid for (child_pid, _start), child in snapshot.items()
                                    if child["ppid"] == identity[0]}
                exited = sorted(child_pids_before - child_pids_after)
                transfers.append({
                    "parent": {"pid": identity[0], "start": identity[1], "role": role},
                    "reaped_child_cpu_delta_ticks": reaped_delta,
                    "observed_exited_child_pids": exited,
                    "child_identity_reading": "observed-child-exit" if exited
                                              else "unknown-short-lived-child",
                })

    # Every measurement window has the same four production classes. A class
    # with no matching PID in any sample has zero boundary counts/counters and
    # an empty identity list; that records sampled absence without claiming
    # that a process shorter than the sampling interval never existed.
    all_roles = PRODUCTION_CLASSES | set(start_totals) | set(final_totals) | set(role_peaks)
    classes = {}
    for role in sorted(all_roles):
        start = start_totals.get(role, {})
        end = final_totals.get(role, {})
        identities = sorted(seen.get(role, set()))
        presence = (
            "observed-at-sampled-boundaries" if identities else
            "unknown-proc-visibility" if unreadable else
            "no-process-observed-at-sampled-boundaries"
        )
        classes[role] = {
            "start_processes": start.get("processes", 0),
            "end_processes": end.get("processes", 0),
            "peak_processes": role_peaks.get(role, {}).get("processes", 0),
            "start_rss_bytes": start.get("rss_bytes", 0),
            "end_rss_bytes": end.get("rss_bytes", 0),
            "peak_simultaneous_rss_bytes": role_peaks.get(role, {}).get("rss_bytes", 0),
            "sampled_own_cpu_delta_ticks": own_deltas.get(role, 0),
            "sampled_own_cpu_delta_seconds": own_deltas.get(role, 0) / hz,
            "reaped_child_counter_delta_ticks": reaped_deltas.get(role, 0),
            "reaped_child_counter_delta_seconds": reaped_deltas.get(role, 0) / hz,
            "pid_start_identities": [list(identity) for identity in identities],
            "presence_observation": presence,
            "unobserved_short_lived_processes": "unknown",
            "counter_semantics": (
                "self CPU belongs to sampled PIDs in this class; reaped-child CPU is "
                "the sampled parent PIDs' cumulative child counter and is not attributed "
                "to the child class"
            ),
        }
    driver_identity = next((identity for identity in start_snapshot
                            if identity[0] == int(seed_pid)), None)
    start_driver = start_snapshot.get(driver_identity, {})
    end_stat = _proc_stat(seed_pid)
    driver_own = (max(0, end_stat["utime"] + end_stat["stime"]
                      - start_driver.get("own_ticks", 0)) if end_stat else None)
    cpu_delta = _inclusive_ticks(final_snapshot) - _inclusive_ticks(start_snapshot)
    if cpu_delta < 0:
        cpu_delta = None
    return {
        "elapsed_seconds": elapsed,
        "samples": len(samples),
        "sample_interval_seconds": COLD_SAMPLE_INTERVAL if ready is not None else SAMPLE_INTERVAL,
        "clock_ticks_per_second": hz,
        "start_monotonic": started,
        "end_monotonic": ended,
        "classes": classes,
        "simultaneous_total_rss_bytes": {
            "start": start_rss, "end": end_rss, "peak": total_peak_rss,
            "production_classes_start": start_production_rss,
            "production_classes_end": end_production_rss,
            "production_classes_peak": production_peak_rss,
            "peak_process_count": total_peak_processes,
            "shared_runner_counted_once": True,
        },
        "inclusive_lineage_cpu_delta_ticks": cpu_delta,
        "inclusive_lineage_cpu_delta_seconds": cpu_delta / hz if cpu_delta is not None else None,
        "production_inclusive_cpu_delta_ticks": (
            sum(item["own_ticks"] + item["reaped_ticks"] for item in final_snapshot.values()
                if item["role"] in PRODUCTION_CLASSES)
            - sum(item["own_ticks"] + item["reaped_ticks"] for item in start_snapshot.values()
                  if item["role"] in PRODUCTION_CLASSES)),
        "production_inclusive_cpu_delta_seconds": (
            sum(item["own_ticks"] + item["reaped_ticks"] for item in final_snapshot.values()
                if item["role"] in PRODUCTION_CLASSES)
            - sum(item["own_ticks"] + item["reaped_ticks"] for item in start_snapshot.values()
                  if item["role"] in PRODUCTION_CLASSES)) / hz,
        "sampler_driver_own_cpu_delta_ticks": driver_own,
        "sampler_driver_includes_fixture_coordination_and_endpoint_threads": True,
        "sampler_cpu_is_separate_from_production_class_cpu": True,
        "raw_pid_counters": {
            "start": _raw_boundary(start_snapshot),
            "end": _raw_boundary(final_snapshot),
        },
        "missing_or_unknown_readings": {
            "unreadable_proc_entries": sorted(set(unreadable)),
            "observed_processes_missing_at_later_sample": missing_between_samples,
            "reaped_child_transfers": transfers,
            "short_lived_reaped_cpu_identity_unknown": sum(
                row["reaped_child_cpu_delta_ticks"] for row in transfers
                if row["child_identity_reading"] == "unknown-short-lived-child"),
        },
        "accounting_note": (
            "inclusive_lineage_cpu_delta is the boundary delta of live self plus "
            "already-reaped child counters across the retained process tree; it "
            "counts live-to-reaped transfer once. Per-class own and reaped-child "
            "deltas are separate counters and must not be added together. The "
            "fixture-driver counter includes sampler coordination and fake endpoint "
            "threads and is reported separately from production-entry classes."
        ),
    }


def sampler_self_checks():
    """Prove CPU transfer accounting and explicit classes with real processes."""
    root = os.getpid()
    before = _proc_stat(root)
    if before is None:
        raise RuntimeError("sampler self-check cannot read its own proc identity")
    short = subprocess.Popen(
        [sys.executable, "-c",
         "import time; end=time.process_time()+0.18; x=0\nwhile time.process_time()<end: x+=1"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    short_identity = _proc_stat(short.pid)
    short.wait(timeout=5)
    short_end = _proc_stat(root)
    short_reaped = ((short_end["cutime"] + short_end["cstime"])
                    - (before["cutime"] + before["cstime"])) if short_end else 0
    if short_reaped <= 0:
        raise AssertionError("short helper reaped between samples was not reflected in parent cutime")

    before_tree, _ = _snapshot(root)
    before_total = _inclusive_ticks(before_tree)
    driver_identity = next(identity for identity in before_tree if identity[0] == root)
    driver_before = before_tree[driver_identity]
    live = subprocess.Popen(
        [sys.executable, "-c",
         "import time; end=time.process_time()+0.32; x=0\nwhile time.process_time()<end: x+=1\ntime.sleep(.12)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    live_identity = _proc_stat(live.pid)
    time.sleep(0.1)
    live_tree, _ = _snapshot(root)
    live_row = next((item for (pid, _start), item in live_tree.items() if pid == live.pid), None)
    if live_row is None:
        raise AssertionError("live child was not present in sampler lineage")
    live_own_at_boundary = live_row["own_ticks"]
    live_total = _inclusive_ticks(live_tree)
    live.wait(timeout=5)
    after_tree, _ = _snapshot(root)
    after_total = _inclusive_ticks(after_tree)
    driver_after_identity = next(identity for identity in after_tree if identity[0] == root)
    driver_after = after_tree[driver_after_identity]
    parent_transfer = driver_after["reaped_ticks"] - driver_before["reaped_ticks"]
    driver_own_delta = driver_after["own_ticks"] - driver_before["own_ticks"]
    boundary_delta = after_total - before_total
    if parent_transfer < live_own_at_boundary:
        raise AssertionError("live child CPU did not transfer into its parent's reaped counters")
    if boundary_delta != driver_own_delta + parent_transfer:
        raise AssertionError("inclusive boundary accounting did not count child transfer exactly once")

    carrier = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.8)", "--carrier-child"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    try:
        sampled = _measure(root, seconds=0.2)
    finally:
        carrier.wait(timeout=5)
    if set(PRODUCTION_CLASSES) - set(sampled["classes"]):
        raise AssertionError("sampler omitted a production class with no observed process")
    carrier_class = sampled["classes"]["carrier"]
    if (carrier_class["peak_processes"] != 1
            or carrier_class["presence_observation"] != "observed-at-sampled-boundaries"
            or not carrier_class["pid_start_identities"]):
        raise AssertionError("real carrier fixture identity was not sampled")
    for role in PRODUCTION_CLASSES - {"carrier"}:
        row = sampled["classes"][role]
        numeric_fields = (
            "start_processes", "end_processes", "peak_processes",
            "start_rss_bytes", "end_rss_bytes", "peak_simultaneous_rss_bytes",
            "sampled_own_cpu_delta_ticks", "sampled_own_cpu_delta_seconds",
            "reaped_child_counter_delta_ticks", "reaped_child_counter_delta_seconds",
        )
        if (any(row[field] != 0 for field in numeric_fields)
                or row["pid_start_identities"]
                or row["presence_observation"] != "no-process-observed-at-sampled-boundaries"
                or row["unobserved_short_lived_processes"] != "unknown"):
            raise AssertionError(f"absent {role} class was not represented as sampled zero/unknown")
    return {
        "short_helper_reaped_between_samples": {
            "pid_start": ([short.pid, short_identity["start"]] if short_identity else None),
            "parent_reaped_cpu_delta_ticks": short_reaped,
            "captured_without_live_child_sample": True,
        },
        "live_child_to_parent_transfer": {
            "pid_start": ([live.pid, live_identity["start"]] if live_identity else None),
            "child_own_cpu_ticks_at_live_boundary": live_own_at_boundary,
            "parent_reaped_cpu_delta_ticks": parent_transfer,
            "driver_own_cpu_delta_ticks": driver_own_delta,
            "inclusive_boundary_delta_ticks": boundary_delta,
            "transfer_counted_once": boundary_delta == driver_own_delta + parent_transfer,
            "inclusive_cpu_at_live_boundary": live_total,
            "inclusive_cpu_after_reap": after_total,
        },
        "sampled_production_classes": {
            "class_names": sorted(sampled["classes"]),
            "observed_carrier_identity": carrier_class["pid_start_identities"],
            "absent_classes_have_sampled_zero_and_unknown_short_lived_identity": True,
        },
    }


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_jobs(path, target_bytes, attempt_rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    size = rows = 0
    unrelated_offset = unrelated_value_offset = None
    with path.open("wb") as stream:
        while size < target_bytes:
            row = (f"2026-10-20T00:00:00Z\trunning\t/repo\t/wt\tworker\t"
                   f"attempt_id=att-unrelated-{rows:07d},note={'n' * 160}\n").encode()
            if unrelated_offset is None:
                unrelated_offset = stream.tell()
                unrelated_value_offset = unrelated_offset + row.index(b"note=") + len(b"note=")
            stream.write(row)
            size += len(row)
            rows += 1
        for row in attempt_rows:
            stream.write(row.encode("utf-8") + b"\n")
            size += len(row.encode("utf-8") + b"\n")
            rows += 1
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "bytes": size, "rows": rows, "sha256": _hash_file(path),
        "unrelated_row_value_offset": unrelated_value_offset,
        "unrelated_value_bytes": 160,
        "target_bytes": target_bytes,
    }


def _mutate_unrelated_row(path, workload, fill=b"x"):
    if len(fill) != 1:
        raise ValueError("unrelated event replacement is one byte to preserve the workload size")
    with Path(path).open("r+b") as stream:
        stream.seek(workload["unrelated_row_value_offset"])
        stream.write(fill * workload["unrelated_value_bytes"])
        stream.flush()
        os.fsync(stream.fileno())
    return _hash_file(path)


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_jsonl(path):
    try:
        return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
                if line.strip()]
    except (OSError, ValueError):
        return []


def _carrier_child(source_root, jobs, attempt, deadline_seconds, result_path, helper_log):
    utilities = Path(source_root) / "utilities"
    spec_path = Path(source_root) / "hooks" / "dispatch-owner-rewake.py"
    import importlib.util
    spec = importlib.util.spec_from_file_location("fixture_dispatch_owner_rewake", spec_path)
    hook = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = hook
    spec.loader.exec_module(hook)
    original_run = hook.subprocess.run

    def record_real_helper(*args, **kwargs):
        completed = original_run(*args, **kwargs)
        command = args[0] if args else kwargs.get("args", [])
        command_text = " ".join(map(str, command)) if isinstance(command, (list, tuple)) else str(command)
        if str(utilities / "dispatch-attempt-ready.py") in command_text:
            try:
                payload = json.loads(completed.stdout or "")
            except (ValueError, TypeError):
                payload = {"state": "unreadable", "stdout": (completed.stdout or "")[:1000]}
            event = {"returncode": completed.returncode, "result": payload,
                     "argv": command_text}
            with Path(helper_log).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, sort_keys=True) + "\n")
        return completed

    # Record the real helper's output after it has parsed/classified the live
    # registry; no readiness operation or result is mocked or bypassed.
    hook.subprocess.run = record_real_helper
    launch = hook.Launch(attempt_id=attempt, jobs=Path(jobs), session_id="sid-target-a")
    started = time.monotonic()
    outcome = hook.wait_for_attempt(
        launch, utilities / "dispatch-attempt-ready.py",
        deadline=started + float(deadline_seconds))
    stat = _proc_stat(os.getpid())
    Path(result_path).write_text(json.dumps({
        "wait_state": outcome[0], "wait_reason": outcome[1],
        "wait_elapsed_seconds": time.monotonic() - started,
        "wait_started_monotonic": started,
        "carrier_stat": ({"own_cpu_ticks": stat["utime"] + stat["stime"],
                          "reaped_child_cpu_ticks": stat["cutime"] + stat["cstime"]}
                         if stat else None),
        "helper_calls": len(_read_jsonl(helper_log)),
    }, sort_keys=True), encoding="utf-8")


def _clean_environment(base=None):
    env = dict(os.environ if base is None else base)
    prefixes = ("AGENT_DISPATCH_", "AGENT_PEER_", "AGENT_SESSION_", "AGENT_RUNTIME_",
                "AGENT_HERDR_", "HERDR_", "CLAUDE_", "CODEX_", "OPENCODE_")
    exact = {"AGENT_HOME", "CLAUDE_HOME", "HARNESS_DATA_ROOT", "HARNESS_STATE_ROOT",
             "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
             "XDG_CACHE_HOME", "XDG_STATE_HOME", "FAKE_HERDR_TARGETS"}
    for key in tuple(env):
        if key.startswith(prefixes) or key in exact:
            env.pop(key, None)
    return env


def _fixture_env(source, data_root, state_root, jobs, case_root, *, fake_bin=None,
                 endpoint=None, targets=None, steward_session=None):
    env = _clean_environment()
    home = Path(case_root) / "home"
    for directory in (home / ".config", home / ".local" / "share",
                      home / ".local" / "state", home / ".cache"):
        directory.mkdir(parents=True, exist_ok=True)
    env.update({
        "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "AGENT_HOME": str(source), "CLAUDE_HOME": str(source),
        "HARNESS_DATA_ROOT": str(data_root), "HARNESS_STATE_ROOT": str(state_root / "harness"),
        "AGENT_DISPATCH_JOBS": str(jobs), "AGENT_PEER_LEDGER_ROOT": str(state_root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((str(Path(source) / "utilities"),
                                        str(Path(source) / "tools" / "install"))),
        "AGENT_CLAUDE_REWAKE_INTERVAL_SECONDS": "5",
        "AGENT_CLAUDE_REWAKE_MAX_SECONDS": str(int(CARRIER_DEADLINE)),
    })
    if endpoint is not None:
        env["HERDR_SOCKET_PATH"] = str(endpoint.path)
        env["AGENT_HERDR_SESSION"] = "default"
        target_map = {row["target"]: row for row in (targets or {}).values()}
        env["FAKE_HERDR_TARGETS"] = json.dumps(target_map, sort_keys=True)
        env["FIXTURE_HERDR_LOG"] = str(Path(case_root) / "herdr.log")
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    if steward_session:
        env["CLAUDE_CODE_SESSION_ID"] = steward_session
    return env


def _spawn_carrier(source, jobs, attempt, state_root, data_root, case_root):
    result_path = Path(case_root) / f"{attempt}.result.json"
    helper_log = Path(case_root) / f"{attempt}.helpers.jsonl"
    env = _fixture_env(source, data_root, state_root, jobs, case_root)
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--carrier-child", str(source),
         str(jobs), attempt, str(CARRIER_DEADLINE), str(result_path), str(helper_log)],
        cwd=str(source), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
    return {"process": process, "attempt_id": attempt,
            "result_path": result_path, "helper_log": helper_log,
            "started": time.monotonic()}


def _carrier_ready(consumers, candidate, state_root):
    # Both the measured baseline and candidate use the shared runner. Observe
    # the same pending projection boundary on each side; the baseline carrier
    # no longer launches a standalone readiness helper subprocess.
    records = []
    for path in (Path(state_root) / "peer-steward" / "obligations").glob("*.json"):
        record = _read_json(path) or {}
        intent = record.get("intent") or {}
        observation = record.get("observation") or {}
        if intent.get("kind") == "readiness-observation":
            records.append(observation)
    pending = [row for row in records
               if row.get("readiness_state") == "pending"
               and isinstance(row.get("shared_readiness"), dict)]
    return len(pending) >= len(consumers)


def _wait_processes(processes, timeout):
    deadline = time.monotonic() + timeout
    for consumer in processes:
        process = consumer["process"]
        remaining = max(0.1, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"carrier did not return within {timeout}s: {consumer['attempt_id']}")
        stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
        if process.returncode != 0:
            raise RuntimeError(f"carrier {consumer['attempt_id']} exited {process.returncode}: {stderr[-1500:]}")
        consumer["result"] = _read_json(consumer["result_path"])
        consumer["helper_events"] = _read_jsonl(consumer["helper_log"])


_CLEANUP_RESULTS = []
_CLEANUP_PATH = None


def _record_fixture_cleanup(scope, result):
    entry = {"scope": scope, **result}
    entry["ok"] = all(value.get("ok") is True
                      for value in result.values() if isinstance(value, dict))
    _CLEANUP_RESULTS.append(entry)
    if _CLEANUP_PATH is not None:
        report = {
            "schema_version": 1,
            "status": "pass" if all(item.get("ok") for item in _CLEANUP_RESULTS) else "fail",
            "results": _CLEANUP_RESULTS,
        }
        temporary = _CLEANUP_PATH.with_suffix(_CLEANUP_PATH.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
        temporary.replace(_CLEANUP_PATH)


def _terminate_processes(processes):
    process_rows = [item["process"] if isinstance(item, dict) else item
                    for item in processes]
    roots = [process.pid for process in process_rows if process.poll() is None]
    tree, unreadable = _snapshot_many(roots) if roots else ({}, [])
    identities = sorted(tree)
    for pid, _start in sorted(tree, reverse=True):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    if tree:
        time.sleep(0.2)
        remaining, _missing = _snapshot_many(roots) if roots else ({}, [])
        for pid, _start in sorted(remaining, reverse=True):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    for process in process_rows:
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                process.wait(timeout=1)
    survivors = []
    for pid, start in identities:
        current = _proc_stat(pid)
        if current is not None and str(current.get("start")) == str(start):
            survivors.append({"pid": pid, "start": str(start)})
    roots_result = [{"pid": process.pid, "returncode": process.poll(),
                     "reaped": process.returncode is not None}
                    for process in process_rows]
    return {
        "root_processes": roots_result,
        "descendant_identities_seen": len(identities),
        "unreadable_at_cleanup_start": unreadable,
        "surviving_identities": survivors,
        "ok": all(row["reaped"] for row in roots_result) and not survivors,
    }


def _stop_state_runners(state_root):
    """Stop only a runner carrying this fixture's unique peer-state root."""
    marker = ("AGENT_PEER_LEDGER_ROOT=" + str(Path(state_root).resolve())).encode()
    seen = {}

    def matching_runners():
        matching = []
        for name in os.listdir("/proc"):
            if not name.isdigit() or int(name) == os.getpid():
                continue
            pid = int(name)
            argv = _cmdline(pid)
            if "__obligation-runner" not in argv or not any(
                    "peer-steward.py" in part for part in argv):
                continue
            try:
                environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            except OSError:
                continue
            if marker in environ:
                stat_row = _proc_stat(pid)
                identity = {"pid": pid,
                            "start": str(stat_row.get("start")) if stat_row else None}
                matching.append(identity)
                seen[(pid, identity["start"])] = identity
        return matching

    for _round in range(10):
        matching = matching_runners()
        if not matching:
            break
        for identity in matching:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(identity["pid"], signal.SIGTERM)
        time.sleep(0.1)
    remaining = matching_runners()
    for identity in remaining:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(identity["pid"], signal.SIGKILL)
    if remaining:
        time.sleep(0.1)
    survivors = matching_runners()
    return {"seen_runner_identities": list(seen.values()),
            "surviving_runner_identities": survivors,
            "ok": not survivors}


def _snapshot_many(root_pids):
    all_rows = {}
    unreadable = []
    for root in root_pids:
        rows, missing = _snapshot(root)
        all_rows.update(rows)
        unreadable.extend(missing)
    return all_rows, unreadable


def _run_carrier_case(source, candidate, jobs, workload, data_root, case_root,
                      consumer_count):
    case_root = Path(case_root)
    source_jobs = Path(jobs)
    state_root = case_root / "peer-state"
    state_root.mkdir(parents=True)
    jobs = state_root / "jobs.log"
    shutil.copyfile(source_jobs, jobs)
    if _hash_file(jobs) != workload["sha256"]:
        raise AssertionError("carrier case did not receive the canonical workload bytes")
    attempts = [f"att-readiness-cpu-{index:02d}" for index in range(consumer_count)]
    consumers = []

    def launch():
        for attempt in attempts:
            consumers.append(_spawn_carrier(
                source, jobs, attempt, state_root, data_root, case_root))
            time.sleep(0.025)

    try:
        cold = _measure(
            os.getpid(), ready=lambda: _carrier_ready(consumers, candidate, state_root),
            prepare=launch, timeout=90, state_root=state_root)
        warm = _measure(os.getpid(), seconds=DEFAULT_WINDOW, state_root=state_root)
        event_digest = _mutate_unrelated_row(jobs, workload, b"u")
        unrelated = _measure(os.getpid(), seconds=DEFAULT_WINDOW, state_root=state_root)
        sample_end = unrelated["end_monotonic"]
        _wait_processes(consumers, CARRIER_DEADLINE + 10)
        for consumer in consumers:
            result = consumer["result"] or {}
            if result.get("wait_state") != "timeout":
                raise AssertionError(f"carrier did not stay on actual pending branch: {result}")
        return {
            "consumers": consumer_count,
            "jobs_path": str(jobs),
            "initial_workload_sha256": workload["sha256"],
            "selection_shape": "one exact attempt" if consumer_count == 1 else
                               "15 distinct exact pending attempts sharing one registry",
            "cold_start": cold,
            "warm_unchanged_12s": warm,
            "unrelated_event_12s": unrelated,
            "unrelated_event_kind": "unrelated registry row changed in place; target attempts unchanged",
            "unrelated_event_workload_sha256": event_digest,
            "branch_evidence": {
                "real_pending_branch": True,
                "helper_results": [event["result"] for row in consumers
                                   for event in row.get("helper_events", [])],
                "total_helper_launches": sum(len(row.get("helper_events", [])) for row in consumers),
                "helper_peak_concurrency": warm["classes"].get(
                    "readiness-helper", {}).get("peak_processes", 0),
                "carrier_results": [row.get("result") for row in consumers],
                "post_window_completion": [
                    _carrier_post_window(row, unrelated) for row in consumers
                    if row.get("result")],
                "window_end_monotonic": sample_end,
            },
        }
    finally:
        cleanup = {
            "consumers": _terminate_processes(consumers),
            "runner": _stop_state_runners(state_root),
        }
        _record_fixture_cleanup(f"carrier/{case_root.name}", cleanup)


def _carrier_post_window(consumer, final_window):
    result = consumer.get("result") or {}
    final_stat = result.get("carrier_stat") or {}
    identity = next((row for row in final_window["raw_pid_counters"]["end"]
                     if row["pid"] == consumer["process"].pid), None)
    boundary = identity or {}
    own_overrun = max(0, final_stat.get("own_cpu_ticks", 0) - boundary.get("own_cpu_ticks", 0))
    child_overrun = max(0, final_stat.get("reaped_child_cpu_ticks", 0)
                        - boundary.get("reaped_child_cpu_ticks", 0))
    wall_at_return = result.get("wait_started_monotonic", consumer["started"]) \
        + result.get("wait_elapsed_seconds", 0)
    return {
        "attempt_id": consumer["attempt_id"],
        "completion_overrun_seconds": max(0.0, wall_at_return - final_window["end_monotonic"]),
        "post_window_own_cpu_ticks": own_overrun,
        "post_window_reaped_child_cpu_ticks": child_overrun,
        "boundary_pid_reading": "present" if identity else "missing-or-exited-before-final-boundary",
    }


class FixtureObservationEndpoint:
    """Pipe-backed runner snapshots and fixture-only watcher wake signals."""

    def __init__(self, path, target_records):
        self.path = Path(path)
        self.target_records = target_records
        self.lock = threading.Lock()
        self.watchers = []
        self.snapshot_path = self.path.with_suffix(".snapshots.jsonl")
        self.event_deliveries = 0
        self.snapshot_census = []
        self.wake_deliveries = 0

    def register_watchers(self, processes):
        with self.lock:
            self.watchers = list(processes)

    def publish_unrelated(self):
        with self.lock:
            watchers = list(self.watchers)
        delivered = 0
        for row in watchers:
            process = row["process"]
            if process.poll() is not None:
                continue
            try:
                os.kill(process.pid, signal.SIGUSR1)
                delivered += 1
            except (OSError, ProcessLookupError):
                continue
        with self.lock:
            self.event_deliveries += delivered
            self.wake_deliveries += delivered
        return delivered

    def evidence(self):
        with self.lock:
            watchers = list(self.watchers)
            deliveries = self.event_deliveries
        try:
            census = [json.loads(line) for line in self.snapshot_path.read_text(
                encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError):
            census = []
        records = []
        for row in self.target_records.values():
            shell = _proc_stat(row["shell_pid"])
            workers = []
            for pid in row["worker_pids"]:
                item = _proc_stat(pid)
                index = row["worker_pids"].index(pid)
                expected_start = row["worker_starts"][index]
                workers.append({"pid": pid, "start": item["start"] if item else None,
                                "expected_start": expected_start,
                                "birth_valid": bool(item and item["start"] == expected_start),
                                "live": bool(item)})
            records.append({"target": row["target"], "session_id": row["session_id"],
                            "pane": row["pane"], "shell_pid": row["shell_pid"],
                            "shell_start": row["shell_start"],
                            "shell_birth_valid": bool(shell and shell["start"] == row["shell_start"]),
                            "bound_process_births": workers})
        names = sorted({agent["name"] for item in census
                        for agent in item.get("agents", [])})
        fixture_names = sorted({row["target"] for row in self.target_records.values()})
        samples = (census[:1] if len(census) <= 1 else [census[0], census[-1]])
        return {"request_counts": {"events.subscribe": 0,
                                    "session.snapshot": len(census)},
                "active_watchers": sum(row["process"].poll() is None for row in watchers),
                "unrelated_event_deliveries": deliveries,
                "unrelated_event_transport": "test-only SIGUSR1 wake surrogate",
                "intended_agents": records, "fixture_target_names": fixture_names,
                "runner_observed_agent_names": names,
                "snapshot_census_count": len(census),
                "snapshot_census_samples": samples}

    def close(self):
        return {"socket_bind_used": False,
                "transport": "pipe-backed runner snapshot; watcher wake signal",
                "ok": True}


_RUNNER_BOOTSTRAP = r'''import fcntl, importlib.util, json, os, sys, time, traceback
from pathlib import Path
from types import SimpleNamespace

source = Path(sys.argv[1]).resolve()
state_root = Path(sys.argv[2]).resolve()
snapshot_path = Path(sys.argv[3])
ready_fd = int(sys.argv[4])
sys.path.insert(0, str(source / "utilities"))
try:
    module_path = source / "utilities" / "peer-steward.py"
    spec = importlib.util.spec_from_file_location("cpu_fixture_peer_steward", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    store = module.peer_obligations.ObligationStore(state_root)
    store.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stall_root = os.environ.get("FIXTURE_RUNNER_STALL_ROOT")
    if stall_root:
        stall_root = Path(stall_root)
        stall_counter = [0]
        def stall_slow_lane(*_args, **_kwargs):
            stall_counter[0] += 1
            sequence = stall_counter[0]
            entered = stall_root / ("entered-%02d" % sequence)
            release = stall_root / ("release-%02d" % sequence)
            entered.write_text(str(time.monotonic()), encoding="ascii")
            while not release.exists():
                time.sleep(0.01)
            return 0
        module.dispatch_batch_obligations.ensure_observers = stall_slow_lane
    lock_fd = os.open(store.root / module.peer_obligations.RUNNER_LOCK_NAME,
                      os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    os.write(ready_fd, b"R")
    os.close(ready_fd)
    if sys.stdin.buffer.readline().strip() != b"go":
        raise RuntimeError("fixture runner start signal missing")

    class PipeObservationTransport:
        def __init__(self):
            self.read_fd, self.write_fd = os.pipe()
            os.set_blocking(self.read_fd, False)
            os.set_blocking(self.write_fd, False)

        def fileno(self):
            return self.read_fd if self.read_fd >= 0 else None

        def observe(self):
            while self.read_fd >= 0:
                try:
                    if not os.read(self.read_fd, 4096):
                        break
                except BlockingIOError:
                    break
                except OSError:
                    break
            target_map = json.loads(os.environ.get("FAKE_HERDR_TARGETS", "{}"))
            agents = [{"agent": "claude", "agent_session": {"value": row["session_id"]},
                       "agent_status": "idle", "name": row["target"],
                       "pane_id": row["pane"]} for row in target_map.values()]
            with snapshot_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"agents": agents}, sort_keys=True) + "\n")
            return SimpleNamespace(agents=tuple(agents))

        def close(self):
            for name in ("read_fd", "write_fd"):
                fd = getattr(self, name)
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    setattr(self, name, -1)

    transport = PipeObservationTransport()
    getattr(module, "_RUNNER_READINESS_TRANSPORTS", {}).clear()
    module._runner_readiness_transport = lambda _server, endpoint=None: transport
    code = module.cmd_obligation_runner(SimpleNamespace(
        lock_fd=lock_fd, state_root=state_root, service_current=None))
    transport.close()
    os._exit(int(code or 0))
except BaseException:
    traceback.print_exc()
    try:
        os.write(ready_fd, b"E")
    except OSError:
        pass
    os._exit(1)
'''


def _start_fixture_runner(source, state_root, endpoint, env, case_root):
    ready_read, ready_write = os.pipe()
    log_path = Path(case_root) / "shared-runner.log"
    command = [sys.executable, "-c", _RUNNER_BOOTSTRAP, str(source),
               str(state_root), str(endpoint.snapshot_path), str(ready_write),
               "__obligation-runner"]
    process = None
    with log_path.open("ab") as log_stream:
        try:
            process = subprocess.Popen(
                command, cwd=str(source), env=env, stdin=subprocess.PIPE,
                stdout=log_stream, stderr=subprocess.STDOUT, close_fds=True,
                pass_fds=(ready_write,), start_new_session=True)
        finally:
            os.close(ready_write)
    try:
        ready = os.read(ready_read, 1)
    finally:
        os.close(ready_read)
    if ready != b"R":
        if process is not None and process.poll() is None:
            with contextlib.suppress(Exception):
                process.terminate()
                process.wait(timeout=2)
        try:
            log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-1600:]
        except OSError:
            log_tail = "runner log unavailable"
        raise RuntimeError(
            f"pipe-backed shared runner failed to lock its state root; "
            f"ready={ready!r}; log_tail={log_tail}")
    return process


def _write_legacy_attempt(jobs, attempt_id, status):
    path = Path(jobs)
    rows = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    rows = [row for row in rows if f"attempt_id={attempt_id}" not in row]
    rows.append("2026-10-24T00:00:00Z\t%s\t/repo\t/wt\tlegacy-worker\tattempt_id=%s"
                % (status, attempt_id))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _measure_runner_readiness_stalls(source, data_root, case_root, *, sample_count=20):
    """Measure close-to-ready latency while the real runner blocks in its slow lane."""
    import importlib.util
    from types import SimpleNamespace

    source = Path(source).resolve(strict=True)
    case_root = Path(case_root)
    case_root.mkdir(parents=True, exist_ok=True)
    state_root = case_root / "peer-state"
    state_root.mkdir()
    jobs = state_root / "jobs.log"
    stall_root = case_root / "slow-lane"
    stall_root.mkdir()
    stall_runner_log = case_root / "stall-runner-snapshots.jsonl"
    env = _fixture_env(source, data_root, state_root, jobs, case_root)
    env["FIXTURE_RUNNER_STALL_ROOT"] = str(stall_root)

    module_name = "fixture_shared_readiness_" + hashlib.sha256(
        str(source).encode()).hexdigest()[:12]
    module_path = source / "utilities" / "shared_readiness.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"shared readiness module unavailable: {module_path}")
    readiness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(readiness)

    sys.path.insert(0, str(source / "utilities"))
    try:
        import peer_obligations
        from dispatch_contract import process_start_ticks
        import dispatch_registry_cache

        store = peer_obligations.ObligationStore(state_root)
        process = None
        raw = []
        try:
            _write_legacy_attempt(jobs, "att-stall-00", "running")
            endpoint = SimpleNamespace(snapshot_path=stall_runner_log)
            process = _start_fixture_runner(
                source, state_root, endpoint, env, case_root)
            runner_stat = _proc_stat(process.pid)
            if runner_stat is None:
                raise RuntimeError("actual obligation runner identity is unavailable")
            if process.stdin is None:
                raise RuntimeError("stall runner stdin is unavailable")
            process.stdin.write(b"go\n")
            process.stdin.flush()

            previous_duty_id = None
            for index in range(sample_count):
                attempt = f"att-stall-{index:02d}"
                if index:
                    _write_legacy_attempt(jobs, attempt, "running")
                consumer_start = str(process_start_ticks(os.getpid()) or "")
                identity = readiness.attempt_identity(
                    jobs, attempt, parent="", slug="", consumer_pid=os.getpid(),
                    consumer_start=consumer_start,
                    request_token=f"paired-stall-{index:02d}")
                duty_id = readiness.duty_id(identity)
                store.create(duty_id, readiness.DUTY_KIND, identity,
                    {"jobs": str(jobs), "attempt_id": attempt, "parent": "", "slug": ""})
                if previous_duty_id:
                    store.update(previous_duty_id, state="complete",
                                 result="consumer-finished", cleanup="complete")
                previous_duty_id = duty_id

                entered_path = stall_root / f"entered-{index + 1:02d}"
                release_path = stall_root / f"release-{index + 1:02d}"
                deadline = time.monotonic() + 30
                while not entered_path.is_file():
                    if process.poll() is not None:
                        raise RuntimeError("slow-lane runner exited before its barrier")
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"runner did not enter slow lane for sample {index + 1}")
                    time.sleep(0.01)
                try:
                    entered_at = float(entered_path.read_text(encoding="ascii"))
                except (OSError, ValueError):
                    entered_at = time.monotonic()

                _write_legacy_attempt(jobs, attempt, "done")
                closed_at = time.monotonic()
                dispatch_registry_cache.clear_cache()
                view = dispatch_registry_cache.registry_view(jobs)
                expected_generation = list(view.signature)
                stall_end = entered_at + 20.0
                ready_at = None
                observed_projection = None

                def observe_ready():
                    nonlocal observed_projection
                    current = store.get(duty_id)
                    result = readiness.current_result(current, identity)
                    projection = ((current.get("observation") or {}).get("shared_readiness")
                                  if isinstance(current, dict) else None)
                    if (result and result.get("state") == "ready"
                            and isinstance(projection, dict)
                            and projection.get("generation") == expected_generation):
                        observed_projection = projection
                        return time.monotonic()
                    return None

                while time.monotonic() < stall_end:
                    if ready_at is None:
                        ready_at = observe_ready()
                    time.sleep(0.02)
                release_path.touch()
                released_at = time.monotonic()
                if ready_at is None:
                    deadline = time.monotonic() + 35
                    while ready_at is None:
                        if process.poll() is not None:
                            raise RuntimeError("slow-lane runner exited before ready projection")
                        ready_at = observe_ready()
                        if ready_at is not None:
                            break
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"ready projection timed out for sample {index + 1}")
                        time.sleep(0.02)
                raw.append({
                    "sample": index + 1,
                    "attempt_id": attempt,
                    "runner_pid": process.pid,
                    "runner_start": str(runner_stat["start"]),
                    "slow_lane_entered_monotonic": entered_at,
                    "slow_lane_released_monotonic": released_at,
                    "close_monotonic": closed_at,
                    "ready_monotonic": ready_at,
                    "close_to_ready_seconds": max(0.0, ready_at - closed_at),
                    "runner_slow_lane_seconds": max(0.0, released_at - entered_at),
                    "projection_observed_at": observed_projection.get("observed_at"),
                    "projection_generation": observed_projection.get("generation"),
                    "ready_before_slow_lane_release": ready_at < released_at,
                })

            latencies = sorted(item["close_to_ready_seconds"] for item in raw)
            rank = max(1, math.ceil(0.95 * len(latencies)))
            return {"sample_count": len(raw), "samples": raw,
                    "nearest_rank_p95_seconds": latencies[rank - 1],
                    "median_seconds": latencies[(len(latencies) - 1) // 2],
                    "all_slowlane_stalls_seconds": [item["runner_slow_lane_seconds"]
                                                     for item in raw],
                    "projections_before_slow_lane_release": sum(
                        item["ready_before_slow_lane_release"] for item in raw),
                    "pass": len(raw) >= 20 and latencies[rank - 1] <= 10.0}
        finally:
            if process is not None:
                if process.stdin is not None:
                    with contextlib.suppress(Exception):
                        process.stdin.close()
                runner_cleanup = _terminate_processes([process])
                state_cleanup = _stop_state_runners(state_root)
                _record_fixture_cleanup("readiness-stall-" + source.name,
                                        {"processes": runner_cleanup,
                                         "state_runners": state_cleanup})
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(str(source / "utilities"))


_DENSITY_PROBE = r'''import json, os, statistics, sys, time
from pathlib import Path
from unittest import mock

source, root_text = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
sys.path.insert(0, str(source / "utilities"))
import dispatch_batch_obligations as batch
import dispatch_registry_cache as registry
import dispatch_terminal_commit
import peer_obligations

def row(attempt):
    meta = ",".join(("attempt_schema_version=2", "dispatch_depth=1",
        "worker_type=owner", "launch_started=1", "supervisor_lease=flock-v1",
        f"supervisor_lease_file=/fixture/{attempt}.lease",
        f"log_file=/fixture/{attempt}.log", f"attempt_id={attempt}",
        "workflow_completion=runtime-v1", "note=completed-supervisor"))
    return f"2026-10-20T00:00:00Z\tdone\t/repo\t/wt\towner\t{meta}\n"

cell_specs = ((0, 0), (1000, 0), (0, 1000), (1000, 1000))
cases = {}
dependency_roots = {}
proof_calls = {}
for owner_count, terminal_count in cell_specs:
    case = root_text / f"owners-{owner_count}-terminal-{terminal_count}"
    case.mkdir(parents=True)
    state_root, jobs = case / "peer-state", case / "jobs.log"
    state_root.mkdir()
    jobs.write_text("".join(row(f"owner-{index:04d}")
                            for index in range(owner_count)), encoding="utf-8")
    dependencies = case / "sealed-dependencies"
    dependencies.mkdir()
    for index in range(owner_count):
        (dependencies / f"owner-{index:04d}.json").write_text(
            "settled", encoding="utf-8")
    store = peer_obligations.ObligationStore(state_root)
    for index in range(terminal_count):
        duty_id = f"terminal-{index:04d}"
        duty = store.create(duty_id, "readiness-observation",
            {"consumer": {"pid": 2_000_000 + index, "pid_start": "1"}}, {})
        store.update(duty["id"], state="complete", result="consumer-exited")
    store.list()
    store.reset_discovery_metrics()
    context = batch.OwnerSettlementReadContext()
    cell = f"owners-{owner_count}-terminal-{terminal_count}"
    jobs_key = str(jobs.resolve())
    cases[cell] = {"owner_count": owner_count, "terminal_count": terminal_count,
                   "jobs": jobs, "store": store, "context": context,
                   "samples": []}
    dependency_roots[jobs_key] = dependencies
    proof_calls[jobs_key] = 0

def settled(jobs, *_args, **_kwargs):
    proof_calls[str(Path(jobs).resolve())] += 1
    return False

def exact_dependency(jobs, _status, metadata):
    base = dependency_roots[str(Path(jobs).resolve())]
    suffix = metadata["attempt_id"].removeprefix("owner-")
    return (base / f"owner-{suffix}.json",)

if hasattr(batch, "_owner_settlement_dependency_paths"):
    dependency_patch = mock.patch.object(batch,
        "_owner_settlement_dependency_paths", side_effect=exact_dependency)
else:
    dependency_patch = mock.patch.object(batch, "_pending_owner_settlements",
        wraps=batch._pending_owner_settlements)

with mock.patch.object(dispatch_terminal_commit, "owner_completion_pending",
                       side_effect=settled), dependency_patch:
    for case in cases.values():
        batch._resume_owner_settlements(case["jobs"], context=case["context"])

    # Interleave matched cells to keep warm-up and timer drift from aligning
    # with one density level; each repetition measures all four cells.
    cell_names = tuple(cases)
    for repeat in range(7):
        order = tuple(cell_names[(repeat + offset) % len(cell_names)]
                      for offset in range(len(cell_names)))
        for cell_name in order:
            case = cases[cell_name]
            jobs, store, context = case["jobs"], case["store"], case["context"]
            with jobs.open("a", encoding="utf-8") as handle:
                handle.write(f"2026-10-21T00:00:00Z\tdone\t/repo\t/wt\t"
                             f"unrelated-{repeat}\tattempt_id=unrelated-{repeat}\n")
            jobs_key = str(jobs.resolve())
            proof_before = proof_calls[jobs_key]
            store.create(f"pass-active-{repeat}", "message", {}, {})
            store.reset_discovery_metrics()
            started = time.perf_counter()
            batch._resume_owner_settlements(jobs, context=context)
            active_records = store.list()
            elapsed = time.perf_counter() - started
            after = store.discovery_metrics()
            terminal_stats = max(0, after.get("stat_calls", 0)
                                - len(active_records) - 1)
            case["samples"].append({
                "repeat": repeat + 1,
                "second_pass_seconds": elapsed,
                "historical_proof_calls": proof_calls[jobs_key] - proof_before,
                "store_stat_calls": after.get("stat_calls", 0),
                "terminal_stat_calls": terminal_stats,
                "terminal_cache_hits": after.get("terminal_cache_hits", 0),
                "directory_entries": after.get("directory_entries", 0),
                "json_reads": after.get("json_reads", 0),
                "active_index_entries": after.get("active_index_entries", 0),
                "active_full_scans": after.get("active_full_scans", 0),
            })

matrix = {}
for cell_name, case in cases.items():
    samples = case["samples"]
    matrix[cell_name] = {
        "owner_count": case["owner_count"],
        "terminal_count": case["terminal_count"],
        "repetitions": samples,
        "mean_second_pass_seconds": statistics.mean(
            row["second_pass_seconds"] for row in samples),
        "stdev_second_pass_seconds": statistics.stdev(
            row["second_pass_seconds"] for row in samples),
    }
print(json.dumps({"matrix": matrix}, sort_keys=True))
'''


def _measure_scheduler_density(source, case_root):
    command = [sys.executable, "-c", _DENSITY_PROBE, str(source), str(case_root)]
    result = subprocess.run(command, cwd=str(source), capture_output=True,
                            text=True, timeout=300)
    if result.returncode:
        raise RuntimeError("density probe failed: " + result.stderr[-3000:])
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])["matrix"]
    except (IndexError, KeyError, ValueError) as exc:
        raise RuntimeError("density probe returned invalid evidence: " +
                           result.stdout[-1000:]) from exc


def _density_acceptance(matrix):
    candidate = matrix["candidate"]
    empty = candidate["owners-0-terminal-0"]["repetitions"]

    def contrast(key):
        high = candidate[key]["repetitions"]
        differences = [right["second_pass_seconds"] - left["second_pass_seconds"]
                       for left, right in zip(empty, high)]
        mean = statistics.mean(differences)
        margin = 1.96 * statistics.stdev(differences) / math.sqrt(len(differences))
        return {"paired_mean_change_seconds": mean,
                "paired_95pct_ci_seconds": [mean - margin, mean + margin],
                "no_positive_increase": mean - margin <= 0,
                "raw_paired_deltas_seconds": differences}

    owner_only = contrast("owners-1000-terminal-0")
    terminal_only = contrast("owners-0-terminal-1000")
    combined = contrast("owners-1000-terminal-1000")
    owner_rows = candidate["owners-1000-terminal-1000"]["repetitions"]
    terminal_rows = candidate["owners-0-terminal-1000"]["repetitions"]
    candidate_terminal_discovery = []
    for row in terminal_rows:
        candidate_terminal_discovery.append({
            "terminal_stat_calls": row["terminal_stat_calls"],
            "store_stat_calls": row["store_stat_calls"],
            "active_index_entries": row["active_index_entries"],
            "active_full_scans": row["active_full_scans"],
            "directory_entries": row["directory_entries"],
        })
    return {
        "owner_only": owner_only,
        "terminal_only": terminal_only,
        "combined": combined,
        "candidate_added_settled_owner_proof_calls": [
            row["historical_proof_calls"] for row in owner_rows],
        "candidate_terminal_discovery": candidate_terminal_discovery,
        "pass": (all(row["historical_proof_calls"] == 0 for row in owner_rows)
                 and all(row["terminal_stat_calls"] == 0
                         and row["active_full_scans"] == 0
                         for row in candidate_terminal_discovery)
                 and owner_only["no_positive_increase"]
                 and terminal_only["no_positive_increase"]
                 and combined["no_positive_increase"]),
    }


_FAKE_HERDR = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
if argv[:1] == ["--session"]:
    argv = argv[2:]
targets = json.loads(os.environ.get("FAKE_HERDR_TARGETS", "{}"))
verb = argv[1] if len(argv) > 1 else ""
target = argv[2] if len(argv) > 2 else ""
if verb == "process-info" or argv[:2] == ["pane", "process-info"]:
    pane = argv[-1]
    record = next((row for row in targets.values() if row["pane"] == pane), None)
    if record is None:
        sys.stderr.write(json.dumps({"error": {"code": "pane_not_found"}})); sys.exit(1)
    foreground = []
    for pid in record["worker_pids"]:
        try:
            cmdline = [part.decode("utf-8", "replace") for part in
                       open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0") if part]
            stat = open(f"/proc/{pid}/stat", encoding="ascii").read()
            tail = stat[stat.rfind(")") + 2:].split()
            foreground.append({"pid": pid, "argv": cmdline, "start": tail[19]})
        except (OSError, IndexError):
            continue
    print(json.dumps({"result": {"process_info": {
        "pane_id": pane, "shell_pid": record["shell_pid"],
        "foreground_process_group_id": foreground[0]["pid"] if foreground else record["shell_pid"],
        "foreground_processes": foreground}}}))
    sys.exit(0)
record = targets.get(target)
if record is None:
    sys.stderr.write(json.dumps({"error": {"code": "agent_not_found"}})); sys.exit(1)
payload = {"id": "fixture-herdr", "result": {"agent": {
    "agent": "claude", "agent_session": {"value": record["session_id"]},
    "agent_status": "idle", "name": record["target"], "pane_id": record["pane"]
}, "type": "agent_info"}}
print(json.dumps(payload))
'''


def _make_fake_herdr(fake_bin):
    fake_bin = Path(fake_bin)
    fake_bin.mkdir(parents=True, exist_ok=True)
    path = fake_bin / "herdr"
    path.write_text(_FAKE_HERDR, encoding="utf-8")
    path.chmod(0o755)
    return path


def _write_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _watch_observer_argv(watch_id, arm, lock_fd, current):
    agent, steward = arm["agent"], arm["steward"]
    argv = [
        "__watch-run", "--watch-id", watch_id, "--target", arm["target"],
        "--server", arm["server"], "--expected-harness", agent["harness"],
        "--expected-session-id", agent["session_id"], "--expected-pane", agent["pane"],
        "--steward-harness", steward["harness"], "--steward-session-id", steward["session_id"],
        "--steward-project", steward["project"], "--armed-ts", arm["armed_ts"],
        "--rearm-count", "1", "--lock-fd", str(lock_fd), "--until", "idle",
        "--timeout", str(arm["timeout"]), "--service-current", str(current),
    ]
    return argv


def _spawn_watchers(source, data_root, state_root, jobs, case_root, endpoint,
                    target_records, watch_specs):
    fake_bin = Path(case_root) / "fakebin"
    _make_fake_herdr(fake_bin)
    processes = []
    watch_root = Path(state_root) / "peer-watches"
    watch_root.mkdir(parents=True, exist_ok=True)
    current = Path(data_root) / "current"
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    for index, target_key in enumerate(watch_specs):
        record = target_records[target_key]
        watch_id = f"{index + 1:016x}"
        steward_sid = f"sid-steward-{index + 1:02d}"
        armed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        arm = {
            "schema_version": 1, "watch_id": watch_id, "target": record["target"],
            "until": ["idle"], "timeout": 600_000,
            "deadline": {"requested_ms": 600_000, "wall_deadline": time.time() + 600,
                         "monotonic_deadline": time.monotonic() + 600, "boot_id": boot},
            "refs": [], "server": "default", "armed_ts": armed,
            "agent": {"harness": "claude", "session_id": record["session_id"],
                      "name": record["target"], "pane": record["pane"]},
            "steward": {"harness": "codex", "session_id": steward_sid,
                        "project": str(source)},
            "observer_protocol": 2, "observer_generation": 1, "rearm_count": 0,
            "watcher": {},
            "foreground_identity": {"pid": record["worker_pids"][0],
                                    "start": _proc_stat(record["worker_pids"][0])["start"],
                                    "shell_pid": record["shell_pid"],
                                    "shell_start": record["shell_start"]},
            "pane_identity": {"shell_pid": record["shell_pid"],
                              "shell_start": record["shell_start"]},
        }
        arm_path = watch_root / f"{watch_id}.json"
        lock_path = watch_root / f"{watch_id}.lock"
        log_path = watch_root / f"{watch_id}.log"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _write_atomic(arm_path, arm)
        argv = _watch_observer_argv(watch_id, arm, lock_fd, current)
        log_fd = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        env = _fixture_env(source, data_root, state_root, jobs, case_root,
                           fake_bin=fake_bin, endpoint=endpoint,
                           targets=target_records, steward_session=steward_sid)
        try:
            process = subprocess.Popen(
                [sys.executable, str(Path(source) / "utilities" / "peer-steward.py"), *argv],
                cwd=str(watch_root), env=env, stdin=subprocess.DEVNULL,
                stdout=log_fd, stderr=subprocess.STDOUT, close_fds=True,
                pass_fds=(lock_fd,), start_new_session=True)
        finally:
            os.close(log_fd)
            os.close(lock_fd)
        arm["watcher"] = {"pid": process.pid,
                          "pid_start": _proc_stat(process.pid)["start"]}
        _write_atomic(arm_path, arm)
        processes.append({"process": process, "watch_id": watch_id,
                          "target": record["target"], "arm_path": arm_path,
                          "observation_path": watch_root / f"{watch_id}.observation.json",
                          "receipt_path": watch_root / f"{watch_id}.receipt.json",
                          "log_path": log_path, "steward_session": steward_sid})
    if processes:
        ensure_env = _fixture_env(source, data_root, state_root, jobs, case_root,
                                  fake_bin=fake_bin, endpoint=endpoint,
                                  targets=target_records)
        ensure = subprocess.run(
            [sys.executable, str(Path(source) / "utilities" / "peer-steward.py"),
             "__ensure-obligations", "--lifecycle-root", str(source)],
            cwd=str(source), env=ensure_env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20,
            check=False)
        if ensure.returncode != 0:
            raise RuntimeError(f"existing obligation runner setup failed: {ensure.stdout[-1500:]}")
    return processes, fake_bin


def _watch_pending(processes):
    observations = []
    for row in processes:
        if row["process"].poll() is not None:
            return False
        observation = _read_json(row["observation_path"])
        native = (observation or {}).get("native_wait") or {}
        if (not observation or observation.get("state") != "pending"
                or observation.get("reason") != "bound-registered-work-pending"
                or native.get("state") != "result"
                or native.get("agent_state") not in {"idle", "done"}):
            return False
        if row["receipt_path"].exists():
            return False
        observations.append(observation)
    return len(observations) == len(processes)


def _watch_diagnostics(processes):
    return [{"watch_id": row["watch_id"], "target": row["target"],
             "returncode": row["process"].poll(),
             "observation": _read_json(row["observation_path"]),
             "receipt": _read_json(row["receipt_path"]),
             "log_tail": (row["log_path"].read_text(errors="replace")[-1200:]
                          if row["log_path"].exists() else "")}
            for row in processes]


def _run_watch_case(source, candidate, jobs, data_root, case_root,
                    target_records, watch_specs):
    case_root = Path(case_root)
    source_jobs = Path(jobs)
    state_root = case_root / "peer-state"
    state_root.mkdir(parents=True)
    jobs = state_root / "jobs.log"
    workload_digest = _hash_file(source_jobs)
    shutil.copyfile(source_jobs, jobs)
    if _hash_file(jobs) != workload_digest:
        raise AssertionError("peer-watch case did not receive the canonical workload bytes")
    endpoint = FixtureObservationEndpoint(case_root / "herdr.sock", target_records)
    processes = []
    fake_bin = None
    runner_process = None
    try:
        def launch():
            nonlocal processes, fake_bin, runner_process
            fake_bin = Path(case_root) / "fakebin"
            _make_fake_herdr(fake_bin)
            runner_env = _fixture_env(
                source, data_root, state_root, jobs, case_root, fake_bin=fake_bin,
                endpoint=endpoint, targets=target_records, steward_session="sid-runner")
            runner_process = _start_fixture_runner(
                source, state_root, endpoint, runner_env, case_root)
            processes, fake_bin = _spawn_watchers(
                source, data_root, state_root, jobs, case_root, endpoint,
                target_records, watch_specs)
            endpoint.register_watchers(processes)
            runner_process.stdin.write(b"go\n")
            runner_process.stdin.flush()

        try:
            cold = _measure(os.getpid(), ready=lambda: _watch_pending(processes),
                            prepare=launch, timeout=45, state_root=state_root)
        except TimeoutError as exc:
            detail = {"watchers": _watch_diagnostics(processes),
                      "endpoint": endpoint.evidence()}
            raise TimeoutError(
                f"{exc}; watcher_diagnostics={json.dumps(detail, sort_keys=True)}") from exc
        if endpoint.evidence()["active_watchers"] < len(processes):
            raise AssertionError("not all production watchers remained alive after readiness pending")
        warm = _measure(os.getpid(), seconds=DEFAULT_WINDOW, state_root=state_root)
        delivered = endpoint.publish_unrelated()
        unrelated = _measure(os.getpid(), seconds=DEFAULT_WINDOW, state_root=state_root)
        if delivered < len(processes):
            raise AssertionError(f"unrelated event did not reach every watcher: {delivered}")
        if not _watch_pending(processes):
            raise AssertionError(f"unrelated event changed a pending watch: {_watch_diagnostics(processes)}")
        observations = [_read_json(row["observation_path"]) for row in processes]
        census = endpoint.evidence()
        expected_targets = sorted({target_records[key]["target"] for key in watch_specs})
        fixture_targets = census["fixture_target_names"]
        runner_targets = set(census["runner_observed_agent_names"])
        if not set(expected_targets).issubset(set(fixture_targets)):
            raise AssertionError(f"pipe-backed runner fixture omitted intended targets: {census}")
        if candidate and (census["snapshot_census_count"] < 1
                          or not set(expected_targets).issubset(runner_targets)):
            raise AssertionError(f"candidate runner did not observe the intended target census: {census}")
        if not all(row["shell_birth_valid"]
                   and all(worker["birth_valid"] for worker in row["bound_process_births"])
                   for row in census["intended_agents"]):
            raise AssertionError(f"endpoint census has invalid process birth evidence: {census}")
        return {
            "watch_count": len(watch_specs),
            "jobs_path": str(jobs),
            "initial_workload_sha256": workload_digest,
            "watch_target_shape": ("one target" if len(set(watch_specs)) == 1 else
                                   "five same-target watches plus one separate-target isolation control"),
            "cold_start": cold,
            "warm_unchanged_12s": warm,
            "unrelated_event_12s": unrelated,
            "branch_evidence": {
                "native_result_idle_pending": True,
                "native_wait_states": [row["native_wait"] for row in observations],
                "watch_ids": [row["watch_id"] for row in processes],
                "steward_sessions": [row["steward_session"] for row in processes],
                "distinct_watch_ids": len({row["watch_id"] for row in processes}) == len(processes),
                "distinct_steward_sessions": len({row["steward_session"] for row in processes}) == len(processes),
                "endpoint_census": census,
                "unrelated_event_transport": "test-only SIGUSR1 watcher wake; no Herdr Unix socket bind",
                "unrelated_event_reached_watchers": delivered,
                "receipts_created_while_registered_work_live": 0,
                "observation_reason": [row["reason"] for row in observations],
            },
        }
    finally:
        cleanup = {"watchers": _terminate_processes(processes)}
        for row in processes:
            with contextlib.suppress(OSError):
                row["arm_path"].unlink()
        if runner_process is not None and runner_process.stdin is not None:
            with contextlib.suppress(OSError):
                runner_process.stdin.close()
            cleanup["runner_process"] = _terminate_processes([runner_process])
        else:
            cleanup["runner_process"] = {"ok": True, "started": False}
        cleanup["runner"] = _stop_state_runners(state_root)
        cleanup["observation_endpoint"] = endpoint.close()
        _record_fixture_cleanup(f"peer-watch/{case_root.name}", cleanup)


def _content_fingerprint(root):
    root = Path(root).resolve()
    digest = hashlib.sha256()
    count = total_bytes = 0
    for relative_root in ("core", "hooks", "utilities", "tools/install"):
        folder = root / relative_root
        if not folder.is_dir():
            continue
        for current, dirs, files in os.walk(folder):
            dirs[:] = sorted(name for name in dirs if name not in
                             {"__pycache__", "node_modules", "dist", "coverage"})
            for name in sorted(files):
                path = Path(current) / name
                if path.suffix in {".pyc", ".prof"}:
                    continue
                relative = path.relative_to(root).as_posix().encode()
                blob = path.read_bytes()
                digest.update(len(relative).to_bytes(4, "big"))
                digest.update(relative)
                digest.update(len(blob).to_bytes(8, "big"))
                digest.update(hashlib.sha256(blob).digest())
                count += 1
                total_bytes += len(blob)
    return {"content_sha256": digest.hexdigest(), "files": count,
            "bytes": total_bytes, "fingerprinted_roots": ["core", "hooks", "utilities", "tools/install"]}


def _git_provenance(root):
    """Report a revision only when Git's discovered top level is this source root."""
    root = Path(root).resolve(strict=True)
    top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, check=False)
    if top.returncode:
        return {"status": "unavailable", "top_level": None, "dirty": None}
    try:
        top_level = Path(top.stdout.strip()).resolve(strict=True)
    except OSError:
        return {"status": "unavailable", "top_level": top.stdout.strip(), "dirty": None}
    if top_level != root:
        return {"status": "different-source-root", "top_level": str(top_level),
                "dirty": None}

    revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=False)
    if revision.returncode:
        return {"status": "matched-source-root", "top_level": str(top_level),
                "revision": None, "dirty": None, "dirty_paths": []}
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all",
         "--", "core", "hooks", "utilities", "tools/install"],
        capture_output=True, text=True, check=False,
    )
    dirty_paths = ([line[3:] for line in status.stdout.splitlines() if len(line) >= 4]
                   if status.returncode == 0 else [])
    return {"status": "matched-source-root", "top_level": str(top_level),
            "revision": revision.stdout.strip(),
            "dirty": bool(dirty_paths) if status.returncode == 0 else None,
            "dirty_paths": dirty_paths,
            "dirty_scope": ["core", "hooks", "utilities", "tools/install"]}


def _verified_adjacent_manifest(root):
    """Use an adjacent manifest only when it names and verifies this exact archive."""
    root = Path(root).resolve(strict=True)
    candidates = []
    for path in sorted(root.parent.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            named_root = Path(payload.get("archive", "")).resolve(strict=False)
        except (OSError, ValueError, TypeError):
            continue
        if named_root == root:
            candidates.append((path, payload))
    if not candidates:
        return {"status": "not-found"}
    if len(candidates) != 1:
        return {"status": "ambiguous", "matching_manifests": [str(row[0]) for row in candidates]}

    path, payload = candidates[0]
    rows = payload.get("file_manifest")
    errors = []
    if not isinstance(rows, list) or not rows:
        errors.append("file_manifest is missing or empty")
        rows = []
    if payload.get("checked_files") != len(rows):
        errors.append("checked_files does not match file_manifest length")
    seen = set()
    checked_bytes = 0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            errors.append("manifest contains a malformed row")
            continue
        relative = PurePosixPath(row["path"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            errors.append(f"unsafe manifest path: {row['path']}")
            continue
        source = root.joinpath(*relative.parts)
        try:
            resolved = source.resolve(strict=True)
            if not resolved.is_relative_to(root) or source.is_symlink() or not source.is_file():
                raise OSError("manifest path is not a regular file inside the archive")
            blob = source.read_bytes()
            expected_size = row.get("bytes")
            expected_sha = row.get("sha256")
            expected_blob = row.get("git_blob")
            expected_mode = row.get("mode")
            actual_mode = "100755" if source.stat().st_mode & 0o111 else "100644"
            actual_blob = hashlib.sha1(
                b"blob " + str(len(blob)).encode("ascii") + b"\0" + blob).hexdigest()
            if (len(blob) != expected_size
                    or hashlib.sha256(blob).hexdigest() != expected_sha
                    or actual_blob != expected_blob or actual_mode != expected_mode):
                errors.append(f"content, Git blob, or mode mismatch: {row['path']}")
            checked_bytes += len(blob)
            if row["path"] in seen:
                errors.append(f"duplicate manifest path: {row['path']}")
            seen.add(row["path"])
        except OSError as exc:
            errors.append(f"{row['path']}: {exc}")
    if payload.get("checked_bytes") != checked_bytes:
        errors.append("checked_bytes does not match verified manifest bytes")
    return {
        "status": "invalid" if errors else "verified",
        "manifest_path": str(path),
        "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "files_verified": len(seen),
        "bytes_verified": checked_bytes,
        "verification_scope": "listed manifest files",
        "errors": errors,
    }


def _source_provenance(root):
    root = Path(root).resolve(strict=True)
    git = _git_provenance(root)
    fingerprint = _content_fingerprint(root)
    result = {
        "kind": "git-worktree" if git["status"] == "matched-source-root" else "source-tree",
        "path": str(root),
        **fingerprint,
        "git": git,
    }
    if git["status"] != "matched-source-root":
        result["archive_manifest"] = _verified_adjacent_manifest(root)
    return result


def _invocation_record(evidence_dir):
    evidence_dir = Path(evidence_dir).resolve()
    return {
        "executable": sys.executable,
        "argv": list(sys.argv),
        "cwd": str(Path.cwd().resolve()),
        "evidence_paths": {
            "json": str(evidence_dir / "readiness-cpu-fixture.json"),
            "comparison": str(evidence_dir / "readiness-cpu-comparison.md"),
            "cleanup": str(evidence_dir / "readiness-cpu-fixture-cleanup.json"),
        },
    }


def _copy_managed_source(source_root, destination):
    data_root = Path(destination)
    releases = data_root / "releases"
    releases.mkdir(parents=True)
    release = releases / "fixture"
    shutil.copytree(
        Path(source_root), release, symlinks=True,
        ignore=shutil.ignore_patterns(".git", ".agent_reports", ".claude_reports", ".dispatch",
                                      "__pycache__", "node_modules", "dist", "coverage"),
    )
    if not (release / "RELEASE_VERSION").is_file():
        (release / "RELEASE_VERSION").write_text("fixture\n", encoding="utf-8")
    if not (release / "harness-manifest.json").is_file():
        (release / "harness-manifest.json").write_text("{}\n", encoding="utf-8")
    (data_root / "current").symlink_to(release, target_is_directory=True)
    return data_root, release


def _start_bound_process(attempt_id):
    env = _clean_environment()
    env["AGENT_DISPATCH_ATTEMPT_ID"] = attempt_id
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(900) # fixture-bound-worker"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env, start_new_session=True)
    stat = _proc_stat(process.pid)
    if stat is None:
        process.terminate()
        raise RuntimeError(f"bound process identity unavailable: {process.pid}")
    identity = {
        "pid": str(process.pid), "pid_start": stat["start"],
        "pid_ns": os.readlink(f"/proc/{process.pid}/ns/pid"),
        "pid_observer_ns": os.readlink("/proc/self/ns/pid"),
    }
    return process, identity, _cmdline(process.pid)


def _make_attempt_row(attempt_id, session_id, process_identity):
    metadata = {
        "attempt_schema_version": "2", "dispatch_depth": "2", "transport": "headless",
        "execution_surface": "registered-headless", "registered_worker": "1",
        "fallback_hop": "same-harness-headless", "attempt_id": attempt_id,
        "harness": "codex", "parent_sid": session_id, "parent_harness": "claude",
        **process_identity,
    }
    pipe = ",".join(f"{key}={value}" for key, value in metadata.items())
    return f"2026-10-20T00:00:01Z\trunning\t/repo\t/wt\tworker\t{pipe}"


def _target_record(target, session_id, pane, shell_process, bound_processes):
    shell_stat = _proc_stat(shell_process.pid)
    if shell_stat is None:
        raise RuntimeError("target shell process exited during fixture setup")
    return {
        "target": target, "session_id": session_id, "pane": pane,
        "shell_pid": shell_process.pid, "shell_start": shell_stat["start"],
        "worker_pids": [row["process"].pid for row in bound_processes],
        "worker_starts": [row["identity"]["pid_start"] for row in bound_processes],
        "worker_argv": [row["argv"] for row in bound_processes],
    }


def _prepare_jobs(root):
    processes = []
    target_a_rows = []
    target_a_processes = []
    for index in range(PENDING_ATTEMPTS):
        attempt = f"att-readiness-cpu-{index:02d}"
        process, identity, argv = _start_bound_process(attempt)
        processes.append(process)
        record = {"process": process, "identity": identity, "argv": argv, "attempt_id": attempt}
        target_a_processes.append(record)
        target_a_rows.append(_make_attempt_row(attempt, "sid-target-a", identity))
    target_b_attempt = "att-readiness-separate-target"
    target_b_process, identity_b, argv_b = _start_bound_process(target_b_attempt)
    processes.append(target_b_process)
    target_b_processes = [{"process": target_b_process, "identity": identity_b,
                           "argv": argv_b, "attempt_id": target_b_attempt}]
    target_b_rows = [_make_attempt_row(target_b_attempt, "sid-target-b", identity_b)]
    shell_a = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(900) # fixture-shell-a"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    shell_b = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(900) # fixture-shell-b"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    processes.extend((shell_a, shell_b))
    target_records = {
        "a": _target_record("fixture-target-a", "sid-target-a", "w1:p9", shell_a,
                            target_a_processes),
        "b": _target_record("fixture-target-b", "sid-target-b", "w1:p10", shell_b,
                            target_b_processes),
    }
    all_rows = target_a_rows + target_b_rows
    jobs_path = Path(root) / "dispatch" / "jobs.log"
    workload = _write_jobs(jobs_path, TARGET_BYTES, all_rows)
    return processes, jobs_path, workload, target_records, target_a_processes


def _cleanup_support(processes):
    return _terminate_processes(processes)


def run_measurements(baseline_root, evidence_dir):
    baseline_root = Path(baseline_root).resolve(strict=True)
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    global _CLEANUP_PATH
    _CLEANUP_RESULTS.clear()
    _CLEANUP_PATH = evidence_dir / "readiness-cpu-fixture-cleanup.json"
    _CLEANUP_PATH.unlink(missing_ok=True)
    self_checks = sampler_self_checks()
    baseline_provenance = _source_provenance(baseline_root)
    candidate_provenance = _source_provenance(SOURCE_ROOT)
    harness_fingerprint = {
        name: hashlib.sha256((SOURCE_ROOT / "utilities" / name).read_bytes()).hexdigest()
        for name in ("shared_readiness_cpu.test.py", "shared_readiness_cpu_fixture.test.py")
        if (SOURCE_ROOT / "utilities" / name).is_file()
    }

    with tempfile.TemporaryDirectory(prefix="hcpu-") as temp_name:
        root = Path(temp_name)
        support, jobs, workload, target_records, target_workers = _prepare_jobs(root)
        baseline_data, baseline_source = _copy_managed_source(
            baseline_root, root / "baseline-data")
        candidate_data, candidate_source = _copy_managed_source(
            SOURCE_ROOT, root / "candidate-data")
        sides = {
            "baseline": {
                "candidate": False, "source": baseline_source, "data_root": baseline_data,
                "fingerprint": baseline_provenance,
            },
            "candidate": {
                "candidate": True, "source": candidate_source, "data_root": candidate_data,
                "fingerprint": candidate_provenance,
            },
        }
        matrix = {"carrier": {}, "peer_watch": {}}
        stall_matrix = {}
        density_matrix = {}
        try:
            for side_name, side in sides.items():
                for label, count in (("one_consumer", 1), ("fifteen_consumers", PENDING_ATTEMPTS)):
                    case_root = root / side_name / "carrier" / label
                    case_root.mkdir(parents=True)
                    case_result = _run_carrier_case(
                        side["source"], side["candidate"], jobs, workload,
                        side["data_root"], case_root, count)
                    matrix["carrier"].setdefault(side_name, {})[label] = case_result
                    # Restore exact initial workload bytes before the matched peer case.
                    _mutate_unrelated_row(jobs, workload, b"n")

                for label, selected_targets in (
                    ("one_same_target", ["a"]),
                    ("five_same_target_plus_separate_control", ["a", "a", "a", "a", "a", "b"]),
                ):
                    case_root = root / side_name / "peer-watch" / label
                    case_root.mkdir(parents=True)
                    watch_result = _run_watch_case(
                        side["source"], side["candidate"], jobs, side["data_root"],
                        case_root, target_records, selected_targets)
                    matrix["peer_watch"].setdefault(side_name, {})[label] = watch_result

            for side_name in sides:
                for kind in ("carrier", "peer_watch"):
                    for scenario in matrix[kind][side_name].values():
                        for window_name in ("cold_start", "warm_unchanged_12s"):
                            window = scenario[window_name]
                            if window["inclusive_lineage_cpu_delta_ticks"] is None:
                                raise AssertionError(f"missing inclusive CPU boundary: {side_name}/{kind}/{window_name}")
                        if scenario["unrelated_event_12s"]["elapsed_seconds"] < 11.5:
                            raise AssertionError("unrelated-event sample was shorter than the required 12-second window")
                        if (kind == "peer_watch" and side_name == "candidate"
                                and scenario["cold_start"]["classes"].get(
                                    "shared-runner", {}).get("peak_processes", 0) < 1):
                            raise AssertionError(
                                "candidate peer cold window did not sample its detached shared runner")
            for side_name, side in sides.items():
                stall_matrix[side_name] = _measure_runner_readiness_stalls(
                    side["source"], side["data_root"],
                    root / side_name / "readiness-stalls", sample_count=20)
                density_matrix[side_name] = _measure_scheduler_density(
                    side["source"], root / side_name / "scheduler-density")
            if workload["bytes"] < 28_000_000 or workload["bytes"] > 31_000_000:
                raise AssertionError(f"jobs fixture is not approximately 28 MB: {workload['bytes']}")
        finally:
            _record_fixture_cleanup("bound-work-support",
                                    {"processes": _cleanup_support(support)})

    evidence = {
        "schema_version": 1,
        "measurement_scope": "isolated production-entry fixture; no host production PID claim",
        "invocation": _invocation_record(evidence_dir),
        "source_provenance": {
            "baseline": baseline_provenance,
            "candidate": candidate_provenance,
            "authority": "source content fingerprint; Git revision is included only for an exact Git toplevel match",
            "measurement_harness_sha256": harness_fingerprint,
        },
        "workload": {key: value for key, value in workload.items()
                     if key not in {"unrelated_row_value_offset"}},
        "product_constraints": {
            "carrier_interval_seconds": 5,
            "same_workload_content_sha256_for_all_cases": workload["sha256"],
            "registry_placement": "each isolated case uses peer-state/jobs.log so the production runner resolves the same canonical registry path; source cases receive byte-identical copies",
            "registered_pending_attempts_on_shared_registry": PENDING_ATTEMPTS,
            "detached_runner_discovery": "runner argv plus exact fixture AGENT_PEER_LEDGER_ROOT, merged by PID/start identity with ordinary descendants",
            "test_only_warmup_flags": "none",
            "production_behavior_changed": False,
        },
        "sampler_self_checks": self_checks,
        "fixture_cleanup": {
            "status": "pass" if all(item.get("ok") for item in _CLEANUP_RESULTS) else "fail",
            "results": list(_CLEANUP_RESULTS),
        },
        "accounting_limits": [
            "PID namespace sees only this isolated fixture; production host PIDs remain inaccessible.",
            "RSS is simultaneous procfs resident pages, not traced heap; short-lived PID identity can be unknown when a child exits between samples.",
            "Per-class own CPU and parent reaped-child counters overlap after wait; inclusive lineage boundary deltas are the transfer-safe total.",
            "Process launch totals come from real helper result records; sampled peak concurrency can miss helpers shorter than the 50 ms sampler interval.",
        ],
        "matrix": matrix,
        "d1_slow_lane_readiness": {
            "scenario": "actual shared runner blocked in ensure_observers for 20 seconds per sample; readiness demand and attempt close occur during the block",
            "paired": stall_matrix,
            "acceptance": {"samples_per_side": 20, "nearest_rank_p95_seconds": 10.0},
        },
        "d2_scheduler_density": {
            "scenario": "paired real registry/store passes after unrelated registry and obligation appends; owner terminal proof is counted at its boundary, terminal duty discovery metrics are from ObligationStore",
            "paired": density_matrix,
            "acceptance": _density_acceptance(density_matrix),
        },
    }
    json_path = evidence_dir / "readiness-cpu-fixture.json"
    json_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    comparison_path = evidence_dir / "readiness-cpu-comparison.md"
    comparison_path.write_text(_comparison_markdown(evidence), encoding="utf-8")
    return evidence, json_path, comparison_path


def _comparison_markdown(evidence):
    rows = [
        "# Readiness CPU fixture comparison",
        "",
        "Isolated production-entry measurements. RSS is simultaneous resident memory; CPU is the transfer-safe inclusive production-entry lineage delta. Fixture-driver CPU is separate in the JSON. Class own and reaped-child counters overlap after process reap and are not summed.",
        "",
        "| Side | Workload | Case | Window | Peak processes (carrier/helper/watch/runner) | Production CPU (s) | Peak production RSS (MiB) | Shared runner peak |",
        "|---|---|---|---|---:|---:|---:|---:|",
    ]
    for workload_name, cases in evidence["matrix"].items():
        for side, scenarios in cases.items():
            for case_name, scenario in scenarios.items():
                for window_name, label in (("cold_start", "cold"),
                                           ("warm_unchanged_12s", "warm unchanged"),
                                           ("unrelated_event_12s", "unrelated event")):
                    window = scenario[window_name]
                    classes = window["classes"]
                    cpu = window.get("production_inclusive_cpu_delta_seconds")
                    rss = window["simultaneous_total_rss_bytes"]["production_classes_peak"] / (1024 * 1024)
                    runner = classes.get("shared-runner", {}).get("peak_processes", 0)
                    peak_shape = "/".join(str(classes.get(role, {}).get("peak_processes", 0))
                                           for role in ("carrier", "readiness-helper", "peer-watch", "shared-runner"))
                    cpu_text = f"{cpu:.3f}" if cpu is not None else "unknown"
                    rows.append(f"| {side} | {workload_name} | {case_name} | {label} | {peak_shape} | {cpu_text} | {rss:.1f} | {runner} |")
    rows.extend(("", "The JSON preserves start/end/peak process counts and RSS, PID/start identities, per-PID raw counters, missing readings, reaped-child deltas, exact helper launch totals, endpoint census, and post-window carrier return timing.", ""))
    stall = evidence.get("d1_slow_lane_readiness", {}).get("paired", {})
    rows.extend((
        "## Runner readiness during a blocked slow lane",
        "",
        "The existing runner process blocks inside its batch-observer call for 20 seconds. Each attempt closes after the block begins; the fixture records the first current ready projection and keeps the slow lane blocked until the full stall ends.",
        "",
        "| Side | Samples | Close to ready p95 (s) | Before stall release | Slow lane hold (s) | Acceptance |",
        "|---|---:|---:|---:|---:|",
    ))
    for side in ("baseline", "candidate"):
        record = stall.get(side, {}) if isinstance(stall, dict) else {}
        samples = record.get("samples", []) if isinstance(record, dict) else []
        hold = (statistics.mean(item.get("runner_slow_lane_seconds", 0) for item in samples)
                if samples else 0)
        before = sum(bool(item.get("ready_before_slow_lane_release")) for item in samples)
        rows.append(f"| {side} | {record.get('sample_count', 0)} | {record.get('nearest_rank_p95_seconds', 'unknown')} | {before}/{len(samples)} | {hold:.3f} | {record.get('pass', False)} |")
    density = evidence.get("d2_scheduler_density", {}).get("paired", {})
    rows.extend((
        "",
        "## Second-pass obligation density",
        "",
        "Each pass follows an unrelated registry append and one new active duty. The owner proof boundary and active-index terminal stat count are separate; raw repetitions and confidence intervals are in the JSON.",
        "",
        "| Side | Settled owners | Terminal duties | Mean pass (s) | Proof calls / pass | Terminal stat calls / pass | Active-index entries |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ))
    for side in ("baseline", "candidate"):
        cells = density.get(side, {}) if isinstance(density, dict) else {}
        for cell in ("owners-0-terminal-0", "owners-1000-terminal-0",
                     "owners-0-terminal-1000", "owners-1000-terminal-1000"):
            record = cells.get(cell, {})
            samples = record.get("repetitions", [])
            proof_calls = (sum(row.get("historical_proof_calls", 0) for row in samples)
                           / len(samples) if samples else 0)
            terminal_stats = (sum(row.get("terminal_stat_calls", 0) for row in samples)
                              / len(samples) if samples else 0)
            active_entries = (sum(row.get("active_index_entries", 0) for row in samples)
                              / len(samples) if samples else 0)
            rows.append(f"| {side} | {record.get('owner_count', '?')} | {record.get('terminal_count', '?')} | {record.get('mean_second_pass_seconds', 'unknown')} | {proof_calls:.1f} | {terminal_stats:.1f} | {active_entries:.1f} |")
    rows.append("")
    return "\n".join(rows)


def main():
    if len(sys.argv) == 8 and sys.argv[1] == "--carrier-child":
        _carrier_child(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4],
                       float(sys.argv[5]), Path(sys.argv[6]), Path(sys.argv[7]))
        return 0
    if len(sys.argv) == 1:
        self_checks = sampler_self_checks()
        print(json.dumps({"status": "sampler-self-checks-passed",
                          "self_checks": self_checks,
                          "paired_manual_measurement": "skipped; use shared_readiness_acceptance.test.py with baseline/evidence inputs"},
                         sort_keys=True))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    evidence, json_path, markdown_path = run_measurements(args.baseline_root, args.evidence_dir)
    print(json.dumps({"evidence_json": str(json_path), "comparison": str(markdown_path),
                      "workload_bytes": evidence["workload"]["bytes"],
                      "workload_sha256": evidence["workload"]["sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
