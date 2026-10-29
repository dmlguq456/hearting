#!/usr/bin/env python3
"""Public finish reconciles a settled replacement's original resource (CPU only)."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

import artifact_producer as P
import dispatch_contract as DC
import dispatch_replacement as DR
import dispatch_resource_wait as WAIT
import dispatch_terminal_commit as T
import review_source_subject

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    'finish_producer_fixture', HERE / 'artifact_producer.test.py')
FIX = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIX)
R = FIX.R


class ResourceTerminalFinishTest(unittest.TestCase):
    FULL = 'autopilot-lab-full-run'
    HOST = 'autopilot-lab-host-run'
    ORIGINAL = 'att-finish-original'

    def setUp(self):
        self.fixture = FIX.TerminalTransactionIntegrationTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.activate()
        self.base = Path(self.fixture._tmp.name)
        self.root = self.fixture.root
        self.jobs = self.fixture.jobs
        self.worktree = self.base / 'source'
        self.worktree.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.worktree)], check=True)
        (self.worktree / 'app.py').write_text('print("CPU fixture")\n')
        subprocess.run(['git', '-C', str(self.worktree), 'add', 'app.py'], check=True)
        subprocess.run(['git', '-C', str(self.worktree), '-c', 'user.name=Fixture',
            '-c', 'user.email=fixture@example.com', 'commit', '-qm', 'CPU fixture'], check=True)
        environment = mock.patch.dict(os.environ, {
            'AGENT_WORKFLOW_ROOT': str(self.base / 'workflow'),
            'AGENT_ARTIFACT_CHECKPOINT': 'off'})
        environment.start()
        self.addCleanup(environment.stop)

    def _write_rows(self, rows):
        self.jobs.write_text(''.join(
            '\t'.join(['2026-10-28T00:00:00Z', status, str(self.worktree),
                       str(self.worktree), meta['attempt_id'],
                       ','.join(f'{key}={value}' for key, value in meta.items())]) + '\n'
            for status, meta in rows))

    def _owner_identity(self, *, live=False):
        process = subprocess.Popen([sys.executable, '-B', '-c', 'import sys; sys.stdin.read()'],
            stdin=subprocess.PIPE, text=True, start_new_session=True)
        def retire():
            if process.poll() is None:
                process.communicate('', timeout=5)
            else:
                process.wait(timeout=5)
                process.stdin.close()
        self.addCleanup(retire)
        identity = DC.process_launch_identity(process.pid)
        self.assertIn('pid_start', identity)
        self.assertEqual(identity['pgid'], str(process.pid))
        if not live:
            retire()
            self.assertEqual(process.returncode, 0)
        return identity

    def _resource(self, node, owner, *, pid):
        directory = self.base / node
        directory.mkdir()
        sentinel = directory / 'exit'
        sentinel.write_text('0\n')
        row = dict(run_id=node + '-run', pid=pid, starttime='1', process_group=pid,
            command_hash='0' * 64, command=['true'], cwd=str(self.base),
            log=str(directory / 'log'), sentinel=str(sentinel), status='running',
            route=str(self.path), node=node, jobs=str(self.jobs),
            resource_policy='supervised-owner', parent_attempt_id=owner,
            owner_wait=dict(parent_attempt_id=owner, session_id=owner + '-native',
                owner_pid=int(self.identities[owner]['pid']),
                owner_start=self.identities[owner]['pid_start'],
                jobs=str(self.jobs), route_id=self.route['route_id'],
                route_hash=self.route['route_hash']))
        registry = directory / 'resource-runs.json'
        registry.write_text(json.dumps({'schema_version': 1, 'runs': {row['run_id']: row}}))
        with contextlib.redirect_stdout(io.StringIO()):
            self.sup.main(['arm', '--route', str(self.path), '--node', node,
                '--predecessor-kind', 'resource', '--predecessor-id', row['run_id'],
                '--resource-registry', str(registry), '--jobs', str(self.jobs),
                '--artifact-base', str(self.output), '--successor-external'])
        observation = next(item for item in self.sup.poll_once(self.route, self.ledger)
                           if item['node'] == node)
        self.assertEqual(observation['action'], 'wait-next-resource', observation)
        return registry

    def _prepare(self, *, live_original=False):
        original_identity = self._owner_identity(live=live_original)
        self.identities = {self.ORIGINAL: original_identity}
        dispatch = FIX.dispatch_evidence()
        for row in dispatch['tuples']:
            row['checked_worktree'] = str(self.worktree)
            row['parent_sandbox'] = 'danger-full-access'
        self.route = R.compose_route(capability='autopilot-code', capability_mode='dev',
            shape='staged', graph='plan,execute,impl-review,autopilot-lab:smoke,'
                'autopilot-lab:full-run,autopilot-lab:host-run,test,report',
            slug='settled-resource-finish', cwd=self.worktree, artifact_root=self.root,
            intensity='standard', parent_harness='codex',
            dispatch_evidence=dispatch, unassigned=True)
        self.path = Path(FIX.L.admit_runtime_route(self.root, self.route).route_file)
        original = dict(attempt_schema_version='2', attempt_id=self.ORIGINAL,
            worker_type='owner', unit='_kernel/owner', dispatch_depth='1',
            registered_worker='1', harness='codex', transport='headless',
            execution_surface='registered-headless', fallback_hop='same-harness-headless',
            workflow_completion='runtime-v1', launch_started='1', launch_claimed='1',
            launch_outcome='reaped-before-publish', parent_sid='fixture-parent',
            owner_route_file=str(self.path), owner_route_id=self.route['route_id'],
            owner_route_hash=self.route['route_hash'], **original_identity)
        self._write_rows([('open', original)])
        self.cycle = P.begin(self.root, route_file=self.path, capability='autopilot-code',
            intensity='standard', jobs=self.jobs, owner_attempt_id=self.ORIGINAL)
        self.output = Path(self.cycle['cycle_dir']) / 'artifacts'
        self.sup = WAIT.supervisor()
        self.ledger = self.sup.ledger_for(self.route, self.jobs)
        self.registry = self._resource(self.FULL, self.ORIGINAL, pid=999999997)

        # The immutable replacement claim and actual launch annotation are the
        # same producer shape as registered replacements; no authority mocks.
        logical = DR._logical_key(self.route, original)
        family = DR._digest(logical)
        self.CONSUMER = 'att-' + hashlib.sha256(('replacement:' + family).encode()).hexdigest()[:48]
        self.identities[self.CONSUMER] = self._owner_identity()
        claim = dict(schema=DR.SCHEMA, family_id=family, logical_node=logical,
            original_attempt_id=self.ORIGINAL, replacement_attempt_id=self.CONSUMER, ordinal=1)
        DR._once(DR._record_path(self.jobs, family), claim)
        DR._reserve_source(self.jobs, self.ORIGINAL, family)
        original.update(replacement_family_id=family, replacement_claim_digest=DR._digest(claim),
            replacement_attempt_id=self.CONSUMER, replacement_ordinal='1',
            note='dead-worker-blocked', failure_class='contract')
        consumer = dict(original, attempt_id=self.CONSUMER,
            replacement_original_attempt_id=self.ORIGINAL, automatic_retry_of=self.ORIGINAL,
            note='completed-supervisor', failure_class='pass',
            **self.identities[self.CONSUMER])
        consumer.pop('replacement_attempt_id')
        self._write_rows([('done', original), ('open', consumer)])
        P.begin(self.root, route_file=self.path, capability='autopilot-code',
            intensity='standard', jobs=self.jobs, owner_attempt_id=self.CONSUMER,
            require_cycle=True, resume_only=True)
        self.host_registry = self._resource(self.HOST, self.CONSUMER, pid=999999996)
        # The host stage was already accepted by the replacement; full-run's
        # earlier wait is deliberately left untouched, as in the reported case.
        host_arm = self.sup.read_armed(self.ledger)[self.HOST]
        host_evidence = self.sup.resource_evidence(host_arm)
        host_evidence.update(owner_attempt_id=self.CONSUMER,
            owner_terminal_settlement=True, runtime_only_output=True,
            resource_execution_identity=host_evidence['identity'])
        self.ledger.record(self.HOST, 'STAGE_SUCCEEDED', evidence=host_evidence,
                           actor='owner-successor-start')

        self.report = self.fixture.write_output(self.cycle,
            rel='plans/fixture/final_report.md', data=b'verified CPU finish fixture\n')
        children = []
        for node_id in ('test', 'report'):
            node = next(item for item in self.route['nodes'] if item['id'] == node_id)
            meta = dict(attempt_schema_version='2', attempt_id='att-finish-' + node_id,
                parent_attempt_id=self.CONSUMER, dispatch_depth='2', transport='headless',
                execution_surface='registered-headless', registered_worker='1',
                fallback_hop='same-harness-headless', harness='codex',
                route_id=self.route['route_id'], route_hash=self.route['route_hash'],
                route_node=node_id, failure_class='pass', note='completed-marker',
                launch_outcome='reaped-before-publish')
            meta.update(review_source_subject.seal_attempt(
                self.jobs, self.route, node, meta['attempt_id'], worktree=self.worktree))
            children.append(('open', meta))
            self._write_rows([('done', original), ('open', consumer), *children])
            R.complete_node(self.route, node, node_id, self.report,
                            attempt_id=meta['attempt_id'], jobs=self.jobs)
            meta['completion_marker'] = str(R.completion_dir(
                self.route['route_id'], jobs=self.jobs) / (node_id + '.json'))
            self.ledger.record(node_id, 'STAGE_SUCCEEDED',
                evidence={'completion_marker': meta['completion_marker']}, actor='fixture')
            children[-1] = ('done', meta)
        log = self.base / 'owner.jsonl'
        text = f'artifact: {self.report}\nverdict: PASS\nblocker: none'
        log.write_text(json.dumps({'type': 'item.completed', 'item': {
            'type': 'agent_message', 'text': text}}) + '\n' +
            json.dumps({'type': 'turn.completed'}) + '\n')
        consumer['log_file'] = str(log)
        self.rows = [('done', original), ('done', consumer), *children]
        self._write_rows(self.rows)
        for state in ('STAGE_SUCCEEDED', 'NEXT_REGISTERED'):
            self.ledger.set_workflow_state(state, evidence={'node': 'report'}, actor='fixture')
        self.request = T.TerminalCommitRequest(self.path, self.CONSUMER, self.jobs, self.root)
        stage = self.ledger.state()['nodes'][self.FULL]
        self.assertEqual(stage['state'], 'RUNNING')
        self.assertTrue(stage['evidence']['succeeded'])
        self.assertTrue(stage['evidence']['awaiting_next_resource'])
        self.assertTrue(stage['evidence']['runtime_only_output'])
        self.assertTrue(R.terminal_gate_observation(
            self.route, jobs=self.jobs, exact_terminal=True)['report']['passed'])
        self.assertEqual(DR.effective_attempts(self.jobs, {self.ORIGINAL})[0], {self.CONSUMER})
        self.assertEqual([status for status, _ in self.rows], ['done'] * len(self.rows))
        self.assertEqual(original['replacement_ordinal'], consumer['replacement_ordinal'])
        self.assertEqual(consumer['replacement_ordinal'], '1')
        self.assertEqual(original['replacement_family_id'], consumer['replacement_family_id'])
        self.assertEqual(original['replacement_claim_digest'], consumer['replacement_claim_digest'])
        self.assertEqual(consumer['replacement_original_attempt_id'], self.ORIGINAL)
        self.resource_before = self.registry.read_bytes()

    def _finish(self):
        result = subprocess.run([sys.executable, '-B', str(HERE / 'dispatch_terminal_commit.py'),
            'finish', '--jobs', str(self.jobs), '--attempt', self.CONSUMER],
            cwd=self.base, env=os.environ.copy(), capture_output=True, text=True, timeout=30)
        receipt = json.loads(result.stdout)
        return result, receipt

    def test_finish_settles_original_resource_after_both_owners_are_done(self):
        self._prepare()
        jobs_before = self.jobs.read_bytes()
        host_before = self.host_registry.read_bytes()
        report_before = self.report.read_bytes()
        result, receipt = self._finish()
        self.assertEqual((result.returncode, receipt['result']), (0, 'completed'),
                         (receipt, result.stderr))
        stage = self.ledger.state()['nodes'][self.FULL]
        self.assertEqual(stage['state'], 'STAGE_SUCCEEDED')
        self.assertTrue(stage['evidence']['owner_terminal_settlement'])
        self.assertEqual(stage['evidence']['owner_attempt_id'], self.CONSUMER)
        self.assertNotIn('awaiting_next_resource', stage['evidence'])
        self.assertEqual(self.ledger.state()['workflow_state'], 'COMPLETE')
        self.assertEqual(json.loads(T._commit_state_path(self.request).read_text())['state'],
                         'owner-envelope-sealed')
        self.assertEqual(P.read_cycle_record(self.root, self.cycle['cycle_id'])['state'], 'sealed')
        full_marker = R.completion_dir(self.route['route_id'], jobs=self.jobs) / (self.FULL + '.json')
        self.assertEqual(json.loads(full_marker.read_text())['resource_execution_identity'],
                         stage['evidence']['resource_execution_identity'])
        settled = self.ledger.journal_path.read_bytes()
        marker_before = full_marker.read_bytes()
        replay, replay_receipt = self._finish()
        self.assertEqual((replay.returncode, replay_receipt['result']), (0, 'completed'),
                         (replay_receipt, replay.stderr))
        self.assertEqual(self.ledger.journal_path.read_bytes(), settled)
        self.assertEqual(full_marker.read_bytes(), marker_before)
        self.assertEqual(self.registry.read_bytes(), self.resource_before)
        self.assertEqual(self.host_registry.read_bytes(), host_before)
        self.assertEqual(self.jobs.read_bytes(), jobs_before)
        self.assertEqual(self.report.read_bytes(), report_before)
        resource = json.loads(self.registry.read_text())['runs'][self.FULL + '-run']
        self.assertEqual(resource['parent_attempt_id'], self.ORIGINAL)
        self.assertEqual(resource['owner_wait']['parent_attempt_id'], self.ORIGINAL)

    def _denied_finish(self, mutation):
        self._prepare(live_original=mutation == 'live-original')
        status, source = self.rows[0]
        _status, consumer = self.rows[1]
        if mutation == 'foreign-family':
            consumer['replacement_family_id'] = 'f' * 64
        elif mutation == 'unlaunched':
            consumer['launch_started'] = '0'
        else:
            self.rows[0] = ('running', source)
        self._write_rows(self.rows)
        before = (self.ledger.state_path.read_bytes(), self.ledger.journal_path.read_bytes(),
                  self.jobs.read_bytes(), self.host_registry.read_bytes(), self.ledger.claims())
        result, receipt = self._finish()
        self.assertEqual((result.returncode, receipt['result'], receipt['reason']),
                         (70, 'recoverable', 'recovery-unavailable'), (receipt, result.stderr))
        self.assertEqual(receipt['detail'], 'resource-stage-output-pending:' + self.FULL)
        self.assertEqual((self.ledger.state_path.read_bytes(), self.ledger.journal_path.read_bytes(),
            self.jobs.read_bytes(), self.host_registry.read_bytes(), self.ledger.claims()), before)
        self.assertEqual(self.registry.read_bytes(), self.resource_before)
        self.assertFalse((R.completion_dir(self.route['route_id'], jobs=self.jobs) /
                          (self.FULL + '.json')).exists())
        self.assertFalse(T._commit_state_path(self.request).exists())
        self.assertEqual(P.read_cycle_record(self.root, self.cycle['cycle_id'])['state'], 'open')

    def test_finish_refuses_foreign_replacement_family_without_stage_mutation(self):
        self._denied_finish('foreign-family')

    def test_finish_refuses_unlaunched_consumer_without_stage_mutation(self):
        self._denied_finish('unlaunched')

    def test_finish_refuses_live_original_owner_without_stage_mutation(self):
        self._denied_finish('live-original')


if __name__ == '__main__':
    unittest.main()
