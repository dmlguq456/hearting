#!/usr/bin/env python3
"""Owner continuation with preserved CPU resource fixtures; no GPU or provider calls."""
from pathlib import Path
from types import SimpleNamespace
import importlib.util
import json
import os
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatch_contract as DC
import dispatch_owner_input as INPUT
import dispatch_replacement as REPLACE
import route_parent_close as CLOSE

spec = importlib.util.spec_from_file_location('owner_resource_fixture', HERE / 'route_parent_close.test.py')
FIXTURE = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = FIXTURE
spec.loader.exec_module(FIXTURE)


class OwnerResourceContinuationTest(unittest.TestCase):
    def setUp(self):
        FIXTURE.ParentCloseTest.setUp(self)
        # Enumerate only this test's real CPU processes. Unrelated private live
        # host processes must not make the fixture's negative census nondeterministic.
        original = Path.iterdir
        def fixture_proc(path):
            if path == Path('/proc'):
                return iter(Path('/proc') / str(process.pid) for process in self.processes)
            return original(path)
        patch = mock.patch.object(Path, 'iterdir', fixture_proc)
        patch.start(); self.addCleanup(patch.stop)
    reap = FIXTURE.ParentCloseTest.reap
    aid = FIXTURE.ParentCloseTest.aid
    process = FIXTURE.ParentCloseTest.process
    row = FIXTURE.ParentCloseTest.row
    resource = FIXTURE.ParentCloseTest.resource

    def ended_owner(self, harness='codex'):
        owner = self.process('att-owner')
        meta = self.row('att-owner', process=owner, harness=harness, status='done',
                        note='dead-runtime-exit', failure_class='runtime', supervisor_lease='flock-v1',
                        supervisor_lease_file=str(DC.supervisor_lease_path(self.jobs, self.aid('att-owner'))),
                        supervisor_lease_nonce='fixture-nonce')
        owner.terminate()
        owner.wait(timeout=5)
        INPUT.OwnerInput(self.jobs, meta['attempt_id'], 'old-thread', 'codex-active-turn', lambda event: None)
        return meta

    def compute(self, meta, run_id='fixture-compute', *, binding=None):
        payload = self.process('att-owner', env={'HEARTING_COMPUTE_RUN_ID': run_id})
        run_root = self.base / 'compute-runs'
        path = run_root / run_id / 'meta.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'provenance': {'attempt_id': meta['attempt_id'],
            'route': {'route_id': self.route['route_id'], 'route_file': str(self.path)}, **(binding or {})}}))
        compute = SimpleNamespace(load_config=lambda: {'run_root': run_root}, ConfigError=ValueError,
            _run_state=lambda config, rid: {'stop_reason': None, 'state': 'running'},
            config_path=lambda: self.base / 'compute.yaml')
        patch = mock.patch.object(CLOSE, '_compute', return_value=compute)
        patch.start(); self.addCleanup(patch.stop)
        return payload, path

    def fields(self):
        return self.jobs.read_text().strip().split('\t')

    def test_registered_compute_and_resource_continue_all_harnesses_without_payload_changes(self):
        for harness in ('claude', 'codex', 'opencode'):
            with self.subTest(harness=harness):
                child = OwnerResourceContinuationTest(); child.setUp()
                try:
                    meta = child.ended_owner(harness)
                    resource = child.process('att-owner')
                    registry, _ = child.resource(resource)
                    compute, record = child.compute(meta)
                    before = {path: path.read_bytes() for path in (registry, record)}
                    whole = DC.attempt_process_quiescence(meta, terminal_receipt=True)
                    self.assertEqual((whole.state, whole.reason), ('live', 'attempt-descendant-live'))
                    with mock.patch.object(CLOSE, '_signal', side_effect=AssertionError('resource signal')), \
                         mock.patch.object(os, 'kill', side_effect=AssertionError('process signal')):
                        proof, preserved = CLOSE.owner_continuation_processes(meta, child.jobs)
                        self.assertEqual((proof.state, proof.reason), ('quiescent', 'registered-resources-preserved'))
                        self.assertEqual({row['kind'] for row in preserved}, {'resource', 'compute'})
                        receipt = INPUT.submit(child.jobs, meta['attempt_id'], 'Continue the existing runs; no relaunch.', 'answer-1')
                        self.assertTrue(receipt['retained'])
                        claim_proof = REPLACE.death_proof(child.fields(), meta, jobs=child.jobs)
                        self.assertEqual(claim_proof['death_kind'], REPLACE.CORRECTED)
                        self.assertEqual(claim_proof['preserved_resources'], preserved)
                        self.assertEqual(len(INPUT.retained(child.jobs, meta['attempt_id'])), 1)
                    self.assertIsNone(resource.poll()); self.assertIsNone(compute.poll())
                    self.assertEqual({path: path.read_bytes() for path in before}, before)
                    self.assertEqual(DC.attempt_process_quiescence(meta).state, 'live')
                finally:
                    child.doCleanups()

    def test_same_resource_claim_revalidation_context_and_owner_only_command(self):
        meta = self.ended_owner()
        payload, record = self.compute(meta)
        args = SimpleNamespace(attempt_id=meta['attempt_id'], jobs_path=self.jobs, worktree=str(self.base),
            replacement_input_argv=['--start', '--attempt-id', meta['attempt_id'], '--prompt-text', 'existing training'],
            owner_route_file=str(self.path), worker_type='owner')
        meta.update(DC.parse_registry_metadata(REPLACE.seal_launch_input(args, 'codex', 'existing training')))
        fields = self.fields(); fields[5] = ','.join(k+'='+str(v) for k,v in meta.items())
        self.jobs.write_text('\t'.join(fields)+'\n')
        # The seal is stable work input; fixture cycle reuse avoids producing a real cycle.
        before = record.read_bytes()
        INPUT.submit(self.jobs, meta['attempt_id'], 'Adopt the recorded run ID. Do not start full-run.', 'answer-1')
        reuse = {'completed': [], 'cycle_id': 'fixture-cycle', 'producer_id': 'fixture-producer', 'gate_releases': []}
        with mock.patch.object(REPLACE, '_reuse_snapshot', return_value=reuse):
            claim = REPLACE.claim(self.jobs, meta['attempt_id'])
            self.assertEqual(claim['proof']['death_kind'], REPLACE.CORRECTED)
            self.assertEqual(REPLACE.claim(self.jobs, meta['attempt_id']), claim)
            source_fields, source, replay = REPLACE.validate_claim_source(self.jobs, self.jobs.read_text().splitlines(), claim)
            command = REPLACE._owner_command(self.jobs, claim, source, replay)
            self.assertTrue(command[1].endswith('/dispatch-owner.py'))
            self.assertNotIn('resource-runner.py', ' '.join(command))
            self.assertNotIn('--node', command)
            context = REPLACE.recovery_instructions(SimpleNamespace(jobs_path=self.jobs,
                automatic_retry_of=meta['attempt_id'], attempt_id=claim['replacement_attempt_id'], worker_type='owner'))
            self.assertIn(str(record), context)
            self.assertIn('fixture-compute', context)
            self.assertIn('do not relaunch a resource node', context)
            self.assertIn('Do not start full-run.', context)
            # A new unregistered residue vetoes the existing claim at actual-spawn revalidation.
            residue = self.process('att-owner')
            with self.assertRaises(DC.DispatchContractError) as caught:
                REPLACE.validate_claim_source(self.jobs, self.jobs.read_text().splitlines(), claim)
            self.assertEqual(caught.exception.reason, 'replacement-process-live')
            self.assertIsNone(residue.poll())
        self.assertIsNone(payload.poll()); self.assertEqual(record.read_bytes(), before)

    def test_unregistered_and_wrong_binding_compute_do_not_continue(self):
        meta = self.ended_owner()
        payload, path = self.compute(meta, binding={'attempt_id': self.aid('att-other')})
        self.assertEqual(CLOSE.owner_continuation_processes(meta, self.jobs)[0].state, 'live')
        with self.assertRaisesRegex(INPUT.InputError, 'owner-input-unavailable'):
            INPUT.submit(self.jobs, meta['attempt_id'], 'answer', 'wrong-owner')
        data = json.loads(path.read_text()); data['provenance']['attempt_id'] = meta['attempt_id']
        data['provenance']['route']['route_file'] = str(self.base / 'other-route.json')
        path.write_text(json.dumps(data))
        self.assertEqual(CLOSE.owner_continuation_processes(meta, self.jobs)[0].state, 'live')
        self.assertIsNone(payload.poll())

    def test_resource_only_id_is_not_registered_compute_protection(self):
        meta = self.ended_owner()
        payload = self.process('att-owner', env={'HEARTING_COMPUTE_RUN_ID': 'fixture-run'})
        bridge = self.process('att-owner')
        self.resource(bridge)
        bridge.terminate(); bridge.wait(timeout=5)
        # Legacy cleanup selection remains unchanged, but continuation still sees the positive.
        resources = CLOSE.linked_resources(self.route, self.path, self.jobs, {meta['attempt_id']})
        self.assertEqual(CLOSE._agent_processes(meta, resources), ([], True))
        self.assertEqual(CLOSE.owner_continuation_processes(meta, self.jobs)[0].state, 'live')
        self.assertIsNone(payload.poll())

    def test_live_owner_foreign_resource_namespace_and_incomplete_receipt_refuse(self):
        owner = self.process('att-owner')
        meta = self.row('att-owner', process=owner, status='done', note='dead-runtime-exit', failure_class='runtime')
        payload = self.process('att-owner'); registry, _ = self.resource(payload)
        self.assertEqual(CLOSE.owner_continuation_processes(meta, self.jobs)[0].state, 'live')
        owner.terminate(); owner.wait(timeout=5)
        namespace = os.readlink('/proc/self/ns/pid')
        altered = {**meta, 'pid_scope': 'namespace-local', 'pid_observer_ns': namespace}
        proof, _ = CLOSE.owner_continuation_processes(altered, self.jobs)
        self.assertEqual((proof.state, proof.reason), ('unverifiable', 'post-exit-receipt-incomplete'))
        data = json.loads(registry.read_text()); data['runs']['fixture-run']['pid_namespace'] = 'pid:[foreign]'
        registry.write_text(json.dumps(data))
        self.assertEqual(CLOSE.owner_continuation_processes(meta, self.jobs)[0].state, 'unverifiable')
        with mock.patch.object(DC, 'process_observation', return_value=('inaccessible', '', '')):
            self.assertEqual(CLOSE.owner_continuation_processes(meta, self.jobs)[0].state, 'unverifiable')
        self.assertIsNone(payload.poll())


