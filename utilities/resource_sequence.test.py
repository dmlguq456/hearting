#!/usr/bin/env python3
"""Sequential supervised resources retain one stage and its output requirement."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
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
import dispatch_resource_wait as WAIT


class ResourceSequenceTest(FIX.WorkflowFixture):
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

    def next_body(self, registry, name, output, *, final=False):
        old = json.loads(registry.read_text())['runs']['fixture-run']
        keep = ('cwd', 'route', 'node', 'jobs', 'parent_attempt_id', 'owner_wait', 'config_ref',
                'config_sha256', 'source_commit', 'source_dirty', 'source_git_state', 'config_layout')
        log = self.base / (name + '.log')
        code = (f'from pathlib import Path; Path({str(output / "run.json")!r}).write_text({json.dumps({"phase":"final"})!r})'
                if final else 'pass')
        return {**{key: old[key] for key in keep if key in old}, 'run_id': name,
                'command': [sys.executable, '-c', code], 'log': str(log), 'sentinel': str(log) + '.exit',
                'status': 'launching', 'workflow_state': 'READY', 'resource_policy': 'supervised-owner'}

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

    def launch(self, route, path, jobs, registry, output, body, *, controller=None, close_at=None,
               exit_code=0, expected_exit_codes=None, real_registration=False, real_arm=False):
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
                contextlib.ExitStack() as stack:
            if not real_registration:
                stack.enter_context(mock.patch.object(runner, 'register_registry'))
            stack.enter_context(mock.patch.object(
                runner, 'start_watch', return_value=(watch, SUP.RR.proc_identity(os.getpid()))))
            if not real_arm:
                stack.enter_context(mock.patch.object(runner.subprocess, 'run', side_effect=arm))
            stack.enter_context(mock.patch.object(runner.subprocess, 'Popen', side_effect=popen))
            stdout = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            runner.start_verified(registry, args, route, path, dict(body), controller=controller)
        expected_codes = (expected_exit_codes if expected_exit_codes is not None
                          else [exit_code] * len(payloads))
        self.assertEqual(len(payloads), len(expected_codes))
        for proc, expected in zip(payloads, expected_codes):
            self.assertEqual(proc.wait(timeout=5), expected)
        return payloads, json.loads(stdout.getvalue().splitlines()[-1])

    def test_guard_exit_same_route_resume_preserves_checkpoint_and_advances_to_run_verify(self):
        import dispatch_owner_input as INPUT
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
        @contextlib.contextmanager
        def locked(*a):
            yield None, {'target': 'same', 'thread_id': 'same-native', 'requests': []}
        verifier = self.base / 'verifier-starts'
        def start_verifier(armed, successor, key):
            self.assertEqual(successor, 'run-verify')
            subprocess.run([sys.executable, '-c',
                f'from pathlib import Path; assert Path({str(output / "run.json")!r}).read_text() == {json.dumps({"phase":"resumed"})!r}; '
                f'Path({str(verifier)!r}).open("a").write("run-verify\\n")'], check=True)
            ledger.record(successor, 'RUNNING', evidence={'claim': key}, actor='fixture-verifier')
            return {'started': True, 'surface': 'fixture-external-verifier'}
        with mock.patch.object(INPUT, '_locked', locked), \
                mock.patch.object(INPUT, '_target', return_value=(None, 'same')), \
                mock.patch.object(SUP, '_start_successor', side_effect=start_verifier):
            advanced = SUP.poll_once(route, ledger)[0]
            self.assertEqual(advanced['action'], 'advanced')
            self.assertEqual([item['successor'] for item in advanced['successors']], ['run-verify'])
            self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'settled')
        self.assertEqual(len(ledger.claims()), 1)
        self.assertEqual(verifier.read_text(), 'run-verify\n')
        self.assertEqual(ledger.state()['nodes']['run-verify']['state'], 'RUNNING')
        self.assertEqual(json.loads(registry.read_text())['runs']['paused'], old)
        self.assertEqual((Path(old['sentinel']).read_bytes(), Path(old['log']).read_bytes()), old_bytes)
        self.assertEqual(json.loads((output / 'run.json').read_text()), {"phase":"resumed"})
        self.assertTrue((jobs.parent / 'completion' / route['route_id'] / 'full-run.json').is_file())

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
                    body.update(launch_state='queued', status='launching', launch_request={})
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
                    body.update(launch_state='queued', status='launching', launch_request={})
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
        @contextlib.contextmanager
        def locked(*a):
            yield None, {'target': 'same', 'thread_id': 'same-native', 'requests': []}
        with mock.patch.object(INPUT, '_locked', locked), \
                mock.patch.object(INPUT, '_target', return_value=(None, 'same')), \
                mock.patch.object(SUP, '_start_successor', return_value={'started': False, 'surface': 'external'}) as start:
            self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'advanced')
            self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'settled')
            self.assertEqual(start.call_count, 1)
        self.assertEqual(len(ledger.claims()), 1)
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

    def _closed_late_lab_fixture(self, *, pending_handoff=False, payload_exit=0,
                                 abandon_reason="route-unrecoverable"):
        import route_identity as ROUTE_IDENTITY
        import artifact_producer as AP
        route = FIX.compile_fixture("autopilot-lab", "setup", str(self.base), ["resource-run"])
        route.update(artifact_root=str(self.base),
                     owner_dispatch_depth=0, max_dispatch_depth=0, effective_intensity="direct",
                     work_request={"text": "late result acceptance fixture", "owner_harness": "codex"})
        route.setdefault("selection", {}).update(transport="interactive", inline_reason="fixture-inline")
        run = {"id": "full-run", "kind": "resource-runner", "depends_on": [],
               "outputs": ["run.json"], "write_scope": ["run.json"],
               "completion_gate": "resource-exit", "continuation": {"kind": "supervised"}}
        verify = {"id": "run-verify", "kind": "review-worker", "depends_on": ["full-run"],
                  "outputs": ["reviews/run-verdict.json"], "write_scope": ["reviews/**"],
                  "completion_gate": "lab-run-verify", "dispatch_depth": 0,
                  "execution_surface": "inline", "terminal": not pending_handoff}
        route["nodes"] = [run, verify]
        run.update(id="full-run", outputs=["run.json"], write_scope=["run.json"],
                   completion_gate="resource-exit", continuation={"kind": "supervised"})
        if pending_handoff:
            verify.pop("terminal_gate", None)
            route["nodes"].append({"id": "handoff", "kind": "review-worker",
                "depends_on": ["run-verify"], "outputs": ["setup-handoff.json"],
                "write_scope": ["setup-handoff.json"], "completion_gate": "lab-setup-handoff",
                "dispatch_depth": 0, "terminal": True, "terminal_gate": "lab-setup-handoff"})
        else:
            verify["terminal_gate"] = "lab-run-verify"
        route["route_hash"] = ROUTE_IDENTITY.route_hash(route)
        route["route_id"] = ROUTE_IDENTITY.route_id_from_hash(route["route_hash"])
        path = SUP.route_module().canonical_route_path(self.base, route["route_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(route, indent=2), encoding="utf-8")
        AP.activate(self.base, repository_id="repo_" + "a" * 32,
                    artifact_root_id="root_" + "b" * 32,
                    w7={"campaign_id": "camp_" + "c" * 32})
        producer_cycle = AP.begin(self.base, route_file=path, capability="autopilot-lab",
                                  intensity="direct")
        output = Path(producer_cycle["env"]["AGENT_ARTIFACT_OUTPUT_DIR"])
        jobs = self.base / "jobs.log"
        jobs.write_text("", encoding="utf-8")
        os.environ["AGENT_DISPATCH_JOBS"] = str(jobs)
        registry = self.base / "resource-runs.json"
        registry.write_text(json.dumps({"schema_version": 1, "runs": {}}), encoding="utf-8")
        original_path = output / "experiments" / "original" / "run.json"
        _verdict_path, original_run, _original_verdict = self._write_late_lab_record(
            route, {"run_id": "late-run"}, output, run_path=original_path,
            write_verdict=False, return_documents=True)
        original_json = json.dumps(original_run, indent=2, sort_keys=True)
        payload_code = (f"from pathlib import Path; p=Path({str(original_path)!r}); "
                        f"p.parent.mkdir(parents=True, exist_ok=True); "
                        f"p.write_text({original_json!r}, encoding='utf-8')")
        if payload_exit:
            payload_code += f"; raise SystemExit({payload_exit})"
        body = {"run_id": "late-run", "route": str(path), "node": "full-run",
                "jobs": str(jobs), "cwd": str(self.base), "log": str(self.base / "late-run.log"),
                "sentinel": str(self.base / "late-run.log.exit"),
                "command": [sys.executable, "-c", payload_code], "status": "launching",
                "workflow_state": "READY"}
        payloads, receipt = self.launch(route, path, jobs, registry, output, body,
                                        expected_exit_codes=(0, payload_exit),
                                        real_registration=True, real_arm=True)
        ledger = SUP.ledger_for(route, jobs)
        self.assertTrue(receipt["payload_spawned"])
        import resource_run_evidence as RUN_EVIDENCE
        initial_poll = SUP.poll_once(route, ledger)[0]
        runtime_document, _ = RUN_EVIDENCE.read_document(output / "run.json")
        expected_action = "halt-missing-artifact" if payload_exit == 0 else "halt-failed"
        self.assertEqual(initial_poll["action"], expected_action,
                         {"poll": initial_poll, "runtime_document": runtime_document})
        if payload_exit == 0:
            self.assertTrue(RUN_EVIDENCE.runtime_only(runtime_document))
        failed = next(entry for entry in reversed(ledger.journal())
                      if entry.get("node") == "full-run" and entry.get("state") == "FAILED_RETRYABLE")
        if payload_exit == 0:
            self.assertTrue(failed["evidence"].get("runtime_only_output"))
            self.assertTrue(RUN_EVIDENCE.runtime_only(
                RUN_EVIDENCE.read_document(output / "run.json")[0]))
        else:
            self.assertFalse(failed["evidence"].get("succeeded"))
        closed, _ = SUP.route_module().close_route(route, path, allow_unproven=True, jobs=jobs)
        self.assertFalse(closed["terminal_gate_proven"])
        AP.finalize(self.base, cycle_id=producer_cycle["cycle_id"], state="abandoned",
                    abandon_reason=abandon_reason)
        return route, path, jobs, registry, output, ledger, payloads

    def _write_late_lab_record(self, route, row, output, *, missing_arm=None, alter_checkpoint=False,
                               run_path=None, write_verdict=True, return_documents=False):
        import hashlib
        arms = {}
        verdict_arms = {}
        for index, arm_id in enumerate(("t", "b"), 1):
            run_id = f"{route['route_id']}-{arm_id}-run"
            config_ref = f"config:{arm_id}.json"
            config_raw = f"config bytes {arm_id}".encode()
            config_sha = hashlib.sha256(config_raw).hexdigest()
            config_dir = output / "experiments" / "late-result" / "_internal" / "configs"
            config_dir.mkdir(parents=True, exist_ok=True)
            snapshot = config_dir / f"{config_sha}.json"
            snapshot.write_bytes(config_raw)
            manifest = config_dir / f"{run_id}.manifest.json"
            manifest.write_text(json.dumps({"run_id": run_id, "config_ref": config_ref,
                "source_sha256": config_sha, "snapshot_sha256": config_sha,
                "snapshot_path": str(snapshot)}), encoding="utf-8")
            checkpoint = output / "checkpoints" / f"{arm_id}.pt"
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            checkpoint.write_bytes(f"CPU checkpoint {arm_id}".encode())
            checkpoint_sha = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
            per_arm = output / "reviews" / "run" / f"run-verify-{arm_id}.json"
            per_arm.parent.mkdir(parents=True, exist_ok=True)
            counters = {"attempt": 400000, "successful": 399857 - index, "skipped": 143 + index}
            run_dir = str(output / "runs" / arm_id)
            compute_id = f"cpu-{arm_id}-execution"
            per_arm_doc = {"arm": arm_id, "status": "PASS",
                "run_dir": run_dir,
                "selected_checkpoint": {"path": str(checkpoint), "sha256": checkpoint_sha},
                "checks": {"status-complete": {"ok": True, "status": {"status": "complete", **counters}},
                           "provenance-continuity": {"ok": True}}}
            per_arm.write_text(json.dumps(per_arm_doc, sort_keys=True), encoding="utf-8")
            per_arm_sha = hashlib.sha256(per_arm.read_bytes()).hexdigest()
            identity_hash = hashlib.sha256(f"{route['route_id']}-{arm_id}".encode()).hexdigest()
            arms[arm_id] = {"run_id": run_id, "compute_run_id": compute_id,
                "compute_run_dir": str(output / ".compute-runs" / compute_id),
                "run_dir": run_dir, "run_verify": str(per_arm), "counters": counters,
                "compute_exit_code": 0, "config_ref": config_ref, "config_sha256": config_sha,
                "config_manifest": str(manifest), "trainer_status": {"status": "complete"},
                "trainer_identity": {"pid": 30000 + index, "starttime": str(40000 + index),
                                     "cmdline_sha256": identity_hash},
                "wrapper_identity": {"pid": 31000 + index, "starttime": str(41000 + index),
                                     "cmdline_sha256": identity_hash},
                "ckpt_path": str(checkpoint), "best": {"checkpoint": str(checkpoint),
                                                            "sha256": checkpoint_sha}}
            verdict_arms[arm_id] = {"status": "PASS", "evidence": str(per_arm),
                "evidence_sha256": per_arm_sha, "counters": counters,
                "selected_checkpoint": {"path": str(checkpoint), "sha256": checkpoint_sha}}
        if missing_arm:
            verdict_arms.pop(missing_arm, None)
        resource = {key: row.get(key) for key in
                    ("run_id", "pid", "starttime", "command_hash", "sentinel", "log", "exit_code")}
        run_document = {"route": route["route_id"], "status": "complete",
                        "source_commit": row.get("source_commit"), "resource": resource, "arms": arms}
        run_path = Path(run_path) if run_path is not None else output / "run.json"
        run_path.parent.mkdir(parents=True, exist_ok=True)
        run_path.write_text(json.dumps(run_document, indent=2, sort_keys=True), encoding="utf-8")
        verdict_path = output / "reviews" / "run-verdict.json"
        verdict_path.parent.mkdir(parents=True, exist_ok=True)
        verdict_document = {"run_verified": True, "verdict": "diagnosed",
                            "resource": {"run_id": row["run_id"]}, "arms": verdict_arms}
        if write_verdict:
            verdict_path.write_text(json.dumps(verdict_document, indent=2, sort_keys=True), encoding="utf-8")
        if alter_checkpoint:
            (output / "checkpoints" / "b.pt").write_bytes(b"altered checkpoint")
        return (verdict_path, run_document, verdict_document) if return_documents else verdict_path

    def _publish_late_verification(self, route, jobs, verdict_path):
        verify = next(node for node in route["nodes"] if node["id"] == "run-verify")
        SUP.route_module().write_completion_marker(
            route, verify, "run-verify", verdict_path, jobs=jobs)

    def _artifact_tree(self):
        import hashlib
        rows = {}
        for path in sorted(self.base.rglob("*")):
            if path.is_file():
                rows[path.relative_to(self.base).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        return rows

    def test_closed_late_success_accepts_all_bound_arms_and_keeps_handoff_pending(self):
        import hashlib
        import dispatch_notice_state as NOTICE
        import work_start as WORK_START
        import artifact_lifecycle as AL
        import artifact_producer as AP
        import late_resource_result as LATE
        route, path, jobs, registry, output, ledger, payloads = self._closed_late_lab_fixture(
            pending_handoff=True)
        raw_outcome = SUP.route_module().outcome_path(path).read_bytes()
        original_journal = ledger.journal_path.read_bytes()
        cycle = AP.route_cycle_for(Path(route["artifact_root"]), route)
        closure = LATE._abandoned_cycle_snapshot(route, cycle)
        self.assertIsNotNone(closure)
        manifest_path = Path(closure["manifest_path"])
        initial_manifest = manifest_path.read_bytes()
        source_snapshot = AL.manifest_snapshot_path(
            Path(route["artifact_root"]), cycle["cycle_id"], closure["snapshot_revision_id"])
        source_snapshot_bytes = source_snapshot.read_bytes()
        row = json.loads(registry.read_text())["runs"]["late-run"]
        verdict = self._write_late_lab_record(route, row, output)
        self._publish_late_verification(route, jobs, verdict)

        result = WORK_START.start_work(route, path, jobs)
        self.assertEqual(result["reason"], "resource-result-accepted-terminal-gates-pending", result)
        projection = NOTICE.closed_outcome(path, route, jobs=jobs)
        self.assertTrue(projection["resource_result_accepted"])
        self.assertFalse(projection["terminal_gate_proven"])
        self.assertEqual(projection["resource_result_nodes"], ["full-run"])
        self.assertEqual(len(projection["accepted_resource_results"][0]["executions"][0]["lab_arms"]
                             ["required_arms"]), 2)
        self.assertEqual({arm["arm"] for arm in projection["accepted_resource_results"][0]
                          ["executions"][0]["lab_arms"]["arms"]}, {"t", "b"})
        self.assertTrue(ledger.journal_path.read_bytes().startswith(original_journal))
        self.assertEqual(ledger.state()["nodes"]["full-run"]["state"], "FAILED_RETRYABLE")
        self.assertEqual(SUP.route_module().outcome_path(path).read_bytes(), raw_outcome)
        self.assertEqual(ledger.claims(), {})
        self.assertEqual(set(json.loads(registry.read_text())["runs"]), {"late-run"})
        self.assertEqual(len(payloads), 2)
        publication = result["resource_adjudication"]["producer_publication"]
        self.assertEqual(publication["status"], "emitted", publication)
        current_manifest = json.loads(manifest_path.read_bytes())
        self.assertEqual(current_manifest["cycle"]["state"], "abandoned")
        self.assertNotEqual(manifest_path.read_bytes(), initial_manifest)
        self.assertEqual(source_snapshot.read_bytes(), source_snapshot_bytes)
        published_proof = next(item for item in current_manifest["artifact_revisions"]
                               if item["locator"]["path"] ==
                               f"artifacts/late-results/{result['resource_adjudication']['proof']['idempotency_key']}.json")
        self.assertEqual(published_proof["content_digest"].removeprefix("sha256:"),
                         hashlib.sha256(Path(publication["proof_path"]).read_bytes()).hexdigest())

        before = self._artifact_tree()
        gates = SUP.route_module().terminal_gate_observation(route, jobs=jobs)
        self.assertFalse(SUP._closed_terminal_projection(
            route, path, gates, ledger.read_only_state()["nodes"], ledger.claims(), jobs=jobs))
        self.assertEqual(NOTICE.closed_outcome(path, route, jobs=jobs)["resource_result_accepted"], True)
        self.assertEqual(self._artifact_tree(), before)

        journal_size = ledger.journal_path.stat().st_size
        with ThreadPoolExecutor(max_workers=2) as pool:
            repeated = list(pool.map(lambda _: WORK_START.start_work(route, path, jobs), range(2)))
        self.assertTrue(all(item["reason"] == "resource-result-accepted-terminal-gates-pending"
                            for item in repeated))
        self.assertEqual(ledger.journal_path.stat().st_size, journal_size)
        self.assertEqual(set(json.loads(registry.read_text())["runs"]), {"late-run"})

    def test_closed_late_acceptance_projection_is_current_when_producer_refresh_is_off(self):
        import dispatch_notice_state as NOTICE
        import work_start as WORK_START
        import artifact_producer as AP
        route, path, jobs, registry, output, ledger, payloads = self._closed_late_lab_fixture(
            pending_handoff=True)
        row = json.loads(registry.read_text())["runs"]["late-run"]
        verdict = self._write_late_lab_record(route, row, output)
        self._publish_late_verification(route, jobs, verdict)
        cycle = AP.route_cycle_for(Path(route["artifact_root"]), route)
        manifest_path = AP._record_cycle_manifest_path(Path(route["artifact_root"]), cycle)
        manifest_before = manifest_path.read_bytes()
        xdg = self.base / "xdg-refresh-off"
        refresh_off = xdg / "hearting" / AP.CLOSED_CYCLE_REFRESH_OFF_FILE
        refresh_off.parent.mkdir(parents=True)
        refresh_off.write_text("off\n", encoding="utf-8")

        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}):
            result = WORK_START.start_work(route, path, jobs)
            projection = NOTICE.closed_outcome(path, route, jobs=jobs)

        self.assertEqual(result["reason"], "resource-result-accepted-terminal-gates-pending", result)
        self.assertTrue(projection["resource_result_accepted"])
        self.assertFalse(projection["terminal_gate_proven"])
        proof = projection["accepted_resource_results"][-1]
        self.assertTrue((output / "late-results" / f"{proof['idempotency_key']}.json").is_file())
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertEqual(set(json.loads(registry.read_text())["runs"]), {"late-run"})
        self.assertEqual(len(payloads), 2)

    def test_closed_late_success_can_complete_only_when_every_terminal_gate_passes(self):
        import work_start as WORK_START
        route, path, jobs, registry, output, ledger, payloads = self._closed_late_lab_fixture()
        raw_outcome = SUP.route_module().outcome_path(path).read_bytes()
        row = json.loads(registry.read_text())["runs"]["late-run"]
        verdict = self._write_late_lab_record(route, row, output)
        self._publish_late_verification(route, jobs, verdict)
        result = WORK_START.start_work(route, path, jobs)
        self.assertEqual(result["state"], "completed", result)
        self.assertTrue(result["outcome"]["terminal_gate_proven"])
        self.assertTrue(result["outcome"]["resource_result_accepted"])
        self.assertEqual(SUP.route_module().outcome_path(path).read_bytes(), raw_outcome)
        self.assertEqual(ledger.state()["nodes"]["full-run"]["state"], "FAILED_RETRYABLE")
        self.assertEqual(ledger.claims(), {})
        self.assertEqual(set(json.loads(registry.read_text())["runs"]), {"late-run"})
        self.assertEqual(len(payloads), 2)

    def test_closed_late_result_rejects_missing_or_altered_required_arm(self):
        import work_start as WORK_START
        for missing_arm, alter_checkpoint in (("b", False), (None, True)):
            with self.subTest(missing_arm=missing_arm, alter_checkpoint=alter_checkpoint), self.subfixture():
                route, path, jobs, registry, output, ledger, payloads = self._closed_late_lab_fixture()
                raw_outcome = SUP.route_module().outcome_path(path).read_bytes()
                row = json.loads(registry.read_text())["runs"]["late-run"]
                verdict = self._write_late_lab_record(route, row, output,
                    missing_arm=missing_arm, alter_checkpoint=alter_checkpoint)
                self._publish_late_verification(route, jobs, verdict)
                result = WORK_START.start_work(route, path, jobs)
                self.assertEqual(result["reason"], "route-closed-unproven")
                self.assertNotIn("resource_result_accepted", result)
                self.assertEqual(SUP.route_module().outcome_path(path).read_bytes(), raw_outcome)
                self.assertFalse(any(entry.get("actor") == "late-resource-result"
                                     for entry in ledger.journal()))
                self.assertEqual(ledger.state()["nodes"]["full-run"]["state"], "FAILED_RETRYABLE")
                self.assertEqual(ledger.claims(), {})
                self.assertEqual(set(json.loads(registry.read_text())["runs"]), {"late-run"})
                self.assertEqual(len(payloads), 2)

    def test_closed_late_result_rejects_matching_subset_against_immutable_producer_roster(self):
        """The verifier marker is current; only the immutable close-time roster catches this subset."""
        import hashlib
        import work_start as WORK_START
        import late_resource_result as LATE
        import artifact_producer as AP

        route, path, jobs, registry, output, ledger, payloads = self._closed_late_lab_fixture()
        row = json.loads(registry.read_text())["runs"]["late-run"]
        original_path = output / "experiments" / "original" / "run.json"
        original_bytes = original_path.read_bytes()
        cycle = AP.route_cycle_for(Path(route["artifact_root"]), route)
        manifest_path = AP._record_cycle_manifest_path(Path(route["artifact_root"]), cycle)
        original_manifest = manifest_path.read_bytes()

        verdict, run_document, verdict_document = self._write_late_lab_record(
            route, row, output, return_documents=True)
        run_document["arms"].pop("b")
        verdict_document["arms"].pop("b")
        (output / "run.json").write_text(
            json.dumps(run_document, indent=2, sort_keys=True), encoding="utf-8")
        verdict.write_text(json.dumps(verdict_document, indent=2, sort_keys=True), encoding="utf-8")
        self._publish_late_verification(route, jobs, verdict)

        node = next(item for item in route["nodes"] if item["id"] == "full-run")
        marker = LATE._marker_proofs(route, node, jobs, LATE._route_module())
        self.assertEqual(marker[0]["evidence_sha256"], hashlib.sha256(verdict.read_bytes()).hexdigest())
        self.assertEqual(set(json.loads((output / "run.json").read_text())["arms"]), {"t"})
        self.assertEqual(set(json.loads(verdict.read_text())["arms"]), {"t"})
        self.assertEqual(original_path.read_bytes(), original_bytes)

        result = WORK_START.start_work(route, path, jobs)

        self.assertEqual(result["reason"], "route-closed-unproven", result)
        self.assertNotIn("resource_result_accepted", result)
        self.assertEqual(original_path.read_bytes(), original_bytes)
        self.assertEqual(manifest_path.read_bytes(), original_manifest)
        self.assertFalse(any(entry.get("actor") == "late-resource-result"
                             for entry in ledger.journal()))
        self.assertEqual(set(json.loads(registry.read_text())["runs"]), {"late-run"})
        self.assertEqual(len(payloads), 2)

    def test_closed_late_entry_preserves_explicit_user_abandon(self):
        """Mechanical and explicit abandon use the same normal start entry."""
        import dispatch_notice_state as NOTICE
        import work_start as WORK_START
        import artifact_producer as AP
        route, path, jobs, registry, output, ledger, payloads = self._closed_late_lab_fixture(
            abandon_reason="operator-decision")
        raw_outcome = SUP.route_module().outcome_path(path).read_bytes()
        row = json.loads(registry.read_text())["runs"]["late-run"]
        verdict = self._write_late_lab_record(route, row, output)
        self._publish_late_verification(route, jobs, verdict)
        cycle = AP.route_cycle_for(Path(route["artifact_root"]), route)
        manifest_path = AP._record_cycle_manifest_path(Path(route["artifact_root"]), cycle)
        manifest_before = manifest_path.read_bytes()

        result = WORK_START.start_work(route, path, jobs)

        self.assertEqual(result["reason"], "route-closed-unproven", result)
        self.assertNotIn("resource_result_accepted", result)
        projection = NOTICE.closed_outcome(path, route, jobs=jobs)
        self.assertFalse(projection.get("resource_result_accepted", False))
        self.assertFalse(projection["terminal_gate_proven"])
        self.assertEqual(SUP.route_module().outcome_path(path).read_bytes(), raw_outcome)
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertFalse(any(entry.get("actor") == "late-resource-result"
                             for entry in ledger.journal()))
        self.assertEqual(set(json.loads(registry.read_text())["runs"]), {"late-run"})
        self.assertEqual(len(payloads), 2)

    def test_closed_late_boundary_rejects_live_unknown_identity_and_parent_stop(self):
        """Controlled liveness observations and a constructed intent are boundary tests, not E2E."""
        import work_start as WORK_START
        import late_resource_result as LATE
        route, path, jobs, registry, output, ledger, payloads = self._closed_late_lab_fixture()
        row = json.loads(registry.read_text())["runs"]["late-run"]
        verdict = self._write_late_lab_record(route, row, output)
        self._publish_late_verification(route, jobs, verdict)
        observer = LATE._supervisor()
        actual_observation = observer.resource_evidence
        for liveness in ("live", "unknown"):
            with mock.patch.object(observer, "resource_evidence",
                    side_effect=lambda armed, value=liveness: {
                        **actual_observation(armed), "liveness": value}):
                result = WORK_START.start_work(route, path, jobs)
            self.assertEqual(result["reason"], "route-closed-unproven", result)
            self.assertNotIn("resource_result_accepted", result)

        ledger._append({"at": WS.now_iso(), "route_id": route["route_id"],
                        "route_hash": route["route_hash"], "actor": "parent-close",
                        "evidence": {"parent_close": {
                            "route": route, "route_file": str(path.resolve()),
                            "owner_attempt_id": "att-fixture-stop", "attempts": [],
                            "resources": [], "protected_resources": [], "stop_resources": False,
                            "resource_branches": {}, "workflow_processes": [],
                            "observer_namespace": {}, "summary": "explicit stop", "head_commit": None}},
                        "pid": os.getpid()})
        stopped = WORK_START.start_work(route, path, jobs)
        self.assertEqual(stopped["reason"], "cancelled-by-parent", stopped)
        self.assertNotIn("resource_result_accepted", stopped)
        self.assertFalse(any(entry.get("actor") == "late-resource-result"
                             for entry in ledger.journal()))
        self.assertEqual(set(json.loads(registry.read_text())["runs"]), {"late-run"})
        self.assertEqual(len(payloads), 2)

    def test_mechanical_failed_terminal_accepts_resource_only_and_keeps_gate_unproven(self):
        """Constructed legacy FAILED_TERMINAL provenance can admit exact evidence, not completion."""
        import work_start as WORK_START
        route, path, jobs, registry, output, ledger, payloads = self._closed_late_lab_fixture()
        raw_outcome = SUP.route_module().outcome_path(path).read_bytes()
        ledger.record("run-verify", "FAILED_TERMINAL",
                      evidence={"reason": "terminal-gate-unproven"}, actor="workflow-supervisor")
        ledger.set_workflow_state("FAILED_TERMINAL",
                                  evidence={"reason": "terminal-gate-unproven"},
                                  actor="workflow-supervisor")
        row = json.loads(registry.read_text())["runs"]["late-run"]
        verdict = self._write_late_lab_record(route, row, output)
        self._publish_late_verification(route, jobs, verdict)

        result = WORK_START.start_work(route, path, jobs)

        self.assertEqual(result["reason"], "resource-result-accepted-terminal-gates-pending", result)
        self.assertTrue(result["outcome"]["resource_result_accepted"])
        self.assertFalse(result["outcome"]["terminal_gate_proven"])
        self.assertEqual(ledger.state()["workflow_state"], "FAILED_TERMINAL")
        self.assertEqual(ledger.state()["nodes"]["run-verify"]["state"], "FAILED_TERMINAL")
        self.assertEqual(SUP.route_module().outcome_path(path).read_bytes(), raw_outcome)
        self.assertEqual(set(json.loads(registry.read_text())["runs"]), {"late-run"})
        self.assertEqual(len(payloads), 2)

    def test_closed_late_result_rejects_foreign_identity_exit_failed_verification_and_binding(self):
        import work_start as WORK_START
        cases = ("foreign-identity", "nonzero-exit", "altered-exit-sentinel",
                 "failed-verification", "foreign-binding", "config-binding",
                 "unrelated-terminal-failure", "untrusted-terminal-failure")
        for case in cases:
            with self.subTest(case=case), self.subfixture():
                payload_exit = 7 if case == "nonzero-exit" else 0
                route, path, jobs, registry, output, ledger, payloads = self._closed_late_lab_fixture(
                    payload_exit=payload_exit)
                raw_outcome = SUP.route_module().outcome_path(path).read_bytes()
                row = json.loads(registry.read_text())["runs"]["late-run"]
                verdict = self._write_late_lab_record(route, row, output)
                if case in {"unrelated-terminal-failure", "untrusted-terminal-failure"}:
                    # Constructed legacy states: unrelated causes and untrusted actors stay blocked.
                    failure_reason = ("terminal-gate-unproven" if case == "untrusted-terminal-failure"
                                      else "verification-failed")
                    failure_actor = ("review-worker" if case == "untrusted-terminal-failure"
                                     else "workflow-supervisor")
                    ledger.record("run-verify", "FAILED_TERMINAL",
                                  evidence={"reason": failure_reason}, actor=failure_actor)
                    ledger.set_workflow_state("FAILED_TERMINAL",
                                              evidence={"reason": failure_reason},
                                              actor="workflow-supervisor")
                elif case == "foreign-identity":
                    run_path = output / "run.json"
                    run_document = json.loads(run_path.read_text())
                    run_document["resource"]["run_id"] = "foreign-resource-run"
                    run_path.write_text(json.dumps(run_document, indent=2, sort_keys=True))
                elif case == "altered-exit-sentinel":
                    Path(row["sentinel"]).write_text("7\n")
                elif case == "config-binding":
                    run_path = output / "run.json"
                    run_document = json.loads(run_path.read_text())
                    run_document["arms"]["t"]["config_ref"] = "config:foreign.json"
                    run_path.write_text(json.dumps(run_document, indent=2, sort_keys=True))
                elif case == "failed-verification":
                    document = json.loads(verdict.read_text())
                    document["arms"]["t"]["status"] = "FAIL"
                    verdict.write_text(json.dumps(document, indent=2, sort_keys=True))
                elif case == "foreign-binding":
                    armed_path = ledger.root / "armed" / "full-run.json"
                    armed = json.loads(armed_path.read_text())
                    armed["route_hash"] = "sha256:" + "0" * 64
                    armed_path.write_text(json.dumps(armed, sort_keys=True))
                self._publish_late_verification(route, jobs, verdict)

                result = WORK_START.start_work(route, path, jobs)

                self.assertEqual(result["reason"], "route-closed-unproven", result)
                self.assertNotIn("resource_result_accepted", result)
                self.assertEqual(SUP.route_module().outcome_path(path).read_bytes(), raw_outcome)
                self.assertFalse(any(entry.get("actor") == "late-resource-result"
                                     for entry in ledger.journal()))
                self.assertEqual(ledger.claims(), {})
                self.assertEqual(set(json.loads(registry.read_text())["runs"]), {"late-run"})
                self.assertEqual(len(payloads), 2)

    def test_closed_late_result_lost_append_reply_replays_one_proof(self):
        import work_start as WORK_START
        route, path, jobs, registry, output, ledger, payloads = self._closed_late_lab_fixture(
            pending_handoff=True)
        raw_outcome = SUP.route_module().outcome_path(path).read_bytes()
        row = json.loads(registry.read_text())["runs"]["late-run"]
        verdict = self._write_late_lab_record(route, row, output)
        self._publish_late_verification(route, jobs, verdict)

        append = WS.WorkflowLedger._append
        def append_then_lose_reply(instance, entry):
            append(instance, entry)
            if entry.get("actor") == "late-resource-result":
                raise OSError("reply lost after durable late-result append")

        with mock.patch.object(WS.WorkflowLedger, "_append", append_then_lose_reply):
            interrupted = WORK_START.start_work(route, path, jobs)
        self.assertEqual(interrupted["state"], "needs-attention", interrupted)
        self.assertIn("reply lost after durable", interrupted["detail"])
        self.assertEqual(sum(entry.get("actor") == "late-resource-result"
                             for entry in ledger.journal()), 1)
        journal_size = ledger.journal_path.stat().st_size
        self.assertEqual(SUP.route_module().outcome_path(path).read_bytes(), raw_outcome)

        replay = WORK_START.start_work(route, path, jobs)

        self.assertEqual(replay["reason"], "resource-result-accepted-terminal-gates-pending", replay)
        self.assertEqual(sum(entry.get("actor") == "late-resource-result"
                             for entry in ledger.journal()), 1)
        self.assertEqual(ledger.journal_path.stat().st_size, journal_size)
        self.assertEqual(SUP.route_module().outcome_path(path).read_bytes(), raw_outcome)
        self.assertEqual(ledger.state()["nodes"]["full-run"]["state"], "FAILED_RETRYABLE")
        self.assertEqual(ledger.claims(), {})
        self.assertEqual(set(json.loads(registry.read_text())["runs"]), {"late-run"})
        self.assertEqual(len(payloads), 2)


if __name__ == '__main__':
    unittest.main()
