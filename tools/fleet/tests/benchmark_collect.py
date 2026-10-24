#!/usr/bin/env python3
"""Measure live Fleet collection and replay real registered jobs logs.

Evidence is intentionally written only to the caller supplied private directory.
The live mode uses the same ``collect_all`` entry point and kwargs as Explorer;
registry mode copies observed logs before repeating the registry evidence scan.
"""
from __future__ import annotations

import argparse
import cProfile
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import pstats
import shutil
import shutil
import statistics
import sys
import tempfile
import time
from unittest import mock


def _source(source_tree):
    tools = str(Path(source_tree).resolve() / "tools")
    if tools in sys.path:
        sys.path.remove(tools)
    sys.path.insert(0, tools)


def _summary(samples):
    return {
        "count": len(samples),
        "wall_seconds": [round(item["wall_ns"] / 1e9, 6) for item in samples],
        "cpu_seconds": [round(item["cpu_ns"] / 1e9, 6) for item in samples],
        "wall_median_seconds": statistics.median(item["wall_ns"] for item in samples) / 1e9,
        "cpu_median_seconds": statistics.median(item["cpu_ns"] for item in samples) / 1e9,
    }


def _profile(sample, profiler):
    stats = pstats.Stats(profiler)
    rows = []
    json_loads = 0
    for (filename, line, function), (primitive, calls, total, cumulative, _callers) in stats.stats.items():
        if function == "loads" and filename.endswith("/json/__init__.py"):
            json_loads += calls
        rows.append({"function": "%s:%d:%s" % (filename, line, function),
                     "calls": calls, "self_seconds": total,
                     "cumulative_seconds": cumulative})
    rows.sort(key=lambda item: item["cumulative_seconds"], reverse=True)
    sample["json_loads_calls"] = json_loads
    sample["top_cumulative"] = rows[:30]


def _live(args):
    _source(args.source_tree)
    from fleet.collectors import collect_all
    from fleet.collectors import dispatch

    samples = []
    parsed = getattr(dispatch, "parsed_registry_rows", None)
    previous_registry_objects = {}
    for index in range(args.iterations + 1):
        profiler = cProfile.Profile() if args.profile and index in (0, 1) else None
        registry_calls = []
        if parsed is not None:
            def observe_registry(path):
                value = parsed(path)
                registry_calls.append((os.path.abspath(os.fspath(path)), value))
                return value
            dispatch.parsed_registry_rows = observe_registry
        wall0, cpu0 = time.perf_counter_ns(), time.process_time_ns()
        if profiler:
            profiler.enable()
        # Explorer's first complete observation asks for a fast initial snapshot;
        # every subsequent pass uses the normal collection contract.
        snapshot = collect_all(harness_filter=None, fast_first=(index == 0))
        if profiler:
            profiler.disable()
        if parsed is not None:
            dispatch.parsed_registry_rows = parsed
        current_registry_objects = {}
        for path, value in registry_calls:
            current_registry_objects.setdefault(path, []).append(value)
        reused_in_tick = sum(max(0, len(values) - len({id(value) for value in values}))
                             for values in current_registry_objects.values())
        reused_from_previous = sum(
            1 for path, value in registry_calls
            if any(value is previous for previous in previous_registry_objects.get(path, ()))
        )
        previous_registry_objects = current_registry_objects
        sample = {"index": index, "kind": "first" if index == 0 else "steady",
                  "wall_ns": time.perf_counter_ns() - wall0,
                  "cpu_ns": time.process_time_ns() - cpu0,
                  "sessions": len(getattr(snapshot, "sessions", ())),
                  "jobs": len(getattr(snapshot, "jobs", ())),
                  "resources": len(getattr(snapshot, "resources", ())),
                  "parsed_registry_cache": {
                      "generations": len(getattr(dispatch, "_PARSED_REGISTRY_ROWS", ())),
                      "calls": len(registry_calls),
                      "same_object_reuses_within_tick": reused_in_tick,
                      "same_object_reuses_from_previous_tick": reused_from_previous,
                      "parse_lru": dispatch._parse_pipe_meta_cached.cache_info()._asdict(),
                  }}
        if profiler:
            _profile(sample, profiler)
        samples.append(sample)
    return {"mode": "live", "source_tree": str(Path(args.source_tree).resolve()),
            "python": sys.version.split()[0], "platform": platform.platform(),
            "samples": samples, "summary": _summary(samples),
            "first": samples[0], "steady": _summary(samples[1:])}


