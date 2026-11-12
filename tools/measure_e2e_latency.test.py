#!/usr/bin/env python3
"""Unit tests for the end-to-end latency tool. They measure nothing and assert no timing.

The measurement command itself (``tools/measure_e2e_latency.py run``) is not a CI step.
What is pinned here: the percentile rule, the fixture extension leaving the default
output untouched, the injectors using the real writers, the pass shim finding a target
in a file run as ``__main__`` (the producer's case), a blind shim failing the run, the
report's top table and the 50 MiB fixture limit.
"""
from __future__ import annotations

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
from e2e_latency import events, fixture as fx, shim_pass, t12  # noqa: E402

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

    def test_route_start_appends_then_replaces_and_leaves_lineage(self):
        before = len(self.jobs_rows())
        stamp = events.route_start(self.fixture, 1)
        rows = self.jobs_rows()
        self.assertEqual(len(rows), before + 1)
        self.assertIn(stamp["marker"], rows[-1])
        self.assertIn("pid_ns=pid:[e2e]", rows[-1])  # the whole-file replacement ran
        lineage = Path(self.fixture["jobs"] + ".lineage.json")
        self.assertTrue(lineage.is_file())
        self.assertGreater(stamp["done_ns"], stamp["t_ns"])

    def test_stage_transition_rewrites_route_file_and_registry_row(self):
        stamp = events.stage_transition(self.fixture, 0)
        route = self.fixture["routes"][0]
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
        route = self.fixture["routes"][1]
        stamp = events.route_end(self.fixture, 1)
        sidecar = Path(route["route_file"]).with_suffix(".outcome.json")
        self.assertEqual(json.loads(sidecar.read_text())["marker"], stamp["marker"])
        last = [row for row in self.jobs_rows() if f"attempt_id={route['attempt_id']}" in row][-1]
        self.assertEqual(last.split("\t")[1], "done")

    def test_worker_tool_appends_a_tool_use_line(self):
        route = self.fixture["routes"][2]
        stamp = events.worker_tool(self.fixture, 2)
        last = json.loads(Path(route["log_file"]).read_text().splitlines()[-1])
        self.assertEqual(last["message"]["content"][0]["name"], stamp["marker"])

    def test_answer_wait_uses_the_real_signal_writer(self):
        stamp = events.answer_wait(self.fixture, 3)
        found = list((Path(self.fixture["env"]["XDG_STATE_HOME"]) / "hearting").rglob("*.json"))
        self.assertTrue(any(stamp["marker"] in path.read_text() for path in found))

    def test_owner_dead_signals_only_the_fixture_owner(self):
        route = self.fixture["routes"][0]
        self.assertTrue(route["owner_pid"])
        events.owner_dead(self.fixture, 0)
        deadline = __import__("time").monotonic() + 5
        while fx._process_start(route["owner_pid"]) == route["owner_pid_start"]:
            try:
                state = Path(f"/proc/{route['owner_pid']}/stat").read_bytes().rsplit(b")", 1)[1].split()[0]
            except OSError:
                break
            if state == b"Z" or __import__("time").monotonic() > deadline:
                break
        self.assertTrue(route["_killed"])


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
        self.assertIn(("human_wait_state", "_refresh_cache"), shim_pass.TARGETS)

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
        self.assertEqual(fleet["failures"], ["render._explorer_compose"])

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

    def test_history_verdicts_follow_the_numbers(self):
        def side(warm, after):
            return {"roles": {"producer": {"warm_median_s": warm, "after_replace_s": after,
                                           "extra_s": after - warm}}}
        flat = {"pairs": [{"label": "A", "small": side(0.40, 0.45), "large": side(0.42, 0.47)}]}
        text = "\n".join(tool.history_verdicts(flat))
        self.assertIn("history 비례는 주원인 아님", text)
        self.assertIn("[다] 먼저", text)
        steep = {"pairs": [{"label": "A", "small": side(0.40, 0.90), "large": side(0.80, 1.40)}]}
        text = "\n".join(tool.history_verdicts(steep))
        self.assertIn("주원인 후보", text)
        self.assertIn("조건 불충족", text)


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
