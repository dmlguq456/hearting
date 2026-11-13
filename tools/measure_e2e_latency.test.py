#!/usr/bin/env python3
"""Unit tests for the end-to-end latency tool. They measure nothing and assert no timing.

The measurement command itself (``tools/measure_e2e_latency.py run``) is not a CI step.
What is pinned here: the percentile rule, the fixture extension leaving the default
output untouched, the injectors using the real writers, the pass shim finding a target
in a file run as ``__main__`` (the producer's case), a blind shim failing the run, the
report's top table and the 50 MiB fixture limit.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "tools"), str(ROOT / "utilities"), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import hook_perf_fixture as hpf  # noqa: E402
import fixture_processes  # noqa: E402
from e2e_latency import events, fixture as fx, shapes, shim_pass, t12, visible  # noqa: E402

_spec = importlib.util.spec_from_file_location("measure_e2e_latency", ROOT / "tools" / "measure_e2e_latency.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

# The registry of ``hook_perf_fixture.build`` at HEAD~ (before the e2e options existed), with the
# constants below and a frozen clock. A default-argument build must stay byte-identical.
DEFAULT_REGISTRY_SHA256 = "ab0df4a58f795b371b6d1e0a30bc41a1fb3f0748f4495673435dec5a8fa1d395"
DEFAULT_FIXTURE_KEYS = ["base", "counts", "dispatch_root", "env", "home", "jobs", "peer_root",
                        "predecessor", "schema", "sessions", "state", "target_session",
                        "target_storage", "terminal_delivery_ids", "transcript", "work", "xdg"]


@contextlib.contextmanager
def small_state(freeze: bool = True):
    """``hook_perf_fixture`` constants shrunk so a build takes a fraction of a second.

    ``freeze`` also pins the clock (for byte comparison); the route-chain ledger sweeps files
    older than 14 days by that clock, so a build that reads it back keeps the real one.
    """
    patches = [mock.patch.object(hpf, name, value) for name, value in (
        ("DEFAULT_LEDGERS", 3), ("DEFAULT_DELIVERIES", 12), ("DEFAULT_PEER_ROWS", 20),
        ("DEFAULT_SNAPSHOTS", 2), ("DEFAULT_STEWARD_MARKERS", 2), ("TRANSCRIPT_ROWS", 5))]
    if freeze:
        patches.append(mock.patch.object(hpf.time, "time", lambda: 1_800_000_000.0))
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        yield


class PercentileTests(unittest.TestCase):
    def test_nearest_rank_and_basis(self):
        stats = t12.summarize([4, 1, 3, 2, 10])
        self.assertEqual((stats["n"], stats["median_s"], stats["p95_s"], stats["max_s"]), (5, 3, 10, 10))
        self.assertEqual(stats["p95_basis"], "max-of-n")
        self.assertEqual(t12.percentile(list(range(1, 101)), 95), 95)
        self.assertEqual(t12.percentile(list(range(1, 21)), 95), 19)
        self.assertNotIn("p95_basis", t12.summarize(list(range(1, 21))))
        self.assertIsNone(t12.summarize([None])["median_s"])


class FixtureExtensionTests(unittest.TestCase):
    def test_default_arguments_leave_the_output_unchanged(self):
        with small_state(), tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            built = hpf.build(tmp + "/b", jobs_rows=40, depth1_rows=8, record_size=700, prime=False)
            digest = hashlib.sha256(Path(built["jobs"]).read_bytes()).hexdigest()
            self.assertEqual(digest, DEFAULT_REGISTRY_SHA256)
            self.assertEqual(sorted(built), DEFAULT_FIXTURE_KEYS)

    def test_e2e_options_build_history_open_routes_and_sessions(self):
        with small_state(freeze=False), tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            built = hpf.build(tmp + "/b", history_rows=90, record_size=2900, open_routes=3, live_sessions=2,
                              worker_streams=True, depth1_rows=6, depth1_open_every=10 ** 9,
                              min_jobs_bytes=0, prime=False)
            rows = Path(built["jobs"]).read_text().splitlines()
            self.assertEqual(len(rows), 90)
            self.assertEqual(len({len(row.encode()) + 1 for row in rows[2 + 6:]}), 1)  # one row size
            open_owner = [row for row in rows if "attempt_id=att-e2e-owner-" in row]
            self.assertEqual(len(open_owner), 3)
            self.assertEqual(len(built["e2e_sessions"]), 2)
            self.assertEqual(len(built["e2e_routes"]), 3)
            for route in built["e2e_routes"]:
                self.assertTrue(Path(route["route_file"]).is_file())
                self.assertTrue(Path(route["log_file"]).is_file())
            route = built["e2e_routes"][0]
            with hpf.applied_env(built):
                from fleet import route_chain
                ledger = Path(route_chain.ledger_path("claude", route["session_id"]))
            self.assertIn(route["route_id"], ledger.read_text())  # written by the real route_chain.append

    def test_no_attempt_other_than_the_open_routes_is_left_open(self):
        """Fleet's required details wait on every open row: a stray open one keeps it loading for good."""
        with small_state(freeze=False), tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            built = hpf.build(tmp + "/b", history_rows=91, record_size=2900, open_routes=3, live_sessions=2,
                              worker_streams=True, depth1_rows=6, depth1_open_every=0,
                              min_jobs_bytes=0, prime=False)          # 91 cuts the last history attempt short
            last: dict[str, str] = {}
            for row in Path(built["jobs"]).read_text().splitlines():
                fields = row.split("\t")
                if len(fields) >= 6:
                    for item in fields[5].split(","):
                        if item.startswith("attempt_id="):
                            last[item[len("attempt_id="):]] = fields[1]
            still_open = sorted(a for a, status in last.items() if status in ("open", "running"))
            self.assertEqual(still_open, [f"att-e2e-owner-{i:05d}" for i in range(3)])

    def test_oversize_registry_is_refused(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            path = Path(tmp) / "jobs.log"
            path.write_bytes(b"x" * 2048)
            self.assertEqual(fx.refuse_oversize(path, limit=4096), 2048)
            with self.assertRaises(ValueError):
                fx.refuse_oversize(path, limit=1024)
            self.assertEqual(fx.MAX_REGISTRY_BYTES, 50 * 1024 * 1024)


class InjectorTests(unittest.TestCase):
    """Each event kind goes through the real writer; nothing here times anything."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="hearting-e2e-unit-", dir="/tmp")
        cls.marker = fx.new_marker()
        cls.addClassCleanup(cls._cleanup)  # registered first: a failing setUpClass still cleans up
        root = Path(cls.tmp) / "fx"
        root.mkdir()
        fx.make_bin(root)
        stub = {"root": str(root), "marker": cls.marker, "jobs": str(root / "state/dispatch/jobs.log")}
        env = fx.child_env(stub)
        pids = fx.spawn_sleepers(cls.marker, 2 + 3, env)
        with small_state(freeze=False):
            base = hpf.build(root, history_rows=60, record_size=2900, open_routes=3, live_sessions=2,
                             worker_streams=True, session_pids=pids, depth1_rows=4,
                             depth1_open_every=10 ** 9, min_jobs_bytes=0, prime=False)
        cls.fixture = {**stub, "jobs": base["jobs"], "env": fx.child_env({**stub, "jobs": base["jobs"]}),
                       "sessions": base["e2e_sessions"], "routes": base["e2e_routes"]}

    @classmethod
    def _cleanup(cls):
        import shutil
        fixture_processes.reap(cls.marker)
        shutil.rmtree(cls.tmp, ignore_errors=True)
        assert not fixture_processes.marked_processes(cls.marker)

    def jobs_rows(self):
        return Path(self.fixture["jobs"]).read_text().splitlines()

    def test_route_start_follows_the_launch_order_and_the_reader_can_find_it(self):
        before = len(self.jobs_rows())
        stamp = events.route_start(self.fixture, 1)
        rows = self.jobs_rows()
        self.assertEqual(len(rows), before + 1)
        attempt = stamp["expect"]["attempt_id"]
        self.assertIn(f"attempt_id={attempt}", rows[-1])
        self.assertIn("pid_ns=pid:[e2e]", rows[-1])  # the whole-file replacement ran
        self.assertRegex(rows[-1], r",pid=\d+,pid_start=\d+,")  # launch identity of a live fixture process
        lineage = Path(self.fixture["jobs"] + ".lineage.json")
        self.assertTrue(lineage.is_file())
        # the session route-chain line, as the original routes got it
        from fleet import route_chain
        with events.applied(self.fixture):
            ledger = Path(route_chain.ledger_path("claude", stamp["expect"]["session_id"]))
        self.assertIn(stamp["expect"]["route_id"], ledger.read_text())
        log = Path(self.fixture["jobs"]).parent / "logs" / f"e2e.{attempt}.claude.jsonl"
        self.assertTrue(log.is_file())

    def test_stage_transition_rewrites_route_file_and_registry_row(self):
        stamp = events.stage_transition(self.fixture, 0)
        route = next(r for r in self.fixture["routes"] if r["route_id"] == stamp["expect"]["route_id"])
        record = json.loads(Path(route["route_file"]).read_text())
        self.assertEqual(record["_fleet_e2e_state"], "test:running")
        # the file moved on, its identity did not: the real reader still accepts it for the row's hash
        from fleet import route as fleet_route
        self.assertIsNotNone(fleet_route.load(route["route_file"], expect_id=route["route_id"],
                                              expect_hash=hpf.e2e_route_hash(route["index"])))
        self.assertTrue(any(f"attempt_id={route['attempt_id']}" in row and "route_node=test" in row
                            for row in self.jobs_rows()))
        self.assertIn(":test:", stamp["marker"])

    def test_every_fixture_route_is_one_the_fleet_reader_accepts(self):
        """A route the reader rejects leaves 'required details' partial and Fleet in loading for good."""
        from fleet import route as fleet_route
        for route in self.fixture["routes"]:
            rows = [r for r in self.jobs_rows() if f"attempt_id={route['attempt_id']}" in r]
            self.assertIn(f"route_hash={hpf.e2e_route_hash(route['index'])}", rows[0])
            self.assertIsNotNone(fleet_route.load(route["route_file"], expect_id=route["route_id"],
                                                  expect_hash=hpf.e2e_route_hash(route["index"])))

    def test_route_end_writes_outcome_sidecar_and_closing_row(self):
        route = events._open_routes(self.fixture)[0]
        before = [row for row in self.jobs_rows() if f"attempt_id={route['attempt_id']}" in row]
        stamp = events.route_end(self.fixture, 1)
        sidecar = Path(route["route_file"]).with_suffix(".outcome.json")
        data = json.loads(sidecar.read_text())
        self.assertEqual(data["marker"], stamp["marker"])
        self.assertTrue(data["closed_at"] and data["terminal_gate_proven"] is True)  # what the reader calls closed
        rows = [row for row in self.jobs_rows() if f"attempt_id={route['attempt_id']}" in row]
        # closed in place by a whole-registry replacement (no extra row), content changed
        self.assertEqual(len(rows), len(before))
        self.assertEqual(rows[-1].split("\t")[1], "done")
        self.assertNotEqual(rows[-1], before[-1])

    def test_worker_tool_appends_a_tool_use_line_at_a_path_the_reader_admits(self):
        stamp = events.worker_tool(self.fixture, 2)
        route = next(r for r in self.fixture["routes"] if r["route_id"] == stamp["expect"]["route_id"])
        last = json.loads(Path(route["log_file"]).read_text().splitlines()[-1])
        self.assertEqual(last["message"]["content"][0]["name"], stamp["marker"])
        from attempt_view import owned_attempt_log_path
        job = types_namespace(_log_file=route["log_file"], attempt_id=route["attempt_id"], harness="claude",
                              _registry_path=self.fixture["jobs"], artifact_root=None, _launch_home=None)
        self.assertEqual(owned_attempt_log_path(job), os.path.realpath(route["log_file"]))

    def test_answer_wait_uses_the_real_signal_writer(self):
        stamp = events.answer_wait(self.fixture, 3)
        found = list((Path(self.fixture["env"]["XDG_STATE_HOME"]) / "hearting").rglob("*.json"))
        self.assertTrue(any(stamp["marker"] in path.read_text() for path in found))

    def test_owner_dead_signals_only_the_fixture_owner(self):
        stamp = events.owner_dead(self.fixture, 0)
        route = next(r for r in self.fixture["routes"] if r["route_id"] == stamp["expect"]["route_id"])
        self.assertTrue(route["owner_pid"])
        deadline = __import__("time").monotonic() + 5
        while fx._process_start(route["owner_pid"]) == route["owner_pid_start"]:
            try:
                state = Path(f"/proc/{route['owner_pid']}/stat").read_bytes().rsplit(b")", 1)[1].split()[0]
            except OSError:
                break
            if state == b"Z" or __import__("time").monotonic() > deadline:
                break
        self.assertTrue(route["_killed"])


    def test_registry_replace_changes_one_line_and_nothing_else(self):
        before = self.jobs_rows()
        stamp = events.registry_replace(self.fixture, 3)
        after = self.jobs_rows()
        self.assertEqual(len(after), len(before))
        changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(changed, [len(before) - 1])
        self.assertEqual(len(after[-1]), len(before[-1]))          # same size: a digest, not a length, tells
        self.assertEqual(stamp["kind"], "registry-replace")

    def test_closing_events_never_take_the_same_route(self):
        fixture = {"routes": [{"route_id": f"rt-{i}", "owner_pid": 100 + i} for i in range(10)]}
        ended, killed = [], []
        for _ in range(4):
            route = events._pick(fixture, "route-end")
            route["_closed"] = True
            ended.append(route["route_id"])
            route = events._pick(fixture, "owner-dead")
            route["_killed"] = True
            killed.append(route["route_id"])
        self.assertFalse(set(ended) & set(killed))
        self.assertEqual(len(set(ended)), 4)
        self.assertEqual(len(set(killed)), 4)


class ShapeTests(unittest.TestCase):
    """The fixture shapes the registry alone does not give."""

    def test_history_carries_the_live_route_owner_blocked_and_dead_owner_shapes(self):
        """Live 2026-11-13: ~88% of the rows carry a route, ~20% are owners, ~1.7% end BLOCKED
        (their routes stay open), ~9% are dead owners; one attempt per row."""
        from workflow_state import WorkflowLedger
        with small_state(freeze=False), tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            built = hpf.build(tmp + "/b", history_rows=1200, record_size=2900, open_routes=3, live_sessions=2,
                              worker_streams=True, depth1_rows=6, depth1_open_every=0, min_jobs_bytes=0,
                              prime=False)
            shape = built["history_shape"]
            rows = Path(built["jobs"]).read_text().splitlines()
            self.assertEqual(len(rows), 1200)
            history = shape["rows"] - hpf.HISTORY_RECENT_24H_ROWS    # the last day's rows stand alone
            self.assertGreater(history, 900)
            self.assertAlmostEqual(shape["routed"] / history, 0.88, delta=0.04)
            self.assertAlmostEqual(shape["owner"] / history, 0.196, delta=0.04)
            self.assertAlmostEqual(shape["blocked"] / history, 0.017, delta=0.008)
            self.assertAlmostEqual(shape["dead_owner"] / history, 0.087, delta=0.035)
            self.assertAlmostEqual(shape["routes"] / history, 0.17, delta=0.03)
            self.assertTrue(shape["real_route_verified"], shape["real_route_reason"])
            self.assertEqual(len(shape["answered_blocked"]), 2)
            attempts = [item.split("attempt_id=")[1].split(",")[0] for item in rows if "attempt_id=att-hist-" in item]
            self.assertEqual(len(attempts), len(set(attempts)))               # one row per attempt
            blocked = [row for row in rows if "note=dead-worker-blocked" in row]
            self.assertEqual(len(blocked), shape["blocked"])
            self.assertEqual(len({len(row.encode()) + 1 for row in rows[2 + 6:]}), 1)  # one row size
            row = blocked[0]
            meta = dict(item.split("=", 1) for item in row.split("\t")[5].split(",") if "=" in item)
            route_file = Path(meta["owner_route_file"])
            self.assertTrue(route_file.is_file())
            route = json.loads(route_file.read_text())
            self.assertEqual(route["route_id"], meta["owner_route_id"])
            ledger = WorkflowLedger(meta["owner_route_id"], meta["owner_route_hash"],
                                    root=Path(built["dispatch_root"]) / "workflow")
            self.assertTrue(ledger.journal_path.is_file())
            self.assertFalse(route_file.with_name(route_file.stem + ".outcome.json").exists())  # a BLOCKED route stays open
            closed = [path for path in (Path(built["base"]) / "routes" / "hist").glob("*.outcome.json")]
            self.assertGreater(len(closed), shape["routes"] // 2)

    def test_stall_notices_are_written_for_open_attempts(self):
        with small_state(freeze=False), tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            built = hpf.build(tmp + "/b", history_rows=300, record_size=2900, open_routes=3, live_sessions=2,
                              worker_streams=True, depth1_rows=6, depth1_open_every=0, min_jobs_bytes=0,
                              prime=False)
            stall = built["history_shape"]["stall"]
            self.assertEqual(stall["written"], 3, stall["reason"])
            state = Path(built["dispatch_root"])
            for attempt in stall["attempts"]:
                self.assertTrue((state / "watchdog" / f"{attempt}.json").is_file())

    def test_d1_lines_judge_the_whole_pass_time_and_report_the_floor_separately(self):
        def side(warm, after, view, rebuild):
            return {"roles": {"producer": {
                "warm_median_s": warm, "after_replace_s": after, "extra_s": after - warm,
                "after_row": {"metrics": {"full_metadata_parses": 1}, "segments": {
                    tool._VIEW_SEGMENT: {"n": 1, "total_s": view},
                    tool._REBUILD_SEGMENT: {"n": 9, "total_s": rebuild}}}}}}
        # the unchanged pass is flat; the pass after a replacement grows 30%, all of it in the floor
        scale = {"pairs": [{"label": "A", "small": side(0.40, 0.60, 0.20, 0.02), "large": side(0.41, 0.78, 0.38, 0.02)}]}
        rows = tool.d1_rows(scale)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertFalse(row["met"])                                         # 0.78 / 0.60 = x1.30 is over x1.20
        self.assertAlmostEqual(row["after_ratio"], 1.3)
        self.assertAlmostEqual(row["floor_small_s"], 0.18)
        self.assertAlmostEqual(row["after_without_floor_ratio"], (0.78 - 0.36) / (0.60 - 0.18))
        text = "\n".join(tool.d1_lines(scale))
        self.assertIn("0/1 칸 충족", text)
        self.assertIn("미충족", text)
        self.assertIn("바닥(읽기·digest)", text)
        flat = {"pairs": [{"label": "A", "small": side(0.40, 0.60, 0.20, 0.02), "large": side(0.41, 0.62, 0.21, 0.02)}]}
        self.assertTrue(tool.d1_rows(flat)[0]["met"])
        self.assertIn("1/1 칸 충족", "\n".join(tool.history_verdicts(flat)))

    def test_recent_history_rows_fall_in_the_fleet_windows(self):
        total = 400
        ages = [hpf._history_age_s(index, total) for index in range(total)]
        self.assertEqual(sum(1 for a in ages if a < 6 * 3600), hpf.HISTORY_RECENT_6H_ROWS)
        self.assertEqual(sum(1 for a in ages if a < 24 * 3600), hpf.HISTORY_RECENT_24H_ROWS)
        self.assertTrue(any(a < 15 * 60 for a in ages))                       # an afterglow-window row
        self.assertTrue(any(361 * 60 < a < 24 * 3600 for a in ages))          # past the stall window, within a day
        self.assertEqual(ages, sorted(ages, reverse=True))                    # rows stay in time order
        self.assertGreater(min(a for a in ages[:-hpf.HISTORY_RECENT_24H_ROWS]), 24 * 3600)

    def test_codex_rollout_is_one_the_lifecycle_scan_reads_to_a_finished_task(self):
        from fleet.collectors import codex
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            facts = shapes.write_codex_rollout(Path(tmp) / "rollout-x.jsonl", cwd="/w", session_id="s", megabytes=0.3)
            self.assertLess(facts["bytes"], shapes.MAX_CODEX_LOG_BYTES)
            cursor = codex._initialize_lifecycle_cursor(facts["path"])
            self.assertEqual(cursor.lifecycle[0], "task_complete")
            self.assertEqual(cursor.offset, facts["bytes"])                       # every byte consumed
            self.assertEqual(codex._rollout_meta(facts["path"]).get("cwd"), "/w")

    def test_claude_transcript_has_finished_and_active_subagents(self):
        from fleet.collectors import claude
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            facts = shapes.write_claude_transcript(Path(tmp) / "t.jsonl", session_id="s", megabytes=1)
            subs = claude._tail_subagents(facts["path"])
            self.assertEqual(len(subs), facts["agent_calls"])
            self.assertTrue(any(sub.active for sub in subs) and any(not sub.active for sub in subs))


class ShimTests(unittest.TestCase):
    def run_script(self, body: str, *, mode=None, roots=None, targets=None):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            shim_dir = shim_pass.install(Path(tmp) / "shim")
            out = Path(tmp) / "out.jsonl"
            script = Path(tmp) / "human_wait_state.py"
            script.write_text(body, encoding="utf-8")
            (Path(tmp) / "helper_mod.py").write_text("VALUE = 1\n", encoding="utf-8")
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **shim_pass.shim_env(
                out=str(out), shim_dir=shim_dir, role="producer", mode=mode, roots=roots, targets=targets)}
            done = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            return shim_pass.read_passes(out), shim_pass.shim_mode(out)

    def test_shim_source_compiles(self):
        compile(shim_pass.render(), "sitecustomize.py", "exec")
        # The Fleet keeps its compiled row selection only while procscan.read_proc_start is the
        # native reader; a shim that wrapped it measured a path an operating Fleet never takes.
        self.assertNotIn("module.read_proc_start =", shim_pass.render())
        self.assertIn(("human_wait_state", "_refresh_cache"), shim_pass.TARGETS)
        # the segments the holder memo (speed-e2e leg C) is judged by
        self.assertIn(("dispatch", "_prime_route_display_rows"), shim_pass.TARGETS)
        self.assertIn(("dispatch_seat_handover", "job_holders"), shim_pass.TARGETS)

    def test_script_run_as_main_is_recorded_by_code_object(self):
        # The producer's case: the file is ``__main__`` and a nested function in it is a target.
        body = ("import time\n"
                "def _route_demands():\n    time.sleep(0.01)\n    return {}\n"
                "def _refresh_cache():\n    _route_demands()\n    return True\n"
                "def _loop():\n    _refresh_cache()\n    _refresh_cache()\n"
                "if __name__ == '__main__':\n    _loop()\n")
        passes, mode = self.run_script(body)
        self.assertEqual(mode, "monitoring" if hasattr(sys, "monitoring") else "import-hook")
        if mode == "monitoring":
            self.assertEqual([p["root"] for p in passes], ["human_wait_state._refresh_cache"] * 2)
            self.assertEqual(passes[0]["seg"]["human_wait_state._route_demands"]["n"], 1)
            self.assertGreater(passes[0]["t1"], passes[0]["t0"])
        check = shim_pass.self_check(passes, "producer")
        if mode == "monitoring":
            self.assertEqual(check["failures"], [])
            self.assertIn("steward.read_relations", check["absent"])  # absent target: not a failure

    def test_import_hook_fallback_records_a_script_main_after_its_first_import(self):
        body = ("def _refresh_cache():\n    import helper_mod\n    return helper_mod.VALUE\n"
                "def _loop():\n    _refresh_cache()\n    _refresh_cache()\n    _refresh_cache()\n"
                "if __name__ == '__main__':\n    _loop()\n")
        passes, mode = self.run_script(body, mode="import-hook")
        self.assertEqual(mode, "import-hook")
        # the first call wrapped nothing yet; later calls are recorded
        self.assertGreaterEqual(len(passes), 1)
        self.assertEqual({p["root"] for p in passes}, {"human_wait_state._refresh_cache"})

    def test_blind_shim_is_a_failure_not_a_number(self):
        check = shim_pass.self_check([], "producer")
        self.assertEqual(check["failures"], ["human_wait_state._refresh_cache"])
        self.assertFalse(check["ok"])
        fleet = shim_pass.self_check([{"root": "__init__.collect_all", "seg": {}}], "fleet")
        self.assertEqual(fleet["failures"], ["render._explorer_compose", "render._draw"])

    def test_orchestrator_exits_one_and_publishes_nothing_when_blind(self):
        fake_fixture = types_namespace(build=lambda **kw: {
            "root": "/tmp/none", "marker": "m", "bytes": 1, "rows": 1, "open_routes": 0,
            "baseline": {}, "sessions": [], "routes": []},
            cleanup=lambda fixture, keep_files=False: {"scratch_removed": True, "leftover_processes": 0})
        blind = {"metric": "T12", "results": [{"metric": "T1", "events": {"x": {"n": 1}}, "segments": {}}],
                 "passes": [], "events": [], "notes": [], "not_measured": [], "shim_mode": "monitoring",
                 "self_check": {"failures": ["human_wait_state._refresh_cache"], "checks": {}}}
        fake_t12 = types_namespace(measure=lambda *a, **k: blind)
        modules = {"fixture": fake_fixture, "t12": fake_t12}
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp, \
                mock.patch.object(tool, "_module", side_effect=lambda name: modules.get(name)), \
                mock.patch.object(Path, "mkdir"):
            out = Path(tmp) / "r.json"
            code = tool.main(["run", "--only", "t1", "--reps", "1", "--json", str(out)])
            report = json.loads(out.read_text())
        self.assertEqual(code, 1)
        self.assertEqual(report["results"], [])
        self.assertIn("self-check failed", json.dumps(report["not_measured"]))


def types_namespace(**kw):
    import types
    return types.SimpleNamespace(**kw)


class ReportTests(unittest.TestCase):
    def sample(self):
        events_ = {"route-start": {"n": 3, "median_s": 1.5, "p95_s": 2.1, "max_s": 2.1, "p95_basis": "max-of-n"}}
        passes = [
            {"role": "producer", "index": 1, "kind": "cold-1", "dur_s": 2.0, "segments": {}, "metrics": {}, "open_calls": 5},
            {"role": "producer", "index": 2, "kind": "warm-unchanged", "dur_s": 0.4, "segments": {}, "metrics": {}, "open_calls": 5},
            {"role": "producer", "index": 3, "kind": "after-route-start", "dur_s": 0.9,
             "segments": {"dispatch.collect": {"n": 1, "total_s": 0.3, "max_s": 0.3}},
             "metrics": {"full_metadata_parses": 1}, "open_calls": 6},
        ]
        return {"results": [{"metric": "T1", "events": events_, "segments": {}}], "passes": passes,
                "replace_contrast": tool.replace_contrast(passes), "fixture": {"bytes": 1, "rows": 2},
                "cleanup": {"scratch_removed": True, "leftover_processes": 0}, "elapsed_s": 1.0,
                "load_before": [1.0], "load_after": [1.0], "notes": [], "not_measured": []}

    def test_top_of_the_report_is_the_baseline_table(self):
        text = tool.render_markdown(self.sample())
        lines = text.splitlines()
        self.assertEqual(lines[2], "## 기준 T1~T4")
        self.assertIn("| T1 | route-start | 3 | 1.500 | 2.100* | 2.100 | 1 s / 2 s |", text)
        self.assertIn("| T3 | (측정 안 됨)", text)
        self.assertLess(text.index("기준 T1~T4"), text.index("warm·cold pass"))
        contrast = self.sample()["replace_contrast"]["producer"]
        self.assertAlmostEqual(contrast["extra_s"], 0.5)
        self.assertGreaterEqual(contrast["extra_share"], 0.2)

    def test_mixed_passes_with_and_without_kind_still_give_the_top_table(self):
        report = self.sample()
        t3_pass = {"condition": "cold-empty", "rep": 0, "first_byte_s": 0.2, "first_nonloading_s": 3.1,
                   "first_observed_s": 3.1, "details_complete_s": 3.1, "paints": 60, "persisted": True}
        t4_pass = {"event": "prompt", "harness": "claude"}
        report["passes"] = report["passes"] + [t3_pass, t4_pass]
        report["results"].append({"metric": "T3", "events": {
            "cold-empty": {"n": 1, "median_s": 3.1, "p95_s": 3.1, "max_s": 3.1, "p95_basis": "max-of-n"},
            "second-launch": {"n": 0, "median_s": None, "p95_s": None, "max_s": None,
                              "no_observed_within_s": 60.0}}, "segments": {}})
        text = tool.render_markdown(report)
        self.assertEqual(text.splitlines()[2], "## 기준 T1~T4")
        self.assertIn("| T1 | route-start |", text)
        self.assertIn("| T3 | cold-empty | 1 | 3.100 |", text)
        self.assertIn("loading을 60s 안에 벗어나지 못함", text)
        self.assertIn("| T4 | (측정 안 됨)", text)
        self.assertIn("| cold-empty | 0 | 0.200 |", text)               # the T3 launch table
        self.assertNotIn("표를 그리지 못함", text)

    def test_orchestrator_tags_a_kind_on_t3_and_t4_passes(self):
        passes = tool.tag_kinds("T3", [{"condition": "second-launch", "rep": 1}])
        passes += tool.tag_kinds("T4", [{"event": "tool"}, {"x": 1}, {"kind": "keep"}])
        self.assertEqual([p["kind"] for p in passes], ["t3-second-launch", "t4-tool", "t4-pass", "keep"])
        self.assertIsNone(tool.replace_contrast(passes))                  # no role: not a producer/Fleet pass

    def test_one_section_failing_does_not_erase_the_others(self):
        report = self.sample()
        report["passes"] = [{"role": "producer", "kind": "cold-1", "dur_s": 1.0, "segments": "broken"}]
        report["replace_contrast"] = {"producer": {}}                     # KeyError in its section
        text = tool.render_markdown(report)
        self.assertIn("| T1 | route-start |", text)
        self.assertIn("표를 그리지 못함", text)
        self.assertIn("## 실행 정보", text)

    def test_history_verdicts_read_the_pass_right_after_the_change(self):
        def side(warm, after, parses=1):
            return {"roles": {"producer": {
                "warm_median_s": warm, "after_replace_s": after, "extra_s": after - warm,
                "after_row": {"metrics": {"full_metadata_parses": parses}}}}}
        # unchanged passes grow 5 %, but what a change adds doubles: not the unchanged pass decides
        steep = {"pairs": [{"label": "A", "small": side(0.40, 0.70), "large": side(0.42, 1.30)}]}
        text = "\n".join(tool.history_verdicts(steep))
        self.assertIn("주원인 후보", text)
        self.assertIn("(참고, 판정 아님)", text)
        self.assertIn("full parses 1/1", text)
        flat = {"pairs": [{"label": "A", "small": side(0.40, 0.46), "large": side(0.42, 0.49)}]}
        text = "\n".join(tool.history_verdicts(flat))
        self.assertIn("history 비례는 주원인 아님", text)
        self.assertIn("[다] 먼저", text)

    def test_history_verdict_needs_a_reparse_on_both_sides(self):
        def side(after, parses):
            return {"roles": {"producer": {
                "warm_median_s": 0.4, "after_replace_s": after, "extra_s": after - 0.4,
                "after_row": {"metrics": {"full_metadata_parses": parses}}}}}
        # the large side's pass answered from a digest cache (no parse): no verdict, however the times look
        pairs = {"pairs": [{"label": "A", "small": side(0.9, 1), "large": side(0.45, 0)}]}
        text = "\n".join(tool.history_verdicts(pairs))
        self.assertIn("history 비례 판정 불가", text)
        self.assertIn("판정 불가 (full parses 1/0", text)
        self.assertNotIn("주원인 후보", text)

    def test_history_verdict_says_to_read_the_extra_numbers_when_every_cell_is_unjudgeable(self):
        # follow-up 4b: an incremental registry path re-parses nothing, so every cell reads "판정 불가"
        def side(after):
            return {"roles": {"producer": {
                "warm_median_s": 0.4, "after_replace_s": after, "extra_s": after - 0.4,
                "after_row": {"metrics": {"full_metadata_parses": 0}}}}}
        text = "\n".join(tool.history_verdicts({"pairs": [{"label": "A", "small": side(0.5), "large": side(0.55)}]}))
        self.assertIn("history 비례 판정 불가", text)
        self.assertIn("추가분 숫자", text)
        self.assertIn("d1의 전체 시간", text)

    def test_report_names_the_git_head_of_the_measured_code(self):
        # follow-up 6: a tree that is a repository gives its head; one that is not says so
        head = tool.code_head(ROOT)
        self.assertTrue(head["git"])
        self.assertRegex(head["head"], r"^[0-9a-f]{40}$")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            outside = tool.code_head(Path(tmp))
        self.assertFalse(outside["git"])
        self.assertIn("git 저장소가 아니다", outside["note"])
        report = self.sample()
        report["code"] = head
        self.assertIn(head["head"], tool.render_markdown(report))
        report["code"] = outside
        self.assertIn("git 아님", tool.render_markdown(report))

    def test_report_prints_the_empty_frames_of_the_drawn_model(self):
        report = self.sample()
        report["drawn_detail"] = {"frames": 10, "with_detail": 6, "empty": 4, "empty_runs": 2,
                                  "empty_s": 2.5, "longest_empty_s": 1.5, "span_s": 12.0}
        text = tool.render_markdown(report)
        self.assertIn("그려진 화면 모델의 route 세부", text)
        self.assertIn("빈 것 **4개**", text)

    def test_table_says_how_many_events_were_seen(self):
        report = self.sample()
        report["results"][0]["events"]["route-start"].update(n=2, n_total=3)
        text = tool.render_markdown(report)
        self.assertIn("| T1 | route-start | 2/3 |", text)


class VisibleTests(unittest.TestCase):
    """An event arrives only when the reader's output holds the value it changed."""

    EXPECT = {"session_id": "S1", "route_id": "rt-1", "attempt_id": "att-1"}

    def projection(self, **row):
        base = {"id": "rt-1", "state": "working", "worker_stage": "execute", "outcome": "",
                "owner_attempt": "att-1", "stages": [{"name": "execute", "st": "active"}], "tool": None}
        base.update(row)
        return {"route_panels": {"claude\0S1": [base]}, "items": []}

    def ev(self, kind, **expect):
        return {"kind": kind, "expect": {**self.EXPECT, **expect}}

    def test_a_publish_without_the_value_is_not_an_arrival(self):
        # the same publish passes every check that only compared observed_at with the write time
        shown = self.projection()
        self.assertFalse(visible.t1(self.ev("worker-tool", tool="E2eTool0004"), shown))
        self.assertFalse(visible.t1(self.ev("route-end"), shown))
        self.assertFalse(visible.t1(self.ev("stage-transition", stage="test"), shown))
        self.assertFalse(visible.t1(self.ev("route-start", attempt_id="att-2", route_id="rt-2"), shown))
        self.assertFalse(visible.t1(self.ev("owner-dead"), shown))

    def test_each_kind_arrives_on_its_own_value(self):
        self.assertTrue(visible.t1(self.ev("worker-tool", tool="E2eTool0004"), self.projection(tool="E2eTool0004")))
        self.assertTrue(visible.t1(self.ev("route-end"), self.projection(outcome="success")))
        self.assertTrue(visible.t1(self.ev("stage-transition", stage="test"), self.projection(
            worker_stage="test", stages=[{"name": "test", "st": "active"}])))
        self.assertTrue(visible.t1(self.ev("route-start"), self.projection()))
        self.assertTrue(visible.t1(self.ev("owner-dead"), self.projection(state="idle")))
        self.assertTrue(visible.t1(self.ev("answer-wait", item_id="S4:x"), {"items": [{"id": "S4:x"}]}))

    def test_gone_counts_only_for_a_route_that_was_shown(self):
        gone = {"route_panels": {"claude\0S1": []}, "items": []}
        self.assertFalse(visible.t1(self.ev("owner-dead", was_present=False), gone))
        self.assertTrue(visible.t1(self.ev("owner-dead", was_present=True), gone))
        self.assertTrue(visible.t1(self.ev("route-end", was_present=True), gone))

    def test_fleet_model_digest_follows_the_same_rules(self):
        digest = {"routes": {"rt-1": {"state": "working", "outcome": None, "active": ["execute"],
                                      "tool": None, "attempt": "att-1"}}, "questions": ["S4:x"]}
        self.assertTrue(visible.t2(self.ev("route-start"), digest))
        self.assertFalse(visible.t2(self.ev("stage-transition", stage="test"), digest))
        self.assertFalse(visible.t2(self.ev("worker-tool", tool="T"), digest))
        self.assertFalse(visible.t2(self.ev("route-end"), digest))
        self.assertTrue(visible.t2(self.ev("answer-wait", item_id="S4:x"), digest))
        self.assertFalse(visible.t2(self.ev("answer-wait", item_id="S4:y"), digest))
        digest["routes"]["rt-1"].update(state="idle")
        self.assertTrue(visible.t2(self.ev("owner-dead"), digest))
        self.assertFalse(visible.t2(self.ev("owner-dead"), None))


class DrawnModelTests(unittest.TestCase):
    """Follow-up 2 and the flicker check: T2 follows the model ``_draw`` drew, not the one composed."""

    ROUTE_WITH = {"a": {"state": "working", "attempt": "att-1", "active": ["execute"]}}
    ROUTE_EMPTY = {"a": {"state": "working", "attempt": None, "active": []}}

    def draws(self, models, step=1_000_000_000):
        return [{"root": "render._draw", "t0": i * step, "t1": i * step + 10, "seg": {},
                 **({"model": {"routes": m, "questions": []}} if m is not None else {})}
                for i, m in enumerate(models)]

    def test_shim_takes_the_digest_after_draw_not_after_compose(self):
        source = shim_pass.render()
        self.assertIn('if name == "render._draw":', source)
        self.assertNotIn('if name == "render._explorer_compose":\n                # After t1', source)
        self.assertIn(("render", "_draw"), shim_pass.FLEET_ROOTS)

    def test_arrival_is_a_draw_whose_model_shows_the_value(self):
        ev = {"kind": "route-start", "t_ns": 5, "expect": {"route_id": "a", "attempt_id": "att-1"}}
        rows = shim_pass.with_models(self.draws([self.ROUTE_EMPTY, self.ROUTE_EMPTY, self.ROUTE_WITH]))
        arrival = t12._fleet_arrival(rows, ev)
        self.assertIs(arrival, rows[2])
        self.assertIsNone(t12._fleet_arrival(rows[:2], ev))

    def test_empty_frames_count_only_after_detail_was_shown(self):
        # before the first frame with detail nothing is "gone"; afterwards an empty one is a blank
        models = [self.ROUTE_EMPTY, self.ROUTE_EMPTY, self.ROUTE_WITH, self.ROUTE_WITH, self.ROUTE_EMPTY,
                  self.ROUTE_EMPTY, self.ROUTE_WITH, None, self.ROUTE_EMPTY, self.ROUTE_WITH]
        rows = self.draws(models)
        for index in (1, 3, 5, 7):          # the digest is written only when it changed
            rows[index].pop("model", None)
        out = t12.drawn_detail(rows)
        self.assertEqual(out["frames"], 8)        # from the first detail frame on, frames that have routes
        self.assertEqual(out["with_detail"], 5)
        self.assertEqual(out["empty"], 3)
        self.assertEqual(out["empty_runs"], 2)
        self.assertEqual(out["longest_empty_s"], 2.0)

    def test_a_steady_screen_has_no_empty_frame(self):
        out = t12.drawn_detail(self.draws([self.ROUTE_WITH, None, None, self.ROUTE_WITH]))
        self.assertEqual((out["empty"], out["empty_runs"], out["longest_empty_s"]), (0, 0, 0.0))
        self.assertEqual(t12.drawn_detail([]), {**t12.drawn_detail([])})
        self.assertEqual(t12.drawn_detail([])["frames"], 0)


class SourceShapeTests(unittest.TestCase):
    """Follow-up 5: a source line that changed shape must not silently restore the default."""

    def setUp(self):
        t12.SOURCE_SHAPE_WARNINGS.clear()
        self.addCleanup(t12.SOURCE_SHAPE_WARNINGS.clear)

    def test_current_source_is_read_without_a_warning(self):
        self.assertGreater(t12.registry_min_gap(), 0)
        self.assertGreater(t12.producer_cadence_s(), 0)
        self.assertEqual(t12.SOURCE_SHAPE_WARNINGS, [])

    def test_unreadable_shape_returns_the_default_and_leaves_a_warning(self):
        with mock.patch.object(Path, "read_text", return_value="_REGISTRY_MIN_GAP_RENAMED = 5\n"):
            self.assertEqual(t12.registry_min_gap(), 2.0)
            self.assertEqual(t12.producer_cadence_s(), 30.0)
        text = "\n".join(t12.SOURCE_SHAPE_WARNINGS)
        self.assertIn("registry_min_gap", text)
        self.assertIn("producer_cadence_s", text)
        self.assertIn("WARNING", text)
        with mock.patch.object(Path, "read_text", return_value="_REGISTRY_MIN_GAP_RENAMED = 5\n"):
            t12.registry_min_gap()
        self.assertEqual(len(t12.SOURCE_SHAPE_WARNINGS), 2)             # the same warning is not repeated

    def test_the_warning_reaches_the_report_notes(self):
        with mock.patch.object(Path, "read_text", return_value=""):
            t12.registry_min_gap()
        report = t12._assemble({"sessions": []}, [], [], [], [], 2.0, 0.025, [], [], {}, "/nonexistent/producer.jsonl")
        self.assertTrue(any("registry_min_gap" in note for note in report["notes"]))
        self.assertIn("drawn_detail", report)


class UnpublishedEventTests(unittest.TestCase):
    """Follow-up 8: an event that was detected but whose publishing pass cannot be found says why."""

    def test_detect_without_a_publisher_pass_is_listed_with_its_reason(self):
        ev = {"kind": "route-start", "seq": 1, "t_ns": 1_000, "t_start_ns": 900, "detect_ns": 5_000,
              "projection_s": 0.01, "expect": {"route_id": "a", "attempt_id": "att-1"}}
        late_pass = {"root": "human_wait_state._refresh_cache", "index": 1, "t0": 9_000_000_000,
                     "t1": 9_100_000_000, "seg": {}}
        report = t12._assemble({"sessions": []}, ["route-start"], [ev], [late_pass], [], 2.0, 0.025, [], [], {}, "/nonexistent/producer.jsonl")
        reasons = [item["reason"] for item in report["not_measured"] if "route-start#1 T1" in item["what"]]
        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons[0].startswith("no_publisher_pass"))
        self.assertNotIn("not_visible", reasons[0])


