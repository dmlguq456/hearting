#!/usr/bin/env python3
"""No-model regressions for SD-157's sealed one-leg replay and round identity."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch_replacement as R
import dispatch_replacement_batch as RB
import review_round_cap as ROUND


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIXTURE = load('_replacement_batch_fixture', 'dispatch-batch.test.py')
BATCH = FIXTURE.BATCH
GOVERNOR = load('_replacement_batch_governor', 'model-worker-governor.py')


class ReplacementBatchTest(unittest.TestCase):
    legs = FIXTURE.DispatchBatchTest.legs
    argv = FIXTURE.DispatchBatchTest.argv
    common_patches = FIXTURE.DispatchBatchTest.common_patches
    write_existing = FIXTURE.DispatchBatchTest.write_existing
    partial_continuation = FIXTURE.DispatchBatchTest.partial_continuation

    def setUp(self):
        FIXTURE.DispatchBatchTest.setUp(self)
        self.route['parallel_groups'] = [{'id': 'plan', 'width': 2, 'join_policy': 'all',
                                         'members': ['plan', 'plan-replica']}]
        self.route_path.write_text(json.dumps(self.route))
        self.peer, self.gap = self.legs()
        self.manifest, _continuation, self.partial = self.partial_continuation(self.legs())
        self.write_existing(self.peer, status='done', note='completed-marker')
        self.write_existing(self.gap, status='done', note='dead-exact-pid', live_identity=False)
        self.fields, self.source = R._rows(self.jobs.read_text().splitlines())[self.gap['attempt_id']]
        self.options = SimpleNamespace(route=self.route_path, parallel_group='plan',
            slug_prefix='fixture', parent='owner', prompt_text=BATCH.DEFAULT_PROMPT,
            qa=None, log_dir=None, allow_degraded_independence=False,
            subdivision_manifest=None, continuation=None)
        RB.seal_launch_input(self.jobs, self.options, self.route, self.manifest,
                             self.source['batch_manifest_sha256'])
        logical = {'root_route_id': self.route['route_id'], 'node': self.gap['node']}
        family = R._digest(logical)
        self.record = {'schema': R.SCHEMA, 'family_id': family, 'logical_node': logical,
            'original_attempt_id': self.gap['attempt_id'],
            'replacement_attempt_id': 'att-'+hashlib.sha256(('replacement:'+family).encode()).hexdigest()[:48],
            'ordinal': 1, 'route_file': str(self.route_path),
            'route_id': self.route['route_id'], 'route_hash': self.route['route_hash']}
        R._once(R._record_path(self.jobs, family), self.record)
        self.replay = {'task': BATCH.DEFAULT_PROMPT+'\nOriginal round protocol\n'}
        proof = mock.patch.object(R, 'validate_claim_source',
                                 return_value=(self.fields, self.source, self.replay))
        self.validate = proof.start()
        self.addCleanup(proof.stop)
        batch = mock.patch.object(RB, '_batch', return_value=BATCH)
        batch.start(); self.addCleanup(batch.stop)
        evidence = mock.patch.object(BATCH.ROUTE_MODULE, '_continuation_reused_evidence',
                                    return_value=(self.partial['realized_peer_set'][0], None))
        evidence.start(); self.addCleanup(evidence.stop)

    def evidence(self):
        command = RB.command(self.jobs, self.record, self.source, self.replay)
        path = Path(command[command.index('--continuation')+1])
        return command, path, json.loads(path.read_text())

    def test_concurrent_reentry_reuses_one_evidence_and_original_input(self):
        before = self.jobs.read_bytes()
        with ThreadPoolExecutor(max_workers=8) as pool:
            commands = list(pool.map(lambda _: RB.command(self.jobs, self.record, self.source, self.replay), range(16)))
        self.assertTrue(all(command == commands[0] for command in commands))
        self.assertEqual(commands[0][commands[0].index('--prompt-text')+1], BATCH.DEFAULT_PROMPT)
        self.assertEqual(self.jobs.read_bytes(), before)
        self.assertEqual(len(list((R._directory(self.jobs)/'batch-claims').glob('*.json'))), 1)

    def test_changed_manifest_or_input_refuses(self):
        path = RB._input_path(self.jobs, self.source['batch_manifest_sha256'])
        value = json.loads(path.read_text())
        value['manifest']['members'][0]['attempt_id'] = 'att-invented-peer'
        path.write_text(json.dumps(value))
        with self.assertRaises(R.DC.DispatchContractError):
            RB.command(self.jobs, self.record, self.source, self.replay)

    def test_pending_peer_does_not_create_replacement_evidence(self):
        with mock.patch.object(BATCH.ROUTE_MODULE, '_continuation_reused_evidence',
                               side_effect=ValueError('peer-still-live')):
            with self.assertRaisesRegex(R.DC.DispatchContractError, 'peer-still-live'):
                RB.command(self.jobs, self.record, self.source, self.replay)
        self.assertFalse(RB._evidence_path(self.jobs, self.record['family_id']).exists())

    def test_reentry_peer_drift_refuses(self):
        self.evidence()
        changed = dict(self.partial['realized_peer_set'][0], marker_digest='sha256:'+'7'*64)
        with mock.patch.object(BATCH.ROUTE_MODULE, '_continuation_reused_evidence', return_value=(changed, None)):
            with self.assertRaises(R.DC.DispatchContractError):
                RB.command(self.jobs, self.record, self.source, self.replay)

    def test_batch_launches_only_gap_with_common_claim_identity(self):
        config=self.base/'d5-defaults.yaml'
        baseline=(FIXTURE.ROOT/'profiles/dispatch-defaults.yaml').read_text()
        config.write_text(baseline.replace('  enabled: [claude, codex, opencode]','  enabled: [claude, opencode]'))
        env=mock.patch.dict(os.environ,{'DISPATCH_DEFAULTS_CONFIG':str(config)})
        env.start();self.addCleanup(env.stop)
        command, path, evidence = self.evidence()
        spawned = []
        class FakeProcess:
            returncode = 0
            pid = 10001
            def __init__(self, argv, **kwargs):
                self.command = argv
                spawned.append(argv)
            def communicate(self):
                return FIXTURE.success_receipt(self.command), ''
        stack, assignments = self.common_patches()
        with stack:
            stack.enter_context(mock.patch.object(BATCH, 'load_route', return_value=self.route))
            stack.enter_context(mock.patch.object(BATCH.DISPATCH_NODE, 'resolve_checked_tuple', side_effect=FIXTURE.resolve_side_effect))
            allocator = stack.enter_context(mock.patch.object(BATCH, 'assign_harnesses',
                side_effect=BATCH.BatchError('successful-peer-backend-now-unavailable')))
            stack.enter_context(mock.patch.object(BATCH, 'resolve_agent_home', return_value=self.base))
            stack.enter_context(mock.patch.object(BATCH, 'resolve_global_registry', return_value=SimpleNamespace(path=self.jobs)))
            stack.enter_context(mock.patch.object(BATCH, 'resolve_live_parent_attempt'))
            stack.enter_context(mock.patch.object(BATCH, 'completion_marker_gate'))
            stack.enter_context(mock.patch.object(BATCH.subprocess, 'check_output', return_value=str(self.base)))
            reserve = stack.enter_context(mock.patch.object(BATCH, 'reserve_batch', return_value=['b'*32]))
            stack.enter_context(mock.patch.object(BATCH, 'cancel_unclaimed'))
            stack.enter_context(mock.patch.object(BATCH.subprocess, 'Popen', side_effect=FakeProcess))
            stack.enter_context(mock.patch.dict(os.environ, {
                'AGENT_DISPATCH_SELF_SLUG': 'owner', 'AGENT_DISPATCH_ATTEMPT_ID': 'att-parent-fixture',
                'AGENT_DISPATCH_CURRENT_HARNESS': 'codex', 'AGENT_DISPATCH_CURRENT_TRANSPORT': 'headless',
                'AGENT_DISPATCH_CURRENT_SANDBOX': 'workspace-write'}))
            output = io.StringIO()
            config.write_text(baseline.replace('  enabled: [claude, codex, opencode]','  enabled: [opencode]'))
            diagnostic=io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostic):
                rc = BATCH.main(command[2:])
            self.assertEqual(diagnostic.getvalue().count('policy_note=disabled-since-admission'),1)
        self.assertEqual(rc, 0, output.getvalue())
        self.assertEqual(len(spawned), 1)
        allocator.assert_not_called()
        self.assertEqual(spawned[0][spawned[0].index('--attempt-id')+1], self.record['replacement_attempt_id'])
        self.assertEqual(spawned[0][spawned[0].index('--automatic-retry-of')+1], self.gap['attempt_id'])
        self.assertEqual(len(reserve.call_args.args[2]), 1)
        self.assertEqual(reserve.call_args.kwargs['source_manifest'], self.manifest)
        self.assertFalse(reserve.call_args.kwargs['replacement_seal']['retry_claim_reused'])
        self.assertIn('reused-successful-peer', output.getvalue())

    def test_failed_initial_reservation_label_does_not_override_registered_source(self):
        original_path = RB._input_path(self.jobs, self.source['batch_manifest_sha256'])
        original_path.unlink()
        self.options.slug_prefix = 'earlier-capacity-refused'
        RB.seal_launch_input(self.jobs, self.options, self.route, self.manifest,
                             self.source['batch_manifest_sha256'])
        # The actual source row still has the later successful caller's label.
        # Reuse the full one-leg test and inspect the immutable command at its
        # launching boundary, where R.admission compares --slug to raw input.
        observed = []
        # The same test checks every other batch invariant. Capture its local
        # fake constructor through the wrapper result, which sees its command.
        real_result = BATCH.wrapper_result
        def inspect(leg, process, stdout, stderr):
            observed.append(process.command)
            return real_result(leg, process, stdout, stderr)
        with mock.patch.object(BATCH, 'wrapper_result', side_effect=inspect):
            self.test_batch_launches_only_gap_with_common_claim_identity()
        self.assertEqual(len(observed), 1)
        command = observed[0]
        self.assertEqual(command[command.index('--slug')+1], self.fields[4])

    def test_source_death_revalidated_before_governor_reservation(self):
        _command, _path, evidence = self.evidence()
        partial = dict(evidence['partial_group_continuation'],
                       continuation_id=evidence['continuation_id'], automatic_replacement_evidence=evidence)
        _, digest, leg_digests = BATCH.build_manifest(
            parallel_group='plan', route_id=self.route['route_id'], parent_attempt_id='att-parent-fixture',
            independence='cross-harness', members=self.manifest['members'],
            required_independence_axes=self.manifest['required_independence_axes'],
            realized_independence_axes=self.manifest['realized_independence_axes'])
        with mock.patch.object(R, 'validate_claim_source', side_effect=R.DC.DispatchContractError('replacement-process-live')):
            with self.assertRaises(BATCH.BatchError):
                BATCH.prepare_partial_replacement(self.jobs, self.route, partial, self.manifest,
                                                  digest, leg_digests, apply=True)

    def test_governor_accepts_only_canonical_family_and_unchanged_successful_peer(self):
        _command, _path, evidence = self.evidence()
        partial = dict(evidence['partial_group_continuation'],
                       continuation_id=evidence['continuation_id'], automatic_replacement_evidence=evidence)
        source, digest, leg_digests = RB.verify_manifest(self.manifest)
        seal = BATCH.prepare_partial_replacement(self.jobs, self.route, partial, source,
                                                  digest, leg_digests, apply=True)
        members = [dict(member) for member in source['members']]
        members[1]['attempt_id'] = self.record['replacement_attempt_id']
        replacement, replacement_digest, _ = BATCH.build_manifest(
            parallel_group='plan', route_id=self.route['route_id'], parent_attempt_id='att-parent-fixture',
            independence='cross-harness', members=members,
            required_independence_axes=source['required_independence_axes'],
            realized_independence_axes=source['realized_independence_axes'])
        peers = [{'jobs': str(self.jobs)}]
        result = GOVERNOR._validate_partial_replacement(
            replacement, replacement_digest, source, evidence, seal, peers)
        self.assertEqual(result[3], self.gap['attempt_id'])
        changed = json.loads(json.dumps(replacement))
        changed['members'][0]['attempt_id'] = 'att-rerun-success'
        with self.assertRaisesRegex(ValueError, 'successful peer replacement forbidden'):
            GOVERNOR._validate_partial_replacement(changed, replacement_digest, source, evidence, seal, peers)
        with mock.patch.object(R, 'validate_claim_source', side_effect=R.DC.DispatchContractError('replacement-process-live')):
            with self.assertRaisesRegex(ValueError, 'replacement-process-live'):
                GOVERNOR._validate_partial_replacement(replacement, replacement_digest, source, evidence, seal, peers)

    def test_node_replays_original_round_task_without_recounting(self):
        node = BATCH.DISPATCH_NODE
        R._reserve_source(self.jobs, self.gap['attempt_id'], self.record['family_id'])
        args = SimpleNamespace(adapter_args=['--', '--automatic-retry-of', self.gap['attempt_id']],
                               attempt_id=self.record['replacement_attempt_id'],
                               adapter=self.source['harness'], parent='owner')
        self.assertEqual(node.replacement_task(args, self.route, self.route['nodes'][1], self.jobs), self.replay['task'])
        args.attempt_id = 'att-unclaimed'
        with self.assertRaises(R.DC.DispatchContractError):
            node.replacement_task(args, self.route, self.route['nodes'][1], self.jobs)

    def test_round_census_keeps_one_canonical_family_at_original_position(self):
        family = self.record['family_id']; digest = R._digest(self.record)
        R._reserve_source(self.jobs, self.gap['attempt_id'], family)
        original = dict(self.source, replacement_family_id=family,
                        replacement_claim_digest=digest, replacement_ordinal='1',
                        replacement_attempt_id=self.record['replacement_attempt_id'])
        replacement = dict(original, attempt_id=self.record['replacement_attempt_id'],
                           replacement_original_attempt_id=self.gap['attempt_id'],
                           automatic_retry_of=self.gap['attempt_id'], note='completed-marker')
        replacement.pop('replacement_attempt_id')
        later = (list(self.fields), dict(self.source, attempt_id='att-later'))
        rows = [(list(self.fields), original), later, (list(self.fields), replacement)]
        effective = ROUND.logical_round_records(rows, jobs=self.jobs)
        self.assertEqual([m['attempt_id'] for _, m in effective], [replacement['attempt_id'], 'att-later'])
        self.assertEqual(ROUND.logical_round_records(rows), rows)
        path = R._record_path(self.jobs, family)
        changed = dict(self.record, ordinal=2)
        path.write_text(json.dumps(changed))
        self.assertEqual(ROUND.logical_round_records(rows, jobs=self.jobs), rows)


if __name__ == '__main__':
    unittest.main()
