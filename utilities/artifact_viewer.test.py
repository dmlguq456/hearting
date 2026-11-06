#!/usr/bin/env python3
import hashlib
import contextlib
import io
import http.client
import os
import socket
import signal
import subprocess
import select
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_viewer as V


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'root'; self.root.mkdir()
        self.outside = self.base / 'outside'; self.outside.write_bytes(b'EXTERNAL_SENTINEL')
        (self.root/'campaigns'/'camp'/'cycles'/'cycle'/'artifacts').mkdir(parents=True)
        self.folder = self.root/'campaigns'/'camp'/'cycles'/'cycle'/'artifacts'
        (self.folder/'sound.wav').write_bytes(b'0123456789')
        (self.folder/'a b%한글.png').write_bytes(b'PNG')
        (self.folder/'doc.md').write_text('# Hello\n\n![pic](a%20b%25%ED%95%9C%EA%B8%80.png)\n\n<audio controls src="sound.wav"></audio>', encoding='utf-8')
        (self.folder/'report.html').write_bytes(b'<img src="a%20b%25%ED%95%9C%EA%B8%80.png"><audio src="sound.wav"></audio>')
        for hidden in V.HIDDEN:
            for parent in (self.root, self.root/'campaigns', self.root/'campaigns'/'camp', self.folder):
                (parent/hidden).mkdir(); (parent/hidden/'secret').write_bytes(b'SECRET')
        (self.root/'_internal-report.md').write_text('visible')
        (self.root/'leaf').symlink_to(self.outside)
        (self.root/'inner').symlink_to(self.folder, target_is_directory=True)
        os.mkfifo(self.root/'fifo')
        self.server = V.ViewerServer(self.root, 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.thread.join(timeout=5); self.server.server_close()
        self.assertFalse(self.thread.is_alive())
        self.tmp.cleanup()

    def request(self, path, method='GET', headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse(); body = response.read(); status = response.status; h = dict(response.getheaders()); conn.close()
        return status, h, body

    def wire(self, target, headers):
        with socket.create_connection(('127.0.0.1', self.port), timeout=2) as sock:
            sock.sendall(('GET '+target+' HTTP/1.1\r\n'+headers+'\r\n\r\n').encode('ascii'))
            data = b''
            while True:
                chunk = sock.recv(65536)
                if not chunk: break
                data += chunk
            return int(data.split(b' ', 2)[1]), data

    def test_host_corpus(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        for host in (f'localhost:{self.port}', f'127.0.0.1:{self.port}'):
            self.assertEqual(self.wire('/', 'Host: '+host)[0], 200)
        for header in ('', 'Host: example.com', f'Host: localhost.evil:{self.port}', f'Host: user@localhost:{self.port}', 'Host: 127.1:8765', 'Host: localhost:1', f'Host: localhost:{self.port}\r\nHost: localhost:{self.port}', f'Host: evil\r\nX-Forwarded-Host: localhost:{self.port}'):
            self.assertEqual(self.wire('/', header)[0], 400, header)

    def test_path_attacks_hidden_and_special(self):
        host = f'Host: localhost:{self.port}'
        attacks = ['/../outside', '/%2e%2e/outside', '/%2fetc', '/%5cetc', '/foo//bar', '//evil/path', 'http://evil/path', '/%00', '/%ff', '/%zz', '/leaf', '/inner/doc.md', '/fifo']
        for target in attacks:
            status, data = self.wire(target, host)
            self.assertEqual(status, 404, target); self.assertNotIn(b'EXTERNAL_SENTINEL', data)
        for hidden in V.HIDDEN:
            for prefix in ('', '/campaigns', '/campaigns/camp', '/campaigns/camp/cycles/cycle/artifacts'):
                for method in ('GET', 'HEAD'):
                    for suffix in ('', '?raw=1'):
                        status, _, body = self.request(prefix+'/'+hidden+'/secret'+suffix, method, {'Range': 'bytes=0-2'})
                        self.assertEqual(status, 404); self.assertNotIn(b'SECRET', body)
        self.assertEqual(self.request('/_internal-report.md')[0], 200)
        listing = self.request('/')[2]
        self.assertNotIn(b'_scratch', listing); self.assertNotIn(b'>leaf<', listing)

    def test_media_range_and_head(self):
        path = '/campaigns/camp/cycles/cycle/artifacts/sound.wav'
        for value, expected, content_range in [('bytes=0-3', b'0123', 'bytes 0-3/10'), ('bytes=4-', b'456789', 'bytes 4-9/10'), ('bytes=-4', b'6789', 'bytes 6-9/10'), ('bytes=8-99', b'89', 'bytes 8-9/10')]:
            status, headers, body = self.request(path, headers={'Range': value})
            self.assertEqual((status, body, headers['Content-Range']), (206, expected, content_range))
            self.assertEqual(int(headers['Content-Length']), len(expected))
        for value in ('bytes=10-', 'bytes=2-1', 'bytes=-0', 'bytes=0-1,3-4', 'other=0-2', 'bytes=-'):
            status, h, body = self.request(path, headers={'Range': value})
            self.assertEqual((status, h['Content-Range'], body), (416, 'bytes */10', b''))
        status, h, body = self.request(path, 'HEAD', {'Range':'bytes=0-1'})
        self.assertEqual((status, h['Content-Length'], body), (200, '10', b''))
        self.assertEqual(self.request(path)[2], b'0123456789')

    def test_streaming_limit_and_empty_range(self):
        large = self.folder/'large.mp3'; large.write_bytes(b'x' * (V.CHUNK * 3 + 7))
        empty = self.folder/'empty.wav'; empty.write_bytes(b'')
        sizes = []
        original = V.os.read
        def tracked(fd, amount):
            sizes.append(amount)
            return original(fd, amount)
        with mock.patch.object(V.os, 'read', side_effect=tracked):
            status, _, body = self.request('/campaigns/camp/cycles/cycle/artifacts/large.mp3')
        self.assertEqual((status, len(body)), (200, V.CHUNK * 3 + 7))
        self.assertTrue(sizes); self.assertLessEqual(max(sizes), V.CHUNK)
        status, headers, _ = self.request('/campaigns/camp/cycles/cycle/artifacts/empty.wav', headers={'Range':'bytes=0-'})
        self.assertEqual((status, headers['Content-Range']), (416, 'bytes */0'))

    def test_documents_and_directories(self):
        prefix = '/campaigns/camp/cycles/cycle/artifacts/'
        status, h, body = self.request(prefix+'doc.md')
        self.assertEqual(status, 200); self.assertIn(b'<h1 id="hello">', body)
        self.assertIn(b'<audio controls src="sound.wav">', body)
        self.assertIn('sandbox', h['Content-Security-Policy'])
        self.assertEqual(self.request(prefix+'a%20b%25%ED%95%9C%EA%B8%80.png')[2], b'PNG')
        self.assertEqual(self.request(prefix+'doc.md?raw=1')[2], (self.folder/'doc.md').read_bytes())
        self.assertEqual(self.request(prefix+'report.html')[2], (self.folder/'report.html').read_bytes())
        self.assertEqual(self.request(prefix[:-1])[0], 301)
        self.assertEqual(self.request(prefix+'sound.wav', 'POST')[0], 405)
        (self.folder/'huge.md').write_bytes(b'x'*(V.MAX_MARKDOWN+1))
        result = self.request(prefix+'huge.md')[2]
        self.assertIn(b'?raw=1', result); self.assertLess(len(result), 1000)

    def test_live_probe_and_read_only(self):
        def inventory():
            return sorted((str(p.relative_to(self.root)), p.lstat().st_mode, p.lstat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else '') for p in self.root.rglob('*'))
        before = inventory()
        self.assertTrue(V._probe(self.root, self.port)); self.assertFalse(V._probe(self.base, self.port))
        url = V.viewer_url(self.root, self.folder/'doc.md', self.port)
        self.assertIn('campaigns/camp', url)
        self.assertIsNone(V.viewer_url(self.root, self.outside, self.port))
        self.request('/'); self.request('/leaf'); self.request('/campaigns/')
        self.assertEqual(before, inventory())

    def test_legacy_cycle_and_directory_pages(self):
        status, _, body = self.request('/campaigns/camp/')
        self.assertEqual(status, 200)
        self.assertIn(b'href="cycles/cycle/"', body)
        self.assertNotIn(b'>cycles/', body)
        for i in range(205):
            (self.root / f'file-{i:03}').write_text('x')
        status, _, body = self.request('/?offset=200')
        self.assertEqual(status, 200); self.assertIn(b'offset=0', body)
        self.assertNotIn(b'file-000', body)
        self.assertEqual(self.request('/campaigns/camp/cycles/cycle/artifacts/report.html', 'HEAD')[2], b'')

    def test_ancestor_symlink_swap_and_unknown_method(self):
        parent = self.root/'race'; parent.mkdir(); (parent/'doc').write_bytes(b'safe')
        outside_dir = self.base/'external-dir'; outside_dir.mkdir(); (outside_dir/'doc').write_bytes(b'EXTERNAL_SENTINEL')
        original = V.os.open
        def swapped(path, flags, *args, **kwargs):
            if path == 'race':
                parent.rename(self.root/'race-old'); parent.symlink_to(outside_dir, target_is_directory=True)
            return original(path, flags, *args, **kwargs)
        with mock.patch.object(V.os, 'open', side_effect=swapped):
            status, _, body = self.request('/race/doc')
        self.assertEqual(status, 404); self.assertNotIn(b'EXTERNAL_SENTINEL', body)
        self.assertEqual(self.request('/', 'WHATEVER')[0], 405)

    def test_no_follow_swap(self):
        # Swap the leaf just before descriptor-relative open, after path parsing.
        original = V.os.open
        target = self.folder/'swap'
        target.write_bytes(b'safe')
        def swapped(path, flags, *args, **kwargs):
            if path == 'swap':
                target.unlink(); target.symlink_to(self.outside)
            return original(path, flags, *args, **kwargs)
        with mock.patch.object(V.os, 'open', side_effect=swapped):
            status, _, body = self.request('/campaigns/camp/cycles/cycle/artifacts/swap')
        self.assertEqual(status, 404); self.assertNotIn(b'EXTERNAL_SENTINEL', body)


class ProbeTests(unittest.TestCase):
    def test_root_refusal_and_arguments(self):
        for result, code in ((mock.Mock(returncode=7, stderr='refused', stdout=''), 7),
                             (mock.Mock(returncode=0, stderr='', stdout='  '), 2)):
            with mock.patch.object(V.subprocess, 'run', return_value=result), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(V.main([]), code)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(V.main(['--host']), 2)

    def test_invalid_response_and_timeout(self):
        for status, body in ((200, b'{'), (302, b'{}'), (200, b'{' + b' ' * 4096), (200, b'[]'),
                             (200, b'{"protocol":"other","root_key":"wrong"}')):
            response = mock.Mock(status=status); response.read.return_value = body
            connection = mock.Mock(); connection.getresponse.return_value = response
            with mock.patch.object(V.http.client, 'HTTPConnection', return_value=connection):
                self.assertFalse(V._probe('/tmp/root'))
                connection.close.assert_called_once()
        with mock.patch.object(V.http.client, 'HTTPConnection', side_effect=socket.timeout):
            self.assertFalse(V._probe('/tmp/root'))
        connection = mock.Mock(); connection.request.side_effect = socket.timeout
        with mock.patch.object(V.http.client, 'HTTPConnection', return_value=connection):
            self.assertFalse(V._probe('/tmp/root'))
            connection.close.assert_called_once()


class ViewerCLITests(unittest.TestCase):
    def test_foreground_same_root_and_other_root(self):
        # Port 8765 is the public contract. Do not disturb an existing listener.
        probe = socket.socket()
        try:
            probe.bind(('127.0.0.1', V.PORT))
        except OSError:
            self.skipTest('port 8765 already occupied')
        finally:
            probe.close()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); root = base/'root'; root.mkdir(); other = base/'other'; other.mkdir()
            env = dict(os.environ, AGENT_ARTIFACT_ROOT=str(root), HOME=tmp, PYTHONDONTWRITEBYTECODE='1')
            script = str(Path(__file__).resolve().parent/'artifact_viewer.py')
            process = subprocess.Popen([sys.executable, '-B', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            try:
                ready, _, _ = select.select([process.stdout], [], [], 10)
                self.assertTrue(ready, 'viewer did not report readiness')
                self.assertIn('127.0.0.1:8765', process.stdout.readline())
                duplicate = subprocess.run([sys.executable, '-B', script], capture_output=True, text=True, env=env, timeout=10)
                self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
                other_env = dict(env, AGENT_ARTIFACT_ROOT=str(other))
                collision = subprocess.run([sys.executable, '-B', script], capture_output=True, text=True, env=other_env, timeout=10)
                self.assertEqual(collision.returncode, 2); self.assertIn('8765', collision.stderr)
                invalid = subprocess.run([sys.executable, '-B', script, '--host'], capture_output=True, text=True, env=env, timeout=10)
                self.assertEqual(invalid.returncode, 2)
            finally:
                process.send_signal(signal.SIGINT)
                try: process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.communicate(); self.fail('viewer did not stop')
            self.assertEqual(process.returncode, 0)


if __name__ == '__main__': unittest.main()
