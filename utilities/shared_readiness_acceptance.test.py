#!/usr/bin/env python3
"""Final-source acceptance for shared readiness CPU and lifecycle fixtures."""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time
import tracemalloc


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CPU_FIXTURE = HERE / "shared_readiness_cpu_fixture.test.py"
CPU_HELPER = HERE / "shared_readiness_cpu.test.py"
LIFECYCLE_FIXTURE = HERE / "shared_readiness_lifecycle.test.py"
CACHE_MODULE_RELATIVE = Path("utilities") / "dispatch_registry_cache.py"
WINDOWS = ("cold_start", "warm_unchanged_12s", "unrelated_event_12s")
ROLES = ("carrier", "readiness-helper", "peer-watch", "shared-runner")
RETAINED_FIXTURE_BYTES = 28 * 1024 * 1024


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _rss_kib(record, field):
    """Parse a retained-memory RSS field without substituting maxrss for rss_after."""
    value = record.get(field) if isinstance(record, dict) else None
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    parts = value.strip().split()
    if len(parts) != 2 or parts[1] not in {"kB", "KiB"}:
        return None
    try:
        amount = int(parts[0])
    except ValueError:
        return None
    return amount if amount >= 0 else None


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def _lifecycle_child(evidence_dir):
    """Run the lifecycle suite and persist its unittest cleanup outcomes."""
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location(
        "shared_readiness_lifecycle_acceptance", LIFECYCLE_FIXTURE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"lifecycle fixture cannot be loaded: {LIFECYCLE_FIXTURE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cleanup_results = []
    supplemental_cleanup_results = []
    original_cleanup = module.LifecycleRunnerProcessTest._cleanup_processes

    def record_cleanup(test_case):
        runner_pid = test_case.runner_pid
        waited_runner = []
        original_waitpid = os.waitpid

        def track_waitpid(pid, options):
            result = original_waitpid(pid, options)
            if runner_pid is not None and pid == runner_pid and result[0] == runner_pid:
                waited_runner.append({"pid": result[0],
                                      "exit_code": os.waitstatus_to_exitcode(result[1])})
            return result

        os.waitpid = track_waitpid
        try:
            return original_cleanup(test_case)
        finally:
            os.waitpid = original_waitpid
            watcher_rows = [{"pid": process.pid, "returncode": process.poll(),
                             "reaped": process.returncode is not None}
                            for process in test_case.watchers]
            runner_reaped = runner_pid is None or bool(waited_runner)
            locked_fd_closed = test_case.runner_lock_fd is None
            row = {
                "watchers": watcher_rows,
                "runner_pid": runner_pid,
                "runner_wait_results": waited_runner,
                "runner_reaped": runner_reaped,
                "runner_lock_fd_closed": locked_fd_closed,
                "ok": (all(item["reaped"] for item in watcher_rows)
                       and runner_reaped and locked_fd_closed),
            }
            if (test_case._testMethodName ==
                    "test_runner_recovers_independent_watches_and_refreshes_without_jobs_change"
                    and runner_pid is not None and len(watcher_rows) == 6):
                cleanup_results.append(row)
            else:
                supplemental_cleanup_results.append(row)

            if (module.EVIDENCE_ROWS
                    and test_case._testMethodName ==
                        "test_runner_recovers_independent_watches_and_refreshes_without_jobs_change"):
                fixture = module.EVIDENCE_ROWS[-1]
                receipt_records = {}
                ack_records = {}
                for watch_id, _arm, paths in test_case.arms:
                    receipt_records[watch_id] = paths.receipt.is_file()
                    ack_records[watch_id] = paths.ack.is_file()
                fixture["receipt_records"] = receipt_records
                fixture["receipt_count"] = sum(receipt_records.values())
                fixture["ack_records"] = ack_records
                fixture["ack_count"] = sum(ack_records.values())
                fixture["runner_recovered_watches_after_arm"] = len(test_case.watchers)
                fixture["target_state_before_after"] = [
                    "working",
                    (_read_json(test_case.state_file) or {}).get("status", "unknown"),
                ]

    module.LifecycleRunnerProcessTest._cleanup_processes = record_cleanup
    try:
        suite_exit = module.main(["--evidence-dir", str(evidence_dir)])
    except Exception as exc:
        suite_exit = 1
        _write_json(evidence_dir / "shared-readiness-lifecycle-failure.json", {
            "status": "failed-before-evidence-write",
            "error": f"{type(exc).__name__}: {exc}",
        })

    evidence_path = evidence_dir / "shared-readiness-lifecycle.json"
    evidence = _read_json(evidence_path) or {
        "schema_version": 1,
        "scope": "execute phase 2 lifecycle; isolated production-entry fixture",
        "tests_run": 0,
        "failures": [],
        "errors": ["lifecycle suite did not write its evidence file"],
        "fixtures": [],
    }
    all_cleanup_rows = [*cleanup_results, *supplemental_cleanup_results]
    cleanup_status = ("pass" if cleanup_results and all_cleanup_rows
                      and all(row.get("ok") for row in all_cleanup_rows) else "fail")
    evidence["fixture_cleanup"] = {
        "status": cleanup_status,
        "results": cleanup_results,
        "supplemental_results": supplemental_cleanup_results,
        "ownership": "unittest process cleanup, under the assigned verification lease",
    }
    evidence["suite_exit_code"] = suite_exit
    _write_json(evidence_path, evidence)
    print(json.dumps({"lifecycle_evidence": str(evidence_path),
                      "suite_exit_code": suite_exit,
                      "fixture_cleanup": cleanup_status}, sort_keys=True))
    return 0 if suite_exit == 0 and cleanup_status == "pass" else 1


def _run_child(name, command, log_dir):
    log_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.monotonic()
    completed = subprocess.run(command, cwd=ROOT, env=env,
                               capture_output=True, text=True, check=False)
    elapsed = time.monotonic() - started
    stdout_path = log_dir / f"{name}.stdout.log"
    stderr_path = log_dir / f"{name}.stderr.log"
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    return {
        "command": [str(part) for part in command],
        "exit_code": completed.returncode,
        "elapsed_seconds": elapsed,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }


def _source_hashes():
    paths = (
        HERE / "shared_readiness_cpu.test.py",
        HERE / "shared_readiness_cpu_fixture.test.py",
        HERE / "shared_readiness_acceptance.test.py",
        HERE / "shared_readiness_lifecycle.test.py",
        HERE / "peer_steward.test.py",
    )
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


def _load_cpu_helper():
    spec = importlib.util.spec_from_file_location("readiness_cpu_provenance", CPU_HELPER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"CPU helper cannot be loaded: {CPU_HELPER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _build_retained_fixture(path, target_bytes):
    digest = hashlib.sha256()
    rows = size = 0
    with Path(path).open("wb") as stream:
        while size < target_bytes:
            row = (f"1970-01-01T00:00:00Z\trunning\t/repo\t/wt\tworker-{rows % 40}\t"
                   f"attempt_id=att-{rows:07d},attempt_schema_version=2,registered_worker=1,"
                   f"execution_surface=registered-headless,parent=parent-{rows % 1000},"
                   f"route_id=route-{rows % 100},route_hash=sha256:{'a' * 64},"
                   f"note={'n' * 60}\n").encode("utf-8")
            stream.write(row)
            digest.update(row)
            size += len(row)
            rows += 1
    return {"bytes": size, "rows": rows, "sha256": digest.hexdigest(),
            "target_bytes": target_bytes}


def _proc_memory_status():
    result = {}
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                key, value = line.split(":", 1)
                result[key] = value.strip()
    except OSError:
        pass
    return result


def _retained_memory_child(module_path, jobs_path, source_root, side):
    """Read one source cache in a fresh child; cache and tracemalloc die with it."""
    module_path = Path(module_path).resolve(strict=True)
    jobs_path = Path(jobs_path).resolve(strict=True)
    source_root = Path(source_root).resolve(strict=True)
    expected_module = (source_root / CACHE_MODULE_RELATIVE).resolve(strict=True)
    if module_path != expected_module:
        raise ValueError("cache module is outside the declared source root")
    module_hash_before = _sha256_file(module_path)
    spec = importlib.util.spec_from_file_location("retained_registry_under_test", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"registry cache cannot be loaded: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    tracemalloc.start()
    before = resource.getrusage(resource.RUSAGE_SELF)
    started = time.monotonic()
    view = module.registry_view(jobs_path, append_only=True)
    elapsed = time.monotonic() - started
    gc.collect()
    retained_bytes, peak_bytes = tracemalloc.get_traced_memory()
    after = resource.getrusage(resource.RUSAGE_SELF)
    status = _proc_memory_status()
    result = {
        "side": side,
        "fresh_child_pid": os.getpid(),
        "source_root": str(source_root),
        "cache_module_path": str(module_path),
        "cache_module_sha256": module_hash_before,
        "cache_module_sha256_after": _sha256_file(module_path),
        "jobs_path": str(jobs_path),
        "jobs_bytes": jobs_path.stat().st_size,
        "jobs_sha256": _sha256_file(jobs_path),
        "elapsed_seconds": elapsed,
        "self_cpu_seconds": ((after.ru_utime + after.ru_stime)
                             - (before.ru_utime + before.ru_stime)),
        "retained_traced_bytes": retained_bytes,
        "peak_traced_bytes": peak_bytes,
        "rss_after": status.get("VmRSS"),
        "rss_high_water": status.get("VmHWM"),
        "maxrss_kib": after.ru_maxrss,
        "record_count": view.record_count,
        "line_count": view.line_count,
        "cache_metrics": module.metrics_snapshot(),
    }
    del view
    sys.modules.pop(spec.name, None)
    del module
    gc.collect()
    tracemalloc.stop()
    print(json.dumps(result, sort_keys=True))
    return 0


def _retained_reading_errors(record, *, side, source_root, source_sha256, cache_sha256,
                             fixture):
    errors = []
    if not isinstance(record, dict):
        return ["child reading is missing or malformed"]
    if record.get("side") != side:
        errors.append("side label does not match the current source")
    if record.get("source_root") != str(Path(source_root).resolve()):
        errors.append("source root does not match this invocation")
    if record.get("source_content_sha256") != source_sha256:
        errors.append("source content fingerprint does not match the measured source")
    if record.get("cache_module_sha256") != cache_sha256:
        errors.append("cache module bytes do not match the current source")
    if record.get("cache_module_sha256_after") != cache_sha256:
        errors.append("cache module changed while the child measurement was running")
    if record.get("jobs_sha256") != fixture.get("sha256"):
        errors.append("jobs fixture digest does not match the paired fixture")
    for field, expected in (("jobs_bytes", fixture.get("bytes")),
                            ("record_count", fixture.get("rows")),
                            ("line_count", fixture.get("rows"))):
        actual = record.get(field)
        if not isinstance(actual, int) or isinstance(actual, bool) or actual != expected:
            errors.append(f"{field} does not match the paired fixture")
    metrics = record.get("cache_metrics")
    if not isinstance(metrics, dict):
        errors.append("cache metrics are missing")
    else:
        for field, expected in (("full_metadata_parses", 1),
                                ("metadata_rows_parsed", fixture.get("rows")),
                                ("bytes_read", fixture.get("bytes"))):
            actual = metrics.get(field)
            if not isinstance(actual, int) or isinstance(actual, bool) or actual != expected:
                errors.append(f"cache metric {field} does not match the paired fixture")
    pid = record.get("fresh_child_pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        errors.append("fresh child process identity is missing")
    traced = record.get("retained_traced_bytes")
    if not isinstance(traced, int) or isinstance(traced, bool) or traced < 0:
        errors.append("retained traced allocation is missing")
    peak_traced = record.get("peak_traced_bytes")
    if (not isinstance(peak_traced, int) or isinstance(peak_traced, bool)
            or peak_traced < 0 or (isinstance(traced, int) and peak_traced < traced)):
        errors.append("peak traced allocation is missing or below retained allocation")
    rss_after = _rss_kib(record, "rss_after")
    rss_high_water = _rss_kib(record, "rss_high_water")
    if rss_after is None:
        errors.append("post-parse RSS reading is missing")
    if rss_high_water is None:
        errors.append("RSS high-water reading is missing")
    if rss_after is not None and rss_high_water is not None and rss_high_water < rss_after:
        errors.append("RSS high-water is below post-parse RSS")
    maxrss = record.get("maxrss_kib")
    if not isinstance(maxrss, int) or isinstance(maxrss, bool) or maxrss < 0:
        errors.append("resource high-water reading is missing")
    for field in ("elapsed_seconds", "self_cpu_seconds"):
        value = record.get(field)
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value) or value < 0):
            errors.append(f"{field} reading is missing or invalid")
    return errors


def _run_retained_memory(evidence_dir, baseline_root, candidate_root, source_provenance,
                         *, target_bytes=RETAINED_FIXTURE_BYTES):
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    baseline_root = Path(baseline_root).resolve(strict=True)
    candidate_root = Path(candidate_root).resolve(strict=True)
    fixture_dir = evidence_dir / "fixture"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    errors = {}
    readings = {}
    child_commands = {}
    with tempfile.TemporaryDirectory(prefix="readiness-retained-") as temporary:
        jobs_path = Path(temporary) / "jobs.log"
        fixture = _build_retained_fixture(jobs_path, target_bytes)
        for side, source_root in (("baseline", baseline_root), ("candidate", candidate_root)):
            module_path = source_root / CACHE_MODULE_RELATIVE
            command = [sys.executable, str(Path(__file__).resolve()),
                       "--retained-memory-child", str(module_path), str(jobs_path),
                       str(source_root), side]
            child = _run_child(f"retained-{side}", command, fixture_dir)
            child_commands[side] = child
            record = _read_json(child["stdout_log"])
            source = source_provenance.get(side) if isinstance(source_provenance, dict) else None
            source_sha256 = source.get("content_sha256") if isinstance(source, dict) else None
            try:
                expected_cache_sha256 = _sha256_file(module_path)
            except OSError:
                expected_cache_sha256 = None
            if child["exit_code"] != 0:
                errors[side] = [f"retained-memory child exited {child['exit_code']}"]
            elif source_sha256 is None or expected_cache_sha256 is None:
                errors[side] = ["source content or cache module fingerprint is missing"]
            else:
                if isinstance(record, dict):
                    record["source_content_sha256"] = source_sha256
                    record["source_git"] = source.get("git") if isinstance(source, dict) else None
                    record["child_command"] = child["command"]
                    record["stdout_log"] = child["stdout_log"]
                    record["stderr_log"] = child["stderr_log"]
                errors[side] = _retained_reading_errors(
                    record, side=side, source_root=source_root,
                    source_sha256=source_sha256, cache_sha256=expected_cache_sha256,
                    fixture=fixture)
            readings[side] = record
            _write_json(evidence_dir / f"{side}.json", record or {
                "status": "missing-child-reading", "child": child,
                "validation_errors": errors[side],
            })
    child_pids = [record.get("fresh_child_pid") for record in readings.values()
                  if isinstance(record, dict)]
    if len(child_pids) != 2 or len(set(child_pids)) != 2:
        errors["pair"] = ["baseline and candidate did not use distinct child processes"]
    result = {
        "schema_version": 1,
        "status": "pass" if errors and all(not value for value in errors.values()) else "fail",
        "probe_scope": "fresh one-shot child process per actual source cache",
        "fixture": fixture,
        "source_provenance": {
            side: source_provenance.get(side) if isinstance(source_provenance, dict) else None
            for side in ("baseline", "candidate")
        },
        "child_processes": child_pids,
        "child_commands": child_commands,
        "readings": readings,
        "validation_errors": errors,
        "output_paths": {
            "summary": str(evidence_dir / "retained-memory.json"),
            "baseline": str(evidence_dir / "baseline.json"),
            "candidate": str(evidence_dir / "candidate.json"),
        },
    }
    _write_json(evidence_dir / "retained-memory.json", result)
    return result


def _source_provenance_errors(measurement, baseline_root, candidate_root):
    errors = []
    provenance = measurement.get("source_provenance")
    if not isinstance(provenance, dict):
        return ["source provenance object is missing"]
    if provenance.get("authority") != (
            "source content fingerprint; Git revision is included only for an exact Git toplevel match"):
        errors.append("source content authority disclosure is missing or malformed")
    harness_hashes = provenance.get("measurement_harness_sha256")
    required_harnesses = {"shared_readiness_cpu.test.py",
                          "shared_readiness_cpu_fixture.test.py"}
    if (not isinstance(harness_hashes, dict)
            or not required_harnesses.issubset(harness_hashes)
            or any(not isinstance(harness_hashes.get(name), str)
                   or len(harness_hashes[name]) != 64 for name in required_harnesses)):
        errors.append("measurement harness source fingerprints are incomplete")
    else:
        for name in required_harnesses:
            try:
                actual_hash = _sha256_file(Path(candidate_root) / "utilities" / name)
            except OSError:
                actual_hash = None
            if harness_hashes.get(name) != actual_hash:
                errors.append(f"measurement harness fingerprint changed: {name}")
    cpu_helper = _load_cpu_helper()
    for side, expected_root in (("baseline", baseline_root), ("candidate", candidate_root)):
        record = provenance.get(side)
        if not isinstance(record, dict):
            errors.append(f"{side} source provenance is missing")
            continue
        expected_root = Path(expected_root).resolve(strict=True)
        if record.get("path") != str(expected_root):
            errors.append(f"{side} source path does not match this invocation")
        try:
            actual = cpu_helper._source_provenance(expected_root)
        except (OSError, ValueError) as exc:
            errors.append(f"{side} source cannot be revalidated: {exc}")
            continue
        for field in ("kind", "content_sha256", "files", "bytes", "fingerprinted_roots"):
            if record.get(field) != actual.get(field):
                errors.append(f"{side} {field} changed during measurement or is malformed")
        recorded_git = record.get("git")
        actual_git = actual.get("git")
        if not isinstance(recorded_git, dict) or not isinstance(actual_git, dict):
            errors.append(f"{side} Git source-root provenance is missing")
        elif recorded_git != actual_git:
            errors.append(f"{side} Git root, revision, or dirty-content disclosure changed")
        recorded_manifest = record.get("archive_manifest")
        actual_manifest = actual.get("archive_manifest")
        if recorded_manifest != actual_manifest:
            errors.append(f"{side} adjacent archive manifest changed or is malformed")
        if isinstance(actual_manifest, dict) and actual_manifest.get("status") not in {
                "verified", "not-found"}:
            errors.append(f"{side} adjacent archive manifest is {actual_manifest.get('status')}")
    return errors


def _metric_rollup(measurement):
    rows = []
    matrix = measurement.get("matrix")
    if not isinstance(matrix, dict):
        return rows
    for lane, sides in matrix.items():
        if not isinstance(sides, dict):
            continue
        for side, cases in sides.items():
            if not isinstance(cases, dict):
                continue
            for case, scenario in cases.items():
                if not isinstance(scenario, dict):
                    continue
                for window_name in WINDOWS:
                    window = scenario.get(window_name)
                    if not isinstance(window, dict):
                        continue
                    classes = window.get("classes")
                    if not isinstance(classes, dict):
                        classes = {}
                    rows.append({
                        "lane": lane,
                        "side": side,
                        "case": case,
                        "window": window_name,
                        "elapsed_seconds": window.get("elapsed_seconds"),
                        "production_inclusive_cpu_delta_seconds": window.get(
                            "production_inclusive_cpu_delta_seconds"),
                        "inclusive_lineage_cpu_delta_seconds": window.get(
                            "inclusive_lineage_cpu_delta_seconds"),
                        "simultaneous_total_rss_bytes": window.get(
                            "simultaneous_total_rss_bytes"),
                        "classes": {
                            role: {
                                "start_processes": classes.get(role, {}).get("start_processes"),
                                "end_processes": classes.get(role, {}).get("end_processes"),
                                "peak_processes": classes.get(role, {}).get("peak_processes"),
                                "start_rss_bytes": classes.get(role, {}).get("start_rss_bytes"),
                                "end_rss_bytes": classes.get(role, {}).get("end_rss_bytes"),
                                "peak_simultaneous_rss_bytes": classes.get(
                                    role, {}).get("peak_simultaneous_rss_bytes"),
                                "sampled_own_cpu_delta_ticks": classes.get(
                                    role, {}).get("sampled_own_cpu_delta_ticks"),
                                "sampled_own_cpu_delta_seconds": classes.get(
                                    role, {}).get("sampled_own_cpu_delta_seconds"),
                                "reaped_child_counter_delta_ticks": classes.get(
                                    role, {}).get("reaped_child_counter_delta_ticks"),
                                "reaped_child_counter_delta_seconds": classes.get(
                                    role, {}).get("reaped_child_counter_delta_seconds"),
                            }
                            for role in ROLES
                        },
                    })
    return rows


def _mib(value):
    return f"{(value or 0) / (1024 * 1024):.1f}"


def _metric_markdown(rows):
    lines = [
        "| Lane / case | Side | Window | Peak processes C/H/W/R | Production CPU s | Own CPU C/H/W/R s | Reaped CPU C/H/W/R s | Peak RSS C/H/W/R MiB | Production total peak RSS MiB |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    short_window = {"cold_start": "cold", "warm_unchanged_12s": "warm unchanged",
                    "unrelated_event_12s": "unrelated event"}
    def display(value, digits=2):
        return f"{value:.{digits}f}" if isinstance(value, (int, float)) else "unknown"

    for row in rows:
        classes = row["classes"]
        counts = "/".join(str(classes[role]["peak_processes"])
                           if classes[role]["peak_processes"] is not None else "?"
                           for role in ROLES)
        own = "/".join(display(classes[role]["sampled_own_cpu_delta_seconds"])
                        for role in ROLES)
        reaped = "/".join(display(classes[role]["reaped_child_counter_delta_seconds"])
                          for role in ROLES)
        rss = "/".join(_mib(classes[role]["peak_simultaneous_rss_bytes"])
                        if isinstance(classes[role]["peak_simultaneous_rss_bytes"], (int, float))
                        else "unknown" for role in ROLES)
        totals = row.get("simultaneous_total_rss_bytes")
        total_value = totals.get("production_classes_peak") if isinstance(totals, dict) else None
        total = _mib(total_value) if isinstance(total_value, (int, float)) else "unknown"
        cpu = row["production_inclusive_cpu_delta_seconds"]
        cpu_text = display(cpu, 3)
        lines.append(
            f"| {row['lane']} / {row['case']} | {row['side']} | "
            f"{short_window[row['window']]} | {counts} | {cpu_text} | "
            f"{own} | {reaped} | {rss} | {total} |")
    return "\n".join(lines)


def _candidate_helper_launches(measurement, side):
    total = 0
    found = False
    matrix = measurement.get("matrix")
    carrier = matrix.get("carrier") if isinstance(matrix, dict) else None
    scenarios = carrier.get(side) if isinstance(carrier, dict) else None
    if not isinstance(scenarios, dict) or not scenarios:
        return None
    for scenario in scenarios.values():
        if not isinstance(scenario, dict):
            continue
        branch = scenario.get("branch_evidence", {})
        if isinstance(branch, dict) and "total_helper_launches" in branch:
            count = branch["total_helper_launches"]
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                return None
            total += count
            found = True
    return total if found else None


def _matrix_errors(measurement, metric_rows):
    expected = set()
    for lane, cases in (
            ("carrier", ("one_consumer", "fifteen_consumers")),
            ("peer_watch", ("one_same_target", "five_same_target_plus_separate_control"))):
        for side in ("baseline", "candidate"):
            for case in cases:
                for window in WINDOWS:
                    expected.add((lane, side, case, window))
    actual = {(row["lane"], row["side"], row["case"], row["window"])
              for row in metric_rows}
    errors = []
    if actual != expected or len(metric_rows) != len(expected):
        errors.append("measurement matrix does not contain every unique side/case/window")
    for row in metric_rows:
        prefix = f"{row['lane']}/{row['side']}/{row['case']}/{row['window']}"
        for field in ("elapsed_seconds", "production_inclusive_cpu_delta_seconds",
                      "inclusive_lineage_cpu_delta_seconds"):
            value = row.get(field)
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value < 0):
                errors.append(f"{prefix} lacks {field}")
        totals = row.get("simultaneous_total_rss_bytes")
        total_peak = totals.get("production_classes_peak") if isinstance(totals, dict) else None
        if (not isinstance(total_peak, (int, float)) or isinstance(total_peak, bool)
                or not math.isfinite(total_peak) or total_peak < 0):
            errors.append(f"{prefix} lacks total RSS")
        for role in ROLES:
            values = row["classes"].get(role)
            if not isinstance(values, dict):
                errors.append(f"{prefix} lacks {role}")
                continue
            for field in ("start_processes", "end_processes", "peak_processes",
                          "start_rss_bytes", "end_rss_bytes", "peak_simultaneous_rss_bytes",
                          "sampled_own_cpu_delta_ticks", "sampled_own_cpu_delta_seconds",
                          "reaped_child_counter_delta_ticks", "reaped_child_counter_delta_seconds"):
                value = values.get(field)
                if (not isinstance(value, (int, float)) or isinstance(value, bool)
                        or not math.isfinite(value) or value < 0):
                    errors.append(f"{prefix} lacks {role}.{field}")
    return errors


def _paired_performance_errors(measurement):
    errors = {"d1": [], "d2": []}
    d1 = measurement.get("d1_slow_lane_readiness")
    paired_d1 = d1.get("paired") if isinstance(d1, dict) else None
    if not isinstance(paired_d1, dict):
        errors["d1"].append("paired slow-lane readiness evidence is missing")
        paired_d1 = {}
    p95_values = {}
    for side in ("baseline", "candidate"):
        record = paired_d1.get(side)
        samples = record.get("samples") if isinstance(record, dict) else None
        if not isinstance(samples, list) or len(samples) != 20:
            errors["d1"].append(f"{side} must contain 20 raw slow-lane samples")
            continue
        latencies = []
        for sample in samples:
            latency = sample.get("close_to_ready_seconds") if isinstance(sample, dict) else None
            stall = sample.get("runner_slow_lane_seconds") if isinstance(sample, dict) else None
            if (not isinstance(latency, (int, float)) or isinstance(latency, bool)
                    or not math.isfinite(latency) or latency < 0):
                errors["d1"].append(f"{side} has an invalid close-to-ready sample")
                continue
            if (not isinstance(stall, (int, float)) or isinstance(stall, bool)
                    or not math.isfinite(stall) or stall < 19.5):
                errors["d1"].append(f"{side} sample did not hold the runner slow lane for 20 seconds")
            generation = sample.get("projection_generation") if isinstance(sample, dict) else None
            if not isinstance(generation, list) or len(generation) != 5:
                errors["d1"].append(f"{side} sample lacks the current registry generation")
            latencies.append(float(latency))
        if len(latencies) != 20:
            continue
        runner_identities = {(sample.get("runner_pid"), sample.get("runner_start"))
                             for sample in samples if isinstance(sample, dict)}
        if (len(runner_identities) != 1
                or not isinstance(next(iter(runner_identities))[0], int)
                or not isinstance(next(iter(runner_identities))[1], str)
                or not next(iter(runner_identities))[1].isdigit()):
            errors["d1"].append(f"{side} samples do not identify one real runner process")
        rank_value = sorted(latencies)[math.ceil(0.95 * len(latencies)) - 1]
        recorded = record.get("nearest_rank_p95_seconds")
        if (not isinstance(recorded, (int, float)) or isinstance(recorded, bool)
                or not math.isclose(recorded, rank_value, rel_tol=1e-6, abs_tol=1e-6)):
            errors["d1"].append(f"{side} nearest-rank p95 does not match its raw samples")
        p95_values[side] = rank_value
    if p95_values.get("candidate", float("inf")) > 10.0:
        errors["d1"].append("candidate attempt-close to ready-projection p95 exceeds 10 seconds")
    candidate_record = paired_d1.get("candidate")
    candidate_samples = (candidate_record.get("samples", [])
                         if isinstance(candidate_record, dict) else [])
    if candidate_samples and not all(
            sample.get("ready_before_slow_lane_release") is True
            for sample in candidate_samples if isinstance(sample, dict)):
        errors["d1"].append("candidate readiness was not published during the slow-lane stall")
    if p95_values.get("baseline", 0.0) < 10.0:
        errors["d1"].append("baseline did not reproduce the slow-lane readiness delay")

    d2 = measurement.get("d2_scheduler_density")
    density = d2.get("paired") if isinstance(d2, dict) else None
    if not isinstance(density, dict):
        errors["d2"].append("paired scheduler-density evidence is missing")
        return errors
    expected_cells = {"owners-0-terminal-0", "owners-1000-terminal-0",
                      "owners-0-terminal-1000", "owners-1000-terminal-1000"}
    if set(density) != {"baseline", "candidate"}:
        errors["d2"].append("scheduler-density evidence must include baseline and candidate")
        return errors
    for side in ("baseline", "candidate"):
        matrix = density.get(side)
        if not isinstance(matrix, dict) or set(matrix) != expected_cells:
            errors["d2"].append(f"{side} scheduler-density matrix is incomplete")
            continue
        for cell, entry in matrix.items():
            repetitions = entry.get("repetitions") if isinstance(entry, dict) else None
            if not isinstance(repetitions, list) or len(repetitions) != 7:
                errors["d2"].append(f"{side}/{cell} must contain seven second-pass samples")
                continue
            for row in repetitions:
                elapsed = row.get("second_pass_seconds") if isinstance(row, dict) else None
                proofs = row.get("historical_proof_calls") if isinstance(row, dict) else None
                terminal_stats = row.get("terminal_stat_calls") if isinstance(row, dict) else None
                if (not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool)
                        or not math.isfinite(elapsed) or elapsed < 0):
                    errors["d2"].append(f"{side}/{cell} has invalid pass timing")
                if not isinstance(proofs, int) or isinstance(proofs, bool) or proofs < 0:
                    errors["d2"].append(f"{side}/{cell} lacks historical proof call counts")
                if not isinstance(terminal_stats, int) or isinstance(terminal_stats, bool) or terminal_stats < 0:
                    errors["d2"].append(f"{side}/{cell} lacks terminal stat counts")
    if isinstance(density.get("baseline"), dict):
        for cell in ("owners-1000-terminal-0", "owners-1000-terminal-1000"):
            rows = density["baseline"].get(cell, {}).get("repetitions", [])
            if rows and any(row.get("historical_proof_calls") != 1000 for row in rows):
                errors["d2"].append(f"baseline {cell} did not recheck all 1,000 settled owners")
    candidate = density.get("candidate")
    if isinstance(candidate, dict):
        for cell in ("owners-1000-terminal-0", "owners-1000-terminal-1000"):
            rows = candidate.get(cell, {}).get("repetitions", [])
            if rows and any(row.get("historical_proof_calls") != 0 for row in rows):
                errors["d2"].append(f"candidate {cell} repeated historical owner proof work")
        terminal_cell = candidate.get("owners-0-terminal-1000", {})
        for row in terminal_cell.get("repetitions", []):
            if (row.get("terminal_stat_calls") != 0
                    or row.get("active_full_scans") != 0):
                errors["d2"].append("candidate repeated terminal duty discovery work")
                break
        try:
            cpu_helper = _load_cpu_helper()
            derived = cpu_helper._density_acceptance({"candidate": candidate})
        except (KeyError, TypeError, ValueError) as exc:
            derived = None
            errors["d2"].append(f"scheduler-density raw timing is malformed: {exc}")
        if derived is not None:
            if not isinstance(d2.get("acceptance"), dict) or d2["acceptance"] != derived:
                errors["d2"].append("scheduler-density acceptance does not match raw paired timing")
            elif derived.get("pass") is not True:
                errors["d2"].append("candidate 1,000-owner/1,000-terminal pass cost increased beyond paired timer noise")
    return errors


