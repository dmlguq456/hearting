"""GPU arbitration tests: fake devices, real concurrent CPU-only payloads."""
import concurrent.futures
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gpu_leases as G

SOURCE = Path(G.__file__).read_text()
OBS = {"reachable": True, "gpus": [
    {"index": 0, "uuid": "GPU-zero", "free_mib": 24000, "utilization_gpu_pct": 0, "processes": []},
    {"index": 1, "uuid": "GPU-one", "free_mib": 23000, "utilization_gpu_pct": 0, "processes": []}]}


class LeasesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "dispatch" / "gpu-leases.json"

    def acquire(self, **kwargs):
        return G.acquire(self.state, OBS, **kwargs)

    def test_selection_and_explicit_conflict_gives_owner_task_time_and_free_gpu(self):
        lease = self.acquire(owner={"label": "SR_CorrNet [cd]"}, task="train", run_id="one")
        self.assertEqual(lease["gpus"], ["0"])
        with self.assertRaisesRegex(G.GPUUnavailable, r"SR_CorrNet \[cd\].*train.*free GPUs: gpu1"):
            self.acquire(requested="0")
        second = self.acquire()
        self.assertEqual(second["gpus"], ["1"])
        with self.assertRaisesRegex(G.GPUUnavailable, "free GPUs: none"):
            self.acquire()

    def test_idle_memory_holding_process_is_occupied_with_or_without_owner(self):
        for owner in ({"label": "TF [33]"}, None):
            observation = json.loads(json.dumps(OBS))
            observation["gpus"][0]["processes"] = [{"pid": 9, "used_memory_mib": 7700,
                                                      "owner": owner, "command": "idle train"}]
            lease = G.acquire(self.state, observation)
            self.assertEqual(lease["gpus"], ["1"])
            G.release(self.state, lease)
            with self.assertRaisesRegex(G.GPUUnavailable, "idle train"):
                G.acquire(self.state, observation, requested="0")

    def test_share_still_records_a_lease_and_default_remains_exclusive(self):
        first = self.acquire(requested="0")
        second = self.acquire(requested="0", share=True)
        self.assertEqual(len(G.snapshot(self.state)), 2)
        G.release(self.state, first)
        with self.assertRaises(G.GPUUnavailable):
            self.acquire(requested="0")
        G.release(self.state, second)
        self.assertEqual(self.acquire(requested="0")["gpus"], ["0"])

    def test_dead_or_reused_pid_is_pruned_immediately_but_unknown_is_retained(self):
        lease = self.acquire(requested="0")
        with G.locked(self.state) as data:
            data["leases"][lease["token"]]["starttime"] = "0"
        self.assertEqual(self.acquire(requested="0")["gpus"], ["0"])
        with G.locked(self.state) as data:
            row = next(iter(data["leases"].values()))
            row["pid_namespace"] = "unknown namespace"
        with self.assertRaises(G.GPUUnavailable):
            self.acquire(requested="0")

    def test_multi_gpu_admission_is_all_or_nothing_and_uuid_is_canonicalized(self):
        self.acquire(requested="1")
        with self.assertRaises(G.GPUUnavailable):
            self.acquire(requested="0,1")
        self.assertEqual(len(G.snapshot(self.state)), 1)
        self.assertEqual(self.acquire(requested="GPU-zero")["gpus"], ["0"])

    def test_snapshot_does_not_create_or_rewrite_remote_state(self):
        missing = self.root / "remote" / "gpu-leases.json"
        self.assertEqual(G.snapshot(missing), [])
        self.assertFalse(missing.parent.exists())
        self.acquire(requested="0")
        before = self.state.read_bytes()
        before_stat = self.state.stat()
        with mock.patch.object(G, "living", return_value=False), \
                mock.patch.object(G, "locked", side_effect=AssertionError("probe must not lock/write")):
            self.assertEqual(G.snapshot(self.state), [])
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.state.stat().st_mtime_ns, before_stat.st_mtime_ns)

    def test_unknown_gpu_measurement_refuses_gpu_but_explicit_cpu_still_runs(self):
        for row in ({"reachable": False}, {**OBS, "detail": "smi error"},
                    {**OBS, "process_detail": "process query failed"}):
            with self.assertRaises(G.GPUUnavailable):
                G.acquire(self.state, row)
            self.assertIsNone(G.acquire(self.state, row, requested=""))

    def options(self, name, requested=None):
        run_dir = self.root / name
        inner = ('__HEARTING_GPU_SETUP__ && python3 -c '
                 + "'import os,time; print(os.environ[\"CUDA_VISIBLE_DEVICES\"], flush=True); time.sleep(.6)'"
                 + ' > ' + str(run_dir / "log") + '; printf "%s" "$?" > ' + str(run_dir / "exit_code"))
        return {"state_path": str(self.state), "observation": OBS, "requested": requested,
                "owner": {"label": name}, "task": "CPU fixture", "run_id": "gpu-lease-test-" + name,
                "run_dir": str(run_dir), "inner": inner}

    def launch(self, options):
        code = SOURCE + '\nprint(json.dumps(launch_compute(%r, %r)))' % (options, SOURCE)
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_two_concurrent_sessions_choose_distinct_gpus_and_release_on_exit(self):
        options = [self.options("one"), self.options("two")]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(self.launch, options))
        self.assertEqual({row["gpus"] for row in receipts}, {"0", "1"})
        deadline = time.monotonic() + 5
        while G.snapshot(self.state) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(G.snapshot(self.state), [])
        for opts, receipt in zip(options, receipts):
            self.assertEqual((Path(opts["run_dir"]) / "log").read_text().strip(), receipt["gpus"])
            self.assertEqual((Path(opts["run_dir"]) / "exit_code").read_text(), "0")

    def test_resource_and_compute_share_the_same_admission(self):
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.state.parent / "jobs.log")}), \
             mock.patch.object(G, "local_observation", return_value=OBS), \
             mock.patch.object(G, "launcher_owner", return_value={"label": "resource [ab]"}):
            path, lease, env = G.resource_admission({"resource_class": "gpu"}, ["true"], run_id="resource")
        self.assertEqual(path, self.state)
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "0")
        receipt = self.launch(self.options("compute"))
        self.assertEqual(receipt["gpus"], "1")
        self.assertEqual(subprocess.run(["sh", "-c", env["HEARTING_GPU_LEASE_RELEASE"]]).returncode, 0)
        self.assertNotIn(lease["token"], {r["token"] for r in G.snapshot(self.state)})

    def test_explicit_cpu_payload_in_gpu_scoped_route_skips_gpu_probe_and_lease(self):
        with mock.patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": ""}), \
             mock.patch.object(G, "local_observation") as probe:
            self.assertEqual(G.resource_admission({"resource_class": "long-running"},
                             ["python", "cpu_payload.py"], gpu_scoped=True), (None, None, {}))
        probe.assert_not_called()

    def test_unbound_wrapper_never_runs_after_its_pending_reservation_was_reaped(self):
        lease = self.acquire()
        G.release(self.state, lease)
        marker = self.root / "forbidden"
        with self.assertRaises(G.GPUUnavailable):
            G.payload(str(self.state), lease, "touch " + str(marker))
        self.assertFalse(marker.exists())

    def test_resource_runner_fences_records_and_releases_actual_gpu_payload(self):
        spec = importlib.util.spec_from_file_location("_gpu_test_runner", Path(G.__file__).with_name("resource-runner.py"))
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        route = self.root / "route.json"
        route.write_text(json.dumps({"capability": "autopilot-lab", "artifact_root": str(self.root),
            "nodes": [{"id": "full-run", "kind": "resource-runner", "resource_class": "long-running",
                       "resource_transport": "detached-process"}]}))
        registry, log = self.root / "runs.json", self.root / "resource.log"
        output, children = io.StringIO(), []
        real_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            children.append(proc)
            return proc
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.state.parent / "jobs.log")}), \
             mock.patch.object(G, "local_observation", return_value=OBS), \
             mock.patch.object(runner.subprocess, "run"), mock.patch.object(runner, "register_registry"), \
             mock.patch.object(runner.subprocess, "Popen", side_effect=spawn), mock.patch("sys.stdout", output):
            runner.main(["--registry", str(registry), "start", "--run-id", "local", "--cwd", str(self.root),
                         "--log", str(log), "--route", str(route), "--node", "full-run", "--smoke-attestation", "unused",
                         "--", sys.executable, "-c", "import os; print(os.environ['CUDA_VISIBLE_DEVICES'])"])
            children[0].wait(timeout=5)
        row = json.loads(output.getvalue())
        self.assertEqual(row["gpus"], "0")
        self.assertEqual(log.read_text().strip(), "0")
        self.assertEqual(Path(row["sentinel"]).read_text(), "0")
        self.assertEqual(G.snapshot(self.state), [])

    def test_shared_state_separates_hosts_and_never_prunes_foreign_host_pids(self):
        first = self.acquire(requested="0")
        with G.locked(self.state) as data:
            data["leases"][first["token"]]["host"] = "another-host"
            data["leases"][first["token"]]["starttime"] = "0"
        second = self.acquire(requested="0")
        self.assertEqual(second["gpus"], ["0"])
        with G.locked(self.state) as data:
            self.assertIn(first["token"], data["leases"])
        self.assertEqual([row["token"] for row in G.snapshot(self.state)], [second["token"]])

    def test_supervised_full_run_uses_existing_gpu_scope_before_releasing_payload(self):
        import artifact_producer
        spec = importlib.util.spec_from_file_location("_gpu_verified_runner", Path(G.__file__).with_name("resource-runner.py"))
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        jobs = self.state.parent / "jobs.log"
        jobs.parent.mkdir(parents=True)
        jobs.write_text("")
        route_file, registry, log = self.root / "verified-route.json", self.root / "verified-runs.json", self.root / "verified.log"
        route = {"route_id": "rt-gpu-scope", "route_hash": "sha256:fixture", "capability": "autopilot-lab",
                 "nodes": [{"id": "full-run", "kind": "resource-runner", "resource_class": "long-running"}]}
        placeholder = {"run_id": "supervised", "cwd": str(self.root), "log": str(log),
            "route": str(route_file), "node": "full-run", "status": "launching", "sentinel": str(log) + ".exit",
            "progress_file": str(log) + ".progress", "owner_wait": {"session_id": "fixture"},
            "command": [sys.executable, "-c", "import os; print(os.environ['CUDA_VISIBLE_DEVICES'])"]}
        children, output = [], io.StringIO()
        real_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            children.append(proc)
            return proc
        with mock.patch.object(G, "local_observation", return_value=OBS), \
             mock.patch.object(runner, "register_registry"), \
             mock.patch.object(artifact_producer, "prepare_route_artifact_env", return_value={"AGENT_ARTIFACT_OUTPUT_DIR": str(self.root)}), \
             mock.patch.object(runner, "start_watch", return_value=(mock.Mock(), runner.proc_identity(os.getpid()))), \
             mock.patch.object(runner.subprocess, "run"), \
             mock.patch.object(runner.subprocess, "Popen", side_effect=spawn), mock.patch("sys.stdout", output):
            runner.start_verified(registry, SimpleNamespace(jobs=str(jobs), run_id="supervised", node="full-run"),
                                  route, route_file, placeholder)
            children[0].wait(timeout=5)
        self.assertEqual(json.loads(output.getvalue())["gpus"], "0")
        self.assertEqual(log.read_text().strip(), "0")
        self.assertEqual(G.snapshot(self.state), [])

    def test_inline_cuda_choice_is_honored_and_suggestions_exclude_reservations(self):
        self.assertEqual(G.requested_devices(["env", "CUDA_VISIBLE_DEVICES=1", "python", "train.py"]), "1")
        tool_spec = importlib.util.spec_from_file_location("_gpu_suggest_compute", Path(G.__file__).with_name("compute-hosts.py"))
        tool = importlib.util.module_from_spec(tool_spec)
        tool_spec.loader.exec_module(tool)
        observation = json.loads(json.dumps(OBS))
        observation["gpus"][0]["reservations"] = [{"token": "busy"}]
        with mock.patch.object(tool, "probe_host", return_value=observation):
            self.assertEqual(tool._run_gpu_observation("here", {})["suggested_gpu"], 1)

    def test_compute_cmd_run_auto_selects_and_refuses_busy_explicit_device_end_to_end(self):
        spec = importlib.util.spec_from_file_location("_gpu_end_to_end_compute", Path(G.__file__).with_name("compute-hosts.py"))
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        finish = self.root / "finish"
        code = ("import os,time; from pathlib import Path; print(os.environ['CUDA_VISIBLE_DEVICES'],flush=True); "
                "end=time.monotonic()+10; p=Path(%r); "
                "exec('while not p.exists() and time.monotonic()<end: time.sleep(.02)')" % str(finish))
        args = SimpleNamespace(host="fixture", command=[sys.executable, "-c", code], name="managed-fixture",
                               cwd=None, env=None, gpus=None, share=False, dry_run=False, json=True)
        real_run = subprocess.run
        def local_remote(_host, script, **kwargs):
            return real_run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
        output, warning = io.StringIO(), io.StringIO()
        with mock.patch.object(tool, "load_config", return_value={"run_root": self.root / "managed-runs",
                         "hosts": {"fixture": {"ssh_host": "local"}}}), \
             mock.patch.object(G, "state_path", return_value=self.state), \
             mock.patch.object(tool, "_run_gpu_observation", return_value=OBS), \
             mock.patch.object(tool, "_launcher_session_owner", return_value=None), \
             mock.patch.object(tool, "_launcher_provenance", return_value={}), \
             mock.patch.object(tool, "_launcher_route", return_value=None), \
             mock.patch.object(tool, "_spawn_completion_watch", return_value=False), \
             mock.patch.object(tool, "remote", side_effect=local_remote), \
             mock.patch("sys.stdout", output), mock.patch("sys.stderr", warning):
            try:
                self.assertEqual(tool.cmd_run(args), 0)
                receipt = json.loads(output.getvalue())
                self.assertEqual(receipt["gpus"], "0")
                self.assertTrue(receipt["gpu_lease"]["token"])
                args.gpus = "0"
                self.assertEqual(tool.cmd_run(args), 1)
                self.assertIn("GPU in use", warning.getvalue())
                self.assertIn("free GPUs: gpu1", warning.getvalue())
            finally:
                finish.write_text("done")
        deadline = time.monotonic() + 5
        while G.snapshot(self.state) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(G.snapshot(self.state), [])
        self.assertEqual((self.root / "managed-runs" / receipt["run_id"] / "log").read_text().strip(), "0")

    def test_composed_probe_script_compiles_without_changing_early_exit_indentation(self):
        spec = importlib.util.spec_from_file_location("_gpu_test_compute", Path(G.__file__).with_name("compute-hosts.py"))
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        def check_script(_host, script, **kwargs):
            body = script.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
            compile(body, "composed-probe", "exec")
            return subprocess.CompletedProcess([], 0, json.dumps({"gpus": [], "gpu_leases": []}), "")
        with mock.patch.object(tool, "remote", side_effect=check_script), \
             mock.patch.object(G, "state_path", return_value=self.state):
            self.assertTrue(tool.probe_host("fixture", {"ssh_host": "local"}, [], [])["reachable"])


if __name__ == "__main__":
    unittest.main()