def _registry(args):
    _source(args.source_tree)
    from fleet.collectors import dispatch

    paths = ([Path(args.registry_input)] if args.registry_input else
             [Path(path) for path in dispatch._candidate_jobs_paths()])
    copies = []
    with tempfile.TemporaryDirectory(prefix="fleet-registry-replay-") as tmp:
        for index, path in enumerate(paths):
            if not path.is_file():
                continue
            target = Path(tmp) / ("jobs-%03d.log" % index)
            shutil.copyfile(path, target)
            copies.append(target)
        line_counts = {str(path): sum(1 for _line in path.open(encoding="utf-8", errors="replace"))
                       for path in copies}
        input_digest = hashlib.sha256()
        for path in copies:
            input_digest.update(path.read_bytes())
        previous_registry_objects = {}
        total_lines = sum(line_counts.values())
        if total_lines == 0:
            raise RuntimeError("no registered jobs log rows were found")
        samples = []
        for index in range(args.iterations + 1):
            profiler = cProfile.Profile() if args.profile and index in (0, 1) else None
            wall0, cpu0 = time.perf_counter_ns(), time.process_time_ns()
            if profiler:
                profiler.enable()
            parsed = getattr(dispatch, "parsed_registry_rows", None)
            registry_calls = []
            if parsed is not None:
                def observe_registry(path):
                    value = parsed(path)
                    registry_calls.append((os.path.abspath(os.fspath(path)), value))
                    return value
                dispatch.parsed_registry_rows = observe_registry
            route_nodes, terminal = {}, {}
            for _pass in range(args.passes_per_tick):
                route_nodes, terminal = dispatch._scan_registry_evidence(copies)
            if parsed is not None:
                dispatch.parsed_registry_rows = parsed
            if profiler:
                profiler.disable()
            parsed_rows = None
            if hasattr(dispatch, "parsed_registry_rows"):
                parsed_rows = sum(len(dispatch.parsed_registry_rows(path)) for path in copies)
            parse_cache = getattr(dispatch, "_parse_pipe_meta_cached", None)
            current_registry_objects = {}
            for path, value in registry_calls:
                current_registry_objects.setdefault(path, []).append(value)
            reused_in_tick = sum(max(0, len(values) - len({id(value) for value in values}))
                                 for values in current_registry_objects.values())
            reused_from_previous = sum(
                1 for path, value in registry_calls
                if any(value is previous for previous in previous_registry_objects.get(path, ()))
            )
            previous_registry_objects = current_registry_objects
            sample = {"index": index, "kind": "first" if index == 0 else "steady",
                      "wall_ns": time.perf_counter_ns() - wall0,
                      "cpu_ns": time.process_time_ns() - cpu0,
                      "route_count": len(route_nodes), "terminal_attempt_count": len(terminal),
                      "parsed_rows": parsed_rows if parsed_rows is not None else total_lines,
                      "parsed_generations": len(getattr(dispatch, "_PARSED_REGISTRY_ROWS", ())),
                      "parsed_registry_calls": len(registry_calls),
                      "same_object_reuses_within_tick": reused_in_tick,
                      "same_object_reuses_from_previous_tick": reused_from_previous,
                      "parse_lru": (parse_cache.cache_info()._asdict()
                                    if parse_cache is not None else None)}
            if profiler:
                _profile(sample, profiler)
            samples.append(sample)
    return {"mode": "registry-replay", "source_tree": str(Path(args.source_tree).resolve()),
            "python": sys.version.split()[0], "platform": platform.platform(),
            "registered_logs": [str(path) for path in paths], "copied_row_counts": line_counts,
            "copied_input_sha256": input_digest.hexdigest(),
            "passes_per_tick": args.passes_per_tick,
            "total_rows": total_lines, "samples": samples,
            "summary": _summary(samples), "first": samples[0],
            "steady": _summary(samples[1:])}


def _default_once(args):
    _source(args.source_tree)
    from fleet import fleet, render
    from fleet.refresh import LiveSnapshot

    fixture_path = Path(args.source_tree) / "tools/fleet/tests/fixtures/explorer/default-once.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    outputs = {}
    with mock.patch.object(fleet, "collect_all", return_value=LiveSnapshot()), \
            mock.patch.object(fleet, "_arm_stall_dump", return_value=None), \
            mock.patch.object(fleet.installinfo, "collect", return_value={}), \
            mock.patch.object(fleet.compute_hosts, "collect", return_value=[]), \
            mock.patch.object(render, "_collect_governor", return_value=None), \
            mock.patch.object(render, "_collect_memory", return_value=None), \
            mock.patch.object(render.time, "time", return_value=1_800_000_000):
        for width in fixture["widths"]:
            stdout, stderr = io.StringIO(), io.StringIO()
            size = shutil.os.terminal_size((width, 45))
            with mock.patch("shutil.get_terminal_size", return_value=size), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = fleet.main(["--once"])
            expected = fixture["outputs"][str(width)]
            actual = {"exit": code, "stdout": stdout.getvalue().encode().decode(),
                      "stderr": stderr.getvalue()}
            matched = (code == expected["exit"] and stderr.getvalue() == expected["stderr"]
                       and stdout.getvalue().encode() == expected["stdout"].encode())
            outputs[str(width)] = {"matched_base_bytes": matched,
                                   "stdout_bytes": len(stdout.getvalue().encode()),
                                   "exit": code}
            if not matched:
                raise RuntimeError("default fleet --once output differs at width %s" % width)
    return {"mode": "default-once", "source_tree": str(Path(args.source_tree).resolve()),
            "baseline_commit": fixture["baseline_commit"], "outputs": outputs}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--mode", choices=("live", "registry", "default-once"), default="live")
    parser.add_argument("--iterations", type=int, default=2,
                        help="steady collections after the first observation")
    parser.add_argument("--passes-per-tick", type=int, default=4,
                        help="repeated registry consumers within each tick")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--registry-input", help="private copied jobs.log to replay verbatim")
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be at least 1")
    if args.passes_per_tick < 1:
        parser.error("--passes-per-tick must be at least 1")
    if args.mode == "live":
        result = _live(args)
    elif args.mode == "registry":
        result = _registry(args)
    else:
        result = _default_once(args)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