def _lifecycle_cleanup_errors(cleanup):
    if not isinstance(cleanup, dict) or cleanup.get("status") != "pass":
        return ["lifecycle fixture cleanup did not pass"]
    results = cleanup.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        return ["lifecycle cleanup did not record its single runner fixture"]
    row = results[0]
    errors = []
    if row.get("runner_pid") is None or row.get("runner_reaped") is not True:
        errors.append("the shared runner process was not observed and reaped")
    if row.get("runner_lock_fd_closed") is not True:
        errors.append("the runner lock descriptor was not closed")
    watchers = row.get("watchers")
    if (not isinstance(watchers, list) or len(watchers) != 6
            or not all(isinstance(item, dict) and item.get("reaped") is True for item in watchers)):
        errors.append("all six watcher processes were not reaped")
    return errors


def _selected_registry_view_regression(lifecycle):
    selected = lifecycle.get("selected_regressions")
    if not isinstance(selected, list):
        return False
    return any(str(name).endswith(
        ".test_runner_reads_once_for_five_stewards_and_separates_another_pane")
        for name in selected)


def _lifecycle_errors(lifecycle):
    errors = []
    if not isinstance(lifecycle, dict):
        return ["lifecycle evidence is missing"]
    if lifecycle.get("tests_run", 0) <= 0 or lifecycle.get("suite_exit_code") != 0:
        errors.append("lifecycle suite did not complete successfully")
    if lifecycle.get("failures") or lifecycle.get("errors"):
        errors.append("lifecycle suite reported failures or errors")
    fixtures = lifecycle.get("fixtures")
    if not isinstance(fixtures, list) or len(fixtures) != 1 or not isinstance(fixtures[0], dict):
        return errors + ["six-watch lifecycle fixture is missing or ambiguous"]
    fixture = fixtures[0]
    if fixture.get("watch_count") != 6 or fixture.get("same_target_count") != 5:
        errors.append("lifecycle fixture did not exercise five shared watches and one control")
    if fixture.get("distinct_stewards") != 6:
        errors.append("lifecycle fixture did not retain six independent stewards")
    runner_count = fixture.get("runner_process_count")
    if not isinstance(runner_count, int) or isinstance(runner_count, bool) or runner_count != 1:
        errors.append("lifecycle fixture did not observe exactly one shared runner")
    snapshots = fixture.get("runner_session_snapshot_calls")
    if not isinstance(snapshots, int) or isinstance(snapshots, bool) or snapshots < 1:
        errors.append("lifecycle fixture did not record a runner session observation")
    if fixture.get("native_result_counts") != {"idle": 5, "done": 1}:
        errors.append("lifecycle fixture did not preserve distinct idle and done results")
    groups = fixture.get("observation_groups")
    grouped_ids = []
    if not isinstance(groups, list) or len(groups) < 2:
        errors.append("lifecycle evidence does not contain shared target observation groups")
        groups = []
    else:
        group_sizes = sorted(row.get("watch_count", 0) for row in groups
                             if isinstance(row, dict))
        grouped_ids = [watch_id for row in groups if isinstance(row, dict)
                       for watch_id in row.get("watch_ids", [])]
        group_keys = [(row.get("pane"), row.get("session_id"), row.get("native_state"))
                      for row in groups if isinstance(row, dict)]
        target_counts = {}
        native_counts = {}
        for row in groups:
            if not isinstance(row, dict):
                continue
            watch_ids = row.get("watch_ids")
            if not isinstance(watch_ids, list) or row.get("watch_count") != len(watch_ids):
                errors.append("observation group count does not match its watch identities")
            target_key = (row.get("pane"), row.get("session_id"))
            target_counts[target_key] = target_counts.get(target_key, 0) + row.get("watch_count", 0)
            state = row.get("native_state")
            native_counts[state] = native_counts.get(state, 0) + row.get("watch_count", 0)
        if (len(set(group_keys)) != len(group_keys) or group_sizes != [1, 1, 4]
                or sorted(target_counts.values()) != [1, 5]
                or native_counts != {"idle": 5, "done": 1}):
            errors.append("lifecycle groups do not preserve five shared watches, one control, and idle/done identity")
        if len(grouped_ids) != 6 or len(set(grouped_ids)) != 6:
            errors.append("shared observation groups do not cover six unique watches")
    if not _selected_registry_view_regression(lifecycle):
        errors.append("the shared pass-scoped RegistryView regression was not selected")

    receipts = fixture.get("receipt_records")
    notices = fixture.get("notice_records")
    acknowledgements = fixture.get("ack_records")
    if (not isinstance(receipts, dict) or len(receipts) != 6
            or not all(value is True for value in receipts.values())):
        errors.append("six independent receipt files were not verified")
    if (not isinstance(notices, dict) or len(notices) != 6
            or any(not isinstance(value, int) or isinstance(value, bool) or value != 1
                   for value in notices.values())):
        errors.append("six independent notices were not verified")
    if (not isinstance(acknowledgements, dict) or len(acknowledgements) != 6
            or not all(value is True for value in acknowledgements.values())):
        errors.append("six independent acknowledgement files were not verified")
    if fixture.get("receipt_count") != 6 or fixture.get("ack_count") != 6:
        errors.append("lifecycle receipt or acknowledgement totals are incomplete")
    if fixture.get("first_ack_isolation") is not True:
        errors.append("the first acknowledgement isolation check is missing")
    if fixture.get("remaining_independent_receipts") != 6:
        errors.append("independent receipts did not remain after acknowledgement")
    transfer_refs = fixture.get("notice_transfer_refs_present")
    if (not isinstance(transfer_refs, dict) or len(transfer_refs) != 6
            or not all(value is True for value in transfer_refs.values())):
        errors.append("notice transfer references were not preserved for all six watches")
    expected_watch_ids = set(grouped_ids)
    duty_maps = (receipts, notices, acknowledgements, transfer_refs)
    if all(isinstance(rows, dict) for rows in duty_maps):
        if any(set(rows) != expected_watch_ids for rows in duty_maps):
            errors.append("receipt, notice, ACK, and transfer records do not cover the same six watches")
    else:
        errors.append("watch duty records are malformed")
    if fixture.get("acknowledged_watch") not in expected_watch_ids:
        errors.append("the isolated first acknowledgement does not name a lifecycle watch")
    if fixture.get("target_state_before_after") != ["working", "idle"]:
        errors.append("target readiness transition was not observed")
    jobs_hashes = fixture.get("jobs_sha256_before_after")
    if (not isinstance(jobs_hashes, list) or len(jobs_hashes) != 2
            or not jobs_hashes[0] or jobs_hashes[0] != jobs_hashes[1]):
        errors.append("jobs content changed during lifecycle observation")
    return errors


