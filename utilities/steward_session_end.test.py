#!/usr/bin/env python3
"""Session closure preserves history and releases repository capacity."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('cap_end_fixture', Path(__file__).with_name('peer_steward_cap.test.py'))
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)

class SessionEndTest(f.GrantFixture):
    def test_end_and_resume_preserve_history_for_three_harnesses(self):
        for harness in ('claude', 'codex', 'opencode'):
            with self.subTest(harness=harness):
                sid = harness + '-owner'
                self.session(sid, self.primary, harness)
                f.peer_message.grant_steward(harness, sid, f.EXPLICIT_TO, 'explicit', f.TS,
                                             source='explicit', steward_seat=None)
                path = f.peer_message.steward_marker_path(harness, sid)
                original = json.loads(path.read_text())
                f.peer_message.observe_steward_end(harness, sid, reason='logout')
                self.assertTrue(path.exists())
                self.assertEqual(json.loads(path.read_text())['targets'], original['targets'])
                self.assertEqual(f.fleet_steward.session_liveness(harness, sid, agents=()), 'ended')
                self.assertFalse(f.fleet_steward.role_flag(harness, sid, sessions=self.rows))
                with mock.patch.object(f.peer_message, '_steward_process_evidence', return_value=None):
                    f.peer_message.observe_steward_resume(harness, sid)
                self.assertNotIn('session_end', json.loads(path.read_text()))
                self.assertTrue(f.fleet_steward.role_flag(harness, sid, sessions=self.rows))
                f.peer_message.observe_steward_end(harness, sid, reason='logout')

    def test_live_role_without_receipt_never_scans_processes(self):
        from fleet.collectors import procscan
        for harness in ('claude', 'codex', 'opencode'):
            with self.subTest(harness=harness):
                sid = harness + '-live'
                self.session(sid, self.primary, harness)
                f.peer_message.grant_steward(harness, sid, f.EXPLICIT_TO, 'explicit', f.TS,
                                             source='explicit', steward_seat=None)
                path = f.peer_message.steward_marker_path(harness, sid)
                marker = json.loads(path.read_text())
                marker['session_process'] = dict(f.peer_message._steward_process_context(),
                    pid=os.getpid(), pid_start=procscan.read_proc_start(os.getpid()))
                self.assertNotIn('session_end', marker)
                path.write_text(json.dumps(marker))
                with mock.patch.object(f.fleet_steward, '_peer_message_module', return_value=f.peer_message), \
                     mock.patch.object(f.peer_message, '_fleet_steward', return_value=f.fleet_steward), \
                     mock.patch.object(f.fleet_steward, 'other_session_process_live') as other, \
                     mock.patch.object(f.fleet_steward, '_process_proof') as census, \
                     mock.patch.object(procscan, '_visible_pids') as scan:
                    self.assertTrue(f.fleet_steward.role_flag(harness, sid, sessions=self.rows))
                self.assertEqual((other.call_count, census.call_count, scan.call_count), (0, 0, 0))
                f.peer_message.observe_steward_end(harness, sid, reason='logout')

    def test_exit_evidence_still_checks_other_holder_with_shared_observation(self):
        from fleet.collectors import procscan
        import dispatch_contract
        self.session('owner', self.primary)
        self.grant('owner')
        path = f.peer_message.steward_marker_path('claude', 'owner')
        marker = json.loads(path.read_text())
        process = dict(f.peer_message._steward_process_context(), pid=os.getpid(),
                       pid_start=procscan.read_proc_start(os.getpid()))
        observation = f.fleet_steward.end_observation()
        for kind in ('receipt', 'dead', 'boot'):
            for has_other in (False, True):
                with self.subTest(kind=kind, has_other=has_other):
                    marker.pop('session_end', None)
                    marker['session_process'] = dict(process)
                    if kind == 'receipt':
                        marker['session_end'] = {'process': process}
                    elif kind == 'boot':
                        marker['session_process']['boot_id'] = 'previous-boot'
                    path.write_text(json.dumps(marker))
                    with mock.patch.object(f.peer_message, '_fleet_steward', return_value=f.fleet_steward), \
                         mock.patch.object(f.fleet_steward, '_peer_message_module', return_value=f.peer_message), \
                         mock.patch.object(dispatch_contract, 'process_identity_disposition', return_value='dead'), \
                         mock.patch.object(f.fleet_steward, 'other_session_process_live', return_value=has_other) as other:
                        self.assertEqual(f.fleet_steward.role_flag('claude', 'owner',
                            sessions=self.rows, observation=observation), has_other)
                    other.assert_called_once_with('claude', 'owner',
                        process if kind == 'receipt' else marker['session_process'], observation=observation)

    def test_other_registry_process_keeps_watch_for_three_harnesses(self):
        import subprocess
        from types import SimpleNamespace
        from fleet import session_registry
        from fleet.collectors import procscan, codex
        import dispatch_seat_handover as handover
        import peer_obligations
        sc = f._load('steward_check_duplicate_end', 'steward_check.py')
        store = peer_obligations.ObligationStore(f.peer_message.peer_state_root())
        for harness in ('claude', 'opencode', 'codex'):
            with self.subTest(harness=harness):
                sid = harness + '-duplicate'
                self.session(sid, self.primary, harness)
                f.peer_message.grant_steward(harness, sid, f.EXPLICIT_TO, 'explicit', f.TS,
                                             source='explicit', steward_seat=None)
                second = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
                try:
                    birth = procscan.read_proc_start(second.pid)
                    directory = Path(session_registry._dir_for(harness))
                    directory.mkdir(parents=True, exist_ok=True)
                    record = directory / (str(second.pid) + '.json')
                    record.write_text(json.dumps(dict(pid=second.pid, procStart=birth,
                        sessionId=sid, cwd=str(self.primary), kind='interactive')))
                    duty = sc.register(harness, sid, store=store, ensure_runner=False)
                    if harness == 'codex':
                        # Codex already rejects a delayed unload while a new TUI owns it.
                        hook = f._load('codex_duplicate_end', '../adapters/codex/hooks/sessionend-lifecycle.py')
                        with mock.patch.object(hook, 'load_payload', return_value={'session_id':sid}), \
                             mock.patch.object(codex, 'process_for_thread', return_value=(second.pid, birth)), \
                             mock.patch('session_summary_trigger.launch_trigger'):
                            self.assertEqual(hook.main(), 0)
                    else:
                        f.peer_message.observe_steward_end(harness, sid, reason='logout')
                    settled = []
                    fake = SimpleNamespace(settle_steward_exited_watch=lambda wid: settled.append(wid) or True)
                    with mock.patch.object(handover, 'resolved_watch_references', return_value=[
                            {'watch_id':'duplicate-watch', 'storage_root':str(f.peer_message.peer_state_root())}]), \
                         mock.patch.object(f.peer_message, '_peer_steward_module', return_value=fake), \
                         mock.patch.object(f.fleet_steward, '_peer_message_module', return_value=f.peer_message):
                        released = sc._release_if_window_closed(duty, store, harness, sid, 1e10)
                    actual = (f.fleet_steward.session_liveness(harness, sid, agents=()),
                              released, len(settled), store.get(duty['id'])['state'])
                    print('duplicate-process', harness, actual, flush=True)
                    self.assertEqual(actual, ('live', False, 0, 'pending'))
                    self.assertTrue(f.fleet_steward.role_flag(harness, sid, sessions=self.rows))
                finally:
                    second.kill(); second.wait()
                    record.unlink(missing_ok=True)
                    f.peer_message.observe_steward_end(harness, sid, reason='logout')

    def test_end_receipt_records_exact_shell_process(self):
        import session_shell
        from fleet.collectors import procscan
        self.session('owner', self.primary)
        self.grant('owner')
        birth = procscan.read_proc_start(os.getpid())
        binding = {'native_pid':os.getpid(), 'native_start':birth}
        with mock.patch.object(session_shell, 'exact_binding', return_value=(binding, '')):
            f.peer_message.observe_steward_end('claude', 'owner', reason='logout')
        end = json.loads(f.peer_message.steward_marker_path('claude', 'owner').read_text())['session_end']
        self.assertEqual((end['process']['pid'], end['process']['pid_start']), (os.getpid(), birth))

    def test_same_process_stale_record_does_not_resurrect_ended_session(self):
        from fleet.collectors import procscan
        self.session('owner', self.primary)
        self.grant('owner')
        process = dict(f.peer_message._steward_process_context(), pid=os.getpid(),
                       pid_start=procscan.read_proc_start(os.getpid()))
        with mock.patch.object(f.peer_message, '_steward_ending_process', return_value=process):
            f.peer_message.observe_steward_end('claude', 'owner', reason='logout')
        record = dict(pid=process['pid'], procStart=process['pid_start'], sessionId='owner')
        with mock.patch.object(f.fleet_steward, '_registry_records', return_value=[record]), \
             mock.patch.object(f.fleet_steward, '_process_proof', return_value='live'), \
             mock.patch.object(procscan, '_visible_pids', return_value={os.getpid()}):
            self.assertEqual(f.peer_message.steward_session_end_proof('claude', 'owner'), 'ended')

    def test_native_holders_override_only_outside_ending_process(self):
        from fleet.collectors import procscan, opencode
        import claude_process_identity as identity
        process = dict(f.peer_message._steward_process_context(), pid=os.getpid(),
                       pid_start=procscan.read_proc_start(os.getpid()))
        other = os.getpid() + 100000
        claim = dict(pid=other, proc_start='123', kind='runtime', argv=['claude', '--resume', 'owner'])
        for harness in ('claude', 'opencode'):
            for has_other in (False, True):
                with self.subTest(harness=harness, has_other=has_other), \
                     mock.patch.object(f.fleet_steward, '_registry_records', return_value=[]), \
                     mock.patch.object(f.fleet_steward, '_process_proof', return_value='live'), \
                     mock.patch.object(procscan, '_visible_pids', return_value={os.getpid(), other}), \
                     mock.patch.object(procscan, 'read_proc_start', side_effect=lambda pid: process['pid_start'] if pid == os.getpid() else '123'), \
                     mock.patch.object(procscan, 'read_environ', return_value={'HOME':str(self.base)}), \
                     mock.patch.object(identity, 'observe_process', side_effect=lambda pid: claim if pid == other and has_other else None), \
                     mock.patch.object(identity, '_native_session_claim', return_value=(None, True)), \
                     mock.patch.object(identity, 'tap_sids_by_pid', return_value=set()), \
                     mock.patch.object(identity, '_argument_sessions', return_value={'owner'}), \
                     mock.patch.object(opencode, 'session_visibility', side_effect=lambda pids: {'visible':{'owner'} if other in pids and has_other else set()}):
                    self.assertEqual(f.fleet_steward.other_session_process_live(harness, 'owner', process), has_other)

    def test_end_hooks_finish_existing_work_before_steward_import_failure(self):
        import builtins
        import io
        from fleet import interaction
        for harness, filename in (
                ('claude', '../hooks/fleet-interaction-state.py'),
                ('codex', '../adapters/codex/hooks/sessionend-lifecycle.py')):
            with self.subTest(harness=harness):
                hook = f._load(harness + '_end_order', filename)
                calls = []
                real_import = builtins.__import__
                def failing_import(name, *args, **kwargs):
                    if name == 'peer_steward_base':
                        calls.append('steward')
                        raise ImportError('fixture')
                    return real_import(name, *args, **kwargs)
                payload = json.dumps({'session_id':'order', 'hook_event_name':'SessionEnd'})
                with mock.patch.object(sys, 'stdin', io.StringIO(payload)), \
                     mock.patch.object(sys, 'argv', ['hook', 'clear']), \
                     mock.patch.object(interaction, 'clear_wait', side_effect=lambda *a, **k: calls.append('clear')), \
                     mock.patch('session_summary_trigger.launch_trigger', side_effect=lambda *a: calls.append('summary')), \
                     mock.patch('builtins.__import__', side_effect=failing_import):
                    self.assertEqual(hook.main(), 0)
                self.assertEqual(calls, ['clear', 'steward'] if harness == 'claude'
                                 else ['clear', 'summary', 'steward'])

    def test_clear_and_compact_leave_role_and_watch_untouched(self):
        self.session('owner', self.primary)
        self.grant('owner')
        path = f.peer_message.steward_marker_path('claude', 'owner')
        original = path.read_bytes()
        for reason in ('clear', 'compact'):
            with mock.patch.object(f.peer_message, 'release_closed_session') as release:
                f.peer_message.observe_steward_end('claude', 'owner', reason=reason)
            release.assert_not_called()
            self.assertEqual(path.read_bytes(), original)

    def test_natural_exit_settles_watches_without_revoking_resumable_role(self):
        import dispatch_seat_handover as handover
        self.session('owner', self.primary)
        self.grant('owner')
        settle = mock.Mock(return_value=True)
        root = str(f.peer_message.peer_state_root())
        with mock.patch.object(handover, 'resolved_watch_references', return_value=[
                {'watch_id':'watch', 'storage_root':root}]), mock.patch.object(
                f.peer_message, '_release_role_relations') as revoke:
            result = f.peer_message.release_closed_session('claude', 'owner',
                        reason='session-ended', settle_watch=settle)
        revoke.assert_not_called()
        settle.assert_called_once_with('watch')
        self.assertEqual(result['watches'], 1)
        self.assertTrue(self.marker_exists('owner'))

    def test_native_end_hooks_are_short_silent_and_preserve_clear(self):
        import subprocess
        hook = Path(__file__).resolve().parents[1] / 'hooks/fleet-interaction-state.py'
        for harness in ('claude', 'opencode'):
            sid = harness + '-hook'
            self.session(sid, self.primary, harness)
            f.peer_message.grant_steward(harness, sid, f.EXPLICIT_TO, 'explicit', f.TS,
                                         source='explicit', steward_seat=None)
            for reason in ('clear', 'logout'):
                result = subprocess.run([sys.executable, str(hook), 'clear', '--harness', harness],
                    input=json.dumps({'session_id': sid, 'scope': 'session',
                        'hook_event_name': 'SessionEnd', 'reason': reason}),
                    text=True, capture_output=True, timeout=10)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
                marker = json.loads(f.peer_message.steward_marker_path(harness, sid).read_text())
                self.assertEqual('session_end' in marker, reason != 'clear')

    def test_inherited_holder_without_own_marker_can_record_end(self):
        import peer_obligations
        sc = f._load('steward_check_inherited_end', 'steward_check.py')
        store = peer_obligations.ObligationStore(f.peer_message.peer_state_root())
        sid = 'clear-successor'
        self.assertFalse(self.marker_exists(sid))
        sc.register('claude', sid, store=store, ensure_runner=False)
        f.peer_message.observe_steward_end('claude', sid, reason='logout')
        marker = json.loads(f.peer_message.steward_marker_path('claude', sid).read_text())
        self.assertEqual(marker['session_end']['reason'], 'logout')
        self.assertEqual(marker['targets'], {})  # lifecycle receipt is never new role evidence
        self.assertFalse(f.peer_message.is_steward_marker(marker))
        f.peer_message.observe_steward_end('claude', 'ordinary-session', reason='logout')
        self.assertFalse(self.marker_exists('ordinary-session'))

    def test_boot_change_is_end_but_foreign_host_and_unreadable_are_unknown(self):
        self.session('owner', self.primary)
        self.grant('owner')
        path = f.peer_message.steward_marker_path('claude', 'owner')
        data = json.loads(path.read_text())
        context = f.peer_message._steward_process_context()
        data['session_process'] = dict(context, pid=123, pid_start='1', boot_id='prior-boot')
        path.write_text(json.dumps(data))
        self.assertEqual(f.peer_message.steward_session_end_proof('claude', 'owner'), 'ended')
        data['session_process']['host'] = 'other-host'
        path.write_text(json.dumps(data))
        self.assertEqual(f.peer_message.steward_session_end_proof('claude', 'owner'), 'unknown')
        with mock.patch.object(f.peer_message, '_steward_process_context', side_effect=OSError):
            self.assertEqual(f.peer_message.steward_session_end_proof('claude', 'owner'), 'unknown')

    def test_killed_native_process_is_settled_by_normal_check_and_resume_reactivates(self):
        import subprocess
        import session_shell
        import peer_obligations
        from types import SimpleNamespace
        from fleet.collectors import procscan
        sc = f._load('steward_check_end_integration', 'steward_check.py')
        store = peer_obligations.ObligationStore(f.peer_message.peer_state_root())
        for harness in ('claude', 'codex', 'opencode'):
            sid = harness + '-killed'
            self.session(sid, self.primary, harness)
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            try:
                birth = procscan.read_proc_start(child.pid)
                row = {'harness':harness, 'sid':sid, 'native_pid':child.pid, 'native_start':birth}
                with mock.patch.object(session_shell, 'exact_binding', return_value=(row, '')):
                    result = f.peer_message.grant_steward(harness, sid, f.EXPLICIT_TO,
                        'explicit', f.TS, source='explicit', steward_seat=None)
                self.assertTrue(result['written'], result)
                marker = f.peer_message.steward_marker_path(harness, sid)
                self.assertEqual(json.loads(marker.read_text())['session_process']['pid'], child.pid)
                duty = sc.register(harness, sid, store=store, ensure_runner=False)
                child.kill(); child.wait()
                self.assertEqual(f.fleet_steward.session_liveness(harness, sid, agents=()), 'ended')
                self.assertTrue(sc._release_if_window_closed(duty, store, harness, sid, 123))
                self.assertEqual(store.get(duty['id'])['state'], 'complete')
                self.assertTrue(marker.exists())
                with mock.patch.object(session_shell, 'exact_binding', return_value=(None, 'attachment-unavailable')):
                    sc.reconcile_self(store=store, identity=SimpleNamespace(harness=harness, session_id=sid))
                self.assertEqual(store.get(duty['id'])['state'], 'pending')
                f.peer_message.observe_steward_end(harness, sid, reason='logout')
            finally:
                if child.poll() is None: child.kill()
                child.wait()

    def test_process_birth_evidence_survives_missing_native_records(self):
        import subprocess
        from fleet.collectors import procscan
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            evidence = f.peer_message._steward_process_context()
            evidence.update(pid=child.pid, pid_start=procscan.read_proc_start(child.pid))
            self.session('owner', self.primary)
            self.grant('owner')
            path = f.peer_message.steward_marker_path('claude', 'owner')
            data = json.loads(path.read_text()); data['session_process'] = evidence
            path.write_text(json.dumps(data))
            child.kill(); child.wait()
            self.assertEqual(f.peer_message.steward_session_end_proof('claude', 'owner'), 'ended')
            data['session_process']['pid_namespace'] = 'foreign-namespace'
            path.write_text(json.dumps(data))
            self.assertEqual(f.peer_message.steward_session_end_proof('claude', 'owner'), 'unknown')
        finally:
            if child.poll() is None: child.kill()
            child.wait()

if __name__ == '__main__':
    unittest.main()
