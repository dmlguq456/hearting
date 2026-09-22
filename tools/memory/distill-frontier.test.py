#!/usr/bin/env python3
"""Captured distillation windows, isolated CLI and actual-worker regressions."""
import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
MEM = ROOT / 'tools/memory/mem.py'
BODY1 = 'FRONTIERALPHA user correction: the approved deployment region is ap-northeast-2 for this synthetic project.'
BODY2 = 'FRONTIERBETA user correction: the project now requires ap-northeast-1 and the previous region is superseded.'


class FrontierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='memory-frontier-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        home = self.base / 'home'; home.mkdir()
        self.project = self.base / 'project'; self.project.mkdir()
        agent = self.base / 'agent'; (agent / 'core').mkdir(parents=True)
        (agent / 'core/CORE.md').write_text('# Isolated source-home fixture\n')
        self.store = self.base / 'store'
        self.env = {
            'HOME': str(home), 'PATH': os.defpath, 'AGENT_HOME': str(agent),
            'XDG_CONFIG_HOME': str(self.base / 'config'),
            'XDG_DATA_HOME': str(self.base / 'data'),
            'XDG_STATE_HOME': str(self.base / 'state'),
            'MEM_STORE': str(self.store), 'MEM_PROJECTS': str(self.base / 'projects'),
            'MEM_WRITE_EVENTS': str(self.base / 'state/write.jsonl'),
            'MEM_RECALL_EVENTS': str(self.base / 'state/recall.jsonl'),
            'MEM_RECALL_RECEIPTS': str(self.base / 'state/receipts'),
            'CODEX_SESSIONS': str(self.base / 'sessions'),
            'OPENCODE_EXPORT_FILE': str(self.base / 'opencode-export.json'),
            'AGENT_MODEL_GOVERNOR_ROOT': str(self.base / 'governor'),
            'AGENT_ARTIFACT_ROOT': str(self.project / '.agent_reports'),
        }
        self.mem('index')

    def mem(self, *args, rc=0, env=None):
        result = subprocess.run([sys.executable, str(MEM), *args], cwd=self.project,
            env=env or self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, rc, (args, result.stdout, result.stderr))
        return result.stdout

    def append(self, source, sid, uuid, text):
        if source == 'codex':
            path = Path(self.env['CODEX_SESSIONS']) / (sid + '.jsonl')
            row = {'type': 'event_msg', 'payload': {'type': 'user_message', 'id': uuid, 'message': text}}
        elif source == 'claude':
            path = Path(self.env['MEM_PROJECTS']) / 'fixture' / (sid + '.jsonl')
            row = {'type': 'user', 'uuid': uuid, 'message': {'role': 'user', 'content': text}}
        else:
            path = Path(self.env['OPENCODE_EXPORT_FILE'])
            rows = json.loads(path.read_text()) if path.exists() else []
            rows.append({'id': uuid, 'role': 'user', 'content': text})
            path.write_text(json.dumps(rows))
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as out:
            out.write(json.dumps(row) + '\n')
        return path

    def capture(self, source, sid):
        return json.loads(self.mem('distill', sid, '--source', source, '--capture'))

    def close(self, source, sid, token, rc=0, env=None):
        return self.mem('distill', sid, '--source', source, '--advance-capture', token, rc=rc, env=env)

    def marker(self, sid):
        return (self.store / ('.distill-state-' + sid)).read_text().strip()

    def test_appended_tail_remains_pending_for_all_sources(self):
        for source in ['claude', 'codex', 'opencode']:
            with self.subTest(source=source):
                sid = 'frontier-' + source
                self.append(source, sid, 'u1', BODY1)
                captured = self.capture(source, sid)
                self.assertEqual(captured['delta'], '[user] ' + BODY1 + '\n')
                decoded = base64.urlsafe_b64decode(captured['frontier']).decode()
                self.assertNotIn(BODY1, decoded)
                path = self.append(source, sid, 'u2', BODY2)
                # Closing must not locate/read/export the current transcript.
                saved = path.with_suffix('.saved'); path.rename(saved)
                try:
                    self.close(source, sid, captured['frontier'])
                finally:
                    saved.rename(path)
                self.assertEqual(self.marker(sid), 'u1')
                self.assertEqual(self.mem('distill', sid, '--source', source), '[user] ' + BODY2 + '\n')
                second = self.capture(source, sid)
                self.close(source, sid, second['frontier'])
                self.assertEqual(self.marker(sid), 'u2')
                self.assertEqual(self.mem('distill', sid, '--source', source), '')

    def test_stale_capture_cannot_regress_newer_marker(self):
        sid = 'stale-frontier'
        self.append('codex', sid, 'u1', BODY1)
        older = self.capture('codex', sid)
        self.append('codex', sid, 'u2', BODY2)
        newer = self.capture('codex', sid)
        self.close('codex', sid, newer['frontier'])
        self.close('codex', sid, older['frontier'], rc=2)
        self.assertEqual(self.marker(sid), 'u2')
        self.close('codex', sid, newer['frontier'])
        self.assertEqual(self.marker(sid), 'u2')

    def test_cross_session_source_store_and_path_tokens_are_rejected(self):
        sid = 'bound-frontier'
        self.append('codex', sid, 'u1', BODY1)
        token = self.capture('codex', sid)['frontier']
        self.close('codex', 'another-session', token, rc=2)
        self.close('claude', sid, token, rc=2)
        self.close('codex', '../outside', token, rc=2)
        other = self.base / 'other-store'
        self.close('codex', sid, token, rc=2, env={**self.env, 'MEM_STORE': str(other)})
        self.assertFalse(other.exists())
        self.assertFalse((self.store / ('.distill-state-' + sid)).exists())
        for malformed in ['!', 'x' * 8193, base64.urlsafe_b64encode(b'{}').decode(),
                          base64.urlsafe_b64encode(b'[' * 1100 + b'0' + b']' * 1100).decode()]:
            self.close('codex', sid, malformed, rc=2)
        self.assertFalse((self.store / ('.distill-state-' + sid)).exists())

    def test_concurrent_different_windows_never_overwrite_a_committed_boundary(self):
        sid = 'parallel-frontiers'
        self.append('codex', sid, 'u1', BODY1)
        first = self.capture('codex', sid)['frontier']
        self.append('codex', sid, 'u2', BODY2)
        second = self.capture('codex', sid)['frontier']
        procs = [subprocess.Popen([sys.executable, str(MEM), 'distill', sid,
                    '--source', 'codex', '--advance-capture', token], env=self.env,
                    cwd=self.project, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for token in [first, second]]
        statuses = []
        for proc in procs:
            proc.communicate(timeout=10); statuses.append(proc.returncode)
        self.assertEqual(sorted(statuses), [0, 2])
        marker = self.marker(sid)
        self.assertIn(marker, ['u1', 'u2'])
        pending = self.mem('distill', sid, '--source', 'codex')
        self.assertEqual(pending, '[user] ' + BODY2 + '\n' if marker == 'u1' else '')

    def test_real_codex_worker_preserves_u2_and_retry_stores_both_records(self):
        sid = 'active-model-frontier'
        self.append('codex', sid, 'u1', BODY1)
        bin_dir = self.base / 'bin'; bin_dir.mkdir()
        started = self.base / 'model-started'
        release = self.base / 'model-release'
        calls = self.base / 'model-calls'
        model = bin_dir / 'codex'
        model.write_text('#!' + sys.executable + '\n'
            'import json, sys, time\nfrom pathlib import Path\n'
            'prompt = sys.stdin.read()\n'
            'calls = Path(' + repr(str(calls)) + ')\n'
            'count = int(calls.read_text()) + 1 if calls.exists() else 1\n'
            'calls.write_text(str(count))\n'
            'if count == 1:\n'
            ' Path(' + repr(str(started)) + ').touch()\n'
            ' deadline = time.monotonic() + 8\n'
            ' while not Path(' + repr(str(release)) + ').exists():\n'
            '  if time.monotonic() >= deadline: sys.exit(89)\n'
            '  time.sleep(0.02)\n'
            'body = ' + repr(BODY2) + ' if "FRONTIERBETA" in prompt else ' + repr(BODY1) + '\n'
            'action = {"tier":"durable", "type":"user-correction", "body":body, "headline":body.split()[0], "aliases":[], "entities":[], "topics":["frontier-tests"], "artifact_refs":[]}\n'
            'Path(sys.argv[sys.argv.index("--output-last-message") + 1]).write_text(json.dumps(action) + "\\n")\n')
        model.chmod(0o755)
        env = {**self.env, 'PATH': str(bin_dir) + os.pathsep + os.defpath,
               'CODEX_DISTILL_ENABLE': '1', 'CODEX_DISTILL_APPLY': '1',
               'CODEX_DISTILL_CONTRACT_ACCEPTED': '1', 'CODEX_DISTILL_TIMEOUT': '10',
               'MEM_SESSION_COMPLETION': '1'}
        command = ['sh', str(ROOT / 'adapters/codex/bin/distill-worker.sh'), sid, str(self.project)]
        worker = subprocess.Popen(command, cwd=self.project, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not started.exists():
                self.assertIsNone(worker.poll(), 'worker exited before model start')
                self.assertLess(time.monotonic(), deadline, 'model did not start')
                time.sleep(0.02)
            self.append('codex', sid, 'u2', BODY2)
            duplicate = subprocess.run(command, cwd=self.project, env=env,
                                       capture_output=True, text=True, timeout=5)
            self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
            self.assertEqual(calls.read_text(), '1')
            release.touch()
            stdout, stderr = worker.communicate(timeout=10)
            self.assertEqual(worker.returncode, 0, (stdout, stderr))
            self.assertEqual(self.marker(sid), 'u1')
            self.assertEqual(self.mem('distill', sid, '--source', 'codex'), '[user] ' + BODY2 + '\n')
            retry = subprocess.run(command, cwd=self.project, env=env,
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(retry.returncode, 0, (retry.stdout, retry.stderr))
            self.assertEqual(calls.read_text(), '2')
            self.assertEqual(self.marker(sid), 'u2')
            self.assertEqual(self.mem('distill', sid, '--source', 'codex'), '')
            rows = json.loads(self.mem('recall', 'frontier-tests', '--topic', 'frontier-tests', '--full', '--json'))['results']
            self.assertEqual({row['body'] for row in rows}, {BODY1, BODY2})
            self.assertEqual(len({row['id'] for row in rows}), 2)
            self.assertTrue(all(row['id'] and row['type'] == 'user-correction' for row in rows))
            for row in rows:
                self.assertIn(row['body'], self.mem('show', row['id']))
        finally:
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            worker.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