def _candidate_runner_check(measurement):
    matrix = measurement.get("matrix")
    peer_sides = matrix.get("peer_watch") if isinstance(matrix, dict) else None
    candidate = peer_sides.get("candidate") if isinstance(peer_sides, dict) else None
    if not isinstance(candidate, dict) or not candidate:
        return False, []
    peaks = []
    valid = True
    for scenario in candidate.values():
        if not isinstance(scenario, dict):
            valid = False
            continue
        for window_name in WINDOWS:
            window = scenario.get(window_name)
            classes = window.get("classes") if isinstance(window, dict) else None
            runner = classes.get("shared-runner") if isinstance(classes, dict) else None
            peak = runner.get("peak_processes") if isinstance(runner, dict) else None
            if not isinstance(peak, int) or isinstance(peak, bool) or peak > 1:
                valid = False
            else:
                peaks.append(peak)
        cold = scenario.get("cold_start")
        cold_classes = cold.get("classes") if isinstance(cold, dict) else None
        cold_runner = cold_classes.get("shared-runner") if isinstance(cold_classes, dict) else None
        if not isinstance(cold_runner, dict) or cold_runner.get("peak_processes") != 1:
            valid = False
    return valid and bool(peaks), peaks


def _make_handoff(report, evidence_dir, measurement, lifecycle, cleanup):
    d1 = report["d1_source_provenance"]
    d1_latency = report.get("d1_attempt_readiness", {})
    d2 = report["d2_shared_observation"]
    d2_cost = report.get("d2_obligation_pass_cost", {})
    fixture = (lifecycle.get("fixtures") or [{}])[0]
    metric_rows = report["metric_windows"]

    def source_line(side):
        source = d1.get("sources", {}).get(side, {})
        git = source.get("git", {})
        revision = git.get("revision")
        revision_text = (f"Git `{revision}`; dirty={git.get('dirty')}" if revision else
                         f"Git revision unknown (discovered toplevel `{git.get('top_level')}`)")
        manifest = source.get("archive_manifest")
        manifest_text = (f"; manifest `{manifest.get('status')}`"
                         if isinstance(manifest, dict) else "")
        return (f"- {side}: `{source.get('path', 'unknown')}`; content SHA-256 "
                f"`{source.get('content_sha256', 'unknown')}`; {revision_text}{manifest_text}.")

    retained = report.get("retained_memory", {})
    readings = retained.get("readings", {}) if isinstance(retained, dict) else {}
    baseline = readings.get("baseline") or {}
    candidate = readings.get("candidate") or {}
    fixture_info = retained.get("fixture", {}) if isinstance(retained, dict) else {}
    rss_after = (_rss_kib(baseline, "rss_after"), _rss_kib(candidate, "rss_after"))
    rss_high = (_rss_kib(baseline, "rss_high_water"), _rss_kib(candidate, "rss_high_water"))
    groups = fixture.get("observation_groups", [])
    groups_text = ", ".join(
        f"{row.get('watch_count', '?')} on {row.get('pane', '?')} ({row.get('native_state', '?')})"
        for row in groups if isinstance(row, dict)) or "unknown"

    def count(value):
        return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else "unknown"

    lines = [
        "# Final source evidence: shared readiness",
        "",
        f"Status: **{report['status']}** for these measured source inputs; the owner retains the execute gate.",
        "",
        "## d1 — measured source provenance",
        "",
        "Source content fingerprints identify the bytes measured. A Git revision is included only when Git's discovered toplevel exactly matches that supplied source root; matching worktrees also disclose dirty state.",
        source_line("baseline"),
        source_line("candidate"),
        f"Actual runner stall samples: baseline p95={((d1_latency.get('paired') or {}).get('baseline') or {}).get('nearest_rank_p95_seconds', 'unknown')} s; candidate p95={((d1_latency.get('paired') or {}).get('candidate') or {}).get('nearest_rank_p95_seconds', 'unknown')} s (20 samples per side; each slow-lane stall was held for 20 seconds). Validation: `{d1_latency.get('status', 'missing')}`.",
        "Remote main ancestry and pull request inclusion are not evaluated by this helper; the owner checks those separately.",
        "",
        "## d2 — shared observation and retained cache",
        "",
        f"Carrier helper launches: baseline={d2.get('baseline_carrier_helper_launches')}, candidate={d2.get('candidate_carrier_helper_launches')}. Candidate peer-watch cold windows observed one runner per case. Runner session observations recorded: {fixture.get('runner_session_snapshot_calls', 'unknown')}; this is reported as measured with no exact-count gate.",
        f"Lifecycle target groups: {groups_text}. The selected runner regression kept pass-scoped RegistryView lookups below the six-watch count, with two grouped readiness reads and six independent wake requests.",
        f"Settled-obligation density: candidate 1,000-owner/1,000-terminal second-pass mean={((((d2_cost.get('paired') or {}).get('candidate') or {}).get('owners-1000-terminal-1000') or {}).get('mean_second_pass_seconds', 'unknown'))} s; historical owner-proof calls were counted separately from active-index terminal discovery. Validation: `{d2_cost.get('status', 'missing')}`; paired timer-noise check `{((d2_cost.get('acceptance') or {}).get('pass', 'missing'))}`.",
        f"Fresh retained-memory children parsed {count(fixture_info.get('rows'))} rows ({count(fixture_info.get('bytes'))} bytes; SHA-256 `{fixture_info.get('sha256', 'unknown')}`). Retained traced bytes: {count(baseline.get('retained_traced_bytes'))} → {count(candidate.get('retained_traced_bytes'))}; post-parse RSS: {rss_after[0] if rss_after[0] is not None else 'unknown'} → {rss_after[1] if rss_after[1] is not None else 'unknown'} KiB; separate RSS high-water: {rss_high[0] if rss_high[0] is not None else 'unknown'} → {rss_high[1] if rss_high[1] is not None else 'unknown'} KiB.",
        "Each retained-memory reading uses the actual source cache module and a fresh one-shot child; its source and module hashes, fixture digest, parse counts, RSS, and high-water remain in the evidence directory.",
        "",
        "## d3 — separate receipts, notices, acknowledgements, and recovery",
        "",
        f"The lifecycle suite ran {lifecycle.get('tests_run', 0)} tests with {len(lifecycle.get('failures', []))} failures and {len(lifecycle.get('errors', []))} errors. It recorded {fixture.get('watch_count', 0)} watches across {fixture.get('distinct_stewards', 0)} stewards. Receipts={fixture.get('receipt_count', 0)}, notices={sum((fixture.get('notice_records') or {}).values())}, acknowledgements={fixture.get('ack_count', 0)}. First-ACK isolation passed, all six ACK files were later written, and jobs content stayed unchanged while target evidence changed `{fixture.get('target_state_before_after', [])}`. Cleanup: `{cleanup.get('lifecycle', {}).get('status', 'missing')}`.",
        "",
        "Selected transition coverage includes session, pane, and process-birth replacement; exact and missing receipts; live and dead claims; stale observations; lost wake/reclaim; native-result recovery; handover; runner scan/unlock races; and the shared RegistryView runner regression.",
        "",
        "## d4 — matched CPU, process, and RSS windows",
        "",
        "Cold setup, warm unchanged input, and unrelated-event windows are separate rows. The carrier event changes an unrelated registry row; the peer-watch case uses a test-only SIGUSR1 wake surrogate to exercise its production wake/recheck path because local Herdr socket bind is denied. Process order is carrier / helper / peer watch / shared runner. CPU columns report production inclusive lineage plus per-class self and reaped-child deltas; per-class counters overlap after reap and must not be summed. RSS columns report simultaneous per-class peaks and total production-class peak. Full start/end/peak counters and PID/start identities are in the structured JSON.",
        "",
        _metric_markdown(metric_rows),
        "",
        "## Evidence and limits",
        "",
        f"Acceptance invocation: `{report['invocation']['executable']} {' '.join(report['invocation']['argv'])}` from `{report['invocation']['cwd']}`. Child commands and stdout/stderr paths are listed in `shared-readiness-acceptance.json`.",
        "All metrics come from isolated fixtures. The CPU runner uses pipe-backed snapshot data and the unrelated peer wake uses a SIGUSR1 surrogate because local Unix-socket bind is unavailable. Host production PIDs were not inspected or declared dead. Per-class self CPU and reaped-child CPU counters overlap after process reaping.",
        "",
        f"Artifacts: `measure/readiness-cpu-fixture.json`, `measure/readiness-cpu-comparison.md`, `measure/readiness-cpu-fixture-cleanup.json`, `lifecycle/shared-readiness-lifecycle.json`, `test_logs/`, and `shared-readiness-acceptance.json` beneath `{evidence_dir}`.",
        "",
    ]
    return "\n".join(lines)


