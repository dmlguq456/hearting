"""Checks for the cross-host inventory and detached run surface.

The local host mode runs everything in-process, so the whole lifecycle —
launch, log, exit code, listing — is exercised without a network.
"""

import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "compute-hosts.py"


def load_module():
    spec = importlib.util.spec_from_file_location("compute_hosts", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SSHNamespacePrefixTest(unittest.TestCase):
    HOST = {"ssh_host": "example.invalid", "ssh_port": 1689, "ssh_user": "operator"}
    NORMAL = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
              "-p", "1689", "operator@example.invalid"]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / ".ssh").mkdir()
        self.config = self.home / ".ssh" / "config"
        self.config.write_text("Host example.invalid\n  User operator\n")
        self.module = load_module()
        self.paths = {
            "/etc/ssh/ssh_config": mock.Mock(),
            "/proc/sys/kernel/overflowuid": mock.Mock(),
            "/proc/self/uid_map": mock.Mock(),
        }
        self.paths["/etc/ssh/ssh_config"].stat.return_value.st_uid = 65534
        self.paths["/proc/sys/kernel/overflowuid"].read_text.return_value = "65534\n"
        self.paths["/proc/self/uid_map"].read_text.return_value = "1002 0 1\n"
        patch = mock.patch.object(self.module, "Path", side_effect=self.paths.__getitem__)
        self.path = patch.start()
        self.addCleanup(patch.stop)
        self.path.home.return_value = self.home
        uid = mock.patch.object(self.module.os, "getuid", return_value=1002)
        uid.start()
        self.addCleanup(uid.stop)

    def test_normal_root_owned_config_keeps_identical_argv_bytes(self):
        self.paths["/etc/ssh/ssh_config"].stat.return_value.st_uid = 0
        actual = self.module.ssh_prefix(self.HOST)
        self.assertEqual(b"\0".join(word.encode() for word in actual),
                         b"\0".join(word.encode() for word in self.NORMAL))
        self.paths["/proc/sys/kernel/overflowuid"].read_text.assert_not_called()

    def test_unmapped_system_owner_uses_user_config_and_disables_key_updates(self):
        for uid_map in ("1002 0 1\n", "0 1002 1\n"):
            with self.subTest(uid_map=uid_map):
                self.paths["/proc/self/uid_map"].read_text.return_value = uid_map
                actual = self.module.ssh_prefix(self.HOST)
                self.assertEqual(actual, self.NORMAL[:5] + [
                    "-F", str(self.config), "-o", "UpdateHostKeys=no"] + self.NORMAL[5:])
                self.assertNotIn("StrictHostKeyChecking=no", actual)
        self.assertEqual(self.config.read_text(), "Host example.invalid\n  User operator\n")

    def test_missing_user_config_uses_dev_null_without_creating_files(self):
        home = self.home / "absent"
        self.path.home.return_value = home
        self.assertEqual(self.module.ssh_prefix(self.HOST), self.NORMAL[:5] + [
            "-F", "/dev/null", "-o", "UpdateHostKeys=no"] + self.NORMAL[5:])
        self.assertFalse(home.exists())

    def test_real_mapped_overflow_owner_keeps_normal_ownership_checks(self):
        for uid_map in ("0 0 4294967295\n", "1002 0 1\n65534 65534 1\n"):
            with self.subTest(uid_map=uid_map):
                self.paths["/proc/self/uid_map"].read_text.return_value = uid_map
                self.assertEqual(self.module.ssh_prefix(self.HOST), self.NORMAL)

    def test_other_or_current_owner_does_not_trigger_namespace_options(self):
        for owner in (1002, 123, 65535):
            with self.subTest(owner=owner):
                self.paths["/etc/ssh/ssh_config"].stat.return_value.st_uid = owner
                self.assertEqual(self.module.ssh_prefix(self.HOST), self.NORMAL)

    def test_kernel_overflow_uid_is_read_instead_of_hardcoded(self):
        self.paths["/etc/ssh/ssh_config"].stat.return_value.st_uid = 65535
        self.paths["/proc/sys/kernel/overflowuid"].read_text.return_value = "65535\n"
        actual = self.module.ssh_prefix(self.HOST)
        self.assertIn("-F", actual)
        self.assertIn("UpdateHostKeys=no", actual)

    def test_unreadable_or_malformed_namespace_evidence_preserves_argv(self):
        for path in self.paths.values():
            with self.subTest(path=path):
                method = path.stat if path is self.paths["/etc/ssh/ssh_config"] else path.read_text
                method.side_effect = OSError("fixture denied")
                self.assertEqual(self.module.ssh_prefix(self.HOST), self.NORMAL)
                method.side_effect = None
        for overflow in ("invalid", "0", "-1"):
            with self.subTest(overflow=overflow):
                self.paths["/proc/sys/kernel/overflowuid"].read_text.return_value = overflow
                self.assertEqual(self.module.ssh_prefix(self.HOST), self.NORMAL)
        self.paths["/proc/sys/kernel/overflowuid"].read_text.return_value = "65534\n"
        for mapping in ("", "invalid", "0 0", "0 0 0", "-1 0 1", "0 -1 1"):
            with self.subTest(mapping=mapping):
                self.paths["/proc/self/uid_map"].read_text.return_value = mapping
                self.assertEqual(self.module.ssh_prefix(self.HOST), self.NORMAL)

    def test_local_host_never_probes_ssh_configuration(self):
        self.assertEqual(self.module.ssh_prefix({"ssh_host": "local"}), [])
        self.path.assert_not_called()


class RunGPUObservationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_root = Path(self.tmp.name) / "runs"
        self.module = load_module()
        self.host = {"ssh_host": "local"}
        self.config = {"run_root": self.run_root, "hosts": {"here": self.host}}

    def run_receipt(self, row, *, dry_run=False, json_output=False):
        args = SimpleNamespace(host="here", command=["true"], name="headroom",
                               cwd=None, env=None, gpus="2", dry_run=dry_run,
                               json=json_output)
        output = io.StringIO()
        with mock.patch.object(self.module, "load_config", return_value=self.config), \
                mock.patch.object(self.module, "probe_host", side_effect=row if isinstance(row, Exception)
                                  else None, return_value=row) as probe, \
                mock.patch.object(self.module, "remote", return_value=subprocess.CompletedProcess(
                    [], 0, "started", "")) as launch, \
                mock.patch.object(self.module, "_launcher_provenance", return_value={}), \
                mock.patch.object(self.module, "_launcher_route", return_value=None), \
                mock.patch.object(self.module, "_spawn_completion_watch", return_value=False), \
                mock.patch("sys.stdout", output):
            rc = self.module.cmd_run(args)
        self.assertEqual(rc, 0)
        probe.assert_called_once_with("here", self.host, ssh_session_bridges=[])
        return output.getvalue(), launch

    def test_run_receipt_keeps_headroom_and_only_suggests_measured_idle_gpus(self):
        row = {"reachable": True, "observed_at": 1728000000, "gpus": [
            {"index": 0, "name": "idle", "free_mib": 9000, "total_mib": 10000,
             "utilization_gpu_pct": 0, "processes": []},
            {"index": 1, "name": "busy", "free_mib": 24000, "total_mib": 24000,
             "utilization_gpu_pct": 70, "processes": []},
            {"index": 2, "name": "unknown", "free_mib": 24000, "total_mib": 24000,
             "utilization_gpu_pct": None, "processes": []},
            {"index": 3, "name": "allocated", "free_mib": 20000, "total_mib": 24000,
             "utilization_gpu_pct": 0, "processes": [{"pid": 123}]},
        ]}
        text, launch = self.run_receipt(row, json_output=True)
        receipt = json.loads(text)
        observed = receipt["gpu_observation"]
        self.assertEqual(observed["observed_at"], row["observed_at"])
        self.assertEqual(observed["suggested_gpu"], 0)
        self.assertEqual(observed["gpus"][2]["utilization_gpu_pct"], None)
        self.assertEqual(receipt["gpus"], "2")
        self.assertIn("CUDA_VISIBLE_DEVICES=2", launch.call_args.args[1])
        meta = json.loads((self.run_root / receipt["run_id"] / "meta.json").read_text())
        self.assertEqual(meta["gpu_observation"], observed)

    def test_dry_run_shows_measurement_without_launch_or_run_state(self):
        row = {"reachable": True, "observed_at": 1728000000, "gpus": [
            {"index": 0, "name": "idle", "free_mib": 5000, "total_mib": 6000,
             "utilization_gpu_pct": 0, "processes": []}]}
        text, launch = self.run_receipt(row, dry_run=True)
        self.assertIn("5000/6000 MiB free, 0% util", text)
        self.assertIn("suggested: gpu0", text)
        self.assertIn("2024-10-04T00:00:00+00:00", text)
        launch.assert_not_called()
        self.assertFalse(self.run_root.exists())

    def test_failed_probe_preserves_launch_and_unknown_receipt(self):
        for row in ({"reachable": False, "observed_at": 1728000000,
                     "detail": "timed out", "gpus": []}, OSError("probe unavailable")):
            with self.subTest(row=row):
                text, launch = self.run_receipt(row)
                self.assertIn("started ", text)
                self.assertIn("GPU headroom: unknown", text)
                self.assertNotIn("suggested:", text)
                launch.assert_called_once()

    def test_missing_process_measurement_never_claims_an_idle_gpu(self):
        row = {"reachable": True, "observed_at": 1728000000,
               "process_detail": "compute process query unavailable", "gpus": [
                   {"index": 0, "free_mib": 8000, "utilization_gpu_pct": 0,
                    "processes": []}]}
        text, _ = self.run_receipt(row, json_output=True)
        self.assertIsNone(json.loads(text)["gpu_observation"]["suggested_gpu"])


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
        # A run's completion watch notifies through the dispatch state root; keep it here.
        self.env = {**os.environ, "COMPUTE_HOSTS_CONFIG": str(self.config),
                    "HARNESS_STATE_ROOT": str(self.root / "state")}
        self.env.pop("AGENT_DISPATCH_JOBS", None)

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

    def test_self_discovery_accepts_short_name_and_name_lists(self):
        # moving4's system hostname changed from `workstation` to
        # `moving4.iip.lab`: an inventory that declares only the short name,
        # or keeps several names, must still recognize the local machine.
        module = load_module()
        with mock.patch.object(module.socket, "gethostname",
                               return_value="moving4.iip.lab"):
            self.assertTrue(module.is_self({"ssh_host": "203.0.113.9",
                                            "hostname": "moving4"}))
            self.assertTrue(module.is_self({"ssh_host": "203.0.113.9",
                                            "hostname": ["workstation",
                                                         "moving4.iip.lab"]}))
            self.assertTrue(module.is_self({"ssh_host": "203.0.113.9",
                                            "hostname": ["workstation",
                                                         "moving4"]}))
            self.assertTrue(module.is_self({"ssh_host": "203.0.113.9",
                                            "hostname": "MOVING4"}))
            self.assertFalse(module.is_self({"ssh_host": "203.0.113.9",
                                             "hostname": ["workstation",
                                                          "cnn"]}))
            self.assertFalse(module.is_self({"ssh_host": "203.0.113.9",
                                             "hostname": "moving40"}))
            self.assertFalse(module.is_self({"ssh_host": "203.0.113.9",
                                             "hostname": [None, 7]}))
        with mock.patch.object(module.socket, "gethostname",
                               return_value="moving4"):
            self.assertTrue(module.is_self({"ssh_host": "203.0.113.9",
                                            "hostname": "moving4.iip.lab"}))

    def test_static_listing_marks_short_and_listed_names_as_self(self):
        import socket
        short = socket.gethostname().split(".")[0]
        self.config.write_text(
            "schema_version: 1\n"
            f"run_root: {self.run_root}\n"
            "hosts:\n"
            "  short:\n"
            f"    hostname: {short}\n"
            "    ssh_host: 203.0.113.9\n"
            "  listed:\n"
            f"    hostname: [stale-name, {short}]\n"
            "    ssh_host: 203.0.113.10\n"
            "  remote:\n"
            "    hostname: somewhere-else.invalid\n"
            "    ssh_host: 203.0.113.11\n",
            encoding="utf-8")
        result = self.run_tool("list", "--static", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        by_host = {row["host"]: row
                   for row in json.loads(result.stdout)["hosts"]}
        self.assertTrue(by_host["short"]["self"])
        self.assertTrue(by_host["listed"]["self"])
        self.assertFalse(by_host["remote"]["self"])

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

    def test_tmux_run_pins_launcher_session_over_stale_server_environment(self):
        fakebin = self.root / "tmux-bin"
        fakebin.mkdir()
        tmux = fakebin / "tmux"
        tmux.write_text(
            "#!/bin/sh\n"
            "while [ \"$#\" -gt 1 ]; do shift; done\n"
            "CODEX_THREAD_ID=stale-thread CODEX_SESSION_ID=other-stale "
            "CLAUDE_CODE_SESSION_ID=stale-claude /bin/bash -lc \"$1\"\n",
            encoding="utf-8",
        )
        tmux.chmod(0o755)
        clean = {key: value for key, value in self.env.items()
                 if key not in dict(load_module().SESSION_ENV_KEYS)}
        env = {**clean, "PATH": str(fakebin) + os.pathsep + os.environ["PATH"],
               "CODEX_THREAD_ID": "launch-thread"}
        result = subprocess.run(
            [sys.executable, str(TOOL), "run", "here", "--name", "tmux-owner",
             "--", "bash", "-c",
             "printf '%s|%s|%s' \"$CODEX_THREAD_ID\" "
             "\"${CODEX_SESSION_ID-}\" \"${CLAUDE_CODE_SESSION_ID-}\""],
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id = result.stdout.split()[1]
        log = (self.run_root / run_id / "log").read_text(encoding="utf-8")
        self.assertEqual(log, "launch-thread||")

    def test_conflicting_launcher_sessions_fail_closed_in_nohup_payload(self):
        fakebin = self.root / "nohup-bin"
        fakebin.mkdir()
        for command in ("bash", "mkdir", "nohup", "setsid"):
            target = Path("/usr/bin") / command
            if not target.exists():
                target = Path("/bin") / command
            (fakebin / command).symlink_to(target)
        clean = {key: value for key, value in self.env.items()
                 if key not in dict(load_module().SESSION_ENV_KEYS)}
        env = {**clean, "PATH": str(fakebin),
               "CODEX_THREAD_ID": "conflicting-codex",
               "CLAUDE_CODE_SESSION_ID": "conflicting-claude"}
        result = subprocess.run(
            [sys.executable, str(TOOL), "run", "here", "--name", "nohup-owner",
             "--", "bash", "-c",
             "printf '%s|%s' \"${CODEX_THREAD_ID-}\" "
             "\"${CLAUDE_CODE_SESSION_ID-}\""],
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id = result.stdout.split()[1]
        exit_path = self.run_root / run_id / "exit_code"
        for _ in range(50):
            if exit_path.is_file():
                break
            time.sleep(0.1)
        log = (self.run_root / run_id / "log").read_text(encoding="utf-8")
        self.assertEqual(log, "|")

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
            log = self.root / "train.log"
            log.write_text("Epoch 3/200\nTRAIN: 50% loss=0.5\n")
            with log.open("ab") as output:
                run_proc = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(10)"],
                    env={**clean, "HEARTING_COMPUTE_RUN_ID": "cnn-run-7",
                         "HEARTING_COMPUTE_HOST": "cnn"},
                    stdout=output, stderr=output,
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
        for gpu in payload["gpus"]:
            for process in gpu["processes"]:
                self.assertNotIn("progress", process)
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

    def _probe_with_fake_smi(self, processes_rows, bin_name):
        module = load_module()
        fakebin = self.root / bin_name
        fakebin.mkdir()
        fake_smi = fakebin / "nvidia-smi"
        fake_smi.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "if any(a.startswith('--query-gpu=') for a in sys.argv):\n"
            " print('0, GPU-A, NVIDIA A100, 42, 40960, 12288')\n"
            "else:\n"
            " for row in json.loads(os.environ['FAKE_GPU_PROCESSES']): print(', '.join(map(str,row)))\n",
            encoding="utf-8",
        )
        fake_smi.chmod(0o755)
        env = {
            **os.environ,
            "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_GPU_PROCESSES": json.dumps(processes_rows),
        }
        result = subprocess.run(
            ["bash", "-c", module.PROBE_SCRIPT], text=True, capture_output=True,
            env=env, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_gpu_probe_reports_cwd_elapsed_and_process_group(self):
        workdir = self.root / "work"
        workdir.mkdir()
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"], cwd=workdir)
        try:
            payload = self._probe_with_fake_smi(
                [["GPU-A", child.pid, "python", 512]], "cwd-bin")
            child_pgid = os.getpgid(child.pid)
        finally:
            child.terminate()
            child.wait(timeout=2)
        (row,) = payload["gpus"][0]["processes"]
        self.assertEqual(row["cwd"], os.path.realpath(workdir))
        self.assertIsInstance(row["elapsed_s"], int)
        self.assertTrue(0 <= row["elapsed_s"] < 60, row["elapsed_s"])
        self.assertEqual(row["pgid"], child_pgid)

    def test_gpu_probe_cwd_is_absent_when_unreadable_or_gone(self):
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait(timeout=5)
        rows = [["GPU-A", gone.pid, "python", 64]]
        if os.geteuid() != 0:
            rows.append(["GPU-A", 1, "init", 32])
        payload = self._probe_with_fake_smi(rows, "gone-bin")
        by_pid = {row["pid"]: row for row in payload["gpus"][0]["processes"]}
        ended = by_pid[gone.pid]
        for key in ("proc_start", "pgid", "cwd", "elapsed_s"):
            self.assertIsNone(ended[key], key)
        if 1 in by_pid:
            other = by_pid[1]
            self.assertIsNone(other["cwd"])
            self.assertIsInstance(other["pgid"], int)
            self.assertIsInstance(other["proc_start"], int)

    def test_gpu_probe_omits_training_logs_and_does_not_create_epoch_cache(self):
        log = self.root / "train.log"
        with log.open("wb") as handle:
            handle.write(b"Epoch 3/10\n"
                         b"TRAIN:  10%|#    | 10/100 [00:01<00:09]\r"
                         b"TRAIN:  11%|#    | 11/100 [00:01<00:09]\n")
        with log.open("ab") as out:
            to_file = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                stdout=out, stderr=out)
        to_pipe = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            with mock.patch.dict(os.environ, {"TMPDIR": str(self.root)}):
                payload = self._probe_with_fake_smi(
                    [["GPU-A", to_file.pid, "python", 512],
                     ["GPU-A", to_pipe.pid, "python", 256]], "progress-bin")
        finally:
            for child in (to_file, to_pipe):
                child.terminate()
                child.wait(timeout=2)
            to_pipe.stdout.close()
        by_pid = {row["pid"]: row for row in payload["gpus"][0]["processes"]}
        self.assertNotIn("progress", by_pid[to_file.pid])
        self.assertNotIn("progress", by_pid[to_pipe.pid])
        cache_dir = self.root / ("hearting-%d" % os.geteuid())
        self.assertFalse(cache_dir.exists())

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
        gpu_pid_file = self.root / "claimed-gpu.pid"
        process = subprocess.Popen(
            [sys.executable, "-c", "import pathlib, subprocess, sys; "
             "child = subprocess.Popen([sys.executable, '-c', "
             "'import time; time.sleep(10)']); "
             "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); child.wait()",
             str(gpu_pid_file)],
            env={**clean, "CLAUDE_CODE_SESSION_ID": "previous-session"},
            start_new_session=True,
        )
        try:
            for _ in range(100):
                if gpu_pid_file.is_file():
                    break
                time.sleep(0.01)
            self.assertTrue(gpu_pid_file.is_file())
            gpu_pid = int(gpu_pid_file.read_text())
            claimed = self.run_tool(
                "claim", "here", str(process.pid), "--harness", "codex",
                "--session", "f11a0486-c090-4098-aeb0-0fd6d79f8d0c", "--json",
            )
            self.assertEqual(claimed.returncode, 0, claimed.stderr)
            claim = json.loads(claimed.stdout)
            self.assertEqual(claim["root_pid"], process.pid)
            self.assertEqual(claim["owner"]["label"], "codex:f11a0486")
            # Without --harness/--session the launcher's own session is the owner.
            keys = {key for key, _harness in load_module().SESSION_ENV_KEYS}
            bare = {k: v for k, v in self.env.items() if k not in keys}

            def claim_with(env):
                return subprocess.run([sys.executable, str(TOOL), "claim", "here", str(process.pid), "--json"],
                                      text=True, capture_output=True, env=env)
            defaulted = claim_with({**bare, "CODEX_THREAD_ID": "f11a0486-c090-4098-aeb0-0fd6d79f8d0c"})
            self.assertEqual(defaulted.returncode, 0, defaulted.stderr)
            self.assertEqual(json.loads(defaulted.stdout)["owner"]["label"], "codex:f11a0486")
            alone = claim_with(bare)
            self.assertNotEqual(alone.returncode, 0)
            self.assertIn("--session", alone.stderr)

            env = {
                **clean,
                "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""),
                "CLAIMED_GPU_PID": str(gpu_pid),
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
            reused_claim = {**claim, "root_start": claim["root_start"] + 1}
            reused_result = subprocess.run(
                ["bash", "-c", module.PROBE_SCRIPT], text=True, capture_output=True,
                env={**env, "HEARTING_OWNER_CLAIMS_JSON": json.dumps([reused_claim])},
                timeout=5,
            )
            self.assertEqual(reused_result.returncode, 0, reused_result.stderr)
            reused_payload = json.loads(reused_result.stdout)
            conflicting_claim = {**claim, "owner": {
                "kind": "session", "harness": "claude", "id": "other-session",
                "label": "claude:other-se",
            }}
            conflict_result = subprocess.run(
                ["bash", "-c", module.PROBE_SCRIPT], text=True, capture_output=True,
                env={**env, "HEARTING_OWNER_CLAIMS_JSON": json.dumps(
                    [claim, conflicting_claim])}, timeout=5,
            )
            self.assertEqual(conflict_result.returncode, 0, conflict_result.stderr)
            conflict_payload = json.loads(conflict_result.stdout)
            child_claim_result = self.run_tool(
                "claim", "here", str(gpu_pid), "--harness", "claude",
                "--session", "different-session", "--json",
            )
            self.assertEqual(child_claim_result.returncode, 0,
                             child_claim_result.stderr)
            child_claim = json.loads(child_claim_result.stdout)
            ancestry_result = subprocess.run(
                ["bash", "-c", module.PROBE_SCRIPT], text=True, capture_output=True,
                env={**env, "HEARTING_OWNER_CLAIMS_JSON": json.dumps(
                    [claim, child_claim])}, timeout=5,
            )
            self.assertEqual(ancestry_result.returncode, 0, ancestry_result.stderr)
            ancestry_payload = json.loads(ancestry_result.stdout)
        finally:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)

        owner = payload["gpus"][0]["processes"][0]["owner"]
        self.assertEqual(owner["kind"], "session")
        self.assertEqual(owner["label"], "codex:f11a0486")
        self.assertEqual(owner["source"], "persistent-claim+ancestry")
        self.assertEqual(payload["gpus"][0]["processes"][0]["session_owner"], owner)
        for rejected in (stale_payload, reused_payload):
            rejected_owner = rejected["gpus"][0]["processes"][0]["owner"]
            if rejected_owner is not None:
                self.assertNotEqual(rejected_owner["source"], "persistent-claim+ancestry")
        for conflict in (conflict_payload, ancestry_payload):
            conflict_process = conflict["gpus"][0]["processes"][0]
            self.assertIsNone(conflict_process["owner"])
            self.assertEqual(conflict_process["attribution_reason"],
                             "ambiguous-session")

    def test_stop_records_sigterm_exit_and_reason(self):
        # `stop` kills the tmux session before the run shell can write its
        # own exit code; the stop itself must leave 143 + a reason behind.
        result = self.run_tool("run", "here", "--name", "stopme", "--",
                               "bash", "-c", "sleep 30")
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id = result.stdout.split()[1]
        try:
            for _ in range(50):
                check = subprocess.run(
                    ["tmux", "has-session", "-t", run_id],
                    capture_output=True, timeout=5)
                if check.returncode == 0:
                    break
                time.sleep(0.1)
            stopped = self.run_tool("stop", run_id)
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            self.assertIn("stopped", stopped.stdout)
            run_dir = self.run_root / run_id
            for _ in range(50):
                if (run_dir / "exit_code").is_file():
                    break
                time.sleep(0.1)
            self.assertEqual(
                (run_dir / "exit_code").read_text(encoding="utf-8").strip(),
                "143")
            self.assertEqual(
                (run_dir / "stop_reason").read_text(encoding="utf-8").strip(),
                "stopped")
            listed = self.run_tool("runs")
            self.assertIn("exit 143 (stopped)", listed.stdout)
            payload = json.loads(self.run_tool("runs", "--json").stdout)
            row = next(item for item in payload if item["run_id"] == run_id)
            self.assertEqual(row["exit_code"], 143)
            self.assertEqual(row["stop_reason"], "stopped")
            self.assertEqual(row["state"], "finished")
            tail = self.run_tool("tail", run_id)
            self.assertIn("exit 143 (stopped)", tail.stdout)
        finally:
            subprocess.run(["tmux", "kill-session", "-t", run_id],
                           capture_output=True, timeout=5)

    def test_stop_never_overwrites_a_natural_finish(self):
        run_dir = self.run_root / "natural-20261002-000001-done"
        run_dir.mkdir(parents=True)
        (run_dir / "meta.json").write_text(
            json.dumps({"run_id": run_dir.name, "host": "here",
                        "command": ["true"]}), encoding="utf-8")
        (run_dir / "exit_code").write_text("0\n", encoding="utf-8")
        stopped = self.run_tool("stop", run_dir.name)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(
            (run_dir / "exit_code").read_text(encoding="utf-8").strip(), "0")
        self.assertFalse((run_dir / "stop_reason").exists())

    def test_runs_marks_a_gone_session_stopped_without_writing(self):
        # Legacy runs stopped before this fix have no exit file and no
        # tmux session left: `runs` must show them cleaned, read-only.
        run_dir = self.run_root / "legacy-20261002-000001-stale"
        run_dir.mkdir(parents=True)
        (run_dir / "meta.json").write_text(
            json.dumps({"run_id": run_dir.name, "host": "here",
                        "command": ["bash", "-c", "sleep 30"]}),
            encoding="utf-8")
        listed = self.run_tool("runs")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("stopped (gone)", listed.stdout)
        self.assertFalse((run_dir / "exit_code").exists())
        payload = json.loads(self.run_tool("runs", "--json").stdout)
        row = next(item for item in payload if item["run_id"] == run_dir.name)
        self.assertIsNone(row["exit_code"])
        self.assertEqual(row["state"], "stopped")
        self.assertTrue(row["stale"])
        # An explicit stop then seals the legacy run the same way as new ones.
        stopped = self.run_tool("stop", run_dir.name)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(
            (run_dir / "exit_code").read_text(encoding="utf-8").strip(), "143")
        self.assertEqual(
            (run_dir / "stop_reason").read_text(encoding="utf-8").strip(),
            "stopped")

    def test_runs_stays_running_when_session_state_is_unknown(self):
        module = load_module()
        run_dir = self.run_root / "unknown-20261002-000001-live"
        run_dir.mkdir(parents=True)
        (run_dir / "meta.json").write_text(
            json.dumps({"run_id": run_dir.name, "host": "here",
                        "command": ["bash", "-c", "sleep 30"]}),
            encoding="utf-8")
        with mock.patch.object(module, "_tmux_session_alive", return_value=None):
            config = module.load_config(str(self.config))
            state = module._run_state(config, run_dir.name)
            self.assertEqual(state["state"], "running")
            self.assertIsNone(module._tmux_session_alive(object(), "x"))
        listed = self.run_tool("runs", "--host", "here")
        # With real tmux present and no such session this row shows stale;
        # the unit point above is that unknown (None) never declares gone.
        self.assertEqual(listed.returncode, 0, listed.stderr)

    def test_record_stop_writes_once_and_keeps_a_natural_exit(self):
        module = load_module()
        run_dir = self.root / "record-once"
        self.assertTrue(module._record_stop(run_dir))
        self.assertEqual((run_dir / "exit_code").read_text(
            encoding="utf-8").strip(), "143")
        self.assertEqual((run_dir / "stop_reason").read_text(
            encoding="utf-8").strip(), "stopped")
        self.assertFalse(module._record_stop(run_dir))
        natural = self.root / "record-natural"
        natural.mkdir(parents=True)
        (natural / "exit_code").write_text("3\n", encoding="utf-8")
        self.assertFalse(module._record_stop(natural))
        self.assertEqual((natural / "exit_code").read_text(
            encoding="utf-8").strip(), "3")
        self.assertFalse((natural / "stop_reason").exists())


class LauncherProvenanceTest(unittest.TestCase):
    """The launcher forwards runtime-known provenance into detached payloads.

    Regression cover for the 2026-10-06 BC_ResNet eval loss
    (`moving4-20261006-102542`): the payload's guard read exactly
    `AGENT_HOME` and `AGENT_DISPATCH_ATTEMPT_ID` with strict lookups and died
    with `KeyError` before the first inference, while the launch receipt
    (`env=null`, no `AGENT_*`) shows the launcher forwarded neither. The
    launcher must now inject what it actually knows across the
    local/SSH/tmux/setsid boundary, omit what it cannot validate, and never
    forward credentials, registry paths, or arbitrary env.
    """

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
            "    note: local fixture\n",
            encoding="utf-8")
        environ = mock.patch.dict(os.environ, {"TMPDIR": str(self.root)})
        environ.start()
        self.addCleanup(environ.stop)
        # A run's completion watch notifies through the dispatch state root; keep it here.
        self.env = {**os.environ, "COMPUTE_HOSTS_CONFIG": str(self.config),
                    "HARNESS_STATE_ROOT": str(self.root / "state")}
        self.env.pop("AGENT_DISPATCH_JOBS", None)

    def tearDown(self):
        self.tmp.cleanup()

    def make_home(self, name="fake-home"):
        home = self.root / name
        (home / "core").mkdir(parents=True)
        (home / "core" / "CORE.md").write_text("# fixture root\n", encoding="utf-8")
        return home

    def scrubbed_env(self, **overrides):
        module = load_module()
        session_keys = {key for key, _harness in module.SESSION_ENV_KEYS}
        env = {key: value for key, value in self.env.items()
               if key not in session_keys
               and key not in ("AGENT_HOME", "CLAUDE_HOME",
                               "AGENT_DISPATCH_ATTEMPT_ID", "AGENT_DISPATCH_JOBS")}
        empty = self.root / "empty-home"
        empty.mkdir(exist_ok=True)
        env["HOME"] = str(empty)
        env["XDG_DATA_HOME"] = str(empty / ".local" / "share")
        env.update(overrides)
        return env

    def run_tool(self, *args, env=None):
        return subprocess.run([sys.executable, str(TOOL), *args],
                              text=True, capture_output=True,
                              env=self.env if env is None else env)

    def wait_exit(self, run_id, tries=50):
        exit_path = self.run_root / run_id / "exit_code"
        for _ in range(tries):
            if exit_path.is_file():
                return exit_path
            time.sleep(0.1)
        self.fail(f"no exit code for {run_id}")

    def test_provenance_selection_is_allowlisted_and_validated(self):
        module = load_module()
        home = self.make_home()
        self.assertEqual(
            module._launcher_provenance({
                "AGENT_HOME": str(home),
                "AGENT_DISPATCH_ATTEMPT_ID": "att-9f2c4b1ad34e",
            }),
            {"AGENT_HOME": str(home.resolve()),
             "AGENT_DISPATCH_ATTEMPT_ID": "att-9f2c4b1ad34e"})
        # An unknown attempt shape is omitted, never forwarded or executed.
        self.assertEqual(
            module._launcher_provenance({
                "AGENT_HOME": str(home),
                "AGENT_DISPATCH_ATTEMPT_ID": "bogus; touch /tmp/pwned",
            }),
            {"AGENT_HOME": str(home.resolve())})
        for bad in ("", "att-", "ATT-9f2c", "att-with space",
                    "att-" + "x" * 241, None, 7, ["att-9f2c"]):
            with self.subTest(bad=bad):
                self.assertFalse(module._valid_attempt_id(bad))
        for good in ("att-9f2c4b1ad34e", "att-retry-abc123", "att-x",
                     "att-a.B_c-d"):
            with self.subTest(good=good):
                self.assertTrue(module._valid_attempt_id(good))

    def test_agent_home_prefers_env_then_resolver_then_nothing(self):
        module = load_module()
        home = self.make_home()
        other = self.make_home("other-home")

        def fail():
            raise AssertionError("resolver must not run when env is valid")

        self.assertEqual(
            module._resolve_launcher_agent_home({"AGENT_HOME": str(home)},
                                                _resolver=fail),
            str(home.resolve()))
        # A dangling env root falls through to the resolver, not to fabrication.
        self.assertEqual(
            module._resolve_launcher_agent_home(
                {"AGENT_HOME": str(self.root / "absent")},
                _resolver=lambda: str(other)),
            str(other.resolve()))
        self.assertEqual(
            module._resolve_launcher_agent_home(
                {}, _resolver=lambda: str(other)),
            str(other.resolve()))
        self.assertEqual(
            module._resolve_launcher_agent_home(
                {"CLAUDE_HOME": str(other)}, _resolver=fail),
            str(other.resolve()))
        for resolver in (lambda: str(self.root / "absent"),
                         lambda: "relative/path",
                         lambda: "",
                         lambda: (_ for _ in ()).throw(OSError("denied"))):
            with self.subTest(resolver=resolver):
                self.assertEqual(
                    module._resolve_launcher_agent_home({}, _resolver=resolver),
                    "")

    def test_agent_home_pins_a_pointer_release(self):
        module = load_module()
        real = self.make_home("release-1.2.3")
        link = self.root / "current"
        link.symlink_to(real)

        def fail():
            raise AssertionError("resolver must not run when env is valid")

        self.assertEqual(
            module._resolve_launcher_agent_home({"AGENT_HOME": str(link)},
                                                _resolver=fail),
            str(real.resolve()))

    def test_dry_run_shows_provenance_without_credentials_or_registry_paths(self):
        home = self.make_home()
        env = self.scrubbed_env(
            AGENT_HOME=str(home),
            AGENT_DISPATCH_ATTEMPT_ID="att-9f2c4b1ad34e",
            AWS_SECRET_ACCESS_KEY="canary-secret",
            SSH_AUTH_SOCK="/tmp/canary-agent.sock",
            AGENT_DISPATCH_JOBS="/tmp/canary-jobs.log",
            MY_ARBITRARY="canary-arbitrary",
            CODEX_THREAD_ID="launch-thread")
        result = self.run_tool("run", "here", "--name", "prov", "--dry-run",
                               "--", "bash", "-c", "echo hi", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("export AGENT_HOME=", result.stdout)
        self.assertIn(str(home.resolve()), result.stdout)
        self.assertIn("export AGENT_DISPATCH_ATTEMPT_ID=att-9f2c4b1ad34e",
                      result.stdout)
        # Session identity keeps its existing independent path.
        self.assertIn("export CODEX_THREAD_ID=launch-thread", result.stdout)
        for canary in ("canary-secret", "canary-agent.sock", "canary-jobs.log",
                       "canary-arbitrary", "AWS_SECRET_ACCESS_KEY",
                       "SSH_AUTH_SOCK", "AGENT_DISPATCH_JOBS", "MY_ARBITRARY"):
            self.assertNotIn(canary, result.stdout)

    def test_unknown_provenance_is_empty_and_launch_stays_whole(self):
        env = self.scrubbed_env()
        dry = self.run_tool("run", "here", "--name", "bare", "--dry-run",
                            "--", "bash", "-c", "echo hi", env=env)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("export AGENT_HOME=''", dry.stdout)
        self.assertIn("export AGENT_DISPATCH_ATTEMPT_ID=''", dry.stdout)
        self.assertIn("export HEARTING_COMPUTE_RUN_ID=", dry.stdout)
        result = self.run_tool("run", "here", "--name", "bare", "--",
                               "bash", "-c", "echo survived", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id = result.stdout.split()[1]
        self.wait_exit(run_id)
        self.assertEqual(
            (self.run_root / run_id / "log").read_text(encoding="utf-8").strip(),
            "survived")
        meta = json.loads(
            (self.run_root / run_id / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["provenance"],
                         {"agent_home": None, "attempt_id": None, "session": None,
                          "route": None})
        # The conda-selection field keeps its own meaning beside provenance.
        self.assertIsNone(meta["env"])

    def test_forwarded_provenance_crosses_a_scrubbed_tmux_boundary(self):
        home = self.make_home()
        attempt = "att-9f2c4b1ad34e"
        fakebin = self.root / "tmux-bin"
        fakebin.mkdir()
        tmux = fakebin / "tmux"
        tmux.write_text(
            "#!/bin/sh\n"
            "while [ \"$#\" -gt 1 ]; do shift; done\n"
            "env -u AGENT_HOME -u AGENT_DISPATCH_ATTEMPT_ID "
            "/bin/bash -lc \"$1\"\n",
            encoding="utf-8",
        )
        tmux.chmod(0o755)
        env = self.scrubbed_env(
            AGENT_HOME=str(home), AGENT_DISPATCH_ATTEMPT_ID=attempt,
            PATH=str(fakebin) + os.pathsep + os.environ.get("PATH", ""))
        result = self.run_tool(
            "run", "here", "--name", "prov-cross", "--", "bash", "-c",
            "printf '%s|%s' \"$AGENT_HOME\" \"${AGENT_DISPATCH_ATTEMPT_ID-}\"",
            env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id = result.stdout.split()[1]
        self.wait_exit(run_id)
        # Only the preamble exports could have supplied these: the transport
        # itself started scrubbed.
        self.assertEqual(
            (self.run_root / run_id / "log").read_text(encoding="utf-8"),
            f"{home.resolve()}|{attempt}")
        meta = json.loads(
            (self.run_root / run_id / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["provenance"],
                         {"agent_home": str(home.resolve()),
                          "attempt_id": attempt, "session": None,
                          "route": None})

    def test_invalid_attempt_never_reaches_the_payload(self):
        home = self.make_home()
        env = self.scrubbed_env(
            AGENT_HOME=str(home),
            AGENT_DISPATCH_ATTEMPT_ID="bogus; touch /tmp/prov-pwned")
        dry = self.run_tool("run", "here", "--name", "prov-bad", "--dry-run",
                            "--", "bash", "-c", "echo hi", env=env)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("export AGENT_HOME=", dry.stdout)
        self.assertIn("export AGENT_DISPATCH_ATTEMPT_ID=''", dry.stdout)
        self.assertNotIn("prov-pwned", dry.stdout)

    def test_the_run_record_names_the_launching_route(self):
        module = load_module()
        route = self.root / "route.json"
        route.write_text(json.dumps({"route_id": "rt-0123456789abcdef"}))
        self.assertEqual(module._launcher_route({"AGENT_OWNER_ROUTE_FILE": str(route)}),
                         {"route_id": "rt-0123456789abcdef", "route_file": str(route), "source": "environment"})
        tools = str(Path(__file__).resolve().parents[1] / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        tail = [{"ts": 1, "route_file": "/gone.json"}, {"ts": 2, "route_file": str(route)}]
        with mock.patch("fleet.route_chain.writer_identity", return_value=("claude", "sid-1")), \
             mock.patch("fleet.route_chain.read_tail", return_value=tail):
            found = module._launcher_route({})
        self.assertEqual((found["route_id"], found["source"]), ("rt-0123456789abcdef", "session-route-chain"))
        self.assertIsNone(module._launcher_route({"AGENT_OWNER_ROUTE_FILE": str(self.root / "absent.json")}))

    def test_provenance_keys_are_always_exported(self):
        home = self.make_home()
        env = self.scrubbed_env(
            AGENT_HOME=str(home),
            AGENT_DISPATCH_ATTEMPT_ID="att-9f2c4b1ad34e")
        dry = self.run_tool("run", "here", "--name", "prov-order",
                            "--dry-run", "--", "bash", "-c", "echo hi", env=env)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        setup = dry.stdout.split("setup: ", 1)[1]
        self.assertIn("export AGENT_DISPATCH_ATTEMPT_ID=att-9f2c4b1ad34e", setup)
        self.assertIn("export AGENT_HOME=", setup)
        # Unknown here is the empty string, never an absent key a strict read dies on.
        unknown = self.run_tool("run", "here", "--name", "prov-unknown", "--dry-run", "--",
                                "bash", "-c", "echo hi", env=self.scrubbed_env(AGENT_HOME=str(home)))
        self.assertIn("export AGENT_DISPATCH_ATTEMPT_ID=''", unknown.stdout.split("setup: ", 1)[1])

    def run_with_foreign_transport(self, name, env):
        """Launch through a tmux server that retains unrelated old values.

        A long-lived tmux server keeps the environment of whichever client
        created it; the fixture pins exactly that: foreign provenance the
        launcher never issued.
        """
        fakebin = self.root / name
        fakebin.mkdir()
        tmux = fakebin / "tmux"
        tmux.write_text(
            "#!/bin/sh\n"
            "while [ \"$#\" -gt 1 ]; do shift; done\n"
            "AGENT_HOME=/foreign/stale-home "
            "AGENT_DISPATCH_ATTEMPT_ID=att-unrelated-old-owner "
            "/bin/bash -lc \"$1\"\n",
            encoding="utf-8",
        )
        tmux.chmod(0o755)
        env = {**env, "PATH": str(fakebin) + os.pathsep
               + os.environ.get("PATH", "")}
        result = self.run_tool(
            "run", "here", "--name", name, "--", "bash", "-c",
            "printf '%s|%s' \"$AGENT_HOME\" \"${AGENT_DISPATCH_ATTEMPT_ID-}\"",
            env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id = result.stdout.split()[1]
        self.wait_exit(run_id)
        return run_id

    def test_unknown_provenance_clears_foreign_values(self):
        run_id = self.run_with_foreign_transport(
            "prov-foreign-unknown", self.scrubbed_env())
        self.assertEqual(
            (self.run_root / run_id / "log").read_text(encoding="utf-8"), "|")
        meta = json.loads(
            (self.run_root / run_id / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["provenance"],
                         {"agent_home": None, "attempt_id": None, "session": None,
                          "route": None})

    def test_invalid_attempt_clears_foreign_attempt_only(self):
        home = self.make_home()
        run_id = self.run_with_foreign_transport(
            "prov-foreign-invalid",
            self.scrubbed_env(AGENT_HOME=str(home),
                              AGENT_DISPATCH_ATTEMPT_ID="bogus-value"))
        self.assertEqual(
            (self.run_root / run_id / "log").read_text(encoding="utf-8"),
            f"{home.resolve()}|")

    def test_known_provenance_overrides_foreign_values(self):
        home = self.make_home()
        attempt = "att-9f2c4b1ad34e"
        run_id = self.run_with_foreign_transport(
            "prov-foreign-known",
            self.scrubbed_env(AGENT_HOME=str(home),
                              AGENT_DISPATCH_ATTEMPT_ID=attempt))
        self.assertEqual(
            (self.run_root / run_id / "log").read_text(encoding="utf-8"),
            f"{home.resolve()}|{attempt}")


def probe_namespace():
    """The probe script's helper functions, without running its collection body."""
    source = load_module().PROBE_SCRIPT.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    namespace = {}
    exec(compile(source.split("\ncpu_count = os.cpu_count()", 1)[0],
                 "<probe-helpers>", "exec"), namespace)
    return namespace


class CompletionWatchTest(unittest.TestCase):
    """A run started from an interactive session tells that session once when it ends."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.run_dir = self.root / "runs" / "here-1"
        self.run_dir.mkdir(parents=True)
        self.meta = {"run_id": "here-1", "host": "here",
                     "provenance": {"session": {"harness": "claude", "id": "sid-7"},
                                    "route": {"route_id": "rt-0123456789abcdef"}}}
        (self.run_dir / "meta.json").write_text(json.dumps(self.meta))

    def test_only_an_interactive_session_launch_is_watched(self):
        module = load_module()
        spawned = []
        spawn = lambda argv, **kw: spawned.append(argv)
        self.assertTrue(module._spawn_completion_watch(self.run_dir, self.meta, environ={}, spawn=spawn))
        self.assertEqual(spawned[0][2:], ["watch-run", str(self.run_dir)])
        empty = self.root / "jobs.log"
        empty.write_text("")
        self.assertFalse(module._spawn_completion_watch(self.run_dir, self.meta, spawn=spawn, environ={
            "AGENT_DISPATCH_DEPTH": "1", "AGENT_DISPATCH_JOBS": str(empty)}))      # no owner row: nobody to tell
        no_session = {**self.meta, "provenance": {"session": None}}
        self.assertFalse(module._spawn_completion_watch(self.run_dir, no_session, environ={}, spawn=spawn))
        self.assertEqual(len(spawned), 1)

    def test_the_watch_waits_for_the_exit_code_and_leaves_one_notice(self):
        module = load_module()
        clock, slept = [0.0], []

        def sleep(seconds):
            slept.append(seconds)
            clock[0] += seconds
            (self.run_dir / "exit_code").write_text("3\n")

        state = self.root / "state"
        with mock.patch.dict(os.environ, {"HARNESS_STATE_ROOT": str(state)}):
            os.environ.pop("AGENT_DISPATCH_JOBS", None)
            module.cmd_watch_run(SimpleNamespace(run_dir=str(self.run_dir)), sleep=sleep, now=lambda: clock[0])
            utilities = str(Path(module.__file__).resolve().parent)
            sys.path.insert(0, utilities)
            import dispatch_session_sweep as sweep
            claimed, _ = sweep.sweep_deliver(state / "dispatch", "claude-parent-runtime", "sid-7")
        self.assertEqual(slept, [module.COMPLETION_POLL_SECONDS])
        self.assertEqual(len(claimed), 1)
        text = sweep.delivery_context([(state / "dispatch", claimed)])
        self.assertIn("run here-1 on here ended with exit 3; read it with: compute-hosts tail here-1", text)
        self.assertEqual(claimed[0]["route_id"], "rt-0123456789abcdef")

    def test_a_run_a_worker_starts_in_a_route_tells_the_routes_parent_session(self):
        module = load_module()
        jobs = self.root / "jobs.log"
        jobs.write_text("now\tdone\t1\tparent\towner\tworker_type=owner,owner_route_id=rt-0123456789abcdef,"
                        "parent_harness=claude,parent_sid=sid-parent\n")
        meta = {**self.meta, "provenance": {**self.meta["provenance"], "attempt_id": "att-worker-1",
                                           "session": {"harness": "claude", "id": "sid-worker"}}}
        spawned = []
        self.assertTrue(module._spawn_completion_watch(self.run_dir, meta, spawn=lambda argv, **kw: spawned.append(argv),
                                                       environ={"AGENT_DISPATCH_DEPTH": "2", "AGENT_DISPATCH_JOBS": str(jobs)}))
        recorded = json.loads((self.run_dir / "meta.json").read_text())
        self.assertEqual(recorded["provenance"]["notify"], {"harness": "claude", "id": "sid-parent"})
        (self.run_dir / "exit_code").write_text("0\n")
        state = self.root / "state"
        with mock.patch.dict(os.environ, {"HARNESS_STATE_ROOT": str(state)}):
            os.environ.pop("AGENT_DISPATCH_JOBS", None)
            module.cmd_watch_run(SimpleNamespace(run_dir=str(self.run_dir)), sleep=lambda s: None, now=lambda: 0.0)
            sys.path.insert(0, str(Path(module.__file__).resolve().parent))
            import dispatch_session_sweep as sweep
            mine, _ = sweep.sweep_deliver(state / "dispatch", "claude-parent-runtime", "sid-worker")
            parents, _ = sweep.sweep_deliver(state / "dispatch", "claude-parent-runtime", "sid-parent")
        self.assertEqual((len(mine), len(parents)), (0, 1))          # the parent, not the worker's own session
        self.assertIn("started by attempt att-worker-1 in route rt-0123456789abcdef",
                      sweep.delivery_context([(state / "dispatch", parents)]))

    def test_a_removed_run_ends_the_watch_quietly(self):
        module = load_module()
        import shutil
        shutil.rmtree(self.run_dir)
        self.assertEqual(module.cmd_watch_run(SimpleNamespace(run_dir=str(self.run_dir)),
                                              sleep=lambda s: self.fail("slept"), now=lambda: 0.0), 0)


class ProbeCommandHashTest(unittest.TestCase):
    """Per-GPU process command hash: stable same-EUID read, else None.

    Keep structured command identity even when the terminal shortens argv.
    Unreadable, dead, or recycled pids fail soft.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ns = probe_namespace()

    def spawn(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        self.addCleanup(child.wait, 2)
        self.addCleanup(child.terminate)
        return child

    def test_live_process_hash_matches_cmdline_bytes(self):
        import hashlib

        child = self.spawn()
        start = self.ns["proc_stat"](child.pid)["start"]
        expected = hashlib.sha256(
            Path("/proc/%d/cmdline" % child.pid).read_bytes()).hexdigest()
        self.assertEqual(self.ns["process_command_hash"](child.pid, start), expected)

    def test_dead_pid_and_recycled_start_are_none(self):
        child = self.spawn()
        start = self.ns["proc_stat"](child.pid)["start"]
        child.terminate()
        child.wait(timeout=5)
        self.assertIsNone(self.ns["process_command_hash"](child.pid, start))
        live = self.spawn()
        live_start = self.ns["proc_stat"](live.pid)["start"]
        self.assertIsNone(self.ns["process_command_hash"](live.pid, live_start + 1))


if __name__ == "__main__":
    unittest.main()
