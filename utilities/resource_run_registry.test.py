#!/usr/bin/env python3
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import resource_run_registry as registry


class ResourceRegistryTest(unittest.TestCase):
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
        }
        row = registry.normalize_run(
            "run-a", raw, Path("/registry.json"), identity_reader=lambda _pid: None,
        )
        for key in ("artifact_root", "route", "route_file", "route_id", "route_hash",
                    "node", "route_node"):
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

    def test_legacy_boot_reset_requires_all_existing_launch_coordinates(self):
        row = {'resource_policy': 'supervised-owner', 'started_at': 50, 'starttime': '10000', 'owner_wait': {'bound': True},
               'pid_namespace': os.readlink('/proc/self/ns/pid')}
        with mock.patch.object(registry, '_boot_epoch', return_value=100), \
                mock.patch.object(Path, 'read_text', return_value='10.0 1.0'), \
                mock.patch.object(os, 'sysconf', return_value=100), \
                mock.patch.object(registry, 'legacy_resource_boot', return_value='old'), \
                mock.patch.object(registry, 'boot_identity', return_value={'boot_id': 'new', 'boot_host': 'local'}), \
                mock.patch.object(registry, 'local_boot_history', return_value={'old', 'new'}):
            self.assertIsNotNone(registry.reboot_evidence(row))
            for changes in ({'started_at': 101}, {'starttime': '500'}, {'started_at': None},
                            {'pid_namespace': 'foreign'}, {'resource_policy': None}, {'boot_id': 'bad'}):
                self.assertIsNone(registry.reboot_evidence({**row, **changes}))
            with mock.patch.object(registry, 'local_boot_history', return_value={'new'}):
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


if __name__ == "__main__":
    unittest.main()
