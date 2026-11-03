"""Opt-in tests against an unmodified installed Claude Code binary.

The CLI and a local TLS CONNECT fixture run inside a private network namespace.
The fixture accepts only the production Claude OAuth/API hostnames, never
forwards requests, and records token generations rather than credential values.
"""
import hashlib
import http.server
import json
import errno
import os
from pathlib import Path
import queue
import re
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest


MODEL = 'claude-3-5-haiku-20241022'
SECURE_ENV = 'CLAUDE_SECURESTORAGE_CONFIG_DIR'
ALLOWED_HOSTS = {'platform.claude.com', 'api.anthropic.com'}
SYNTHETIC_VALUES = [
    'synthetic-access-0', 'synthetic-access-1', 'synthetic-access-2',
    'synthetic-refresh-0', 'synthetic-refresh-1', 'synthetic-refresh-2',
]


def _clean_text(value):
    if isinstance(value, bytes):
        value = value.decode(errors='replace')
    for secret in SYNTHETIC_VALUES:
        value = value.replace(secret, '[redacted-synthetic-token]')
    return value


def _credential(access_generation, refresh_generation, expires_at):
    return {
        'claudeAiOauth': {
            'accessToken': f'synthetic-access-{access_generation}',
            'refreshToken': f'synthetic-refresh-{refresh_generation}',
            'expiresAt': expires_at,
            'scopes': ['user:inference', 'user:profile', 'user:sessions:claude_code'],
            'subscriptionType': 'max',
            'rateLimitTier': 'default_claude_max_5x',
        }
    }


def _write_credential(directory, access_generation=0, refresh_generation=0,
                      expires_at=0):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / '.credentials.json'
    path.write_text(json.dumps(_credential(
        access_generation, refresh_generation, expires_at)), encoding='utf-8')
    path.chmod(0o600)
    return path


class FixtureState:
    def __init__(self):
        self.lock = threading.Lock()
        self.all_hosts = []
        self.all_rejected_hosts = []
        self.reset('startup')

    def reset(self, scenario, hold_first_refresh=False):
        with getattr(self, 'lock', threading.Lock()):
            self.scenario = scenario
            self.refresh_count = 0
            self.events = []
            self.rejected_hosts = []
            self.connection_count = 0
            self.hold_first_refresh = hold_first_refresh
            self.first_refresh = threading.Event()
            self.release_refresh = threading.Event()

    def connection(self):
        with self.lock:
            self.connection_count += 1
            return self.connection_count

    def arm_refresh_gate(self):
        with self.lock:
            self.hold_first_refresh = True
            self.first_refresh = threading.Event()
            self.release_refresh = threading.Event()

    def event(self, value):
        with self.lock:
            self.events.append({'scenario': self.scenario, **value})

    def refresh(self, payload):
        with self.lock:
            self.refresh_count += 1
            generation = self.refresh_count
            held = self.hold_first_refresh and generation == 1
            old_refresh = payload.get('refresh_token') == 'synthetic-refresh-0'
            new_refresh = payload.get('refresh_token') == 'synthetic-refresh-1'
            self.events.append({
                'scenario': self.scenario,
                'event': 'oauth-refresh',
                'host': 'platform.claude.com',
                'generation': generation,
                'input_refresh_generation': '0' if old_refresh else '1' if new_refresh else 'other',
            })
            if held:
                self.first_refresh.set()
        if held and not self.release_refresh.wait(20):
            return 503, {'error': 'fixture refresh barrier timed out'}
        return 200, {
            'access_token': f'synthetic-access-{generation}',
            'refresh_token': f'synthetic-refresh-{generation}',
            'expires_in': 3600,
            'scope': 'user:inference user:profile user:sessions:claude_code',
            'token_type': 'Bearer',
        }


class FixtureServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_args):
        pass

    def do_CONNECT(self):
        host = self.path.rsplit(':', 1)[0].lower()
        if host not in ALLOWED_HOSTS:
            with self.server.state.lock:
                self.server.state.rejected_hosts.append(host)
                self.server.state.all_rejected_hosts.append(host)
            self.connection.sendall(
                b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            self.close_connection = True
            return
        self.connection.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        self.rfile.close()
        self.wfile.close()
        try:
            self.connection = self.server.tls_context.wrap_socket(
                self.connection, server_side=True)
            self.rfile = self.connection.makefile('rb', self.rbufsize)
            self.wfile = self.connection.makefile('wb', self.wbufsize)
            self.fixture_host = host
            self.connection_id = self.server.state.connection()
            with self.server.state.lock:
                self.server.state.all_hosts.append(host)
        except (OSError, ssl.SSLError):
            self.close_connection = True

    def _answer(self, status, payload, content_type='application/json'):
        body = payload if isinstance(payload, bytes) else json.dumps(
            payload, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    @staticmethod
    def _prompt_label(payload):
        messages = payload.get('messages')
        if not isinstance(messages, list):
            return 'unclassified'
        for message in reversed(messages):
            content = message.get('content') if isinstance(message, dict) else None
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = ' '.join(
                    part.get('text', '') for part in content
                    if isinstance(part, dict) and isinstance(part.get('text'), str))
            else:
                text = ''
            for label in ('legacy-worker', 'empty-override', 'relative-override',
                          'old-worker-before', 'old-worker-after', 'main-login',
                          'legacy-cas-worker', 'legacy-cas-main',
                          'builder-binding', 'split-legacy-child',
                          'post-lock-a', 'post-lock-c',
                          'session-first', 'session-resume', 'late-cas-worker',
                          'main-before', 'worker-a', 'worker-b', 'main-after'):
                if label in text:
                    return label
        return 'unclassified'

    @staticmethod
    def _auth_generation(header):
        token = header.removeprefix('Bearer ').strip()
        for generation in range(3):
            if token == f'synthetic-access-{generation}':
                return str(generation)
        return 'other'

    def do_POST(self):
        size = int(self.headers.get('Content-Length', '0'))
        raw = self.rfile.read(size)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = {}
        host = getattr(self, 'fixture_host', 'unknown')
        if host == 'platform.claude.com' and self.path.startswith('/v1/oauth/token'):
            status, response = self.server.state.refresh(payload)
            self._answer(status, response)
            return

        if self.path.startswith('/v1/messages'):
            label = self._prompt_label(payload)
            generation = self._auth_generation(self.headers.get('Authorization', ''))
            self.server.state.event({
                'event': 'message', 'host': host, 'path': self.path,
                'connection': getattr(self, 'connection_id', 0),
                'prompt': label, 'access_generation': generation,
            })
            required = {
                'legacy-worker': '1', 'empty-override': '1',
                'relative-override': '1', 'main-before': '0',
                'old-worker-before': '1', 'old-worker-after': '1', 'main-login': '2',
                'legacy-cas-worker': '2', 'legacy-cas-main': '2',
                'builder-binding': '1', 'split-legacy-child': '2',
                'post-lock-a': '1', 'post-lock-c': '1',
                'session-first': '1', 'session-resume': '1',
                'worker-a': '1', 'worker-b': '1', 'main-after': '1',
            }.get(label)
            if required is not None and generation != required:
                self._answer(401, {
                    'type': 'error', 'error': {
                        'type': 'authentication_error',
                        'message': 'synthetic fixture rejected unexpected generation',
                    }})
                return
            chunks = [
                ('message_start', {'type': 'message_start', 'message': {
                    'id': 'msg_synthetic', 'type': 'message', 'role': 'assistant',
                    'model': MODEL, 'content': [], 'stop_reason': None,
                    'stop_sequence': None,
                    'usage': {'input_tokens': 1, 'output_tokens': 0}}}),
                ('content_block_start', {'type': 'content_block_start', 'index': 0,
                                         'content_block': {'type': 'text', 'text': ''}}),
                ('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                         'delta': {'type': 'text_delta', 'text': 'OK'}}),
                ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                ('message_delta', {'type': 'message_delta',
                                   'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
                                   'usage': {'output_tokens': 1}}),
                ('message_stop', {'type': 'message_stop'}),
            ]
            data = ''.join(
                f'event: {event}\ndata: {json.dumps(value, separators=(",", ":"))}\n\n'
                for event, value in chunks).encode('utf-8')
            self._answer(200, data, 'text/event-stream')
            return

        self.server.state.event({
            'event': 'other-post', 'host': host, 'path': self.path})
        self._answer(404, {'type': 'error', 'error': {
            'type': 'not_found', 'message': 'synthetic fixture only'}})

    def do_GET(self):
        self.server.state.event({
            'event': 'get', 'host': getattr(self, 'fixture_host', 'unknown'),
            'path': self.path,
            'access_generation': self._auth_generation(
                self.headers.get('Authorization', '')),
        })
        self._answer(404, {'type': 'error', 'error': {
            'type': 'not_found', 'message': 'synthetic fixture only'}})


def _make_certificate(root):
    ca_key = root / 'fixture-ca.key'
    ca_cert = root / 'fixture-ca.crt'
    server_key = root / 'fixture-server.key'
    request = root / 'fixture-server.csr'
    server_cert = root / 'fixture-server.crt'
    extensions = root / 'fixture-server.ext'
    extensions.write_text(
        'subjectAltName=DNS:platform.claude.com,DNS:api.anthropic.com\n'
        'extendedKeyUsage=serverAuth\n', encoding='ascii')
    commands = [
        ['/usr/bin/openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
         '-keyout', str(ca_key), '-out', str(ca_cert), '-days', '1',
         '-subj', '/CN=Hearting Synthetic Claude Test CA'],
        ['/usr/bin/openssl', 'req', '-newkey', 'rsa:2048', '-nodes',
         '-keyout', str(server_key), '-out', str(request),
         '-subj', '/CN=platform.claude.com'],
        ['/usr/bin/openssl', 'x509', '-req', '-in', str(request), '-CA', str(ca_cert),
         '-CAkey', str(ca_key), '-CAcreateserial', '-out', str(server_cert),
         '-days', '1', '-extfile', str(extensions)],
    ]
    for command in commands:
        result = subprocess.run(command, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError('fixture certificate generation failed')
    return ca_cert, server_cert, server_key


def _write_settings(config_home, settings=None):
    config_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_home.chmod(0o700)
    (config_home / 'settings.json').write_text(
        json.dumps(settings or {}), encoding='utf-8')
    (config_home / 'settings.json').chmod(0o600)


def _native_env(home, config, secure_value, proxy_url, ca_cert, cwd):
    env = {
        'PATH': '/usr/bin:/bin', 'HOME': str(home),
        'XDG_CONFIG_HOME': str(home / '.config'),
        'XDG_CACHE_HOME': str(home / '.cache'),
        'XDG_STATE_HOME': str(home / '.state'),
        'CLAUDE_CONFIG_DIR': str(config),
        'HTTPS_PROXY': proxy_url, 'https_proxy': proxy_url,
        'HTTP_PROXY': proxy_url, 'http_proxy': proxy_url,
        'ALL_PROXY': proxy_url, 'all_proxy': proxy_url,
        'NO_PROXY': '', 'no_proxy': '',
        'NODE_EXTRA_CA_CERTS': str(ca_cert),
        'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
        'DISABLE_TELEMETRY': '1', 'DISABLE_ERROR_REPORTING': '1',
        'CI': '1', 'TERM': 'dumb', 'PYTHONDONTWRITEBYTECODE': '1',
    }
    if secure_value is not None:
        env[SECURE_ENV] = secure_value
    forbidden = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
                 'CLAUDE_CODE_OAUTH_TOKEN', 'AWS_ACCESS_KEY_ID',
                 'AWS_SECRET_ACCESS_KEY', 'GOOGLE_APPLICATION_CREDENTIALS')
    if any(key in env for key in forbidden):
        raise RuntimeError('credential environment allowlist failure')
    return env


class NativeHarness:
    def __init__(self, cli, root):
        self.cli = str(cli)
        self.root = Path(root)
        self.state = FixtureState()
        ca_cert, cert, key = _make_certificate(self.root)
        self.ca_cert = ca_cert
        self.tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls_context.load_cert_chain(cert, key)
        self.server = FixtureServer(('127.0.0.1', 0), FixtureHandler)
        self.server.state = self.state
        self.server.tls_context = self.tls_context
        self.port = self.server.server_address[1]
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.processes = []
        self.read_errors = []

    @property
    def proxy_url(self):
        return f'http://127.0.0.1:{self.port}'

    def cli_command(self, trace, *args):
        return [
            '/usr/bin/strace', '-ff', '-qq', '-e', 'trace=%file,execve',
            '-s', '200', '-o', str(trace), self.cli, *args,
        ]

    def run_cli(self, alias, prompt, home, config, secure_value, cwd=None,
                settings=None, session_args=(), persist=False, timeout=35,
                injected_env=None, write_settings=True):
        if write_settings:
            if settings is not None:
                _write_settings(config, settings)
            else:
                _write_settings(config)
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        trace = self.root / f'trace-{alias}'
        env = _native_env(home, config, secure_value, self.proxy_url,
                          self.ca_cert, cwd or self.root)
        if injected_env:
            env.update(injected_env)
        args = ['-p', prompt, '--output-format', 'text', '--model', MODEL,
                '--setting-sources', 'user']
        if not persist:
            args.append('--no-session-persistence')
        args.extend(session_args)
        proc = subprocess.Popen(
            self.cli_command(trace, *args), cwd=str(cwd or self.root), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.processes.append(proc)
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._terminate(proc)
            raise RuntimeError(f'{alias} CLI timed out')
        if proc.returncode != 0:
            raise RuntimeError(
                f'{alias} CLI exit={proc.returncode}: {_clean_text(output[-1200:])}')
        return trace, proc.pid

    def start_stream_cli(self, alias, home, config, secure_value, settings=None):
        if settings is not None:
            _write_settings(config, settings)
        else:
            _write_settings(config)
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        trace = self.root / f'trace-{alias}'
        env = _native_env(home, config, secure_value, self.proxy_url, self.ca_cert,
                          self.root)
        args = ['-p', '--input-format', 'stream-json', '--output-format',
                'stream-json', '--model', MODEL, '--setting-sources', 'user',
                '--no-session-persistence', '--verbose']
        proc = subprocess.Popen(
            self.cli_command(trace, *args), cwd=str(self.root), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True)
        self.processes.append(proc)
        output_queue = queue.Queue()

        def collect():
            for line in proc.stdout:
                output_queue.put(line)
            output_queue.put(None)

        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        return {'alias': alias, 'trace': trace, 'proc': proc,
                'queue': output_queue, 'reader': reader}

    def stream_prompt(self, session, label, timeout=30):
        self.send_stream_prompt(session, label)
        return self.wait_stream_result(session, label, timeout=timeout)

    def send_stream_prompt(self, session, label):
        proc = session['proc']
        if proc.poll() is not None:
            raise RuntimeError(f"{session['alias']} stream process exited early")
        event = {'type': 'user', 'message': {
            'role': 'user', 'content': [{'type': 'text', 'text': label}]}}
        proc.stdin.write(json.dumps(event) + '\n')
        proc.stdin.flush()

    def wait_stream_result(self, session, label, timeout=30):
        proc = session['proc']
        deadline = time.monotonic() + timeout
        observed = []
        while time.monotonic() < deadline:
            try:
                line = session['queue'].get(timeout=min(1, deadline - time.monotonic()))
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if line is None:
                break
            observed.append(line)
            try:
                result = json.loads(line)
            except ValueError:
                continue
            if isinstance(result, dict) and result.get('type') == 'result':
                return result
        raise RuntimeError(
            f"{session['alias']} stream request {label} did not finish: "
            f"{_clean_text(''.join(observed[-8:]))}")

    def close_stream(self, session):
        proc = session['proc']
        if proc.poll() is None:
            proc.stdin.close()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._terminate(proc)
        session['reader'].join(timeout=2)

    @staticmethod
    def _terminate(proc):
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=2)

    def traces(self, prefix):
        files = sorted(self.root.glob(Path(prefix).name + '.*'))
        return {path: path.read_text(encoding='utf-8', errors='replace')
                for path in files}

    def cli_pid(self, prefix, prompt=None, stream=False):
        target = self.cli.replace('\\', '\\\\').replace('"', '\\"')
        if stream:
            marker = f'execve("{target}", ["{target}", "-p", "--input-format", "stream-json"'
        else:
            marker = f'execve("{target}", ["{target}", "-p", "{prompt}"'
        matches = []
        for path, content in self.traces(prefix).items():
            if marker in content:
                matches.append(path.name.rsplit('.', 1)[-1])
        return matches

    def cleanup(self):
        for proc in reversed(self.processes):
            self._terminate(proc)
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=3)


def _read_generations(path, errors, stop):
    observed = []
    while not stop.is_set():
        try:
            content = path.read_bytes()
            value = json.loads(content)
            token = value['claudeAiOauth']['accessToken']
            if token not in ('synthetic-access-0', 'synthetic-access-1'):
                errors.append('unexpected synthetic generation')
            observed.append(token.rsplit('-', 1)[-1])
        except FileNotFoundError:
            errors.append('canonical credential path missing during replacement')
        except (OSError, ValueError, KeyError, TypeError):
            errors.append('canonical credential file was incomplete')
        time.sleep(0.001)
    return observed


def _trace_text(harness, alias):
    return '\n'.join(harness.traces(harness.root / f'trace-{alias}').values())


def _observed_lock_paths(trace_text):
    return sorted(set(re.findall(r'"([^"\n]*\.lock[^"\n]*)"', trace_text)))


def _reread_after_lock_contention(trace_text, lock_path):
    lines = trace_text.splitlines()
    contended = [index for index, line in enumerate(lines)
                 if lock_path in line and ('mkdir(' in line or 'mkdirat(' in line)
                 and 'EEXIST' in line]
    credential_opens = [index for index, line in enumerate(lines)
                        if '.credentials.json' in line and 'openat' in line]
    if not contended or not credential_opens:
        return False, {'contention': bool(contended),
                       'credential_open_count': len(credential_opens)}
    first_wait = contended[0]
    before = any(index < first_wait for index in credential_opens)
    after = any(index > first_wait for index in credential_opens)
    return before and after, {
        'contended_shared_refresh_lock': True,
        'credential_open_before_and_after_contention': before and after,
        'credential_open_count': len(credential_opens),
        'successful_reacquire_observed': any(
            lock_path in line and ('mkdir(' in line or 'mkdirat(' in line)
            and '= 0' in line for line in lines[first_wait + 1:]),
    }


def _lock_mkdir_lines(trace_text, lock_paths):
    return [line for line in trace_text.splitlines()
            if any(f'"{path}"' in line for path in lock_paths)
            and ('mkdir(' in line or 'mkdirat(' in line)]


def _wait_for_pending_lock_mkdir(harness, alias, lock_paths, timeout=15):
    """Return the thread id whose delayed lock mkdir is printed but not returned."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        traces = harness.traces(harness.root / f'trace-{alias}')
        for path, content in traces.items():
            if any(' = ' not in line for line in _lock_mkdir_lines(content, lock_paths)):
                return path.name.rsplit('.', 1)[-1]
        if _lock_mkdir_lines('\n'.join(traces.values()), lock_paths):
            raise RuntimeError(f'{alias} completed its lock mkdir before the pause')
        time.sleep(0.03)
    raise RuntimeError(f'{alias} did not reach the native refresh lock')


def _thread_group(tid):
    status = Path(f'/proc/{tid}/status').read_text(encoding='ascii')
    return re.search(r'^Tgid:\s+(\d+)$', status, re.M).group(1)


def _watch_child_threads(tracer_pid, seen, stop):
    """Record (process, thread) ids of a tracer's children while they run."""
    while not stop.is_set():
        try:
            children = Path(f'/proc/{tracer_pid}/task/{tracer_pid}/children').read_text(
                encoding='ascii').split()
        except OSError:
            children = []
        for child in children:
            try:
                seen.update((child, tid) for tid in os.listdir(f'/proc/{child}/task'))
            except OSError:
                pass
        time.sleep(0.005)


def _timed_lines(harness, alias, threads):
    """Return (timestamp, line) from the given threads' strace -ttt files."""
    events = []
    for path, content in harness.traces(harness.root / f'trace-{alias}').items():
        if path.name.rsplit('.', 1)[-1] not in threads:
            continue
        for line in content.splitlines():
            stamp = line.split(' ', 1)[0]
            if re.fullmatch(r'\d+\.\d+', stamp):
                events.append((float(stamp), line))
    return sorted(events)


def _wait_for_lock_contention(harness, alias, lock_path, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lines = _trace_text(harness, alias).splitlines()
        if any(lock_path in line and 'EEXIST' in line for line in lines):
            return
        time.sleep(0.03)
    raise RuntimeError(f'{alias} did not contend on the native refresh lock')


def _native_child(cli, root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    probe = socket.socket()
    probe.settimeout(0.5)
    egress = probe.connect_ex(('198.51.100.1', 443))
    probe.close()
    if egress not in (errno.ENETUNREACH, errno.EHOSTUNREACH):
        raise RuntimeError('private namespace unexpectedly has external egress')

    harness = NativeHarness(cli, root)
    summary = {
        'binary': cli,
        'binary_sha256': hashlib.sha256(Path(cli).read_bytes()).hexdigest(),
        'egress_probe': 'blocked',
        'transport': 'loopback-CONNECT; production-hostnames; fixture-CA-trusted',
        'scenarios': {},
    }
    try:
        # Reproduce the old link layout with synthetic credentials only.
        harness.state.reset('legacy-symlink')
        legacy_home = root / 'legacy-home'
        legacy_main = legacy_home / 'main-config'
        legacy_worker = legacy_home / 'worker-config'
        expiry = int(time.time() * 1000) - 60_000
        main_credential = _write_credential(legacy_main, expires_at=expiry)
        legacy_worker.mkdir(parents=True, mode=0o700)
        legacy_worker.chmod(0o700)
        (legacy_worker / 'settings.json').write_text('{}', encoding='utf-8')
        (legacy_worker / '.credentials.json').symlink_to(main_credential)
        before_main = main_credential.read_bytes()
        before_link_inode = (legacy_worker / '.credentials.json').lstat().st_ino
        trace, parent_pid = harness.run_cli(
            'legacy', 'legacy-worker', legacy_home, legacy_worker, None)
        legacy_after = json.loads((legacy_worker / '.credentials.json').read_text())
        if main_credential.read_bytes() != before_main:
            raise RuntimeError('legacy refresh unexpectedly changed main credentials')
        if (legacy_worker / '.credentials.json').is_symlink():
            raise RuntimeError('native atomic write did not replace the worker symlink')
        if (legacy_worker / '.credentials.json').stat().st_ino == before_link_inode:
            raise RuntimeError('worker credential inode did not change on native write')
        if legacy_after['claudeAiOauth']['accessToken'] != 'synthetic-access-1':
            raise RuntimeError('legacy worker did not store the synthetic refresh result')
        if (legacy_worker / '.credentials.json').stat().st_mode & 0o777 != 0o600:
            raise RuntimeError('legacy credential permissions were not 0600')
        summary['scenarios']['legacy_symlink'] = {
            'actual_cli_pid': harness.cli_pid(trace, prompt='legacy-worker'),
            'parent_pid_alias': parent_pid,
            'worker_link_replaced': True,
            'main_unchanged': True,
            'refresh_requests': harness.state.refresh_count,
        }

        # Verify the CLI's explicit empty override selects HOME/.claude.
        harness.state.reset('empty-override')
        empty_home = root / 'empty-home'
        empty_store = empty_home / '.claude'
        empty_config = empty_home / 'masked-config'
        empty_cred = _write_credential(empty_store, expires_at=expiry)
        trace, parent_pid = harness.run_cli(
            'empty', 'empty-override', empty_home, empty_config, '')
        if json.loads(empty_cred.read_text())['claudeAiOauth']['accessToken'] != 'synthetic-access-1':
            raise RuntimeError('empty override did not use synthetic HOME/.claude store')
        if (empty_config / '.credentials.json').exists() or (empty_config / '.oauth_refresh.lock').exists():
            raise RuntimeError('empty override wrote auth state into masked config home')
        summary['scenarios']['empty_override'] = {
            'actual_cli_pid': harness.cli_pid(trace, prompt='empty-override'),
            'parent_pid_alias': parent_pid,
            'store': 'HOME/.claude', 'refresh_requests': harness.state.refresh_count,
        }

        # A relative native path is interpreted from the CLI process cwd.
        harness.state.reset('relative-override')
        relative_home = root / 'relative-home'
        relative_config = relative_home / 'masked-config'
        relative_store = root / 'relative-auth'
        relative_cred = _write_credential(relative_store, expires_at=expiry)
        trace, parent_pid = harness.run_cli(
            'relative', 'relative-override', relative_home, relative_config,
            'relative-auth', cwd=root)
        if json.loads(relative_cred.read_text())['claudeAiOauth']['accessToken'] != 'synthetic-access-1':
            raise RuntimeError('relative override did not resolve from the CLI cwd')
        if (relative_config / '.credentials.json').exists():
            raise RuntimeError('relative override wrote auth into masked config home')
        summary['scenarios']['relative_override'] = {
            'actual_cli_pid': harness.cli_pid(trace, prompt='relative-override'),
            'parent_pid_alias': parent_pid, 'store': 'caller-cwd/relative-auth',
            'refresh_requests': harness.state.refresh_count,
        }

        # Exercise the real common builder's returned child environment at an
        # actual installed CLI spawn, mirroring dispatch-headless env.update().
        harness.state.reset('builder-spawn-consumer')
        import worker_runtime_home
        builder_source = root / 'builder-main-config'
        builder_home = root / 'builder-cli-home'
        builder_credential = _write_credential(builder_source, expires_at=expiry)
        _write_settings(builder_source, {
            'env': {'USER_GUARD': '1'},
            'permissions': {'deny': ['Read(secret)']},
        })
        builder_parent_env = {
            'HOME': str(builder_home),
            'CLAUDE_CONFIG_DIR': str(builder_source),
            'AGENT_DISPATCH_JOBS': str(root / 'dispatch/jobs.log'),
        }
        built_env = worker_runtime_home.prepare_worker_home(
            Path(__file__).resolve().parents[1], 'claude', 'stage',
            'native-spawn-consumer', env=builder_parent_env)
        built_config = Path(built_env['CLAUDE_CONFIG_DIR'])
        if built_env.get(SECURE_ENV) != str(builder_source.resolve()):
            raise RuntimeError('common builder did not bind the source auth directory')
        if built_config == builder_source or (built_config / '.credentials.json').exists():
            raise RuntimeError('common builder did not keep the config home masked')
        built_settings = json.loads((built_config / 'settings.json').read_text())
        if built_settings.get('env', {}).get('USER_GUARD') != '1' or \
                built_settings.get('permissions') != {'deny': ['Read(secret)']}:
            raise RuntimeError('common builder changed user guard settings')
        trace, parent_pid = harness.run_cli(
            'builder-binding', 'builder-binding', builder_home, built_config,
            built_env[SECURE_ENV], injected_env=built_env, write_settings=False)
        if json.loads(builder_credential.read_text())['claudeAiOauth'][
                'accessToken'] != 'synthetic-access-1':
            raise RuntimeError('actual CLI did not consume the builder auth binding')
        if (built_config / '.credentials.json').exists() or \
                (built_config / '.oauth_refresh.lock').exists():
            raise RuntimeError('actual CLI wrote auth state under its masked config home')
        summary['scenarios']['builder_spawn_consumer'] = {
            'actual_cli_pid': harness.cli_pid(trace, prompt='builder-binding'),
            'builder_binding': 'source-config',
            'masked_config_home_distinct': True,
            'user_guard_settings_preserved': True,
            'source_store_updated': True,
            'masked_auth_files_absent': True,
        }

        # Resume a persisted synthetic session through the real CLI while
        # keeping config and native auth stores separate.
        harness.state.reset('resume-transport')
        resume_home = root / 'resume-home'
        resume_config = resume_home / 'masked-config'
        resume_store = root / 'resume-auth'
        resume_credential = _write_credential(
            resume_store, access_generation=1, refresh_generation=1,
            expires_at=int(time.time() * 1000) + 24 * 60 * 60 * 1000)
        session_id = '11111111-1111-4111-8111-111111111111'
        first_trace, first_parent = harness.run_cli(
            'session-first', 'session-first', resume_home, resume_config,
            str(resume_store), session_args=['--session-id', session_id], persist=True)
        resume_trace, resume_parent = harness.run_cli(
            'session-resume', 'session-resume', resume_home, resume_config,
            str(resume_store), session_args=['--resume', session_id], persist=True)
        resume_messages = [event for event in harness.state.events
                           if event.get('event') == 'message']
        resume_generations = {event['prompt']: event['access_generation']
                              for event in resume_messages}
        if resume_generations != {'session-first': '1', 'session-resume': '1'}:
            raise RuntimeError('actual CLI resume did not use the shared synthetic auth store')
        if json.loads(resume_credential.read_text())['claudeAiOauth'][
                'accessToken'] != 'synthetic-access-1':
            raise RuntimeError('resume changed the native credential generation')
        if (resume_config / '.credentials.json').exists():
            raise RuntimeError('resume copied auth into the masked config home')
        summary['scenarios']['resume_transport'] = {
            'actual_cli_pids': {
                'first': harness.cli_pid(first_trace, prompt='session-first'),
                'resume': harness.cli_pid(resume_trace, prompt='session-resume'),
            },
            'session_id': 'synthetic-session-1',
            'message_auth_generations': resume_generations,
            'refresh_requests': harness.state.refresh_count,
            'shared_auth_store': True,
        }

        # Hold a real CLI refresh response, atomically publish a newer
        # synthetic main login, then let the stale response reach the CLI's
        # native refresh-token compare-and-swap.
        harness.state.reset('late-refresh-vs-main-login', hold_first_refresh=True)
        cas_home = root / 'cas-home'
        cas_config = cas_home / 'masked-config'
        cas_store = root / 'cas-auth'
        cas_credential = _write_credential(cas_store, expires_at=0)
        _write_settings(cas_config)
        cas_trace = root / 'trace-late-cas'
        cas_env = _native_env(cas_home, cas_config, str(cas_store),
                              harness.proxy_url, harness.ca_cert, root)
        cas_proc = subprocess.Popen(
            harness.cli_command(
                cas_trace, '-p', 'late-cas-worker', '--output-format', 'text',
                '--model', MODEL, '--setting-sources', 'user',
                '--no-session-persistence'),
            cwd=str(root), env=cas_env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        harness.processes.append(cas_proc)
        if not harness.state.first_refresh.wait(15):
            raise RuntimeError('stale CLI refresh did not reach the CAS barrier')
        login_value = _credential(
            2, 2, int(time.time() * 1000) + 24 * 60 * 60 * 1000)
        login_temp = cas_store / '.credentials.json.synthetic-login'
        login_temp.write_text(json.dumps(login_value), encoding='utf-8')
        login_temp.chmod(0o600)
        with login_temp.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(login_temp, cas_credential)
        main_login_bytes = cas_credential.read_bytes()
        harness.state.release_refresh.set()
        try:
            cas_output, _ = cas_proc.communicate(timeout=35)
        except subprocess.TimeoutExpired:
            harness._terminate(cas_proc)
            raise RuntimeError('late-CAS CLI process timed out')
        if cas_proc.returncode != 0:
            raise RuntimeError(f'late-CAS CLI failed: {_clean_text(cas_output[-1200:])}')
        if cas_credential.read_bytes() != main_login_bytes:
            raise RuntimeError('late stale refresh overwrote the synthetic main login')
        cas_after = json.loads(cas_credential.read_text())['claudeAiOauth']
        if cas_after.get('accessToken') != 'synthetic-access-2' or \
                cas_after.get('refreshToken') != 'synthetic-refresh-2':
            raise RuntimeError('native CAS did not preserve the newer main token pair')
        cas_messages = [event for event in harness.state.events
                        if event.get('event') == 'message']
        if not any(event.get('prompt') == 'late-cas-worker' and
                   event.get('access_generation') in ('1', '2')
                   for event in cas_messages):
            raise RuntimeError('late-CAS worker did not complete with a synthetic token')
        summary['scenarios']['late_refresh_cas'] = {
            'actual_cli_pid': harness.cli_pid(cas_trace, prompt='late-cas-worker'),
            'refresh_requests': harness.state.refresh_count,
            'main_token_pair_preserved': True,
            'newer_generation': '2',
            'worker_message_generation': next(
                event['access_generation'] for event in cas_messages
                if event.get('prompt') == 'late-cas-worker'),
            'newer_main_write_survived_stale_refresh': True,
        }

        # Exercise the old running-worker case while its original credential
        # symlink is still intact: the main CLI rotates the shared file while
        # the worker's real refresh response is held at the fixture.
        harness.state.reset('legacy-running-cas', hold_first_refresh=True)
        legacy_cas_home = root / 'legacy-cas-home'
        legacy_cas_main = legacy_cas_home / 'main-config'
        legacy_cas_worker_config = legacy_cas_home / 'worker-config'
        legacy_cas_credential = _write_credential(legacy_cas_main, expires_at=0)
        legacy_cas_worker_config.mkdir(parents=True, mode=0o700)
        legacy_cas_worker_config.chmod(0o700)
        (legacy_cas_worker_config / '.credentials.json').symlink_to(
            legacy_cas_credential)
        legacy_cas_worker = harness.start_stream_cli(
            'legacy-cas-worker', legacy_cas_home, legacy_cas_worker_config, None)
        harness.send_stream_prompt(legacy_cas_worker, 'legacy-cas-worker')
        if not harness.state.first_refresh.wait(15):
            raise RuntimeError('old worker did not reach its held native refresh')
        legacy_cas_main_trace, _ = harness.run_cli(
            'legacy-cas-main', 'legacy-cas-main', legacy_cas_home,
            legacy_cas_main, str(legacy_cas_main))
        main_cas_bytes = legacy_cas_credential.read_bytes()
        if json.loads(main_cas_bytes)['claudeAiOauth']['accessToken'] != \
                'synthetic-access-2':
            raise RuntimeError('main CLI did not publish the newer synthetic generation')
        if not (legacy_cas_worker_config / '.credentials.json').is_symlink():
            raise RuntimeError('old worker detached before the main rotation')
        harness.state.release_refresh.set()
        harness.wait_stream_result(legacy_cas_worker, 'legacy-cas-worker')
        if legacy_cas_credential.read_bytes() != main_cas_bytes:
            raise RuntimeError('old worker late response replaced the main token pair')
        if not (legacy_cas_worker_config / '.credentials.json').is_symlink():
            raise RuntimeError('old worker CAS failure unexpectedly replaced the link')
        legacy_cas_messages = [event for event in harness.state.events
                               if event.get('event') == 'message']
        legacy_cas_generations = {event['prompt']: event['access_generation']
                                  for event in legacy_cas_messages}
        if legacy_cas_generations.get('legacy-cas-main') != '2' or \
                legacy_cas_generations.get('legacy-cas-worker') != '2':
            raise RuntimeError('old worker did not adopt the latest linked main token')
        summary['scenarios']['legacy_running_refresh_cas'] = {
            'actual_cli_pids': {
                'old_worker': harness.cli_pid(legacy_cas_worker['trace'], stream=True),
                'main': harness.cli_pid(legacy_cas_main_trace, prompt='legacy-cas-main'),
            },
            'refresh_requests': harness.state.refresh_count,
            'credential_link_survived': True,
            'main_token_pair_preserved': True,
            'old_worker_message_generation': legacy_cas_generations['legacy-cas-worker'],
            'main_message_generation': legacy_cas_generations['legacy-cas-main'],
        }

        # Keep a main CLI process alive with generation 0 cached while two
        # independent worker CLIs contend for the native refresh lock.
        harness.state.reset('shared-store-race')
        race_home = root / 'race-home'
        store = root / 'canonical-auth'
        decoy = root / 'settings-env-decoy'
        config_main = race_home / 'main-config'
        config_a = race_home / 'worker-a-config'
        config_b = race_home / 'worker-b-config'
        for path in (race_home, decoy, config_main, config_a, config_b):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        expires_at = int(time.time() * 1000) + 24 * 60 * 60 * 1000
        canonical = _write_credential(store, expires_at=expires_at)
        settings = {'env': {SECURE_ENV: str(store)}}
        main = harness.start_stream_cli(
            'main', race_home, config_main, str(decoy), settings=settings)
        harness.stream_prompt(main, 'main-before')
        if main['proc'].poll() is not None:
            raise RuntimeError('main stream process did not remain alive after first request')
        if json.loads(canonical.read_text())['claudeAiOauth']['accessToken'] != 'synthetic-access-0':
            raise RuntimeError('main did not begin with the original synthetic token')
        _write_credential(store, expires_at=0)
        initial_inode = canonical.stat().st_ino
        harness.state.arm_refresh_gate()

        observed = []
        stop_observer = threading.Event()
        observer = threading.Thread(target=lambda: observed.extend(
            _read_generations(canonical, harness.read_errors, stop_observer)), daemon=True)
        observer.start()
        trace_a = root / 'trace-worker-a'
        env_a = _native_env(race_home, config_a, str(store), harness.proxy_url,
                            harness.ca_cert, root)
        proc_a = subprocess.Popen(
            harness.cli_command(trace_a, '-p', 'worker-a', '--output-format', 'text',
                                '--model', MODEL, '--setting-sources', 'user',
                                '--no-session-persistence'),
            cwd=str(root), env=env_a, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        harness.processes.append(proc_a)
        if not harness.state.first_refresh.wait(15):
            raise RuntimeError('worker A did not reach the controlled OAuth endpoint')

        trace_b = root / 'trace-worker-b'
        env_b = _native_env(race_home, config_b, str(store), harness.proxy_url,
                            harness.ca_cert, root)
        proc_b = subprocess.Popen(
            harness.cli_command(trace_b, '-p', 'worker-b', '--output-format', 'text',
                                '--model', MODEL, '--setting-sources', 'user',
                                '--no-session-persistence'),
            cwd=str(root), env=env_b, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        harness.processes.append(proc_b)
        current_lock = str(store / '.oauth_refresh.lock')
        _wait_for_lock_contention(harness, 'worker-b', current_lock)
        if proc_b.poll() is not None:
            raise RuntimeError('worker B exited while worker A held the native lock')
        if harness.state.refresh_count != 1:
            a_lock = '\\n'.join(line for line in _trace_text(harness, 'worker-a').splitlines()
                                 if '.oauth_refresh.lock' in line or '.lock' in line)
            b_lock = '\\n'.join(line for line in _trace_text(harness, 'worker-b').splitlines()
                                 if '.oauth_refresh.lock' in line or '.lock' in line)
            raise RuntimeError(
                'duplicate refresh request reached controlled server; '
                f'ledger={json.dumps(harness.state.events)}; A-lock={a_lock[-1800:]}; '
                f'B-lock={b_lock[-1800:]}')
        trace_b_text = _trace_text(harness, 'worker-b')
        if '.credentials.json' not in trace_b_text:
            raise RuntimeError('worker B did not access the canonical credential file')

        harness.state.release_refresh.set()
        out_a, _ = proc_a.communicate(timeout=35)
        out_b, _ = proc_b.communicate(timeout=35)
        if proc_a.returncode != 0:
            raise RuntimeError(f'worker A failed: {_clean_text(out_a[-1200:])}')
        if proc_b.returncode != 0:
            raise RuntimeError(f'worker B failed: {_clean_text(out_b[-1200:])}')

        stop_observer.set()
        observer.join(timeout=2)
        if harness.read_errors:
            raise RuntimeError('reader observed a missing or incomplete credential file')
        if not observed or set(observed) - {'0', '1'}:
            raise RuntimeError('atomic observer saw an unexpected credential generation')
        if canonical.stat().st_ino == initial_inode:
            raise RuntimeError('native refresh did not atomically replace the canonical inode')
        if canonical.stat().st_mode & 0o777 != 0o600:
            raise RuntimeError('canonical credential permissions were not 0600')
        write_lock = str(store / '.storage-write.lock')
        trace_a_text = _trace_text(harness, 'worker-a')
        trace_b_text = _trace_text(harness, 'worker-b')
        if current_lock not in trace_a_text or current_lock not in trace_b_text:
            raise RuntimeError('both workers did not use the same native refresh lock')
        if write_lock not in trace_a_text:
            raise RuntimeError('native credential replacement did not use storage-write lock')
        reread_after_contention, reread_evidence = _reread_after_lock_contention(
            trace_b_text, current_lock)
        if not reread_after_contention:
            b_events = [line for line in trace_b_text.splitlines()
                        if '.credentials.json' in line or '.oauth_refresh.lock' in line]
            raise RuntimeError(
                'worker B trace did not reread canonical credentials after lock contention: '
                + json.dumps(reread_evidence) + '; events=' + '\\n'.join(b_events[-80:]))
        if (config_a / '.credentials.json').exists() or (config_b / '.credentials.json').exists():
            raise RuntimeError('a worker config home received a credential file')
        if any((path / '.oauth_refresh.lock').exists() for path in (config_a, config_b)):
            raise RuntimeError('a worker config home received a refresh lock')

        # The same already-running main process makes a later request after
        # the workers' refresh and must use the newly stored access token.
        main_pid = harness.cli_pid(main['trace'], stream=True)
        if len(main_pid) != 1:
            raise RuntimeError('could not identify one persistent main CLI process')
        harness.stream_prompt(main, 'main-after')
        if main['proc'].poll() is not None:
            raise RuntimeError('main CLI exited before its later request completed')
        messages = [event for event in harness.state.events
                    if event.get('event') == 'message']
        by_prompt = {event['prompt']: event['access_generation'] for event in messages}
        expected = {'main-before': '0', 'worker-a': '1',
                    'worker-b': '1', 'main-after': '1'}
        if any(by_prompt.get(key) != value for key, value in expected.items()):
            raise RuntimeError('main/worker requests did not reuse the latest access token')
        if harness.state.refresh_count != 1:
            raise RuntimeError('workers issued more than one native refresh request')
        if decoy.joinpath('.credentials.json').exists():
            raise RuntimeError('settings env did not override the inherited decoy path')
        if len({str(config_main), str(config_a), str(config_b)}) != 3:
            raise RuntimeError('worker and main config homes are not isolated')
        rejected = list(harness.state.all_rejected_hosts)
        if rejected:
            raise RuntimeError('CLI attempted nonfixture proxy host(s): ' + ','.join(rejected))
        summary['scenarios']['shared_store_race'] = {
            'actual_cli_pids': {
                'main': main_pid,
                'worker_a': harness.cli_pid(trace_a, prompt='worker-a'),
                'worker_b': harness.cli_pid(trace_b, prompt='worker-b'),
            },
            'config_homes_distinct': True,
            'canonical_inode_changed': True,
            'canonical_mode': '0600',
            'complete_generations_observed': sorted(set(observed)),
            'refresh_requests': harness.state.refresh_count,
            'message_auth_generations': by_prompt,
            'same_main_process_before_after': True,
            'settings_env_overrode_decoy': True,
            'refresh_lock_path_shared': True,
            'storage_write_lock_observed': True,
            'worker_b_post_lock_credential_reread': reread_evidence,
            'worker_a_lock_paths': _observed_lock_paths(trace_a_text),
            'worker_b_lock_paths': _observed_lock_paths(trace_b_text),
        }

        # Pause worker C inside its first native refresh-lock mkdir, after it
        # has read the expired credential and decided to refresh. While C is
        # held at that syscall entry, actual CLI A refreshes and releases the
        # same lock. C then acquires the lock successfully and must reread the
        # canonical file while holding it and use A's token without a second
        # refresh. The CLI performs file I/O on pool threads, so ordering is
        # judged by strace timestamps across the threads of C's own process.
        harness.state.reset('post-lock-reread')
        reread_home = root / 'post-lock-home'
        reread_store = root / 'post-lock-auth'
        reread_config_a = reread_home / 'worker-a-config'
        reread_config_c = reread_home / 'worker-c-config'
        reread_credential = _write_credential(reread_store, expires_at=0)
        for path in (reread_config_a, reread_config_c):
            _write_settings(path)
        reread_locks = [str(reread_store / '.oauth_refresh.lock'),
                        str(reread_store.resolve()) + '.lock']
        pause_us = 10_000_000
        trace_c = root / 'trace-post-lock-c'
        command_c = [
            '/usr/bin/strace', '-ff', '-qq', '-ttt', '-T', '-e', 'trace=%file,execve',
            '-P', reread_locks[0], '-P', str(reread_credential), '-P', harness.cli,
            '-e', f'inject=mkdir,mkdirat:delay_enter={pause_us}:when=1',
            '-s', '200', '-o', str(trace_c), harness.cli,
            '-p', 'post-lock-c', '--output-format', 'text', '--model', MODEL,
            '--setting-sources', 'user', '--no-session-persistence',
        ]
        env_c = _native_env(reread_home, reread_config_c, str(reread_store),
                            harness.proxy_url, harness.ca_cert, root)
        proc_c = subprocess.Popen(
            command_c, cwd=str(root), env=env_c, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        harness.processes.append(proc_c)
        c_threads = set()
        stop_threads = threading.Event()
        thread_watcher = threading.Thread(target=_watch_child_threads, args=(
            proc_c.pid, c_threads, stop_threads), daemon=True)
        thread_watcher.start()
        paused_tid = _wait_for_pending_lock_mkdir(harness, 'post-lock-c', reread_locks)
        paused_process = _thread_group(paused_tid)
        if harness.state.refresh_count != 0:
            raise RuntimeError('worker C refreshed before reaching the native lock')
        trace_a_reread, _ = harness.run_cli(
            'post-lock-a', 'post-lock-a', reread_home, reread_config_a,
            str(reread_store), write_settings=False)
        if harness.state.refresh_count != 1:
            raise RuntimeError('worker A did not perform exactly one refresh')
        if json.loads(reread_credential.read_text())['claudeAiOauth'][
                'accessToken'] != 'synthetic-access-1':
            raise RuntimeError('worker A did not publish the refreshed credential')
        pending = _lock_mkdir_lines(_trace_text(harness, 'post-lock-c'), reread_locks)
        if not any(' = ' not in line for line in pending):
            raise RuntimeError('worker C left its paused lock mkdir before A finished')
        out_c, _ = proc_c.communicate(timeout=40)
        stop_threads.set()
        thread_watcher.join(timeout=2)
        if proc_c.returncode != 0:
            raise RuntimeError(f'worker C failed: {_clean_text(out_c[-1200:])}')
        trace_c_text = _trace_text(harness, 'post-lock-c')
        c_process = harness.cli_pid(trace_c, prompt='post-lock-c')
        if c_process != [paused_process]:
            raise RuntimeError('paused lock thread does not belong to worker C')
        own_threads = {tid for process, tid in c_threads if process == paused_process}
        if paused_tid not in own_threads:
            raise RuntimeError('worker C thread inventory missed the paused thread')
        if [line for line in _lock_mkdir_lines(trace_c_text, reread_locks)
                if 'EEXIST' in line or ('(DELAYED)' in line and ' = 0 (DELAYED)' not in line)]:
            raise RuntimeError('worker C contended instead of acquiring the free lock')
        timeline = _timed_lines(harness, 'post-lock-c', own_threads)
        acquired = [(stamp, line) for stamp, line in timeline
                    if line in _lock_mkdir_lines(line, reread_locks[:1])
                    and ' = 0 (DELAYED)' in line]
        released = [stamp for stamp, line in timeline
                    if f'rmdir("{reread_locks[0]}")' in line and ' = 0 ' in line]
        if len(acquired) != 1 or len(released) != 1:
            raise RuntimeError('worker C did not acquire and release the lock once')
        lock_returned = acquired[0][0] + float(
            re.search(r'<([0-9.]+)>\s*$', acquired[0][1]).group(1))
        rereads = [stamp for stamp, line in timeline
                   if '.credentials.json' in line and 'openat(' in line
                   and ' = -1 ' not in line and lock_returned <= stamp <= released[0]]
        if not rereads:
            raise RuntimeError('worker C did not reread credentials while holding the lock')
        reread_delay_ms = round((rereads[0] - lock_returned) * 1000, 3)
        c_generations = [event['access_generation'] for event in harness.state.events
                         if event.get('event') == 'message' and
                         event.get('prompt') == 'post-lock-c']
        if not c_generations or set(c_generations) != {'1'}:
            raise RuntimeError('worker C did not use the token reread under the lock')
        if harness.state.refresh_count != 1:
            raise RuntimeError('worker C issued a duplicate refresh after the lock reread')
        if (reread_config_a / '.credentials.json').exists() or \
                (reread_config_c / '.credentials.json').exists():
            raise RuntimeError('a worker config home received a credential file')
        summary['scenarios']['post_lock_reread'] = {
            'actual_cli_pids': {
                'worker_a': harness.cli_pid(trace_a_reread, prompt='post-lock-a'),
                'worker_c': harness.cli_pid(trace_c, prompt='post-lock-c'),
            },
            'worker_c_paused_at_lock_mkdir_us': pause_us,
            'paused_thread_in_worker_c_process': True,
            'reread_under_lock_after_return_ms': reread_delay_ms,
            'reread_opens_while_lock_held': len(rereads),
            'worker_a_refreshed_while_c_paused': True,
            'successful_reacquire_then_reread': True,
            'worker_c_lock_contention': False,
            'worker_c_access_generations': sorted(set(c_generations)),
            'refresh_requests': harness.state.refresh_count,
        }

        # Keep a legacy worker alive across a main login. Its old environment
        # still resolves the masked directory, and its native rename has already
        # detached its credential file from the main store.
        harness.state.reset('old-running-worker-transition')
        old_home = root / 'old-running-home'
        old_main_config = old_home / 'main-config'
        old_worker_config = old_home / 'worker-config'
        old_main_credential = _write_credential(old_main_config, expires_at=0)
        old_worker_config.mkdir(parents=True, mode=0o700)
        old_worker_config.chmod(0o700)
        (old_worker_config / '.credentials.json').symlink_to(old_main_credential)
        old_main_before = old_main_credential.read_bytes()
        old_worker = harness.start_stream_cli(
            'old-worker', old_home, old_worker_config, None)
        harness.stream_prompt(old_worker, 'old-worker-before')
        if old_worker['proc'].poll() is not None:
            raise RuntimeError('legacy worker did not remain alive after its first request')
        if (old_worker_config / '.credentials.json').is_symlink():
            raise RuntimeError('legacy running worker did not split on native atomic write')
        if old_main_credential.read_bytes() != old_main_before:
            raise RuntimeError('legacy running worker changed main credentials')

        main_trace, main_parent = harness.run_cli(
            'new-main-login', 'main-login', old_home, old_main_config,
            str(old_main_config))
        main_login_generation = json.loads(old_main_credential.read_text())[
            'claudeAiOauth']['accessToken']
        if main_login_generation != 'synthetic-access-2':
            raise RuntimeError('new main login did not publish generation 2')
        main_after_login = old_main_credential.read_bytes()
        harness.stream_prompt(old_worker, 'old-worker-after')
        if old_worker['proc'].poll() is not None:
            raise RuntimeError('legacy running worker did not survive the main login')
        if old_main_credential.read_bytes() != main_after_login:
            raise RuntimeError('legacy running worker overwrote the newer main login')
        old_worker_file = json.loads((old_worker_config / '.credentials.json').read_text())[
            'claudeAiOauth']['accessToken']
        if old_worker_file != 'synthetic-access-1':
            raise RuntimeError('legacy running worker unexpectedly rebound to main storage')
        old_messages = [event for event in harness.state.events
                        if event.get('event') == 'message']
        old_generations = {event['prompt']: event['access_generation']
                           for event in old_messages}
        if old_generations.get('old-worker-before') != '1' or \
                old_generations.get('main-login') != '2' or \
                old_generations.get('old-worker-after') != '1':
            raise RuntimeError('old worker/main generation separation was not observed')
        summary['scenarios']['old_running_worker_transition'] = {
            'actual_cli_pids': {
                'old_worker': harness.cli_pid(old_worker['trace'], stream=True),
                'main_login': harness.cli_pid(main_trace, prompt='main-login'),
            },
            'main_generation_after_login': '2',
            'old_worker_generation_after_login': '1',
            'old_worker_remained_alive': True,
            'main_login_not_overwritten': True,
            'old_process_environment_rebound': False,
            'generation_labels': old_generations,
        }

        # A new launch from that split legacy worker must use the main store.
        # Mark the old home as the legacy builder did and build the child from
        # the old worker's own launch environment into a reused home that still
        # holds an expired split credential. Both split files stay untouched.
        harness.state.reset('split-legacy-parent-new-launch')
        (old_worker_config / 'worker-home.json').write_text(json.dumps({
            'harness': 'claude', 'worker_type': 'owner'}), encoding='utf-8')
        old_worker_split = (old_worker_config / '.credentials.json').read_bytes()
        main_before_child = old_main_credential.read_bytes()
        reused_home = root / 'split-reused-worker-home'
        reused_split_path = _write_credential(
            reused_home, access_generation=1, refresh_generation=1, expires_at=0)
        (reused_home / 'worker-home.json').write_text(
            json.dumps({'harness': 'claude'}), encoding='utf-8')
        reused_split = reused_split_path.read_bytes()
        legacy_parent_env = {
            'HOME': str(old_home),
            'CLAUDE_CONFIG_DIR': str(old_worker_config),
            'HEARTING_WORKER_HOME': str(old_worker_config),
            'HEARTING_MODEL_CONFIG_HOME': str(old_main_config),
            'AGENT_DISPATCH_JOBS': str(root / 'dispatch/jobs.log'),
        }
        child_env = worker_runtime_home.prepare_worker_home(
            Path(__file__).resolve().parents[1], 'claude', 'stage',
            'split-legacy-child', env=legacy_parent_env, destination=reused_home)
        if child_env.get(SECURE_ENV) != str(old_main_config.resolve()):
            raise RuntimeError('split legacy parent did not bind its new child to main')
        child_config = Path(child_env['CLAUDE_CONFIG_DIR'])
        trace, _ = harness.run_cli(
            'split-legacy-child', 'split-legacy-child', old_home, child_config,
            child_env[SECURE_ENV], injected_env=child_env, write_settings=False)
        child_generations = [event['access_generation'] for event in harness.state.events
                             if event.get('event') == 'message' and
                             event.get('prompt') == 'split-legacy-child']
        if not child_generations or set(child_generations) != {'2'}:
            raise RuntimeError('new child did not use the main store generation')
        if harness.state.refresh_count != 0:
            raise RuntimeError('new child refreshed from a split credential')
        if (old_worker_config / '.credentials.json').read_bytes() != old_worker_split or \
                reused_split_path.read_bytes() != reused_split:
            raise RuntimeError('split worker credentials were not preserved')
        if old_main_credential.read_bytes() != main_before_child:
            raise RuntimeError('new child changed the valid main credential')
        if old_worker['proc'].poll() is not None:
            raise RuntimeError('old split worker did not keep running')
        summary['scenarios']['split_legacy_parent_new_launch'] = {
            'actual_cli_pid': harness.cli_pid(trace, prompt='split-legacy-child'),
            'binding': 'legacy-parent-source-main',
            'child_used_main_store': True,
            'child_access_generations': sorted(set(child_generations)),
            'refresh_requests': harness.state.refresh_count,
            'split_files_preserved': True,
            'main_unchanged': True,
            'old_split_worker_running': True,
        }

        # The proxy never opens an upstream socket; every event is fixture-only.
        events = list(harness.state.events)
        summary['request_events'] = events
        summary['only_fixture_hosts'] = sorted(set(harness.state.all_hosts))
        summary['blocked_nonfixture_attempts'] = rejected
        (root / 'summary.json').write_text(
            json.dumps(summary, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        print(json.dumps(summary, sort_keys=True))
    finally:
        harness.cleanup()


class NativeClaudeStorage(unittest.TestCase):
    def test_installed_cli_uses_shared_native_store_and_lock(self):
        cli = os.environ.get('HEARTING_CLAUDE_CLI')
        required = os.environ.get('HEARTING_REQUIRE_CLAUDE_CLI', '').lower() in (
            '1', 'true', 'yes')
        if not cli or not Path(cli).is_file():
            message = 'set HEARTING_CLAUDE_CLI to an installed Claude Code binary'
            if required:
                self.fail('mandatory native CLI test unavailable: ' + message)
            self.skipTest(message)
        cli = str(Path(cli).resolve())
        for tool in ('/usr/bin/bwrap', '/usr/bin/strace', '/usr/bin/openssl'):
            if not Path(tool).is_file():
                self.fail(f'mandatory native CLI test requires {tool}')

        with tempfile.TemporaryDirectory(prefix='hearting-native-storage-') as temp:
            root = Path(temp)
            (root / 'hosts').write_text(
                '127.0.0.1 platform.claude.com api.anthropic.com\n', encoding='ascii')
            (root / 'resolv.conf').write_text('nameserver 127.0.0.1\n', encoding='ascii')
            sandbox_root = '/tmp/hearting-native-storage'
            command = [
                '/usr/bin/bwrap', '--unshare-all', '--die-with-parent',
                '--ro-bind', '/', '/', '--dev', '/dev', '--proc', '/proc',
                '--tmpfs', '/tmp', '--bind', str(root), sandbox_root,
                '--ro-bind', str(root / 'hosts'), '/etc/hosts',
                '--ro-bind', str(root / 'resolv.conf'), '/etc/resolv.conf',
                '--tmpfs', '/home/example/.claude',
                '--setenv', 'PATH', '/usr/bin:/bin',
                '--setenv', 'HOME', sandbox_root,
                '--chdir', sandbox_root,
                '--', '/usr/bin/python3', str(Path(__file__).resolve()),
                '--native-child', cli, sandbox_root,
            ]
            clean_env = {
                'PATH': '/usr/bin:/bin', 'HOME': temp,
                'PYTHONDONTWRITEBYTECODE': '1',
            }
            try:
                result = subprocess.run(
                    command, cwd=str(root), env=clean_env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, timeout=110)
            except subprocess.TimeoutExpired as error:
                self.fail('native CLI fixture exceeded 110 seconds: ' +
                          _clean_text(error.stdout or ''))
            self.assertEqual(result.returncode, 0, _clean_text(result.stdout[-6000:]))
            summary = json.loads((root / 'summary.json').read_text(encoding='utf-8'))
            self.assertEqual(summary['binary_sha256'], hashlib.sha256(
                Path(cli).read_bytes()).hexdigest())
            self.assertEqual(summary['egress_probe'], 'blocked')
            self.assertEqual(summary['scenarios']['legacy_symlink']['main_unchanged'], True)
            self.assertTrue(summary['scenarios']['legacy_symlink']['worker_link_replaced'])
            self.assertEqual(summary['scenarios']['empty_override']['store'], 'HOME/.claude')
            self.assertEqual(summary['scenarios']['relative_override']['store'],
                             'caller-cwd/relative-auth')
            self.assertTrue(summary['scenarios']['builder_spawn_consumer'][
                'source_store_updated'])
            self.assertTrue(summary['scenarios']['resume_transport']['shared_auth_store'])
            self.assertTrue(summary['scenarios']['late_refresh_cas'][
                'newer_main_write_survived_stale_refresh'])
            self.assertTrue(summary['scenarios']['legacy_running_refresh_cas'][
                'credential_link_survived'])
            self.assertTrue(summary['scenarios']['legacy_running_refresh_cas'][
                'main_token_pair_preserved'])
            self.assertTrue(summary['scenarios']['shared_store_race']['same_main_process_before_after'])
            self.assertTrue(summary['scenarios']['shared_store_race']['settings_env_overrode_decoy'])
            self.assertEqual(summary['scenarios']['shared_store_race']['refresh_requests'], 1)
            self.assertTrue(summary['scenarios']['post_lock_reread'][
                'successful_reacquire_then_reread'])
            self.assertEqual(summary['scenarios']['post_lock_reread']['refresh_requests'], 1)
            self.assertTrue(summary['scenarios']['old_running_worker_transition'][
                'main_login_not_overwritten'])
            self.assertTrue(summary['scenarios']['old_running_worker_transition'][
                'old_worker_remained_alive'])
            self.assertFalse(summary['scenarios']['old_running_worker_transition'][
                'old_process_environment_rebound'])
            self.assertTrue(summary['scenarios']['split_legacy_parent_new_launch'][
                'child_used_main_store'])
            self.assertTrue(summary['scenarios']['split_legacy_parent_new_launch'][
                'split_files_preserved'])
            self.assertTrue({'platform.claude.com', 'api.anthropic.com'}.issubset(
                set(summary['only_fixture_hosts'])))
            self.assertEqual(summary['blocked_nonfixture_attempts'], [])
            print('native_cli_evidence=' + _clean_text(result.stdout.strip().splitlines()[-1]))


if __name__ == '__main__' and len(sys.argv) >= 2 and sys.argv[1] == '--native-child':
    _native_child(sys.argv[2], sys.argv[3])
elif __name__ == '__main__':
    unittest.main()
