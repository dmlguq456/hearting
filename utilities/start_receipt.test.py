import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'utilities'))
import start_receipt as receipt
from session_identity import SessionIdentity
sys.path.insert(0, str(ROOT / 'tools'))
import fixture_processes


class StartReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(fixture_processes.reap, self.tmp.name)
        self.jobs = Path(self.tmp.name) / 'jobs.log'
        self.result = {'route_id': 'rt-test', 'state': 'running', 'parent_next': 'bounded-wait',
                       'parent_next_command': 'hearting run capability-route start --route rt-test --wait',
                       'launches': [{'receipt': 'started=1\n'}]}

    def save(self, harness='codex', sid='own'):
        with patch.object(receipt, 'identity', return_value=SessionIdentity(harness, sid, 'test', 'sole')):
            return receipt.save(self.result, self.jobs)

    def test_saved_full_receipt_survives_filtered_stdout_and_new_receipt_republishes_once(self):
        saved = self.save()
        path = Path(saved['receipt_file'])
        self.assertEqual(json.loads(path.read_text())['receipt'], saved)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        text = receipt.context('codex', 'own', self.jobs)
        self.assertIn('parent_next=bounded-wait', text)
        self.assertIn(self.result['parent_next_command'], text)
        self.assertEqual(text.splitlines()[-1], receipt.next_line(saved))
        self.assertEqual(receipt.context('codex', 'own', self.jobs), '')
        self.result['parent_next'] = 'end-turn'
        self.result['parent_next_command'] = ''
        self.save()
        self.assertIn('parent_next=end-turn', receipt.context('codex', 'own', self.jobs))

    def test_foreign_stale_and_malformed_receipts_never_republish(self):
        path = Path(self.save()['receipt_file'])
        self.assertEqual(receipt.context('claude', 'own', self.jobs), '')
        self.assertEqual(receipt.context('codex', 'foreign', self.jobs), '')
        row = json.loads(path.read_text()); row['saved_at'] = 0; path.write_text(json.dumps(row))
        self.assertEqual(receipt.context('codex', 'own', self.jobs), '')
        path.write_text('broken')
        self.assertEqual(receipt.context('codex', 'own', self.jobs), '')

    def test_storage_failure_and_missing_identity_preserve_execution_result(self):
        with patch.object(receipt, '_write', side_effect=OSError('read only')):
            self.assertIs(self.save(), self.result)
        with patch.object(receipt, 'identity', return_value=SessionIdentity()):
            saved = receipt.save(self.result, self.jobs)
        self.assertEqual(saved['parent_next'], self.result['parent_next'])
        self.assertTrue(Path(saved['receipt_file']).is_file())
        self.assertEqual(receipt.context('codex', 'own', self.jobs), '')

    def test_native_hook_json_and_opencode_text_read_same_saved_value(self):
        for harness in ('claude', 'codex', 'opencode'):
            with self.subTest(harness=harness):
                self.save(harness)
                env = {**os.environ, 'AGENT_HOME': str(ROOT), 'AGENT_DISPATCH_JOBS': str(self.jobs)}
                if harness == 'opencode':
                    cmd = [sys.executable, str(ROOT/'utilities/start_receipt.py'), '--harness', harness, '--session-id', 'own']
                    payload = ''
                else:
                    cmd = (['sh', str(ROOT/'adapters/codex/hooks/run-hook.sh'), 'start-receipt-context.py']
                           if harness == 'codex' else [sys.executable, str(ROOT/f'adapters/{harness}/hooks/start-receipt-context.py')])
                    if harness == 'codex': cmd.append('--codex')
                    payload = json.dumps({'hook_event_name': 'PostToolUse', 'session_id': 'own', 'tool_response': 'FILTERED'})
                proc = subprocess.run(cmd, env=env, input=payload, text=True, capture_output=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                text = proc.stdout if harness == 'opencode' else json.loads(proc.stdout)['hookSpecificOutput']['additionalContext']
                self.assertIn('parent_next=bounded-wait', text)
                self.assertEqual(subprocess.run(cmd, env=env, input=payload, text=True, capture_output=True).stdout, '')

    def test_installed_hook_copy_and_opencode_callback_keep_receipt_visible(self):
        self.save('claude')
        env = {**os.environ, 'AGENT_HOME': str(ROOT), 'AGENT_DISPATCH_JOBS': str(self.jobs),
               'XDG_STATE_HOME': self.tmp.name}
        installed = Path(self.tmp.name) / 'runtime-hooks/start-receipt-context.py'
        installed.parent.mkdir(); installed.write_bytes((ROOT/'hooks/start-receipt-context.py').read_bytes())
        payload = json.dumps({'hook_event_name': 'PostToolUse', 'session_id': 'own'})
        proc = subprocess.run([sys.executable, str(installed)], input=payload, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('parent_next=bounded-wait', json.loads(proc.stdout)['hookSpecificOutput']['additionalContext'])
        self.save('opencode')
        plugin = (ROOT/'adapters/opencode/plugins/hearting-guards.js').as_uri()
        script = f'''import {{ AgentHarnessGuards }} from {json.dumps(plugin)};
const hooks = await AgentHarnessGuards({{directory: {json.dumps(self.tmp.name)}, client: {{}}}});
const output = {{output: "FILTERED", args: {{}}}};
await hooks["tool.execute.after"]({{tool: "bash", sessionID: "own", args: {{}}}}, output);
console.log(JSON.stringify(output));
hooks.dispose();'''
        proc = subprocess.run(['node', '--input-type=module', '-e', script], env=env, text=True,
                              capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        output = json.loads(proc.stdout)
        self.assertTrue(output['output'].startswith('FILTERED\n[start-receipt]'))
        self.assertIn('parent_next=bounded-wait', output['output'])


class NextLineTest(unittest.TestCase):
    """One plain line restating the receipt's directive, so nobody parses the JSON to act."""

    def test_each_common_receipt_names_its_next_action_in_plain_words(self):
        wait = 'dispatch-wait.sh --attempt-id att-1 --max 600'
        cases = [
            ({'state': 'running', 'parent_next': 'end-turn', 'next_step': 'You are the parent session.'},
             'next: end this turn; the runtime brings the result back to this session'),
            ({'state': 'running', 'parent_next': 'bounded-wait', 'parent_next_command': wait},
             'next: run this bounded wait, then read its result: ' + wait),
            ({'state': 'needs-attention', 'reason': 'owner-launch-not-admitted', 'parent_next': 'end-turn',
              'required_action': 'wait-for-auto-resume', 'auto_resume': {'resume_at': '2026-11-02T00:53:37Z'}},
             'next: end this turn; nothing to run - the runtime starts this route again at '
             '2026-11-02T00:53:37Z and leaves one notice'),
            ({'state': 'needs-attention', 'required_action': 'answer-blocked-owner',
              'correction_command': 'py capability-route.py correct --attempt-id att-o'},
             "next: get the person's answer to owner_report, then run: "
             'py capability-route.py correct --attempt-id att-o --message-file <answer file>'),
            ({'state': 'completed', 'required_action': 'advance-completed'},
             'next: the route is complete; report its result'),
            ({'state': 'needs-attention', 'reason': 'frame-launch-not-admitted', 'required_action': 'inspect-preparation',
              'next_step': 'Fix it,\n  then use resume_command.', 'resume_command': 'py start --route rt-x'},
             'next: inspect-preparation (frame-launch-not-admitted): Fix it, then use resume_command. '
             'resume_command=py start --route rt-x'),
            ({'state': 'not-admitted', 'reason': 'owner-input-unsupported', 'next_step': 'Retain the correction.'},
             'next: not-admitted (owner-input-unsupported): Retain the correction.'),
            ({'applied': 'not-verified', 'next_step': 'Inspect delivery receipts.'},
             'next: Inspect delivery receipts.'),
            ({'state': 'preparing', 'resume_command': 'py start --route rt-x'},
             'next: state preparing; to continue, run: py start --route rt-x'),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                line = receipt.next_line(value)
                self.assertEqual(line, expected)
                self.assertNotIn('\n', line)
        self.assertEqual(receipt.next_line({}), '')
        self.assertEqual(receipt.next_line(None), '')

    def test_an_inline_route_is_told_to_do_the_work_and_finish_it(self):
        line = receipt.next_line({'state': 'inline', 'required_action': 'execute-inline', 'route_id': 'rt-' + 'a' * 16,
                                  'route_file': '/r/.runtime/routes/rt-' + 'a' * 16 + '.json'})
        self.assertTrue(line.startswith('next: do the work in this session, then run: '), line)
        self.assertIn('capability-route.py finish --route rt-' + 'a' * 16
                      + ' --evidence <FILE|URL> --summary-file <FILE>', line)

    def test_the_line_goes_last_to_stderr_and_stdout_keeps_only_the_json(self):
        script = ('import json, sys; sys.path.insert(0, %r); import start_receipt as r; '
                  'value = {"state": "running", "parent_next": "end-turn"}; '
                  'print(json.dumps(value)); r.print_next_line(value)') % str(ROOT / 'utilities')
        proc = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {'state': 'running', 'parent_next': 'end-turn'})
        self.assertEqual(proc.stderr.splitlines()[-1],
                         'next: end this turn; the runtime brings the result back to this session')


if __name__ == '__main__':
    unittest.main()
