import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import shlex
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

    def test_known_runtime_commands_run_with_spaces_and_no_path_discovery(self):
        # The shared launcher exists for all adapters; Claude has no preflight.sh.
        import shutil
        runtime = Path(self.tmp.name) / 'runtime with spaces'
        runtime.mkdir()
        (runtime / 'tools/install').mkdir(parents=True)
        shutil.copy2(ROOT / 'tools/install/harness.sh', runtime / 'tools/install/harness.sh')
        (runtime / 'utilities').mkdir()
        (runtime / 'utilities/fixture.py').write_text('print("known-runtime")\n')
        for harness in ('claude', 'codex', 'opencode'):
            preflight = runtime / f'adapters/{harness}/bin/preflight.sh'
            if (ROOT / f'adapters/{harness}/bin/preflight.sh').is_file():
                preflight.parent.mkdir(parents=True)
                preflight.write_text('echo known-preflight\n')
            with self.subTest(harness=harness), patch.object(receipt, 'identity',
                    return_value=SessionIdentity(harness, 'own', 'test', 'sole')):
                commands = receipt.runtime_commands(runtime)
                env = {**os.environ, 'AGENT_HOME': str(runtime)}
                ran = subprocess.run([*shlex.split(commands['run_command']), 'fixture'],
                                     env=env, capture_output=True, text=True, check=True)
                self.assertEqual(ran.stdout, 'known-runtime\n')
                self.assertEqual(commands['agent_home'], str(runtime))
                if preflight.is_file():
                    ran = subprocess.run(shlex.split(commands['preflight_command']),
                                         capture_output=True, text=True, check=True)
                    self.assertEqual(ran.stdout, 'known-preflight\n')
                else:
                    self.assertNotIn('preflight_command', commands)
        with patch.object(receipt, 'identity', return_value=SessionIdentity()):
            self.assertNotIn('preflight_command', receipt.runtime_commands(runtime))

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

    def route_files(self, route_id='rt-test', closed=False, outcome_text=None):
        """A route file and, once closed, its outcome beside it; the receipt names the route file."""
        routes = Path(self.tmp.name) / '.runtime' / 'routes'
        routes.mkdir(parents=True, exist_ok=True)
        route = {'route_id': route_id, 'route_hash': 'sha256:' + 'b' * 64}
        path = routes / (route_id + '.json')
        path.write_text(json.dumps(route))
        if closed or outcome_text is not None:
            (routes / (route_id + '.outcome.json')).write_text(
                outcome_text if outcome_text is not None else json.dumps(dict(route, closed_at='2026-11-26T00:00:00Z')))
        return path

    def after_compaction(self):
        import context_repeat
        context_repeat.reset('codex', 'own', compaction=True)

    def test_a_closed_routes_receipt_is_not_published_again_after_a_compaction(self):
        self.result['route_file'] = str(self.route_files(closed=True))
        self.save()
        self.after_compaction()
        self.assertEqual(receipt.context('codex', 'own', self.jobs), '')

    def test_an_open_routes_receipt_is_published_again_after_a_compaction(self):
        self.result['route_file'] = str(self.route_files())
        self.save()
        self.assertIn('state=running', receipt.context('codex', 'own', self.jobs))
        self.after_compaction()
        self.assertIn('state=running', receipt.context('codex', 'own', self.jobs))

    def test_an_unreadable_closure_leaves_the_receipt_published(self):
        saved = self.save()                                  # no route_file at all
        self.assertIn('parent_next=bounded-wait', receipt.context('codex', 'own', self.jobs))
        for route_file, outcome in (('relative/rt-test.json', None),                  # not absolute
                                    (str(Path(self.tmp.name) / 'gone.json'), None),   # route file missing
                                    (None, 'not json'),                               # outcome unreadable
                                    (None, json.dumps({'route_id': 'rt-test', 'route_hash': 'sha256:other'}))):  # other generation
            self.result['route_file'] = route_file or str(self.route_files(outcome_text=outcome))
            self.save()
            self.after_compaction()
            self.assertIn('state=running', receipt.context('codex', 'own', self.jobs), (route_file, outcome))

    def test_a_closed_routes_receipt_gives_way_to_the_sessions_newer_route(self):
        # One receipt file per session: a later start overwrites it, so the next Bash shows that route.
        self.result['route_file'] = str(self.route_files(closed=True))
        self.save()
        self.result.update(route_id='rt-next', route_file=str(self.route_files('rt-next')))
        self.save()
        self.assertIn('route_id=rt-next', receipt.context('codex', 'own', self.jobs))


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
            # (나) A failed owner: the person decides; close first (accept as it is, or stop), then the correction.
            ({'state': 'needs-attention', 'required_action': 'answer-failed-owner', 'owner_report': '/r/report.md',
              'correction_command': 'py capability-route.py correct --attempt-id att-o',
              'close_command': 'env AGENT_DISPATCH_JOBS=/j py capability-route.py close --route /r/rt-x.json'},
             'next: the owner ended FAIL; the person decides (owner_report says what it needs): to accept the result '
             'as it is or stop the work, run: env AGENT_DISPATCH_JOBS=/j py capability-route.py close --route '
             '/r/rt-x.json; to give the owner a fix or an answer, put it in a file and run: '
             'py capability-route.py correct --attempt-id att-o --message-file <answer file>'),
            # No readable report: the line names what the receipt names instead (its log), or nothing.
            ({'state': 'needs-attention', 'required_action': 'answer-failed-owner', 'owner_log_file': '/l/o.jsonl',
              'correction_command': 'py correct --attempt-id att-o', 'close_command': 'py close --route rt-x'},
             'next: the owner ended FAIL; the person decides (its result is in owner_log_file /l/o.jsonl): to '
             'accept the result as it is or stop the work, run: py close --route rt-x; to give the owner a fix or '
             'an answer, put it in a file and run: py correct --attempt-id att-o --message-file <answer file>'),
            ({'state': 'needs-attention', 'required_action': 'answer-failed-owner',
              'correction_command': 'py correct --attempt-id att-o', 'close_command': 'py close --route rt-x'},
             'next: the owner ended FAIL; the person decides: to accept the result as it is or stop the work, run: '
             'py close --route rt-x; to give the owner a fix or an answer, put it in a file and run: '
             'py correct --attempt-id att-o --message-file <answer file>'),
            # A BLOCKED owner waits for an answer: the answer first, the stop after.
            ({'state': 'needs-attention', 'required_action': 'answer-blocked-owner',
              'correction_command': 'py capability-route.py correct --attempt-id att-o',
              'close_command': 'env AGENT_DISPATCH_JOBS=/j py capability-route.py close --route /r/rt-x.json'},
             "next: the owner is waiting for an answer; get the person's answer to owner_report, then run: "
             'py capability-route.py correct --attempt-id att-o --message-file <answer file>; to stop the work '
             'instead, run: env AGENT_DISPATCH_JOBS=/j py capability-route.py close --route /r/rt-x.json'),
            # A receipt that carries no close_command keeps the answer alone.
            ({'state': 'needs-attention', 'required_action': 'answer-blocked-owner',
              'correction_command': 'py capability-route.py correct --attempt-id att-o'},
             "next: get the person's answer to owner_report, then run: "
             'py capability-route.py correct --attempt-id att-o --message-file <answer file>'),
            # (다) A refusal the same start repeats: its own next step and the close it names, no resume_command.
            ({'state': 'needs-attention', 'reason': 'replacement-workflow-not-open',
              'required_action': 'inspect-preparation', 'resume_command': 'py start --route rt-x',
              'close_command': 'py close --route rt-x',
              'next_step': "This route's workflow already ended (COMPLETE), so no replacement owner can continue "
                           "it, and running the same start again is refused the same way. Read the owner's result; "
                           "to end the route as it is, run close_command; for more work, compose a new route."},
             'next: inspect-preparation (replacement-workflow-not-open): This route\'s workflow already ended '
             '(COMPLETE), so no replacement owner can continue it, and running the same start again is refused the '
             "same way. Read the owner's result; to end the route as it is, run close_command; for more work, "
             'compose a new route. close_command=py close --route rt-x'),
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
