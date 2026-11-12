#!/usr/bin/env python3
"""Deterministic harness checks for production PTY measurement contracts."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import ast
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


MODULE_PATH = Path(__file__).with_name("benchmark_navigation.py")
SPEC = importlib.util.spec_from_file_location("benchmark_navigation", MODULE_PATH)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class BenchmarkNavigationReadinessTest(unittest.TestCase):

    def test_real_detail_observation_is_recognized_by_both_measurement_paths(self):
        from tools.fleet.refresh import LiveSnapshot, set_required_details, required_details_complete

        snapshot = LiveSnapshot(observations={
            "basic": {"state": "ok", "generation_identity": "current-basic"}})
        set_required_details(snapshot, "ok", all_new=True)
        self.assertTrue(required_details_complete(snapshot.observations))
        readiness = {"ready": True, "fully_fresh": True, "all_new": True,
                     "sources": snapshot.observations}
        metrics = benchmark._frame_metrics(
            readiness, generation=1, replay_active=False,
            frame_mode="overview", frame_present=True)
        self.assertTrue(metrics["fresh_all_new"])

        # Exercise the code shipped to the real launcher without installing its
        # process-wide observation wrappers in this test process.
        functions = [node for node in ast.parse(benchmark._observer_source()).body
                     if isinstance(node, ast.FunctionDef)
                     and node.name in ("_states", "_frame_event")]
        namespace = {}
        exec(compile(ast.Module(body=functions, type_ignores=[]),
                     "production-observer", "exec"), namespace)
        render = SimpleNamespace(_EXPLORER_FRAME=SimpleNamespace(mode="overview"),
                                 _EXPLORER_READINESS=readiness,
                                 _EXPLORER_REPLAY_ACTIVE=False,
                                 _EXPLORER_GENERATION=1)
        self.assertTrue(namespace["_frame_event"](render)["fresh_all_new"])
        self.assertEqual(namespace["_states"](snapshot.observations),
                         namespace["_states"](readiness))
        from tools.fleet.explorer.runtime import ExplorerRuntime
        render._EXPLORER_FRAME = ExplorerRuntime().loading_frame(100, 45)
        loading = namespace["_frame_event"](render)
        self.assertTrue(loading["loading"])
        self.assertFalse(loading["fresh_complete"])

    def _readiness(self, *, detail="complete", all_new=True):
        ready = detail == "complete" or detail == "continued"
        return {"ready": ready, "fully_fresh": ready,
                "all_new": all_new, "pending": [] if detail == "complete" else ["required-details"],
                "sources": {
                    "basic": {"state": "complete", "all_new": True},
                    "required-details": {"state": detail, "all_new": all_new,
                                         "continuation_validated": detail == "continued"},
                    "resource-runs": {"state": "absent", "attempted": True, "all_new": True},
                }}

    def test_generation_with_pending_required_detail_is_not_fresh(self):
        metrics = benchmark._frame_metrics(
            self._readiness(detail="pending"), generation=8,
            replay_active=False, frame_mode="overview", frame_present=True)
        self.assertFalse(metrics["ready"])
        self.assertFalse(metrics["fresh_complete"])
        self.assertFalse(metrics["fresh_all_new"])

    def test_loading_and_replay_frames_are_not_fresh(self):
        readiness = self._readiness()
        loading = benchmark._frame_metrics(
            readiness, generation=4, replay_active=False,
            frame_mode="loading", frame_present=True)
        replay = benchmark._frame_metrics(
            readiness, generation=4, replay_active=True,
            frame_mode="overview", frame_present=True)
        self.assertFalse(loading["fresh_complete"])
        self.assertFalse(replay["fresh_complete"])
        self.assertTrue(replay["replay"])

    def test_fresh_and_all_new_are_separate_predicates(self):
        metrics = benchmark._frame_metrics(
            self._readiness(all_new=False), generation=2,
            replay_active=False, frame_mode="overview", frame_present=True)
        self.assertTrue(metrics["fresh_complete"])
        self.assertFalse(metrics["fresh_all_new"])

    def test_validated_continuation_is_fresh_but_not_all_new(self):
        metrics = benchmark._frame_metrics(
            self._readiness(detail="continued", all_new=False), generation=2,
            replay_active=False, frame_mode="overview", frame_present=True)
        self.assertTrue(metrics["fresh_complete"])
        self.assertFalse(metrics["fresh_all_new"])
        pending = self._readiness(detail="continued", all_new=False)
        pending["sources"]["required-details"]["continuation_validated"] = False
        self.assertFalse(benchmark._frame_metrics(
            pending, generation=2, replay_active=False,
            frame_mode="overview", frame_present=True)["fresh_complete"])

    def test_production_observer_wraps_completed_window_refresh(self):
        nodes = ast.parse(benchmark._observer_source()).body
        cls = next(node for node in nodes if isinstance(node, ast.ClassDef)
                   and node.name == "WindowRefreshObserver")
        namespace = {}
        exec(compile(ast.Module(body=[cls], type_ignores=[]),
                     "production-refresh-observer", "exec"), namespace)
        calls = []

        class Window:
            def refresh(self):
                calls.append("refresh-completed")
                return 17

        observer = namespace["WindowRefreshObserver"](
            Window(), lambda: calls.append("before"),
            lambda succeeded: calls.append(("after", succeeded)))
        self.assertEqual(observer.refresh(), 17)
        self.assertEqual(calls, ["before", "refresh-completed", ("after", True)])


class BenchmarkProductionContractTest(unittest.TestCase):

    def test_production_observer_source_compiles_with_optional_startup_profiling(self):
        compile(benchmark._observer_source(), "production-observer", "exec")
        profile = benchmark._startup_profile_summary([
            {"kind": "installinfo-call", "fast_local": True, "duration_ns": 500},
            {"kind": "synchronous-draw", "loading": True, "duration_ns": 300},
            {"kind": "readiness-check", "ready": False, "duration_ns": 100},
        ])
        imports = benchmark._importtime_profile(
            b"import time: 10 | 30 | fleet.collectors\n"
            b"import time: 5 | 8 | json\n")
        self.assertEqual(imports["entry_count"], 2)
        self.assertEqual(imports["largest_cumulative"][0]["module"], "fleet.collectors")
        self.assertEqual(len(profile["installinfo_calls"]), 1)
        self.assertEqual(len(profile["synchronous_draw_samples"]), 1)
        self.assertEqual(len(profile["readiness_check_samples"]), 1)

    def test_production_schedule_keeps_exact_key_count_and_duration(self):
        schedule = benchmark._production_schedule(100, 60)
        self.assertEqual(len(schedule), 100)
        self.assertEqual(schedule[0], 0)
        self.assertEqual(schedule[-1], 59.4)
        self.assertEqual(benchmark._production_schedule(0, 60), [])

    def test_production_order_alternates_pinned_arms(self):
        self.assertEqual(benchmark._production_order(), [
            ["base", "candidate", "legacy"],
            ["candidate", "base", "legacy"],
            ["base", "candidate", "legacy"],
        ])

    def test_production_legacy_launcher_supports_current_and_historical_trees(self):
        with tempfile.TemporaryDirectory() as tmp:
            for legacy_layout, expected_legacy in (
                    ("current", "fleet-monitor.sh"),
                    ("historical", "fleet.sh")):
                with self.subTest(legacy_layout=legacy_layout):
                    fixture_root = Path(tmp) / legacy_layout
                    trees = {}
                    for role in ("base", "candidate"):
                        tree = fixture_root / role
                        path = tree / "tools/fleet/fleet2.sh"
                        path.parent.mkdir(parents=True)
                        path.write_text("#!/bin/sh\n", encoding="utf-8")
                        trees[role] = tree

                    legacy_tree = fixture_root / "legacy"
                    legacy_dir = legacy_tree / "tools/fleet"
                    legacy_dir.mkdir(parents=True)
                    legacy_names = (["fleet-monitor.sh", "fleet.sh"]
                                    if legacy_layout == "current" else ["fleet.sh"])
                    for launcher in legacy_names:
                        (legacy_dir / launcher).write_text("#!/bin/sh\n", encoding="utf-8")
                    trees["legacy"] = legacy_tree

                    self.assertEqual(benchmark._production_argv("legacy", trees), [
                        str((legacy_dir / expected_legacy).resolve())
                    ])
                    for role in ("base", "candidate"):
                        self.assertEqual(benchmark._production_argv(role, trees), [
                            str((trees[role] / "tools/fleet/fleet2.sh").resolve())
                        ])

                    argv = [
                        "benchmark_navigation.py", "--mode", "production",
                        "--source-tree", str(trees["candidate"]),
                        "--base-tree", str(trees["base"]),
                        "--legacy-tree", str(trees["legacy"]),
                        "--runtime-dir", str(fixture_root / "runtime"),
                        "--evidence-dir", str(fixture_root / "evidence"),
                    ]
                    with mock.patch("sys.argv", argv), \
                            mock.patch.object(benchmark, "_production_campaign",
                                              return_value={"status": "complete"}) as campaign, \
                            mock.patch("builtins.print"):
                        self.assertEqual(benchmark.main(), 0)
                    campaign.assert_called_once()

    def test_environment_preserves_live_registry_and_xdg_but_clears_worker_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            observer = root / "observer"
            cache = root / "cache"
            with mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_JOBS": "/private/registry/jobs.log",
                "AGENT_ROUTE_FILE": "/private/worker/route.json",
                "AGENT_DISPATCH_MODEL_ROLE": "fast-implementer",
                "XDG_STATE_HOME": "/private/user/state",
                "PYTHONPATH": "/existing/python/path",
            }, clear=True):
                env = benchmark._production_environment(
                    root, cache, observer, 22, 1234, root / "runtime")
            self.assertEqual(env["AGENT_DISPATCH_JOBS"], "/private/registry/jobs.log")
            self.assertEqual(env["XDG_STATE_HOME"], "/private/user/state")
            self.assertNotIn("AGENT_ROUTE_FILE", env)
            self.assertNotIn("AGENT_DISPATCH_MODEL_ROLE", env)
            self.assertNotIn("PYTHONDONTWRITEBYTECODE", env)
            self.assertEqual(env["PYTHONPYCACHEPREFIX"], str(root / "runtime" / "pycache"))
            self.assertTrue(env["PYTHONPATH"].startswith(str(observer)))

    def test_explicit_no_bytecode_setting_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"}, clear=True):
                env = benchmark._production_environment(
                    root, root / "cache", root / "observer", 22, 1234,
                    root / "runtime")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")

    def test_fresh_paint_does_not_mask_nonzero_process_exit(self):
        # A painted fresh frame is evidence of rendering, not a successful process exit.
        status, error = benchmark._production_status(
            1, agent_home_matches_tree=True, seed=False, cache_seed_valid=False)
        self.assertEqual(status, "nonzero-exit")
        self.assertIsNone(error)

    def test_quit_wait_allows_bounded_cleanup_past_old_two_second_window(self):
        clock = SimpleNamespace(now=0.0)
        events = []
        sampled = []

        class Process:
            pid = 123

            def __init__(self):
                self.q_decoded = False
                self.returncode = None
                self.reaped = False

            def write_q(self):
                self.q_decoded = True
                events.append({"kind": "input-decoded", "input_kind": "quit"})

            def poll(self):
                if self.q_decoded and clock.now >= 4.25:
                    self.returncode = 0
                return self.returncode

            def wait(self, timeout=None):
                self.assert_poll_complete()
                self.reaped = True
                return self.returncode

            def assert_poll_complete(self):
                if self.poll() is None:
                    raise AssertionError("normal quit wait returned before cleanup finished")

        process = Process()
        process.write_q()

        def drain(_timeout):
            clock.now += 0.25

        result = benchmark._production_closeout(
            process, 0, drain, lambda: sampled.append(clock.now),
            monotonic=lambda: clock.now,
            monotonic_ns=lambda: int(clock.now * 1_000_000_000),
            killpg=lambda *_args: self.fail("normal cleanup must not be escalated"))

        self.assertTrue(any(event.get("input_kind") == "quit" for event in events))
        self.assertGreater(clock.now, 2.0)
        self.assertLess(clock.now, benchmark._PRODUCTION_NORMAL_QUIT_TIMEOUT_SECONDS)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["exit_after_q"], 0)
        self.assertIsNone(result["sigterm_ns"])
        self.assertIsNone(result["sigkill_ns"])
        self.assertTrue(process.reaped)
        self.assertEqual(result["parent_actions"][-1]["action"], "process-reaped")
        self.assertEqual(result["parent_actions"][-1]["elapsed_after_q_ms"], 4250.0)
        self.assertEqual(len(sampled), 17)

    def test_hung_quit_process_is_escalated_and_reaped(self):
        clock = SimpleNamespace(now=0.0)
        signals = []

        class Process:
            pid = 456
            returncode = None
            reaped = False

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                if timeout is not None:
                    raise benchmark.subprocess.TimeoutExpired("fake-fleet", timeout)
                self.reaped = True
                return self.returncode

        process = Process()

        def killpg(_pid, sig):
            signals.append(sig)
            if sig == benchmark.signal.SIGKILL:
                process.returncode = -benchmark.signal.SIGKILL

        result = benchmark._production_closeout(
            process, 0, lambda _timeout: setattr(clock, "now", clock.now + 0.5),
            lambda: None, monotonic=lambda: clock.now,
            monotonic_ns=lambda: int(clock.now * 1_000_000_000), killpg=killpg)

        self.assertEqual(signals, [benchmark.signal.SIGTERM, benchmark.signal.SIGKILL])
        self.assertIsNone(result["exit_after_q"])
        self.assertIsNotNone(result["sigterm_ns"])
        self.assertIsNotNone(result["sigkill_ns"])
        self.assertEqual(result["exit_code"], -benchmark.signal.SIGKILL)
        self.assertTrue(process.reaped)
        self.assertEqual([item["action"] for item in result["parent_actions"]],
                         ["sigterm", "sigkill", "process-reaped"])
        status, _error = benchmark._production_status(
            result["exit_code"], agent_home_matches_tree=True)
        self.assertEqual(status, "nonzero-exit")

    def test_post_exit_drain_keeps_trailing_telemetry_and_nonzero_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = root / "fake-fleet2"
            launcher.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "fd = int(os.environ['FLEET_BENCHMARK_TELEMETRY_FD'])\n"
                "record = b'{\"kind\":\"diagnostic-padding\",\"padding\":\"' + b'x' * 96 + b'\"}\\n'\n"
                "payload = record * 3200\n"
                "while payload:\n"
                "    written = os.write(fd, payload)\n"
                "    payload = payload[written:]\n"
                "os.write(fd, b'{\"kind\":\"process-exit\",\"process_times\":{}}\\n')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8")
            launcher.chmod(0o700)
            with mock.patch.object(benchmark, "_production_argv",
                                   return_value=[str(launcher)]):
                result = benchmark._production_run(
                    "candidate", root, root / "cache", root / "observer",
                    root / "runtime", root / "run", keys=0, duration=0,
                    event_timeout=1)
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["status"], "nonzero-exit")
        self.assertTrue(result["shutdown"]["process_exit_event_seen"])
        self.assertTrue(result["shutdown"]["telemetry_eof_seen"])
        self.assertGreater(result["telemetry_raw"]["bytes"], 262144)

    def test_cache_validation_requires_private_complete_all_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text(json.dumps({"fresh_complete": True, "all_new": True}),
                            encoding="utf-8")
            path.chmod(0o600)
            self.assertTrue(benchmark._valid_cache(path))
            path.chmod(0o644)
            self.assertFalse(benchmark._valid_cache(path))

    def test_raw_runtime_writer_creates_private_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private" / "capture.raw"
            benchmark._write_private_bytes(path, b"private payload")
            self.assertEqual(path.read_bytes(), b"private payload")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_pump_records_distinguish_initial_and_steady_phases(self):
        samples = benchmark._pump_phase_samples([
            {"kind": "pump-start", "name": "fleet-refresh", "time_ns": 10,
             "interval_seconds": 2},
            {"kind": "pump-done", "name": "fleet-refresh", "duration_ns": 5,
             "cpu_ns": 3},
            {"kind": "pump-start", "name": "fleet-refresh", "time_ns": 2_000_000_010,
             "interval_seconds": 2},
            {"kind": "pump-done", "name": "fleet-refresh", "duration_ns": 7,
             "cpu_ns": 4},
            {"kind": "pump-done", "name": "fleet-detail-refresh", "duration_ns": 8,
             "cpu_ns": 2},
        ])
        done = [row for row in samples if row.get("duration_ns") is not None]
        self.assertEqual([row["phase"] for row in done], [
            "first-basic", "steady-basic", "required-detail",
        ])
        other = benchmark._pump_phase_samples([
            {"kind": "pump-done", "name": "fleet-install-refresh", "duration_ns": 3},
            {"kind": "pump-done", "name": "fleet-selection", "duration_ns": 4},
        ])
        self.assertEqual([row["phase"] for row in other], ["install", "selection"])
        cadence = [row for row in samples if row["phase"] == "start"][-1]
        self.assertEqual(cadence["observed_cadence_seconds"], 2)


class BenchmarkInputCorrelationTest(unittest.TestCase):

    @staticmethod
    def _sample(stream, sample, key, start, write_ns):
        payload = b"\x1b[A" if key == "up" else b"\x1b[B"
        stream.update(payload)
        return {
            "sample": sample, "key": key, "parent_write_ns": write_ns,
            "bytes_requested": len(payload), "bytes_written": len(payload),
            "byte_start": start, "byte_end": start + len(payload),
            "stream_prefix_sha256": stream.hexdigest(), "write_error": None,
        }

    @staticmethod
    def _decoded(input_id, kind, row, time_ns, *, count=None, digest=None,
                 start=None, end=None, proven=True):
        return {
            "kind": "input-decoded", "input_id": input_id,
            "input_kind": kind, "time_ns": time_ns,
            "source_byte_start": row["byte_start"] if start is None else start,
            "source_byte_end": row["byte_end"] if end is None else end,
            "source_byte_proven": proven,
            "child_byte_count": row["byte_end"] if count is None else count,
            "child_stream_sha256": (row["stream_prefix_sha256"]
                                    if digest is None else digest),
        }

    @staticmethod
    def _raw(start, end):
        return [{"kind": "input-raw-byte", "byte_offset": offset}
                for offset in range(start, end)]

    def test_unknown_and_quit_interleaving_do_not_shift_navigation_matches(self):
        stream = hashlib.sha256()
        down = self._sample(stream, 0, "down", 0, 100)
        stream.update(b"x")
        unknown_digest = stream.hexdigest()
        up = self._sample(stream, 1, "up", 4, 200)
        stream.update(b"q")
        quit_digest = stream.hexdigest()
        events = [
            *self._raw(0, 8),
            self._decoded(1, "down", down, 120),
            {"kind": "input-decoded", "input_id": 2, "input_kind": None,
             "time_ns": 130, "source_byte_start": 3, "source_byte_end": 4,
             "source_byte_proven": True, "child_byte_count": 4,
             "child_stream_sha256": unknown_digest},
            self._decoded(3, "up", up, 230),
            {"kind": "input-decoded", "input_id": 4, "input_kind": "quit",
             "time_ns": 300, "source_byte_start": 7, "source_byte_end": 8,
             "source_byte_proven": True, "child_byte_count": 8,
             "child_stream_sha256": quit_digest},
            {"kind": "flush", "input_id": 1, "time_ns": 160},
            {"kind": "flush", "input_id": 3, "time_ns": 260},
            {"kind": "flush", "input_id": 4, "time_ns": 310},
        ]

        samples = benchmark._match_production_keys([down, up], events)

        self.assertEqual([row["status"] for row in samples], ["painted", "painted"])
        self.assertEqual([row["input_id"] for row in samples], [1, 3])
        self.assertEqual([row["end_to_flush_ms"] for row in samples], [0.00006, 0.00006])
        self.assertNotIn(4, [row["input_id"] for row in samples])
        self.assertEqual(sum(item.get("kind") == "input-decoded" for item in events), 4)
        self.assertEqual(sum(item.get("input_kind") in ("up", "down")
                             for item in events if item.get("kind") == "input-decoded"), 2)
        self.assertEqual(sum(item.get("kind") == "flush" for item in events), 3)

    def test_missing_and_repeated_arrow_decodes_fail_closed(self):
        stream = hashlib.sha256()
        first = self._sample(stream, 0, "down", 0, 100)
        second = self._sample(stream, 1, "up", 3, 200)
        events = [
            *self._raw(0, 6),
            self._decoded(2, "up", second, 300),
            self._decoded(3, "up", second, 301),
            {"kind": "flush", "input_id": 2, "time_ns": 400},
            {"kind": "flush", "input_id": 3, "time_ns": 401},
        ]

        samples = benchmark._match_production_keys([first, second], events)

        self.assertEqual([row["status"] for row in samples],
                         ["missing-decode", "ambiguous-decode"])
        self.assertTrue(all(row["end_to_flush_ms"] is None for row in samples))

        repeated_stream = hashlib.sha256()
        first = self._sample(repeated_stream, 0, "down", 0, 100)
        second = self._sample(repeated_stream, 1, "up", 3, 200)
        actual_repeated = hashlib.sha256(b"\x1b[B\x1b[B").hexdigest()
        repeated_events = [
            *self._raw(0, 6),
            self._decoded(1, "down", first, 150),
            self._decoded(2, "down", second, 250, digest=actual_repeated),
            {"kind": "flush", "input_id": 1, "time_ns": 160},
            {"kind": "flush", "input_id": 2, "time_ns": 260},
        ]
        repeated = benchmark._match_production_keys(
            [first, second], repeated_events)
        self.assertEqual([row["status"] for row in repeated],
                         ["painted", "ambiguous-read"])
        self.assertIsNone(repeated[1]["end_to_flush_ms"])

    def test_unknown_arrow_span_and_stream_digest_mismatch_are_not_latencies(self):
        stream = hashlib.sha256()
        sample = self._sample(stream, 0, "down", 0, 100)
        unknown = self._decoded(1, None, sample, 120)
        unknown["input_kind"] = None
        events = [*self._raw(0, 3), unknown,
                  {"kind": "flush", "input_id": 1, "time_ns": 130}]
        outcome = benchmark._match_production_keys([sample], events)
        self.assertEqual(outcome[0]["status"], "unknown-decode")
        self.assertIsNone(outcome[0]["end_to_flush_ms"])

        mismatched = self._decoded(2, "down", sample, 140,
                                   digest=hashlib.sha256(b"\x1b[A").hexdigest())
        mismatch = benchmark._match_production_keys(
            [sample], [*self._raw(0, 3), mismatched,
                       {"kind": "flush", "input_id": 2, "time_ns": 150}])
        self.assertEqual(mismatch[0]["status"], "ambiguous-read")
        self.assertIsNone(mismatch[0]["end_to_flush_ms"])

    def test_partial_write_and_write_failure_keep_exact_ranges(self):
        stream = hashlib.sha256()
        partial, next_offset = benchmark._write_pty_input(
            7, b"\x1b[B", 0, stream, write=lambda _fd, _data: 2,
            monotonic_ns=iter((10, 20)).__next__)
        self.assertEqual((partial["byte_start"], partial["byte_end"]), (0, 2))
        self.assertEqual(partial["bytes_written"], 2)
        self.assertEqual(next_offset, 2)
        self.assertEqual(partial["stream_prefix_sha256"],
                         hashlib.sha256(b"\x1b[").hexdigest())

        def fail_write(_fd, _data):
            raise OSError("closed PTY")

        failed, unchanged_offset = benchmark._write_pty_input(
            7, b"\x1b[A", next_offset, stream, write=fail_write,
            monotonic_ns=iter((30, 40)).__next__)
        self.assertEqual(failed["write_error"], "OSError")
        self.assertEqual((failed["byte_start"], failed["byte_end"]), (2, 2))
        self.assertEqual(failed["bytes_written"], 0)
        self.assertEqual(unchanged_offset, 2)
        self.assertEqual(failed["stream_prefix_sha256"], partial["stream_prefix_sha256"])
        outcomes = benchmark._match_production_keys([partial, failed], [])
        self.assertEqual([row["status"] for row in outcomes], ["partial-write", "write-error"])

    def test_coalesced_source_span_cannot_paint_two_scheduled_samples(self):
        stream = hashlib.sha256()
        first = self._sample(stream, 0, "down", 0, 100)
        second = self._sample(stream, 1, "up", 3, 200)
        event = self._decoded(1, "down", second, 300, start=0, end=6, count=6)
        event["child_stream_sha256"] = second["stream_prefix_sha256"]
        events = [*self._raw(0, 6), event, {"kind": "flush", "input_id": 1,
                                             "time_ns": 400}]

        samples = benchmark._match_production_keys([first, second], events)

        self.assertEqual([row["status"] for row in samples],
                         ["coalesced-decode", "coalesced-decode"])
        self.assertTrue(all(row["end_to_flush_ms"] is None for row in samples))

    def test_delayed_flush_is_measured_and_duplicate_flush_is_ambiguous(self):
        stream = hashlib.sha256()
        sample = self._sample(stream, 0, "down", 0, 100)
        decoded = self._decoded(8, "down", sample, 200)
        raw = self._raw(0, 3)
        delayed = benchmark._match_production_keys(
            [sample], [*raw, decoded, {"kind": "flush", "input_id": 8,
                                      "time_ns": 1_000_000_100}])
        self.assertEqual(delayed[0]["status"], "painted")
        self.assertEqual(delayed[0]["end_to_flush_ms"], 1000.0)

        duplicate = benchmark._match_production_keys(
            [sample], [*raw, decoded,
                       {"kind": "flush", "input_id": 8, "time_ns": 300},
                       {"kind": "flush", "input_id": 8, "time_ns": 400}])
        self.assertEqual(duplicate[0]["status"], "ambiguous-flush")
        self.assertIsNone(duplicate[0]["end_to_flush_ms"])

        missing = benchmark._match_production_keys([sample], [*raw, decoded])
        self.assertEqual(missing[0]["status"], "decoded-no-flush")

        reversed_time = benchmark._match_production_keys(
            [sample], [*raw, self._decoded(9, "down", sample, 99),
                       {"kind": "flush", "input_id": 9, "time_ns": 300}])
        self.assertEqual(reversed_time[0]["status"], "ambiguous-read")
        self.assertIsNone(reversed_time[0]["end_to_flush_ms"])

    def test_source_offsets_survive_partial_pending_reads_and_ready_bytes(self):
        import curses
        from tools.fleet.render import _CursesInput

        observer_nodes = ast.parse(benchmark._observer_source()).body
        observer_class = next(node for node in observer_nodes
                              if isinstance(node, ast.ClassDef)
                              and node.name == "WindowRefreshObserver")
        install = next(node for node in observer_nodes
                       if isinstance(node, ast.FunctionDef) and node.name == "install")
        source_function = next(node for node in ast.walk(install)
                               if isinstance(node, ast.FunctionDef)
                               and node.name == "source_bytes")
        namespace = {"state": {"reader_bytes": {}}}
        exec(compile(ast.Module(body=[observer_class, source_function], type_ignores=[]),
                     "production-input-observer", "exec"), namespace)
        source_bytes = namespace["source_bytes"]

        class Screen:
            def __init__(self):
                self.values = []
                self.delay = 100

            def feed(self, data):
                self.values.extend(data)

            def getch(self):
                return self.values.pop(0) if self.values else curses.ERR

            def timeout(self, value):
                self.delay = value

        def run_case(data, *, expected_kind=None):
            reader = _CursesInput(curses)
            screen = Screen()
            offset = 0
            event_id = 0

            def observe(value):
                nonlocal offset, event_id
                if 0 <= value <= 255:
                    event_id += 1
                    namespace["state"]["reader_bytes"].setdefault(id(reader), []).append(
                        {"offset": offset, "value": value, "event_id": event_id})
                    offset += 1

            proxy_type = namespace["WindowRefreshObserver"]
            proxy = proxy_type(screen, lambda: None, lambda _ok: None, observe)
            result = None
            source = None
            for part in data:
                screen.feed(part)
                started_at = event_id + 1
                result = reader.read(proxy)
                code = result[0]
                if code not in (curses.ERR, getattr(curses, "KEY_RESIZE", -999999)):
                    kind = {curses.KEY_UP: "up", curses.KEY_DOWN: "down"}.get(code)
                    source = source_bytes(reader, code, kind, started_at)
                    if kind:
                        self.assertEqual(kind, expected_kind)
                        break
            return reader, result, source, proxy

        reader, result, source, proxy = run_case(
            [[27], [ord("[")], [ord("B")]], expected_kind="down")
        self.assertEqual(result[0], curses.KEY_DOWN)
        self.assertEqual(bytes(reader.pending), b"")
        self.assertEqual(source, {"source_byte_start": 0, "source_byte_end": 3,
                                  "source_byte_proven": True})

        reader, result, source, proxy = run_case([[27, ord("x")]])
        self.assertEqual(result[0], 27)
        self.assertEqual(reader.ready, [(ord("x"), None)])
        self.assertEqual(source, {"source_byte_start": 0, "source_byte_end": 1,
                                  "source_byte_proven": True})
        started_at = 1
        next_result = reader.read(proxy)
        next_source = source_bytes(reader, next_result[0], None, started_at)
        self.assertEqual(next_result[0], ord("x"))
        self.assertEqual(next_source, {"source_byte_start": 1, "source_byte_end": 2,
                                      "source_byte_proven": True})

    def test_lower_order_p95_estimator_is_explicit_and_retained(self):
        self.assertEqual(benchmark._percentile(list(range(1, 101)), 0.95), 95)


if __name__ == "__main__":
    unittest.main()
