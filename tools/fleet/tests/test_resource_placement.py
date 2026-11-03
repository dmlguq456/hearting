"""Local GPU selection is visible before use, without borrowing another run."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools"))
import resource_placement as placement
from fleet import fleet, model, render
from fleet.collectors import compute_hosts


class PlacementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = Path(self.tmp.name)
        self.process(100, 1, "10", children=[101])
        self.process(101, 100, "11", children=[102, 103])
        for pid in (102, 103):
            self.process(pid, 101, str(pid), state="D", devices="1")
        self.run = {"run_id": "tf-resume", "pid": 100, "starttime": "10",
                    "command_hash": hashlib.sha256(b"wrapper\0").hexdigest()}

    def process(self, pid, parent, start, children=(), state="S", devices=None,
                run_id="tf-resume"):
        root = self.proc / str(pid)
        root.mkdir(exist_ok=True)
        fields = [state, str(parent), str(pid)] + ["0"] * 16 + [start]
        (root / "stat").write_text(f"{pid} (worker) " + " ".join(fields))
        (root / "cmdline").write_bytes(b"wrapper\0")
        env = f"HEARTING_RESOURCE_RUN_ID={run_id}\0"
        if devices is not None:
            env += f"CUDA_VISIBLE_DEVICES={devices}\0"
        env += "SECRET=must-not-be-projected\0"
        (root / "environ").write_bytes(env.encode())
        task = root / "task" / str(pid)
        task.mkdir(parents=True, exist_ok=True)
        (task / "children").write_text(" ".join(map(str, children)))

    def observe(self):
        with mock.patch.object(placement, "MAX_SECONDS", 1), \
             mock.patch.object(placement.socket, "gethostname", return_value="mgpu-a1.example.test"):
            return placement.observe(self.run, self.proc)

    def test_tf_children_selection_and_io_wait_are_display_only(self):
        result = self.observe()
        self.assertEqual(result["requested_devices"], ["1"])
        self.assertTrue(result["io_wait"])
        self.assertEqual({p["pid"] for p in result["processes"]}, {100, 101, 102, 103})
        self.assertNotIn("SECRET", str(result))
        self.assertNotIn("must-not-be-projected", str(result))

    def test_reused_root_and_nested_or_reparented_children_are_not_claimed(self):
        self.run["starttime"] = "wrong"
        self.assertIsNone(self.observe())
        self.run["starttime"] = "10"
        self.process(102, 101, "102", devices="2", run_id="different-resource")
        self.process(103, 999, "103", devices="3")
        result = self.observe()
        self.assertEqual(result["requested_devices"], [])
        self.assertEqual({p["pid"] for p in result["processes"]}, {100, 101})

    def test_root_exec_and_child_reuse_during_read_discard_evidence(self):
        original = placement._read
        def changed(path, *args):
            value = original(path, *args)
            if path == self.proc / "102" / "environ":
                self.process(102, 101, "999", devices="1")
            return value
        with mock.patch.object(placement, "_read", side_effect=changed):
            self.assertIsNone(self.observe())
        self.process(102, 101, "102", state="D", devices="1")
        def exec_root(path, *args):
            value = original(path, *args)
            if path == self.proc / "102" / "environ":
                (self.proc / "100" / "cmdline").write_bytes(b"different\0")
            return value
        with mock.patch.object(placement, "_read", side_effect=exec_root):
            self.assertIsNone(self.observe())

    def test_no_directory_scan_or_subprocess_and_bounded_tree(self):
        with mock.patch.object(Path, "iterdir", side_effect=AssertionError("whole proc scan")), \
             mock.patch.object(placement, "MAX_PROCESSES", 2):
            result = self.observe()
        self.assertEqual(len(result["processes"]), 2)
        self.assertFalse(result["complete"])
        with mock.patch.object(placement.time, "monotonic", side_effect=[0, 1, 1]):
            self.assertIsNone(placement.observe(self.run, self.proc))

    def test_truncated_environment_cannot_hide_a_nested_resource(self):
        raw = (b"CUDA_VISIBLE_DEVICES=2\0FILLER=" + b"x" * placement.MAX_BYTES
               + b"\0HEARTING_RESOURCE_RUN_ID=different-resource\0")
        (self.proc / "102" / "environ").write_bytes(raw)
        result = self.observe()
        self.assertEqual(result["requested_devices"], ["1"])
        self.assertNotIn(102, {p["pid"] for p in result["processes"]})
        self.assertFalse(result["complete"])
        (self.proc / "100" / "environ").write_bytes(raw)
        self.assertIsNone(self.observe())


class PlacementNowTest(unittest.TestCase):
    def setUp(self):
        self.child = model.ResourceJob(run_id="tf-resume", node="resume-run", liveness="working",
            pid=100, starttime="10", process_group=100, command=["python", "resume_run.py"],
            local_placement={"hostname": "mgpu-a1.example.test", "requested_devices": ["1"],
                "io_wait": True, "processes": [{"pid": 102, "starttime": "102"}]})
        self.snapshot = {"configured": True, "hosts": [{"host": "mgpu-a1", "self": True, "reachable": True,
            "gpus": [{"index": 1, "uuid": "GPU-12345678-abcd", "processes": [
                {"pid": 777, "proc_start": "777", "pgid": 777,
                 "used_memory_mib": 8192, "command": "unrelated.py"}]}]}]}

    def now(self, harness):
        owner = model.DispatchJob(key="lab", slug="tf-owner", harness=harness, liveness="idle",
            resource_children=[self.child], resource_wait={"run_ids": ["tf-resume"]})
        with mock.patch.object(render, "_fresh_compute_hosts", return_value=(self.snapshot, 0)):
            return render._resource_now_text(owner)

    def test_selection_before_use_is_shared_and_never_creates_gpu_memory(self):
        for harness in ("claude", "codex", "opencode"):
            self.assertIn('mgpu-a1:1 selected · GPU not in use · I/O wait', self.now(harness))
        self.assertEqual(render._resource_gpu_resources(self.child, self.snapshot), [])

    def test_observed_child_with_new_group_wins_and_reused_pid_does_not(self):
        process = self.snapshot["hosts"][0]["gpus"][0]["processes"][0]
        process.update(pid=102, proc_start="102", pgid=102)
        for harness in ("claude", "codex", "opencode"):
            self.assertIn("mgpu-a1:1", self.now(harness))
            self.assertNotIn("selected", self.now(harness))
            self.assertNotIn('GPU not in use', self.now(harness))
        process["proc_start"] = "reused"
        self.assertIn("selected", self.now("opencode"))

    def test_uuid_selection_and_no_sample_do_not_guess(self):
        self.child.local_placement["requested_devices"] = ["GPU-12345678"]
        self.assertIn('mgpu-a1:1 selected', self.now("codex"))
        self.child.local_placement = None
        self.assertIn('host/GPU unknown', self.now("codex"))

    def test_failed_probe_or_partial_tree_keeps_selection_without_claiming_no_use(self):
        host = self.snapshot["hosts"][0]
        for field in ("detail", "process_detail", "gpu_error", "process_error"):
            with self.subTest(field=field):
                host[field] = "unavailable"
                self.assertIn('mgpu-a1:1 selected · GPU use unknown', self.now("opencode"))
                host.pop(field)
        self.child.local_placement["complete"] = False
        self.assertIn('GPU use unknown', self.now("opencode"))

    def test_exact_new_group_child_is_registered_in_json_only_on_this_host(self):
        process = self.snapshot["hosts"][0]["gpus"][0]["processes"][0]
        process.update(pid=102, proc_start="102", pgid=102)
        with mock.patch.object(fleet, "_collect_memory", return_value=None), \
             mock.patch.object(fleet, "_collect_governor", return_value=None):
            payload = json.loads(fleet._snapshot_json(
                [], [], resource_jobs=[self.child], compute_host_snapshot=self.snapshot))
        self.assertEqual(payload["unregistered_gpu"], [])
        process["proc_start"] = "reused"
        self.assertEqual(len(compute_hosts.unregistered_gpu(self.snapshot, [self.child])), 1)
        process["proc_start"] = "102"
        self.snapshot["hosts"][0]["self"] = False
        self.assertEqual(len(compute_hosts.unregistered_gpu(self.snapshot, [self.child])), 1)

    def test_gpu_pid_without_confirmed_identity_does_not_claim_use_or_preuse(self):
        process = self.snapshot["hosts"][0]["gpus"][0]["processes"][0]
        process.update(pid=102, proc_start=None, pgid=None,
                       attribution_reason="process-unavailable")
        for start in (None, "reused"):
            process["proc_start"] = start
            for harness in ("claude", "codex", "opencode"):
                self.assertIn('mgpu-a1:1 selected · GPU use unknown', self.now(harness))
            self.assertEqual(render._resource_gpu_resources(self.child, self.snapshot), [])


if __name__ == "__main__":
    unittest.main()
