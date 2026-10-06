"""Checks for the cross-host inventory and detached run surface.

The local host mode runs everything in-process, so the whole lifecycle —
launch, log, exit code, listing — is exercised without a network.
"""

import importlib.util
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
        # The probe's epoch cache lives under TMPDIR; keep it inside this fixture.
        environ = mock.patch.dict(os.environ, {"TMPDIR": str(self.root)})
        environ.start()
        self.addCleanup(environ.stop)
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

    def test_gpu_probe_reports_progress_only_for_file_redirected_output(self):
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
            payload = self._probe_with_fake_smi(
                [["GPU-A", to_file.pid, "python", 512],
                 ["GPU-A", to_pipe.pid, "python", 256]], "progress-bin")
        finally:
            for child in (to_file, to_pipe):
                child.terminate()
                child.wait(timeout=2)
            to_pipe.stdout.close()
        by_pid = {row["pid"]: row for row in payload["gpus"][0]["processes"]}
        progress = by_pid[to_file.pid]["progress"]
        self.assertEqual(progress["line"], "TRAIN: 11%|# | 11/100 [00:01<00:09]")
        self.assertIsInstance(progress["age_s"], int)
        self.assertEqual(progress["epoch"], {"n": "3", "of": 10})
        self.assertNotIn("progress", by_pid[to_pipe.pid])
        cache_dir = self.root / ("hearting-%d" % os.geteuid())
        self.assertEqual(cache_dir.stat().st_mode & 0o777, 0o700)
        (cache_file,) = cache_dir.glob("progress-epochs-v1-*.json")
        self.assertEqual(cache_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(json.loads(cache_file.read_text())["files"]), 1)

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


def probe_namespace():
    """The probe script's helper functions, without running its collection body."""
    source = load_module().PROBE_SCRIPT.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    namespace = {}
    exec(compile(source.split("\ncpu_count = os.cpu_count()", 1)[0],
                 "<probe-helpers>", "exec"), namespace)
    return namespace


class ProbeProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ns = probe_namespace()

    def spawn(self, stdout, stderr):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"],
                                 stdout=stdout, stderr=stderr)
        if child.stdout is not None:
            self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait, 2)
        self.addCleanup(child.terminate)
        return child

    def progress(self, child):
        return self.ns["process_progress"](child.pid, self.ns["proc_stat"](child.pid)["start"])

    def test_latest_regular_file_wins_and_tqdm_line_is_clean(self):
        older = self.root / "out.log"
        newer = self.root / "err.log"
        older.write_bytes(b"epoch 1 done\n")
        newer.write_bytes(
            b"loading\n"
            b"TRAIN:  51%|\x1b[33m\xe2\x96\x88\x1b[0m| 10458/20000 [46:49<47:30, 3.3batch/s]\r"
            b"TRAIN:  52%|\x1b[33m\xe2\x96\x88\x1b[0m| 10459/20000 [46:50<47:21,  3.36batch/s]"
            b" , L_se=6.77e-03\x1b]0;title\x07\r\r")
        os.utime(older, (time.time() - 600, time.time() - 600))
        with older.open("ab") as out, newer.open("ab") as err:
            child = self.spawn(out, err)
        progress = self.progress(child)
        self.assertEqual(progress["line"],
                         "TRAIN: 52%|\u2588| 10459/20000 [46:50<47:21, 3.36batch/s] , L_se=6.77e-03")
        self.assertTrue(0 <= progress["age_s"] < 60, progress)

    def test_only_the_tail_of_a_large_file_is_read(self):
        log = self.root / "big.log"
        with log.open("wb") as handle:
            handle.write((b"x" * 1023 + b"\n") * 2048)
            handle.write(b"step 9/10 loss=0.5\n")
        size = log.stat().st_size
        with log.open("ab") as out:
            child = self.spawn(out, subprocess.DEVNULL)
        fdinfo = Path("/proc/%d/fdinfo/1" % child.pid)
        position = fdinfo.read_text().splitlines()[0]
        with mock.patch.object(os, "pread", wraps=os.pread) as pread:
            progress = self.progress(child)
        self.assertEqual(progress["line"], "step 9/10 loss=0.5")
        pread.assert_called_once()
        self.assertEqual(pread.call_args.args[1:], (4096, size - 4096))
        # The trainer's own file offset is never moved by the probe.
        self.assertEqual(fdinfo.read_text().splitlines()[0], position)

    def test_long_line_is_display_bounded(self):
        log = self.root / "long.log"
        log.write_bytes(b"a" * 3000 + b"\n")
        with log.open("ab") as out:
            child = self.spawn(out, out)
        self.assertEqual(self.progress(child)["line"], "a" * 200)

    def test_json_progress_is_parsed_before_display_clipping_without_more_io(self):
        record = {"utc": "2026-10-04T04:46:16Z", "metadata": "x" * 400,
                  "phase": "training-updates", "arm": "baseline",
                  "pid": 123, "attempt": 17808, "successful": 17808}
        raw = json.dumps(record)
        log = self.root / "json.log"
        log.write_text(raw + "\n")
        with log.open("ab") as out:
            child = self.spawn(out, out)
        with mock.patch.object(os, "pread", wraps=os.pread) as pread:
            progress = self.progress(child)
        pread.assert_called_once()
        self.assertEqual(progress["line"], raw[:200])
        self.assertEqual(progress["summary"],
                         "training-updates · baseline · attempt 17808 · successful 17808")
        self.assertNotIn("epoch", progress)

    def test_json_progress_keeps_attempt_and_successful_as_separate_units(self):
        summarize = self.ns["progress_json_summary"]
        self.assertEqual(summarize(b'{"attempt":9}'), "attempt 9")
        self.assertEqual(summarize(b'{"successful":8}'), "successful 8")
        self.assertEqual(
            summarize(b'{"phase":"training-updates","arm":"baseline",'
                      b'"attempt":17808,"successful":17808}'),
            "training-updates · baseline · attempt 17808 · successful 17808")
        self.assertEqual(
            summarize(b'{"attempt":100,"successful":95}'),
            "attempt 100 · successful 95")
        for raw in (b'{"attempt":true}', b'{"attempt":-1}', b'{"attempt":1.5}',
                    b'{"attempt":"3"}', b'{"attempt":1000000000000}'):
            with self.subTest(raw=raw):
                self.assertIsNone(summarize(raw))

    def test_json_progress_unknown_and_partial_records_keep_raw_fallback(self):
        for raw in (b'{"phase":"train","step":', b'{"step":true}',
                    b'{"step":-1}', b'{"step":1.5}', b'{"step":"3"}',
                    b'{"step":1000000000000}', b'{"loss":0.4}',
                    b'{"successful":1}\nnot a progress record', b'[]'):
            with self.subTest(raw=raw):
                self.assertIsNone(self.ns["progress_json_summary"](raw))
        self.assertEqual(self.ns["progress_json_summary"](
            b'{"phase":"train","epoch":0,"global_step":17}'),
            "train · epoch 0 · global_step 17")

    def test_json_progress_control_only_labels_do_not_break_collection(self):
        log = self.root / "control-labels.log"
        log.write_text(json.dumps({"phase": "\x00", "arm": "\u200b",
                                   "successful": 17808}) + "\n")
        with log.open("ab") as out:
            child = self.spawn(out, out)
        self.assertEqual(self.progress(child)["summary"], "successful 17808")
        self.assertEqual(self.ns["progress_json_summary"](
            b'{"phase":"\\u0000","arm":"baseline","step":9}'),
            "baseline · step 9")

    def test_pipe_tty_and_device_outputs_have_no_progress(self):
        piped = self.spawn(subprocess.PIPE, subprocess.DEVNULL)
        self.assertIsNone(self.progress(piped))
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        try:
            tty = self.spawn(slave, slave)
        finally:
            os.close(slave)
        self.assertIsNone(self.progress(tty))

    def test_stale_output_reports_its_age(self):
        log = self.root / "stale.log"
        log.write_bytes(b"TRAIN: 5%|#| 1/20 [00:01<00:19]\n")
        os.utime(log, (time.time() - 900, time.time() - 900))
        with log.open("ab") as out:
            child = self.spawn(out, out)
        self.assertGreaterEqual(self.progress(child)["age_s"], 899)

    def test_read_failures_and_identity_changes_are_fail_soft(self):
        log = self.root / "train.log"
        log.write_bytes(b"step 1\n")
        other = self.root / "other.log"
        other.write_bytes(b"step 2\n")
        with log.open("ab") as out:
            child = self.spawn(out, out)
        start = self.ns["proc_stat"](child.pid)["start"]
        with mock.patch.object(os, "open", side_effect=PermissionError("denied")):
            self.assertIsNone(self.ns["process_progress"](child.pid, start))
        with mock.patch.object(os, "pread", side_effect=OSError("io")):
            self.assertIsNone(self.ns["process_progress"](child.pid, start))
        with mock.patch.object(os, "fstat", return_value=other.stat()):
            self.assertIsNone(self.ns["process_progress"](child.pid, start))
        self.assertIsNone(self.ns["process_progress"](child.pid, start + 1))
        self.ns["same_euid"] = mock.Mock(return_value=False)
        self.assertIsNone(self.ns["process_progress"](child.pid, start))