class PreservedTagObservationTest(unittest.TestCase):
    def test_cached_and_single_scan_keep_denial_namespace_errors_and_new_birth(self):
        with __import__('tempfile').TemporaryDirectory() as directory:
            root = Path(directory)
            entries = []
            for pid, birth in ((123, '200'), (124, '201')):
                entry = root / str(pid); entry.mkdir(); entries.append(entry)
                (entry/'stat').write_text(f'{pid} (fixture) '+' '.join(['S', '1', str(pid)]+['0']*16+[birth]))
                (entry/'environ').write_bytes(b'AGENT_DISPATCH_ATTEMPT_ID=att-fixture\0')
            original_bytes = Path.read_bytes
            def denied(path):
                if path == entries[1]/'environ': raise PermissionError(13, 'denied')
                return original_bytes(path)
            meta = {'attempt_id': 'att-fixture', 'pid_start': '100'}
            preserved = {(123, '200')}
            with mock.patch.object(Path, 'iterdir', side_effect=lambda: iter(entries)), \
                 mock.patch.object(Path, 'read_bytes', denied), \
                 mock.patch.object(DC, '_tag_access_observation', return_value=('201', 'same-uid-unobservable')), \
                 mock.patch.object(DC, 'attempt_scan_namespace_authority', return_value=True):
                scan = DC.scan_process_table()
                self.assertEqual(DC.attempt_tagged_descendants(meta).state, 'populated')
                for cached in (False, True):
                    with self.subTest(cached=cached):
                        token = DC._PROCESS_TABLE_SCAN.set(scan if cached else None)
                        try:
                            result = DC.attempt_tagged_descendants(meta, preserved=preserved)
                            self.assertEqual((result.state, result.reason), ('unverifiable', 'same-uid-unobservable'))
                            # Excluding an old birth never excludes this PID's current identity.
                            self.assertEqual(DC.attempt_tagged_descendants(meta, preserved={(123, '199')}).state, 'populated')
                        finally: DC._PROCESS_TABLE_SCAN.reset(token)
            for reason, authority in (('malformed', True), ('', False)):
                scan = DC.ProcessTableScan({'att-fixture': ((123,'200','S'),)}, {}, incomplete_reason=reason)
                with mock.patch.object(DC, 'attempt_scan_namespace_authority', return_value=authority):
                    result = DC._tagged_descendants_from_scan(scan, meta, 'att-fixture', preserved=preserved)
                    self.assertEqual(result.state, 'unverifiable')


if __name__ == '__main__':
    unittest.main()
