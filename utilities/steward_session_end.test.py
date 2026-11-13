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