class ReplaceSamplesTests(unittest.TestCase):
    """The pass right after a whole-registry replacement is a median of several, not one noisy pass."""

    def write_log(self, path, root, spans):
        with open(path, "w", encoding="utf-8") as handle:
            for index, (t0, t1) in enumerate(spans, 1):
                handle.write(json.dumps({"root": root, "index": index, "t0": int(t0 * 1e9), "t1": int(t1 * 1e9),
                                         "seg": {}, "metrics": {}}) + "\n")

    def test_each_replacement_is_sampled_and_the_median_is_reported(self):
        warm = [(2, 3), (4, 5), (6, 7), (7.5, 8.5)]
        after = [(9, 10), (11, 11.5), (15, 17), (18, 19), (21, 24), (25, 26)]    # 1 s, settle, 2 s, settle, 3 s, settle
        times = iter([int(8.8e9), int(14e9), int(20e9)])
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            producer, fleet = Path(tmp) / "producer.jsonl", Path(tmp) / "fleet.jsonl"
            self.write_log(producer, "human_wait_state._refresh_cache", [(0, 1)] + warm + after)
            self.write_log(fleet, "__init__.collect_all", [(0, 1)] + warm + after)

            class FakeChildren:
                def __init__(self, *args, **kwargs):
                    self.producer_log, self.fleet_log = producer, fleet

                def start_producer(self):
                    pass

                def start_fleet(self):
                    pass

                def close(self):
                    pass

            with mock.patch.object(t12, "Children", FakeChildren), \
                    mock.patch.object(t12.events, "registry_replace",
                                      side_effect=lambda fixture, n: {"t_ns": next(times)}), \
                    mock.patch.object(t12.time, "sleep", lambda _s: None):
                out = t12.measure_replace({"rows": 1, "bytes": 2}, scratch=Path(tmp), warm_passes=3)
        self.assertEqual(out["replacements"], 3)
        for role in ("producer", "fleet"):
            entry = out["roles"][role]
            self.assertEqual(entry["after_samples_s"], [1.0, 2.0, 3.0])
            self.assertEqual(entry["after_replace_s"], 2.0)
            self.assertEqual(entry["warm_median_s"], 1.0)
            self.assertEqual(entry["extra_s"], 1.0)
            self.assertEqual(entry["after_row"]["dur_s"], 2.0)       # the row shown is the median pass itself