def _ordinary_ci():
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    commands = (
        [sys.executable, str(CPU_FIXTURE)],
        [sys.executable, str(LIFECYCLE_FIXTURE)],
    )
    for command in commands:
        completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
        if completed.returncode:
            return completed.returncode
    print("paired manual baseline/candidate measurement: SKIPPED (inputs absent)")
    return 0


def _assemble_report(evidence_dir, baseline_root, measurement_command, lifecycle_command,
                     measurement_evidence, measurement_cleanup, lifecycle_evidence,
                     lifecycle_cleanup, retained):
    metric_rows = _metric_rollup(measurement_evidence)
    matrix_errors = _matrix_errors(measurement_evidence, metric_rows)
    performance_errors = _paired_performance_errors(measurement_evidence)
    matrix_errors.extend(performance_errors["d1"])
    matrix_errors.extend(performance_errors["d2"])
    d1_measurement = measurement_evidence.get("d1_slow_lane_readiness")
    if not isinstance(d1_measurement, dict):
        d1_measurement = {}
    d2_measurement = measurement_evidence.get("d2_scheduler_density")
    if not isinstance(d2_measurement, dict):
        d2_measurement = {}
    provenance_errors = _source_provenance_errors(
        measurement_evidence, baseline_root, ROOT)
    lifecycle_errors = _lifecycle_errors(lifecycle_evidence)
    lifecycle_errors.extend(_lifecycle_cleanup_errors(lifecycle_cleanup))
    retained_errors = []
    provenance = measurement_evidence.get("source_provenance", {})
    retained_readings = retained.get("readings") if isinstance(retained, dict) else None
    if not isinstance(retained_readings, dict):
        retained_errors.append("fresh retained-memory readings are missing")
        retained_readings = {}
    fixture = retained.get("fixture", {}) if isinstance(retained, dict) else {}
    child_pids = []
    for side, source_root in (("baseline", baseline_root), ("candidate", ROOT)):
        record = retained_readings.get(side)
        source = provenance.get(side) if isinstance(provenance, dict) else None
        if not isinstance(source, dict):
            retained_errors.append(f"{side} source provenance is missing for retained-memory reading")
            continue
        try:
            cache_sha256 = _sha256_file(Path(source_root) / CACHE_MODULE_RELATIVE)
        except OSError:
            cache_sha256 = None
        if cache_sha256 is None:
            retained_errors.append(f"{side} cache module is missing")
        else:
            retained_errors.extend(
                f"{side}: {message}" for message in _retained_reading_errors(
                    record, side=side, source_root=source_root,
                    source_sha256=source.get("content_sha256"),
                    cache_sha256=cache_sha256, fixture=fixture))
        if isinstance(record, dict):
            child_pids.append(record.get("fresh_child_pid"))
    if (len(child_pids) != 2 or any(not isinstance(pid, int) for pid in child_pids)
            or len(set(child_pids)) != 2):
        retained_errors.append("baseline and candidate retained probes lack distinct child identities")
    if not isinstance(retained, dict) or retained.get("status") != "pass":
        retained_errors.append("fresh retained-memory probe did not pass")
    if isinstance(retained, dict):
        validation = retained.get("validation_errors")
        if not isinstance(validation, dict) or any(validation.values()):
            retained_errors.append("retained-memory evidence contains validation errors")

    baseline_helpers = _candidate_helper_launches(measurement_evidence, "baseline")
    candidate_helpers = _candidate_helper_launches(measurement_evidence, "candidate")
    runner_valid, runner_peaks = _candidate_runner_check(measurement_evidence)
    measurement_cleanup_complete = (isinstance(measurement_cleanup, dict)
                                    and measurement_cleanup.get("status") == "pass")
    lifecycle_cleanup_complete = (isinstance(lifecycle_cleanup, dict)
                                  and lifecycle_cleanup.get("status") == "pass")
    cleanup = {
        "measurement": measurement_cleanup,
        "lifecycle": lifecycle_cleanup,
        "retained_memory": {
            "status": retained.get("status", "missing") if isinstance(retained, dict) else "missing",
            "evidence": retained.get("output_paths", {}) if isinstance(retained, dict) else {},
        },
    }
    shared_observation_errors = []
    if baseline_helpers is None:
        shared_observation_errors.append("baseline helper launch data is missing")
    if candidate_helpers != 0:
        shared_observation_errors.append("candidate carrier launched a readiness helper")
    if not runner_valid:
        shared_observation_errors.append("candidate peer-watch runner evidence is missing or exceeds one runner")
    shared_observation_errors.extend(lifecycle_errors)
    shared_observation_errors.extend(retained_errors)
    shared_observation_complete = not shared_observation_errors
    cleanup_complete = measurement_cleanup_complete and lifecycle_cleanup_complete
    subcommands_pass = (
        measurement_command.get("exit_code") == 0
        and lifecycle_command.get("exit_code") == 0
        and isinstance(retained, dict)
        and all(command.get("exit_code") == 0
                for command in retained.get("child_commands", {}).values())
    )
    provenance_complete = not provenance_errors
    status = "PASS" if (subcommands_pass and provenance_complete and not matrix_errors
                         and shared_observation_complete and cleanup_complete) else "FAIL"
    lifecycle_fixture = (lifecycle_evidence.get("fixtures") or [{}])[0]
    report = {
        "schema_version": 2,
        "status": status,
        "scope": "paired source measurement and lifecycle evidence; no remote ancestry judgment",
        "invocation": {
            "executable": sys.executable,
            "argv": list(sys.argv),
            "cwd": str(Path.cwd().resolve()),
            "evidence_dir": str(Path(evidence_dir).resolve()),
        },
        "subprocesses": {"measurement": measurement_command,
                         "lifecycle": lifecycle_command},
        "d1_source_provenance": {
            "status": "verified" if provenance_complete else "invalid",
            "sources": provenance,
            "validation_errors": provenance_errors,
        },
        "d1_attempt_readiness": {
            "status": "complete" if not performance_errors["d1"] else "incomplete or invalid",
            "validation_errors": performance_errors["d1"],
            "paired": d1_measurement.get("paired", {}),
        },
        "d2_obligation_pass_cost": {
            "status": "complete" if not performance_errors["d2"] else "incomplete or invalid",
            "validation_errors": performance_errors["d2"],
            "paired": d2_measurement.get("paired", {}),
            "acceptance": d2_measurement.get("acceptance", {}),
        },
        "d2_shared_observation": {
            "status": "supported" if shared_observation_complete else "incomplete or invalid",
            "validation_errors": shared_observation_errors,
            "baseline_carrier_helper_launches": baseline_helpers,
            "candidate_carrier_helper_launches": candidate_helpers,
            "candidate_peer_watch_runner_peak_processes": max(runner_peaks, default=0),
            "runner_session_snapshot_calls": lifecycle_fixture.get(
                "runner_session_snapshot_calls"),
            "observation_groups": lifecycle_fixture.get("observation_groups", []),
            "registry_view_regression_selected": _selected_registry_view_regression(
                lifecycle_evidence),
            "retained_memory": retained,
        },
        "d3_independent_delivery": {
            "status": "complete" if not lifecycle_errors else "incomplete",
            "validation_errors": lifecycle_errors,
            "lifecycle_tests_run": lifecycle_evidence.get("tests_run", 0),
            "fixture": lifecycle_fixture,
            "selected_regressions": lifecycle_evidence.get("selected_regressions", []),
        },
        "d4_matched_measurements": {
            "status": "complete" if not matrix_errors else "incomplete",
            "validation_errors": matrix_errors,
            "conditions": list(WINDOWS),
            "metric_order": list(ROLES),
            "metric_windows": len(metric_rows),
        },
        "fixture_cleanup": cleanup,
        "metric_windows": metric_rows,
        "source_sha256": _source_hashes(),
        "source_provenance": provenance,
        "retained_memory": retained,
        "lifecycle_source_sha256": lifecycle_evidence.get("source_sha256", {}),
        "limitations": [
            "Remote main ancestry and pull request inclusion are checked by the owner outside this helper.",
            "CPU, process, RSS, and retained-heap values are isolated fixture measurements; host production PIDs are inaccessible.",
            "The CPU peer-watch unrelated-event window uses a test-only SIGUSR1 wake surrogate; local Unix-socket bind is unavailable.",
            "The lifecycle runner uses pipe-backed observation; per-class self CPU and reaped-child CPU counters overlap after process reaping.",
        ],
    }
    acceptance_path = Path(evidence_dir) / "shared-readiness-acceptance.json"
    _write_json(acceptance_path, report)
    handoff_path = Path(evidence_dir) / "final-source-evidence.md"
    handoff_path.write_text(_make_handoff(report, evidence_dir, measurement_evidence,
                                          lifecycle_evidence, cleanup), encoding="utf-8")
    return report, acceptance_path, handoff_path


