"""Checks for the cross-host inventory and detached run surface.

The local host mode runs everything in-process, so the whole lifecycle —
launch, log, exit code, listing — is exercised without a network.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "compute-hosts.py"


def load_module():
    spec = importlib.util.spec_from_file_location("compute_hosts", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ComputeHostsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.root = Path(self.tmp.name)
        self.run_root = self.root / "runs"
        self.config = self.root / "compute-hosts.yaml"
        self.config.write_text(
            "schema_version: 1\n"
            f"run_root: {self.run_root}\n"
            "hosts:\n"
            "  here:\n"
            "    ssh_host: local\n"
            "    note: local fixture\n"
            "  elsewhere:\n"
            "    ssh_host: 203.0.113.7\n"
            "    ssh_port: 2222\n"
            "    ssh_user: someone\n",
            encoding="utf-8")
        self.env = {**os.environ, "COMPUTE_HOSTS_CONFIG": str(self.config)}

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *args):
        return subprocess.run([sys.executable, str(TOOL), *args],
                              text=True, capture_output=True, env=self.env)

    def test_missing_inventory_is_a_typed_failure(self):
        env = {**os.environ, "COMPUTE_HOSTS_CONFIG": str(self.root / "absent.yaml")}
        result = subprocess.run([sys.executable, str(TOOL), "list", "--static"],
                                text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not initialized", result.stderr)

    def test_static_listing_reads_the_inventory(self):
        result = self.run_tool("list", "--static", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual({h["host"] for h in payload["hosts"]},
                         {"here", "elsewhere"})
        self.assertEqual(payload["run_root"], str(self.run_root))

    def test_unknown_host_is_rejected(self):
        result = self.run_tool("list", "--static", "nope")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown host", result.stderr)

    def test_the_local_entry_is_discovered_by_hostname(self):
        # The inventory is identical on every machine, so moving the session
        # host must not require editing which entry says "local".
        module = load_module()
        import socket
        here = socket.gethostname()
        self.assertTrue(module.is_self({"ssh_host": "example.invalid",
                                        "hostname": here}))
        self.assertFalse(module.is_self({"ssh_host": "example.invalid",
                                         "hostname": here + "-other"}))
        self.assertFalse(module.is_self({"ssh_host": "example.invalid"}))
        # The explicit marker stays valid for a single-machine inventory.
        self.assertTrue(module.is_self({"ssh_host": "local"}))
        self.assertEqual(module.ssh_prefix({"ssh_host": "example.invalid",
                                            "hostname": here}), [])

    def test_ssh_prefix_carries_port_and_user(self):
        module = load_module()
        argv = module.ssh_prefix({"ssh_host": "h", "ssh_port": 2222,
                                  "ssh_user": "someone"})
        self.assertIn("-p", argv)
        self.assertIn("2222", argv)
        self.assertEqual(argv[-1], "someone@h")
        self.assertEqual(module.ssh_prefix({"ssh_host": "local"}), [])

    def test_proc_tcp_endpoints_decode_ipv4_ipv6_and_mapped_addresses(self):
        module = load_module()
        import socket
        self.assertEqual(
            module._decode_proc_net_endpoint("63DDEFA3:840C", socket.AF_INET),
            ("163.239.221.99", 33804),
        )
        self.assertEqual(
            module._decode_proc_net_endpoint(
                "00000000000000000000000001000000:0016", socket.AF_INET6),
            ("::1", 22),
        )
        self.assertEqual(
            module._decode_proc_net_endpoint(
                "B80D0120000000000000000001000000:0016", socket.AF_INET6),
            ("2001:db8::1", 22),
        )
        self.assertEqual(
            module._decode_proc_net_endpoint(
                "0000000000000000FFFF0000370DEFA3:0016", socket.AF_INET6),
            ("163.239.13.55", 22),
        )
        self.assertEqual(module._normalize_ip("::ffff:163.239.13.55"),
                         "163.239.13.55")
        self.assertIsNone(module._decode_proc_net_endpoint("bad", socket.AF_INET))

        proc_root = self.root / "proc-net"
        (proc_root / "net").mkdir(parents=True)
        (proc_root / "net" / "unix").write_text(
            "Num RefCount Protocol Flags Type St Inode Path\n"
            "0: 2 0 00010000 0001 01 9002 /tmp/control\n"
            "0: 3 0 00000000 0001 03 9003\n",
            encoding="ascii",
        )
        self.assertEqual(module._unix_listener_inodes(proc_root), {9002})

    def test_local_ssh_bridge_requires_one_unique_session_and_stable_pid(self):
        module = load_module()
        self.assertEqual(module._unique_session_owner({
            "CODEX_THREAD_ID": "sid-one", "CODEX_SESSION_ID": "sid-one",
        }), {"kind": "session", "harness": "codex", "id": "sid-one"})
        self.assertIsNone(module._unique_session_owner({
            "CODEX_THREAD_ID": "sid-one", "CODEX_SESSION_ID": "sid-two",
        }))
        self.assertIsNone(module._unique_session_owner({
            "CODEX_THREAD_ID": "sid-one", "CLAUDE_CODE_SESSION_ID": "sid-one",
        }))

        connection = ("192.0.2.10", 41000, "198.51.100.20", 22)
        with mock.patch.object(module, "_local_proc_identity",
                               side_effect=[77, 77]), \
                mock.patch.object(module, "_is_ssh_process", return_value=True), \
                mock.patch.object(module, "_local_identity_env", return_value={
                    "CODEX_THREAD_ID": "sid-one", "CODEX_SESSION_ID": "sid-one",
                }), \
                mock.patch.object(module, "_socket_inodes", return_value={9001}):
            rows = module._ssh_session_bridges_for_pid(
                12, {9001: connection}, set())
        self.assertEqual(rows, [{
            "_pid": 12,
            "_proc_start": 77,
            "_socket_inode": 9001,
            "client_address": "192.0.2.10", "client_port": 41000,
            "server_address": "198.51.100.20", "server_port": 22,
            "owner": {"kind": "session", "harness": "codex", "id": "sid-one"},
        }])

        with mock.patch.object(module, "_local_proc_identity",
                               side_effect=[77, 78]), \
                mock.patch.object(module, "_is_ssh_process", return_value=True), \
                mock.patch.object(module, "_local_identity_env", return_value={
                    "CODEX_THREAD_ID": "sid-one",
                }), \
                mock.patch.object(module, "_socket_inodes", return_value={9001}):
            self.assertEqual(
                module._ssh_session_bridges_for_pid(
                    12, {9001: connection}, set()), [])

        with mock.patch.object(module, "_local_proc_identity",
                               side_effect=[77, 77]), \
                mock.patch.object(module, "_is_ssh_process", return_value=True), \
                mock.patch.object(module, "_local_identity_env", return_value={
                    "CODEX_THREAD_ID": "sid-one",
                }), \
                mock.patch.object(module, "_socket_inodes",
                                  return_value={9001, 9002}):
            self.assertEqual(
                module._ssh_session_bridges_for_pid(
                    12, {9001: connection}, {9002}), [])

        with mock.patch.object(module, "_local_proc_identity", return_value=77), \
                mock.patch.object(module, "_is_ssh_process", return_value=True), \
                mock.patch.object(module, "_local_identity_env", return_value={
                    "CODEX_THREAD_ID": "sid-one",
                }), \
                mock.patch.object(module, "_socket_inodes", return_value=None):
            self.assertEqual(
                module._ssh_session_bridges_for_pid(
                    12, {9001: connection}, set()), [])

        proc_root = self.root / "proc"
        (proc_root / "12").mkdir(parents=True)
        candidate = {
            "_pid": 12,
            "_proc_start": 77,
            "_socket_inode": 9001,
            "client_address": "192.0.2.10", "client_port": 41000,
            "server_address": "198.51.100.20", "server_port": 22,
            "owner": {"kind": "session", "harness": "codex", "id": "sid-one"},
        }
        with mock.patch.object(module, "_established_tcp_sockets", side_effect=[
                    {9001: connection},
                    {9001: ("192.0.2.10", 41001, "198.51.100.20", 22)},
                ]), \
                mock.patch.object(module, "_unix_listener_inodes",
                                  side_effect=[set(), set()]), \
                mock.patch.object(module, "_ssh_session_bridges_for_pid",
                                  return_value=[candidate]):
            self.assertEqual(module.collect_ssh_session_bridges(proc_root), [])

    def test_conflicting_local_owners_for_one_connection_fail_closed(self):
        module = load_module()
        base = {
            "client_address": "192.0.2.10", "client_port": 41000,
            "server_address": "198.51.100.20", "server_port": 22,
        }
        rows = module._deduplicate_ssh_session_bridges([
            {**base, "owner": {"kind": "session", "harness": "codex",
                                "id": "sid-one"}},
            {**base, "owner": {"kind": "session", "harness": "claude",
                                "id": "sid-two"}},
        ])
        self.assertEqual(rows, [])

    def test_probe_serializes_transient_ssh_bridge_separately_from_claims(self):
        module = load_module()
        bridge = {
            "client_address": "192.0.2.10", "client_port": 41000,
            "server_address": "198.51.100.20", "server_port": 22,
            "owner": {"kind": "session", "harness": "codex", "id": "sid-one"},
        }
        captured = {}

        def fake_remote(_host, script, *, timeout):
            captured["script"] = script
            captured["timeout"] = timeout
            payload = {"hostname": "remote", "gpus": [], "observed_at": 1.0}
            return subprocess.CompletedProcess([], 0, json.dumps(payload), "")

        with mock.patch.object(module, "remote", side_effect=fake_remote):
            row = module.probe_host("remote", {"ssh_host": "example.invalid"},
                                    owner_claims=[], ssh_session_bridges=[bridge])
        self.assertTrue(row["reachable"])
        self.assertIn("HEARTING_OWNER_CLAIMS_JSON", captured["script"])
        self.assertIn("HEARTING_SSH_SESSION_BRIDGES_JSON", captured["script"])
        self.assertIn("sid-one", captured["script"])
        self.assertEqual(captured["timeout"], module.GPU_PROBE_TIMEOUT)

    def test_options_before_the_separator_are_not_swallowed(self):
        # argparse.REMAINDER would capture --name/--dry-run as part of the
        # command; the separator has to be split off before parsing.
        result = self.run_tool("run", "here", "--name", "label", "--dry-run",
                               "--", "echo", "hello")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-label", result.stdout)
        self.assertIn("would run: echo hello", result.stdout)
        self.assertFalse(self.run_root.exists(), "dry run created state")

    def test_local_run_records_log_and_exit_code(self):
        result = self.run_tool("run", "here", "--name", "ok", "--",
                               "bash", "-c", "echo 'quoted output'; exit 0")
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id = result.stdout.split()[1]
        log = self.run_root / run_id / "log"
        for _ in range(50):
            if (self.run_root / run_id / "exit_code").is_file():
                break
            time.sleep(0.1)
        self.assertEqual(log.read_text(encoding="utf-8").strip(), "quoted output")
        self.assertEqual(
            (self.run_root / run_id / "exit_code").read_text(encoding="utf-8").strip(),
            "0")
        listed = json.loads(self.run_tool("runs", "--json").stdout)
        self.assertEqual(listed[0]["run_id"], run_id)
        self.assertEqual(listed[0]["state"], "finished")
        tail = self.run_tool("tail", run_id)
        self.assertIn("quoted output", tail.stdout)
        self.assertIn("exit 0", tail.stdout)

    def test_local_run_exports_exact_compute_identity(self):
        result = self.run_tool(
            "run", "here", "--name", "identity", "--", "bash", "-c",
            "printf '%s|%s' \"$HEARTING_COMPUTE_RUN_ID\" \"$HEARTING_COMPUTE_HOST\"",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id = result.stdout.split()[1]
        exit_path = self.run_root / run_id / "exit_code"
        for _ in range(50):
            if exit_path.is_file():
                break
            time.sleep(0.1)
        log = (self.run_root / run_id / "log").read_text(encoding="utf-8")
        self.assertEqual(log, f"{run_id}|here")

    def test_failure_exit_code_is_preserved(self):
        result = self.run_tool("run", "here", "--", "bash", "-c", "exit 7")
        run_id = result.stdout.split()[1]
        for _ in range(50):
            if (self.run_root / run_id / "exit_code").is_file():
                break
            time.sleep(0.1)
        self.assertEqual(
            (self.run_root / run_id / "exit_code").read_text(encoding="utf-8").strip(),
            "7")

    def test_workdir_run_keeps_state_under_the_run_root(self):
        workdir = self.root / "workdir"
        workdir.mkdir()
        result = self.run_tool(
            "run", "here", "--cwd", str(workdir), "--",
            "bash", "-c", "pwd; exit 7",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id = result.stdout.split()[1]
        exit_path = self.run_root / run_id / "exit_code"
        for _ in range(50):
            if exit_path.is_file():
                break
            time.sleep(0.1)
        self.assertEqual(exit_path.read_text(encoding="utf-8").strip(), "7")
        self.assertEqual(
            (self.run_root / run_id / "log").read_text(encoding="utf-8").strip(),
            str(workdir),
        )
        self.assertFalse((workdir / "exit_code").exists())

    def test_two_launches_in_one_second_get_separate_directories(self):
        module = load_module()
        import datetime
        now = datetime.datetime(2026, 8, 21, 13, 48, 0)
        self.run_root.mkdir(parents=True, exist_ok=True)
        first = module._run_id("here", "x", now, run_root=self.run_root)
        (self.run_root / first).mkdir()
        second = module._run_id("here", "x", now, run_root=self.run_root)
        self.assertNotEqual(first, second)

    def test_conda_env_requires_a_declared_root(self):
        result = self.run_tool("run", "here", "--env", "someenv", "--dry-run",
                               "--", "true")
        self.assertEqual(result.returncode, 2)
        self.assertIn("conda root", result.stderr)

    def test_gpu_selection_and_workdir_reach_the_command(self):
        result = self.run_tool("run", "here", "--gpus", "1", "--cwd", str(self.root),
                               "--dry-run", "--", "true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CUDA_VISIBLE_DEVICES=1", result.stdout)
        self.assertIn(f"cd {self.root}", result.stdout)

    def test_host_probes_run_in_parallel_and_keep_inventory_order(self):
        module = load_module()
        barrier = threading.Barrier(3, timeout=1.0)

        def fake_probe(name, _host, _claims, _bridges):
            barrier.wait()
            return {"host": name, "reachable": True, "gpus": []}

        selected = [(name, {"ssh_host": "local"}) for name in ("a", "b", "c")]
        with mock.patch.object(module, "probe_host", side_effect=fake_probe), \
                mock.patch.object(module, "collect_ssh_session_bridges", return_value=[]):
            rows = module._probe_selected(selected)
        self.assertEqual([row["host"] for row in rows], ["a", "b", "c"])

    def test_gpu_probe_keeps_multi_gpu_processes_and_refuses_ambiguous_sessions(self):
        module = load_module()
        fakebin = self.root / "bin"
        fakebin.mkdir()
        fake_smi = fakebin / "nvidia-smi"
        fake_smi.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "if any(a.startswith('--query-gpu=') for a in sys.argv):\n"
            " print('0, GPU-A, NVIDIA A100, 42, 40960, 12288')\n"
            " print('1, GPU-B, NVIDIA A100, N/A, 40960, 2048')\n"
            "else:\n"
            " for row in json.loads(os.environ['FAKE_GPU_PROCESSES']): print(', '.join(map(str,row)))\n",
            encoding="utf-8",
        )
        fake_smi.chmod(0o755)

        identity_keys = {
            "AGENT_DISPATCH_ATTEMPT_ID", "AGENT_DISPATCH_SELF_SLUG",
            "HEARTING_COMPUTE_RUN_ID", "HEARTING_COMPUTE_HOST",
            "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
            "OPENCODE_SESSION_ID",
        }
        clean = {key: value for key, value in os.environ.items() if key not in identity_keys}
        processes = []
        try:
            run_proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                env={**clean, "HEARTING_COMPUTE_RUN_ID": "cnn-run-7",
                     "HEARTING_COMPUTE_HOST": "cnn"},
            )
            ambiguous_proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                env={**clean, "CODEX_THREAD_ID": "same-session-token",
                     "CLAUDE_CODE_SESSION_ID": "same-session-token"},
            )
            job_proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                env={**clean, "AGENT_DISPATCH_ATTEMPT_ID": "att-42",
                     "AGENT_DISPATCH_SELF_SLUG": "train-test",
                     "HEARTING_COMPUTE_RUN_ID": "shadowed-run"},
            )
            session_proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                env={**clean, "CODEX_THREAD_ID": "codex-exact-session"},
            )
            processes = [run_proc, ambiguous_proc, job_proc, session_proc]
            env = {
                **clean,
                "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""),
                "FAKE_GPU_PROCESSES": json.dumps([
                    ["GPU-A", run_proc.pid, "python", 8192],
                    ["GPU-A", ambiguous_proc.pid, "python", 2048],
                    ["GPU-B", job_proc.pid, "python", 1024],
                    ["GPU-B", session_proc.pid, "python", 512],
                ]),
            }
            result = subprocess.run(
                ["bash", "-c", module.PROBE_SCRIPT], text=True, capture_output=True,
                env=env, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                process.wait(timeout=2)

        self.assertEqual([gpu["index"] for gpu in payload["gpus"]], [0, 1])
        first, second = payload["gpus"]
        self.assertEqual(len(first["processes"]), 2)
        by_pid = {row["pid"]: row for row in first["processes"]}
        self.assertEqual(by_pid[run_proc.pid]["owner"]["kind"], "run")
        self.assertEqual(by_pid[run_proc.pid]["owner"]["label"], "run:cnn-run-7")
        self.assertIsNone(by_pid[ambiguous_proc.pid]["owner"])
        self.assertEqual(by_pid[ambiguous_proc.pid]["attribution_reason"],
                         "ambiguous-session")
        second_by_pid = {row["pid"]: row for row in second["processes"]}
        self.assertEqual(second_by_pid[job_proc.pid]["owner"]["kind"], "job")
        self.assertEqual(second_by_pid[job_proc.pid]["owner"]["label"], "job:train-test")
        self.assertEqual(second_by_pid[session_proc.pid]["owner"]["kind"], "session")
        self.assertEqual(second_by_pid[session_proc.pid]["owner"]["label"], "codex:codex-ex")
        self.assertIsInstance(second_by_pid[job_proc.pid]["proc_start"], int)
        self.assertIsInstance(payload["cpu_count"], int)
        self.assertGreater(payload["cpu_count"], 0)
        self.assertGreaterEqual(payload["cpu_utilization_pct"], 0)
        self.assertLessEqual(payload["cpu_utilization_pct"], 100)
        self.assertEqual(len(payload["cpu_thread_utilization_pct"]),
                         payload["cpu_count"])
        self.assertTrue(all(value is None or isinstance(value, int)
                            for value in payload["cpu_thread_utilization_pct"]))
        self.assertTrue(all(value is None or 0 <= value <= 100
                            for value in payload["cpu_thread_utilization_pct"]))
        for key in ("memory_total_mib", "memory_used_mib",
                    "swap_total_mib", "swap_used_mib"):
            self.assertTrue(payload[key] is None or isinstance(payload[key], int))
        self.assertGreater(payload["memory_total_mib"], 0)
        self.assertGreaterEqual(payload["memory_used_mib"], 0)

    def test_persistent_claim_reconnects_a_detached_root_to_its_session(self):
        module = load_module()
        fakebin = self.root / "claim-bin"
        fakebin.mkdir()
        fake_smi = fakebin / "nvidia-smi"
        fake_smi.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "if any(a.startswith('--query-gpu=') for a in sys.argv):\n"
            " print('0, GPU-A, NVIDIA A100, 61, 40960, 12288')\n"
            "else:\n"
            " print('GPU-A, %s, python, 12288' % os.environ['CLAIMED_GPU_PID'])\n",
            encoding="utf-8",
        )
        fake_smi.chmod(0o755)
        identity_keys = {
            "AGENT_DISPATCH_ATTEMPT_ID", "AGENT_DISPATCH_SELF_SLUG",
            "HEARTING_COMPUTE_RUN_ID", "HEARTING_COMPUTE_HOST",
            "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
            "OPENCODE_SESSION_ID",
        }
        clean = {key: value for key, value in os.environ.items() if key not in identity_keys}
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"], env=clean,
            start_new_session=True,
        )
        try:
            claimed = self.run_tool(
                "claim", "here", str(process.pid), "--harness", "claude",
                "--session", "f11a0486-c090-4098-aeb0-0fd6d79f8d0c", "--json",
            )
            self.assertEqual(claimed.returncode, 0, claimed.stderr)
            claim = json.loads(claimed.stdout)
            self.assertEqual(claim["root_pid"], process.pid)
            self.assertEqual(claim["owner"]["label"], "claude:f11a0486")

            env = {
                **clean,
                "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""),
                "CLAIMED_GPU_PID": str(process.pid),
                "HEARTING_OWNER_CLAIMS_JSON": json.dumps([claim]),
            }
            result = subprocess.run(
                ["bash", "-c", module.PROBE_SCRIPT], text=True, capture_output=True,
                env=env, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            stale_claim = {**claim, "root_cmdline_sha256": "0" * 64}
            stale_result = subprocess.run(
                ["bash", "-c", module.PROBE_SCRIPT], text=True, capture_output=True,
                env={**env, "HEARTING_OWNER_CLAIMS_JSON": json.dumps([stale_claim])},
                timeout=5,
            )
            self.assertEqual(stale_result.returncode, 0, stale_result.stderr)
            stale_payload = json.loads(stale_result.stdout)
        finally:
            process.terminate()
            process.wait(timeout=2)

        owner = payload["gpus"][0]["processes"][0]["owner"]
        self.assertEqual(owner["kind"], "session")
        self.assertEqual(owner["label"], "claude:f11a0486")
        self.assertEqual(owner["source"], "persistent-claim+ancestry")
        stale_process = stale_payload["gpus"][0]["processes"][0]
        stale_owner = stale_process["owner"]
        if stale_owner is not None:  # the test runner's own session ancestor may still be exact
            self.assertNotEqual(stale_owner["label"], "claude:f11a0486")
            self.assertNotEqual(stale_owner["source"], "persistent-claim+ancestry")


if __name__ == "__main__":
    unittest.main()