class PhasePlanTests(unittest.TestCase):
    def test_every_kind_covers_the_whole_cadence_and_the_plan_reproduces(self):
        kinds = ("worker-tool", "answer-wait", "owner-dead")
        plan = t12.slow_phase_plan(kinds, 8, 30.0)
        self.assertEqual(plan, t12.slow_phase_plan(kinds, 8, 30.0))            # fixed seed
        self.assertNotEqual(plan, t12.slow_phase_plan(kinds, 8, 30.0, seed=1))
        for kind in kinds:
            slices = sorted(int(plan[(rep, kind)] // (30.0 / 8)) for rep in range(8))
            self.assertEqual(slices, list(range(8)))                           # one sample in each eighth
        self.assertTrue(all(0.0 <= v < 30.0 for v in plan.values()))

    def test_kinds_do_not_share_one_phase_schedule(self):
        plan = t12.slow_phase_plan(("worker-tool", "answer-wait", "owner-dead"), 8, 30.0)
        orders = [[int(plan[(rep, k)] // (30.0 / 8)) for rep in range(8)]
                  for k in ("worker-tool", "answer-wait", "owner-dead")]
        self.assertEqual(len({tuple(o) for o in orders}), 3)

    def test_model_digest_carries_forward_between_changes(self):
        rows = [{"root": "render._explorer_compose", "t0": 1, "t1": 2, "seg": {}, "model": {"routes": {"a": {}}}},
                {"root": "render._explorer_compose", "t0": 3, "t1": 4, "seg": {}},
                {"root": "render._explorer_compose", "t0": 5, "t1": 6, "seg": {}, "model": {"routes": {}}}]
        now = [r["model_now"] for r in shim_pass.with_models(rows)]
        self.assertEqual(now, [{"routes": {"a": {}}}, {"routes": {"a": {}}}, {"routes": {}}])


class PublishWatchTests(unittest.TestCase):
    def test_only_a_publish_that_holds_the_value_stamps_the_event(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            cache = Path(tmp) / "projection.json"
            cache.write_text(json.dumps({"observed_at": 1.0, "items": []}))
            watch = t12.PublishWatch(cache)
            ev = {"kind": "answer-wait", "wall": 0.0, "expect": {"session_id": "S1", "item_id": "S4:x"}}
            watch.add(ev)
            self.assertNotIn("detect_ns", ev)
            cache.write_text(json.dumps({"observed_at": 2.0, "items": []}))     # a later publish, still without it
            os.utime(cache, ns=(2, 2))
            watch.poll()
            self.assertNotIn("detect_ns", ev)
            self.assertIsNotNone(watch.last_publish_ns)
            cache.write_text(json.dumps({"observed_at": 3.0, "items": [{"id": "S4:x"}]}))
            os.utime(cache, ns=(3, 3))
            watch.poll()
            self.assertIn("detect_ns", ev)
            self.assertEqual(watch.pending, [])


class PathsTests(unittest.TestCase):
    def test_measurement_modules_are_not_regression_suites(self):
        for name in ("t12.py", "fixture.py", "events.py", "shim_pass.py"):
            self.assertFalse(name.endswith(".test.py") or name.startswith("test_"))

    def test_isolated_child_env_has_no_runtime_variables_and_a_closed_path(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            stub = {"root": tmp, "marker": "hearting-e2e-x", "jobs": tmp + "/jobs.log"}
            with mock.patch.dict(os.environ, {"HERDR_SOCKET": "x", "TMUX": "y", "CODEX_HOME": "/real"}):
                env = fx.child_env(stub)
            self.assertNotIn("HERDR_SOCKET", env)
            self.assertNotIn("TMUX", env)
            self.assertEqual(env["PATH"], tmp + "/bin")
            self.assertEqual(env["HOME"], tmp + "/home")
            self.assertEqual(env[fx.MARKER_ENV], "hearting-e2e-x")


if __name__ == "__main__":
    unittest.main()
