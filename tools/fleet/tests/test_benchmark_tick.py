#!/usr/bin/env python3
"""Integrity tests for the versioned Fleet tick benchmark CLI."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TOOLS_DIR = str(Path(__file__).resolve().parents[2])
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from fleet.tests import benchmark_tick
from fleet.collectors import codex, collect_all


SCRIPT = Path(benchmark_tick.__file__).resolve()
FIXTURE = SCRIPT.parent / "fixtures" / "tick_benchmark" / "v1" / "fixture.json"


def _sample(edge_builds=None, cwd_parses=6, cursor_initializations=0,
            exact_scans=0, phase="unchanged-warm"):
    paths = {
        name: {"helper_calls": 0, "costly_evaluations": 0,
               "physical_reads": 0, "rows_parsed": 0,
               "file_touches": 0, "unique_keys": 0, "by_phase": {}}
        for name in ("registry", "tcp", "seat-ledger", "route-gate", "owner-lifecycle")
    }
    return {
        "wall_ns": 100,
        "cpu_ns": 90,
        "counters": {
            "lifecycle_cursor_initializations": cursor_initializations,
            "lifecycle_exact_scans": exact_scans,
            "raw_edge_builds_by_home": edge_builds or {"$FIXTURE_ROOT/home": 1},
            "cwd_parses": cwd_parses,
            "cache_evictions": 0,
            "roots_visited": 1,
            "files_visited": 1,
            "phase": phase,
            "five_paths": paths,
        },
    }


def _valid_result():
    payload = {"sessions": [], "jobs": []}
    return {
        "benchmark_schema": benchmark_tick.BENCHMARK_SCHEMA,
        "fixture_schema": benchmark_tick.FIXTURE_SCHEMA,
        "fixture_sha256": "fixture-digest",
        "iterations": 20,
        "cold_sample": _sample(cursor_initializations=3, phase="cold"),
        "warm_samples": [_sample() for _index in range(20)],
        "append_warm_sample": _sample(phase="append-warm"),
        "warm_summary": {
            "wall_median_ns": 100,
            "wall_p95_ns": 100,
            "cpu_median_ns": 90,
            "cpu_p95_ns": 90,
        },
        "normalized_payload": payload,
        "semantic_digest": hashlib.sha256(
            benchmark_tick._canonical_bytes(payload)
        ).hexdigest(),
        "expected": {"unique_rollouts": 6},
    }


class _SentinelAuditRecorder:
    """Record filesystem probes beneath one path without touching it itself."""

    def __init__(self, prefix):
        self.prefix = os.path.abspath(prefix)
        self.active = False
        self.accesses = []

    def __call__(self, event, args):
        if not self.active or not args or event not in {
            "open", "os.listdir", "os.scandir",
        }:
            return
        try:
            path = os.path.abspath(os.fspath(args[0]))
        except (TypeError, ValueError):
            return
        if path == self.prefix or path.startswith(self.prefix + os.sep):
            self.accesses.append((event, path))


class HermeticFixtureIsolationTest(unittest.TestCase):
    """The hermetic tick may read only the fixture tree, never ambient state."""

    def _write_sentinel_state(self, root):
        dispatch = os.path.join(root, "dispatch")
        artifacts = os.path.join(root, "artifacts")
        state = os.path.join(root, "state")
        for path in (
            dispatch, artifacts, state,
            os.path.join(root, "home"),
            os.path.join(root, "xdg-state"),
            os.path.join(root, "xdg-data"),
            os.path.join(root, "xdg-cache"),
            os.path.join(root, "tmp"),
        ):
            os.makedirs(path)
        index = os.path.join(dispatch, "resource-runs.index.json")
        registry = os.path.join(dispatch, "resource-runs.json")
        route = os.path.join(dispatch, "route.json")
        jobs = os.path.join(dispatch, "jobs.log")
        attempt = "att-sentinel"
        owner_pid = 999999999
        starttime = 4611686018427387904
        pid_namespace = os.readlink("/proc/self/ns/pid")
        with open(route, "w", encoding="utf-8") as handle:
            json.dump({"route_id": "rt-sentinel"}, handle)
        metadata = ",".join((
            "attempt_id=" + attempt,
            "worker_type=owner",
            "owner_route_file=" + route,
            "owner_route_id=rt-sentinel",
            "owner_route_hash=hash-sentinel",
            "pid=" + str(owner_pid),
            "pid_start=" + str(starttime),
            "pid_ns=" + pid_namespace,
        ))
        with open(jobs, "w", encoding="utf-8") as handle:
            handle.write("\t".join((
                "2033-05-18T03:28:00+00:00", "open", "fixture", root,
                "sentinel", metadata,
            )) + "\n")
        run = {
            "cwd": root,
            "pid": owner_pid,
            "starttime": starttime,
            "command_hash": "sentinel",
            "pid_namespace": pid_namespace,
            "started_at": 1000.0,
            "resource_policy": "supervised-owner",
            "route": route,
            "route_id": "rt-sentinel",
            "parent_attempt_id": attempt,
            "jobs": jobs,
            "owner_wait": {
                "parent_attempt_id": attempt,
                "jobs": jobs,
                "owner_pid": owner_pid,
                "owner_start": starttime,
                "route_id": "rt-sentinel",
                "route_hash": "hash-sentinel",
            },
        }
        with open(registry, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": 1, "runs": {"run-sentinel": run}}, handle)
        with open(index, "w", encoding="utf-8") as handle:
            json.dump({
                "schema_version": 1,
                "registries": {"sentinel": {"path": registry}},
            }, handle)
        return {
            "AGENT_DISPATCH_JOBS": jobs,
            "AGENT_RESOURCE_RUN_INDEX": index,
            "HARNESS_STATE_ROOT": state,
            "AGENT_ARTIFACT_ROOT": artifacts,
            "HOME": os.path.join(root, "home"),
            "XDG_STATE_HOME": os.path.join(root, "xdg-state"),
            "XDG_DATA_HOME": os.path.join(root, "xdg-data"),
            "XDG_CACHE_HOME": os.path.join(root, "xdg-cache"),
            "TMPDIR": os.path.join(root, "tmp"),
        }

    @staticmethod
    def _snapshot(root):
        entries = {}
        for base, dirs, files in os.walk(root):
            dirs.sort()
            entries[os.path.relpath(base, root) + os.sep] = None
            for name in sorted(files):
                path = os.path.join(base, name)
                entries[os.path.relpath(path, root)] = (
                    Path(path).read_bytes(), os.stat(path).st_mtime_ns,
                )
        return entries

    def test_hermetic_tick_never_reads_ambient_dispatch_state(self):
        fixture, fixture_hash = benchmark_tick._load_fixture(str(FIXTURE))
        with tempfile.TemporaryDirectory() as tmp:
            fixture_root = os.path.join(tmp, "fixture")
            os.makedirs(fixture_root)
            sentinel = os.path.join(tmp, "sentinel")
            os.makedirs(sentinel)
            env = self._write_sentinel_state(sentinel)
            before = self._snapshot(sentinel)
            recorder = _SentinelAuditRecorder(sentinel)
            # Audit hooks cannot be removed; the recorder's active flag scopes
            # recording to this test's hermetic call.
            sys.addaudithook(recorder)
            with mock.patch.dict(os.environ, env):
                recorder.active = True
                try:
                    benchmark_tick._hermetic_result(
                        fixture, fixture_hash, fixture_root, 1)
                finally:
                    recorder.active = False
            self.assertEqual([], recorder.accesses)
            self.assertEqual(before, self._snapshot(sentinel))
            self.assertEqual([], collect_all.last_resource_jobs)


class BenchmarkComparisonGateTest(unittest.TestCase):

    def test_phase_counter_separates_calls_unique_keys_and_tcp_scans(self):
        counters = benchmark_tick._Counters("/tmp/fixture")
        counters.reset("cold")
        counters.record("registry", "helper_calls", key=("jobs.log",))
        counters.record("registry", "helper_calls", key=("jobs.log",))
        counters.record("registry", "costly_evaluations", key=("jobs.log",))
        counters.record("tcp", "helper_calls", key=("initial", "43123"))
        counters.record("tcp", "costly_evaluations", key=("initial", "43123"))
        counters.record("tcp", "helper_calls", key=("recheck", "43123"))
        result = counters.snapshot()
        self.assertEqual(result["phase"], "cold")
        self.assertEqual(result["five_paths"]["registry"]["helper_calls"], 2)
        self.assertEqual(result["five_paths"]["registry"]["unique_keys"], 1)
        self.assertEqual(set(result["five_paths"]), {
            "registry", "tcp", "seat-ledger", "route-gate", "owner-lifecycle",
        })
        self.assertEqual(set(result["five_paths"]["tcp"]["by_phase"]), {
            "initial", "recheck",
        })

    def test_invalid_cold_structural_counter_fails_comparison(self):
        baseline = _valid_result()
        for name, mutate, check in (
            (
                "edge build",
                lambda result: result["cold_sample"]["counters"][
                    "raw_edge_builds_by_home"
                ].update({"$FIXTURE_ROOT/home": 2}),
                "edge_builds_at_most_one_per_home",
            ),
            (
                "cwd parse",
                lambda result: result["cold_sample"]["counters"].update(
                    {"cwd_parses": 7}
                ),
                "cwd_parses_equal_unique_rollouts",
            ),
        ):
            with self.subTest(name=name):
                current = copy.deepcopy(baseline)
                mutate(current)
                comparison = benchmark_tick._comparison(baseline, current)
                self.assertFalse(comparison["pass"])
                self.assertFalse(comparison["checks"][check])
                self.assertTrue(
                    comparison["checks"][
                        "unchanged_warm_lifecycle_cursor_initializations_zero"
                    ]
                )

    def test_lifecycle_observer_counters_separate_exact_scans_from_cursor_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rollout.jsonl")
            Path(path).write_text(
                '{"type":"event_msg","payload":'
                '{"type":"task_started","turn_id":"t"}}\n',
                encoding="utf-8",
            )
            codex._LIFECYCLE_CACHE.clear()
            counters = benchmark_tick._Counters(tmp)
            with benchmark_tick._instrument(counters):
                codex._latest_task_lifecycle(path)
                codex._latest_task_lifecycle(path)
                codex._latest_task_lifecycle(path, max_scan=None)
            observed = counters.snapshot()
        self.assertEqual(observed["lifecycle_calls"], 3)
        self.assertEqual(observed["lifecycle_cursor_initializations"], 1)
        self.assertEqual(observed["lifecycle_exact_scans"], 1)
        self.assertNotIn("cache_hits", observed)
        self.assertNotIn("cache_misses", observed)


class BenchmarkComparisonCliTest(unittest.TestCase):

    def _assert_invalid_cold_counter(self, mutate, affected_check):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline_path = root / "baseline.json"
            result_path = root / "result.json"
            comparison_path = root / "comparison.json"
            baseline = _valid_result()
            current = copy.deepcopy(baseline)
            mutate(current)
            baseline_path.write_text(
                json.dumps(baseline, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            baseline_before = baseline_path.read_bytes()

            with mock.patch.object(
                benchmark_tick, "_hermetic_result", return_value=current
            ):
                return_code = benchmark_tick.main([
                    "--fixture",
                    str(FIXTURE),
                    "--iterations",
                    "20",
                    "--compare-baseline",
                    str(baseline_path),
                    "--result-out",
                    str(result_path),
                    "--comparison-out",
                    str(comparison_path),
                ])

            comparison = json.loads(
                comparison_path.read_text(encoding="utf-8")
            )
            self.assertEqual(return_code, 1)
            self.assertTrue(result_path.is_file())
            self.assertFalse(comparison["pass"])
            self.assertFalse(comparison["checks"][affected_check])
            self.assertEqual(baseline_path.read_bytes(), baseline_before)

    def test_main_rejects_invalid_cold_edge_build_count(self):
        self._assert_invalid_cold_counter(
            lambda result: result["cold_sample"]["counters"][
                "raw_edge_builds_by_home"
            ].update({"$FIXTURE_ROOT/home": 2}),
            "edge_builds_at_most_one_per_home",
        )

    def test_main_rejects_invalid_cold_cwd_parse_count(self):
        self._assert_invalid_cold_counter(
            lambda result: result["cold_sample"]["counters"].update(
                {"cwd_parses": 7}
            ),
            "cwd_parses_equal_unique_rollouts",
        )


class BenchmarkPathAliasCliTest(unittest.TestCase):

    def _write_baseline(self, path):
        path.write_text(
            json.dumps(_valid_result(), sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _run(self, cwd, baseline, result, comparison):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--fixture",
                str(FIXTURE),
                "--iterations",
                "20",
                "--compare-baseline",
                str(baseline),
                "--result-out",
                str(result),
                "--comparison-out",
                str(comparison),
            ],
            cwd=str(cwd),
            text=True,
            capture_output=True,
        )

    def _assert_rejected_unchanged(
        self, cwd, baseline, result, comparison, watched
    ):
        before = {path: path.read_bytes() for path in watched}
        completed = self._run(cwd, baseline, result, comparison)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("must resolve to distinct paths", completed.stderr)
        self.assertEqual(
            {path: path.read_bytes() for path in watched},
            before,
        )

    def test_direct_baseline_result_and_comparison_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline.json"
            other = root / "other.json"
            self._write_baseline(baseline)
            other.write_bytes(b"other sentinel\n")
            with self.subTest(alias="result"):
                self._assert_rejected_unchanged(
                    root, baseline, baseline, other, [baseline, other]
                )
            with self.subTest(alias="comparison"):
                self._assert_rejected_unchanged(
                    root, baseline, other, baseline, [baseline, other]
                )

    def test_relative_and_absolute_baseline_alias_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline.json"
            comparison = root / "comparison.json"
            self._write_baseline(baseline)
            comparison.write_bytes(b"comparison sentinel\n")
            self._assert_rejected_unchanged(
                root,
                baseline.resolve(),
                Path("baseline.json"),
                comparison,
                [baseline, comparison],
            )

    def test_symlink_and_hardlink_baseline_aliases_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline.json"
            comparison = root / "comparison.json"
            self._write_baseline(baseline)
            comparison.write_bytes(b"comparison sentinel\n")
            symlink = root / "baseline-symlink.json"
            symlink.symlink_to(baseline)
            self._assert_rejected_unchanged(
                root, baseline, symlink, comparison, [baseline, comparison]
            )
            hardlink = root / "baseline-hardlink.json"
            os.link(str(baseline), str(hardlink))
            self._assert_rejected_unchanged(
                root, baseline, hardlink, comparison, [baseline, comparison]
            )

    def test_result_and_comparison_direct_and_symlink_aliases_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline.json"
            output = root / "output.json"
            self._write_baseline(baseline)
            output.write_bytes(b"output sentinel\n")
            self._assert_rejected_unchanged(
                root, baseline, output, output, [baseline, output]
            )
            output_link = root / "output-symlink.json"
            output_link.symlink_to(output)
            self._assert_rejected_unchanged(
                root, baseline, output, output_link, [baseline, output]
            )


if __name__ == "__main__":
    unittest.main()
