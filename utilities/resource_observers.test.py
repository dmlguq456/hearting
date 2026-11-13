#!/usr/bin/env python3
"""Old-release adoption and auxiliary execution preserve the primary resource."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools" / "install"))
import fixture_env


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


OBS = load('resource-observers')
FIX = load('workflow_supervisor.test')
SUP = FIX.SUP
R = OBS.load_tool('resource-runner')


class ObserverTest(FIX.WorkflowFixture):
    _resume_fixture = FIX.TestSupervisorAdvance._resume_fixture

    @contextlib.contextmanager
    def subfixture(self):
        with self.nested_base():
            yield

    def fixture(self):
        route, path, jobs, registry, output = self._resume_fixture(ordinary=True)
        row = json.loads(registry.read_text())['runs']['fixture-run']
        row.update(**SUP.RR.proc_identity(os.getpid()), status='running', exit_code=None)
        row.pop('ended_at', None)
        Path(row['sentinel']).unlink(missing_ok=True)
        registry.write_text(json.dumps({'schema_version': 1, 'runs': {'fixture-run': row}}))
        return route, path, jobs, registry, output, row

    def start_side(self, registry, route, path, jobs, command, *, run_id='score', log=None):
        args = SimpleNamespace(run_id=run_id, route=str(path), node='full-run', cwd=str(self.base),
                               log=str(log or (self.base / (run_id + '.log'))), jobs=str(jobs) if jobs else None)
        spawn = subprocess.Popen
        def launch(*a, **kw):
            proc = spawn(*a, **kw)
            self.addCleanup(self.cleanup_process, proc)
            return proc
        with mock.patch.object(R.subprocess, 'Popen', side_effect=launch), \
             mock.patch.object(R, 'observation_runtime', return_value=OBS), \
             mock.patch.object(OBS, 'ensure_resource', return_value={'started': True}), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            R.start_observer(registry, args, route, path, command)
        return json.loads(out.getvalue())

    def cleanup_process(self, proc):
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=3)

    def reap(self, registry, row):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.waitpid(row['pid'], os.WNOHANG)
            except ChildProcessError:
                pass
            current = json.loads(registry.read_text())['runs'][row['run_id']]
            if R.read_sentinel(row['sentinel']) is not None and SUP.RR.classify_identity(current)[0] == 'exited':
                return R.settle(registry, row['run_id'], current)[0]
            time.sleep(.01)
        self.fail('observer did not exit')

    def test_success_failure_and_replay_preserve_stage_and_deliver_to_each_harness(self):
        import resource_progress as progress
        import session_notice
        for harness, code in [('claude', 0), ('codex', 7), ('opencode', 0)]:
            with self.subTest(harness=harness), self.subfixture():
                route, path, jobs, registry, output, primary = self.fixture()
                primary.update(session={'harness': harness, 'id': 'owner'},
                               notify={'harness': harness, 'id': 'supervisor'})
                registry.write_text(json.dumps({'schema_version': 1, 'runs': {'fixture-run': primary}}))
                ledger = SUP.ledger_for(route, jobs)
                armed_before = SUP.read_armed(ledger)
                journal_before = ledger.journal_path.read_bytes()
                side = self.start_side(registry, route, path, None, ['/bin/sh', '-c', f'exit {code}'])
                self.assertEqual(side['jobs'], str(jobs))
                self.assertEqual(side['observer_of'], 'fixture-run')
                self.assertEqual(side['node'], primary['node'])
                self.assertNotEqual(side['sentinel'], primary['sentinel'])
                ended = self.reap(registry, side)
                self.assertEqual(ended['exit_code'], code)
                replay = self.start_side(registry, route, path, jobs, side['command'])
                self.assertTrue(replay['replayed'])
                self.assertFalse(replay['payload_spawned'])
                data = json.loads(registry.read_text())
                self.assertEqual(data['runs']['fixture-run'], primary)
                self.assertFalse(Path(primary['sentinel']).exists())
                self.assertEqual(SUP.read_armed(ledger), armed_before)
                self.assertEqual(ledger.journal_path.read_bytes(), journal_before)
                # A live side run cannot block admission of the next primary; evidence paths stay protected.
                candidate = {'log': str(self.base / 'next.log')}
                R.route_authority.require_resource_predecessors([side], candidate)
                with self.assertRaises(ValueError):
                    R.route_authority.require_resource_predecessors([side], {'log': side['log']})
                notices = []
                def receive(h, s, **kw):
                    notices.append((h, s, kw))
                    return {'status': 'accepted'}
                observe = SUP.RR.observe_run_if_due
                calls = []
                def transient(*a, **kw):
                    calls.append(True)
                    if len(calls) == 1:
                        raise OSError('transient observation failure')
                    return observe(*a, **kw)
                with mock.patch.object(session_notice, 'notify', side_effect=receive), \
                     mock.patch.object(SUP.RR, 'observe_run_if_due', side_effect=transient):
                    OBS.watch_resource(registry, side['run_id'], sleep=lambda _: None)
                self.assertEqual(len(calls), 2)
                self.assertEqual({(h, s) for h, s, _ in notices}, {(harness, 'owner'), (harness, 'supervisor')})
                self.assertIn(f'exit={code}', ' '.join(n['text'] for _, _, n in notices))
                record = progress.read_public_run_record(route['artifact_root'], side['run_id'])
                self.assertEqual(record['status'], 'succeeded' if code == 0 else 'failed')

    def test_missing_primary_and_primary_evidence_alias_are_refused(self):
        route, path, jobs, registry, output, primary = self.fixture()
        with self.assertRaisesRegex(ValueError, 'body-conflict'):
            self.start_side(registry, route, path, jobs, ['/bin/true'], log=primary['log'])
        registry.write_text(json.dumps({'runs': {}}))
        with self.assertRaisesRegex(ValueError, 'requires-live-resource'):
            self.start_side(registry, route, path, jobs, ['/bin/true'])
        self.assertFalse((self.base / 'score.log').exists())


class AdoptionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def test_compute_adoption_does_not_inherit_installer_project_identity(self):
        import resource_progress as progress
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compute = OBS.load_tool('compute-hosts')
            meta = {'run_id': 'legacy', 'artifact_root': str(root / 'original'),
                    'provenance': {'session': {'harness': 'claude', 'id': 'original-session'}}}
            with mock.patch.dict(os.environ, {'AGENT_ARTIFACT_ROOT': str(root / 'installer'),
                       'AGENT_ARTIFACT_CAMPAIGN_ID': 'installer-campaign', 'AGENT_ARTIFACT_CYCLE_ID': 'installer-cycle'}), \
                 mock.patch.object(progress, 'observe_experiment', side_effect=lambda record, *a, **kw: record):
                record = compute._observe_compute_experiment(meta, root, 100)
            self.assertEqual(record['artifact_root'], str(root / 'original'))
            self.assertIsNone(record['campaign_id'])
            self.assertIsNone(record['cycle_id'])
            self.assertEqual(record['session']['id'], 'original-session')

    def test_reconcile_adopts_both_registries_and_preserves_source_bytes(self):
        import resource_run_registry as registry_api
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / 'compute' / 'old-running'
            run.mkdir(parents=True)
            meta = run / 'meta.json'
            meta.write_text(json.dumps({'run_id': run.name, 'provenance': {'session': {'harness': 'codex', 'id': 'old'}}}))
            registry = root / 'resource-runs.json'
            registry.write_text(json.dumps({'schema_version': 1, 'runs': {
                'live-resource': {'pid': os.getpid(), 'status': 'running'}}}))
            index = root / 'index.json'
            registry_api.register_registry(registry, index_path=index)
            before = meta.read_bytes(), registry.read_bytes()
            compute = OBS.load_tool('compute-hosts')
            with mock.patch.dict(os.environ, {'AGENT_RESOURCE_RUN_INDEX': str(index), 'AGENT_DISPATCH_DEPTH': '1'}), \
                 mock.patch.object(OBS, 'load_tool', return_value=compute), \
                 mock.patch.object(compute, 'load_config', return_value={'run_root': root / 'compute'}), \
                 mock.patch.object(compute, '_refuse_namespace_self'), \
                 mock.patch.object(compute, '_spawn_completion_watch', return_value=True) as attach, \
                 mock.patch.object(OBS, 'ensure_resource', return_value={'started': True}) as side:
                result = OBS.reconcile()
            self.assertEqual({row['kind'] for row in result['results']}, {'compute', 'resource'})
            self.assertEqual(attach.call_args.kwargs['environ']['AGENT_DISPATCH_DEPTH'], '0')
            side.assert_called_once_with(registry, 'live-resource')
            self.assertEqual(before, (meta.read_bytes(), registry.read_bytes()))

    def test_old_watcher_is_replaced_once_without_touching_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = []
            for release in ('old', 'new'):
                script = root / release / 'compute-hosts.py'
                script.parent.mkdir()
                script.write_text('import time\ntime.sleep(60)\n')
                scripts.append(script)
            source = root / 'run'
            source.mkdir()
            old = subprocess.Popen([sys.executable, str(scripts[0]), 'watch-run', str(source)])
            payload = subprocess.Popen(['/bin/sleep', '60'])
            children = [old, payload]
            def spawn(*a, **kw):
                proc = subprocess.Popen(*a, **kw)
                children.append(proc)
                return proc
            try:
                command = [sys.executable, str(scripts[1]), 'watch-run', str(source)]
                result = OBS.ensure_watch(command, source / '.watch.lock', spawn=spawn)
                self.assertTrue(result['started'])
                self.assertEqual(old.wait(timeout=3), -signal.SIGTERM)
                again = OBS.ensure_watch(command, source / '.watch.lock', spawn=spawn)
                self.assertTrue(again['reused'])
                self.assertEqual(result['pid'], again['pid'])
                self.assertIsNone(payload.poll())
            finally:
                for proc in children:
                    if proc.poll() is None:
                        proc.terminate()
                    proc.wait(timeout=3)

    def test_reconcile_stops_watching_a_quiet_run_and_reports_a_late_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compute = OBS.load_tool('compute-hosts')
            old = time.time() - compute.RUN_QUIET_SECONDS - 3600
            runs = {}
            for name in ('quiet', 'quiet-alive', 'active', 'late', 'finished'):
                run = root / 'compute' / name
                run.mkdir(parents=True)
                (run / 'meta.json').write_text(json.dumps(
                    {'run_id': name, 'provenance': {'session': {'harness': 'codex', 'id': 'old'}}}))
                runs[name] = run
            (runs['late'] / compute.QUIET_RUN_MARKER).write_text('{}\n')
            for name in ('late', 'finished'):
                (runs[name] / 'exit_code').write_text('0\n')
            for name in ('quiet', 'quiet-alive', 'late', 'finished'):
                for path in runs[name].iterdir():
                    os.utime(path, (old, old))
            with mock.patch.dict(os.environ, {'AGENT_RESOURCE_RUN_INDEX': str(root / 'index.json')}), \
                 mock.patch.object(OBS, 'load_tool', return_value=compute), \
                 mock.patch.object(compute, 'load_config', return_value={'run_root': root / 'compute'}), \
                 mock.patch.object(compute, '_refuse_namespace_self'), \
                 mock.patch.object(compute, '_spawn_completion_watch', return_value=True) as attach, \
                 mock.patch.object(OBS, 'retire_watch', return_value=0) as retire, \
                 mock.patch.object(compute, '_run_session_alive',
                                   side_effect=lambda run: False if run.name == 'quiet' else None):
                result = OBS.reconcile()
            # The active run, a quiet run whose session may be alive, and the
            # late exit get an observer; a quiet run whose session is gone and
            # an ordinary finished run do not.
            self.assertEqual(sorted(Path(call.args[0]).name for call in attach.call_args_list),
                             ['active', 'late', 'quiet-alive'])
            quiet = next(row for row in result['results'] if row.get('run_id') == 'quiet')
            self.assertEqual((quiet['watching'], quiet['state']), (False, 'unknown'))
            self.assertTrue((runs['quiet'] / compute.QUIET_RUN_MARKER).is_file())
            self.assertFalse((runs['quiet'] / 'exit_code').exists())
            retire.assert_called_once()
            self.assertEqual(retire.call_args.args[0][2:], ['watch-run', str(runs['quiet'])])

    def test_retire_watch_stops_only_an_older_release_observer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = []
            for release in ('old', 'new'):
                script = root / release / 'compute-hosts.py'
                script.parent.mkdir()
                script.write_text('import time\ntime.sleep(60)\n')
                scripts.append(script)
            source = root / 'run'
            source.mkdir()
            old = subprocess.Popen([sys.executable, str(scripts[0]), 'watch-run', str(source)])
            current = subprocess.Popen([sys.executable, str(scripts[1]), 'watch-run', str(source)])
            try:
                command = [sys.executable, str(scripts[1]), 'watch-run', str(source)]
                self.assertEqual(OBS.retire_watch(command, source / '.watch.lock'), 1)
                self.assertEqual(old.wait(timeout=3), -signal.SIGTERM)
                self.assertIsNone(current.poll())     # this release's observer leaves by itself
            finally:
                for proc in (old, current):
                    if proc.poll() is None:
                        proc.terminate()
                    proc.wait(timeout=3)

    def test_installed_current_subprocess_observes_rootless_exact_source(self):
        import resource_progress

        source_bundle = self.base / "bundle-source"
        (source_bundle / "utilities").mkdir(parents=True)
        shutil.copytree(HERE, source_bundle / "utilities", dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.test.py", "*.test.sh"))
        install_root = self.base / "fixture-home" / ".local" / "share" / "hearting"
        release = install_root / "releases" / "fixture-release"
        install_root.mkdir(parents=True)
        shutil.copytree(source_bundle, release)
        current = install_root / "current"
        current.symlink_to(release, target_is_directory=True)
        self.assertEqual(current.resolve(), release.resolve())

        registry = self.base / "registry" / "resource-runs.json"
        registry.parent.mkdir()
        artifact = self.base / "artifacts"
        artifact.mkdir()
        source = self.base / "writer" / "observation.json"
        source.parent.mkdir()
        source.write_text(json.dumps({"observed_at": time.time() - 30,
                                     "sample_units": 21}))
        proof = self.base / "writer-proof.json"
        target_id, writer_id = "fixture-target-x4", "fixture-writer-p2"
        proof.write_text(json.dumps({"target": target_id, "writer": writer_id,
            "command": ["fixture", "--target", target_id]}))
        script_proof = self.base / "writer-proof.py"
        script_proof.write_bytes(b"def emit_sample():\n    return {'sample_units': 21}\n")
        binding = {"target_run_id": target_id, "row_key": "run",
            "path": str(source), "writer_run_id": writer_id,
            "timestamp_pointer": "/observed_at", "scope": "shared-results",
            "counters": [{"name": "sample_units", "pointer": "/sample_units",
                          "comparison_eligible": False}],
            "proofs": [{"path": str(proof),
                "sha256": hashlib.sha256(proof.read_bytes()).hexdigest(),
                "assertions": [{"pointer": "/target", "equals": target_id},
                               {"pointer": "/writer", "equals": writer_id},
                               {"pointer": "/command/2", "equals": target_id}]},
                {"path": str(script_proof),
                 "sha256": hashlib.sha256(script_proof.read_bytes()).hexdigest(),
                 "assertions": []}]}
        sentinel = self.base / "absent-sentinel"
        row = {"status": "failed", "workflow_state": "FAILED_RETRYABLE",
            "failure_class": "resource-launch-incomplete", "resource_policy": "supervised-owner",
            "exit_code": None, "sentinel": str(sentinel), "started_at": time.time() - 30,
            "artifact_root": str(artifact), "observation": {"source_bindings": [binding]}}
        registry_bytes = json.dumps({"schema_version": 1,
            "runs": {target_id: row}}, sort_keys=True).encode() + b"\n"
        registry.write_bytes(registry_bytes)
        source_before, proof_before = source.read_bytes(), proof.read_bytes()
        script_before = script_proof.read_bytes()
        env = fixture_env.build_environment(self.base / "fixture-env", current)
        env.pop("PYTHONPATH", None)
        command = [sys.executable, str(current / "utilities" / "resource-observers.py"),
                   "watch", str(registry), target_id]
        completed = subprocess.run(command, cwd=self.base, env=env,
            capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(registry.read_bytes(), registry_bytes)
        self.assertEqual((source.read_bytes(), proof.read_bytes(), script_proof.read_bytes()),
                         (source_before, proof_before, script_before))
        record = resource_progress.read_public_run_record(artifact, target_id)
        self.assertEqual(record["progress"]["observed_counters"][0]["value"], 21)
        self.assertEqual(record["progress"]["observed_counters"][0]["writer_run_id"],
                         writer_id)
        self.assertIsNone(record["progress"]["reached"])
        self.assertIsNone(record["eta"])

    def test_installed_current_subprocess_continues_persisted_stale_source(self):
        import resource_progress

        source_bundle = self.base / "stale-bundle-source"
        (source_bundle / "utilities").mkdir(parents=True)
        shutil.copytree(HERE, source_bundle / "utilities", dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.test.py", "*.test.sh"))
        install_root = self.base / "fixture-home-stale" / ".local" / "share" / "hearting"
        release = install_root / "releases" / "fixture-stale-release"
        install_root.mkdir(parents=True)
        shutil.copytree(source_bundle, release)
        current = install_root / "current"
        current.symlink_to(release, target_is_directory=True)

        registry = self.base / "stale-registry" / "resource-runs.json"
        registry.parent.mkdir()
        dispatch_jobs = self.base / "stale-dispatch" / "jobs.log"
        dispatch_jobs.parent.mkdir()
        dispatch_jobs.touch()
        artifact = self.base / "stale-artifacts"
        artifact.mkdir()
        route = artifact / "route.json"
        route.write_text(json.dumps({"route_id": "stale-route",
                                     "artifact_root": str(artifact)}))
        run_id, writer_id = "installed-stale-target", "installed-independent-writer"
        source = self.base / "stale-writer" / "observation.json"
        source.parent.mkdir()
        proof = self.base / "stale-writer-proof.json"
        proof.write_text(json.dumps({"target": run_id, "writer": writer_id,
            "command": ["writer", "--target", run_id]}))
        binding = {"target_run_id": run_id, "row_key": "run",
            "path": str(source), "writer_run_id": writer_id,
            "timestamp_pointer": "/observed_at", "scope": "shared-results",
            "counters": [{"name": "sample_units", "pointer": "/sample_units",
                          "comparison_eligible": False}],
            "proofs": [{"path": str(proof),
                "sha256": hashlib.sha256(proof.read_bytes()).hexdigest(),
                "assertions": [{"pointer": "/target", "equals": run_id},
                               {"pointer": "/writer", "equals": writer_id},
                               {"pointer": "/command/2", "equals": run_id}]}]}
        raw_row = {"run_id": run_id, "status": "running", "pid": 777,
            "starttime": "9", "command_hash": "d" * 64,
            "pid_namespace": "pid:[foreign-fixture]", "route": str(route),
            "route_id": "stale-route", "jobs": str(dispatch_jobs),
            "execution": {"kind": "local-resource", "run_id": "installed-execution"}}
        registry_bytes = json.dumps({"schema_version": 1,
            "runs": {run_id: raw_row}}, sort_keys=True).encode() + b"\n"
        registry.write_bytes(registry_bytes)
        seed_now = time.time() - 120
        source.write_text(json.dumps({"observed_at": seed_now - 10, "sample_units": 11}))
        accepted = {**raw_row, "artifact_root": str(artifact),
                    "observation": {"source_bindings": [binding]}}
        seeded = resource_progress.observe_experiment(accepted, registry, seed_now,
            row_key="run", started_at=None, status="stale", notify_events=False)
        self.assertEqual(seeded["observation"]["source_observations"][0]["value"], 11)

        source.write_text(json.dumps({"observed_at": time.time(), "sample_units": 22}))
        source_before, proof_before = source.read_bytes(), proof.read_bytes()
        env = fixture_env.build_environment(self.base / "fixture-env-stale", current)
        env.pop("PYTHONPATH", None)
        driver = self.base / "run-installed-observer-once.py"
        driver.write_text(
            "import importlib.util, sys\n"
            "from pathlib import Path\n"
            "utilities = Path(sys.argv[1])\n"
            "sys.path.insert(0, str(utilities))\n"
            "spec = importlib.util.spec_from_file_location('installed_observer', "
            "utilities / 'resource-observers.py')\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "class StopAfterPoll(Exception): pass\n"
            "def stop(_seconds): raise StopAfterPoll()\n"
            "try: module.watch_resource(sys.argv[2], sys.argv[3], sleep=stop)\n"
            "except StopAfterPoll: pass\n")
        command = [sys.executable, "-B", str(driver), str(current / "utilities"),
                   str(registry), run_id]
        completed = subprocess.run(command, cwd=self.base, env=env,
            capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(registry.read_bytes(), registry_bytes)
        self.assertEqual((source.read_bytes(), proof.read_bytes()), (source_before, proof_before))

        state_path = resource_progress._observation_state_path(raw_row, registry, "run",
                                                                create=False)
        state = resource_progress._read_observation_state(state_path)
        record = resource_progress.read_public_run_record(artifact, run_id)
        self.assertEqual(state["source_bindings"], [binding])
        self.assertEqual(state["source_observations"][0]["value"], 22)
        self.assertEqual(record["status"], "stale")
        self.assertEqual(record["progress"]["observed_counters"][0]["value"], 22)
        self.assertIsNone(record["progress"]["reached"])
        self.assertIsNone(record["progress"]["total"])
        self.assertIsNone(record["progress"].get("eta_seconds"))
        self.assertIsNone(record["eta"])
        self.assertTrue(any("raw counter=sample_units:22" in event["text"]
                            for event in state["outbox"]))

    def test_hash_bound_request_discovery_and_invalid_digest(self):
        import resource_progress as progress
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output'
            output.mkdir()
            request = root / 'request.json'
            request.write_text(json.dumps({'output': str(output)}))
            digest = hashlib.sha256(request.read_bytes()).hexdigest()
            run = {'cwd': str(root), 'command': ['python', 'train.py', '--request', str(request),
                                               '--request-sha256', digest]}
            self.assertEqual(progress.discover_output_root(run), str(output))
            request.write_text(json.dumps({'output': str(root)}))
            self.assertIsNone(progress.discover_output_root(run))


if __name__ == '__main__':
    unittest.main()
