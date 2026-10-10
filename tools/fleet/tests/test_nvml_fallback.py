"""NVML failure fixtures use fake proc/device files; never touch a compute host."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools.fleet import render
from tools.fleet.collectors import compute_hosts

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utilities"))
import gpu_leases

SPEC = importlib.util.spec_from_file_location("nvml_compute", ROOT / "utilities/compute-hosts.py")
CH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CH)
ERROR = "Failed to initialize NVML: Driver/library version mismatch\nNVML library version: 580.178"


class NVMLFallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, content in {
            "proc/stat": "cpu  100 0 0 900\ncpu0 100 0 0 900\n",
            "proc/uptime": "10000 0\n", "proc/loadavg": "0 0 0\n",
            "proc/driver/nvidia/version": "NVRM version: NVIDIA UNIX Open Kernel Module  580.173.02\n",
            "var/run/reboot-required": "reboot required\n",
        }.items():
            self.write(name, content)
        for index in (0, 1):
            self.write("proc/driver/nvidia/gpus/0000:%d/information" % index,
                       "Model: NVIDIA GeForce RTX 5090\nGPU UUID: GPU-%d\nDevice Minor: %d\n" % (index, index))
            self.write("dev/nvidia%d" % index, "")
        self.process(71, "fix", "0")
        self.process(72, "varying", "1")
        self.process(73, "fix", "0", parent=71)  # data loader, not a third training
        self.process(74, "cpu-only", "0", devices=())  # mask alone is insufficient

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def process(self, pid, run, mask, parent=0, devices=(0, 1), harness="claude"):
        self.write("proc/%d/stat" % pid,
                   "%d (python) S %d %d " % (pid, parent, pid) + "0 " * 16 + "99 0\n")
        self.write("proc/%d/status" % pid, "Uid:\t%d\t%d\t%d\t%d\n" % ((os.geteuid(),) * 4))
        key = {"claude": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_THREAD_ID",
               "opencode": "OPENCODE_SESSION_ID"}[harness]
        self.write("proc/%d/environ" % pid,
                   "HEARTING_COMPUTE_RUN_ID=%s\0CUDA_VISIBLE_DEVICES=%s\0%s=sid-exact\0" % (run, mask, key))
        self.write("proc/%d/cmdline" % pid,
                   "python\0run.py\0--engine_mode\0train\0--config\0%s.yaml\0" % run)
        self.write("proc/%d/comm" % pid, "python\n")
        base = self.root / "proc" / str(pid)
        (base / "cwd").symlink_to("/work/SR_CorrNet")
        (base / "fd").mkdir(exist_ok=True)
        for index in devices:
            (base / "fd" / str(index + 10)).symlink_to("/dev/nvidia%d" % index)

    def probe(self, error=ERROR, overrides=None, script=None):
        real_path, real_readlink = Path, os.readlink

        def mapped_path(value, *args):
            value = str(value)
            if value == "/proc" or value.startswith(("/proc/", "/dev/", "/var/run/")):
                return real_path(self.root / value.lstrip("/"), *args)
            return real_path(value, *args)

        def readlink(value, *args, **kwargs):
            return real_readlink(mapped_path(value), *args, **kwargs)

        script = (script or CH.PROBE_SCRIPT).split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        output = io.StringIO()
        with mock.patch("pathlib.Path", side_effect=mapped_path), \
                mock.patch("os.readlink", side_effect=readlink), \
                mock.patch("os.access", return_value=True), \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", error)), \
                contextlib.redirect_stdout(output):
            try:
                helpers, body = script.split("\ncpu_count = os.cpu_count()", 1)
                namespace = {}
                exec(compile(helpers, "<nvml-fixture-helpers>", "exec"), namespace)
                namespace.update(overrides or {})
                exec(compile("cpu_count = os.cpu_count()" + body, "<nvml-fixture>", "exec"), namespace)
            except SystemExit as exc:
                self.assertEqual(exc.code, 0)
        payload = json.loads(output.getvalue())
        # Exercise the real host JSON normalization too.
        with mock.patch.object(CH, "remote", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")):
            return CH.probe_host("cnn", {"ssh_host": "local"}, ssh_session_bridges=[])

    def test_managed_runs_remain_visible_with_unknown_nvml_values(self):
        host = self.probe()
        self.assertEqual([g["index"] for g in host["gpus"]], [0, 1])
        self.assertEqual([[p["pid"] for p in g["processes"]] for g in host["gpus"]], [[71], [72]])
        for gpu in host["gpus"]:
            for key in ("memory_total_mib", "memory_used_mib", "free_mib", "utilization_gpu_pct"):
                self.assertIsNone(gpu[key])
            self.assertIsNone(gpu["processes"][0]["used_memory_mib"])
            self.assertEqual(gpu["processes"][0]["session_owner"]["id"], "sid-exact")
        status = host["gpu_status"]
        self.assertEqual(status["running_count"], 2)
        self.assertEqual((status["library_version"], status["module_version"]), ("580.178", "580.173.02"))
        self.assertIn("재부팅 필요", status["summary"])
        self.assertNotIn("업데이트", status["summary"])

    def test_unknown_error_or_missing_reboot_signal_does_not_invent_a_cause(self):
        (self.root / "var/run/reboot-required").unlink()
        status = self.probe()["gpu_status"]
        self.assertNotIn("재부팅 필요", status["summary"])
        status = self.probe("GPU read failed")["gpu_status"]
        self.assertNotIn("드라이버 불일치", status["summary"])
        self.assertIsNone(status["library_version"])

    def test_pid_reuse_environment_race_and_foreign_euid_supply_no_work(self):
        stable = {"ppid": 0, "pgid": 71, "start": 99, "state": "S"}
        calls = {}

        def reused(pid):
            calls[pid] = calls.get(pid, 0) + 1
            return {**stable, "start": 99 if calls[pid] == 1 else 100}

        changes = iter(range(1000))
        for overrides in ({"proc_stat": reused}, {"same_euid": lambda pid: False},
                          {"identity_env": lambda pid: {"CUDA_VISIBLE_DEVICES": str(next(changes))}}):
            with self.subTest(overrides=list(overrides)):
                host = self.probe(overrides=overrides)
                self.assertTrue(all(not g["processes"] for g in host["gpus"]))

    def test_uuid_mask_and_unknown_mask_keep_placement_evidence_distinct(self):
        self.write("proc/71/environ", "HEARTING_COMPUTE_RUN_ID=fix\0CUDA_VISIBLE_DEVICES=GPU-0\0")
        self.write("proc/73/environ", "HEARTING_COMPUTE_RUN_ID=fix\0CUDA_VISIBLE_DEVICES=GPU-0\0")
        host = self.probe()
        self.assertEqual([p["pid"] for p in host["gpus"][0]["processes"]], [71])
        self.assertEqual([p["pid"] for p in host["gpus"][1]["processes"]], [72])
        self.write("proc/71/environ", "HEARTING_COMPUTE_RUN_ID=fix\0CUDA_VISIBLE_DEVICES=unresolved\0")
        self.write("proc/73/environ", "HEARTING_COMPUTE_RUN_ID=fix\0CUDA_VISIBLE_DEVICES=unresolved\0")
        host = self.probe()
        self.assertEqual(host["gpu_status"]["running_count"], 1)
        accessed = next(p for p in host["gpus"][0]["processes"] if p["pid"] == 71)
        self.assertEqual(accessed["gpu_placement"], "device-access-only")
        self.assertEqual(render._gpu_state(host["gpus"][0]), "unknown")

    def test_render_and_session_lines_preserve_work_in_every_harness(self):
        host = self.probe()
        render.set_compute_hosts({"configured": True, "hosts": [host]})
        self.addCleanup(render.set_compute_hosts, None)
        with mock.patch.dict(render._ROUTE_FOLD, {render._GPU_FOLD_ALL: False}, clear=True):
            for width in (60, 100, 168):
                rows = render._compute_host_rows(width)
                text = "\n".join(render._plain(row) for row in rows)
                self.assertIn("GPU 상태 확인 불가", text)
                self.assertIn("실행 중 2개", text)
                self.assertIn("모름", text)
                self.assertIn("fix.yaml", text)
                self.assertIn("varying.yaml", text)
                self.assertNotIn("Failed to initialize", text)
                self.assertTrue(all(render._dw(render._plain(row)) <= width for row in rows))
            for harness in ("claude", "codex", "opencode"):
                for gpu in host["gpus"]:
                    gpu["processes"][0]["session_owner"]["harness"] = harness
                resources = render._gpu_session_resources({"hosts": [host]})
                self.assertEqual([r["index"] for r in resources[harness, "sid-exact"]], [0, 1])
                self.assertIn("모름", "\n".join(render._plain(r) for r in render._gpu_resource_strip(resources[harness, "sid-exact"], 168)))
        entries = compute_hosts.unregistered_gpu({"configured": True, "hosts": [host]})
        self.assertEqual(len(entries), 2)

    def test_admission_refuses_unknown_measurement_even_with_share(self):
        host = self.probe()
        with mock.patch.object(CH, "probe_host", return_value=host):
            observation = CH._run_gpu_observation("cnn", {"ssh_host": "local"})
        self.assertEqual(observation["gpu_status"], host["gpu_status"])
        for requested in (None, "0", "1"):
            for share in (False, True):
                with self.assertRaisesRegex(gpu_leases.GPUUnavailable, "GPU 상태 확인 불가.*실행 중 2개"):
                    gpu_leases.select(observation, [], requested=requested, share=share)
        self.assertEqual(gpu_leases.select(host, [], requested=""), [])

    def test_probe_human_output_uses_summary_and_keeps_raw_error_in_json(self):
        host = self.probe()
        from types import SimpleNamespace
        config = {"hosts": {"cnn": {"ssh_host": "local"}}, "run_root": self.root}
        for as_json in (True, False):
            output, warning = io.StringIO(), io.StringIO()
            with mock.patch.object(CH, "load_config", return_value=config), \
                    mock.patch.object(CH, "_probe_selected", return_value=[host]), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(warning):
                self.assertEqual(CH.cmd_probe(SimpleNamespace(hosts=["cnn"], json=as_json)), 0)
            self.assertNotIn("Failed to initialize", warning.getvalue())
            if as_json:
                self.assertIn("Failed to initialize", json.loads(output.getvalue())[0]["detail"])
                self.assertIn("GPU 상태 확인 불가", warning.getvalue())
            else:
                self.assertIn("GPU 상태 확인 불가", output.getvalue())
                self.assertIn("모름", output.getvalue())
                self.assertIn("fix.yaml", output.getvalue())

    def test_empty_query_error_cannot_allow_share_on_an_unmeasured_gpu(self):
        host = self.probe("  \n")
        self.assertTrue(host["detail"])
        for row in (host, {**host, "detail": ""}, {**host, "detail": "", "gpu_status": None}):
            with self.assertRaises(gpu_leases.GPUUnavailable):
                gpu_leases.select(row, [], share=True)

    def test_failed_nvml_reaches_real_reservation_snapshot_without_writing(self):
        namespace = self.root / "proc/self/ns/pid"
        namespace.parent.mkdir(parents=True)
        namespace.symlink_to("pid:[fixture]")
        state = self.root / "gpu-leases.json"
        state.write_text(json.dumps({"schema_version": 1, "leases": {"lease": {
            "host": socket.gethostname().lower().split(".")[0], "pid": 71,
            "starttime": "99", "pid_namespace": "pid:[fixture]", "gpus": ["0"],
            "owner": {"label": "확인된 예약"}, "task": "train", "started_at": 10,
        }}}))
        original, mtime = state.read_bytes(), state.stat().st_mtime_ns
        with mock.patch.object(gpu_leases, "state_path", return_value=state), \
                mock.patch.object(CH, "remote", return_value=subprocess.CompletedProcess([], 0, '{"gpus":[]}', "")) as remote:
            CH.probe_host("cnn", {"ssh_host": "local"}, ssh_session_bridges=[])
        # Execute the actual composed script, including its embedded lease API.
        host = self.probe(script=remote.call_args.args[1])
        self.assertEqual(host["gpus"][0]["reservations"][0]["owner"]["label"], "확인된 예약")
        self.assertEqual(host["gpu_status"]["running_count"], 2)
        self.assertEqual(state.read_bytes(), original)
        self.assertEqual(state.stat().st_mtime_ns, mtime)
        self.assertFalse(Path(str(state) + ".lock").exists())

    def test_invalid_cuda_identifier_stops_mask_and_uuid_prefix_is_host_unique(self):
        for pid in (71, 73):
            self.write("proc/%d/environ" % pid,
                       "HEARTING_COMPUTE_RUN_ID=fix\0CUDA_VISIBLE_DEVICES=0,-1,1\0")
        host = self.probe()
        self.assertEqual([p["pid"] for p in host["gpus"][0]["processes"]], [71])
        self.assertEqual([p["pid"] for p in host["gpus"][1]["processes"]], [72])
        self.process(75, "ambiguous-uuid", "GPU-", devices=(0,))
        host = self.probe()
        process = next(p for p in host["gpus"][0]["processes"] if p["pid"] == 75)
        self.assertEqual(process["gpu_placement"], "device-access-only")
        self.assertEqual(host["gpu_status"]["running_count"], 2)


if __name__ == "__main__":
    unittest.main()
