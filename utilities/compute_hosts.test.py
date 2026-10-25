"""Checks for the cross-host inventory and detached run surface.

The local host mode runs everything in-process, so the whole lifecycle —
launch, log, exit code, listing — is exercised without a network.
"""

import contextlib
import importlib.util
import io
import datetime
import json
import os
import signal
import selectors
import shlex
import shutil
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


def controlled_proc_root(root, processes):
    """Keep live fixture PID data while placing ancestry behind a test root."""
    proc_root = root / "proc"
    proc_root.mkdir()
    fixture_parent_pid = 2_000_000_000
    while (Path("/proc") / str(fixture_parent_pid)).exists():
        fixture_parent_pid -= 1

    parent = proc_root / str(fixture_parent_pid)
    parent.mkdir()
    parent_fields = ["S", "0", str(os.getpgrp())] + ["0"] * 17
    parent_fields[19] = "1"
    (parent / "stat").write_text(
        f"{fixture_parent_pid} (probe-fixture-root) {' '.join(parent_fields)}\n",
        encoding="utf-8",
    )
    uid = os.geteuid()
    (parent / "status").write_text(
        f"Name:\tprobe-fixture-root\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n",
        encoding="utf-8",
    )
    (parent / "environ").write_bytes(b"")
    (parent / "cmdline").write_bytes(b"")
    (parent / "comm").write_text("probe-fixture-root\n", encoding="utf-8")

    for process in processes:
        live = Path("/proc") / str(process.pid)
        raw_stat = (live / "stat").read_text(encoding="utf-8")
        prefix, suffix = raw_stat.rsplit(")", 1)
        fields = suffix.split()
        fields[1] = str(fixture_parent_pid)
        fixture = proc_root / str(process.pid)
        fixture.mkdir()
        (fixture / "stat").write_text(
            prefix + ") " + " ".join(fields) + "\n", encoding="utf-8")
        for name in ("status", "environ", "cmdline", "comm"):
            (fixture / name).symlink_to(live / name)
    return proc_root


def terminate_and_reap(process, timeout=2):
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait(timeout=timeout)
    if process.stdout is not None:
        process.stdout.close()


