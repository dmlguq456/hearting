#!/usr/bin/env python3
"""Sequential supervised resources retain one stage and its output requirement."""
import contextlib
import copy
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('sequence_fixture', HERE / 'workflow_supervisor.test.py')
FIX = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = FIX
spec.loader.exec_module(FIX)
SUP, WS = FIX.SUP, FIX.WS
ROOT = FIX.ROOT
import dispatch_resource_wait as WAIT
import dispatch_contract as DC
import route_authority as RA


class ResourceSequenceTest(FIX.WorkflowFixture):
    EMPTY_LAUNCH_REQUEST = {"smoke_attestation": None, "smoke_attestation_sha256": None,
                            "config_manifest": None, "config_manifest_sha256": None}
    _resume_fixture = FIX.TestSupervisorAdvance._resume_fixture
    _settle_resource_owner = FIX.TestSupervisorAdvance._settle_resource_owner

    def fixture(self):
        route, path, jobs, registry, output = self._resume_fixture(ordinary=True)
        row = json.loads(registry.read_text())['runs']['fixture-run']
        row['parent_attempt_id'] = 'att-parent'
        registry.write_text(json.dumps({'schema_version': 1, 'runs': {row['run_id']: row}}))
        ledger = SUP.ledger_for(route, jobs)
        arm = ledger.root / 'armed/full-run.json'
        armed = json.loads(arm.read_text())
        armed['resource_binding'] = WAIT.resource_body_digest(row)
        armed['successor_log'] = str(ledger.root / 'resource/verification-start.log')
        arm.write_text(json.dumps(armed))
        (output / 'run.json').unlink()
        return route, path, jobs, registry, output, ledger

    def live_owner(self, route, jobs, registry, *, harness='codex'):
        import dispatch_owner_input as INPUT
        identity = SUP.RR.proc_identity(os.getpid())
        attempt = 'att-owner'
        lease = DC.supervisor_lease_path(jobs, attempt)
        nonce = 'd' * 64
        metadata = {
            'attempt_schema_version': '2', 'dispatch_depth': '1',
            'transport': 'headless', 'execution_surface': 'registered-headless',
            'registered_worker': '1', 'fallback_hop': 'same-harness-headless',
            'worker_type': 'owner', 'unit': '_kernel/owner', 'harness': harness,
            'runtime_sandbox': 'workspace-write', 'completion_delivery':
                'app-server-supervised' if harness == 'codex' else 'session-resume-supervised',
            'supervisor_lease': DC.SUPERVISOR_LEASE_KIND,
            'supervisor_lease_file': str(lease), 'supervisor_lease_nonce': nonce,
            'attempt_id': attempt, 'pid': str(identity['pid']), 'pid_start': identity['starttime'],
            'owner_route_id': route['route_id'], 'owner_route_hash': route['route_hash'],
            'route_id': route['route_id'], 'route_hash': route['route_hash'],
            'parent_sid': 'fixture-parent-session',
            'capability': route['capability'], 'capability_mode': route['capability_mode'],
            'intensity': route['effective_intensity'],
        }
        row = '\t'.join(['2026-10-20T00:00:00Z', 'open', str(self.base), str(self.base),
                         'fixture-owner', ','.join(f'{key}={value}' for key, value in metadata.items())])
        with Path(jobs).open('a', encoding='utf-8') as stream:
            stream.write(row + '\n')
        lease_context = DC.hold_supervisor_lease(jobs, attempt, lease)
        lease_context.__enter__()
        self.addCleanup(lease_context.__exit__, None, None, None)
        owner_input = INPUT.OwnerInput(jobs, attempt, 'same-native', 'codex-active-turn', lambda _: None)
        self.addCleanup(owner_input.close)
        resource_data = json.loads(registry.read_text())
        armed_path = SUP.ledger_for(route, jobs).root / 'armed/full-run.json'
        armed = json.loads(armed_path.read_text())
        resource_row = resource_data['runs'][armed['predecessor_id']]
        resource_row['parent_attempt_id'] = attempt
        resource_row['owner_wait'].update(
            parent_attempt_id=attempt, session_id='same-native', route_id=route['route_id'],
            route_hash=route['route_hash'], jobs=str(Path(jobs).resolve()),
            owner_pid=str(identity['pid']), owner_start=identity['starttime'],
        )
        registry.write_text(json.dumps(resource_data))
        armed['resource_binding'] = WAIT.resource_body_digest(resource_row)
        armed_path.write_text(json.dumps(armed))
        return attempt, owner_input

    def test_compute_adopted_row_passes_normal_owner_successor_admission_once(self):
        for harness in ('claude', 'codex', 'opencode'):
            with self.subTest(owner_harness=harness), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                (output / 'run.json').write_bytes(b'{"state":"compute-result"}\n')
                attempt, _owner_input = self.live_owner(route, jobs, registry, harness=harness)
                data = json.loads(registry.read_text())
                row = data['runs']['fixture-run']
                row.update(status='succeeded', workflow_state='STAGE_SUCCEEDED', exit_code=0)
                compute_dir = self.base / 'compute' / 'compute-fixture'
                compute_dir.mkdir(parents=True)
                (compute_dir / 'log').write_text('remote fixture complete\n')
                (compute_dir / 'exit_code').write_text('0\n')
                row.update(compute_run_id='compute-fixture', log=str(compute_dir / 'log'),
                           sentinel=str(compute_dir / 'exit_code'))
                row['execution'] = {
                    'kind': 'compute-hosts', 'run_id': 'compute-fixture',
                    'run_root': str(self.base / 'compute'), 'meta_sha256': 'm' * 64,
                    'exit_sha256': 'e' * 64, 'receipt_sha256': 'r' * 64,
                    'outputs_sha256': 'o' * 64,
                    'contract_sha256': 'c' * 64,
                    'provenance': {'route_id': route['route_id'], 'route_hash': route['route_hash']},
                }
                compute_identity = SUP.RR.compute_execution_identity(row)
                registry.write_text(json.dumps(data))
                armed = SUP.read_armed(ledger)['full-run']
                armed['resource_binding'] = WAIT.resource_body_digest(row)
                armed_path = ledger.root / 'armed/full-run.json'
                armed_path.write_text(json.dumps(armed))
                args = SimpleNamespace(
                    route_file=str(path), route_node='run-verify', attempt_id='att-verify',
                    jobs=str(jobs), agent_home=ROOT, worktree=str(self.base), dispatch_depth=2,
                    parent_slug='fixture-owner', parent_attempt_id=attempt,
                    parent_harness=harness, parent_transport='headless',
                    parent_sandbox='workspace-write', subsession_id=None,
                )
                marker = jobs.parent / 'completion' / route['route_id'] / 'full-run.json'
                observer_calls = []
                def observe_outside_writer_locks(_row):
                    observer_calls.append(True)
                    for lock_path in (ledger.lock_path, Path(str(registry) + '.lock')):
                        with lock_path.open('a+') as lock_file:
                            try:
                                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                self.fail(f'compute observation ran while writer lock held: {lock_path}')
                            finally:
                                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    return {'state': 'quiescent', 'reason': 'fixture-remote-empty'}
                with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
                     mock.patch('subprocess.check_output', return_value=str(self.base)), \
                     mock.patch.object(SUP.RR, 'compute_execution_observation',
                                       side_effect=observe_outside_writer_locks), \
                     mock.patch.object(
                         SUP, 'reconcile_legacy_supervised_resource_success',
                         wraps=SUP.reconcile_legacy_supervised_resource_success,
                     ) as reconcile:
                    self.assertIsNone(RA.completion_gate(
                        args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
                    ))
                    marker_before = marker.read_bytes()
                    journal_before = ledger.journal_path.read_bytes()
                    claims_before = ledger.claims()
                    claim_history_before = ledger.claim_history()
                    self.assertIsNone(RA.completion_gate(
                        args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
                    ))
                    with mock.patch('route_parent_close.ledger_intent', return_value=None), \
                         mock.patch('artifact_checkpoint_trigger.launch_for_route'):
                        polled = SUP.poll_once(route, ledger)
                self.assertEqual(len(observer_calls), 1)
                self.assertEqual(reconcile.call_count, 0)
                self.assertEqual(polled, [{'node': 'full-run', 'action': 'settled',
                                           'state': 'STAGE_SUCCEEDED'}])
                self.assertTrue(marker.is_file())
                self.assertEqual(marker.read_bytes(), marker_before)
                self.assertEqual(json.loads(marker.read_bytes())['resource_execution_identity'],
                                 compute_identity)
                self.assertEqual(ledger.journal_path.read_bytes(), journal_before)
                self.assertEqual(ledger.claims(), claims_before)
                self.assertEqual(ledger.claim_history(), claim_history_before)
                self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'STAGE_SUCCEEDED')
                self.assertEqual(len(ledger.claims()), 1)
                self.assertEqual(len(ledger.claim_history()), 1)

    def test_changed_captured_owner_settlement_refuses_without_reobserve_or_claim(self):
        with self.subfixture():
            route, path, jobs, registry, output, ledger = self.fixture()
            (output / 'run.json').write_bytes(b'{"state":"compute-result"}\n')
            attempt, _owner_input = self.live_owner(route, jobs, registry, harness='codex')
            data = json.loads(registry.read_text())
            row = data['runs']['fixture-run']
            row.update(status='succeeded', workflow_state='STAGE_SUCCEEDED', exit_code=0)
            compute_dir = self.base / 'compute' / 'compute-fixture'
            compute_dir.mkdir(parents=True)
            (compute_dir / 'log').write_text('remote fixture complete\n')
            (compute_dir / 'exit_code').write_text('0\n')
            row.update(compute_run_id='compute-fixture', log=str(compute_dir / 'log'),
                       sentinel=str(compute_dir / 'exit_code'))
            row['execution'] = {
                'kind': 'compute-hosts', 'run_id': 'compute-fixture',
                'run_root': str(self.base / 'compute'), 'meta_sha256': 'm' * 64,
                'exit_sha256': 'e' * 64, 'receipt_sha256': 'r' * 64,
                'outputs_sha256': 'o' * 64, 'contract_sha256': 'c' * 64,
                'provenance': {'route_id': route['route_id'], 'route_hash': route['route_hash']},
            }
            registry.write_text(json.dumps(data))
            armed_path = ledger.root / 'armed/full-run.json'
            armed = SUP.read_armed(ledger)['full-run']
            armed['resource_binding'] = WAIT.resource_body_digest(row)
            armed_path.write_text(json.dumps(armed))
            args = SimpleNamespace(
                route_file=str(path), route_node='run-verify', attempt_id='att-verify',
                jobs=str(jobs), agent_home=ROOT, worktree=str(self.base), dispatch_depth=2,
                parent_slug='fixture-owner', parent_attempt_id=attempt,
                parent_harness='codex', parent_transport='headless',
                parent_sandbox='workspace-write', subsession_id=None,
            )
            marker = jobs.parent / 'completion' / route['route_id'] / 'full-run.json'
            observer_calls = []

            def observe_outside_writer_locks(_row):
                observer_calls.append(True)
                for lock_path in (ledger.lock_path, Path(str(registry) + '.lock')):
                    with lock_path.open('a+') as lock_file:
                        try:
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            self.fail(f'compute observation ran while writer lock held: {lock_path}')
                        finally:
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                return {'state': 'quiescent', 'reason': 'fixture-remote-empty'}

            with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
                 mock.patch('subprocess.check_output', return_value=str(self.base)), \
                 mock.patch.object(SUP.RR, 'compute_execution_observation',
                                   side_effect=observe_outside_writer_locks):
                self.assertIsNone(RA.completion_gate(
                    args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
                ))
                marker_before = marker.read_bytes()
                journal_before = ledger.journal_path.read_bytes()
                claims_before = ledger.claims()
                owner_supervisor = WAIT.supervisor()
                real_snapshot = owner_supervisor._owner_settlement_snapshot
                mutated = False

                def mutate_after_settlement_capture(*snapshot_args):
                    nonlocal mutated
                    snapshot = real_snapshot(*snapshot_args)
                    if snapshot is not None and not mutated:
                        changed = json.loads(registry.read_text())
                        changed['runs']['fixture-run']['owner_wait']['session_id'] = 'changed-session'
                        registry.write_text(json.dumps(changed))
                        mutated = True
                    return snapshot

                with mock.patch.object(owner_supervisor, '_owner_settlement_snapshot',
                                       side_effect=mutate_after_settlement_capture):
                    refused = RA.completion_gate(
                        args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
                    )
            self.assertTrue(mutated)
            self.assertIsNotNone(refused)
            self.assertEqual(refused[0], 'resource-owner-stage-admission-blocked')
            self.assertEqual(observer_calls, [True])
            self.assertEqual(marker.read_bytes(), marker_before)
            self.assertEqual(ledger.journal_path.read_bytes(), journal_before)
            self.assertEqual(ledger.claims(), claims_before)

    def test_compute_run_start_then_normal_owner_run_verify_start(self):
        import artifact_producer
        import resource_run_registry as RR
        spec = importlib.util.spec_from_file_location('resource_runner_e2e', HERE / 'resource-runner.py')
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        route, path, jobs, registry, output, ledger = self.fixture()
        owner_attempt, _owner_input = self.live_owner(route, jobs, registry, harness='codex')
        (output / 'run.json').write_bytes(b'{"state":"compute-result"}\n')
        (output / 'logs').mkdir()
        (output / 'logs/nested').mkdir()
        (output / 'logs/train.log').write_text('train completed\n')
        (output / 'logs/nested/metrics.json').write_text('{"loss":0.1}\n')
        declared_outputs = list(WS.route_node(route, 'full-run').get('outputs') or [])
        self.assertEqual(declared_outputs, ['run.json', 'logs/**', 'experiment-artifact'])
        attestation = self.base / 'smoke-attestation.json'
        attestation.write_text('{"fixture":true}\n')
        attestation_hash = __import__('hashlib').sha256(attestation.read_bytes()).hexdigest()
        old = json.loads(registry.read_text())['runs']['fixture-run']
        command = ['compute-hosts', 'run', 'gpu-a', '--cwd', '/lab', '--env', 'env-a', '--',
                   'python', 'train.py']
        launch_request = {'smoke_attestation': str(attestation),
                          'smoke_attestation_sha256': attestation_hash,
                          'config_manifest': None, 'config_manifest_sha256': None}
        old.update(status='failed', workflow_state='FAILED_RETRYABLE', failure_class='payload-exit',
                   exit_code=3, command=command, launch_request=launch_request,
                   config_ref='fixture-config', config_sha256='c' * 64,
                   source_commit='fixture-commit', source_dirty=False,
                   source_git_state='clean', config_layout='fixture-layout')
        Path(old['sentinel']).write_text('3\n')
        registry.write_text(json.dumps({'schema_version': 1, 'runs': {'fixture-run': old}}))
        armed = SUP.read_armed(ledger)['full-run']
        armed['resource_binding'] = WAIT.resource_body_digest(old)
        (ledger.root / 'armed/full-run.json').write_text(json.dumps(armed))

        compute_root = self.base / 'compute-runs'
        compute_dir = compute_root / 'compute-fixture'
        compute_dir.mkdir(parents=True)
        (compute_dir / 'log').write_text('remote compute completed\n')
        (compute_dir / 'exit_code').write_text('0\n')
        sealed = {key: old.get(key) for key in ('command', 'cwd', 'config_ref', 'config_sha256',
                 'source_commit', 'source_dirty', 'source_git_state', 'config_layout', 'launch_request')}
        parent = {'route_id': route['route_id'], 'route_file': str(path.resolve()),
                  'route_hash': route['route_hash'], 'node': 'full-run', 'run_id': 'fixture-run',
                  'attempt_id': owner_attempt, 'sealed': sealed, 'artifact_base': str(output),
                  'declared_outputs': declared_outputs,
                  'inputs': [{'name': 'smoke_attestation', 'path': str(attestation),
                              'sha256': attestation_hash, 'verified': True}]}
        provenance = {'run_id': 'fixture-run', 'registry': str(registry.resolve()),
                      'route_id': route['route_id'], 'route_file': str(path.resolve()),
                      'route_hash': route['route_hash'], 'node': 'full-run',
                      'parent_attempt_id': owner_attempt,
                      'contract': parent}
        execution_contract = {'command': ['python', 'train.py'], 'cwd': '/lab', 'env': 'env-a',
            'share': False, 'resource_parent': parent,
            'route': {'route_id': route['route_id'], 'route_file': str(path.resolve()),
                      'route_hash': route['route_hash']}}
        compute_meta = {'run_id': 'compute-fixture', 'host': 'gpu-a',
            'provenance': {'resource': provenance}, 'execution_contract': execution_contract,
            'contract_sha256': __import__('hashlib').sha256(json.dumps(
                execution_contract, ensure_ascii=False, sort_keys=True,
                separators=(',', ':')).encode('utf-8')).hexdigest()}
        (compute_dir / 'meta.json').write_text(json.dumps(compute_meta), encoding='utf-8')
        (compute_dir / 'launch-receipt.json').write_text(json.dumps({
            'schema_version': 1, 'run_id': 'compute-fixture', 'host': 'fixture-host',
            'identity': {'pid': 4, 'starttime': '4', 'process_group': 4,
                         'pid_namespace': 'pid:[77]', 'boot_id': 'boot-fixture'},
            'state': 'finished', 'exit_code': 0, 'started_at': 1.0, 'ended_at': 2.0}),
            encoding='utf-8')
        compute_spec_real = importlib.util.spec_from_file_location(
            'compute_hosts_sequence_e2e', HERE / 'compute-hosts.py')
        compute = importlib.util.module_from_spec(compute_spec_real)
        compute_spec_real.loader.exec_module(compute)
        compute.remote = lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, '{"state":"empty","reason":"fixture-empty-group"}\n', '')
        compute._record_compute_outputs(compute_dir, compute_meta)
        compute_config_file = self.base / 'compute-hosts.yaml'
        compute_config_file.write_text(
            'schema_version: 1\n'
            f'run_root: {compute_root}\n'
            'hosts:\n  gpu-a:\n    ssh_host: fixture.invalid\n', encoding='utf-8')
        candidate = compute.execution_record(compute.load_config(compute_config_file), 'compute-fixture')
        self.assertEqual(candidate['state'], 'quiescent', candidate)
        execution = candidate['execution']
        spec_loader = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda _module: None))
        real_spec = importlib.util.spec_from_file_location
        real_module = importlib.util.module_from_spec
        def load_spec(name, location, *args, **kwargs):
            if name == 'resource_runner_compute_hosts':
                return spec_loader
            return real_spec(name, location, *args, **kwargs)
        def load_module(specification):
            if specification is spec_loader:
                return compute
            return real_module(specification)
        owner_wait = old['owner_wait']
        owner_wait.update(parent_attempt_id=owner_attempt, session_id='same-native',
                          route_id=route['route_id'], route_hash=route['route_hash'], jobs=str(jobs))
        args = SimpleNamespace(jobs=str(jobs), parent_attempt_id=owner_attempt)
        arm_attempts = 0
        def arm_command(argv, **_kwargs):
            nonlocal arm_attempts
            if len(argv) > 1 and argv[1] == str(HERE / 'workflow-supervisor.py'):
                arm_attempts += 1
                if arm_attempts == 1:
                    raise subprocess.TimeoutExpired(argv, 30)
                self.assertEqual(SUP.main(argv[2:]), 0)
                return subprocess.CompletedProcess(argv, 0)
            raise AssertionError(f'unexpected child process from adoption: {argv!r}')
        with mock.patch.object(WAIT, 'supervisor', return_value=SUP), \
             mock.patch.object(WAIT, 'start_binding', return_value=owner_wait), \
             mock.patch.object(RR, 'classify_identity', return_value=('exited', None, 'process-absent')), \
             mock.patch.object(RR, 'resource_process_group_quiescent', return_value=True), \
             mock.patch('importlib.util.spec_from_file_location', side_effect=load_spec), \
             mock.patch('importlib.util.module_from_spec', side_effect=load_module), \
             mock.patch.object(runner, 'verify_launch_input'), \
             mock.patch.object(runner.subprocess, 'run', side_effect=arm_command), \
             mock.patch.object(runner.subprocess, 'Popen') as spawned, \
             mock.patch.object(artifact_producer, 'prepare_route_artifact_env', return_value={
                 'AGENT_ARTIFACT_OUTPUT_DIR': str(output)}), \
             mock.patch.dict(os.environ, {'AGENT_DISPATCH_JOBS': str(jobs),
                 'AGENT_DISPATCH_COMPLETION_MODE': 'supervised',
                 'COMPUTE_HOSTS_CONFIG': str(compute_config_file)}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as before_arm:
                runner.adopt_compute_run(registry, args, route, path, 'compute-fixture')
            self.assertEqual(before_arm.exception.code, 65)
            self.assertEqual(set(json.loads(registry.read_text())['runs']), {'fixture-run', 'fixture-run__a1'})

            output_receipt_path = compute_dir / 'outputs.json'
            original_output_receipt = output_receipt_path.read_bytes()
            changed_receipt = json.loads(original_output_receipt)
            changed_receipt['outputs'].append({'path': 'logs/injected.log', 'sha256': '0' * 64,
                                               'size': 1, 'state': 'present'})
            output_receipt_path.write_text(json.dumps(changed_receipt, ensure_ascii=False,
                                                       sort_keys=True) + '\n', encoding='utf-8')
            marker = jobs.parent / 'completion' / route['route_id'] / 'full-run.json'
            def protected_snapshot():
                paths = [registry, Path(old['sentinel']), jobs, ledger.journal_path, marker]
                files = {str(item): item.read_bytes() if item.is_file() else None for item in paths}
                claims = {item.name: item.read_bytes() for item in ledger.claims_dir.glob('*.claim')}
                return files, claims
            before_refusal = protected_snapshot()
            with self.assertRaises(SystemExit) as changed_receipt_refusal:
                runner.adopt_compute_run(registry, args, route, path, 'compute-fixture')
            self.assertEqual(changed_receipt_refusal.exception.code, 65)
            refusal = json.loads(changed_receipt_refusal.exception.resource_message)
            self.assertEqual(refusal['reason'], 'resource-compute-output-mismatch')
            self.assertTrue(refusal['differences'][0].get('expected') is not None)
            self.assertTrue(refusal['differences'][0].get('actual') is not None)
            self.assertTrue(refusal['next_action'])
            self.assertFalse(refusal['retryable'])
            self.assertEqual(protected_snapshot(), before_refusal)
            self.assertEqual(set(json.loads(registry.read_text())['runs']), {'fixture-run', 'fixture-run__a1'})
            output_receipt_path.write_bytes(original_output_receipt)

            replay_before_arm = runner.adopt_compute_run(registry, args, route, path, 'compute-fixture')
            self.assertTrue(replay_before_arm['replayed'])
            self.assertEqual(spawned.call_count, 0)
            replay_after_arm = runner.adopt_compute_run(registry, args, route, path, 'compute-fixture')
            self.assertTrue(replay_after_arm['replayed'])
            self.assertEqual(arm_attempts, 2)
            self.assertEqual(spawned.call_count, 0)
            owner_args = SimpleNamespace(route_file=str(path), route_node='run-verify',
                attempt_id='att-verify', jobs=str(jobs), agent_home=ROOT, worktree=str(self.base),
                dispatch_depth=2, parent_slug='fixture-owner', parent_attempt_id=owner_attempt,
                parent_harness='codex', parent_transport='headless', parent_sandbox='workspace-write',
                subsession_id=None)
            armed_outputs = SUP.read_armed(ledger)['full-run']
            import resource_run_evidence as RUN_EVIDENCE
            self.assertEqual(RUN_EVIDENCE.outputs(armed_outputs), ['run.json'])
            self.assertEqual(SUP.artifact_evidence(armed_outputs), {
                'checked': True, 'missing': [], 'reason': 'artifacts-present'})
            with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
                 mock.patch('subprocess.check_output', return_value=str(self.base)), \
                 mock.patch.object(DC, 'canonical_repository_identity', return_value='fixture-repository'), \
                 mock.patch.object(RR, 'compute_execution_observation', return_value={
                     'state': 'quiescent', 'reason': 'fixture-remote-empty'}):
                self.assertIsNone(RA.completion_gate(
                    owner_args, 'start', ROOT, jobs, gate=DC.completion_marker_gate))
        rows = json.loads(registry.read_text())['runs']
        adopted = rows['fixture-run__a1']
        self.assertEqual(adopted['execution']['run_id'], 'compute-fixture')
        marker = jobs.parent / 'completion' / route['route_id'] / 'full-run.json'
        self.assertEqual(json.loads(marker.read_bytes())['resource_execution_identity'],
                         RR.compute_execution_identity(adopted))
        self.assertEqual(len(ledger.claims()), 1)
        self.assertEqual(len(ledger.claim_history()), 1)

    def next_body(self, registry, name, output, *, final=False):
        old = json.loads(registry.read_text())['runs']['fixture-run']
        keep = ('cwd', 'route', 'node', 'jobs', 'parent_attempt_id', 'owner_wait', 'config_ref',
                'config_sha256', 'source_commit', 'source_dirty', 'source_git_state', 'config_layout')
        log = self.base / (name + '.log')
        code = (f'from pathlib import Path; Path({str(output / "run.json")!r}).write_text({json.dumps({"phase":"final"})!r})'
                if final else 'pass')
        return {**{key: old[key] for key in keep if key in old}, 'run_id': name,
                'command': [sys.executable, '-c', code], 'log': str(log), 'sentinel': str(log) + '.exit',
                'status': 'launching', 'workflow_state': 'READY', 'resource_policy': 'supervised-owner',
                'launch_request': dict(self.EMPTY_LAUNCH_REQUEST)}

    def arm(self, path, registry, *, node='full-run', run_id='fixture-run', extra=()):
        argv = ['arm', '--route', str(path), '--node', node, '--predecessor-kind', 'resource',
                '--predecessor-id', run_id, '--resource-registry', str(registry),
                '--successor-external', '--successor-log',
                str(SUP.ledger_for(SUP.load_route(path), self.base / 'jobs.log').root / 'resource/verification-start.log'), *extra]
        # The initial inherited fixture uses its own original arm helper.
        if run_id == 'fixture-run':
            return FIX.WorkflowFixture.arm(self, path, registry, node=node, extra=extra)
        with contextlib.redirect_stdout(io.StringIO()):
            return SUP.main(argv)

    def launch(self, route, path, jobs, registry, output, body, *, controller=None, close_at=None, exit_code=0):
        import artifact_producer
        runner = SUP.runner()
        args = SimpleNamespace(jobs=str(jobs), run_id=body['run_id'], node=body['node'])
        payloads = []
        real_popen = subprocess.Popen
        def popen(*a, **kw):
            proc = real_popen(*a, **kw)
            payloads.append(proc)
            if close_at == 'fence':
                close()
            return proc
        def close():
            ledger = SUP.ledger_for(route, jobs)
            with ledger.lock():
                ledger.record(body['node'], 'RUNNING', evidence={'parent_close': {'preserve_resource': True}})
        def arm(argv, **kwargs):
            if close_at == 'reservation':
                close()
            result = SUP.main(argv[2:])
            if close_at == 'arm':
                close()
            return subprocess.CompletedProcess(argv, result)
        watch = mock.Mock()
        watch.poll.return_value = None
        with mock.patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": ""}), \
                mock.patch.object(WAIT, 'supervisor', return_value=SUP), \
                mock.patch.object(artifact_producer, 'prepare_route_artifact_env',
                    return_value={'AGENT_ARTIFACT_OUTPUT_DIR': str(output)}), \
                mock.patch.object(runner, 'register_registry'), \
                mock.patch.object(runner, 'start_watch', return_value=(watch, SUP.RR.proc_identity(os.getpid()))), \
                mock.patch.object(runner.subprocess, 'run', side_effect=arm), \
                mock.patch.object(runner.subprocess, 'Popen', side_effect=popen), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            runner.start_verified(registry, args, route, path, dict(body), controller=controller)
        for proc in payloads:
            self.assertEqual(proc.wait(timeout=5), exit_code)
        return payloads, json.loads(out.getvalue().splitlines()[-1])

    def test_verified_sequential_launch_release_accepts_multidigit_fd(self):
        with self.subfixture():
            route, path, jobs, registry, output, ledger = self.fixture()
            body = self.next_body(registry, 'highfd-next', output)
            marker = self.base / 'highfd-payload'
            body['command'] = [sys.executable, '-c',
                'from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])',
                str(marker), 'argv-preserved']
            held = []
            try:
                for _ in range(6):
                    held.extend(os.pipe())
                self.assertGreaterEqual(max(held), 10)
                payloads, receipt = self.launch(route, path, jobs, registry, output, body)
            finally:
                for fd in held:
                    os.close(fd)
            self.assertTrue(receipt['payload_spawned'])
            self.assertEqual(len(payloads), 1)
            self.assertEqual(marker.read_text(), 'argv-preserved')
            self.assertEqual(SUP.runner().read_sentinel(receipt['sentinel']), 0)

    def test_guard_exit_same_route_resume_preserves_checkpoint_until_owner_successor_start(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        registry.write_text(json.dumps({'schema_version': 1, 'runs': {}}))
        (ledger.root / 'armed/full-run.json').unlink()
        checkpoint = self.base / 'checkpoint'
        def body(name, code):
            log = self.base / (name + '.log')
            return {'run_id': name, 'route': str(path), 'node': 'full-run', 'jobs': str(jobs),
                    'cwd': str(self.base), 'log': str(log), 'sentinel': str(log) + '.exit',
                    'command': [sys.executable, '-c', code],
                    'status': 'launching', 'workflow_state': 'READY',
                    'parent_attempt_id': 'att-parent', 'resource_policy': 'supervised-owner',
                    'owner_wait': {'parent_attempt_id': 'att-parent', 'session_id': 'same-native'}}
        first = body('paused', f'from pathlib import Path; Path({str(checkpoint)!r}).write_text("saved"); raise SystemExit(3)')
        self.launch(route, path, jobs, registry, output, first, exit_code=3)
        failed = SUP.poll_once(route, ledger)[0]
        self.assertEqual(failed['action'], 'halt-failed')
        self.assertEqual(failed['evidence']['exit_code'], 3)
        self.assertEqual(ledger.claims(), {})
        old = json.loads(registry.read_text())['runs']['paused']
        old_bytes = Path(old['sentinel']).read_bytes(), Path(old['log']).read_bytes()
        old_arm = SUP.read_armed(ledger)['full-run']
        resumed = body('resumed',
            f'from pathlib import Path; assert Path({str(checkpoint)!r}).read_text() == "saved"; '
            f'Path({str(output / "run.json")!r}).write_text({json.dumps({"phase":"resumed"})!r})')
        procs, receipt = self.launch(route, path, jobs, registry, output, resumed)
        self.assertEqual(len(procs), 1)
        self.assertTrue(receipt['payload_spawned'])
        self.assertEqual(ledger.state()['workflow_state'], 'RUNNING')
        self.assertEqual(SUP.read_armed(ledger)['full-run']['predecessor_id'], 'resumed')
        self.assertTrue(any((entry.get('evidence') or {}).get('previous_resource') == old_arm
                            for entry in ledger.journal()))
        result = SUP.poll_once(route, ledger)[0]
        self.assertEqual(result['action'], 'wait-next-resource')
        self.assertTrue(result['evidence']['succeeded'])
        self.assertEqual(len(ledger.claims()), 0)
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')
        self.assertFalse((jobs.parent / 'completion' / route['route_id'] / 'full-run.json').exists())
        self.assertEqual(json.loads(registry.read_text())['runs']['paused'], old)
        self.assertEqual((Path(old['sentinel']).read_bytes(), Path(old['log']).read_bytes()), old_bytes)
        self.assertEqual(json.loads((output / 'run.json').read_text()), {"phase":"resumed"})

    def test_rebooted_running_resource_without_sentinel_is_settled_before_real_successor(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        data = json.loads(registry.read_text())
        old = data['runs']['fixture-run']
        Path(old['sentinel']).unlink()
        old.update(status='running', workflow_state='RUNNING', exit_code=None,
                   boot_id='83f954bc-4963-4dfa-9f2f-c8f3597900a6', boot_host='local')
        data['runs']['fixture-run'] = old
        registry.write_text(json.dumps(data))
        checkpoint = self.base / 'checkpoint'
        checkpoint.write_text('epoch-26')
        Path(old['log']).write_text('epoch-26 saved\n')
        log_before = Path(old['log']).read_bytes()
        body = self.next_body(registry, 'fixture-run__a1', output)
        body['command'] = [sys.executable, '-c',
            f'from pathlib import Path; assert Path({str(checkpoint)!r}).read_text() == "epoch-26"']
        current = SUP.RR.boot_identity()
        # New payload capture must retain the real boot; only the old row belongs to the prior one.
        old['boot_host'] = current['boot_host']
        registry.write_text(json.dumps(data))
        procs, receipt = self.launch(route, path, jobs, registry, output, body)
        self.assertEqual(len(procs), 1)
        self.assertTrue(receipt['payload_spawned'])
        settled = json.loads(registry.read_text())['runs']['fixture-run']
        self.assertEqual((settled['status'], settled['failure_class']), ('failed', 'host-reboot'))
        self.assertIsNone(settled['exit_code'])
        self.assertFalse(Path(old['sentinel']).exists())
        self.assertEqual(Path(old['log']).read_bytes(), log_before)
        self.assertEqual(checkpoint.read_text(), 'epoch-26')
        again, repeated = self.launch(route, path, jobs, registry, output, body)
        self.assertEqual(again, [])
        self.assertFalse(repeated['payload_spawned'])

    def unstarted_fixture(self, **changes):
        route, path, jobs, registry, output, ledger = self.fixture()
        data = json.loads(registry.read_text())
        row = data['runs']['fixture-run']
        Path(row['sentinel']).unlink()
        for key in ('pid', 'starttime', 'command_hash', 'process_group', 'pid_namespace',
                    'launch_argv', 'supervision', 'exit_code', 'ended_at'):
            row.pop(key, None)
        row.update(status='failed', workflow_state='FAILED_RETRYABLE',
                   failure_class='resource-launch-incomplete', **changes)
        registry.write_text(json.dumps(data))
        return route, path, jobs, registry, output, ledger

    def test_legacy_armed_admission_failure_settles_and_retries_same_node_once(self):
        route, path, jobs, registry, output, ledger = self.unstarted_fixture()
        result = SUP.poll_once(route, ledger)[0]
        self.assertEqual(result['action'], 'halt-failed')
        self.assertTrue(result['evidence']['never_started'])
        self.assertIsNone(result['evidence']['exit_code'])
        self.assertEqual(ledger.state()['workflow_state'], 'FAILED_RETRYABLE')
        self.assertEqual(ledger.claims(), {})
        old = json.loads(registry.read_text())['runs']['fixture-run']
        self.assertTrue(old['ended_at'])
        retry = self.next_body(registry, 'fixture-run__a1', output)
        payloads, receipt = self.launch(route, path, jobs, registry, output, retry)
        self.assertEqual(len(payloads), 1)
        self.assertTrue(receipt['payload_spawned'])
        self.assertEqual(ledger.state()['workflow_state'], 'RUNNING')
        self.assertEqual(SUP.read_armed(ledger)['full-run']['predecessor_id'], retry['run_id'])
        self.assertEqual(json.loads(registry.read_text())['runs']['fixture-run'], old)
        payloads, receipt = self.launch(route, path, jobs, registry, output, retry)
        self.assertEqual(payloads, [])
        self.assertFalse(receipt['payload_spawned'])
        self.assertEqual(ledger.claims(), {})

    def test_unproven_claim_and_post_start_crash_do_not_admit_retry(self):
        route, path, jobs, registry, output, ledger = self.unstarted_fixture()
        original = json.loads(registry.read_text())['runs']['fixture-run']
        for changes in ({'launch_state': 'claimed', 'launch_controller': {'pid': 123}},
                        {'launch_state': 'started'}, {'pid': 999999999, 'starttime': '1', 'command_hash': 'x'},
                        {'failure_class': 'no-exit-sentinel'}, {'exit_code': 1},
                        {'cancel_requested': True}, {'parent_close_requested': True},
                        {'launch_state': 'not-started', **SUP.RR.proc_identity(os.getpid())}):
            with self.subTest(changes=changes):
                registry.write_text(json.dumps({'schema_version': 1, 'runs': {
                    'fixture-run': {**original, **changes}}}))
                old = json.loads(registry.read_text())['runs']['fixture-run']
                self.assertFalse(SUP.RR.resource_never_started(old))
                self.assertFalse(WAIT.resource_execution_finished(old))
                retry = self.next_body(registry, 'fixture-run__a1', output)
                with self.assertRaisesRegex(ValueError, 'resource-route-body-conflict'):
                    self.launch(route, path, jobs, registry, output, retry)
                self.assertEqual(json.loads(registry.read_text())['runs']['fixture-run'], old)

    def test_runner_failure_after_arm_settles_without_payload_and_opens_retry(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        body = self.next_body(registry, 'fixture-run__a1', output)
        forbidden = self.base / 'payload-must-not-start'
        body['command'] = [sys.executable, '-c',
            f'from pathlib import Path; Path({str(forbidden)!r}).write_text("wrong")']
        # Fail the watch identity check after real arm and wrapper spawn,
        # before the private fence permits the payload to execute.
        with mock.patch.object(SUP.runner().RESOURCE_RESUME, 'supervisor_alive', return_value=False):
            self.launch(route, path, jobs, registry, output, body, exit_code=125)
        row = json.loads(registry.read_text())['runs'][body['run_id']]
        self.assertEqual(row['launch_state'], 'not-started')
        self.assertFalse(Path(row['sentinel']).exists())
        self.assertFalse(forbidden.exists())
        self.assertEqual(ledger.state()['workflow_state'], 'FAILED_RETRYABLE')
        self.assertEqual(ledger.claims(), {})
        retry = self.next_body(registry, 'fixture-run__a2', output)
        payloads, receipt = self.launch(route, path, jobs, registry, output, retry)
        self.assertEqual(len(payloads), 1)
        self.assertTrue(receipt['payload_spawned'])

    def test_native_owner_reads_exact_never_started_failure_without_pid(self):
        import owner_route_binding as OWNER
        route, path, jobs, registry, output, ledger = self.unstarted_fixture(launch_state='not-started')
        data = json.loads(registry.read_text())
        row = data['runs']['fixture-run']
        ident = SUP.RR.proc_identity(os.getpid())
        row['owner_wait'].update(route_id=route['route_id'], route_hash=route['route_hash'],
            jobs=str(jobs), owner_pid=ident['pid'], owner_start=ident['starttime'])
        registry.write_text(json.dumps(data))
        arm_path = ledger.root / 'armed/full-run.json'
        armed = json.loads(arm_path.read_text())
        armed.update(resource_binding=WAIT.resource_body_digest(row), successor_external=True,
                     successor_command=None)
        arm_path.write_text(json.dumps(armed))
        args = SimpleNamespace(parent_attempt_id='att-parent', route_id=route['route_id'],
            route_hash=route['route_hash'], route_file=str(path), jobs=str(jobs))
        binding = SimpleNamespace(route_file=str(path), route_id=route['route_id'], route_hash=route['route_hash'])
        parent = SimpleNamespace(status='open', raw='time\topen\trepo\tworktree\tslug\tmeta',
            metadata={'pid':str(ident['pid']), 'pid_start':ident['starttime']})
        with mock.patch.object(WAIT, 'supervisor', return_value=SUP), \
             mock.patch.object(OWNER, 'resolve_owner_route_lifecycle', return_value=(binding, None)), \
             mock.patch.object(OWNER, '_owner_row_proof'), \
             mock.patch.object(WAIT.JOIN, 'exact_attempt_row', return_value=parent):
            found = WAIT.context(args, SimpleNamespace(thread_id='same-native'))
            self.assertEqual(found[3][0][1]['run_id'], 'fixture-run')
            self.assertFalse(found[3][0][1].get('pid'))
            row['failure_class'] = 'no-exit-sentinel'
            registry.write_text(json.dumps(data))
            with self.assertRaisesRegex(WAIT.JOIN.JoinContractError, 'resource-owner-binding-invalid'):
                WAIT.context(args, SimpleNamespace(thread_id='same-native'))

    def test_gpu_admission_failure_precedes_arm_and_can_retry(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        body = self.next_body(registry, 'fixture-run__a1', output)
        with mock.patch.object(SUP.runner().gpu_leases, 'resource_admission',
                               side_effect=SUP.runner().gpu_leases.GPUUnavailable('unknown GPU 99')), \
             mock.patch.object(SUP, 'main') as arm:
            self.launch(route, path, jobs, registry, output, body)
        arm.assert_not_called()
        row = json.loads(registry.read_text())['runs'][body['run_id']]
        self.assertTrue(SUP.RR.resource_never_started(row))
        retry = self.next_body(registry, 'fixture-run__a2', output)
        payloads, receipt = self.launch(route, path, jobs, registry, output, retry)
        self.assertEqual(len(payloads), 1)
        self.assertTrue(receipt['payload_spawned'])

    def receipt_fixture(self, *, queued_controller=False, failed=False):
        route, path, jobs, registry, output, ledger = self.fixture()
        if queued_controller:
            data = json.loads(registry.read_text())
            row = data['runs']['fixture-run']
            row['owner_wait']['launch_scope'] = 'codex-owner-controller'
            registry.write_text(json.dumps(data))
            arm_path = ledger.root / 'armed/full-run.json'
            armed = json.loads(arm_path.read_text())
            armed['resource_binding'] = WAIT.resource_body_digest(row)
            arm_path.write_text(json.dumps(armed))
        if failed:
            data = json.loads(registry.read_text())
            row = data['runs']['fixture-run']
            Path(row['sentinel']).write_text('3')
            row.update(status='failed', exit_code=3)
            registry.write_text(json.dumps(data))
        SUP.poll_once(route, ledger)
        args = SimpleNamespace(parent_attempt_id='att-parent', route_id=route['route_id'],
            route_hash=route['route_hash'], route_file=str(path), jobs=str(jobs))
        control = SimpleNamespace(thread_id='same-native', pending=lambda: False)
        state = self.base / 'state.json'
        WAIT.JOIN.write_supervisor_state(state, 'att-parent', set(), phase='running-turn')
        def context(*_):
            armed = SUP.read_armed(ledger)['full-run']
            row = json.loads(Path(armed['resource_registry']).read_text())['runs'][armed['predecessor_id']]
            return SUP, route, ledger, [(armed, row)]
        with mock.patch.object(WAIT, 'context', side_effect=context), mock.patch.object(WAIT, 'supervisor', return_value=SUP):
            prompt = WAIT.wait(args, state, control, set(), lambda _: None, sleep=lambda _: self.fail('extra wait'))
        return route, path, jobs, registry, output, ledger, args, control, state, context, prompt

    def test_failed_predecessor_can_resume_before_poll_and_with_new_owner_or_registry(self):
        for observed, new_owner, new_registry in ((False, False, False), (True, True, False), (True, False, True)):
            with self.subTest(observed=observed, new_owner=new_owner, new_registry=new_registry), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                data = json.loads(registry.read_text())
                old = data['runs']['fixture-run']
                Path(old['sentinel']).write_text('3')
                old.update(status='failed', exit_code=3)
                registry.write_text(json.dumps(data))
                if observed:
                    self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'halt-failed')
                body = self.next_body(registry, 'next', output, final=True)
                if new_owner:
                    body.update(parent_attempt_id='att-resumed',
                                owner_wait={'parent_attempt_id': 'att-resumed', 'session_id': 'resumed-native'})
                target = registry
                if new_registry:
                    target = self.base / 'resumed-registry.json'
                    target.write_text(json.dumps({'runs': {}}))
                self.launch(route, path, jobs, target, output, body)
                self.assertEqual(SUP.read_armed(ledger)['full-run']['predecessor_id'], 'next')
                self.assertEqual(ledger.state()['workflow_state'], 'RUNNING')
                self.assertEqual(json.loads(registry.read_text())['runs']['fixture-run'], old)
                self.assertEqual(Path(old['sentinel']).read_text(), '3')

    def test_settled_cancellation_rearms_normal_start_without_rewriting_history(self):
        for compute in (False, True):
            with self.subTest(compute=compute), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                data = json.loads(registry.read_text())
                old = data['runs']['fixture-run']
                Path(old['sentinel']).unlink()
                old.update(status='failed', workflow_state='CANCELLED', exit_code=None,
                           failure_class='cancelled', cancel_requested=True, ended_at=time.time(),
                           pid_namespace=os.readlink('/proc/self/ns/pid'))
                if compute:
                    compute_root = self.base / 'compute'
                    old.update(compute_run_id='remote-cancelled',
                               log=str(compute_root / 'remote-cancelled/log'),
                               sentinel=str(compute_root / 'remote-cancelled/exit_code'))
                    old['execution'] = {
                        'kind': 'compute-hosts', 'run_id': 'remote-cancelled',
                        'run_root': str(compute_root), 'meta_sha256': 'm' * 64,
                        'exit_sha256': 'e' * 64, 'receipt_sha256': 'r' * 64,
                        'outputs_sha256': 'o' * 64, 'contract_sha256': 'c' * 64,
                        'provenance': {'route_id': route['route_id'], 'route_hash': route['route_hash']},
                    }
                registry.write_text(json.dumps(data))
                armed = SUP.read_armed(ledger)['full-run']
                armed['resource_binding'] = WAIT.resource_body_digest(old)
                (ledger.root / 'armed/full-run.json').write_text(json.dumps(armed))
                body = self.next_body(registry, 'next', output, final=True)
                body['owner_wait'] = {'parent_attempt_id': 'att-parent', 'session_id': 'same-native'}
                def observe_outside_locks(_row):
                    for lock_path in (ledger.lock_path, Path(str(registry) + '.lock')):
                        with lock_path.open('a+') as stream:
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                    return {'state': 'quiescent'}
                with mock.patch.object(SUP.RR, 'compute_execution_observation',
                                       side_effect=observe_outside_locks):
                    payloads, receipt = self.launch(route, path, jobs, registry, output, body)
                self.assertTrue(receipt['payload_spawned'], receipt)
                self.assertEqual(len(payloads), 1)
                self.assertEqual(SUP.read_armed(ledger)['full-run']['predecessor_id'], 'next')
                self.assertEqual(json.loads(registry.read_text())['runs']['fixture-run'], old)
                self.assertFalse(Path(old['sentinel']).exists())

    def test_verified_resume_failed_predecessor_rearms_and_claims_verification_once(self):
        route, path, jobs, registry, output = self._resume_fixture()
        ledger = SUP.ledger_for(route, jobs)
        armed_path = ledger.root / 'armed/resume-run.json'
        armed = json.loads(armed_path.read_text())
        armed['successor_command'] = [sys.executable, str(HERE / 'capability-route.py'),
                                     'start', '--route', str(path), '--jobs', str(jobs)]
        armed['successor_log'] = str(ledger.root / 'resource/verification-start.log')
        armed_path.write_text(json.dumps(armed))
        data = json.loads(registry.read_text())
        old = data['runs']['fixture-run']
        Path(old['sentinel']).write_text('3')
        old.update(status='failed', exit_code=3)
        registry.write_text(json.dumps(data))
        self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'halt-failed')
        self.launch(route, path, jobs, registry, output, self.next_body(registry, 'resumed', output, final=True))
        with mock.patch.object(SUP, '_start_successor', return_value={'started': True}) as start:
            self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'advanced')
            self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'settled')
            self.assertEqual(start.call_count, 1)
        claim = next(iter(ledger.claims().values()))
        self.assertEqual(claim['successor'], 'one-shot')
        self.assertTrue(WS.route_node(route, 'one-shot')['verification_only'])
        self.assertEqual(json.loads(registry.read_text())['runs']['fixture-run'], old)

    def test_failed_exit_in_another_pid_namespace_waits_for_group_observation_before_retry(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        data = json.loads(registry.read_text())
        old = data['runs']['fixture-run']
        self.assertIsNotNone(old.get('process_group'))
        Path(old['sentinel']).write_text('1')
        old.update(status='failed', exit_code=1, pid_namespace='pid:[foreign]')
        registry.write_text(json.dumps(data))
        before = copy.deepcopy(old), Path(old['sentinel']).read_bytes(), registry.read_bytes()
        self.assertEqual(SUP.RR.classify_identity(old),
                         ('stale', None, 'process-namespace-mismatch'))
        self.assertFalse(SUP.RR.resource_process_group_quiescent(old))
        retry = self.next_body(registry, 'namespace-retry', output)
        with self.assertRaisesRegex(ValueError, 'resource-route-body-conflict'):
            self.launch(route, path, jobs, registry, output, retry)
        self.assertEqual(registry.read_bytes(), before[2])
        self.assertEqual(SUP.read_armed(ledger)['full-run']['predecessor_id'], 'fixture-run')

        # The runner's ordinary retry path admits once its group helper has a
        # positive observation that the foreign recorded group has ended.
        with mock.patch.object(SUP.RR, 'resource_process_group_quiescent', return_value=True) as observe:
            procs, receipt = self.launch(route, path, jobs, registry, output, retry)
        self.assertGreaterEqual(observe.call_count, 1)
        self.assertTrue(all(call.args[0] == old for call in observe.call_args_list))
        self.assertEqual(len(procs), 1)
        self.assertTrue(receipt['payload_spawned'])
        self.assertEqual(SUP.read_armed(ledger)['full-run']['predecessor_id'], 'namespace-retry')
        self.assertEqual(json.loads(registry.read_text())['runs']['fixture-run'], before[0])
        self.assertEqual(Path(old['sentinel']).read_bytes(), before[1])

    def test_cross_role_and_aliased_evidence_paths_refuse_without_changing_bytes(self):
        for collision in ('log-sentinel', 'sentinel-log', 'symlink', 'hardlink', 'partial', 'progress'):
            with self.subTest(collision=collision), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                SUP.poll_once(route, ledger)
                body = self.next_body(registry, 'next', output)
                old = json.loads(registry.read_text())['runs']['fixture-run']
                if collision == 'log-sentinel':
                    body['log'] = old['sentinel']
                elif collision == 'sentinel-log':
                    body['sentinel'] = old['log']
                elif collision == 'partial':
                    body['log'] = old['sentinel'] + '.partial'
                elif collision == 'progress':
                    body['progress_file'] = old['sentinel']
                else:
                    alias = self.base / 'alias'
                    if collision == 'symlink':
                        alias.symlink_to(old['sentinel'])
                    else:
                        os.link(old['sentinel'], alias)
                    body['log'] = str(alias)
                before = registry.read_bytes(), Path(old['sentinel']).read_bytes(), SUP.read_armed(ledger)
                with self.assertRaisesRegex(ValueError, 'resource-route-body-conflict'):
                    self.launch(route, path, jobs, registry, output, body)
                self.assertEqual((registry.read_bytes(), Path(old['sentinel']).read_bytes(), SUP.read_armed(ledger)), before)

    def test_third_registry_cannot_reuse_first_resource_evidence(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        middle_registry = self.base / 'middle-registry.json'
        middle_registry.write_text(json.dumps({'runs': {}}))
        self.launch(route, path, jobs, middle_registry, output, self.next_body(registry, 'middle', output))
        SUP.poll_once(route, ledger)
        body = self.next_body(registry, 'last', output)
        old = json.loads(registry.read_text())['runs']['fixture-run']
        body['log'] = old['sentinel']
        last_registry = self.base / 'last-registry.json'
        last_registry.write_text(json.dumps({'runs': {}}))
        before = last_registry.read_bytes(), Path(old['sentinel']).read_bytes()
        with self.assertRaisesRegex(ValueError, 'resource-route-body-conflict'):
            self.launch(route, path, jobs, last_registry, output, body)
        self.assertEqual((last_registry.read_bytes(), Path(old['sentinel']).read_bytes()), before)
        last_registry.write_text(json.dumps({'runs': {body['run_id']: body}}))
        prior = SUP.read_armed(ledger)
        with self.assertRaisesRegex(ValueError, 'resource-watch-binding-conflict'):
            self.arm(path, last_registry, run_id=body['run_id'], extra=('--jobs', str(jobs), '--artifact-base', str(output)))
        self.assertEqual(SUP.read_armed(ledger), prior)

    def test_unacknowledged_receipt_survives_next_registration_and_queued_intent(self):
        for other_registry, queued in ((False, False), (True, False), (True, True)):
            with self.subTest(other_registry=other_registry, queued=queued), self.subfixture():
                route, path, jobs, registry, output, ledger, args, control, state, context, prompt = self.receipt_fixture(queued_controller=queued)
                body = self.next_body(registry, 'next', output)
                target = registry
                if other_registry:
                    target = self.base / 'next-registry.json'
                    target.write_text(json.dumps({'runs': {}}))
                if queued:
                    body.update(launch_state='queued', status='launching',
                                launch_request=dict(self.EMPTY_LAUNCH_REQUEST))
                    self.assertTrue(WAIT.controller_intent(body))
                    target.write_text(json.dumps({'runs': {'next': body}}))
                    self.arm(path, target, run_id='next', extra=('--jobs', str(jobs), '--artifact-base', str(output)))
                else:
                    self.launch(route, path, jobs, target, output, body)
                with mock.patch.object(WAIT, 'context', side_effect=context), mock.patch.object(WAIT, 'supervisor', return_value=SUP), \
                        mock.patch.object(WAIT, 'admit_controller_launch') as admit:
                    self.assertEqual(WAIT.pending_prompt(state, 'att-parent', args, control), prompt)
                    self.assertEqual(WAIT.wait(args, state, control, set(), lambda _: None), prompt)
                    admit.assert_not_called()
                saved = WAIT.JOIN.read_supervisor_phase_state(state, 'att-parent').resource
                self.assertEqual(saved['delivered'], [])
                self.assertTrue(WAIT.acknowledge(state, 'att-parent', saved['outbox']['receipt_id']))
                self.assertFalse(WAIT.acknowledge(state, 'att-parent', saved['outbox']['receipt_id']))
                self.assertIsNone(WAIT.pending_prompt(state, 'att-parent', args, control))

    def test_unacknowledged_failure_receipt_survives_retry_and_receiving_turn_interruption(self):
        for other_registry, queued in ((False, False), (True, False), (True, True)):
            with self.subTest(other_registry=other_registry, queued=queued), self.subfixture():
                route, path, jobs, registry, output, ledger, args, control, state, context, prompt = self.receipt_fixture(queued_controller=queued, failed=True)
                saved = WAIT.JOIN.read_supervisor_phase_state(state, 'att-parent').resource
                self.assertEqual(saved['outbox']['receipt']['exit_code'], 3)
                self.assertEqual(saved['outbox']['receipt']['reason'], 'FAILED_RETRYABLE')
                body = self.next_body(registry, 'retry', output)
                target = registry
                if other_registry:
                    target = self.base / 'retry-registry.json'
                    target.write_text(json.dumps({'runs': {}}))
                if queued:
                    body.update(launch_state='queued', status='launching',
                                launch_request=dict(self.EMPTY_LAUNCH_REQUEST))
                    target.write_text(json.dumps({'runs': {'retry': body}}))
                    self.arm(path, target, run_id='retry', extra=('--jobs', str(jobs), '--artifact-base', str(output)))
                else:
                    self.launch(route, path, jobs, target, output, body)
                with mock.patch.object(WAIT, 'context', side_effect=context), mock.patch.object(WAIT, 'supervisor', return_value=SUP), \
                        mock.patch.object(WAIT, 'admit_controller_launch') as admit:
                    self.assertEqual(WAIT.pending_prompt(state, 'att-parent', args, control), prompt)
                    self.assertEqual(WAIT.wait(args, state, control, set(), lambda _: None), prompt)
                    admit.assert_not_called()
                self.assertEqual(WAIT.JOIN.read_supervisor_phase_state(state, 'att-parent').resource, saved)
                self.assertTrue(WAIT.acknowledge(state, 'att-parent', saved['outbox']['receipt_id']))
                self.assertFalse(WAIT.acknowledge(state, 'att-parent', saved['outbox']['receipt_id']))
                self.assertIsNone(WAIT.pending_prompt(state, 'att-parent', args, control))
                if not queued:
                    with mock.patch.object(WAIT, 'context', side_effect=context), mock.patch.object(WAIT, 'supervisor', return_value=SUP):
                        next_prompt = WAIT.wait(args, state, control, set(), lambda _: None,
                                                sleep=lambda _: self.fail('completed retry should not wait'))
                    self.assertIn('"run_id":"retry"', next_prompt)
                    current = WAIT.JOIN.read_supervisor_phase_state(state, 'att-parent').resource
                    self.assertEqual(current['delivered'], [saved['outbox']['key']])
                    self.assertTrue(WAIT.acknowledge(state, 'att-parent', current['outbox']['receipt_id']))

    def test_historical_receipt_rejects_mutated_identity_sentinel_and_session(self):
        for failed, mutation in ((failed, mutation) for failed in (False, True)
                                 for mutation in ('identity', 'sentinel', 'session')):
            with self.subTest(failed=failed, mutation=mutation), self.subfixture():
                route, path, jobs, registry, output, ledger, args, control, state, context, prompt = self.receipt_fixture(failed=failed)
                self.launch(route, path, jobs, registry, output, self.next_body(registry, 'next', output))
                data = json.loads(registry.read_text())
                old = data['runs']['fixture-run']
                if mutation == 'identity':
                    old['starttime'] = 'changed'
                    registry.write_text(json.dumps(data))
                elif mutation == 'sentinel':
                    Path(old['sentinel']).write_text('7')
                else:
                    control.thread_id = 'foreign-native'
                with mock.patch.object(WAIT, 'context', side_effect=context), mock.patch.object(WAIT, 'supervisor', return_value=SUP):
                    with self.assertRaisesRegex((ValueError, WAIT.JOIN.JoinContractError), 'binding'):
                        WAIT.pending_prompt(state, 'att-parent', args, control)
                self.assertIsNotNone(WAIT.JOIN.read_supervisor_phase_state(state, 'att-parent').resource['outbox'])

    def test_close_between_reservation_arm_and_release_never_runs_payload(self):
        for controller_mode in (False, True):
            for close_at in ('reservation', 'arm', 'fence'):
                with self.subTest(controller=controller_mode, close_at=close_at), self.subfixture():
                    route, path, jobs, registry, output, ledger = self.fixture()
                    SUP.poll_once(route, ledger)
                    body = self.next_body(registry, 'next', output)
                    effect = self.base / 'payload-effect'
                    body['command'] = [sys.executable, '-c', 'from pathlib import Path; Path(' + repr(str(effect)) + ').touch()']
                    controller = None
                    if controller_mode:
                        body['launch_state'] = 'queued'
                        body['launch_request'] = dict(self.EMPTY_LAUNCH_REQUEST)
                        data = json.loads(registry.read_text())
                        data['runs']['next'] = body
                        registry.write_text(json.dumps(data))
                        controller = SimpleNamespace(expected=copy.deepcopy(body), identity={**SUP.RR.proc_identity(os.getpid()),
                            'pid_namespace': os.readlink('/proc/self/ns/pid')}, command=body['command'],
                            sandbox='fixture', guard=contextlib.nullcontext)
                    self.launch(route, path, jobs, registry, output, body, controller=controller,
                                close_at=close_at, exit_code=125)
                    self.assertFalse(effect.exists())
                    self.assertEqual(ledger.claims(), {})
                    self.assertEqual(json.loads(registry.read_text())['runs']['next']['status'], 'failed')

    def test_next_registration_preserves_existing_active_workflow_progress(self):
        for target_state in ('STAGE_SUCCEEDED', 'NEXT_REGISTERED', 'NEXT_RUNNING'):
            with self.subTest(state=target_state), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                SUP.poll_once(route, ledger)
                for step in ('STAGE_SUCCEEDED', 'NEXT_REGISTERED', 'NEXT_RUNNING'):
                    ledger.set_workflow_state(step)
                    if step == target_state:
                        break
                self.launch(route, path, jobs, registry, output, self.next_body(registry, 'next', output))
                self.assertEqual(ledger.state()['workflow_state'], target_state)
                self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')

    def test_65th_receipt_acknowledges_once_without_lifetime_count_limit(self):
        route, path, jobs, registry, output, ledger, args, control, state, context, prompt = self.receipt_fixture()
        saved = WAIT.JOIN.read_supervisor_phase_state(state, 'att-parent')
        resource = copy.deepcopy(saved.resource)
        resource['delivered'] = [f'{i:064x}' for i in range(64)]
        WAIT._write(state, 'att-parent', set(), resource, 'deliverable')
        receipt_id = resource['outbox']['receipt_id']
        self.assertTrue(WAIT.acknowledge(state, 'att-parent', receipt_id))
        self.assertFalse(WAIT.acknowledge(state, 'att-parent', receipt_id))
        resource = WAIT.JOIN.read_supervisor_phase_state(state, 'att-parent').resource
        self.assertEqual(len(resource['delivered']), 65)
        self.assertIsNone(resource['outbox'])
        with mock.patch.object(WAIT, 'context', side_effect=context):
            self.assertIsNone(WAIT.wait(args, state, control, set(), lambda _: None, sleep=lambda _: self.fail('already delivered')))
        for invalid in ([resource['delivered'][0]] * 2, ['invalid-digest']):
            self.assertFalse(WAIT.JOIN.valid_resource_state({**resource, 'delivered': invalid}))

    def test_intermediate_success_waits_without_marker_claim_or_completion(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        result = SUP.poll_once(route, ledger)[0]
        self.assertEqual(result['action'], 'wait-next-resource')
        self.assertTrue(result['evidence']['succeeded'])
        self.assertEqual(ledger.state()['workflow_state'], 'RUNNING')
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')
        self.assertEqual(ledger.claims(), {})
        self.assertFalse((jobs.parent / 'completion' / route['route_id'] / 'full-run.json').exists())
        journal = ledger.journal_path.read_bytes()
        self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'wait-next-resource')
        self.assertEqual(ledger.journal_path.read_bytes(), journal)
        self.assertEqual(self._settle_resource_owner(route, path, jobs)[0].result, 'completed')
        self.assertTrue(ledger.journal_path.read_bytes().startswith(journal))

    def test_prepared_owner_manifest_stays_provisional_until_existing_successor_start(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        producer = output / 'run.json'
        prepared = b'{"state":"prepared","runs":[{"status":"prepared"}]}\n'
        producer.write_bytes(prepared)
        owner_attempt, owner_input = self.live_owner(route, jobs, registry)
        attempt = 'att-verify'
        args = SimpleNamespace(
            route_file=str(path), route_node='run-verify', attempt_id=attempt,
            jobs=str(jobs), agent_home=ROOT, worktree=str(self.base), dispatch_depth=2,
            parent_slug='fixture-owner', parent_attempt_id=owner_attempt,
            parent_harness='codex', parent_transport='headless',
            parent_sandbox='workspace-write', subsession_id=None,
        )
        marker_path = jobs.parent / 'completion' / route['route_id'] / 'full-run.json'
        before = ledger.journal_path.read_bytes()
        with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
             mock.patch('subprocess.check_output', return_value=str(self.base)):
            self.assertIsNone(RA.completion_gate(
                args, 'register', ROOT, jobs, gate=DC.completion_marker_gate,
            ))
            self.assertFalse(marker_path.exists())
            self.assertEqual(ledger.journal_path.read_bytes(), before)
            refused = RA.completion_gate(
                args, 'dry-run', ROOT, jobs, gate=DC.completion_marker_gate,
            )
            self.assertIsNotNone(refused)
            self.assertFalse(marker_path.exists())
            self.assertEqual(ledger.journal_path.read_bytes(), before)
            import dispatch_owner_input as INPUT
            queued = INPUT.submit(jobs, owner_attempt, 'apply the pending correction first', 'pending')
            self.assertEqual(queued['requests'][0]['state'], 'queued')
            blocked = RA.completion_gate(
                args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
            )
            self.assertIsNotNone(blocked)
            self.assertEqual(blocked[0], 'resource-owner-stage-admission-blocked')
            self.assertFalse(marker_path.exists())
            owner_state = INPUT._path(jobs, owner_attempt)
            correction_state = json.loads(owner_state.read_text())
            correction_state['requests'][0]['state'] = 'delivered'
            owner_state.write_text(json.dumps(correction_state))
            self.assertIsNone(RA.completion_gate(
                args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
            ))
            marker_bytes = marker_path.read_bytes()
            self.assertIsNone(RA.completion_gate(
                args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
            ))
        self.assertEqual(producer.read_bytes(), prepared)
        self.assertEqual(json.loads(marker_bytes)['resource_execution_identity'],
                         ledger.state()['nodes']['full-run']['evidence']['identity'])
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'STAGE_SUCCEEDED')
        self.assertEqual(len(ledger.claims()), 1)
        self.assertEqual(marker_path.read_bytes(), marker_bytes)

    def test_old_poll_false_success_and_unstarted_claim_recover_on_same_route(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        producer = output / 'run.json'
        producer.write_bytes(b'{"state":"prepared","runs":[{"status":"prepared"}]}\n')
        owner_attempt, owner_input = self.live_owner(route, jobs, registry)
        polled = SUP.poll_once(route, ledger)[0]
        self.assertEqual(polled['action'], 'wait-next-resource')
        armed = SUP.read_armed(ledger)['full-run']
        evidence = dict(polled['evidence'])
        evidence.pop('awaiting_next_resource', None)
        evidence.pop('runtime_only_output', None)
        marker_path = jobs.parent / 'completion' / route['route_id'] / 'full-run.json'
        marker = SUP.route_module().write_completion_marker(
            route, WS.route_node(route, 'full-run'), 'full-run', producer, jobs=jobs,
        )
        ledger.record('full-run', 'STAGE_SUCCEEDED', evidence=evidence, actor='poll')
        old_claims = SUP._claim_successors(route, ledger, armed, 'full-run',
                                           armed['successors'], evidence)
        self.assertTrue(old_claims[0]['created'])
        old_marker_bytes = marker_path.read_bytes()
        old_claim_key = old_claims[0]['claim']
        old_claim_bytes = ledger.claim_path(old_claim_key).read_bytes()

        failed_next = self.next_body(registry, 'validation__a2', output)
        prior_row = json.loads(registry.read_text())['runs']['fixture-run']
        failed_next.update(
            parent_attempt_id=owner_attempt, owner_wait=dict(prior_row['owner_wait']),
            started_at=float(prior_row['ended_at']) + 1.0, status='failed',
            workflow_state='FAILED_RETRYABLE', failure_class='resource-launch-incomplete',
            launch_state='not-started', exit_code=None, ended_at=time.time(),
        )
        data = json.loads(registry.read_text())
        data['runs'][failed_next['run_id']] = failed_next
        registry.write_text(json.dumps(data))

        with ledger.lock():
            self.assertTrue(SUP.reconcile_legacy_supervised_resource_success(route, ledger, armed))
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')
        self.assertEqual(ledger.claims(), {})
        self.assertEqual(ledger.claim_history()[old_claim_key]['successor'], 'run-verify')
        self.assertEqual(ledger.claim_path(old_claim_key).read_bytes(), old_claim_bytes)
        self.assertEqual(marker_path.read_bytes(), old_marker_bytes)
        self.assertFalse(DC.completion_marker_is_current(
            route, WS.route_node(route, 'full-run'), marker_path, marker, jobs=jobs,
        ))

        actual = self.next_body(registry, 'full-run__a3', output)
        processes, receipt = self.launch(route, path, jobs, registry, output, actual)
        self.assertEqual(len(processes), 1)
        self.assertTrue(receipt['payload_spawned'])
        self.assertEqual(json.loads(registry.read_text())['runs']['fixture-run'], prior_row)
        self.assertEqual(producer.read_bytes(), b'{"state":"prepared","runs":[{"status":"prepared"}]}\n')
        self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'wait-next-resource')
        args = SimpleNamespace(
            route_file=str(path), route_node='run-verify', attempt_id='att-verify',
            jobs=str(jobs), agent_home=ROOT, worktree=str(self.base), dispatch_depth=2,
            parent_slug='fixture-owner', parent_attempt_id=owner_attempt,
            parent_harness='codex', parent_transport='headless',
            parent_sandbox='workspace-write', subsession_id=None,
        )
        with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
             mock.patch('subprocess.check_output', return_value=str(self.base)):
            self.assertIsNone(RA.completion_gate(
                args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
            ))
        current_marker = json.loads(marker_path.read_text())
        self.assertGreater(current_marker['sequence'], marker['sequence'])
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'STAGE_SUCCEEDED')
        self.assertEqual(len(ledger.claims()), 1)
        self.assertEqual(ledger.claim_path(old_claim_key).read_bytes(), old_claim_bytes)
        self.assertEqual(len(ledger.claim_history()), 2)
        self.assertTrue(DC.completion_marker_is_current(
            route, WS.route_node(route, 'full-run'), marker_path, current_marker, jobs=jobs,
        ))

    def test_owner_successor_start_retries_marker_journal_and_claim_crash_boundaries(self):
        for crash_after in ('marker', 'journal', 'claim'):
            with self.subTest(crash_after=crash_after):
                case = ResourceSequenceTest()
                case.setUp()
                try:
                    route, path, jobs, registry, output, ledger = case.fixture()
                    (output / 'run.json').write_bytes(b'{"state":"done"}\n')
                    case.live_owner(route, jobs, registry)
                    args = SimpleNamespace(
                        route_file=str(path), route_node='run-verify', attempt_id='att-verify',
                        jobs=str(jobs), agent_home=ROOT, worktree=str(case.base), dispatch_depth=2,
                        parent_slug='fixture-owner', parent_attempt_id='att-owner',
                        parent_harness='codex', parent_transport='headless',
                        parent_sandbox='workspace-write', subsession_id=None,
                    )
                    marker_path = jobs.parent / 'completion' / route['route_id'] / 'full-run.json'
                    record = WS.WorkflowLedger.record
                    claim = WS.WorkflowLedger.claim
                    def interrupted_record(instance, node, state, **kwargs):
                        if crash_after == 'marker' and node == 'full-run' and state == 'STAGE_SUCCEEDED':
                            raise OSError('crash after marker before journal')
                        result = record(instance, node, state, **kwargs)
                        if crash_after == 'journal' and node == 'full-run' and state == 'STAGE_SUCCEEDED':
                            raise OSError('crash after stage journal')
                        return result
                    def interrupted_claim(instance, key, payload):
                        result = claim(instance, key, payload)
                        if crash_after == 'claim' and payload.get('predecessor') == 'full-run':
                            raise OSError('crash after exclusive successor claim')
                        return result
                    with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
                         mock.patch('subprocess.check_output', return_value=str(case.base)), \
                         mock.patch.object(WS.WorkflowLedger, 'record', interrupted_record), \
                         mock.patch.object(WS.WorkflowLedger, 'claim', interrupted_claim):
                        refused = RA.completion_gate(
                            args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
                        )
                        self.assertIsNotNone(refused)
                        self.assertEqual(refused[0], 'resource-owner-start-authority-unavailable')
                    self.assertTrue(marker_path.is_file())
                    marker_bytes = marker_path.read_bytes()
                    claim_files = {item.name: item.read_bytes() for item in ledger.claims_dir.glob('*.claim')}
                    if crash_after == 'claim':
                        self.assertEqual(len(claim_files), 1)
                    else:
                        self.assertEqual(claim_files, {})
                    with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
                         mock.patch('subprocess.check_output', return_value=str(case.base)):
                        self.assertIsNone(RA.completion_gate(
                            args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
                        ))
                    self.assertEqual(marker_path.read_bytes(), marker_bytes)
                    claim_files_after = {
                        item.name: item.read_bytes() for item in ledger.claims_dir.glob('*.claim')
                    }
                    self.assertEqual(len(claim_files_after), 1)
                    if crash_after == 'claim':
                        self.assertEqual(claim_files_after, claim_files)
                    self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'STAGE_SUCCEEDED')
                    self.assertEqual(len(ledger.claim_history()), 1)
                    self.assertEqual(ledger.state()['workflow_state'], 'NEXT_REGISTERED')
                finally:
                    case.doCleanups()

    def test_three_sequential_runs_and_old_replay_preserve_history_then_advance_once(self):
        import dispatch_owner_input as INPUT
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        old = json.loads(registry.read_text())['runs']['fixture-run']
        old_sentinel = Path(old['sentinel']).read_bytes()
        first = self.next_body(registry, 'middle', output)
        procs, receipt = self.launch(route, path, jobs, registry, output, first)
        self.assertEqual(len(procs), 1)
        self.assertTrue(receipt['payload_spawned'])
        self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'wait-next-resource')
        first_armed = SUP.read_armed(ledger)['full-run']
        procs, receipt = self.launch(route, path, jobs, registry, output, first)
        self.assertEqual(procs, [])
        self.assertTrue(receipt['replayed'])
        self.assertEqual(SUP.read_armed(ledger)['full-run'], first_armed)
        final = self.next_body(registry, 'final', output, final=True)
        procs, receipt = self.launch(route, path, jobs, registry, output, final)
        self.assertEqual(len(procs), 1)
        self.assertEqual(json.loads(registry.read_text())['runs']['fixture-run'], old)
        self.assertEqual(Path(old['sentinel']).read_bytes(), old_sentinel)
        self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'wait-next-resource')
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')
        self.assertEqual(ledger.claims(), {})
        owner_attempt, _owner_input = self.live_owner(route, jobs, registry)
        args = SimpleNamespace(
            route_file=str(path), route_node='run-verify', attempt_id='att-verify',
            jobs=str(jobs), agent_home=ROOT, worktree=str(self.base), dispatch_depth=2,
            parent_slug='fixture-owner', parent_attempt_id=owner_attempt,
            parent_harness='codex', parent_transport='headless',
            parent_sandbox='workspace-write', subsession_id=None,
        )
        with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
             mock.patch('subprocess.check_output', return_value=str(self.base)):
            self.assertIsNone(RA.completion_gate(
                args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
            ))
            marker_bytes = (jobs.parent / 'completion' / route['route_id'] / 'full-run.json').read_bytes()
            self.assertIsNone(RA.completion_gate(
                args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
            ))
        self.assertEqual(len(ledger.claims()), 1)
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'STAGE_SUCCEEDED')
        self.assertEqual((jobs.parent / 'completion' / route['route_id'] / 'full-run.json').read_bytes(), marker_bytes)
        self.assertEqual(json.loads((output / 'run.json').read_text()), {"phase":"final"})
        self.assertNotEqual(ledger.state()['workflow_state'], 'COMPLETE')
        final_armed = SUP.read_armed(ledger)['full-run']
        procs, receipt = self.launch(route, path, jobs, registry, output, first)
        self.assertEqual(procs, [])
        self.assertTrue(receipt['replayed'])
        self.assertEqual(SUP.read_armed(ledger)['full-run'], final_armed)

    def test_watch_armed_before_node_scope_keys_admits_next_run_and_replays(self):
        # f7 2026-10-17: a watch armed by a release before write_scope/part_io existed refused
        # the next run (`__a2`) after an update as resource-watch-binding-conflict.
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        arm_file = ledger.root / 'armed/full-run.json'
        def strip():
            legacy = json.loads(arm_file.read_text())
            for key in ('write_scope', 'part_io'):
                legacy.pop(key, None)
            arm_file.write_text(json.dumps(legacy))
            return legacy
        legacy = strip()
        body = self.next_body(registry, 'next', output)
        procs, receipt = self.launch(route, path, jobs, registry, output, body)
        self.assertEqual(len(procs), 1)
        self.assertTrue(receipt['payload_spawned'])
        armed = SUP.read_armed(ledger)['full-run']
        self.assertEqual(armed['predecessor_id'], 'next')
        self.assertTrue('write_scope' in armed and 'part_io' in armed)
        self.assertTrue(any((entry.get('evidence') or {}).get('previous_resource') == legacy
                            for entry in ledger.journal()))
        legacy = strip()
        procs, receipt = self.launch(route, path, jobs, registry, output, body)
        self.assertEqual(procs, [])
        self.assertTrue(receipt['replayed'])
        self.assertEqual(SUP.read_armed(ledger)['full-run'], legacy)

    def test_new_registry_can_follow_but_live_or_unverifiable_prior_cannot(self):
        for invalid in (None, 'live', 'reused', 'sentinel', 'failed'):
            with self.subTest(invalid=invalid), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                SUP.poll_once(route, ledger)
                body = self.next_body(registry, 'next', output)
                old = json.loads(registry.read_text())['runs']['fixture-run']
                if invalid == 'live':
                    old.update(SUP.RR.proc_identity(os.getpid()), status='running')
                elif invalid == 'reused':
                    old.update(SUP.RR.proc_identity(os.getpid()), starttime='wrong')
                elif invalid == 'sentinel':
                    Path(old['sentinel']).write_text('7')
                elif invalid == 'failed':
                    old.update(status='failed', exit_code=7)
                if invalid:
                    registry.write_text(json.dumps({'schema_version': 1, 'runs': {old['run_id']: old}}))
                target = self.base / 'other-registry.json'
                target.write_text(json.dumps({'schema_version': 1, 'runs': {body['run_id']: body}}))
                prior_arm = SUP.read_armed(ledger)['full-run']
                if invalid:
                    with self.assertRaisesRegex(ValueError, 'resource-watch-binding-conflict'):
                        self.arm(path, target, run_id=body['run_id'], extra=('--jobs', str(jobs), '--artifact-base', str(output)))
                    self.assertEqual(SUP.read_armed(ledger)['full-run'], prior_arm)
                else:
                    self.arm(path, target, run_id=body['run_id'], extra=('--jobs', str(jobs), '--artifact-base', str(output)))
                    self.assertEqual(SUP.read_armed(ledger)['full-run']['resource_registry'], str(target))

    @contextlib.contextmanager
    def subfixture(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=self.base) as directory, mock.patch.object(self, 'base', Path(directory)):
            yield

    def test_existing_missing_output_failure_can_follow_but_other_failure_cannot(self):
        for other in (False, True):
            with self.subTest(other=other), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                evidence = SUP.poll_once(route, ledger)[0]['evidence']
                evidence.pop('awaiting_next_resource', None)
                evidence['artifacts'] = {'checked': True, 'reason': 'declared-artifact-missing',
                                         'missing': ['run.json']}
                ledger.record('full-run', 'FAILED_RETRYABLE', evidence=evidence, actor='old-watch')
                ledger.set_workflow_state('FAILED_RETRYABLE', evidence={'node': 'full-run'}, actor='old-watch')
                if other:
                    ledger.record('run-verify', 'FAILED_TERMINAL', evidence={'reason': 'real-failure'})
                body = self.next_body(registry, 'next', output)
                if other:
                    self.launch(route, path, jobs, registry, output, body)
                    failed = json.loads(registry.read_text())['runs']['next']
                    self.assertEqual(failed['launch_error']['message'], 'resource-watch-binding-conflict')
                    self.assertTrue(SUP.RR.resource_never_started(failed))
                    self.assertEqual(ledger.state()['nodes']['run-verify']['state'], 'FAILED_TERMINAL')
                else:
                    self.launch(route, path, jobs, registry, output, body)
                    self.assertEqual(ledger.state()['workflow_state'], 'RUNNING')
                    self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')

    def test_parent_close_blocks_registration_without_changing_prior_binding(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        body = self.next_body(registry, 'next', output)
        ledger.record('full-run', 'RUNNING', evidence={'parent_close': {'preserve_resource': True}})
        before = registry.read_bytes(), SUP.read_armed(ledger)
        with self.assertRaisesRegex(ValueError, 'resource-parent-close-requested'):
            self.launch(route, path, jobs, registry, output, body)
        self.assertEqual((registry.read_bytes(), SUP.read_armed(ledger)), before)

    def test_same_registry_live_reused_inconsistent_failure_and_reused_log_refuse_before_reservation(self):
        for invalid in ('live', 'reused', 'failed', 'log'):
            with self.subTest(invalid=invalid), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                SUP.poll_once(route, ledger)
                body = self.next_body(registry, 'next', output)
                old = json.loads(registry.read_text())['runs']['fixture-run']
                if invalid in ('live', 'reused'):
                    old.update(SUP.RR.proc_identity(os.getpid()), status='running')
                    if invalid == 'reused':
                        old['starttime'] = 'wrong'
                elif invalid == 'failed':
                    old['status'] = 'failed'
                else:
                    body.update(log=old['log'], sentinel=old['sentinel'])
                registry.write_text(json.dumps({'schema_version': 1, 'runs': {old['run_id']: old}}))
                before = registry.read_bytes(), SUP.read_armed(ledger)
                with self.assertRaisesRegex(ValueError, 'resource-route-body-conflict'):
                    self.launch(route, path, jobs, registry, output, body)
                self.assertEqual((registry.read_bytes(), SUP.read_armed(ledger)), before)

    def test_concurrent_different_registries_admit_only_one_next_predecessor(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        def arm_next(name):
            body = self.next_body(registry, name, output)
            target = self.base / (name + '-registry.json')
            target.write_text(json.dumps({'schema_version': 1, 'runs': {name: body}}))
            try:
                self.arm(path, target, run_id=name, extra=('--jobs', str(jobs), '--artifact-base', str(output)))
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(arm_next, ['next-a', 'next-b']))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(ledger.claims(), {})

    def test_intermediate_controller_receipt_resumes_same_owner_without_successors(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        armed = SUP.read_armed(ledger)['full-run']
        row = json.loads(registry.read_text())['runs']['fixture-run']
        args = SimpleNamespace(parent_attempt_id='att-parent', route_id=route['route_id'], route_hash=route['route_hash'], jobs=str(jobs))
        control = SimpleNamespace(thread_id='same-native', pending=lambda: False)
        state = self.base / 'state.json'
        WAIT.JOIN.write_supervisor_state(state, 'att-parent', set(), phase='running-turn')
        context = (SUP, route, ledger, [(armed, row)])
        with mock.patch.object(WAIT, 'context', return_value=context), mock.patch.object(WAIT, 'supervisor', return_value=SUP):
            prompt = WAIT.wait(args, state, control, set(), lambda _: None,
                               sleep=lambda _: self.fail('intermediate result must return without another wait'))
        receipt = WAIT.JOIN.read_supervisor_phase_state(state, 'att-parent').resource['outbox']['receipt']
        self.assertEqual(receipt['state'], 'succeeded')
        self.assertEqual(receipt['reason'], 'awaiting-next-resource')
        self.assertFalse(receipt['verification_pass'])
        self.assertEqual(receipt['successors'], [])
        self.assertIn('same-native', prompt)

    def test_late_final_output_resolves_running_wait_without_claiming_successor(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        original = registry.read_bytes()
        (output / 'run.json').write_text('{"phase":"final"}')
        with ledger.lock():
            SUP.reconcile_resource_artifacts(route, ledger, 'att-parent', jobs)
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'STAGE_SUCCEEDED')
        self.assertEqual(ledger.claims(), {})
        self.assertEqual(registry.read_bytes(), original)

    def test_two_real_resources_without_owner_run_record_settle_full_run(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        first_record = json.loads((output / 'run.json').read_text())['hearting_resource_runs']['runs']['fixture-run']
        for name in ('arm-t', 'arm-b'):
            body = self.next_body(registry, name, output)
            wrong = output / 'experiments' / name / 'run.json'
            body['command'] = [sys.executable, '-c',
                f'from pathlib import Path; p=Path({str(wrong)!r}); p.parent.mkdir(parents=True); '
                'p.write_text(\'{"metric": 1}\')']
            procs, receipt = self.launch(route, path, jobs, registry, output, body)
            self.assertEqual(len(procs), 1)
            self.assertEqual(receipt['runtime_output'], str(output / 'run.json'))
            self.assertIn(str(output / 'run.json'), receipt['expected_outputs'])
            self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'wait-next-resource')
        doc = json.loads((output / 'run.json').read_text())['hearting_resource_runs']['runs']
        self.assertEqual(set(doc), {'fixture-run', 'arm-t', 'arm-b'})
        self.assertEqual(doc['fixture-run'], first_record)
        self.assertEqual([doc[name]['exit_code'] for name in ('arm-t', 'arm-b')], [0, 0])
        registry_bytes = registry.read_bytes()
        self.assertEqual(self._settle_resource_owner(route, path, jobs)[0].result, 'completed')
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'STAGE_SUCCEEDED')
        self.assertEqual(registry.read_bytes(), registry_bytes)
        self.assertEqual(ledger.claims(), {})


if __name__ == '__main__':
    unittest.main()