def bar(desc, done, total, colour=b"33"):
    """One tqdm update as it lands in a redirected log: coloured bar, `\r`-separated."""
    pct = done * 100 // total
    head = (desc + b": ") if desc else b""
    return (head + b"%3d%%|\x1b[" % pct + colour + b"m" + b"\xe2\x96\x88" * (pct // 10)
            + b"\x1b[0m| %d/%d [00:01<00:09, 3.2batch/s] , L_se=4.4e-03\r" % (done, total))


def bars(desc, total, upto=None, colour=b"33"):
    return b"".join(bar(desc, done, total, colour)
                    for done in (0, 1, (upto or total) // 2, upto or total))


class ProbeEpochTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache_dir = self.root / "cache"
        self.cache_dir.mkdir(mode=0o700)
        self.ns = probe_namespace()

    def write(self, name, *parts, mode="wb"):
        path = self.root / name
        with path.open(mode) as handle:
            handle.write(b"".join(parts))
        return path

    def probe(self, log, cache_dir=None):
        """One probe of `log` through a freshly loaded cache: the published epoch."""
        cache = self.ns["epoch_cache_open"](
            str(self.cache_dir) if cache_dir is None else cache_dir or None)
        handle = os.open(log, os.O_RDONLY)
        try:
            opened = os.fstat(handle)
            line = self.ns["progress_last_line"](
                os.pread(handle, 4096, max(0, opened.st_size - 4096)))
            view = self.ns["file_epoch"](handle, opened, line, cache)
        finally:
            os.close(handle)
        self.ns["epoch_cache_save"](cache)
        return view

    def cached(self):
        (path,) = self.cache_dir.glob("progress-epochs-v1-*.json")
        return json.loads(path.read_text())["files"]

    def test_end_of_epoch_lines_count_the_next_epoch_once_its_bar_restarts(self):
        log = self.write(
            "sr.log",
            b"{'config': {'max_epoch': 200, 'num_per_epoch': 20000}}\n",
            bars(b"VALID", 2000, colour=b"31"), b"\n",
            b"[Epoch 0] INIT-valid (156.1s)L_se=9.63e-02 | L_loc=5.73e-01\n",
            bars(b"TRAIN", 20000), b"\n",
            b"2026-10-02 12:16:28.981 | INFO | engine:run:333 - [cfg.yaml]\n",
            b"[Epoch 1] TRAIN (6254.3s)L_se=6.56e-03 | L_loc=1.94e-01\n",
            bars(b"VALID", 2000, colour=b"31"), b"\n",
            b"[Epoch 1] VALID-valid (151.2s)L_se=4.83e-03 | L_loc=3.73e-02\n",
            b"2026-10-02 12:19:00.231 | INFO | engine:run:345 - [cfg.yaml][Epoch   1] LR=2.00e-04\n",
            b"2026-10-02 12:19:02.202 | INFO | util:save_checkpoint:194 - "
            b"New best model at epoch 1 (metric=4.8307e-03)\n",
            b"2026-10-02 12:19:02.300 | DEBUG | util:prune:143 - Pruned old checkpoint: epoch.0019.pth\n",
            bars(b"TRAIN", 20000, upto=12888))
        self.assertEqual(self.probe(log), {"n": "2", "of": 200})
        # Training of epoch 2 ends; its validation runs: the honest reading is "2 done".
        self.write("sr.log", bar(b"TRAIN", 20000, 20000), b"\n",
                   b"[Epoch 2] TRAIN (6307.4s)L_se=4.41e-03 | L_loc=2.68e-02\n",
                   bars(b"VALID", 2000, upto=700, colour=b"31"), mode="ab")
        self.assertEqual(self.probe(log), {"n": "2", "of": 200, "done": True})
        self.write("sr.log", bar(b"VALID", 2000, 2000, b"31"), b"\n",
                   b"[Epoch 2] VALID-valid (149.8s)L_se=4.01e-03\n",
                   b"New best model at epoch 2 (metric=4.0083e-03)\n",
                   bars(b"TRAIN", 20000, upto=40), mode="ab")
        self.assertEqual(self.probe(log), {"n": "3", "of": 200})

    def test_validation_summary_first_still_finds_the_training_bar(self):
        log = self.write(
            "m7.log",
            bars(b"TRAIN", 20000), b"\n", bars(b"VALID", 2000, colour=b"31"), b"\n",
            b"[Epoch 25] VALID-valid (268.0s)L_se=3.26e-03\n",
            b"[Epoch 25] TRAIN (7325.2s)L_se=3.28e-03\n",
            b"[m7.yaml][Epoch  25] LR=9.57e-05\n",
            bars(b"TRAIN", 20000, upto=2456))
        self.assertEqual(self.probe(log), {"n": "26"})

    def test_start_of_epoch_and_in_progress_notations(self):
        cases = {
            "keras": ((b"Epoch 3/100\n", b"  12/625 [>....] - ETA: 1:02 - loss: 0.51\r",
                       b"  13/625 [>....] - ETA: 1:01 - loss: 0.50"), {"n": "3", "of": 100}),
            "tutorial": ((b"Epoch 2/24\n", b"----------\n", b"train Loss: 0.31 Acc: 0.90\n",
                          b"val Loss: 0.20 Acc: 0.93\n", bars(b"", 120, upto=30)),
                         {"n": "2", "of": 24}),
            "postfix": ((b"train: 50%|##| 50/100 [00:01<00:01, epoch=3, loss=0.2]\n",
                         b"UserWarning: something\n"), {"n": "3"}),
            "hf": ((b"  Num Epochs = 3\n",
                    b"{'loss': 0.51, 'grad_norm': 1.2, 'learning_rate': 5e-05, 'epoch': 1.23}\n",
                    bars(b"", 3000, upto=1234)), {"n": "1.23", "of": 3}),
            "mmengine": ((b"Epoch(train)  [3][ 100/1250]  lr: 1.0e-03  eta: 1:00:00  loss: 0.5\n",
                          b"UserWarning: something\n"), {"n": "3"}),
            "lightning-not-last": ((b"Epoch 3/99:  45%|####| 100/220 [00:10<00:12, loss=0.3]\n",
                                    b"UserWarning: something\n"), {"n": "3", "of": 99}),
            "start-word": ((b"[trainer] starting epoch 7, best so far 0.3\n",
                            bars(b"train", 50, upto=10)), {"n": "7"}),
            "fairseq-done": ((b"epoch 004 | loss 4.3 | done\n", bars(b"", 10, upto=2)),
                             {"n": "4", "done": True}),
        }
        for name, (parts, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.probe(self.write(name + ".log", *parts)), expected)

    def test_an_epoch_already_in_the_last_line_is_left_to_the_line(self):
        log = self.write("pl.log", b"Epoch 2: 100%|####| 220/220 [00:20<00:00]\n",
                         b"Epoch 3:  45%|####| 100/220 [00:10<00:12, loss=0.3]")
        self.assertIsNone(self.probe(log))

    def test_non_epoch_words_and_look_back_mentions(self):
        log = self.write(
            "noise.log",
            b"steps per epoch: 625, num_per_epoch=20000, epoch_loss=0.3, warmup_epochs: 5\n",
            b"saved epoch.0019.pth; every epoch 9 a checkpoint\n",
            bars(b"train", 50, upto=10))
        self.assertIsNone(self.probe(log))
        self.write("noise.log", b"\nepochs: 50\n", bars(b"train", 50), b"\n",
                   b"[Epoch 3] train (10.0s) loss=0.12\n",
                   b"Best so far: epoch 1 (loss=0.2)\n",
                   bars(b"train", 50, upto=10), mode="ab")
        self.assertEqual(self.probe(log), {"n": "4", "of": 50})

    def test_large_log_is_read_incrementally_after_its_first_scan(self):
        self.ns.update(EPOCH_CHUNK_BYTES=4096, EPOCH_PROBE_BYTES=8192,
                       EPOCH_FIRST_SCAN_BYTES=64 * 1024)
        noise = b"".join(b"step %06d loss=0.5\n" % i for i in range(5000))   # ~100 KiB
        log = self.write("big.log", b"[Epoch 1] train (9.0s) loss=0.9\n", noise,
                         bars(b"train", 50), b"\n", b"[Epoch 7] train (9.0s) loss=0.2\n",
                         noise[:30000], bars(b"train", 50, upto=20))
        size = log.stat().st_size
        with mock.patch.object(os, "pread", wraps=os.pread) as pread:
            self.assertIsNone(self.probe(log))       # budget spent: not current yet
        # A single bounded header read supplements, rather than widens, the tail scan.
        reads = [(call.args[1], call.args[2]) for call in pread.call_args_list]
        self.assertEqual([length for length, offset in reads if offset == 0], [64 * 1024])
        self.assertEqual(min(offset for _length, offset in reads if offset), size - 64 * 1024)
        self.assertLessEqual(sum(length for length, offset in reads if offset and offset != size - 4096),
                             8192 + 64)
        views = [self.probe(log) for _ in range(12)]
        self.assertEqual(views[-1], {"n": "8"})
        (entry,) = self.cached().values()
        self.assertEqual(entry["off"], size)
        self.write("big.log", bar(b"train", 21, 50), bar(b"train", 22, 50), mode="ab")
        with mock.patch.object(os, "pread", wraps=os.pread) as pread:
            self.assertEqual(self.probe(log), {"n": "8"})
        size = log.stat().st_size
        reads = [(call.args[1], call.args[2]) for call in pread.call_args_list]
        # Besides the existing 4 KiB tail: a 64-byte anchor before the saved offset,
        # then only the newly written bytes.
        self.assertEqual(reads[0], (4096, size - 4096))
        self.assertEqual(reads[1], (64, entry["off"] - 64))
        self.assertEqual(sum(length for length, _offset in reads[2:]), size - entry["off"])

    def test_missing_or_broken_cache_rescans_and_rewrites(self):
        log = self.write("t.log", b"Epoch 4/9\n", bars(b"", 10, upto=3))
        self.assertEqual(self.probe(log), {"n": "4", "of": 9})
        (path,) = self.cache_dir.glob("progress-epochs-v1-*.json")
        for broken in (b"{not json", b"[]", b'{"files": []}',
                       json.dumps({"files": {k: dict(v, off="x") for k, v in self.cached().items()}}).encode(),
                       json.dumps({"files": {k: dict(v, state={"n": "4; rm"}) for k, v in self.cached().items()}}).encode()):
            with self.subTest(broken[:20]):
                path.write_bytes(broken)
                self.assertEqual(self.probe(log), {"n": "4", "of": 9})
                (entry,) = self.cached().values()
                self.assertEqual(entry["state"]["n"], "4")
        path.unlink()
        self.assertEqual(self.probe(log), {"n": "4", "of": 9})

    def head_log(self, name, header, tail=b"Epoch 7\n", upto=10):
        self.ns.update(EPOCH_FIRST_SCAN_BYTES=2048, EPOCH_CHUNK_BYTES=2048)
        return self.write(name, header, b"noise\n" * 12000, tail,
                          bars(b"train", 50, upto=upto))

    def measured_probe(self, log, **kwargs):
        reads = []
        pread = os.pread
        def measured(fd, count, offset):
            raw = pread(fd, count, offset)
            reads.append((offset, count, len(raw)))
            return raw
        with mock.patch.object(os, "pread", side_effect=measured):
            view = self.probe(log, **kwargs)
        return view, reads

    def test_bounded_header_total_does_not_supply_the_current_epoch(self):
        log = self.head_log("head.log", b"Epoch 99\n{'max_epoch': 200}\n")
        view, reads = self.measured_probe(log)
        self.assertEqual(view, {"n": "7", "of": 200})
        head = [row for row in reads if row[0] == 0]
        self.assertEqual(head, [(0, 65536, 65536)])
        self.assertLessEqual(sum(row[2] for row in reads), 65536 + 4096 + 2048)
        view, reads = self.measured_probe(log)
        self.assertEqual(view, {"n": "7", "of": 200})
        self.assertEqual([row for row in reads if row[0] == 0], [])
        self.assertEqual(sum(row[2] for row in reads), 4096 + 64)

    def test_old_cache_without_header_result_is_backfilled_once(self):
        log = self.head_log("old.log", b"max_epoch: 200\n")
        self.assertEqual(self.probe(log), {"n": "7", "of": 200})
        (path,) = self.cache_dir.glob("progress-epochs-v1-*.json")
        old = self.cached()
        (entry,) = old.values()
        entry.pop("head_total")
        entry["state"]["of"] = None
        path.write_text(json.dumps({"files": old}))
        view, reads = self.measured_probe(log)
        self.assertEqual(view, {"n": "7", "of": 200})
        self.assertEqual([row for row in reads if row[0] == 0], [(0, 65536, 65536)])
        again, reads = self.measured_probe(log)
        self.assertEqual(again, view)
        self.assertEqual([row for row in reads if row[0] == 0], [])

    def test_transient_header_read_failure_recovers_on_the_next_normal_probe(self):
        log = self.head_log("transient.log", b"max_epoch: 200\n")
        pread = os.pread
        heads = []
        def fail_header(fd, count, offset):
            if offset == 0 and count == 65536:
                heads.append((offset, count))
                raise OSError("one transient header read failure")
            return pread(fd, count, offset)
        with mock.patch.object(os, "pread", side_effect=fail_header):
            self.assertEqual(self.probe(log), {"n": "7"})
        self.assertEqual(heads, [(0, 65536)])
        (entry,) = self.cached().values()
        self.assertNotIn("head_total", entry)
        view, reads = self.measured_probe(log)
        self.assertEqual(view, {"n": "7", "of": 200})
        self.assertEqual([row for row in reads if row[0] == 0], [(0, 65536, 65536)])
        (entry,) = self.cached().values()
        self.assertEqual(entry["head_total"], 200)
        view, reads = self.measured_probe(log)
        self.assertEqual(view, {"n": "7", "of": 200})
        self.assertEqual([row for row in reads if row[0] == 0], [])
        self.assertEqual(sum(row[2] for row in reads), 4096 + 64)

    def test_no_total_header_is_cached_across_incremental_and_large_gap_reads(self):
        log = self.head_log("negative.log", b"num_per_epoch=20000\nwarmup_epochs: 5\n")
        view, reads = self.measured_probe(log)
        self.assertEqual(view, {"n": "7"})
        self.assertEqual([row for row in reads if row[0] == 0], [(0, 65536, 65536)])
        (entry,) = self.cached().values()
        self.assertIn("head_total", entry)
        self.assertIsNone(entry["head_total"])
        appended = b"\nEpoch 8\n" + bars(b"train", 50, upto=20)
        self.write("negative.log", appended, mode="ab")
        view, reads = self.measured_probe(log)
        self.assertEqual(view, {"n": "8"})
        self.assertEqual([row for row in reads if row[0] == 0], [])
        self.assertEqual(sum(row[2] for row in reads), 4096 + 64 + len(appended))
        self.ns["EPOCH_GAP_BYTES"] = 1024
        self.write("negative.log", b"noise\n" * 1000, b"Epoch 9\n",
                   bars(b"train", 50, upto=30), mode="ab")
        view, reads = self.measured_probe(log)
        self.assertEqual(view, {"n": "9"})
        self.assertEqual([row for row in reads if row[0] == 0], [])

    def test_tail_total_wins_and_invalid_or_ambiguous_header_totals_stay_unknown(self):
        log = self.head_log("tail.log", b"max_epoch: 200\n", b"Epoch 7/300\n")
        self.assertEqual(self.probe(log), {"n": "7", "of": 300})
        self.ns["EPOCH_GAP_BYTES"] = 1024
        self.write("tail.log", b"noise\n" * 1000, b"Epoch 8\n",
                   bars(b"train", 50, upto=20), mode="ab")
        view, reads = self.measured_probe(log)
        self.assertEqual(view, {"n": "8", "of": 300})
        self.assertEqual([row for row in reads if row[0] == 0], [])
        cases = (b"max_epoch: false\n", b"max_epoch: 0\n", b"max_epoch: 200.5\n",
                 b"max_epoch: 200e3\n", b"max_epoch: '200'\n",
                 b"max_epoch: 200\nnum_epochs: 300\n",
                 b"num_per_epoch=20000\nwarmup_epochs: 5\nevery epochs: 8\n")
        for index, header in enumerate(cases):
            with self.subTest(header=header):
                log = self.head_log("invalid%d.log" % index, header)
                self.assertEqual(self.probe(log), {"n": "7"})
                self.assertEqual([row for row in self.measured_probe(log)[1] if row[0] == 0], [])

    def test_bounded_header_never_accepts_a_cut_integer(self):
        log = self.head_log("cut.log", b"x\n" * (65536 // 2 - 7) + b"max_epoch: 200\n")
        self.assertEqual(self.probe(log), {"n": "7"})

    def test_header_result_is_rechecked_after_rotation_truncation_or_anchor_mismatch(self):
        log = self.head_log("generation.log", b"max_epoch: 200\n")
        self.assertEqual(self.probe(log), {"n": "7", "of": 200})
        log = self.head_log("generation.log", b"max_epoch: 300\n", b"Epoch 6\n", upto=11)
        view, reads = self.measured_probe(log)
        self.assertEqual(view, {"n": "6", "of": 300})
        self.assertEqual([row for row in reads if row[0] == 0], [(0, 65536, 65536)])
        self.write("generation.log", b"max_epoch: 400\nEpoch 1\n", bars(b"train", 50, upto=10))
        view, reads = self.measured_probe(log)
        self.assertEqual(view, {"n": "1", "of": 400})
        self.assertEqual(len([row for row in reads if row[0] == 0 and row[1] == log.stat().st_size]), 2)
        log.rename(self.root / "generation.log.old")
        log = self.head_log("generation.log", b"max_epoch: 500\n")
        view, reads = self.measured_probe(log)
        self.assertEqual(view, {"n": "7", "of": 500})
        self.assertEqual([row for row in reads if row[0] == 0], [(0, 65536, 65536)])

    def test_cache_unavailable_does_not_add_header_reads_to_its_existing_tail_budget(self):
        log = self.head_log("uncached.log", b"max_epoch: 200\n")
        self.ns["EPOCH_NOCACHE_BYTES"] = 2048
        for _probe in range(2):
            view, reads = self.measured_probe(log, cache_dir=False)
            self.assertEqual(view, {"n": "7"})
            self.assertEqual([row for row in reads if row[0] == 0], [])
            self.assertEqual(sum(row[2] for row in reads), 4096 + 2048)

    def test_unsafe_or_missing_cache_dir_reads_only_a_small_tail(self):
        self.ns["EPOCH_NOCACHE_BYTES"] = 2048
        log = self.write("t.log", b"Epoch 5/9\n", b"x" * 4096 + b"\n", b"Epoch 6/9\n",
                         bars(b"", 10, upto=3))
        with mock.patch.object(os, "pread", wraps=os.pread) as pread:
            self.assertEqual(self.probe(log, cache_dir=False), {"n": "6", "of": 9})
        self.assertGreaterEqual(min(call.args[2] for call in pread.call_args_list),
                                log.stat().st_size - 4096)
        self.assertEqual(list(self.cache_dir.iterdir()), [])
        shared = self.root / ("hearting-%d" % os.geteuid())
        shared.mkdir(mode=0o700)
        shared.chmod(0o777)
        with mock.patch.dict(os.environ, {"TMPDIR": str(self.root)}):
            self.assertIsNone(self.ns["epoch_cache_dir"]())
            shared.rmdir()
            shared.symlink_to(self.cache_dir)
            self.assertIsNone(self.ns["epoch_cache_dir"]())
            shared.unlink()
            self.assertEqual(self.ns["epoch_cache_dir"](), str(shared))
            self.assertEqual(shared.stat().st_mode & 0o777, 0o700)

    def test_rotated_or_rewritten_files_never_reuse_old_state(self):
        log = self.write("r.log", bars(b"train", 50), b"\n",
                         b"[Epoch 7] train (9.0s) loss=0.2\n", bars(b"train", 50, upto=10))
        self.assertEqual(self.probe(log), {"n": "8"})
        # Rotation: a new file (new inode) at the same path starts from its own content.
        log.rename(self.root / "r.log.1")
        log = self.write("r.log", b"resumed\n", bars(b"train", 50, upto=10))
        self.assertIsNone(self.probe(log))
        self.assertEqual(len(self.cached()), 2)
        # Same inode rewritten past the old offset: the content anchor no longer matches.
        self.write("r.log", b"Epoch 2/5\n", b"y" * 600 + b"\n", bars(b"train", 50, upto=10))
        self.assertEqual(self.probe(log), {"n": "2", "of": 5})
        self.write("r.log", b"Epoch 1/5\n", mode="wb")
        self.write("r.log", bars(b"train", 50, upto=3), mode="ab")
        self.assertEqual(self.probe(log), {"n": "1", "of": 5})

    def spawn(self, log):
        with log.open("ab") as out:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"],
                                     stdout=out, stderr=out)
        self.addCleanup(child.wait, 2)
        self.addCleanup(child.terminate)
        return child

    def test_epoch_belongs_to_the_output_file_not_the_pid(self):
        first, second = self.root / "a.log", self.root / "b.log"
        first.write_bytes(b"Epoch 3/10\n" + bars(b"", 10, upto=4))
        second.write_bytes(b"Epoch 8/10\n" + bars(b"", 10, upto=4))
        cache = self.ns["epoch_cache_open"](str(self.cache_dir))
        old = self.spawn(first)
        self.assertEqual(self.ns["process_progress"](
            old.pid, self.ns["proc_stat"](old.pid)["start"], cache)["epoch"],
            {"n": "3", "of": 10})
        old.terminate()
        old.wait(2)
        new = self.spawn(second)
        progress = self.ns["process_progress"](
            new.pid, self.ns["proc_stat"](new.pid)["start"], cache)
        self.assertEqual(progress["epoch"], {"n": "8", "of": 10})
        # A start time that no longer matches its pid yields nothing at all.
        self.assertIsNone(self.ns["process_progress"](
            new.pid, self.ns["proc_stat"](new.pid)["start"] + 1, cache))

    def test_processes_sharing_one_log_scan_it_once_per_probe(self):
        log = self.root / "ddp.log"
        log.write_bytes(b"Epoch 2/4\n" + bars(b"", 10, upto=4))
        ranks = [self.spawn(log), self.spawn(log)]
        cache = self.ns["epoch_cache_open"](str(self.cache_dir))
        scan = self.ns["epoch_scan_file"]
        self.ns["epoch_scan_file"] = mock.Mock(wraps=scan)
        views = [self.ns["process_progress"](
            child.pid, self.ns["proc_stat"](child.pid)["start"], cache)["epoch"]
            for child in ranks]
        self.assertEqual(views, [{"n": "2", "of": 4}] * 2)
        self.ns["epoch_scan_file"].assert_called_once()

    def test_epoch_failures_never_cost_the_progress_line(self):
        log = self.root / "f.log"
        log.write_bytes(b"Epoch 2/4\n" + bars(b"", 10, upto=4))
        child = self.spawn(log)
        start = self.ns["proc_stat"](child.pid)["start"]
        cache = self.ns["epoch_cache_open"](str(self.cache_dir))
        self.ns["epoch_scan_file"] = mock.Mock(side_effect=RuntimeError("bug"))
        progress = self.ns["process_progress"](child.pid, start, cache)
        self.assertNotIn("epoch", progress)
        self.assertTrue(progress["line"].endswith("L_se=4.4e-03"))
        # A failing cache write is skipped silently.
        cache = self.ns["epoch_cache_open"](str(self.cache_dir))
        cache["dirty"] = True
        with mock.patch.object(os, "replace", side_effect=OSError("read-only")):
            self.ns["epoch_cache_save"](cache)
        self.assertEqual([path.name for path in self.cache_dir.iterdir()], [])

if __name__ == "__main__":
    unittest.main()
