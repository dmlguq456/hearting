#!/usr/bin/env python3
"""Storage failure recovery through the ordinary owner join and correction."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import dispatch_contract as D
import dispatch_completion_join as J
import dispatch_owner_input as I
import dispatch_replacement as R
import dispatch_supervisor_terminal as T
from split_module_fixture import patch_split


class DeadOwnerOSErrorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / 'jobs.log'
        self.attempt = 'att-storage-owner'
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                   start_new_session=True)
        identity = D.process_launch_identity(process.pid)
        process.terminate()
        process.wait(timeout=5)
        # Keep unrelated host process permissions out of this storage-failure
        # fixture. The leader/group identity remains a real exited process.
        tags = patch_split(D, 'attempt_tagged_descendants',
                                 return_value=D.ProcessGroupObservation('empty'))
        tags.start()
        self.addCleanup(tags.stop)
        self.log = self.root / 'owner.jsonl'
        self.route = self.root / 'route.json'
        self.route.write_text('{"route_id":"rt-preserved"}\n')
        self.marker = self.root / 'review.complete.json'
        self.marker.write_text('{"verdict":"PASS","conflicts":0}\n')
        self.meta = {
            'attempt_schema_version': '2', 'dispatch_depth': '1', 'transport': 'headless',
            'execution_surface': 'registered-headless', 'registered_worker': '1',
            'fallback_hop': 'same-harness-headless', 'worker_type': 'owner', 'harness': 'codex',
            'completion_delivery': 'app-server-supervised', 'supervisor_lease': 'flock-v1',
            'supervisor_lease_file': str(D.supervisor_lease_path(self.jobs, self.attempt)),
            'supervisor_lease_nonce': 'd' * 64, 'attempt_id': self.attempt,
            'owner_route_file': str(self.route), 'owner_route_id': 'rt-preserved',
            'owner_route_hash': 'sha256:' + 'a' * 64, 'log_file': str(self.log),
            'launch_claimed': '1', 'launch_started': '1', 'pgid': str(process.pid), **identity,
        }
        self.write_row()
        # The input consumer bound a native session before its supervisor died.
        I.OwnerInput(self.jobs, self.attempt, 'native-session', 'codex-active-turn', lambda _: None)
        self.terminal = T.classify_supervisor_error('codex', 'terminal-reconcile-failed-OSError')
        with patch_split(D, '_atomic_registry_replace', side_effect=OSError(errno.ENOSPC, 'disk full')):
            with self.assertRaises(OSError):
                T.reconcile_supervisor_terminal(self.jobs, self.attempt, self.terminal)
        self.log.write_text(json.dumps({'type': 'dispatch.supervisor.turn-completed'}) + '\n' +
                            json.dumps({'type': 'dispatch.supervisor.error',
                                        'reason': 'terminal-reconcile-failed-OSError'}) + '\n')

    def write_row(self):
        metadata = ','.join(f'{key}={value}' for key, value in self.meta.items())
        self.jobs.write_text(f'2026-10-10T00:00:00Z\topen\t{self.root}\t{self.root}\towner\t{metadata}\n')

    def test_join_retries_the_failed_terminal_writer_for_every_harness(self):
        for harness in ('codex', 'claude', 'opencode'):
            with self.subTest(harness=harness):
                self.meta['harness'] = harness
                self.meta['completion_delivery'] = ('app-server-supervised' if harness == 'codex'
                                                    else 'session-resume-supervised')
                self.write_row()
                receipt = J.join_selected_attempts(jobs=self.jobs, expected_attempts={self.attempt})
                row = J.exact_attempt_row(self.jobs, self.attempt)
                self.assertEqual(row.status, 'done', receipt)
                self.assertEqual(row.metadata['note'], 'dead-runtime-exit')
                self.assertEqual(row.metadata['reconcile_reason'], 'terminal-reconcile-failed-OSError')
                self.assertEqual(receipt['state'], 'ready', receipt)
                before = self.jobs.read_bytes()
                J.join_selected_attempts(jobs=self.jobs, expected_attempts={self.attempt})
                # Cleanup may publish its existing receipt once, never rewrite
                # the result or replay the assignment.
                self.assertEqual(J.exact_attempt_row(self.jobs, self.attempt).metadata['note'],
                                 'dead-runtime-exit')
                settled = self.jobs.read_bytes()
                J.join_selected_attempts(jobs=self.jobs, expected_attempts={self.attempt})
                self.assertEqual(self.jobs.read_bytes(), settled)
                self.assertEqual(self.marker.read_text(), '{"verdict":"PASS","conflicts":0}\n')

    def test_correction_closes_dead_owner_and_pins_the_remaining_work(self):
        answer = '이미 통과한 검토를 보존하고 남은 관찰 5개와 verdict만 완료하세요.'
        before_route = self.route.read_bytes()
        response = I.submit(self.jobs, self.attempt, answer)
        self.assertTrue(response['retained'], response)
        row = J.exact_attempt_row(self.jobs, self.attempt)
        self.assertEqual(row.status, 'done')
        self.assertEqual(R.death_kind(row.raw.split('\t'), row.metadata, jobs=self.jobs), R.CORRECTED)
        proof = R.death_proof(row.raw.split('\t'), row.metadata, jobs=self.jobs)
        self.assertIn(answer, R._correction_context(self.jobs, self.attempt, proof))
        self.assertEqual(row.metadata['owner_route_id'], 'rt-preserved')
        self.assertEqual(self.route.read_bytes(), before_route)

    def test_unsupervised_owner_keeps_its_existing_completion_path(self):
        self.meta.pop('supervisor_lease')
        self.meta.pop('supervisor_lease_file')
        self.meta['completion_delivery'] = 'poll-fallback'
        self.write_row()
        before = self.jobs.read_bytes()
        self.assertIsNone(J.settle_exited_owner_terminal(
            J.exact_attempt_row(self.jobs, self.attempt), jobs=self.jobs))
        import dispatch_batch_obligations as obligations
        with mock.patch.object(J, 'settle_finished_attempt') as settle:
            obligations.ensure_observers(self.jobs)
            settle.assert_not_called()
        self.assertEqual(self.jobs.read_bytes(), before)

    def test_unobservable_namespace_or_live_descendant_keeps_owner_open(self):
        for observation in (D.ProcessQuiescence('unverifiable', 'process-namespace-unverifiable'),
                            D.ProcessQuiescence('live', 'attempt-descendant-live')):
            with self.subTest(observation=observation):
                before = self.jobs.read_bytes()
                with patch_split(D, 'attempt_process_quiescence', return_value=observation), \
                     patch_split(J, 'attempt_process_quiescence', return_value=observation):
                    result = J.settle_finished_attempt(self.jobs, J.exact_attempt_row(self.jobs, self.attempt))
                    self.assertFalse(result['closed'], result)
                self.assertEqual(self.jobs.read_bytes(), before)

    def test_denied_environment_reobserved_as_zombie_is_no_live_descendant(self):
        entry = self.root / '99'
        entry.mkdir()
        tail = ['Z', '1'] + ['0'] * 18
        tail[19] = '100'
        (entry / 'stat').write_text('99 (exited) ' + ' '.join(tail))
        self.assertEqual(D._tag_access_observation(entry, '100'), ('', ''))
        tail[0] = 'S'
        (entry / 'stat').write_text('99 (private) ' + ' '.join(tail))
        self.assertEqual(D._tag_access_observation(entry, '100'),
                         ('100', 'procfs-environ:99:same-uid-unobservable'))

    def test_normal_reconnection_retries_storage_failure_without_a_model_launch(self):
        import dispatch_batch_obligations as obligations
        with patch_split(D, '_atomic_registry_replace', side_effect=OSError(errno.ENOSPC, 'full')), \
             mock.patch.object(obligations.subprocess, 'Popen') as launch:
            self.assertEqual(obligations.ensure_observers(self.jobs), 0)
            self.assertEqual(J.exact_attempt_row(self.jobs, self.attempt).status, 'open')
            launch.assert_not_called()
        with mock.patch.object(obligations.subprocess, 'Popen') as launch:
            self.assertEqual(obligations.ensure_observers(self.jobs), 0)
            self.assertEqual(J.exact_attempt_row(self.jobs, self.attempt).status, 'done')
            before = self.jobs.read_bytes()
            obligations.ensure_observers(self.jobs)
            self.assertEqual(self.jobs.read_bytes(), before)
            launch.assert_not_called()

    def test_storage_error_preserves_an_already_written_native_pass(self):
        artifact = self.root / 'report.md'
        artifact.write_text('완료\n')
        text = f'artifact: {artifact}\nverdict: PASS\nblocker: none'
        for harness, native in (
            ('codex', [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}},
                       {'type': 'dispatch.supervisor.turn.completed', 'status': 'completed'}]),
            ('claude', []),
            ('opencode', []),
        ):
            with self.subTest(harness=harness):
                self.meta['harness'] = harness
                self.meta['completion_delivery'] = ('app-server-supervised' if harness == 'codex'
                                                    else 'session-resume-supervised')
                self.write_row()
                terminal = (T.classify_codex_result(text) if harness == 'codex' else
                            T.classify_session_result({'is_error': False, 'result': text},
                                                      0, runtime=harness))
                with patch_split(D, '_atomic_registry_replace',
                                       side_effect=OSError(errno.ENOSPC, 'full')):
                    with self.assertRaises(OSError):
                        T.reconcile_supervisor_terminal(self.jobs, self.attempt, terminal,
                                                        emit=native.append)
                native.append({'type': 'dispatch.supervisor.error',
                               'reason': 'terminal-reconcile-failed-OSError'})
                native.append({'type': 'dispatch.supervisor.error',
                               'reason': 'supervisor-finalize-state-OSError'})
                self.log.write_text('\n'.join(json.dumps(row) for row in native) + '\n')
                self.assertEqual(T.classify_supervisor_log(self.log, harness), terminal)
                import dispatch_batch_obligations as obligations
                obligations.ensure_observers(self.jobs)
                row = J.exact_attempt_row(self.jobs, self.attempt)
                self.assertEqual((row.status, row.metadata.get('failure_class')), ('done', 'pass'))

    def test_old_codex_turn_completion_does_not_claim_owner_settlement(self):
        text = f'artifact: {self.marker}\nverdict: PASS\nblocker: none'
        rows = [{'type': 'dispatch.supervisor.turn.started', 'turn_id': 'current'},
                {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}},
                {'type': 'dispatch.supervisor.turn.completed', 'status': 'completed'},
                {'type': 'dispatch.supervisor.error', 'reason': 'terminal-reconcile-failed-OSError'},
                {'type': 'dispatch.supervisor.error', 'reason': 'supervisor-finalize-lease-OSError'}]
        for tail in ([], rows[3:], [rows[4]]):
            for status in ('completed', 'interrupted'):
                with self.subTest(tail=tail, status=status):
                    rows[2]['status'] = status
                    self.log.write_text('\n'.join(json.dumps(row) for row in rows[:3] + tail) + '\n')
                    self.assertEqual(T.classify_supervisor_log(self.log, 'codex').failure_class, 'runtime')
        # The old late owner envelope remains authoritative after settlement.
        rows[2]['status'] = 'completed'
        rows = rows[:3] + [{'type': 'turn.completed'}]
        self.log.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
        self.assertEqual(T.classify_supervisor_log(self.log, 'codex').failure_class, 'pass')
        self.log.write_text('\n'.join(json.dumps(row) for row in rows[:3]) + '\n')
        answer = '기존 완료 기록을 보존하고 남은 작업만 이어 가세요.'
        response = I.submit(self.jobs, self.attempt, answer)
        self.assertTrue(response['retained'], response)
        row = J.exact_attempt_row(self.jobs, self.attempt)
        self.assertEqual((row.status, row.metadata['note']), ('done', 'dead-runtime-exit'))
        self.assertEqual(R.death_kind(row.raw.split('\t'), row.metadata, jobs=self.jobs), R.CORRECTED)

    def test_previous_turn_terminal_does_not_finish_current_failed_turn(self):
        text = f'artifact: {self.marker}\nverdict: PASS\nblocker: none'
        for harness, prior in (
            ('codex', [{'type': 'dispatch.supervisor.turn.failed',
                        'codex_error_info': 'usageLimitExceeded'}]),
            ('claude', [{'type': 'result', 'is_error': False, 'result': text}]),
            ('opencode', [{'type': 'text', 'part': {'text': text}},
                         {'type': 'step_finish', 'part': {'reason': 'stop'}}]),
        ):
            with self.subTest(harness=harness):
                start = ('dispatch.supervisor.turn.started' if harness == 'codex'
                         else 'dispatch.supervisor.turn-started')
                rows = prior + [{'type': start}, {'type': 'dispatch.supervisor.error',
                                                'reason': 'terminal-reconcile-failed-OSError'}]
                self.log.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
                self.assertEqual(T.classify_supervisor_log(self.log, harness).reconcile_reason,
                                 'terminal-reconcile-failed-OSError')

    def test_reconnection_writes_only_the_canonical_registry(self):
        import dispatch_batch_obligations as obligations
        legacy = self.root / 'legacy'
        legacy.mkdir()
        legacy_jobs = legacy / 'jobs.log'
        legacy_jobs.write_bytes(self.jobs.read_bytes())
        before = legacy_jobs.read_bytes()
        with patch_split(D, 'dispatch_state_roots', return_value=(self.root, legacy)), \
             mock.patch.object(obligations.subprocess, 'Popen') as launch:
            self.assertEqual(obligations.ensure_observers(), 0)
            launch.assert_not_called()
        self.assertEqual(J.exact_attempt_row(self.jobs, self.attempt).status, 'done')
        self.assertEqual(legacy_jobs.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
