#!/usr/bin/env python3
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
            state_path = root / "runtime" / "state.json"
            state_path.parent.mkdir()
            identity = {"pid": 77, "starttime": "9", "command_hash": "a" * 64,
                        "process_group": 77}
            row = {**identity, "cwd": str(project), "status": "running",
                   "started_at": 0, "route": str(route), "route_id": "route-fixture",
                   "command": ["python", "train.py", "--config", "configs/run.json"],
                   "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest()}
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
                view = registry.normalize_run("run-view", row, root / "resource-runs.json",
                    identity_reader=lambda _pid: identity, now=7201)
            self.assertEqual(observed["experiment_run_record"]["route_id"], "route-fixture")
            self.assertEqual(view["experiment_run_record"]["output_dir"], str(output))
            self.assertEqual(view["experiment_run_record"]["metric_summary"]["latest"]["value"], -8.77)

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