def load_module():
    spec = importlib.util.spec_from_file_location("compute_hosts", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_fake_tmux(fakebin, *, stale_environment=None):
    fakebin.mkdir(parents=True, exist_ok=True)
    tmux = fakebin / "tmux"
    lines = ["#!/bin/sh"]
    for key, value in (stale_environment or {}).items():
        lines.append(f"export {key}={shlex.quote(value)}")
    lines.extend([
        'if [ "$1" = "-L" ]; then shift 2; fi',
        'if [ "$1" = "start-server" ]; then exit 0; fi',
        '[ "$1" = "new-session" ] || exit 1',
        "shift",
        "cwd=/",
        'while [ "$#" -gt 0 ]; do',
        '  case "$1" in',
        '    -d) shift ;;',
        '    -s) run_id=$2; shift 2 ;;',
        '    -c) cwd=$2; shift 2 ;;',
        '    -e) echo "tmux: unknown option -- e" >&2; exit 1 ;;',
        '    *) command=$1; break ;;',
        "  esac",
        "done",
        'cd "$cwd" || exit 1',
        'exec /bin/sh -c "$command"',
    ])
    tmux.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmux.chmod(0o755)
    return tmux


class ComputeRunLauncherIdentityTest(unittest.TestCase):
    def test_registered_empty_codex_uses_parent_for_payload_lease_provenance_and_notify(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "dispatch" / "jobs.log"
            jobs.parent.mkdir(parents=True)
            attempt = "att-compute-owner"
            route_id = "rt-compute-owner"
            metadata = ",".join(("attempt_id=" + attempt, "worker_type=owner",
                "dispatch_depth=1", "owner_route_id=" + route_id,
                "owner_route_hash=sha256:compute-owner", "parent_sid=route-parent"))
            jobs.write_text("\t".join(("2099-01-01T00:00:00Z", "done", "/work", "/work",
                                      "owner", metadata)) + "\n", encoding="utf-8")
            route_state = root / "route-chains"
            parent_ledger = route_state / "claude" / "route-parent.jsonl"
            parent_ledger.parent.mkdir(parents=True)
            parent_ledger.write_text('{"event":"compose","sid":"route-parent"}\n', encoding="utf-8")
            run_root = root / "runs"
            observation = {"reachable": True, "gpus": [{"index": 0, "uuid": "GPU-zero",
                "name": "NVIDIA Test GPU", "utilization_gpu_pct": 0,
                "memory_used_mib": 0, "memory_total_mib": 24000, "processes": [],
                "reservations": []}]}
            receipt = {"gpus": "0", "gpu_lease": {"token": "remote-lease"}}
            remote = mock.Mock(return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(receipt) + "\n", stderr=""))
            args = SimpleNamespace(host="here", command=["python", "-c", "pass"],
                name="resolver-fixture", cwd=None, env=None, gpus=None, share=False,
                dry_run=False, json=True)
            with mock.patch.dict(os.environ, {
                    "AGENT_HOME": str(ROOT), "AGENT_DISPATCH_JOBS": str(jobs),
                    "AGENT_DISPATCH_ATTEMPT_ID": attempt, "AGENT_DISPATCH_DEPTH": "1",
                    "AGENT_DISPATCH_CALLER_HARNESS": "codex", "CODEX_THREAD_ID": "",
                    "CLAUDE_CODE_SESSION_ID": "inherited-worker-session",
                    "XDG_STATE_HOME": str(root / "state-home"),
                    "FLEET_ROUTE_CHAIN_DIR": str(route_state)}, clear=False), \
                 mock.patch.object(module, "load_config", return_value={"run_root": run_root}), \
                 mock.patch.object(module, "_select", return_value=[("here", {"ssh_host": "gpu.invalid"})]), \
                 mock.patch.object(module, "_run_gpu_observation", return_value=observation), \
                 mock.patch.object(module, "_launcher_provenance", return_value={
                     "AGENT_HOME": str(ROOT), "AGENT_DISPATCH_ATTEMPT_ID": attempt}), \
                 mock.patch.object(module, "_launcher_route", return_value={"route_id": route_id}), \
                 mock.patch.object(module, "remote", remote), \
                 mock.patch.object(module, "_spawn_completion_watch", return_value=False), \
                 mock.patch("sys.stdout"):
                self.assertEqual(module.cmd_run(args), 0)

            launched = remote.call_args.args[1]
            self.assertIn("export CLAUDE_CODE_SESSION_ID=route-parent", launched)
            script = shlex.split(launched)[2]
            self.assertIn("'owner': {'kind': 'session', 'harness': 'claude', 'id': 'route-parent'", script)
            run_dirs = list(run_root.iterdir())
            self.assertEqual(len(run_dirs), 1)
            recorded = json.loads((run_dirs[0] / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(recorded["provenance"]["session"],
                             {"harness": "claude", "id": "route-parent"})
            self.assertEqual(recorded["provenance"]["notify"],
                             {"harness": "claude", "id": "route-parent"})

    def test_resource_parent_launch_keeps_payload_provenance_and_share_contract(self):
        import gpu_leases

        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_root = root / "runs"
            route_file = root / "route.json"
            route_file.write_text("{}\n", encoding="utf-8")
            registry = root / "resource-runs.json"
            registry.write_text("{}\n", encoding="utf-8")
            route_id, route_hash = "rt-resource-parent", "sha256:resource-parent"
            route = {"route_id": route_id, "route_hash": route_hash,
                     "route_file": str(route_file)}
            parent_contract = {"route_id": route_id, "route_file": str(route_file),
                               "route_hash": route_hash, "run_id": "parent-run",
                               "node": "full-run"}
            parent = {"run_id": "parent-run", "registry": str(registry),
                      "route_id": route_id, "route_file": str(route_file),
                      "route_hash": route_hash, "contract": parent_contract}
            payload_home = str(root / "payload-home")
            provenance = {"AGENT_HOME": str(root / "agent-home"),
                          "AGENT_DISPATCH_ATTEMPT_ID": "att-parent"}
            args = SimpleNamespace(host="here", command=["python", "-c", "pass"],
                name="resource-child", cwd=None, env=None, gpus=None, share=True,
                dry_run=False, json=True)
            receipt = {"gpus": "", "gpu_lease": None,
                       "tmux_transport": {"kind": "nohup-v1"}}
            remote = mock.Mock(return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(receipt) + "\n", stderr=""))
            with mock.patch.object(module, "load_config", return_value={
                    "run_root": run_root, "hosts": {}}), \
                 mock.patch.object(module, "_refuse_namespace_self"), \
                 mock.patch.object(module, "_select", return_value=[
                     ("here", {"ssh_host": "local"})]), \
                 mock.patch.object(module, "_run_gpu_observation", return_value={
                     "reachable": True, "gpus": []}), \
                 mock.patch.object(module, "_launcher_provenance", return_value=provenance), \
                 mock.patch.object(module, "_launcher_session_owner", return_value=None), \
                 mock.patch.object(module, "_launcher_route", return_value=route), \
                 mock.patch.object(module, "_resource_parent", return_value=parent), \
                 mock.patch.object(module, "_select_payload_agent_home", return_value=(
                     payload_home, "published", "fixture")), \
                 mock.patch.object(module, "remote", remote), \
                 mock.patch.object(module, "_spawn_completion_watch", return_value=False), \
                 mock.patch.object(gpu_leases, "launcher_session_environment",
                                   return_value={}), \
                 mock.patch.object(gpu_leases, "serialized_compute_source",
                                   return_value="# synthetic compute source\n"), \
                 mock.patch.object(gpu_leases, "serialized_payload_source",
                                   return_value="# synthetic payload source\n"), \
                 mock.patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(module.cmd_run(args), 0)

            (run_dir,) = list(run_root.iterdir())
            meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["provenance"]["resource"], parent)
            self.assertEqual(meta["provenance"]["payload_agent_home"], payload_home)
            self.assertEqual(meta["provenance"]["agent_home_status"], "published")
            self.assertTrue(meta["execution_contract"]["share"])
            self.assertEqual(meta["execution_contract"]["resource_parent"], parent_contract)
            launch_script = shlex.split(remote.call_args.args[1])[2]
            self.assertIn("'HEARTING_RESOURCE_RUN_ID': 'parent-run'", launch_script)


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


class ProbeObservationTest(unittest.TestCase):
    def test_json_snapshot_preserves_probe_failure_without_failing_the_consumer(self):
        module = load_module()
        host = {"ssh_host": "local"}
        config = {"run_root": Path("/unused"), "hosts": {"here": host}}
        output, warning = io.StringIO(), io.StringIO()
        with mock.patch.object(module, "load_config", return_value=config), \
             mock.patch.object(module, "_load_claims", return_value=[]), \
             mock.patch.object(module, "collect_ssh_session_bridges", return_value=[]), \
             mock.patch.object(module, "remote", return_value=subprocess.CompletedProcess(
                 [], 1, "", "GPU query unavailable")), \
             mock.patch("sys.stdout", output), mock.patch("sys.stderr", warning):
            code = module.cmd_probe(SimpleNamespace(hosts=["here"], json=True))
        self.assertEqual(code, 0)
        row, = json.loads(output.getvalue())
        self.assertFalse(row["reachable"])
        self.assertEqual(row["gpus"], [])
        self.assertIn("GPU query unavailable", row["detail"])
        self.assertIn("observation unavailable", warning.getvalue())

    def test_human_probe_and_invalid_selection_still_fail(self):
        module = load_module()
        config = {"run_root": Path("/unused"), "hosts": {"here": {"ssh_host": "local"}}}
        with mock.patch.object(module, "load_config", return_value=config), \
             mock.patch.object(module, "_probe_selected", return_value=[
                 {"host": "here", "reachable": False, "gpus": [], "detail": "unavailable"}]), \
             mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(module.cmd_probe(SimpleNamespace(hosts=["here"], json=False)), 1)
            with self.assertRaises(module.ConfigError):
                module.cmd_probe(SimpleNamespace(hosts=["unknown"], json=True))

    def test_transport_exception_returns_an_unavailable_sample(self):
        module = load_module()
        for error in (OSError("transport unavailable"), subprocess.TimeoutExpired("probe", 1)):
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(module, "remote", side_effect=error):
                row = module.probe_host("gpu", {"ssh_host": "local"}, ssh_session_bridges=[])
            self.assertFalse(row["reachable"])
            self.assertEqual(row["gpus"], [])
            self.assertTrue(row["detail"])


class RunGPUObservationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_root = Path(self.tmp.name) / "runs"
        self.module = load_module()
        self.host = {"ssh_host": "local"}
        self.config = {"run_root": self.run_root, "hosts": {"here": self.host}}

    def session_receipt(self, values):
        import gpu_leases
        args = SimpleNamespace(host="here", command=["true"], name="identity",
                               cwd=None, env=None, gpus=None, dry_run=False, json=True)
        output = io.StringIO()
        with mock.patch.dict(os.environ, values, clear=True), \
                mock.patch.object(gpu_leases, "state_path", return_value=self.run_root.parent / "gpu-leases.json"), \
                mock.patch.object(self.module, "load_config", return_value=self.config), \
                mock.patch.object(self.module, "_run_gpu_observation", return_value={}), \
                mock.patch.object(self.module, "remote", return_value=subprocess.CompletedProcess(
                    [], 0, '{"gpus": null}', "")) as launch, \
                mock.patch.object(self.module, "_launcher_provenance", return_value={}), \
                mock.patch.object(self.module, "_launcher_route", return_value=None), \
                mock.patch.object(self.module, "_spawn_completion_watch", return_value=False), \
                mock.patch("sys.stdout", output):
            self.assertEqual(self.module.cmd_run(args), 0)
        receipt = json.loads(output.getvalue())
        meta = json.loads((self.run_root / receipt["run_id"] / "meta.json").read_text())
        self.assertEqual(meta["provenance"]["session"], receipt["provenance"]["session"])
        return receipt["provenance"]["session"], launch.call_args.args[1]

    def test_clear_alias_conflict_records_and_exports_the_route_writer_session(self):
        tools = str(ROOT.parent / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from fleet import route_chain
        values = {"CLAUDE_CODE_SESSION_ID": "sid-current", "CLAUDE_SESSION_ID": "sid-before-clear"}
        harness, sid = route_chain.writer_identity(values)
        owner, launch = self.session_receipt(values)
        self.assertEqual(owner, {"harness": harness, "id": sid})
        self.assertIn("export CLAUDE_CODE_SESSION_ID=sid-current", launch)
        self.assertNotIn("sid-before-clear", launch)

    def test_launch_without_a_session_keeps_none_and_exports_no_session_id(self):
        owner, launch = self.session_receipt({})
        self.assertIsNone(owner)
        self.assertIn("unset CLAUDE_CODE_SESSION_ID CLAUDE_SESSION_ID", launch)
        for key, _harness in self.module.SESSION_ENV_KEYS:
            self.assertNotIn("export " + key + "=", launch)

    def run_receipt(self, row, *, dry_run=False, json_output=False, cpu=False):
        args = SimpleNamespace(host="here", command=["true"], name="headroom",
                               cwd=None, env=None, gpus="" if cpu else None, dry_run=dry_run,
                               json=json_output)
        output = io.StringIO()
        with mock.patch.object(self.module, "load_config", return_value=self.config), \
                mock.patch.object(self.module, "probe_host", side_effect=row if isinstance(row, Exception)
                                  else None, return_value=row) as probe, \
                mock.patch.object(self.module, "remote", return_value=subprocess.CompletedProcess(
                    [], 0, '{"gpus": "0"}', "")) as launch, \
                mock.patch.object(self.module, "_launcher_provenance", return_value={}), \
                mock.patch.object(self.module, "_launcher_route", return_value=None), \
                mock.patch.object(self.module, "_spawn_completion_watch", return_value=False), \
                mock.patch("sys.stdout", output):
            rc = self.module.cmd_run(args)
        self.assertEqual(rc, 0)
        probe.assert_called_once_with("here", self.host, ssh_session_bridges=[])
        return output.getvalue(), launch

    def run_failed_launch(self, result, *, run_id="launch-failure", record_error=False):
        import gpu_leases
        args = SimpleNamespace(host="here", command=["true"], name=run_id,
                               cwd=None, env=None, gpus="", dry_run=False, json=True)
        output, warning = io.StringIO(), io.StringIO()
        with mock.patch.object(gpu_leases, "state_path", return_value=self.run_root.parent / "leases.json"), \
             mock.patch.object(self.module, "load_config", return_value=self.config), \
             mock.patch.object(self.module, "_run_id", return_value=run_id), \
             mock.patch.object(self.module, "_run_gpu_observation", return_value={"gpus": []}), \
             mock.patch.object(self.module, "_launcher_provenance", return_value={}), \
             mock.patch.object(self.module, "_launcher_route", return_value=None), \
             mock.patch.object(self.module, "_launcher_session_owner", return_value=None), \
             mock.patch.object(self.module, "remote", return_value=result), \
             mock.patch.object(self.module, "_write_launch_failure",
                               side_effect=OSError("diagnostic disk full") if record_error else None,
                               wraps=None if record_error else self.module._write_launch_failure), \
             mock.patch.object(self.module, "_spawn_completion_watch") as watch, \
             mock.patch("sys.stdout", output), mock.patch("sys.stderr", warning):
            try:
                code = self.module.cmd_run(args)
                error = None
            except self.module.ConfigError as exc:
                code, error = None, exc
        return self.run_root / run_id, code, error, output.getvalue(), warning.getvalue(), watch

    def test_failed_launch_persists_bounded_output_and_only_definitive_status(self):
        run_dir, code, error, _out, warning, watch = self.run_failed_launch(
            subprocess.CompletedProcess([], 2, "remote-stdout", "remote-stderr"))
        self.assertEqual(code, 1)
        self.assertIsNone(error)
        self.assertIn("launch.log", warning)
        log = (run_dir / "launch.log").read_text()
        self.assertIn("remote_exit_code: 2", log)
        self.assertIn("classification: remote-failure", log)
        self.assertIn("remote-stdout", log)
        self.assertIn("remote-stderr", log)
        self.assertEqual((run_dir / "exit_code").read_text(), "125\n")
        self.assertEqual((run_dir / "stop_reason").read_text(), "launch-failed\n")
        state = self.module._run_state(self.config, "launch-failure")
        self.assertEqual((state["state"], state["exit_code"], state["stop_reason"]),
                         ("finished", 125, "launch-failed"))
        watch.assert_not_called()

        uncertain_dir, code, error, _out, _warning, watch = self.run_failed_launch(
            subprocess.CompletedProcess([], 255, "partial-out", "ssh down"),
            run_id="launch-uncertain")
        self.assertEqual(code, 1)
        self.assertIsNone(error)
        self.assertTrue((uncertain_dir / "launch.log").is_file())
        self.assertFalse((uncertain_dir / "exit_code").exists())
        self.assertFalse((uncertain_dir / "stop_reason").exists())
        watch.assert_not_called()

        timeout_dir, code, error, _out, _warning, watch = self.run_failed_launch(
            subprocess.CompletedProcess([], 255, "partial timeout stdout",
                                        "partial timeout stderr\ntimed out"),
            run_id="launch-timeout")
        self.assertEqual(code, 1)
        self.assertIsNone(error)
        timeout_log = (timeout_dir / "launch.log").read_text()
        self.assertIn("classification: timeout", timeout_log)
        self.assertIn("partial timeout stdout", timeout_log)
        self.assertIn("partial timeout stderr", timeout_log)
        self.assertFalse((timeout_dir / "exit_code").exists())
        self.assertFalse((timeout_dir / "stop_reason").exists())
        watch.assert_not_called()

    def test_invalid_receipt_shapes_are_logged_without_terminal_status_or_watch(self):
        for index, receipt in enumerate(("", "not-json", "null", "[]", "{}",
                                         '{"gpus": []}')):
            with self.subTest(receipt=receipt):
                run_dir, code, error, _out, _warning, watch = self.run_failed_launch(
                    subprocess.CompletedProcess([], 0, receipt, ""),
                    run_id="receipt-invalid-%d" % index)
                self.assertIsNone(code)
                self.assertRegex(str(error), "invalid GPU launch receipt")
                self.assertIn("host here", str(error))
                self.assertIn(str(run_dir / "launch.log"), str(error))
                self.assertEqual((run_dir / "launch.log").read_text().splitlines()[1],
                                 "classification: receipt-invalid")
                self.assertFalse((run_dir / "exit_code").exists())
                self.assertFalse((run_dir / "stop_reason").exists())
                watch.assert_not_called()

    def test_remote_timeout_keeps_partial_stdout_and_stderr(self):
        error = subprocess.TimeoutExpired(["ssh"], 2, output=b"partial-out", stderr="partial-err")
        with mock.patch.object(self.module, "ssh_prefix", return_value=["ssh"]), \
             mock.patch.object(self.module.subprocess, "run", side_effect=error):
            result = self.module.remote({"ssh_host": "remote"}, "true", timeout=2)
        self.assertEqual(result.returncode, 255)
        self.assertEqual(result.stdout, "partial-out")
        self.assertIn("partial-err", result.stderr)
        self.assertIn("timed out", result.stderr)

    def test_diagnostic_write_error_keeps_original_launch_failure_visible(self):
        run_dir, code, error, _out, warning, watch = self.run_failed_launch(
            subprocess.CompletedProcess([], 2, "remote-out", "original remote error"),
            run_id="recording-error", record_error=True)
        self.assertEqual(code, 1)
        self.assertIsNone(error)
        self.assertIn("original remote error", warning)
        self.assertIn("diagnostic disk full", warning)
        self.assertIn(str(run_dir / "launch.log"), warning)
        self.assertFalse((run_dir / "launch.log").exists())
        watch.assert_not_called()

    def test_launch_failure_output_is_capped_and_existing_terminal_files_survive(self):
        run_dir = self.run_root / "bounded-launch-failure"
        result = subprocess.CompletedProcess([], 2, "o" * (20 * 1024), "e" * (20 * 1024))
        log = self.module._write_launch_failure(run_dir, result, "remote-failure")
        content = log.read_text()
        self.assertLessEqual(len(content.encode("utf-8")), 33 * 1024)
        self.assertIn("...[truncated]", content)
        self.assertEqual((run_dir / "exit_code").read_text(), "125\n")
        self.assertEqual((run_dir / "stop_reason").read_text(), "launch-failed\n")

        existing = self.run_root / "existing-terminal"
        existing.mkdir(parents=True)
        (existing / "exit_code").write_text("0\n")
        (existing / "stop_reason").write_text("completed\n")
        self.module._write_launch_failure(
            existing, subprocess.CompletedProcess([], 3, "out", "err"), "remote-failure")
        self.assertEqual((existing / "exit_code").read_text(), "0\n")
        self.assertEqual((existing / "stop_reason").read_text(), "completed\n")

        existing_reason = self.run_root / "existing-reason"
        existing_reason.mkdir(parents=True)
        (existing_reason / "stop_reason").write_text("path-namespace-unshare\n")
        self.module._write_launch_failure(
            existing_reason, subprocess.CompletedProcess([], 3, "out", "err"), "remote-failure")
        self.assertFalse((existing_reason / "exit_code").exists())
        self.assertEqual((existing_reason / "stop_reason").read_text(), "path-namespace-unshare\n")

    def test_tail_uses_launch_log_only_when_payload_log_is_missing(self):
        run_id = "tail-launch-log"
        run_dir = self.run_root / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "launch.log").write_text("launch diagnostic\nsecond line\n")
        config = {"run_root": self.run_root, "hosts": {}}
        output, warning = io.StringIO(), io.StringIO()
        with mock.patch.object(self.module, "load_config", return_value=config), \
             mock.patch("sys.stdout", output), mock.patch("sys.stderr", warning):
            self.assertEqual(self.module.cmd_tail(SimpleNamespace(run_id=run_id, lines=10)), 0)
        self.assertEqual(output.getvalue(), "launch diagnostic\nsecond line\n")

        (run_dir / "log").write_text("payload log wins\n")
        output = io.StringIO()
        with mock.patch.object(self.module, "load_config", return_value=config), \
             mock.patch("sys.stdout", output), mock.patch("sys.stderr", warning):
            self.assertEqual(self.module.cmd_tail(SimpleNamespace(run_id=run_id, lines=10)), 0)
        self.assertEqual(output.getvalue(), "payload log wins\n")

        (run_dir / "log").unlink()
        (run_dir / "launch.log").unlink()
        warning = io.StringIO()
        with mock.patch.object(self.module, "load_config", return_value=config), \
             mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", warning):
            self.assertEqual(self.module.cmd_tail(SimpleNamespace(run_id=run_id, lines=10)), 1)
        self.assertIn("no log yet", warning.getvalue())

    def test_run_receipt_keeps_headroom_and_selected_gpu(self):
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
        self.assertEqual(receipt["gpus"], "0")
        self.assertIn("launch_compute", launch.call_args.args[1])
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

    def test_failed_probe_preserves_explicit_cpu_launch_and_unknown_receipt(self):
        for row in ({"reachable": False, "observed_at": 1728000000,
                     "detail": "timed out", "gpus": []}, OSError("probe unavailable")):
            with self.subTest(row=row):
                text, launch = self.run_receipt(row, cpu=True)
                self.assertIn("started ", text)
                self.assertIn("GPU headroom: unknown", text)
                self.assertNotIn("suggested:", text)
                launch.assert_called_once()

    def test_missing_process_measurement_never_claims_an_idle_gpu(self):
        row = {"reachable": True, "observed_at": 1728000000,
               "process_detail": "compute process query unavailable", "gpus": [
                   {"index": 0, "free_mib": 8000, "utilization_gpu_pct": 0,
                    "processes": []}]}
        text, _ = self.run_receipt(row, json_output=True, cpu=True)
        self.assertIsNone(json.loads(text)["gpu_observation"]["suggested_gpu"])


class ComputeRunQuiescenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.module = load_module()
        self.run_id = "fixture-compute-run"
        self.run_dir = self.root / self.run_id
        self.run_dir.mkdir()
        self.identity = {"pid": 12, "starttime": "44", "process_group": 12,
                         "pid_namespace": "pid:[77]", "boot_id": "boot-fixture"}
        output = self.root / "science.json"
        output.write_text('{"result":1}\n')
        output_row = {"path": "science.json", "sha256": self.module.hashlib.sha256(
            output.read_bytes()).hexdigest(), "size": output.stat().st_size, "state": "present"}
        parent_contract = {"artifact_base": str(self.root), "declared_outputs": ["science.json"]}
        contract = {"resource_parent": parent_contract}
        contract_sha = self.module.hashlib.sha256(json.dumps(
            contract, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        meta = {"run_id": self.run_id, "host": "fixture",
                "provenance": {"resource": {"contract": parent_contract}},
                "execution_contract": contract, "contract_sha256": contract_sha}
        (self.run_dir / "meta.json").write_text(json.dumps(meta))
        (self.run_dir / "exit_code").write_text("0\n")
        (self.run_dir / "launch-receipt.json").write_text(json.dumps({
            "schema_version": 1, "run_id": self.run_id, "host": "fixture", "identity": self.identity,
            "state": "finished", "exit_code": 0,
        }))
        (self.run_dir / "outputs.json").write_text(json.dumps({
            "schema_version": 1, "run_id": self.run_id, "outputs": [output_row]}))
        self.config = {"run_root": self.root, "hosts": {"fixture": {"ssh_host": "host"}}}
        self.expected = {
            "meta_sha256": self.module.hashlib.sha256((self.run_dir / "meta.json").read_bytes()).hexdigest(),
            "exit_sha256": self.module.hashlib.sha256((self.run_dir / "exit_code").read_bytes()).hexdigest(),
            "receipt_sha256": self.module.hashlib.sha256((self.run_dir / "launch-receipt.json").read_bytes()).hexdigest(),
            "outputs_sha256": self.module.hashlib.sha256((self.run_dir / "outputs.json").read_bytes()).hexdigest(),
            "contract": contract, "provenance": {"contract": parent_contract},
        }

    def observe(self, payload):
        return self.module.observe_run_quiescence(
            self.config, self.run_id, expected=self.expected,
            remote_call=lambda *_args, **_kwargs: subprocess.CompletedProcess(
                [], 0, json.dumps(payload), ""))

    def test_resource_parent_lineage_requires_exact_route_registry_owner_and_node(self):
        jobs = self.root / "jobs.log"
        jobs.write_text("")
        route_file = self.root / "route.json"
        route_file.write_text(json.dumps({"route_id": "rt-parent", "route_hash": "sha256:parent",
            "nodes": [{"id": "full-run", "kind": "resource-runner",
                       "continuation": {"kind": "supervised"}, "outputs": ["science.json"]}]}))
        registry = self.root / "resource-runs.json"
        owner = {"parent_attempt_id": "att-owner", "jobs": str(jobs),
                 "route_id": "rt-parent", "route_hash": "sha256:parent"}
        row = {"run_id": "parent-run", "resource_policy": "supervised-owner",
               "route": str(route_file), "node": "full-run", "jobs": str(jobs),
               "parent_attempt_id": "att-owner", "owner_wait": owner,
               "status": "running", "command": ["compute-hosts", "run"]}
        registry.write_text(json.dumps({"runs": {"parent-run": row}}))
        values = {"HEARTING_RESOURCE_RUN_ID": "parent-run",
                  "HEARTING_RESOURCE_REGISTRY": str(registry)}
        route = {"route_id": "rt-parent", "route_file": str(route_file)}
        parent = self.module._resource_parent(values, route)
        self.assertEqual(parent["contract"]["route_hash"], "sha256:parent")
        owner["parent_attempt_id"] = "att-foreign"
        row["owner_wait"] = owner
        registry.write_text(json.dumps({"runs": {"parent-run": row}}))
        self.assertIsNone(self.module._resource_parent(values, route))

    def test_quiescence_requires_matching_fresh_remote_empty_group(self):
        result = self.observe({"state": "empty", "reason": "observed-empty"})
        self.assertEqual(result["state"], "quiescent")
        self.assertEqual(result["identity"], self.identity)

    def test_live_or_unknown_remote_group_never_proves_quiescence(self):
        self.assertEqual(self.observe({"state": "populated"})["state"], "not-quiescent")
        self.assertEqual(self.observe({"state": "unknown"})["state"], "unknown")

    def test_cancelled_execution_needs_no_exit_or_outputs_but_still_observes_group(self):
        (self.run_dir / "exit_code").unlink()
        (self.run_dir / "outputs.json").unlink()
        self.module._record_stop(self.run_dir)
        for remote_state, expected in (("empty", "quiescent"), ("populated", "not-quiescent"),
                                       ("unknown", "unknown")):
            with self.subTest(remote_state=remote_state):
                call = mock.Mock(return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps({"state": remote_state, "reason": "fixture-group"}), ""))
                result = self.module.observe_run_quiescence(
                    self.config, self.run_id, expected=self.expected,
                    cancelled=True, remote_call=call)
                self.assertEqual(result["state"], expected)
                call.assert_called_once()
                self.assertIn(self.identity["pid_namespace"], call.call_args.args[1])
        self.assertNotEqual(self.observe({"state": "empty"})["state"], "quiescent")

    def test_cancelled_observation_keeps_identity_and_running_receipts_protected(self):
        path = self.run_dir / "launch-receipt.json"
        original = path.read_bytes()
        for change in ({"state": "running"}, {"identity": {}}, {"run_id": "other"}):
            with self.subTest(change=change):
                path.write_text(json.dumps({**json.loads(original), **change}))
                expected = {**self.expected, "receipt_sha256":
                            self.module.hashlib.sha256(path.read_bytes()).hexdigest()}
                call = mock.Mock()
                result = self.module.observe_run_quiescence(
                    self.config, self.run_id, expected=expected, cancelled=True, remote_call=call)
                self.assertEqual(result["state"], "not-quiescent")
                call.assert_not_called()
        path.write_bytes(original + b" ")
        self.assertEqual(self.module.observe_run_quiescence(
            self.config, self.run_id, expected=self.expected, cancelled=True)["reason"],
            "compute-run-evidence-changed")

    def test_changed_exit_bytes_invalidate_the_observation(self):
        (self.run_dir / "exit_code").write_text("1\n")
        result = self.observe({"state": "empty"})
        self.assertEqual(result["reason"], "compute-run-evidence-changed")


class ComputeOutputReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.run_root = self.root / "compute-runs"
        self.run_id = "fixture-output-run"
        self.run_dir = self.run_root / self.run_id
        self.run_dir.mkdir(parents=True)
        self.module = load_module()
        self.config = {"run_root": self.run_root,
                       "hosts": {"fixture": {"ssh_host": "fixture.invalid"}}}
        self.module.remote = lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, '{"state":"empty","reason":"fixture-empty-group"}\n', "")
        self.identity = {"pid": 12, "starttime": "44", "process_group": 12,
                         "pid_namespace": "pid:[77]", "boot_id": "boot-fixture"}

    def write_execution(self, declared, files):
        for relative, raw in files.items():
            target = self.artifacts / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        parent_contract = {"run_id": "fixture-resource", "route_id": "rt-output-fixture",
                           "route_hash": "sha256:output-fixture",
                           "artifact_base": str(self.artifacts),
                           "declared_outputs": list(declared)}
        provenance = {"run_id": "fixture-resource", "route_id": parent_contract["route_id"],
                      "route_hash": parent_contract["route_hash"],
                      "contract": parent_contract}
        contract = {"resource_parent": parent_contract,
                    "route": {"route_id": parent_contract["route_id"],
                              "route_hash": parent_contract["route_hash"]}}
        canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
        meta = {"run_id": self.run_id, "host": "fixture",
                "provenance": {"resource": provenance},
                "execution_contract": contract,
                "contract_sha256": self.module.hashlib.sha256(canonical).hexdigest()}
        (self.run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        (self.run_dir / "exit_code").write_text("0\n", encoding="ascii")
        (self.run_dir / "launch-receipt.json").write_text(json.dumps({
            "schema_version": 1, "run_id": self.run_id, "host": "fixture-host",
            "identity": self.identity, "state": "finished", "exit_code": 0,
            "started_at": 1.0, "ended_at": 2.0,
        }), encoding="utf-8")
        receipt = self.module._record_compute_outputs(self.run_dir, meta)
        return receipt, meta

    def observe(self):
        return self.module.execution_record(self.config, self.run_id)

    def test_standard_full_run_glob_receipt_reaches_fresh_quiescent_observation(self):
        declared = ["run.json", "logs/**", "experiment-artifact"]
        receipt, _meta = self.write_execution(declared, {
            "run.json": b'{"run":"full"}\n',
            "logs/train.log": b"train complete\n",
            "logs/nested/metrics.json": b'{"loss":0.1}\n',
        })
        self.assertEqual([row["path"] for row in receipt["outputs"]],
                         ["logs/nested/metrics.json", "logs/train.log", "run.json"])
        self.assertTrue(all(row["state"] == "present" for row in receipt["outputs"]))
        result = self.observe()
        self.assertEqual(result["state"], "quiescent", result)
        self.assertEqual([row["path"] for row in result["execution"]["outputs"]],
                         ["logs/nested/metrics.json", "logs/train.log", "run.json"])

    def test_standard_eval_run_glob_receipt_reaches_fresh_quiescent_observation(self):
        declared = ["run.json", "raw-results/**"]
        receipt, _meta = self.write_execution(declared, {
            "run.json": b'{"run":"eval"}\n',
            "raw-results/fold-1/scores.json": b'{"accuracy":0.9}\n',
        })
        self.assertEqual([row["path"] for row in receipt["outputs"]],
                         ["raw-results/fold-1/scores.json", "run.json"])
        self.assertEqual(self.observe()["state"], "quiescent")

    def test_overlapping_recursive_patterns_are_sorted_and_deduplicated(self):
        declared = ["run.json", "logs/**", "logs/nested/**", "experiment-artifact"]
        receipt, _meta = self.write_execution(declared, {
            "run.json": b"run\n", "logs/z.log": b"z\n",
            "logs/nested/a.log": b"a\n",
        })
        paths = [row["path"] for row in receipt["outputs"]]
        self.assertEqual(paths, ["logs/nested/a.log", "logs/z.log", "run.json"])
        self.assertEqual(len(paths), len(set(paths)))
        self.assertNotIn("experiment-artifact", paths)
        self.assertEqual(self.observe()["state"], "quiescent")

    def test_zero_match_is_recorded_as_missing_and_refused(self):
        receipt, _meta = self.write_execution(["run.json", "logs/**", "experiment-artifact"], {
            "run.json": b"run\n",
        })
        missing = [row for row in receipt["outputs"] if row.get("state") == "missing"]
        self.assertEqual(missing, [{"path": "logs/**", "state": "missing", "reason": "no-matches"}])
        result = self.observe()
        self.assertEqual(result["state"], "not-quiescent")
        self.assertEqual(result["reason"], "compute-run-output-missing")
        self.assertEqual(result["differences"][0]["field"], "outputs.logs/**")

    def test_literal_run_json_receipt_shape_stays_compatible(self):
        receipt, _meta = self.write_execution(["run.json"], {"run.json": b"literal\n"})
        self.assertEqual(set(receipt), {"schema_version", "run_id", "outputs", "observed_at"})
        self.assertEqual(set(receipt["outputs"][0]), {"path", "sha256", "size", "state"})
        self.assertEqual(receipt["outputs"][0]["path"], "run.json")
        self.assertEqual(self.observe()["state"], "quiescent")

    def test_added_removed_and_mutated_glob_members_break_the_receipt_snapshot(self):
        self.write_execution(["run.json", "logs/**"], {
            "run.json": b"run\n", "logs/one.log": b"one\n", "logs/two.log": b"two\n",
        })
        self.assertEqual(self.observe()["state"], "quiescent")
        (self.artifacts / "logs/added.log").write_bytes(b"added\n")
        result = self.observe()
        self.assertEqual(result["reason"], "compute-run-output-membership-changed")
        self.assertEqual(result["differences"][0]["field"], "outputs.paths")
        (self.artifacts / "logs/added.log").unlink()
        (self.artifacts / "logs/one.log").unlink()
        self.assertEqual(self.observe()["reason"], "compute-run-output-membership-changed")
        (self.artifacts / "logs/one.log").write_bytes(b"one changed\n")
        self.assertEqual(self.observe()["reason"], "compute-run-output-changed")

    def test_snapshot_recheck_reports_output_receipt_and_file_bytes_expected_actual(self):
        self.write_execution(["run.json", "logs/**"], {
            "run.json": b"run\n", "logs/train.log": b"complete\n",
        })
        execution = self.observe()["execution"]
        output_path = self.artifacts / "logs/train.log"
        original_output = output_path.read_bytes()
        output_path.write_bytes(b"changed\n")
        differences = self.module.run_snapshot_differences(
            self.config, self.run_id, execution)
        file_difference = next(item for item in differences
                               if item["field"] == "outputs.logs/train.log")
        original_row = next(row for row in execution["outputs"]
                            if row["path"] == "logs/train.log")
        self.assertEqual(file_difference["expected"], {
            "sha256": original_row["sha256"], "size": original_row["size"]})
        self.assertNotEqual(file_difference["expected"], file_difference["actual"])
        output_path.write_bytes(original_output)

        receipt_path = self.run_dir / "outputs.json"
        receipt = json.loads(receipt_path.read_bytes())
        receipt["observed_at"] = receipt.get("observed_at", 0) + 1
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False,
                                           sort_keys=True) + "\n", encoding="utf-8")
        differences = self.module.run_snapshot_differences(
            self.config, self.run_id, execution)
        receipt_difference = next(item for item in differences
                                  if item["field"] == "compute_run.outputs_sha256")
        self.assertEqual(receipt_difference["expected"], execution["outputs_sha256"])
        self.assertNotEqual(receipt_difference["expected"], receipt_difference["actual"])

    def test_snapshot_recheck_reports_sealed_pattern_set_expected_actual(self):
        declared = ["run.json", "logs/**", "experiment-artifact"]
        _receipt, _meta = self.write_execution(declared, {
            "run.json": b"run\n", "logs/train.log": b"complete\n",
        })
        execution = self.observe()["execution"]
        meta_path = self.run_dir / "meta.json"
        meta = json.loads(meta_path.read_bytes())
        parent = meta["provenance"]["resource"]["contract"]
        parent["declared_outputs"] = ["run.json"]
        meta["execution_contract"]["resource_parent"] = parent
        meta["contract_sha256"] = self.module.hashlib.sha256(json.dumps(
            meta["execution_contract"], ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        differences = self.module.run_snapshot_differences(
            self.config, self.run_id, execution)
        pattern_difference = next(item for item in differences
                                  if item["field"] == "declared_outputs")
        self.assertEqual(pattern_difference["expected"], declared)
        self.assertEqual(pattern_difference["actual"], ["run.json"])

    def test_symlink_leaf_ancestor_and_base_escape_never_hash_external_bytes(self):
        external = self.root / "external.bin"
        external.write_bytes(b"outside-secret-bytes\n")
        external_hash = self.module.hashlib.sha256(external.read_bytes()).hexdigest()
        for name, prepare in (
                ("leaf", lambda: (self.artifacts / "logs").mkdir(exist_ok=True)),
                ("ancestor", lambda: None)):
            with self.subTest(kind=name):
                for path in self.artifacts.iterdir():
                    if path.is_dir() and not path.is_symlink():
                        import shutil
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                prepare()
                target = self.artifacts / "logs" if name == "ancestor" else self.artifacts / "logs/external.bin"
                if name == "ancestor":
                    target.symlink_to(self.root, target_is_directory=True)
                else:
                    target.symlink_to(external)
                receipt, _meta = self.write_execution(["run.json", "logs/**"], {
                    "run.json": b"run\n",
                })
                self.assertNotIn(external_hash, json.dumps(receipt))
                self.assertNotEqual(self.observe()["state"], "quiescent")
        for unsafe in ("../external.bin", str(external)):
            with self.subTest(pattern=unsafe):
                for path in self.artifacts.iterdir():
                    if path.is_dir() and not path.is_symlink():
                        import shutil
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                receipt, _meta = self.write_execution(["run.json", unsafe], {
                    "run.json": b"run\n",
                })
                self.assertNotIn(external_hash, json.dumps(receipt))
                self.assertNotEqual(self.observe()["state"], "quiescent")


class ComputeHostsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
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
        self.fakebin = self.root / "fake-tmux-bin"
        install_fake_tmux(self.fakebin)
        # A run's completion watch notifies through the dispatch state root; keep it here.
        self.env = {**os.environ, "COMPUTE_HOSTS_CONFIG": str(self.config),
                    "HARNESS_STATE_ROOT": str(self.root / "state"),
                    "PATH": str(self.fakebin) + os.pathsep + os.environ.get("PATH", "")}
        self.env.pop("AGENT_DISPATCH_JOBS", None)
        for key in ("AGENT_ROUTE_FILE", "AGENT_OWNER_ROUTE_FILE", "AGENT_DISPATCH_DEPTH"):
            self.env.pop(key, None)

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *args, env=None):
        if args[:1] == ("run",) and "--gpus" not in args:
            args = (*args[:2], "--gpus", "", *args[2:])
        return subprocess.run([sys.executable, str(TOOL), *args],
                              text=True, capture_output=True, env=env or self.env)

    def test_missing_inventory_is_a_typed_failure(self):
        env = {**os.environ, "COMPUTE_HOSTS_CONFIG": str(self.root / "absent.yaml")}
        result = subprocess.run([sys.executable, str(TOOL), "list", "--static"],
                                text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not initialized", result.stderr)

    def test_self_command_probes_local_without_loading_inventory(self):
        module = load_module()
        expected = {"host": "fixture-machine", "reachable": True, "gpus": [{"index": 0}]}
        output = io.StringIO()
        with mock.patch.object(module.socket, "gethostname", return_value="fixture-machine"), \
                mock.patch.object(module, "load_config", side_effect=AssertionError("inventory read")), \
                mock.patch.object(module, "probe_host", return_value=expected) as probe, \
                mock.patch("sys.stdout", output):
            self.assertEqual(module.main(["self", "--json"]), 0)
        probe.assert_called_once_with("fixture-machine", {"ssh_host": "local"}, (), ())
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["hosts"][0]["host"], "fixture-machine")
        self.assertTrue(payload["hosts"][0]["self"])

    def test_static_listing_reads_the_inventory(self):
        result = self.run_tool("list", "--static", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual({h["host"] for h in payload["hosts"]},
                         {"here", "elsewhere"})
        self.assertEqual(payload["run_root"], str(self.run_root))

    def test_runs_skips_dot_directories_before_state_or_remote_probe(self):
        module = load_module()
        cache = self.run_root / ".hearting-releases"
        cache.mkdir(parents=True)
        (cache / "fake-entry").mkdir()
        output = io.StringIO()
        with mock.patch.object(module, "load_config", return_value={"run_root": self.run_root}), \
                mock.patch.object(module, "_refuse_namespace_self"), \
                mock.patch.object(module, "_run_state", side_effect=AssertionError("dot-dir scanned")), \
                mock.patch("sys.stdout", output):
            self.assertEqual(module.cmd_runs(SimpleNamespace(limit=10, host=None, json=True)), 0)
        self.assertEqual(output.getvalue().strip(), "[]")

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
        install_fake_tmux(fakebin, stale_environment={
            "CODEX_THREAD_ID": "stale-thread",
            "CODEX_SESSION_ID": "other-stale",
            "CLAUDE_CODE_SESSION_ID": "stale-claude",
        })
        session_keys = {key for key, _harness in load_module().SESSION_ENV_KEYS}
        env = {key: value for key, value in self.env.items()
               if key not in session_keys
               and key not in ("AGENT_HOME", "CLAUDE_HOME",
                               "AGENT_DISPATCH_ATTEMPT_ID", "AGENT_DISPATCH_JOBS")}
        for key in tuple(env):
            if (key.startswith("AGENT_DISPATCH_")
                    or key in ("AGENT_OWNER_ROUTE_FILE", "AGENT_ROUTE_FILE",
                               "AGENT_SESSION_ROLE", "FLEET_ROUTE_CHAIN_DIR",
                               "FLEET_TITLE_REFRESH")):
                env.pop(key, None)
        empty_home = self.root / "empty-home"
        empty_home.mkdir(exist_ok=True)
        env.update({
            "AGENT_DISPATCH_DEPTH": "0",
            "HOME": str(empty_home),
            "XDG_DATA_HOME": str(empty_home / ".local" / "share"),
            "XDG_STATE_HOME": str(empty_home / ".local" / "state"),
            "PATH": str(fakebin) + os.pathsep + os.environ["PATH"],
            "CODEX_THREAD_ID": "launch-thread",
        })
        result = subprocess.run(
            [sys.executable, str(TOOL), "run", "here", "--gpus", "", "--name", "tmux-owner",
             "--", "bash", "-c",
             "printf '%s|%s|%s' \"$CODEX_THREAD_ID\" "
             "\"${CODEX_SESSION_ID-}\" \"${CLAUDE_CODE_SESSION_ID-}\""],
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id = result.stdout.split()[1]
        log = (self.run_root / run_id / "log").read_text(encoding="utf-8")
        self.assertEqual(log, "launch-thread||")
        meta = json.loads((self.run_root / run_id / "meta.json").read_text())
        self.assertEqual(meta["tmux_transport"]["kind"], "hearting-tmux-v1")

    def test_conflicting_launcher_sessions_fail_closed_in_nohup_payload(self):
        fakebin = self.root / "nohup-bin"
        fakebin.mkdir()
        for command in ("bash", "id", "mkdir", "nohup", "setsid", "python3", "sh", "tr"):
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
            [sys.executable, str(TOOL), "run", "here", "--gpus", "", "--name", "nohup-owner",
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
        module = load_module()
        observation = {"reachable": True, "gpus": [{"index": 1, "free_mib": 100,
                        "utilization_gpu_pct": 0, "processes": []}]}
        out = io.StringIO()
        with mock.patch.object(module, "load_config", return_value={"run_root": self.run_root,
             "hosts": {"here": {"ssh_host": "local"}}}), \
             mock.patch.object(module, "_run_gpu_observation", return_value=observation), \
             mock.patch("sys.stdout", out):
            self.assertEqual(module.main(["run", "here", "--gpus", "1", "--cwd", str(self.root),
                                         "--dry-run", "--", "true"]), 0)
        self.assertIn("CUDA_VISIBLE_DEVICES=1", out.getvalue())
        self.assertIn(f"cd {self.root}", out.getvalue())

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

        def launch_fixture(process_env, *, stdout=None):
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                env=process_env, stdout=stdout or subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append(process)
            return process

        try:
            log = self.root / "train.log"
            log.write_text("Epoch 3/200\nTRAIN: 50% loss=0.5\n")
            with log.open("ab") as output:
                run_proc = launch_fixture(
                    {**clean, "HEARTING_COMPUTE_RUN_ID": "cnn-run-7",
                     "HEARTING_COMPUTE_HOST": "cnn"}, stdout=output)
            ambiguous_proc = launch_fixture(
                {**clean, "CODEX_THREAD_ID": "same-session-token",
                 "CLAUDE_CODE_SESSION_ID": "same-session-token"})
            job_proc = launch_fixture(
                {**clean, "AGENT_DISPATCH_ATTEMPT_ID": "att-42",
                 "AGENT_DISPATCH_SELF_SLUG": "train-test",
                 "HEARTING_COMPUTE_RUN_ID": "shadowed-run"})
            session_proc = launch_fixture(
                {**clean, "CODEX_THREAD_ID": "codex-exact-session"})
            probe = probe_namespace()
            proc_starts = {}
            for process in processes:
                observed = probe["proc_stat"](process.pid)
                self.assertIsNotNone(observed, process.pid)
                proc_starts[process.pid] = observed["start"]
            proc_root = controlled_proc_root(self.root, processes)
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
            probe_script = module.PROBE_SCRIPT.replace(
                'Path("/proc")', f"Path({str(proc_root)!r})")
            self.assertNotEqual(probe_script, module.PROBE_SCRIPT)
            result = subprocess.run(
                ["bash", "-c", probe_script], text=True, capture_output=True,
                env=env, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
        finally:
            for process in processes:
                terminate_and_reap(process)

        self.assertEqual([gpu["index"] for gpu in payload["gpus"]], [0, 1])
        first, second = payload["gpus"]
        for gpu in payload["gpus"]:
            for process in gpu["processes"]:
                self.assertNotIn("progress", process)
        self.assertEqual(len(first["processes"]), 2)
        by_pid = {row["pid"]: row for row in first["processes"]}
        self.assertEqual(by_pid[run_proc.pid]["owner"]["kind"], "run")
        self.assertEqual(by_pid[run_proc.pid]["owner"]["label"], "run:cnn-run-7")
        self.assertEqual(by_pid[run_proc.pid]["proc_start"], proc_starts[run_proc.pid])
        self.assertIsNone(by_pid[ambiguous_proc.pid]["owner"])
        self.assertEqual(by_pid[ambiguous_proc.pid]["attribution_reason"],
                         "ambiguous-session")
        self.assertEqual(by_pid[ambiguous_proc.pid]["proc_start"],
                         proc_starts[ambiguous_proc.pid])
        second_by_pid = {row["pid"]: row for row in second["processes"]}
        self.assertEqual(second_by_pid[job_proc.pid]["owner"]["kind"], "job")
        self.assertEqual(second_by_pid[job_proc.pid]["owner"]["label"], "job:train-test")
        self.assertEqual(second_by_pid[job_proc.pid]["proc_start"],
                         proc_starts[job_proc.pid])
        self.assertEqual(second_by_pid[session_proc.pid]["owner"]["kind"], "session")
        self.assertEqual(second_by_pid[session_proc.pid]["owner"]["label"], "codex:codex-ex")
        self.assertEqual(second_by_pid[session_proc.pid]["proc_start"],
                         proc_starts[session_proc.pid])
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

    def _delayed_gpu_probe(self, delay):
        module = load_module()
        fakebin = self.root / "delayed-bin"
        fakebin.mkdir()
        fake = fakebin / "nvidia-smi"
        fake.write_text(
            "#!/usr/bin/env python3\nimport sys,time\ntime.sleep(%r)\n"
            "if any(a.startswith('--query-gpu=') for a in sys.argv):\n"
            " print('0, GPU-A, NVIDIA A100, 42, 40960, 12288')\n" % delay)
        fake.chmod(0o755)
        env = {**self.env, "PATH": str(fakebin) + os.pathsep + os.environ["PATH"]}
        before = time.monotonic()
        with mock.patch.dict(os.environ, env, clear=True):
            row = module.probe_host("here", {"ssh_host": "local"},
                                    owner_claims=[], ssh_session_bridges=[])
        return row, time.monotonic() - before

    def test_slow_successful_nvidia_queries_survive_both_inner_and_host_deadlines(self):
        row, elapsed = self._delayed_gpu_probe(2.0)
        self.assertTrue(row["reachable"], row)
        self.assertNotIn("gpu_status", row)
        self.assertEqual(row["gpus"][0]["utilization_gpu_pct"], 42)
        self.assertEqual(row["gpus"][0]["memory_used_mib"], 12288)
        self.assertGreater(elapsed, 3.0)  # The old entire-host deadline was 3s.

    def test_hung_nvidia_query_still_returns_timeout_with_unknown_measurements(self):
        row, elapsed = self._delayed_gpu_probe(30.0)
        self.assertTrue(row["reachable"], row)
        self.assertIn("timed out", row["detail"])
        self.assertIn("gpu_status", row)
        for gpu in row["gpus"]:
            self.assertIsNone(gpu["utilization_gpu_pct"])
            self.assertIsNone(gpu["memory_used_mib"])
        self.assertLess(elapsed, 20.0)

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
        payload = self._probe_with_fake_smi(
            [["GPU-A", gone.pid, "python", 64]], "gone-bin")
        by_pid = {row["pid"]: row for row in payload["gpus"][0]["processes"]}
        ended = by_pid[gone.pid]
        for key in ("proc_start", "pgid", "cwd", "elapsed_s"):
            self.assertIsNone(ended[key], key)

        probe = probe_namespace()
        pid = os.getpid()
        current = probe["proc_stat"](pid)
        self.assertIsNotNone(current)
        denied_cwd = f"/proc/{pid}/cwd"
        readlink = os.readlink

        def fixture_readlink(path, *args, **kwargs):
            if os.fspath(path) == denied_cwd:
                raise PermissionError("fixture-controlled proc read denial")
            return readlink(path, *args, **kwargs)

        with mock.patch.object(probe["os"], "readlink", side_effect=fixture_readlink):
            self.assertIsNone(probe["process_cwd"](pid, current["start"]))

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
        child_code = ("import pathlib, subprocess, sys; "
                      "gpu = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']); "
                      "pathlib.Path(sys.argv[1]).write_text(str(gpu.pid)); gpu.wait()")
        root_code = ("import os, subprocess, sys; "
                     "child_env = {**os.environ, 'HEARTING_COMPUTE_RUN_ID': 'managed-run'}; "
                     "child = subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], "
                     "env=child_env); child.wait()")
        process = subprocess.Popen(
            [sys.executable, "-c", root_code, child_code, str(gpu_pid_file)],
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
            outside_claimed = self.run_tool(
                "claim", "here", str(os.getpid()), "--harness", "claude",
                "--session", "outside-run-group", "--json",
            )
            self.assertEqual(outside_claimed.returncode, 0, outside_claimed.stderr)
            outside_claim = json.loads(outside_claimed.stdout)
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
                "HEARTING_OWNER_CLAIMS_JSON": json.dumps([claim, outside_claim]),
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

        process = payload["gpus"][0]["processes"][0]
        owner = process["owner"]
        self.assertEqual(owner["kind"], "run")
        self.assertEqual(owner["id"], "managed-run")
        session_owner = process["session_owner"]
        self.assertEqual(session_owner["kind"], "session")
        self.assertEqual(session_owner["label"], "codex:f11a0486")
        self.assertEqual(session_owner["source"], "persistent-claim+ancestry")
        for rejected in (stale_payload, reused_payload):
            rejected_owner = rejected["gpus"][0]["processes"][0]["session_owner"]
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
        env = {**self.env, "PATH": os.pathsep.join(
            entry for entry in self.env["PATH"].split(os.pathsep)
            if Path(entry) != self.fakebin)}
        if shutil.which("tmux", path=env["PATH"]) is None:
            self.skipTest("tmux is not installed")
        probe_server = "hearting-test-%d-%d" % (os.getpid(), time.time_ns())
        probe = subprocess.run(["tmux", "-L", probe_server, "start-server"],
                               capture_output=True, text=True, env=env)
        if probe.returncode != 0:
            self.skipTest("tmux server is unavailable in this environment: " +
                          (probe.stderr or probe.stdout).strip())
        subprocess.run(["tmux", "-L", probe_server, "kill-server"],
                       capture_output=True, text=True, env=env)
        result = self.run_tool("run", "here", "--name", "stopme", "--",
                               "bash", "-c", "sleep 30", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id = result.stdout.split()[1]
        meta = json.loads((self.run_root / run_id / "meta.json").read_text())
        transport = meta["tmux_transport"]
        self.assertEqual(transport["kind"], "hearting-tmux-v1")
        server = transport["server"]
        try:
            for _ in range(50):
                check = subprocess.run(
                    ["tmux", "-L", server, "has-session", "-t", run_id],
                    capture_output=True, timeout=5, env=env)
                if check.returncode == 0:
                    break
                time.sleep(0.1)
            stopped = self.run_tool("stop", run_id, env=env)
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
            listed = self.run_tool("runs", env=env)
            self.assertIn("exit 143 (stopped)", listed.stdout)
            payload = json.loads(self.run_tool("runs", "--json", env=env).stdout)
            row = next(item for item in payload if item["run_id"] == run_id)
            self.assertEqual(row["exit_code"], 143)
            self.assertEqual(row["stop_reason"], "stopped")
            self.assertEqual(row["state"], "finished")
            tail = self.run_tool("tail", run_id, env=env)
            self.assertIn("exit 143 (stopped)", tail.stdout)
        finally:
            subprocess.run(["tmux", "-L", server, "kill-session", "-t", run_id],
                           capture_output=True, timeout=5, env=env)
            subprocess.run(["tmux", "-L", server, "kill-server"],
                           capture_output=True, timeout=5, env=env)

    def test_tmux_transport_probes_only_the_recorded_managed_server(self):
        module = load_module()
        server = "hearting-0123456789abcdefabcd"
        completed = subprocess.CompletedProcess([], 1, "", "")
        with mock.patch.object(module, "remote", return_value=completed) as remote:
            self.assertIs(
                module._tmux_session_alive(
                    {"ssh_host": "unused"}, "run-1",
                    transport={"kind": "hearting-tmux-v1", "server": server},
                ),
                False,
            )
            self.assertIn(
                f"tmux -L {server} has-session -t run-1",
                remote.call_args.args[1],
            )
            self.assertIsNone(module._tmux_session_alive(
                {"ssh_host": "unused"}, "run-1",
                transport={"kind": "hearting-tmux-v1", "server": "default"},
            ))
            self.assertIsNone(module._tmux_session_alive(
                {"ssh_host": "unused"}, "run-1",
                transport={"kind": "nohup-v1"},
            ))
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(module._tmux_command_prefix(), ["tmux"])

    def test_stop_never_overwrites_a_natural_finish(self):
        run_dir = self.run_root / "natural-20261002-000001-done"
        run_dir.mkdir(parents=True)
        (run_dir / "meta.json").write_text(
            json.dumps({"run_id": run_dir.name, "host": "here",
                        "command": ["true"],
                        "tmux_transport": {"kind": "hearting-tmux-v1",
                                           "server": "hearting-00000000000000000000"}}),
            encoding="utf-8")
        (run_dir / "exit_code").write_text("0\n", encoding="utf-8")
        fakebin = self.root / "natural-fake-tmux"
        install_fake_tmux(fakebin)
        stopped = self.run_tool(
            "stop", run_dir.name,
            env={**self.env, "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", "")},
        )
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
                        "command": ["bash", "-c", "sleep 30"],
                        "tmux_transport": {"kind": "hearting-tmux-v1",
                                           "server": "hearting-00000000000000000000"}}),
            encoding="utf-8")
        fakebin = self.root / "stale-fake-tmux"
        install_fake_tmux(fakebin)
        fixture_env = {**self.env,
                       "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", "")}
        listed = self.run_tool("runs", env=fixture_env)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("stopped (gone)", listed.stdout)
        self.assertFalse((run_dir / "exit_code").exists())
        payload = json.loads(self.run_tool("runs", "--json", env=fixture_env).stdout)
        row = next(item for item in payload if item["run_id"] == run_dir.name)
        self.assertIsNone(row["exit_code"])
        self.assertEqual(row["state"], "stopped")
        self.assertTrue(row["stale"])
        # An explicit stop then seals the legacy run the same way as new ones.
        stopped = self.run_tool("stop", run_dir.name, env=fixture_env)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(
            (run_dir / "exit_code").read_text(encoding="utf-8").strip(), "143")
        self.assertEqual(
            (run_dir / "stop_reason").read_text(encoding="utf-8").strip(),
            "stopped")

    def test_unconfirmed_launches_are_unknown_without_repairing_history(self):
        module = load_module()
        config = {"run_root": self.run_root}
        run_ids = ("xavier-20261024-193701-ami-tf-a1-historical-parity",
                   "xavier-20261024-191238-ami-leg2-e1-recovery",
                   "just-created")
        for run_id in run_ids:
            run_dir = self.run_root / run_id
            run_dir.mkdir(parents=True)
            if run_id != "just-created":
                os.utime(run_dir, (1, 1))
            self.assertEqual(module._run_state(config, run_id)["state"], "unknown")
        # Listing never probes a host or fabricates an exit for an unconfirmed
        # launch. The ordinary CLI corrects both old and newly created records.
        with mock.patch.object(module, "load_config", return_value=config), \
             mock.patch.object(module, "_tmux_session_alive") as probe, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(module.cmd_runs(SimpleNamespace(limit=10, host=None, json=True)), 0)
        probe.assert_not_called()
        rows = json.loads(output.getvalue())
        self.assertEqual({row["run_id"] for row in rows}, set(run_ids))
        self.assertTrue(all(row["state"] == "unknown" and row["exit_code"] is None
                            and not row["stale"] for row in rows))
        listed = self.run_tool("runs")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(listed.stdout.count("unknown"), 3)
        self.assertNotIn("running", listed.stdout)
        for run_id in run_ids:
            self.assertEqual(list((self.run_root / run_id).iterdir()), [])

    def test_unconfirmed_launch_observes_metadata_or_exit_when_they_arrive(self):
        module = load_module()
        config = {"run_root": self.run_root}
        run_dir = self.run_root / "launch-in-progress"
        run_dir.mkdir(parents=True)
        self.assertEqual(module._run_state(config, run_dir.name)["state"], "unknown")
        receipt = {"schema_version": 1, "run_id": run_dir.name,
                   "identity": {"pid": 1234, "starttime": "fixture-start"}}
        (run_dir / "launch-receipt.json").write_text(json.dumps(receipt))
        observed = module._run_state(config, run_dir.name)
        self.assertEqual(observed["state"], "unknown")
        self.assertEqual(observed["launch_receipt"], receipt)
        # The payload can finish even before the launcher records metadata.
        (run_dir / "exit_code").write_text("0\n")
        self.assertEqual(module._run_state(config, run_dir.name)["state"], "finished")
        (run_dir / "exit_code").unlink()
        (run_dir / "meta.json").write_text(json.dumps({
            "host": "here", "command": ["sleep", "30"],
            "tmux_transport": {"kind": "nohup-v1"}}))
        self.assertEqual(module._run_state(config, run_dir.name)["state"], "running")
        listed = self.run_tool("runs", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout)[0]["state"], "running")

    def test_runs_stays_running_when_session_state_is_unknown(self):
        module = load_module()
        run_dir = self.run_root / "unknown-20261002-000001-live"
        run_dir.mkdir(parents=True)
        (run_dir / "meta.json").write_text(
            json.dumps({"run_id": run_dir.name, "host": "here",
                        "command": ["bash", "-c", "sleep 30"],
                        "tmux_transport": {"kind": "nohup-v1"}}),
            encoding="utf-8")
        with mock.patch.object(module, "_tmux_session_alive", return_value=None):
            config = module.load_config(str(self.config))
            state = module._run_state(config, run_dir.name)
            self.assertEqual(state["state"], "running")
            self.assertIsNone(module._tmux_session_alive(object(), "x"))
        listed = self.run_tool("runs", "--host", "here")
        # An explicitly non-tmux transport stays unknown and never probes the
        # host's default tmux server.
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
        self.tmp = tempfile.TemporaryDirectory()
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
        for key in ("AGENT_ROUTE_FILE", "AGENT_OWNER_ROUTE_FILE", "AGENT_DISPATCH_DEPTH"):
            self.env.pop(key, None)

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
        if args[:1] == ("run",) and "--gpus" not in args:
            args = (*args[:2], "--gpus", "", *args[2:])
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

    def test_remote_missing_launcher_release_runs_preflight(self):
        """A managed release copied to run_root stays usable after its source vanishes."""
        module = load_module()
        source = self.root / "launcher-release"
        (source / "core").mkdir(parents=True)
        (source / "capabilities").mkdir()
        (source / "adapters/codex/bin").mkdir(parents=True)
        (source / "adapters/codex/utilities").mkdir()
        (source / "utilities").mkdir()
        (source / "roles").mkdir()
        (source / "claude_setting").mkdir()
        (source / "core/CORE.md").write_text("# fixture root\n", encoding="utf-8")
        for name, payload in (("harness-manifest.json", "{}\n"),
                              ("manifest.json", "{}\n"),
                              ("capabilities/topologies.json", "{}\n"),
                              ("roles/README.md", "fixture roles\n")):
            (source / name).write_text(payload, encoding="utf-8")
        preflight_source = ROOT / "../" / "adapters/codex/bin/preflight.sh"
        shutil.copy2(preflight_source, source / "adapters/codex/bin/preflight.sh")
        (source / "utilities/agent-home.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (source / "utilities/agent-home.sh").chmod(0o755)
        (source / "adapters/codex/utilities/agent-home.sh").symlink_to(
            "../../../utilities/agent-home.sh")
        (source / "claude_setting/commands").symlink_to(
            "../adapters/claude/commands")
        (source / "utilities/artifact-root.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (source / "utilities/artifact-root.sh").chmod(0o755)
        (source / "RELEASE_VERSION").write_text("vfixture.1\n", encoding="utf-8")
        (source / ".hearting-release.json").write_text(json.dumps({
            "schema": 1, "version": "vfixture.1", "archive_sha256": "a" * 64,
            "published_at": "fixture",
        }), encoding="utf-8")
        identity = module._verified_release_identity(source)
        self.assertIsNotNone(identity, "active release must satisfy the managed identity")
        provenance = {"AGENT_HOME": str(source)}
        non_self = {"ssh_host": "fixture-remote"}
        before = list(self.run_root.iterdir()) if self.run_root.exists() else []
        dry_path, dry_status, _ = module._select_payload_agent_home(
            provenance, non_self, self.run_root, dry_run=True)
        self.assertEqual(dry_status, "would-publish")
        self.assertFalse(self.run_root.exists())
        self.assertEqual(before, [])
        self.assertEqual(module._select_payload_agent_home(
            provenance, {"ssh_host": module.LOCAL}, self.run_root, dry_run=True)[0],
            str(source.resolve()))
        payload_home, status, reason = module._publish_managed_agent_home(
            source, self.run_root)
        self.assertIn(status, {"published", "present"}, reason)
        self.assertTrue(payload_home.startswith(str(self.run_root / ".hearting-releases")))
        copied = Path(payload_home)
        self.assertEqual(module._verified_release_identity(copied), identity)
        self.assertEqual((copied / "adapters/codex/utilities/agent-home.sh").readlink(),
                         (source / "adapters/codex/utilities/agent-home.sh").readlink())
        self.assertEqual((copied / "claude_setting/commands").readlink(),
                         Path("../adapters/claude/commands"))
        self.assertEqual((copied / "utilities/agent-home.sh").stat().st_mode & 0o777,
                         (source / "utilities/agent-home.sh").stat().st_mode & 0o777)
        reused, reuse_status, _ = module._publish_managed_agent_home(source, self.run_root)
        self.assertEqual((reused, reuse_status), (payload_home, "present"))
        self.assertEqual(module._select_payload_agent_home(
            provenance, non_self, self.run_root, dry_run=True)[:2], (payload_home, "present"))
        # Ownership/version markers survive a content change. Warm identity caches
        # must not let either real reuse or its dry-run trust that changed copy.
        core = copied / "core/CORE.md"
        original_core = core.read_bytes()
        marker_path = copied.parent / module.RELEASE_MARKER_NAME
        marker_before = marker_path.read_bytes()
        try:
            core.write_bytes(original_core + b"# changed cached content\n")
            key = module._release_key(identity)
            self.assertFalse(module._release_entry_valid(copied.parent, key, identity))
            dry_home, dry_status, dry_reason = module._select_payload_agent_home(
                provenance, non_self, self.run_root, dry_run=True)
            self.assertEqual((dry_home, dry_status), ("", "publish-failed"), dry_reason)
            self.assertEqual(marker_path.read_bytes(), marker_before)
            failed_home, failed_status, failed_reason = module._publish_managed_agent_home(
                source, self.run_root)
            self.assertEqual((failed_home, failed_status), ("", "publish-failed"), failed_reason)
            self.assertIn("cache entry exists but is not reusable", failed_reason)
            self.assertEqual(core.read_bytes(), original_core + b"# changed cached content\n")
            self.assertEqual(marker_path.read_bytes(), marker_before)
        finally:
            core.write_bytes(original_core)
        self.assertEqual(module._publish_managed_agent_home(source, self.run_root)[:2],
                         (payload_home, "present"))
        hidden = source.with_name(source.name + ".hidden-for-test")
        source.rename(hidden)
        self.addCleanup(lambda: hidden.rename(source) if hidden.exists() else None)
        campaign = self.root / "campaigns" / "fixture" / "artifacts" / "raw-results"
        campaign.mkdir(parents=True)
        preflight = Path(payload_home) / "adapters/codex/bin/preflight.sh"
        result = subprocess.run([str(preflight), "write", str(campaign)],
                                text=True, capture_output=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(list(campaign.iterdir()), [])
        hidden.rename(source)

        # Absolute and escaping links are rejected by real and dry-run paths.
        for name, target in (("absolute", str(source / "core/CORE.md")),
                             ("escaping", "../../outside")):
            bad_link = source / f"unsafe-{name}"
            bad_link.symlink_to(target)
            rejected_run_root = self.root / f"runs-{name}"
            with mock.patch.object(module, "_verified_release_identity", return_value=identity):
                dry_home, dry_status, dry_reason = module._select_payload_agent_home(
                    provenance, non_self, rejected_run_root, dry_run=True)
                payload, publish_status, publish_reason = module._publish_managed_agent_home(
                    source, rejected_run_root)
            self.assertEqual((dry_home, dry_status), ("", "publish-failed"), dry_reason)
            self.assertEqual((payload, publish_status), ("", "publish-failed"), publish_reason)
            self.assertFalse(rejected_run_root.exists())
            bad_link.unlink()

        # Exercise cmd_run's serialized remote launch through a fake SSH client.
        hosts = self.config.read_text(encoding="utf-8").replace(
            "    note: local fixture\n",
            "    note: local fixture\n"
            "  remote:\n"
            "    ssh_host: fixture-remote\n")
        self.config.write_text(hosts, encoding="utf-8")
        fakebin = self.root / "fake-remote-bin"
        install_fake_tmux(fakebin)
        ssh = fakebin / "ssh"
        ssh.write_text(
            "#!/bin/sh\n"
            "set +e\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  case \"$1\" in -o|-p|-F) shift 2 ;; -*) shift ;; *) shift; break ;; esac\n"
            "done\n"
            "src=$FAKE_HIDE_SOURCE\n"
            "if [ -n \"$src\" ]; then mv \"$src\" \"$src.hidden\" || exit 90; fi\n"
            "bash -lc \"$*\"\n"
            "status=$?\n"
            "if [ -n \"$src\" ]; then mv \"$src.hidden\" \"$src\" || exit 91; fi\n"
            "exit $status\n", encoding="utf-8")
        ssh.chmod(0o755)
        remote_env = self.scrubbed_env(
            AGENT_HOME=str(source), PATH=str(fakebin) + os.pathsep + os.environ.get("PATH", ""),
            FAKE_HIDE_SOURCE=str(source))
        remote_env["COMPUTE_HOSTS_CONFIG"] = str(self.config)
        launched = self.run_tool("run", "remote", "--name", "payload-write", "--",
                                 "bash", "-c",
                                 f'"$AGENT_HOME/adapters/codex/bin/preflight.sh" write {shlex.quote(str(campaign))}',
                                 env=remote_env)
        self.assertEqual(launched.returncode, 0, launched.stderr + launched.stdout)
        run_id = launched.stdout.split()[1]
        self.wait_exit(run_id)
        self.assertEqual((self.run_root / run_id / "exit_code").read_text().strip(), "0")
        meta = json.loads((self.run_root / run_id / "meta.json").read_text())
        self.assertEqual(meta["provenance"]["agent_home"], str(source))
        self.assertIn(meta["provenance"]["agent_home_status"], {"published", "present"})
        self.assertEqual(meta["provenance"]["payload_agent_home"], payload_home)

        invalid_source = self.root / "invalid-managed-launcher"
        (invalid_source / "core").mkdir(parents=True)
        (invalid_source / "core/CORE.md").write_text("# root\n", encoding="utf-8")
        (invalid_source / ".hearting-release.json").write_text("not-json", encoding="utf-8")
        failed_env = self.scrubbed_env(
            AGENT_HOME=str(invalid_source), PATH=remote_env["PATH"], FAKE_HIDE_SOURCE="")
        failed_env["COMPUTE_HOSTS_CONFIG"] = str(self.config)
        failed_launch = self.run_tool("run", "remote", "--name", "invalid-managed", "--",
                                      "bash", "-c", 'printf "<%s>" "$AGENT_HOME"',
                                      env=failed_env)
        self.assertEqual(failed_launch.returncode, 0,
                         failed_launch.stderr + failed_launch.stdout)
        self.assertIn("AGENT_HOME publish-failed", failed_launch.stderr)
        failed_id = failed_launch.stdout.split()[1]
        self.wait_exit(failed_id)
        self.assertEqual((self.run_root / failed_id / "log").read_text().strip(), "<>")
        failed_meta = json.loads((self.run_root / failed_id / "meta.json").read_text())
        self.assertEqual(failed_meta["provenance"]["agent_home_status"], "publish-failed")
        self.assertEqual(failed_meta["provenance"]["payload_agent_home"], "")

        concurrent_root = self.root / "concurrent-runs"
        worker = (
            "import importlib.util,json,sys; "
            "spec=importlib.util.spec_from_file_location('compute_hosts',sys.argv[1]); "
            "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
            "print(json.dumps(m._publish_managed_agent_home(sys.argv[2],sys.argv[3])))")
        procs = [subprocess.Popen([sys.executable, "-c", worker, str(TOOL), str(source),
                                   str(concurrent_root)], text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE) for _ in range(2)]
        outcomes = []
        for process in procs:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            outcomes.append(json.loads(stdout))
        self.assertEqual(outcomes[0][0], outcomes[1][0])
        self.assertEqual(sorted(row[1] for row in outcomes), ["present", "published"])
        concurrent_cache = concurrent_root / module.RELEASE_CACHE_NAME
        self.assertEqual([p.name for p in concurrent_cache.iterdir()
                          if p.name.startswith(".stage-")], [])

    def test_gc_handles_real_run_root_shape_and_deletes_only_unreferenced_old_entry(self):
        module = load_module()
        cache = self.run_root / module.RELEASE_CACHE_NAME
        cache.mkdir(parents=True)
        now = time.time()

        def add_entry(version, last_used):
            identity = {"schema": 1, "version": version,
                        "release_id": f"release:{version}:0123456789ab",
                        "content_digest": "sha256:" + "a" * 64,
                        "archive_sha256": "b" * 64}
            key = module._release_key(identity)
            entry = cache / key
            entry.mkdir()
            (entry / "source").mkdir()
            (entry / "marker.json").write_text(json.dumps({
                "schema": 1, "kind": "hearting-compute-release", "key": key,
                "identity": identity,
                "last_used_at": datetime.datetime.fromtimestamp(
                    last_used, datetime.timezone.utc).isoformat(),
            }), encoding="utf-8")
            return entry

        unreferenced = add_entry("v3.1", now - 15 * 86400)
        referenced = add_entry("v3.2", now - 20 * 86400)
        current = add_entry("v3.3", now - 30 * 86400)
        for name in ("ami-eval-smoke-20261020", "asr-coresidence-20261007"):
            (self.run_root / name).mkdir(parents=True)
        local_only = self.run_root / "old-local-path-run"
        local_only.mkdir()
        (local_only / "meta.json").write_bytes(b'{"agent_home":"/launcher/release"}')
        naming = self.run_root / "old-cache-run"
        naming.mkdir()
        (naming / "meta.json").write_bytes(b"malformed-but-names-" + referenced.name.encode())

        self.assertEqual(module._release_gc(self.run_root, cache, current.name, now=now),
                         "complete")
        self.assertFalse(unreferenced.exists())
        self.assertTrue(referenced.exists())
        self.assertTrue(current.exists())
        (naming / "exit_code").write_text("0\n", encoding="utf-8")
        self.assertEqual(module._release_gc(self.run_root, cache, current.name,
                                            now=now + 25 * 3600), "complete")
        self.assertFalse(referenced.exists())
        self.assertTrue(current.exists())

    def test_gc_is_stamped_at_most_once_per_day(self):
        module = load_module()
        cache = self.run_root / module.RELEASE_CACHE_NAME
        cache.mkdir(parents=True)
        key = "v3.1-0123456789ab-" + "a" * 64
        entry = cache / key
        entry.mkdir()
        (entry / "source").mkdir()
        identity = {"schema": 1, "version": "v3.1",
                    "release_id": "release:v3.1:0123456789ab",
                    "content_digest": "sha256:" + "a" * 64,
                    "archive_sha256": "b" * 64}
        (entry / "marker.json").write_text(json.dumps({
            "schema": 1, "kind": "hearting-compute-release", "key": entry.name,
            "identity": identity,
            "last_used_at": datetime.datetime.fromtimestamp(
                time.time() - 20 * 86400, datetime.timezone.utc).isoformat(),
        }), encoding="utf-8")
        now = time.time()
        (cache / module.RELEASE_GC_STAMP).write_text(
            json.dumps({"timestamp": now - 23 * 3600}), encoding="utf-8")
        self.assertEqual(module._release_gc(self.run_root, cache, "current", now=now),
                         "throttled")
        self.assertTrue(entry.exists())
        self.assertEqual(module._release_gc(self.run_root, cache, "current", now=now + 2 * 3600),
                         "complete")
        self.assertFalse(entry.exists())

    def test_gc_skips_ambiguous_metadata_and_removes_only_marked_old_staging(self):
        module = load_module()
        cache = self.run_root / module.RELEASE_CACHE_NAME
        cache.mkdir(parents=True)
        now = time.time()
        identity = {"schema": 1, "version": "v3.7",
                    "release_id": "release:v3.7:0123456789ab",
                    "content_digest": "sha256:" + "c" * 64,
                    "archive_sha256": "d" * 64}
        key = module._release_key(identity)
        old = cache / key
        old.mkdir()
        (old / "source").mkdir()
        (old / "marker.json").write_text(json.dumps({
            "schema": 1, "kind": "hearting-compute-release", "key": key,
            "identity": identity,
            "last_used_at": datetime.datetime.fromtimestamp(
                now - 20 * 86400, datetime.timezone.utc).isoformat(),
        }), encoding="utf-8")
        live = self.run_root / "oversized-live-meta"
        live.mkdir()
        (live / "meta.json").write_bytes(b"x" * (module.RELEASE_META_MAX_BYTES + 1))
        self.assertEqual(module._release_gc(self.run_root, cache, "current", now=now),
                         "skipped-live-metadata")
        self.assertTrue(old.exists())

        (live / "meta.json").unlink()
        unreadable = live / "meta.json"
        unreadable.write_text("{}", encoding="utf-8")
        original_read_bytes = Path.read_bytes
        def fail_meta_read(path):
            if path == unreadable:
                raise PermissionError("fixture unreadable metadata")
            return original_read_bytes(path)
        with mock.patch.object(Path, "read_bytes", fail_meta_read):
            self.assertEqual(module._release_live_references(self.run_root, cache), None)
        self.assertTrue(old.exists())

        # The daily stamp prevents another traversal until the interval expires.
        (live / "meta.json").unlink()
        staging_key = {"schema": 1, "version": "v3.8",
                       "release_id": "release:v3.8:1123456789ab",
                       "content_digest": "sha256:" + "e" * 64,
                       "archive_sha256": "f" * 64}
        stage_key = module._release_key(staging_key)
        aged = cache / f".stage-{stage_key}-123-1"
        fresh = cache / f".stage-{stage_key}-123-2"
        unmarked = cache / ".stage-unmarked-123-3"
        for stage, age in ((aged, now - 2 * 86400), (fresh, now + 24 * 3600)):
            stage.mkdir()
            (stage / "marker.json").write_text(json.dumps({
                "schema": 1, "kind": "hearting-compute-release", "key": stage_key,
                "identity": staging_key, "staging": True,
                "created_at": datetime.datetime.fromtimestamp(
                    age, datetime.timezone.utc).isoformat(),
            }), encoding="utf-8")
        unmarked.mkdir()
        self.assertEqual(module._release_gc(self.run_root, cache, "current",
                                            now=now + 25 * 3600), "complete")
        self.assertFalse(aged.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(unmarked.exists())

    def test_managed_and_unmanaged_forwarding_are_distinguished(self):
        module = load_module()
        remote_host = {"ssh_host": "fixture-remote"}
        unmanaged = self.make_home("unmanaged-home")
        self.assertEqual(module._select_payload_agent_home(
            {"AGENT_HOME": str(unmanaged)}, remote_host, self.run_root),
            (str(unmanaged), "unmanaged-forwarded",
             "unmanaged checkout forwarded; remote visibility is not guaranteed"))
        invalid = self.root / "invalid-managed"
        invalid.mkdir()
        (invalid / ".hearting-release.json").write_text("not-json", encoding="utf-8")
        payload, status, reason = module._select_payload_agent_home(
            {"AGENT_HOME": str(invalid)}, remote_host, self.run_root)
        self.assertEqual((payload, status), ("", "publish-failed"))
        self.assertIn("identity validation", reason)
        cache_root = self.root / "symlink-cache-run-root"
        cache_root.mkdir()
        outside = self.root / "outside-cache"
        outside.mkdir()
        (cache_root / module.RELEASE_CACHE_NAME).symlink_to(outside, target_is_directory=True)
        valid_identity = {"schema": 1, "version": "v3.1",
                          "release_id": "release:v3.1:0123456789ab",
                          "content_digest": "sha256:" + "a" * 64}
        with mock.patch.object(module, "_verified_release_identity",
                               return_value=valid_identity), \
                mock.patch.object(module, "_release_tree_safe", return_value=True):
            payload, status, reason = module._publish_managed_agent_home(
                self.root / "launcher-release", cache_root)
        self.assertEqual((payload, status), ("", "publish-failed"))
        self.assertIn("cache path is a symlink", reason)
        self.assertEqual(list(outside.iterdir()), [])

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
        # Without dispatch depth, this attempt's session identity is unresolved.
        self.assertNotIn("export CODEX_THREAD_ID=launch-thread", result.stdout)
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
        fakebin = self.root / "unknown-tmux-bin"
        install_fake_tmux(fakebin)
        env["PATH"] = str(fakebin) + os.pathsep + os.environ.get("PATH", "")
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
                         {"agent_home": None, "payload_agent_home": "",
                          "agent_home_status": "unknown",
                          "agent_home_reason": "launcher AGENT_HOME is unavailable",
                          "attempt_id": None, "session": None, "route": None})
        # The conda-selection field keeps its own meaning beside provenance.
        self.assertIsNone(meta["env"])

    def test_forwarded_provenance_crosses_a_scrubbed_tmux_boundary(self):
        home = self.make_home()
        attempt = "att-9f2c4b1ad34e"
        fakebin = self.root / "tmux-bin"
        install_fake_tmux(fakebin, stale_environment={
            "AGENT_HOME": "/foreign/old-release",
            "AGENT_DISPATCH_ATTEMPT_ID": "att-stale-owner",
        })
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
                          "payload_agent_home": str(home.resolve()),
                          "agent_home_status": "self",
                          "agent_home_reason": "self host keeps its pinned release path",
                          "attempt_id": attempt, "session": None, "route": None})

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
        """Exercise stale server values against the per-launch env prefix."""
        fakebin = self.root / name
        install_fake_tmux(fakebin, stale_environment={
            "AGENT_HOME": "/foreign/stale-home",
            "AGENT_DISPATCH_ATTEMPT_ID": "att-unrelated-old-owner",
        })
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
                         {"agent_home": None, "payload_agent_home": "",
                          "agent_home_status": "unknown",
                          "agent_home_reason": "launcher AGENT_HOME is unavailable",
                          "attempt_id": None, "session": None, "route": None})

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
        jobs.write_text("")
        meta = {**self.meta, "provenance": {**self.meta["provenance"], "attempt_id": "att-worker-1",
                                           "session": {"harness": "claude", "id": "sid-parent"},
                                           "notify": {"harness": "claude", "id": "sid-parent"}}}
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
        self.addCleanup(terminate_and_reap, child)
        return child

    def test_live_process_hash_matches_cmdline_bytes(self):
        import hashlib

        child = subprocess.Popen(
            [sys.executable, "-c",
             "import os, time; os.write(1, b'fixture-ready\\n'); time.sleep(10)"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
        )
        self.addCleanup(terminate_and_reap, child)
        with selectors.DefaultSelector() as readiness:
            readiness.register(child.stdout, selectors.EVENT_READ)
            self.assertTrue(readiness.select(timeout=5), "fixture child did not become ready")
            self.assertEqual(child.stdout.readline(), b"fixture-ready\n")

        observed = self.ns["proc_stat"](child.pid)
        self.assertIsNotNone(observed)
        start = observed["start"]
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


class PathNamespaceInventoryTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "inventory.yaml"

    def load_text(self, path_namespace, run_root="/home/nas/runs"):
        self.path.write_text(
            "schema_version: 1\nrun_root: " + run_root + "\nhosts:\n"
            "  mpwav:\n    ssh_host: mpwav\n    path_namespace:\n"
            + path_namespace, encoding="utf-8")
        return self.module.load_config(str(self.path))

    def test_accepts_block_mapping_and_multiple_disjoint_targets_without_readiness_checks(self):
        with mock.patch.object(self.module.os.path, "exists",
                               side_effect=AssertionError("inventory validation must not probe mounts")):
            config = self.load_text("      /home/nas: /home/nas_IIP\n"
                                    "      /datasets: /mnt/datasets\n")
        self.assertEqual(config["hosts"]["mpwav"]["path_namespace"],
                         {"/home/nas": "/home/nas_IIP", "/datasets": "/mnt/datasets"})

    def test_rejects_invalid_mapping_shapes_and_path_relationships(self):
        invalid = [
            ("      /home/nas: {}\n", "paths must be absolute"),
            ("      /home/nas: relative\n", "paths must be absolute"),
            ("      /home/nas: /mnt/data/../x\n", "paths must be absolute"),
            ("      /home/nas: /mnt/data/\n", "paths must be absolute"),
            ("      /home/nas: /home/nas\n", "source and target must differ"),
            ("      /home/nas: /mnt/x\n      /home/nas/nested: /mnt/y\n", "targets overlap"),
            ("      /home/nas: /mnt/x\n      /data: /home/nas/sub\n", "source overlaps target"),
        ]
        for mapping, reason in invalid:
            with self.subTest(mapping=mapping), self.assertRaisesRegex(
                    self.module.ConfigError, reason):
                self.load_text(mapping)
        with self.assertRaisesRegex(self.module.ConfigError, "run_root must be below"):
            self.load_text("      /data: /mnt/data\n")

    def test_rejects_empty_inline_and_quoted_target_mappings(self):
        for suffix in ("      /home/nas: {}\n", "      /home/nas: []\n",
                       "      \"/home/nas\": /mnt/nas\n"):
            with self.subTest(suffix=suffix), self.assertRaises(self.module.ConfigError):
                self.load_text(suffix)

    def test_configured_remote_run_serializes_namespace_arguments(self):
        target = Path(self.tmp.name) / "target"
        source = Path(self.tmp.name) / "source"
        target.mkdir(); source.mkdir()
        run_root = target / "runs"
        host = {"ssh_host": "fixture.example",
                "path_namespace": {str(target): str(source)}}
        config = {"run_root": run_root, "hosts": {"remote": host}}
        args = SimpleNamespace(host="remote", command=["true"], name="fixture",
                               cwd=None, env=None, gpus="", dry_run=False,
                               json=True, share=False)
        import gpu_leases
        launch_result = subprocess.CompletedProcess(
            [], 0, json.dumps({"gpus": "", "gpu_lease": None,
                               "tmux_transport": {"kind": "nohup-v1"}}), "")
        output = io.StringIO()
        with mock.patch.object(self.module, "load_config", return_value=config), \
             mock.patch.object(self.module, "_run_id", return_value="fixture-run"), \
             mock.patch.object(self.module, "_run_gpu_observation", return_value={"gpus": []}), \
             mock.patch.object(self.module, "_launcher_provenance", return_value={}), \
             mock.patch.object(self.module, "_launcher_route", return_value=None), \
             mock.patch.object(self.module, "_spawn_completion_watch", return_value=False), \
             mock.patch.object(gpu_leases, "state_path", return_value=Path(self.tmp.name) / "lease.json"), \
             mock.patch.object(self.module, "remote", return_value=launch_result) as remote, \
             mock.patch("sys.stdout", output):
            self.assertEqual(self.module.cmd_run(args), 0)
        script = remote.call_args.args[1]
        self.assertIn("launch_compute_namespaced", script)
        self.assertIn("path_namespace", script)
        self.assertIn(str(run_root), script)

    def test_configured_self_launcher_refuses_all_run_root_commands_before_access(self):
        target = {"ssh_host": "remote.example"}
        self_host = {"ssh_host": "local", "path_namespace": {"/home/nas": "/mnt/lab"}}
        config = {"run_root": Path(self.tmp.name) / "must-not-touch",
                  "hosts": {"self": self_host, "other": target}}
        module = self.module
        calls = (
            (module.cmd_run, SimpleNamespace(host="other", command=["true"], dry_run=True, name=None)),
            (module.cmd_claim, SimpleNamespace(host="other", session=None, harness=None, pid=1)),
            (module.cmd_runs, SimpleNamespace(limit=10, host=None, json=False)),
            (module.cmd_tail, SimpleNamespace(run_id="anything", lines=10)),
            (module.cmd_stop, SimpleNamespace(run_id="anything")),
            (module.cmd_watch_run, SimpleNamespace(run_dir=str(config["run_root"] / "anything"))),
        )
        for command, args in calls:
            with self.subTest(command=command.__name__), \
                 mock.patch.object(module, "load_config", return_value=config), \
                 mock.patch.object(module.Path, "is_dir", side_effect=AssertionError("run_root accessed")), \
                 mock.patch.object(module, "_run_id", side_effect=AssertionError("run_id reached")), \
                 mock.patch.object(module, "remote", side_effect=AssertionError("remote reached")):
                with self.assertRaisesRegex(module.ConfigError, "path_namespace refuses local"):
                    command(args)

    def test_configured_self_probe_and_list_skip_claim_root(self):
        config = {"run_root": Path(self.tmp.name) / "must-not-touch",
                  "hosts": {"self": {"ssh_host": "local",
                                      "path_namespace": {"/home/nas": "/mnt/lab"}}}}
        module = self.module
        row = {"host": "self", "reachable": True, "gpus": [], "load": "0",
               "cpu_utilization_pct": 0, "gpu_status": None}
        with mock.patch.object(module, "load_config", return_value=config), \
             mock.patch.object(module, "_probe_selected", return_value=[row]) as probe, \
             mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(module.cmd_list(SimpleNamespace(hosts=[], static=False, json=False)), 0)
            self.assertIsNone(probe.call_args.args[1])
            self.assertEqual(module.cmd_probe(SimpleNamespace(hosts=[], json=True)), 0)
            self.assertIsNone(probe.call_args.args[1])

    def test_configured_host_non_run_remote_scripts_match_unconfigured(self):
        module = self.module
        plain = {"ssh_host": "fixture.example"}
        mapped = {**plain, "path_namespace": {"/home/nas": "/home/nas_IIP"}}
        captured = []
        def capture(_host, script, **_kwargs):
            captured.append(script)
            if "HEARTING_CLAIM_PID" in script:
                return subprocess.CompletedProcess([], 0, json.dumps(
                    {"pid": 7, "start": 11, "cmdline_sha256": "fixture"}), "")
            return subprocess.CompletedProcess([], 0, json.dumps({"gpus": [], "gpu_leases": []}), "")

        with mock.patch.object(module, "remote", side_effect=capture), \
             mock.patch.object(module, "collect_ssh_session_bridges", return_value=[]):
            module.probe_host("mpwav", plain, [], [])
            probe_plain = captured.pop()
            module.probe_host("mpwav", mapped, [], [])
            self.assertEqual(probe_plain, captured.pop())

        for host in (plain, mapped):
            config = {"run_root": Path(self.tmp.name), "hosts": {"mpwav": host}}
            with mock.patch.object(module, "load_config", return_value=config), \
                 mock.patch.object(module, "remote", side_effect=capture), \
                 mock.patch.object(module, "_record_claim"), \
                 mock.patch("sys.stdout", io.StringIO()):
                args = SimpleNamespace(host="mpwav", session="session", harness="codex",
                                       pid=7, json=True)
                self.assertEqual(module.cmd_claim(args), 0)
        claim_plain, claim_mapped = captured[-2:]
        self.assertEqual(claim_plain, claim_mapped)

        captured.clear()
        def capture_stop(_host, script, **_kwargs):
            captured.append(script)
            return subprocess.CompletedProcess([], 1, "", "")
        state = {"meta": {"host": "mpwav", "tmux_transport": {
                    "kind": "hearting-tmux-v1", "server": "hearting-0123456789abcdefabcd"}},
                 "dir": Path(self.tmp.name), "exit_code": None, "stop_reason": None}
        for host in (plain, mapped):
            config = {"run_root": Path(self.tmp.name), "hosts": {"mpwav": host}}
            with mock.patch.object(module, "load_config", return_value=config), \
                 mock.patch.object(module, "_run_state", return_value=state), \
                 mock.patch.object(module, "remote", side_effect=capture_stop):
                self.assertEqual(module.cmd_stop(SimpleNamespace(run_id="fixture-run")), 0)
                module._tmux_session_alive(host, "fixture-run", transport=state["meta"]["tmux_transport"])
        self.assertEqual(captured[0], captured[2])
        self.assertEqual(captured[1], captured[3])

    def test_namespace_refusal_record_is_terminal_to_state_and_watch(self):
        module = self.module
        run_dir = Path(self.tmp.name) / "run"
        run_dir.mkdir()
        (run_dir / "stop_reason").write_text("path-namespace-unshare\n")
        (run_dir / "exit_code").write_text("125\n")
        meta = {"run_id": "fixture-run", "host": "mpwav",
                "provenance": {"notify": {"harness": "codex", "id": "session-1"}}}
        (run_dir / "meta.json").write_text(json.dumps(meta))
        config = {"run_root": Path(self.tmp.name), "hosts": {}}
        state = module._run_state(config, "run")
        self.assertEqual((state["state"], state["exit_code"], state["stop_reason"]),
                         ("finished", 125, "path-namespace-unshare"))
        notice = mock.Mock()
        with mock.patch.object(module, "load_config", return_value=config), \
             mock.patch.dict(sys.modules, {"session_notice": SimpleNamespace(notify=notice)}):
            self.assertEqual(module.cmd_watch_run(SimpleNamespace(run_dir=str(run_dir))), 0)
        notice.assert_called_once()
        self.assertIn("exit 125", notice.call_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
