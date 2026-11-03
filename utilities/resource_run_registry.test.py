#!/usr/bin/env python3
import errno
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import resource_run_registry as registry


class ResourceRegistryTest(unittest.TestCase):
    def test_compute_identity_requires_output_receipt_but_keeps_stable_format(self):
        execution = {"kind": "compute-hosts", "run_id": "compute-1",
                     "meta_sha256": "m" * 64, "exit_sha256": "e" * 64,
                     "receipt_sha256": "r" * 64, "outputs_sha256": "o" * 64,
                     "contract_sha256": "c" * 64}
        identity = registry.compute_execution_identity({"execution": execution})
        self.assertEqual(identity, "compute-hosts:compute-1:" + ":".join(
            execution[key] for key in
            ("meta_sha256", "exit_sha256", "receipt_sha256", "contract_sha256")))
        self.assertEqual(registry.compute_execution_identity({"execution": {
            **execution, "outputs_sha256": "x" * 64}}), identity)
        self.assertIsNone(registry.compute_execution_identity({"execution": {
            key: value for key, value in execution.items() if key != "outputs_sha256"}}))
        self.assertIsNone(registry.compute_execution_identity({"execution": {
            "kind": "local-resource", "outputs_sha256": "o" * 64}}))

    def test_compute_identity_uses_recorded_receipt_without_remote_or_local_probe(self):
        row = {"execution": {"kind": "compute-hosts", "run_id": "compute-1",
                             "receipt_state": "finished"},
               "pid": None, "starttime": None, "command_hash": None,
               "resource_policy": "supervised-owner", "status": "succeeded"}
        identity_reader = mock.Mock(side_effect=AssertionError("local PID probe"))
        with mock.patch.object(registry, "compute_execution_observation",
                               side_effect=AssertionError("remote observation")):
            self.assertEqual(registry.classify_identity(row, identity_reader),
                             ("stale", None, "compute-hosts-recorded-state-non-authoritative"))
            self.assertFalse(registry.is_alive(row, identity_reader))
            running = {**row, "status": "running", "execution": {**row["execution"],
                                                                       "receipt_state": "running"}}
            self.assertEqual(registry.classify_identity(running, identity_reader),
                             ("working", None, "compute-hosts-recorded-running"))
            self.assertTrue(registry.is_alive(running, identity_reader))
            self.assertEqual(registry.normalize_run("resource-1", row, Path("/fixture/registry.json"))["liveness"],
                             "exited")
        identity_reader.assert_not_called()
        with mock.patch.object(registry, "compute_execution_observation",
                               return_value={"state": "populated"}) as observer:
            self.assertFalse(registry.resource_process_group_quiescent(row))
            observer.assert_called_once_with(row)
        with mock.patch.object(registry, "compute_execution_observation",
                               return_value={"state": "quiescent"}):
            self.assertTrue(registry.resource_process_group_quiescent(row))

    def test_compute_run_projection_does_not_probe_a_local_pid(self):
        execution = {"kind": "compute-hosts", "run_id": "compute-1",
                     "host": "gpu-a", "receipt_state": "finished"}
        with mock.patch.object(registry, "proc_identity",
                               side_effect=AssertionError("local PID probe")):
            row = registry.normalize_run("resource-1", {"execution": execution},
                                         Path("/fixture/registry.json"))
        self.assertEqual(row["compute_run_id"], "compute-1")
        self.assertEqual(row["compute_host"], "gpu-a")
        self.assertEqual(row["liveness"], "exited")
        self.assertIsNone(row["pid"])

    def test_never_started_without_identity_is_exited_but_partial_identity_stays_stale(self):
        row = {
            "resource_policy": "supervised-owner", "status": "failed",
            "workflow_state": "FAILED_RETRYABLE", "failure_class": "resource-launch-incomplete",
            "exit_code": None, "sentinel": "/missing/sentinel", "launch_state": "not-started",
            "pid": None, "starttime": None, "command_hash": None,
        }
        self.assertEqual(registry.classify_identity(row),
                         ("exited", None, "launcher-recorded-not-started"))
        partial = {**row, "pid": 123}
        self.assertEqual(registry.classify_identity(partial),
                         ("stale", None, "recorded-identity-incomplete"))

    def test_legacy_jobs_identity_is_memoized_until_file_signature_changes(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            row = {"jobs": str(jobs), "parent_attempt_id": "att-owner",
                   "route": str(Path(td) / "route.json"), "pid_namespace": "pid:[fixture]",
                   "owner_wait": {"parent_attempt_id": "att-owner", "jobs": str(jobs),
                                  "owner_pid": 123, "owner_start": "456",
                                  "route_id": "rt-test", "route_hash": "hash"}}
            line = ("now\tdone\tx\tx\towner\tattempt_id=att-owner,worker_type=owner,"
                    f"owner_route_file={row['route']},owner_route_id=rt-test,"
                    "owner_route_hash=hash,pid=123,pid_start=456,pid_ns=pid:[fixture]\n")
            jobs.write_text(line)
            registry._LEGACY_RESOURCE_IDENTITY_CACHE.pop(str(jobs), None)
            original_read = Path.read_text
            reads = []

            def count_read(path, *args, **kwargs):
                if path == jobs:
                    reads.append(path)
                return original_read(path, *args, **kwargs)

            with mock.patch.object(Path, "read_text", count_read):
                registry.legacy_resource_identity(row)
                registry.legacy_resource_identity(row)
                self.assertEqual(len(reads), 1)
                jobs.write_text(line + "\n")
                registry.legacy_resource_identity(row)
            self.assertEqual(len(reads), 2)

    def test_pinned_legacy_identity_parses_only_rows_naming_the_attempt(self):
        import dispatch_contract
        import dispatch_registry_cache
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            route = str(Path(td) / "route.json")
            owner = ("now\tdone\tx\tx\towner\tattempt_id=att-owner,worker_type=owner,"
                     f"owner_route_file={route},owner_route_id=rt-test,owner_route_hash=hash,"
                     "pid=123,pid_start=456,pid_ns=pid:[fixture]\n")
            # Near-miss rows contain the needle text but name another attempt.
            near = ("now\tdone\tx\tx\tchild\tattempt_id=att-owner2,parent_attempt_id=att-owner\n"
                    "now\tdone\tx\tx\tother\tnote=x,attempt_id=att-other\n")
            filler = "".join(f"t\tdone\t/r\t/w\ts-{i}\tattempt_id=att-{i},note=row\n"
                             for i in range(300))
            row = {"jobs": str(jobs), "parent_attempt_id": "att-owner", "route": route,
                   "pid_namespace": "pid:[fixture]",
                   "owner_wait": {"parent_attempt_id": "att-owner", "jobs": str(jobs),
                                  "owner_pid": 123, "owner_start": "456",
                                  "route_id": "rt-test", "route_hash": "hash"}}

            def identities(text):
                jobs.write_text(text)
                found = []
                with mock.patch.object(registry, "recorded_governor_identity",
                                       side_effect=lambda meta: dict(meta)):
                    registry._LEGACY_RESOURCE_IDENTITY_CACHE.pop(str(jobs), None)
                    found.append(registry.legacy_resource_identity(row))
                    dispatch_contract._registry_metadata_items.cache_clear()
                    with dispatch_registry_cache.registry_read_scope():
                        found.append(registry.legacy_resource_identity(row))
                        found.append(registry.legacy_resource_identity(row))
                    parsed = dispatch_contract._registry_metadata_items.cache_info().misses
                return found, parsed

            found, parsed = identities(filler + near + owner)
            self.assertEqual(found[0]["owner_route_id"], "rt-test")
            self.assertEqual(found, [found[0]] * 3)
            # Only the owner and the needle-matching near-miss row are parsed.
            self.assertEqual(parsed, 2)
            found, _parsed = identities(filler + owner + owner)
            self.assertEqual(found, [None] * 3)
            found, _parsed = identities(filler + near)
            self.assertEqual(found, [None] * 3)

    def test_empty_log_has_no_output_timestamp_but_nonempty_log_does(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "resource.log"
            log.touch()
            raw = {"status": "succeeded", "log": str(log)}
            row = registry.normalize_run("run", raw, Path(tmp) / "runs.json")
            self.assertIsNone(row["log_updated_at"])
            self.assertEqual(row["log_size"], 0)
            log.write_text("observed output\n")
            row = registry.normalize_run("run", raw, Path(tmp) / "runs.json")
            self.assertEqual(row["log_updated_at"], log.stat().st_mtime)
            self.assertGreater(row["log_size"], 0)

    def test_normalize_preserves_additive_artifact_attribution_fields(self):
        raw = {
            "status": "succeeded",
            "artifact_root": "/artifacts",
            "route": "/artifacts/.runtime/routes/rt-a.json",
            "route_file": "/artifacts/.runtime/routes/rt-a.json",
            "route_id": "rt-a",
            "route_hash": "sha256:" + "a" * 64,
            "node": "test",
            "route_node": "test",
            "pid_namespace": "pid:[fixture]",
        }
        row = registry.normalize_run(
            "run-a", raw, Path("/registry.json"), identity_reader=lambda _pid: None,
        )
        for key in ("artifact_root", "route", "route_file", "route_id", "route_hash",
                    "node", "route_node", "pid_namespace"):
            self.assertEqual(row[key], raw[key])

    def test_missing_registry_is_typed_skip_with_valid_neighbor(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); index = base / "index.json"
            good = base / "good.json"; missing = base / "gone" / "registry.json"
            good.write_text(json.dumps({"schema_version": 1, "runs": {"good": {"pid": 7}}}))
            index.write_text(json.dumps({"schema_version": 1, "registries": {
                "good": {"path": str(good)}, "gone": {"path": str(missing)}}}))
            rows, diagnostics = registry.scan(index)
            self.assertEqual([r["run_id"] for r in rows], ["good"])
            self.assertEqual(diagnostics[0]["kind"], "missing-registry")
            self.assertEqual(diagnostics[0]["path"], str(missing))
            self.assertEqual(registry.counts(index)["malformed"], 0)
            self.assertEqual(registry.counts(index)["missing"], 1)

    def test_dangling_link_retains_registered_path_and_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); link = base / "link"; link.symlink_to(base / "absent")
            index = base / "index.json"
            for source in (link, link / "registry.json"):
                index.write_text(json.dumps({"schema_version": 1,
                    "registries": {"bad": {"path": str(source)}}}))
                paths, _ = registry.indexed_paths(index)
                self.assertEqual(paths, [source])
                _, diagnostics = registry.scan(index)
                self.assertNotEqual(diagnostics[0]["kind"], "missing-registry")
                self.assertEqual(diagnostics[0]["path"], str(source))

    def test_boot_change_is_host_bound_and_rejects_reused_pid(self):
        current = {'boot_id': '67138871-1a60-467f-b9dc-d46749025baa', 'boot_host': 'local'}
        row = {'pid': 123, 'starttime': '42', 'command_hash': 'hash',
               'boot_id': '83f954bc-4963-4dfa-9f2f-c8f3597900a6', 'boot_host': 'local'}
        with mock.patch.object(registry, 'boot_identity', return_value=current):
            self.assertEqual(registry.classify_identity(row, lambda _: row), ('exited', None, 'host-reboot'))
            for changes in ({'boot_host': 'foreign'}, {'boot_id': current['boot_id']},
                            {'boot_id': 'invalid'}, {'boot_host': None}):
                self.assertIsNone(registry.reboot_evidence({**row, **changes}))

    def test_shared_previous_boot_proof_requires_host_or_local_journal(self):
        current = {'boot_id': '67138871-1a60-467f-b9dc-d46749025baa', 'boot_host': 'local'}
        previous = '83f954bc-4963-4dfa-9f2f-c8f3597900a6'
        with mock.patch.object(registry, 'boot_identity', return_value=current), \
                mock.patch.object(registry, 'local_boot_history', return_value={previous, current['boot_id']}) as history:
            self.assertIsNotNone(registry.previous_boot_evidence({'boot_id': previous, 'boot_host': 'local'}))
            history.assert_not_called()
            self.assertIsNotNone(registry.previous_boot_evidence({'boot_id': previous}))
            for identity in ({}, None, {'boot_id': 'bad'}, {'boot_id': current['boot_id']},
                             {'boot_id': previous, 'boot_host': 'foreign'}, {'boot_id': previous, 'boot_host': None}):
                self.assertIsNone(registry.previous_boot_evidence(identity))
            with mock.patch.object(registry, 'local_boot_history', return_value={current['boot_id']}):
                self.assertIsNone(registry.previous_boot_evidence({'boot_id': previous}))

    def test_legacy_boot_reset_requires_all_existing_launch_coordinates(self):
        current, previous = '67138871-1a60-467f-b9dc-d46749025baa', '83f954bc-4963-4dfa-9f2f-c8f3597900a6'
        row = {'resource_policy': 'supervised-owner', 'started_at': 50, 'starttime': '10000', 'owner_wait': {'bound': True},
               'pid_namespace': os.readlink('/proc/self/ns/pid')}
        with mock.patch.object(registry, '_boot_epoch', return_value=100), \
                mock.patch.object(Path, 'read_text', return_value='10.0 1.0'), \
                mock.patch.object(os, 'sysconf', return_value=100), \
                mock.patch.object(registry, 'legacy_resource_identity', return_value={'boot_id': previous}), \
                mock.patch.object(registry, 'boot_identity', return_value={'boot_id': current, 'boot_host': 'local'}), \
                mock.patch.object(registry, 'local_boot_history', return_value={previous, current}):
            self.assertIsNotNone(registry.reboot_evidence(row))
            for changes in ({'started_at': 101}, {'starttime': '500'}, {'started_at': None},
                            {'pid_namespace': 'foreign'}, {'resource_policy': None}, {'boot_id': 'bad'}):
                self.assertIsNone(registry.reboot_evidence({**row, **changes}))
            with mock.patch.object(registry, 'local_boot_history', return_value={current}):
                self.assertIsNone(registry.reboot_evidence(row))
            with mock.patch.object(registry, 'legacy_resource_identity',
                    return_value={'boot_id': previous, 'boot_host': 'foreign'}):
                self.assertIsNone(registry.reboot_evidence(row))
        with mock.patch.object(registry, '_boot_epoch', return_value=None):
            self.assertIsNone(registry.reboot_evidence(row))

    def test_legacy_boot_is_bound_to_the_exact_owner_and_governor_claim(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); jobs = root / 'jobs.log'
            namespace = os.readlink('/proc/self/ns/pid')
            row = {'jobs': str(jobs), 'parent_attempt_id': 'att-owner', 'route': str(root / 'route.json'),
                   'pid_namespace': namespace, 'owner_wait': {'parent_attempt_id': 'att-owner',
                       'jobs': str(jobs), 'owner_pid': 123, 'owner_start': '456',
                       'route_id': 'rt-test', 'route_hash': 'hash'}}
            meta = {'attempt_id': 'att-owner', 'worker_type': 'owner', 'owner_route_file': row['route'],
                    'owner_route_id': 'rt-test', 'owner_route_hash': 'hash', 'pid': '123',
                    'pid_start': '456', 'pid_ns': namespace, 'artifact_root': str(root)}
            jobs.write_text('now\tdone\tx\tx\towner\t' + ','.join(k+'='+v for k,v in meta.items()) + '\n')
            state = root / '.runtime/model-worker-governor/state.json'
            state.parent.mkdir(parents=True)
            identity = {'pid': 123, 'starttime': '456', 'pid_namespace': int(namespace[5:-1]),
                        'boot_id': '83f954bc-4963-4dfa-9f2f-c8f3597900a6'}
            state.write_text(json.dumps({'claims': {'token': {'claimant_identity': identity}}}))
            self.assertEqual(registry.legacy_resource_boot(row), identity['boot_id'])
            state.write_text(json.dumps({'claims': {'token': {'claimant_identity': identity},
                'ambiguous': {'claimant_identity': {**identity, 'boot_id': '67138871-1a60-467f-b9dc-d46749025baa'}}}}))
            self.assertIsNone(registry.legacy_resource_boot(row))
            state.write_text(json.dumps({'claims': {'token': {'claimant_identity': identity}}}))
            for change in ({'owner_pid': 124}, {'owner_start': '457'}, {'route_hash': 'foreign'},
                           {'jobs': 'foreign'}, {'parent_attempt_id': 'foreign'}):
                self.assertIsNone(registry.legacy_resource_boot({**row, 'owner_wait': {**row['owner_wait'], **change}}))
            for change in ({'pid': 124}, {'starttime': '457'}, {'pid_namespace': 1}):
                state.write_text(json.dumps({'claims': {'token': {'claimant_identity': {**identity, **change}}}}))
                self.assertIsNone(registry.legacy_resource_boot(row))

    def test_local_journal_requires_current_boot_and_never_uses_unreadable_history(self):
        from types import SimpleNamespace
        current, previous = '67138871-1a60-467f-b9dc-d46749025baa', '83f954bc-4963-4dfa-9f2f-c8f3597900a6'
        output = f'-1 {previous.replace("-", "")} previous\n0 {current.replace("-", "")} current\n'
        for code, stdout, expected in ((0, output, {current, previous}),
                                       (0, output.splitlines()[0], set()), (1, output, set())):
            registry.local_boot_history.cache_clear()
            with mock.patch.object(registry.subprocess, 'run', return_value=SimpleNamespace(returncode=code, stdout=stdout)):
                self.assertEqual(registry.local_boot_history(current), expected)
        registry.local_boot_history.cache_clear()
        with mock.patch.object(registry.subprocess, 'run', side_effect=PermissionError()):
            self.assertEqual(registry.local_boot_history(current), set())
        registry.local_boot_history.cache_clear()

    def test_live_exited_and_pid_reuse(self):
        row = {"pid": 2147483647, "starttime": "11", "command_hash": "abc"}
        exact = lambda pid: {"pid": pid, "starttime": "11", "command_hash": "abc"}
        reused = lambda pid: {"pid": pid, "starttime": "12", "command_hash": "def"}
        self.assertEqual(registry.classify_identity(row, exact)[0], "working")
        self.assertEqual(registry.classify_identity(row, lambda _pid: None)[0], "exited")
        self.assertEqual(registry.classify_identity(row, reused)[0], "stale")
        unreadable = {**row, "pid": os.getpid()}
        self.assertEqual(registry.classify_identity(
            unreadable, lambda _pid: None)[0], "stale")

    def test_launched_identity_waits_out_the_empty_cmdline_after_exec(self):
        child = mock.Mock(pid=4242)
        child.poll.return_value = None
        found = {"pid": 4242, "starttime": "7", "command_hash": "abc"}
        with mock.patch.object(registry, "proc_identity", side_effect=[None, None, found]) as probe, \
                mock.patch.object(registry.time, "sleep") as sleep:
            self.assertEqual(registry.launched_identity(child), found)
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_launched_identity_stops_once_the_child_has_exited(self):
        child = mock.Mock(pid=4242)
        child.poll.return_value = 0
        with mock.patch.object(registry, "proc_identity", return_value=None) as probe, \
                mock.patch.object(registry.time, "sleep") as sleep:
            self.assertIsNone(registry.launched_identity(child))
        self.assertEqual(probe.call_count, 1)
        sleep.assert_not_called()

    def test_supervised_success_waits_for_its_recorded_process_group_to_drain(self):
        leader = subprocess.Popen(
            [sys.executable, "-c", "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])"],
            start_new_session=True,
        )
        identity = registry.launched_identity(leader)
        self.assertIsNotNone(identity)
        namespace = os.readlink("/proc/self/ns/pid")
        leader.wait(timeout=5)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        sentinel = Path(temporary.name) / "run.exit"
        sentinel.write_text("0")
        row = {
            **identity, "run_id": "supervised-process-group",
            "resource_policy": "supervised-owner", "status": "succeeded",
            "workflow_state": "STAGE_SUCCEEDED", "exit_code": 0,
            "process_group": leader.pid, "pid_namespace": namespace,
            "sentinel": str(sentinel),
        }
        try:
            self.assertEqual(registry.classify_identity(row)[0], "exited")
            self.assertFalse(registry.resource_process_group_quiescent(row))
            os.killpg(leader.pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while not registry.resource_process_group_quiescent(row):
                self.assertLess(time.monotonic(), deadline, "payload child remained in its process group")
                time.sleep(0.01)
            self.assertTrue(registry.resource_process_group_quiescent(row))
        finally:
            try:
                os.killpg(leader.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_an_unrelated_process_exiting_mid_scan_does_not_hold_a_drained_group(self):
        leader = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        identity = registry.launched_identity(leader)
        self.assertIsNotNone(identity)
        leader.wait(timeout=5)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        sentinel = Path(temporary.name) / "run.exit"
        sentinel.write_text("0")
        row = {
            **identity, "run_id": "drained-process-group",
            "resource_policy": "supervised-owner", "status": "succeeded",
            "workflow_state": "STAGE_SUCCEEDED", "exit_code": 0,
            "process_group": leader.pid, "pid_namespace": os.readlink("/proc/self/ns/pid"),
            "sentinel": str(sentinel),
        }
        self.assertEqual(registry.classify_identity(row)[0], "exited")
        self.assertTrue(registry.resource_process_group_quiescent(row))
        # Another of this user's processes exits while the scan reads it: its
        # open /proc stat file reads ESRCH, or its entry is already gone.
        vanished = Path(f"/proc/{os.getpid()}")
        real_read_text, real_stat = Path.read_text, Path.stat

        def read_text(path, *args, **kwargs):
            if path == vanished / "stat":
                raise ProcessLookupError(errno.ESRCH, "No such process")
            return real_read_text(path, *args, **kwargs)

        def stat(path, *args, **kwargs):
            if path == vanished:
                raise ProcessLookupError(errno.ESRCH, "No such process")
            return real_stat(path, *args, **kwargs)

        for name, replacement in (("read_text", read_text), ("stat", stat)):
            with self.subTest(vanished_during=name), \
                    mock.patch.object(Path, name, replacement):
                self.assertTrue(registry.resource_process_group_quiescent(row))

    def test_detached_wrapper_zombie_is_exited_not_unreadable(self):
        # A detached wrapper (no launch controller) is reaped by whichever
        # process adopted it. Until then /proc keeps a zombie with no cmdline.
        child = subprocess.Popen(["/bin/sleep", "30"])
        try:
            # Until exec has set it up, a new child's cmdline can still be empty.
            deadline = time.monotonic() + 10
            identity = registry.proc_identity(child.pid)
            while identity is None and time.monotonic() < deadline:
                time.sleep(0.01)
                identity = registry.proc_identity(child.pid)
            self.assertIsNotNone(identity)
            row = {key: identity[key] for key in registry.IDENTITY_KEYS}
            os.kill(child.pid, signal.SIGKILL)
            stat = Path(f"/proc/{child.pid}/stat")
            deadline = time.monotonic() + 10
            while (stat.read_text().rsplit(") ", 1)[1].split()[0] != "Z"
                   and time.monotonic() < deadline):
                time.sleep(0.01)
            self.assertEqual(stat.read_text().rsplit(") ", 1)[1].split()[0], "Z")
            self.assertEqual(registry.classify_identity(row),
                             ("exited", None, "process-exited-unreaped"))
            # A controller-launched child keeps the exact-owner rule.
            controlled = {**row, "launch_controller": {"pid": os.getpid()}}
            self.assertEqual(registry.classify_identity(controlled),
                             ("stale", None, "process-identity-unreadable"))
            self.assertEqual(registry.classify_identity({**row, "starttime": "1"})[0], "stale")
        finally:
            if child.returncode is None:
                child.kill()
                child.wait()
        self.assertEqual(registry.classify_identity(row), ("exited", None, "process-absent"))

    def test_owned_zombie_requires_exact_kernel_and_controller_identity(self):
        pid = 999999999
        parent = registry.proc_identity(os.getpid())
        namespace = os.readlink("/proc/self/ns/pid")
        row = {"pid": pid, "starttime": "42", "command_hash": "a" * 64,
               "process_group": pid, "pid_namespace": namespace,
               "resource_policy": "supervised-owner", "launch_state": "started",
               "owner_wait": {"launch_scope": "codex-owner-controller"},
               "launch_controller": {**parent, "pid_namespace": namespace}}
        # Linux fields 3/4/5/22: state, parent PID, group, kernel start tick.
        fields = ["Z", str(os.getpid()), str(pid)] + ["0"] * 16 + ["42"]
        raw = f"{pid} (wrapper) " + " ".join(fields)
        reader = lambda candidate: parent if candidate == os.getpid() else None
        with mock.patch.object(Path, "read_text", return_value=raw), \
             mock.patch.object(Path, "read_bytes", return_value=b""), \
             mock.patch.object(Path, "exists", return_value=True):
            self.assertEqual(registry.classify_identity(row, reader),
                             ("reaping", None, "owned-wrapper-awaiting-reap"))
            self.assertFalse(registry.is_alive(row, reader))
            for change in ({"starttime": "43"}, {"process_group": pid + 1},
                           {"pid_namespace": "pid:[foreign]"}, {"launch_state": "claimed"},
                           {"resource_policy": "verified-resume"}, {"owner_wait": {}},
                           {"launch_controller": {**parent, "starttime": "0", "pid_namespace": namespace}},
                           {"launch_controller": {**parent, "command_hash": "foreign", "pid_namespace": namespace}}):
                with self.subTest(change=change):
                    self.assertEqual(registry.classify_identity({**row, **change}, reader)[0], "stale")
            for index, value in ((0, "S"), (1, str(os.getpid() + 1)), (2, str(pid + 1)), (19, "43")):
                altered = list(fields); altered[index] = value
                with self.subTest(field=index), mock.patch.object(Path, "read_text",
                        return_value=f"{pid} (wrapper) " + " ".join(altered)):
                    self.assertEqual(registry.classify_identity(row, reader)[0], "stale")
            for method in ("read_text", "read_bytes"):
                with self.subTest(error=method), mock.patch.object(Path, method, side_effect=PermissionError()):
                    self.assertEqual(registry.classify_identity(row, reader)[0], "stale")
            with mock.patch.object(Path, "read_text", side_effect=[raw, raw.replace("42", "43")]):
                self.assertEqual(registry.classify_identity(row, reader)[0], "stale")
            with mock.patch.object(Path, "read_text", return_value="malformed"):
                self.assertEqual(registry.classify_identity(row, reader)[0], "stale")
            with mock.patch.object(Path, "read_bytes", return_value=b"live-command\0"):
                self.assertEqual(registry.classify_identity(row, reader)[0], "stale")

    def test_multi_project_index_and_malformed_registry_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            index = root / "index.json"
            good_a = root / "a.json"
            good_b = root / "b.json"
            bad = root / "bad.json"
            identity = {"pid": 7, "starttime": "11", "command_hash": "abc"}
            for path, cwd, run_id in (
                (good_a, "/projects/a", "a1"),
                (good_b, "/projects/b", "b1"),
            ):
                path.write_text(json.dumps({
                    "schema_version": 1,
                    "runs": {run_id: {**identity, "cwd": cwd, "status": "running"}},
                }))
                registry.register_registry(path, index)
            bad.write_text("{")
            # An indexed registry can later become malformed; collection must
            # preserve every other project.
            payload = json.loads(index.read_text())
            payload["registries"]["bad"] = {"path": str(bad)}
            index.write_text(json.dumps(payload))
            rows, diagnostics = registry.scan(index, identity_reader=lambda pid: identity)
            self.assertEqual({row["run_id"] for row in rows}, {"a1", "b1"})
            self.assertEqual({row["cwd"] for row in rows}, {"/projects/a", "/projects/b"})
            self.assertTrue(any(d["kind"] == "malformed-registry" for d in diagnostics))

    def test_registry_view_uses_exact_route_config_and_public_record(self):
        import hashlib
        import resource_progress
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = root / "artifacts"
            artifact.mkdir()
            project = root / "project"
            (project / "configs").mkdir(parents=True)
            output = project / "results"
            output.mkdir()
            (output / "logs").mkdir()
            (output / "logs" / "epochs.jsonl").write_text(
                '{"epoch":1,"split":"valid","losses":{"L_se":-8.77}}\n')
            config = project / "configs" / "run.json"
            config.write_text(json.dumps({"output_dir": "results"}))
            route = artifact / "route.json"
            route.write_text(json.dumps({"route_id": "route-fixture",
                                         "artifact_root": str(artifact)}))
            source = root / "writer" / "observation.json"
            source.parent.mkdir()
            source.write_text(json.dumps({"observed_at": 7190, "finished_cells": 21}))
            proof = root / "proof.json"
            proof.write_text(json.dumps({"target": "run-view", "writer": "writer-view"}))
            binding = {"target_run_id": "run-view", "row_key": "run",
                "path": str(source), "writer_run_id": "writer-view",
                "timestamp_pointer": "/observed_at", "scope": "shared-results",
                "counters": [{"name": "finished_cells", "pointer": "/finished_cells",
                              "comparison_eligible": False}],
                "proofs": [{"path": str(proof),
                    "sha256": hashlib.sha256(proof.read_bytes()).hexdigest(),
                    "assertions": [{"pointer": "/target", "equals": "run-view"},
                                   {"pointer": "/writer", "equals": "writer-view"}]}]}
            state_path = root / "runtime" / "state.json"
            state_path.parent.mkdir()
            identity = {"pid": 77, "starttime": "9", "command_hash": "a" * 64,
                        "process_group": 77}
            row = {**identity, "cwd": str(project), "status": "running",
                   "started_at": 0, "route": str(route), "route_id": "route-fixture",
                   "command": ["python", "train.py", "--config", "configs/run.json"],
                   "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
                   "observation": {"source_bindings": [binding]}}
            with mock.patch.object(resource_progress, "_observation_state_path", return_value=state_path), \
                    mock.patch.object(registry, "proc_identity", return_value=identity):
                view = registry.normalize_run("run-view", row, root / "resource-runs.json",
                    identity_reader=lambda _pid: identity, now=7200)
            self.assertEqual(view["liveness"], "working")
            self.assertIsNone(view["experiment_run_record"])
            self.assertFalse(state_path.exists())
            self.assertFalse((artifact / "resource-runs" / "run-view.json").exists())
            with mock.patch.object(resource_progress, "_observation_state_path", return_value=state_path), \
                    mock.patch.object(registry, "proc_identity", return_value=identity):
                observed = registry.observe_run_if_due(
                    "run-view", row, root / "resource-runs.json", now=7200)
                unchanged = {key: value for key, value in row.items() if key != "observation"}
                resumed = registry.observe_run_if_due(
                    "run-view", unchanged, root / "resource-runs.json", now=7261)
                view = registry.normalize_run("run-view", row, root / "resource-runs.json",
                    identity_reader=lambda _pid: identity, now=7201)
            self.assertEqual(observed["experiment_run_record"]["route_id"], "route-fixture")
            self.assertEqual(view["experiment_run_record"]["output_dir"], str(output))
            self.assertEqual(view["experiment_run_record"]["metric_summary"]["latest"]["value"], -8.77)
            self.assertEqual(resumed["experiment_observation"]["observation"]
                ["source_observations"][0]["value"], 21)

    def test_rootless_stale_observer_resumes_exact_admission_only_history(self):
        import resource_metric_sources as metrics
        import resource_progress

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            artifact = base / "artifacts"
            artifact.mkdir()
            route = artifact / "route.data"
            route.write_text(json.dumps({"route_id": "route-admission-only",
                                         "artifact_root": str(artifact)}))
            registry_dir = base / "registry"
            bundle = registry_dir / "arbitrary-admission-bundle"
            bundle.mkdir(parents=True)
            registry_path = registry_dir / "resource-runs.json"
            run_id = "rootless-admission-only-run"
            (bundle / "arbitrary-context.data").write_text(json.dumps({
                "run_id": run_id, "started_at": 800}))
            (bundle / "prior-admission-record.data").write_text(json.dumps({
                "admitted": False, "snapshot": {"t": 850}}))
            identity = {"pid": 77, "starttime": "9", "command_hash": "a" * 64,
                        "process_group": 77}
            raw_row = {**identity, "run_id": run_id, "status": "running",
                "started_at": 800, "pid_namespace": "pid:[foreign-fixture]",
                "route": str(route), "route_id": "route-admission-only",
                "jobs": str(base / "dispatch" / "jobs.log"),
                "execution": {"kind": "local-resource", "run_id": "execution-admission"},
                "owner_wait": {"harness": "codex", "session_id": "fixture-owner"}}
            registry_bytes = json.dumps({"schema_version": 1,
                "runs": {run_id: raw_row}}, sort_keys=True).encode() + b"\n"
            registry_path.write_bytes(registry_bytes)

            decoded = metrics.read_admission_sources([registry_dir], run_id=run_id,
                registry_path=registry_path, now=900)
            self.assertEqual(decoded["coverage"], "observed")
            self.assertEqual(len(decoded["samples"]), 1)
            summary, admission_state = resource_progress._admission_summary(
                {}, decoded, 900, run_id, "run", None)
            observation = {"admission": summary, "reached": None, "total": None,
                "unit": None, "rate_per_second": None, "eta_seconds": None,
                "elapsed_seconds": None, "stagnation": {"state": "unknown"},
                "attention": []}
            state_path = resource_progress._observation_state_path(
                raw_row, registry_path, "run")
            state_path.parent.mkdir(parents=True, exist_ok=True)
            resource_progress._write_observation_state(state_path, {
                "schema_version": resource_progress.OBSERVATION_VERSION,
                "run_id": run_id, "row_key": "run", **admission_state,
                "admission_summary": summary, "admission_coverage": decoded["coverage"],
                "last_admission_observed_at": 900, "last_observer_poll_at": 900,
                "public_status": "stale", "clock_scope": "unknown",
                "row_started_at": None, "schedule_started_at": None,
                "cursor": None, "samples": [], "sources": [],
                "observation": observation, "event_keys": [], "outbox": []})
            self.assertNotIn("source_bindings", resource_progress._read_observation_state(state_path))
            self.assertNotIn("source_observations", resource_progress._read_observation_state(state_path))
            state_before_read = state_path.read_bytes()

            jobs_path = Path(raw_row["jobs"])
            jobs_path.parent.mkdir(parents=True, exist_ok=True)
            jobs_path.touch()
            notices = []

            def capture_notice(*args, **kwargs):
                notices.append(kwargs)
                return {"state": "pending"}

            with mock.patch.object(registry, "proc_identity", return_value=identity), \
                    mock.patch("session_notice.notify", side_effect=capture_notice):
                normalized = registry.normalize_run(run_id, raw_row, registry_path,
                    identity_reader=lambda _pid: identity, now=901)
                self.assertEqual(state_path.read_bytes(), state_before_read)
                self.assertEqual(normalized["liveness"], "stale")
                self.assertEqual(normalized["state_evidence"]["reason"],
                                 "process-namespace-mismatch")
                self.assertEqual(normalized["experiment_observation"]["observation"],
                                 observation)

                (bundle / "unrelated-admission-record.data").write_text(json.dumps({
                    "admitted": False, "snapshot": {"t": 950},
                    "preceding_evaluation_leases": [{"task": "evaluation",
                                                       "host": "fixture-host"}]}))
                due = registry.observe_run_if_due(run_id, raw_row, registry_path, now=1000)
                final_state = resource_progress._read_observation_state(state_path)
                public = resource_progress.read_public_run_record(artifact, run_id)

            self.assertEqual(registry_path.read_bytes(), registry_bytes)
            self.assertNotIn("output_dir", raw_row)
            self.assertNotIn("progress_file", raw_row)
            self.assertNotIn("observation", raw_row)
            self.assertEqual(due["liveness"], "stale")
            self.assertEqual(due["state_evidence"]["reason"],
                             "process-namespace-mismatch")
            self.assertEqual(final_state["admission_counts"],
                             {"admitted": 0, "denied": 2, "unknown": 0})
            self.assertEqual(final_state["admission_current_state"], "waiting")
            self.assertEqual(final_state["clock_scope"], "row")
            self.assertEqual(final_state["row_started_at"], 800)
            self.assertEqual(final_state["source_bindings"], [])
            self.assertEqual(final_state["source_observations"], [])
            final_observation = final_state["observation"]
            self.assertEqual(final_observation["admission"]["denied_sample_ratio"], 1.0)
            self.assertIsNone(final_observation["admission"]["wait_duration_seconds"])
            self.assertIsNone(final_observation["admission"]["time_ratio"])
            self.assertIsNone(final_observation["reached"])
            self.assertIsNone(final_observation["total"])
            self.assertIsNone(final_observation["unit"])
            self.assertIsNone(final_observation["eta_seconds"])
            self.assertEqual(final_observation["elapsed_seconds"], 200)
            self.assertIsNone(final_observation["rate_per_second"])
            self.assertIsNone(final_observation["predicted_total_seconds"])
            self.assertEqual(public["status"], "stale")
            self.assertEqual(public["admission"]["denied_samples"], 2)
            self.assertEqual(public["progress"]["observed_counters"], [])
            self.assertIsNone(public["progress"]["reached"])
            self.assertIsNone(public["progress"]["total"])
            self.assertIsNone(public["progress"].get("eta_seconds"))
            self.assertEqual(public["rows"][0]["started_at"], 800)
            self.assertEqual(public["rows"][0]["clock_scope"], "row")
            self.assertEqual(len(notices), 1)
            self.assertIn("admission=waiting", notices[0]["text"])
            event = next(item for item in final_state["outbox"]
                         if item.get("kind") == "attention")
            self.assertEqual(event["recipients"][0]["session_id"], "fixture-owner")
            self.assertEqual(notices[0]["lineage"], event["recipients"][0]["lineage"])

    def test_rootless_stale_observer_resumes_exact_persisted_generic_binding(self):
        import hashlib
        import resource_progress

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            artifact = base / "artifacts"
            artifact.mkdir()
            route = artifact / "route.json"
            route.write_text(json.dumps({"route_id": "route-fixture",
                                         "artifact_root": str(artifact)}))
            registry_dir = base / "registry"
            bundle = registry_dir / "admission-bundle"
            bundle.mkdir(parents=True)
            registry_path = registry_dir / "resource-runs.json"
            jobs_path = base / "dispatch" / "jobs.log"
            jobs_path.parent.mkdir()
            jobs_path.touch()
            run_id, writer_id = "rootless-stale-run", "independent-writer"
            (bundle / "execution-record.json").write_text(json.dumps({"run_id": run_id}))
            admission_path = bundle / "arbitrary-admission.json"
            admission_path.write_text(json.dumps({"admitted": False,
                "snapshot": {"t": 900}, "first_snapshot": {"t": 800},
                "reasons": ["capacity"],
                "preceding_evaluation_leases": [{"task": "evaluation", "host": "h1"}]}))
            source = base / "writer" / "observation.json"
            source.parent.mkdir()
            source.write_text(json.dumps({"observed_at": 950, "finished_cells": 11}))
            proof = base / "writer-proof.json"
            proof.write_text(json.dumps({"target": run_id, "writer": writer_id,
                "command": ["writer", "--target", run_id]}))
            binding = {"target_run_id": run_id, "row_key": "run",
                "path": str(source), "writer_run_id": writer_id,
                "timestamp_pointer": "/observed_at", "scope": "shared-results",
                "counters": [{"name": "finished_cells", "pointer": "/finished_cells",
                              "comparison_eligible": False}],
                "proofs": [{"path": str(proof),
                    "sha256": hashlib.sha256(proof.read_bytes()).hexdigest(),
                    "assertions": [{"pointer": "/target", "equals": run_id},
                                   {"pointer": "/writer", "equals": writer_id},
                                   {"pointer": "/command/2", "equals": run_id}]}]}
            identity = {"pid": 77, "starttime": "9", "command_hash": "a" * 64,
                        "process_group": 77}
            raw_row = {**identity, "run_id": run_id, "status": "running",
                "pid_namespace": "pid:[foreign-fixture]", "route": str(route),
                "route_id": "route-fixture", "jobs": str(jobs_path),
                "execution": {"kind": "local-resource", "run_id": "execution-a"},
                "owner_wait": {"harness": "codex", "session_id": "fixture-owner"}}
            registry_bytes = json.dumps({"schema_version": 1,
                "runs": {run_id: raw_row}}, sort_keys=True).encode() + b"\n"
            registry_path.write_bytes(registry_bytes)
            initial = {**raw_row, "observation": {"source_bindings": [binding]}}
            notices = []

            def capture_notice(*args, **kwargs):
                notices.append(kwargs)
                return {"state": "pending"}

            with mock.patch.object(registry, "proc_identity", return_value=identity), \
                    mock.patch("session_notice.notify", side_effect=capture_notice):
                first = registry.observe_run_if_due(run_id, initial, registry_path, now=1000)
                first_state_path = resource_progress._observation_state_path(
                    {**raw_row, "run_id": run_id}, registry_path, "run", create=False)
                first_state = resource_progress._read_observation_state(first_state_path)
                raw_view = registry.normalize_run(run_id, raw_row, registry_path,
                    identity_reader=lambda _pid: identity, now=1001)
                unchanged_poll = registry.observe_run_if_due(
                    run_id, raw_row, registry_path, now=1061)

                # The registry row remains byte-for-byte unchanged and carries no
                # metadata on this later normal call; only the exact state retains it.
                source.write_text(json.dumps({"observed_at": 1120, "finished_cells": 21}))
                admission_path.write_text(json.dumps({"admitted": True,
                    "snapshot": {"t": 1100}, "first_snapshot": {"t": 800}}))
                resumed = registry.observe_run_if_due(run_id, raw_row, registry_path, now=1122)
                normalized = registry.normalize_run(run_id, raw_row, registry_path,
                    identity_reader=lambda _pid: identity, now=1123)
                final_state = resource_progress._read_observation_state(first_state_path)
                public = resource_progress.read_public_run_record(artifact, run_id)

            self.assertEqual(registry_path.read_bytes(), registry_bytes)
            self.assertNotIn("started_at", raw_row)
            self.assertNotIn("output_dir", raw_row)
            self.assertNotIn("progress_file", raw_row)
            for view in (first, raw_view, resumed, normalized):
                self.assertEqual(view["liveness"], "stale")
                self.assertEqual(view["state_evidence"]["reason"],
                                 "process-namespace-mismatch")
            self.assertEqual(first["experiment_observation"]["observation"]
                ["source_observations"][0]["value"], 11)
            self.assertEqual(raw_view["experiment_observation"]["observation"]
                ["source_observations"][0]["value"], 11)
            self.assertEqual(resumed["experiment_observation"]["observation"]
                ["source_observations"][0]["value"], 21)
            self.assertEqual(unchanged_poll["experiment_observation"]["observation"]
                ["source_observations"][0]["value"], 11)
            normalized_observation = normalized["experiment_observation"]["observation"]
            self.assertEqual(normalized_observation["source_observations"][0]["value"], 21)
            # The admission file reports the latest decision, while current
            # admission status remains unknown because this registry row has
            # no trustworthy birth clock.
            self.assertEqual(normalized_observation["admission"]["state"], "unknown")
            self.assertEqual(normalized_observation["admission"]["admitted_samples"], 1)
            self.assertEqual(normalized_observation["admission"]["denied_samples"], 1)
            self.assertIsNone(normalized_observation["reached"])
            self.assertIsNone(normalized_observation["total"])
            self.assertIsNone(normalized_observation["unit"])
            self.assertIsNone(normalized_observation["rate_per_second"])
            self.assertIsNone(normalized_observation["eta_seconds"])
            self.assertIsNone(normalized_observation["elapsed_seconds"])
            self.assertIsNone(final_state["row_started_at"])
            self.assertEqual(final_state["clock_scope"], "unknown")
            self.assertEqual(final_state["source_bindings"], [binding])
            self.assertEqual(final_state["source_observations"][0]["value"], 21)
            self.assertNotIn("final:run", final_state["event_keys"])
            self.assertFalse(any(event.get("kind") == "final"
                                 for event in final_state["outbox"]))
            self.assertEqual(final_state["admission_counts"],
                             {"admitted": 1, "denied": 1, "unknown": 0})
            self.assertEqual(final_state["admission_latest"]["state"], "admitted")
            self.assertEqual(public["status"], "stale")
            self.assertIsNone(public["ended_at"])
            self.assertEqual(public["progress"]["observed_counters"][0]["value"], 21)
            self.assertEqual(public["admission"]["state"], "unknown")
            self.assertIsNone(public["progress"]["reached"])
            self.assertIsNone(public["progress"]["total"])
            self.assertIsNone(public["progress"].get("eta_seconds"))
            self.assertIsNone(public["eta"])
            self.assertIsNone(public["rows"][0]["started_at"])
            self.assertEqual(public["rows"][0]["clock_scope"], "unknown")

            first_notice = next(event for event in first_state["outbox"]
                if "raw counter=finished_cells:11" in event["text"])
            resumed_notice = next(event for event in final_state["outbox"]
                if "raw counter=finished_cells:21" in event["text"])
            self.assertNotEqual(first_notice["key"], resumed_notice["key"])
            self.assertEqual(first_notice["recipients"], resumed_notice["recipients"])
            self.assertIn("admission=unknown", resumed_notice["text"])
            self.assertIn("samples denied/valid=1/2", resumed_notice["text"])
            self.assertIn("raw counter=finished_cells:21", notices[-1]["text"])
            self.assertIn("samples denied/valid=1/2", notices[-1]["text"])
            self.assertEqual(notices[-1]["lineage"], resumed_notice["recipients"][0]["lineage"])

            mismatched_execution = {**raw_row, "execution": {"kind": "local-resource",
                "run_id": "execution-b"}}
            unrelated_identity = {**raw_row, "starttime": "10"}
            mismatched = registry.normalize_run(run_id, mismatched_execution, registry_path,
                identity_reader=lambda _pid: identity, now=1063)
            unrelated = registry.normalize_run("other-stale-run", unrelated_identity,
                registry_path, identity_reader=lambda _pid: identity, now=1063)
            self.assertIsNone(mismatched["experiment_observation"])
            self.assertIsNone(unrelated["experiment_observation"])

    def test_explicit_observer_requires_exact_terminal_evidence_and_reuses_one_notice(self):
        import resource_progress
        import dispatch_pending_delivery
        from peer_obligations import ObligationStore

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "dispatch" / "jobs.log"
            jobs.parent.mkdir()
            jobs.write_text("")
            artifact = root / "artifact"
            artifact.mkdir()
            output = root / "run"
            (output / "logs").mkdir(parents=True)
            (output / "logs" / "epochs.jsonl").write_text(
                '{"epoch":1,"split":"valid","losses":{"L_se":-8.0}}\n')
            registry_path = root / "resource-runs.json"
            run_id = "observer-terminal-evidence"
            row = {
                "run_id": run_id, "pid": 99999999, "starttime": "1",
                "command_hash": "c" * 64, "status": "running", "started_at": 100,
                "output_dir": str(output), "artifact_root": str(artifact),
                "jobs": str(jobs), "notify": {"harness": "claude", "id": "fixture-supervisor"},
            }
            registry_path.write_text(json.dumps({"schema_version": 1, "runs": {run_id: row}}))
            state_path = resource_progress._observation_state_path(row, registry_path, "run")
            statuses = (("unknown", "unknown"), ("stale", "stale"),
                        ("reaping", "reaping"), ("exited", "unknown"))
            with mock.patch.object(resource_progress, "observe_experiment",
                                   wraps=resource_progress.observe_experiment) as observe, \
                    mock.patch("session_notice.notify",
                               side_effect=AssertionError("unverified state sent a notice")):
                for index, (liveness, public_status) in enumerate(statuses):
                    with self.subTest(liveness=liveness), \
                            mock.patch.object(registry, "classify_identity",
                                return_value=(liveness, None, "fixture-" + liveness)):
                        view = registry.observe_run_if_due(run_id, row, registry_path,
                            now=120 + index)
                    self.assertFalse(observe.call_args.kwargs["final"])
                    public = view["experiment_run_record"]
                    self.assertEqual(public["status"], public_status)
                    self.assertIsNone(public["ended_at"])
                    self.assertIsNone(public["rows"][0]["ended_at"])
                    state = resource_progress._read_observation_state(state_path)
                    self.assertNotIn("final:run", state.get("event_keys", []))
                    self.assertFalse(any(event.get("kind") == "final"
                                         for event in state.get("outbox", [])))
            self.assertEqual(ObligationStore(jobs.parent).list(kinds={"run-notice"}), [])
            self.assertFalse((jobs.parent / "pending-delivery").exists())

            sentinel = root / "run.exit"
            sentinel.write_text("7\n")
            ended_at = 125.0
            terminal = {**row, "status": "failed", "workflow_state": "FAILED_RETRYABLE",
                        "failure_class": "exit-7", "exit_code": 7, "sentinel": str(sentinel),
                        "ended_at": ended_at}
            registry_path.write_text(json.dumps({"schema_version": 1,
                                                  "runs": {run_id: terminal}}))
            with mock.patch.object(resource_progress, "observe_experiment",
                                   wraps=resource_progress.observe_experiment) as observe, \
                    mock.patch("peer_obligations.ensure_normal_runner", return_value=False), \
                    mock.patch("peer_obligations.request_dispatch_reconciliation", return_value=False), \
                    mock.patch.object(registry, "classify_identity",
                        return_value=("exited", None, "process-absent")):
                final_view = registry.observe_run_if_due(run_id, terminal, registry_path, now=180)
                self.assertTrue(observe.call_args.kwargs["final"])
                repeat_view = registry.observe_run_if_due(run_id, terminal, registry_path, now=181)
                self.assertTrue(observe.call_args.kwargs["final"])

            public = final_view["experiment_run_record"]
            repeated_public = repeat_view["experiment_run_record"]
            self.assertEqual(public["status"], "failed")
            self.assertEqual(public["ended_at"], ended_at)
            self.assertEqual(public["rows"][0]["ended_at"], ended_at)
            self.assertEqual(repeated_public["status"], "failed")
            self.assertEqual(repeated_public["ended_at"], ended_at)
            self.assertEqual(terminal["exit_code"], 7)
            self.assertEqual(terminal["failure_class"], "exit-7")
            record_path = resource_progress.public_record_path(artifact, run_id)
            self.assertEqual(list(record_path.parent.glob("*.json")), [record_path])
            state = resource_progress._read_observation_state(state_path)
            final_events = [event for event in state.get("outbox", [])
                            if event.get("kind") == "final"]
            self.assertEqual(len(final_events), 1)
            self.assertEqual(final_events[0]["key"], "final:run")
            duties = ObligationStore(jobs.parent).list(kinds={"run-notice"})
            self.assertEqual(len(duties), 1)
            delivery_id = duties[0]["intent"]["delivery_id"]
            notice = dispatch_pending_delivery.read(jobs.parent, "fixture-supervisor", delivery_id)
            self.assertIsNotNone(notice)
            self.assertEqual(notice["state"], "pending")

    def test_normalize_scan_and_read_cli_leave_observation_record_and_pending_unchanged(self):
        import resource_progress
        import dispatch_session_sweep as sweep
        import session_notice
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs.log"
            jobs.touch()
            artifact = root / "artifact"
            artifact.mkdir()
            output = root / "run"
            (output / "logs").mkdir(parents=True)
            (output / "logs" / "epochs.jsonl").write_text(
                '{"epoch":1,"split":"valid","losses":{"L_se":-8.0}}\n')
            registry_path = root / "resource-runs.json"
            identity = {"pid": 71, "starttime": "12", "command_hash": "b" * 64,
                        "process_group": 71}
            row = {**identity, "run_id": "read-only-run", "status": "running",
                   "started_at": 0, "output_dir": str(output),
                   "artifact_root": str(artifact), "jobs": str(jobs),
                   "route_id": "route-read-only", "node": "eval",
                   "notify": {"harness": "claude", "id": "fixture-supervisor"}}
            registry_path.write_text(json.dumps({"schema_version": 1,
                                                  "runs": {"read-only-run": row}}))
            state_path = root / "runtime" / "resource-observations" / "row.json"
            state_path.parent.mkdir(parents=True)
            resource_progress._write_observation_state(state_path, {
                "schema_version": 1, "run_id": "read-only-run", "row_key": "run",
                "outbox": [], "event_keys": [], "sources": [], "samples": [], "warnings": [],
            })
            record_path = resource_progress.write_public_run_record(artifact, "read-only-run", {
                "run_id": "read-only-run", "route_id": "route-read-only", "status": "running",
                "metrics": [], "rows": [],
            })
            with mock.patch("peer_obligations.ensure_normal_runner", return_value=False), \
                    mock.patch("peer_obligations.request_dispatch_reconciliation", return_value=False):
                existing = session_notice.notify("claude", "fixture-supervisor",
                    key="existing-notice", subject="existing", text="existing pending notice",
                    jobs=jobs)
            pending_root = root / "pending-delivery"
            duties_root = root / "peer-steward" / "obligations"

            def snapshot():
                return {
                    "state": state_path.read_bytes(),
                    "record": record_path.read_bytes(),
                    "pending": {str(path.relative_to(pending_root)): path.read_bytes()
                                for path in sorted(pending_root.rglob("*.json"))},
                    "duties": {path.name: path.read_bytes()
                               for path in sorted(duties_root.glob("*.json"))},
                    "notice": sweep.pending_delivery.read(root, "fixture-supervisor",
                                                          existing["delivery_id"]),
                }

            before = snapshot()
            with mock.patch.object(resource_progress, "_observation_state_path",
                                   return_value=state_path), \
                    mock.patch.object(registry, "proc_identity", return_value=identity), \
                    mock.patch.object(registry, "indexed_paths", return_value=([registry_path], [])), \
                    mock.patch("session_notice.notify",
                               side_effect=AssertionError("read triggered delivery")):
                normalized = registry.normalize_run("read-only-run", row, registry_path, now=7200)
                scanned, diagnostics = registry.scan(identity_reader=lambda _pid: identity, now=7200)
            self.assertIsNotNone(normalized["experiment_run_record"])
            self.assertEqual([item["run_id"] for item in scanned], ["read-only-run"])
            self.assertEqual(diagnostics, [])
            cli_env = {**os.environ, "AGENT_ARTIFACT_ROOT": str(artifact),
                       "AGENT_DISPATCH_JOBS": str(jobs), "AGENT_RUNTIME_DIR": str(root / "runtime"),
                       "HOME": str(root / "home"), "CODEX_HOME": str(root / "codex")}
            cli = subprocess.run([sys.executable, "-B",
                                  str(Path(__file__).with_name("resource-runner.py")),
                                  "--registry", str(registry_path), "observation", "--run-id",
                                  "read-only-run", "--json"], capture_output=True, text=True,
                                 env=cli_env, check=False)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertEqual(json.loads(cli.stdout)["route_id"], "route-read-only")
            self.assertEqual(snapshot(), before)


if __name__ == "__main__":
    unittest.main()