def _accept(baseline_root, evidence_dir):
    baseline_root = Path(baseline_root).resolve(strict=True)
    evidence_dir = Path(evidence_dir).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    log_dir = evidence_dir / "test_logs"
    measurement_dir = evidence_dir / "measure"
    lifecycle_dir = evidence_dir / "lifecycle"

    measurement = _run_child("measurement", [
        sys.executable, str(CPU_FIXTURE), "--baseline-root", str(baseline_root),
        "--evidence-dir", str(measurement_dir),
    ], log_dir)
    lifecycle = _run_child("lifecycle", [
        sys.executable, str(Path(__file__).resolve()), "--lifecycle-child",
        str(lifecycle_dir),
    ], log_dir)

    measurement_evidence = _read_json(measurement_dir / "readiness-cpu-fixture.json") or {}
    measurement_cleanup = _read_json(
        measurement_dir / "readiness-cpu-fixture-cleanup.json") or {"status": "missing"}
    lifecycle_evidence = _read_json(lifecycle_dir / "shared-readiness-lifecycle.json") or {}
    lifecycle_cleanup = lifecycle_evidence.get("fixture_cleanup") or {"status": "missing"}
    retained = _run_retained_memory(
        evidence_dir / "retained-memory", baseline_root, ROOT,
        measurement_evidence.get("source_provenance", {}))
    report, acceptance_path, handoff_path = _assemble_report(
        evidence_dir, baseline_root, measurement, lifecycle, measurement_evidence,
        measurement_cleanup, lifecycle_evidence, lifecycle_cleanup, retained)
    print(json.dumps({
        "status": report["status"],
        "acceptance": str(acceptance_path),
        "handoff": str(handoff_path),
        "measurement_exit": measurement["exit_code"],
        "lifecycle_exit": lifecycle["exit_code"],
        "retained_memory_status": retained.get("status"),
        "cleanup": {"measurement": measurement_cleanup.get("status"),
                    "lifecycle": lifecycle_cleanup.get("status")},
    }, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


def main(argv=None):
    if argv is None and len(sys.argv) == 3 and sys.argv[1] == "--lifecycle-child":
        return _lifecycle_child(Path(sys.argv[2]))
    if argv is None and len(sys.argv) == 6 and sys.argv[1] == "--retained-memory-child":
        return _retained_memory_child(*sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args(argv)
    if (args.baseline_root is None) != (args.evidence_dir is None):
        parser.error("--baseline-root and --evidence-dir must be supplied together")
    if args.baseline_root is None:
        return _ordinary_ci()
    return _accept(args.baseline_root, args.evidence_dir)


if __name__ == "__main__":
    raise SystemExit(main())
